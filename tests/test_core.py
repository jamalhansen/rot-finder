import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from rot_finder.core import (
    Finding,
    Scan,
    deferrals,
    gone_quiet,
    load_code,
    load_runs,
    render,
    scan,
    stale_claims,
    stalled_notes,
    unchecked_items,
)

NOW = datetime(2026, 9, 29, 12, 0)  # noqa: DTZ001 - the tool works in naive local time


def _write(path: Path, text: str, age_days: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if age_days is not None:
        t = (NOW - timedelta(days=age_days)).timestamp()
        os.utime(path, (t, t))
    return path


def _days(*offsets: int) -> list[datetime]:
    return [NOW - timedelta(days=d) for d in offsets]


class TestGoneQuiet:
    def test_flags_silence_far_beyond_usual_rhythm(self):
        runs = {"pebble": _days(170, 169, 168, 167, 166)}
        [f] = gone_quiet(runs, NOW)
        assert f.where == "pebble"
        assert f.age_days == 166
        assert "usually every ~1d" in f.what

    def test_weekly_tool_on_schedule_is_fine(self):
        assert gone_quiet({"weekly": _days(35, 28, 21, 14, 7)}, NOW) == []

    def test_many_runs_on_one_day_are_one_run_day(self):
        busy = [NOW - timedelta(days=100, minutes=m) for m in range(50)]
        assert gone_quiet({"burst": busy}, NOW) == []

    def test_ignored_tools_skipped(self):
        assert gone_quiet({"pebble": _days(170, 169, 168, 167, 166)}, NOW, ignore=["pebble"]) == []

    def test_load_runs_reads_successful_runs_only(self, tmp_path):
        db = tmp_path / "log.duckdb"
        with duckdb.connect(str(db)) as c:
            c.execute("CREATE TABLE processing_log (tool_name TEXT, created_at TIMESTAMP, success BOOLEAN)")
            c.execute("INSERT INTO processing_log VALUES ('a', ?, true), ('a', ?, false)", [NOW, NOW])
        assert load_runs(db) == {"a": [NOW]}


class TestStaleClaims:
    @pytest.fixture
    def code(self, tmp_path):
        _write(tmp_path / "repo/src/pkg/models.py", "class ContentMetadata:\n    pass\n")
        _write(tmp_path / "repo/src/pkg/tagger.py", "from pkg.models import ContentMetadata\n")
        _write(tmp_path / "repo/tests/test_only.py", "OnlyTested = 1\n")
        return load_code([tmp_path / "repo"])

    def test_catches_claim_contradicted_by_a_real_user(self, tmp_path, code):
        _write(tmp_path / "vault/status.md", "| `ContentMetadata` model | Nothing uses it yet |\n", age_days=169)
        [f] = stale_claims([tmp_path / "vault"], code, NOW)
        assert f.where.endswith("status.md:1")
        assert "1 file(s)" in f.what and f.age_days == 169

    def test_defining_file_and_tests_do_not_count_as_users(self, tmp_path, code):
        _write(tmp_path / "vault/a.md", "`OnlyTested` is unused.\n")
        assert stale_claims([tmp_path / "vault"], code, NOW) == []

    def test_plain_words_and_long_multi_symbol_lines_ignored(self, tmp_path, code):
        _write(tmp_path / "vault/a.md", "Status `draft` is unused.\n")
        long_line = "`ContentMetadata` `pkg_a` `pkg_b` all exist; something else is not used.\n"
        _write(tmp_path / "vault/b.md", long_line)
        assert stale_claims([tmp_path / "vault"], code, NOW) == []


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args, date=None):
        env = {**os.environ, "GIT_AUTHOR_DATE": date or "", "GIT_COMMITTER_DATE": date or ""} if date else None
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env=env)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    _write(root / "ROADMAP.md", "- [ ] old item\n- [x] done item\n")
    _write(root / "src/pkg/core.py", "x = 1  # TODO: handle unicode\n")
    _write(root / "tests/fixtures/sample.md", "- [ ] fixture checkbox\n")
    git("add", ".")
    old = (NOW - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%S")
    git("commit", "-qm", "init", date=old)
    return root


class TestAgedByGitBlame:
    def test_unchecked_items_in_planning_docs_only(self, repo):
        findings = unchecked_items([repo], NOW)
        assert [f.what for f in findings] == ["- [ ] old item"]
        assert findings[0].age_days == 120

    def test_young_items_not_reported(self, repo):
        assert unchecked_items([repo], NOW, min_age_days=200) == []

    def test_code_deferral_aged_by_blame(self, repo):
        [f] = deferrals([], [repo], NOW)
        assert "TODO: handle unicode" in f.what and f.age_days == 120

    def test_doc_deferral_phrase(self, tmp_path):
        _write(tmp_path / "v/n.md", "Decided not worth fixing standalone.\nWe parked it.\n", age_days=40)
        [f] = deferrals([tmp_path / "v"], [], NOW)
        assert f.where.endswith("n.md:1")


class TestStalledNotes:
    def test_in_progress_status_untouched(self, tmp_path):
        _write(tmp_path / "v/seed.md", "---\nstatus: developing\ntitle: Starter kit\n---\nbody\n", age_days=158)
        _write(tmp_path / "v/claim.md", "---\nstatus: active\n---\nbody\n", age_days=300)
        _write(tmp_path / "v/fresh.md", "---\nstatus: developing\n---\nbody\n", age_days=5)
        [f] = stalled_notes([tmp_path / "v"], NOW)
        assert "Starter kit" in f.what and f.age_days == 158


def test_scan_notes_missing_db_and_renders_sections(tmp_path):
    result = scan([tmp_path], [], tmp_path / "missing.duckdb", NOW)
    assert any("gone-quiet skipped" in n for n in result.notes)
    text = render(result, NOW)
    assert "# Rot report -- 2026-09-29" in text
    assert "## Gone quiet (0)" in text


def test_render_orders_by_age_and_caps(tmp_path):
    result = Scan(findings=[Finding("stalled", f"f{i}", "x", i) for i in range(5)])
    text = render(result, NOW, limit=2)
    assert text.index("**4d**") < text.index("**3d**")
    assert "and 3 more" in text
