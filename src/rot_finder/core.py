"""Pure domain logic for rot-finder -- no CLI/Typer imports here.

`fleet status` reports what is failing. This reports what looks fine and isn't:
tools that stopped running without erroring, docs whose claims the code has
since contradicted, deferrals nobody came back to, and work marked in progress
that nobody has touched. Deterministic -- no LLM -- so it is cheap to run weekly.
"""

import os
import re
import statistics
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from pathlib import Path

import duckdb
import frontmatter

EXCLUDE_DIRS = frozenset(
    {
        "archive",
        "archived",
        ".obsidian",
        ".trash",
        ".git",
        ".venv",
        "node_modules",
        "results",
        "templates",
        "Templates",
        "sessions",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "examples",
        "fixtures",
    }
)
PLANNING_DOC_RE = re.compile(r"^(?:_?notes|roadmap|todo|plan|backlog|next|readme)\b.*\.md$", re.IGNORECASE)

# Not "active": Contexta uses it for "this claim is current", not work in progress.
ACTIVE_STATUSES = frozenset({"developing", "in-progress", "in progress", "doing", "wip", "drafting"})

_CLAIM_RE = re.compile(
    r"nothing uses|not (?:yet )?used|\bunused\b|isn't used|never used|not (?:yet )?(?:implemented|wired up|built)",
    re.IGNORECASE,
)
_IDENT_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{3,})(?:\(\))?`")
# CamelCase, snake_case or UPPER_CASE -- not plain words like `draft` that name statuses.
_CODE_LIKE_RE = re.compile(r"_|[a-z][A-Z]|^[A-Z][a-z]+[A-Z]")
# A claim on a long line with many identifiers is usually about one of them; guessing
# which produced mostly false positives, so only short, focused lines count.
MAX_CLAIM_LINE = 300
MAX_CLAIM_IDENTS = 2
_DOC_DEFERRAL_RE = re.compile(
    r"not worth fixing|decided not to (?:fix|bother)|won'?t fix|known (?:dead )?bug|revisit (?:this )?later",
    re.IGNORECASE,
)
# Character classes ([T]ODO, ...) keep this line from matching itself.
_CODE_DEFERRAL_RE = re.compile(
    r"#\s*(?:[T]ODO|[F]IXME|[H]ACK|[X]XX)\b|#.*\b(?:[w]orkaround|[n]ot worth fixing)\b", re.IGNORECASE
)
_UNCHECKED_RE = re.compile(r"^\s*[-*] \[ \] ")


@dataclass
class Finding:
    kind: str  # gone-quiet | stale-claim | deferral | stalled
    where: str
    what: str
    age_days: int | None = None


@dataclass
class Scan:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def of(self, kind: str) -> list[Finding]:
        return sorted((f for f in self.findings if f.kind == kind), key=lambda f: -(f.age_days or 0))


# --- file walking and dating ---------------------------------------------------


def iter_files(roots: Iterable[Path], suffix: str) -> Iterator[Path]:
    for root in roots:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith("."))
            for name in sorted(files):
                if name.endswith(suffix):
                    yield Path(dirpath) / name


def numbered_lines(path: Path) -> Iterator[tuple[int, str]]:
    """(lineno, line) pairs, skipping fenced code blocks in markdown -- READMEs show
    sample output there, and a checkbox or claim in an example isn't the author's."""
    in_fence = False
    is_markdown = path.suffix == ".md"
    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
        if is_markdown and line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield lineno, line


def mtime_age(path: Path, now: datetime) -> int:
    return (now - datetime.fromtimestamp(path.stat().st_mtime)).days  # noqa: DTZ006 - naive local time, same as processing_log's timestamps


def blame_ages(path: Path, now: datetime) -> dict[int, int] | None:
    """Age in days of every line (1-based) per `git blame`, or None outside git or for untracked files."""
    proc = subprocess.run(
        ["git", "blame", "--line-porcelain", path.name],
        cwd=path.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    ages: dict[int, int] = {}
    line_no = None
    for line in proc.stdout.splitlines():
        parts = line.split(" ")
        if len(parts) >= 3 and len(parts[0]) == 40 and parts[1].isdigit():
            line_no = int(parts[2])
        elif line.startswith("author-time ") and line_no is not None:
            ages[line_no] = (now - datetime.fromtimestamp(int(line.split()[1]))).days  # noqa: DTZ006 - naive local time, same as processing_log's timestamps
    return ages


def _line_age(path: Path, lineno: int, now: datetime, cache: dict[Path, dict[int, int] | None]) -> int:
    if path not in cache:
        cache[path] = blame_ages(path, now)
    ages = cache[path]
    return ages.get(lineno, mtime_age(path, now)) if ages else mtime_age(path, now)


# --- detector 1: gone quiet ----------------------------------------------------


def load_runs(db_path: Path) -> dict[str, list[datetime]]:
    with duckdb.connect(str(db_path), read_only=True) as conn:
        rows = conn.execute("SELECT tool_name, created_at FROM processing_log WHERE success").fetchall()
    runs: dict[str, list[datetime]] = {}
    for tool, created in rows:
        runs.setdefault(tool, []).append(created)
    return runs


def gone_quiet(
    runs: dict[str, list[datetime]],
    now: datetime,
    min_run_days: int = 5,
    factor: float = 4.0,
    min_silence_days: int = 14,
    ignore: Iterable[str] = (),
) -> list[Finding]:
    """Tools whose current silence is far longer than their own usual rhythm.

    Runs are collapsed to distinct days so one busy afternoon doesn't read as a
    daily cadence. Not failing -- just stopped, which `fleet status` can't see.
    """
    ignored = set(ignore)
    findings = []
    for tool, times in sorted(runs.items()):
        if tool in ignored:
            continue
        days = sorted({t.date() for t in times})
        if len(days) < min_run_days:
            continue
        typical = statistics.median((b - a).days for a, b in pairwise(days))
        silence = (now.date() - days[-1]).days
        if silence > max(min_silence_days, factor * typical):
            findings.append(
                Finding(
                    "gone-quiet",
                    tool,
                    f"ran on {len(days)} days, usually every ~{typical:g}d -- silent since {days[-1]}",
                    silence,
                )
            )
    return findings


# --- detector 2: stale claims --------------------------------------------------


def load_code(code_roots: Iterable[Path]) -> list[tuple[Path, str]]:
    """Non-test source: a symbol only exercised by its own tests is still unused."""
    return [
        (p, p.read_text(encoding="utf-8", errors="ignore"))
        for p in iter_files(code_roots, ".py")
        if "tests" not in p.parts and not p.name.startswith("test_")
    ]


def stale_claims(doc_roots: Iterable[Path], code: list[tuple[Path, str]], now: datetime) -> list[Finding]:
    """Docs saying a `symbol` is unused or unbuilt while other code imports or calls it."""
    findings = []
    for doc in iter_files(doc_roots, ".md"):
        for lineno, line in numbered_lines(doc):
            if len(line) > MAX_CLAIM_LINE or not _CLAIM_RE.search(line):
                continue
            idents = [i for i in dict.fromkeys(_IDENT_RE.findall(line)) if _CODE_LIKE_RE.search(i)]
            if len(idents) > MAX_CLAIM_IDENTS:
                continue
            for ident in idents:
                pattern = re.compile(rf"\b{re.escape(ident)}\b")
                defines = re.compile(rf"^\s*(?:class|def|async def)\s+{re.escape(ident)}\b", re.MULTILINE)
                users = [p for p, text in code if pattern.search(text) and not defines.search(text)]
                if users:
                    findings.append(
                        Finding(
                            "stale-claim",
                            f"{doc}:{lineno}",
                            f"says `{ident}` is unused/unbuilt, but {len(users)} file(s) use it, e.g. {users[0]}",
                            mtime_age(doc, now),
                        )
                    )
    return findings


# --- detector 3: aging deferrals -----------------------------------------------


def deferrals(
    doc_roots: Iterable[Path], code_roots: Iterable[Path], now: datetime, min_age_days: int = 30
) -> list[Finding]:
    """Written-down decisions not to deal with something, dated so their age is visible."""
    findings = []
    cache: dict[Path, dict[int, int] | None] = {}
    sources = [(p, _DOC_DEFERRAL_RE) for p in iter_files(doc_roots, ".md")]
    sources += [(p, _CODE_DEFERRAL_RE) for p in iter_files(code_roots, ".py")]
    for path, regex in sources:
        for lineno, line in numbered_lines(path):
            if not regex.search(line):
                continue
            age = _line_age(path, lineno, now, cache)
            if age >= min_age_days:
                findings.append(Finding("deferral", f"{path}:{lineno}", line.strip()[:160], age))
    return findings


# --- detector 4: stalled work --------------------------------------------------


def unchecked_items(repo_roots: Iterable[Path], now: datetime, min_age_days: int = 60) -> list[Finding]:
    """Unchecked `- [ ]` boxes in repo planning docs, aged by when the line was written."""
    findings = []
    cache: dict[Path, dict[int, int] | None] = {}
    for path in iter_files(repo_roots, ".md"):
        if not PLANNING_DOC_RE.match(path.name):
            continue
        for lineno, line in numbered_lines(path):
            if _UNCHECKED_RE.match(line):
                age = _line_age(path, lineno, now, cache)
                if age >= min_age_days:
                    findings.append(Finding("stalled", f"{path}:{lineno}", line.strip()[:160], age))
    return findings


def stalled_notes(vault_roots: Iterable[Path], now: datetime, min_age_days: int = 90) -> list[Finding]:
    """Vault notes whose frontmatter says in-progress but that nobody has touched."""
    findings = []
    for path in iter_files(vault_roots, ".md"):
        try:
            meta = frontmatter.load(path).metadata
        except Exception:  # noqa: BLE001, S112 - malformed frontmatter isn't this detector's problem
            continue
        status = str(meta.get("status", "")).strip().lower()
        if status not in ACTIVE_STATUSES:
            continue
        age = mtime_age(path, now)
        if age >= min_age_days:
            title = meta.get("title") or path.stem
            findings.append(Finding("stalled", str(path), f"status: {status} -- {title}", age))
    return findings


# --- orchestration and rendering -----------------------------------------------


def scan(
    vault_roots: list[Path],
    repo_roots: list[Path],
    db_path: Path | None,
    now: datetime,
    ignore_tools: Iterable[str] = (),
) -> Scan:
    result = Scan()
    if db_path and db_path.exists():
        try:
            result.findings += gone_quiet(load_runs(db_path), now, ignore=ignore_tools)
        except duckdb.Error as e:
            result.notes.append(f"gone-quiet skipped: couldn't read {db_path} ({e.__class__.__name__}, likely in use)")
    else:
        result.notes.append(f"gone-quiet skipped: no processing log at {db_path}")
    code_roots = [r for r in repo_roots if r.exists()]
    code = load_code(code_roots)
    doc_roots = vault_roots + code_roots
    result.findings += stale_claims(doc_roots, code, now)
    result.findings += deferrals(doc_roots, code_roots, now)
    result.findings += unchecked_items(code_roots, now)
    result.findings += stalled_notes(vault_roots, now)
    return result


SECTIONS = [
    ("gone-quiet", "Gone quiet", "Not failing -- just stopped. Still wanted? Restart it or retire it on purpose."),
    ("stale-claim", "Stale claims", "Docs that say something is unused or unbuilt while the code says otherwise."),
    ("deferral", "Aging deferrals", "Things decided not to deal with, and how long ago."),
    ("stalled", "Stalled work", "Marked in progress or left unchecked, and untouched since."),
]


def _short(where: str, home: str) -> str:
    return where.replace(home, "~")


def render(result: Scan, now: datetime, limit: int = 10) -> str:
    home = str(Path.home())
    counts = ", ".join(f"{len(result.of(k))} {title.lower()}" for k, title, _ in SECTIONS)
    lines = [f"# Rot report -- {now.date()}", "", counts, ""]
    for kind, title, blurb in SECTIONS:
        items = result.of(kind)
        lines += [f"## {title} ({len(items)})", f"_{blurb}_", ""]
        for f in items[:limit]:
            age = f"{f.age_days}d" if f.age_days is not None else "?"
            lines.append(f"- **{age}** `{_short(f.where, home)}` -- {f.what}")
        if len(items) > limit:
            lines.append(f"- ... and {len(items) - limit} more (`--limit` to see them)")
        lines.append("")
    for note in result.notes:
        lines.append(f"> {note}")
    return "\n".join(lines).rstrip() + "\n"
