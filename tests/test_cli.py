from typer.testing import CliRunner

from rot_finder.cli import app


def test_default_invocation_exits_clean():
    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0
    assert "not yet implemented" in result.output
