import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from local_first_common.cli import dry_run_option, json_option
from local_first_common.tracking import register_tool, timed_run
from rich.console import Console

from .core import SECTIONS, render, scan

TOOL_NAME = "rot-finder"
_TOOL = register_tool(TOOL_NAME)

DEFAULT_VAULTS = ["~/vaults/BrainSync", "~/vaults/Contexta", "~/vaults/KeySix"]
DEFAULT_REPOS_DIR = "~/projects/local-first"
DEFAULT_DB = "~/sync/local-first/processing_log.duckdb"
# Run on demand by design, so long silences are expected, not rot.
DEFAULT_IGNORE_TOOLS = ["model-comparison-harness", "template-tool", "fleet", "rot-finder"]

console = Console(stderr=True)
app = typer.Typer(help="Surfaces what's quietly rotting across the fleet and vaults: tools gone quiet, stale claims, aging deferrals, stalled work.")


def _repos(repos_dir: Path) -> list[Path]:
    return sorted(p for p in repos_dir.iterdir() if (p / ".git").exists())


@app.command()
def main(
    vault: Annotated[list[str] | None, typer.Option("--vault", help="Vault to scan (repeatable)")] = None,
    repos_dir: Annotated[str, typer.Option("--repos-dir", help="Directory of fleet git repos")] = DEFAULT_REPOS_DIR,
    db: Annotated[str, typer.Option("--db", help="processing_log DuckDB, for gone-quiet")] = DEFAULT_DB,
    ignore_tool: Annotated[list[str] | None, typer.Option("--ignore-tool", help="Tool that runs on demand (repeatable)")] = None,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Findings shown per section")] = 10,
    output: Annotated[str | None, typer.Option("--output", "-o", help="Also write the report to this file")] = None,
    as_json: Annotated[bool, json_option()] = False,
    dry_run: Annotated[bool, dry_run_option()] = False,
) -> None:
    """Scan vaults and fleet repos and print a rot report."""
    vaults = [Path(v).expanduser() for v in (vault or DEFAULT_VAULTS)]
    vaults = [v for v in vaults if v.exists()]
    now = datetime.now()  # noqa: DTZ005 - ages are compared against local file mtimes and naive DB timestamps
    with timed_run(TOOL_NAME, None, source_location=repos_dir) as run:
        result = scan(
            vaults,
            _repos(Path(repos_dir).expanduser()),
            Path(db).expanduser(),
            now,
            ignore_tools=ignore_tool if ignore_tool is not None else DEFAULT_IGNORE_TOOLS,
        )
        run.item_count = len(result.findings)

    if as_json:
        typer.echo(json.dumps({"findings": [asdict(f) for f in result.findings], "notes": result.notes}, indent=2))
    else:
        report = render(result, now, limit)
        typer.echo(report)
        if output and not dry_run:
            Path(output).expanduser().write_text(report, encoding="utf-8")
            console.print(f"[dim]Written: {output}[/dim]")
    console.print("[dim]" + ", ".join(f"{len(result.of(k))} {k}" for k, _, _ in SECTIONS) + "[/dim]")


if __name__ == "__main__":
    app()
