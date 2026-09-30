from typing import Annotated

import typer
from local_first_common.cli import (
    dry_run_option,
    model_option,
    no_llm_option,
    provider_option,
    resolve_dry_run,
)
from local_first_common.tracking import register_tool
from rich.console import Console

from .core import run

TOOL_NAME = "rot-finder"
_TOOL = register_tool(TOOL_NAME)

console = Console(stderr=True)
app = typer.Typer(help="Surfaces what's quietly rotting across the fleet and vaults: tools gone quiet, stale claims, aging deferrals, stalled work")


@app.command()
def main(
    provider: Annotated[str, provider_option()] = "ollama",
    model: Annotated[str | None, model_option()] = None,
    dry_run: Annotated[bool, dry_run_option()] = False,
    no_llm: Annotated[bool, no_llm_option()] = False,
) -> None:
    """Surfaces what's quietly rotting across the fleet and vaults: tools gone quiet, stale claims, aging deferrals, stalled work"""
    dry_run = resolve_dry_run(dry_run, no_llm)
    result = run(dry_run=dry_run)
    console.print(result)


if __name__ == "__main__":
    app()
