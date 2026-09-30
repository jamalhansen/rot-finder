import json

from typer.testing import CliRunner

from rot_finder.cli import app

runner = CliRunner()


def _args(tmp_path):
    (tmp_path / "vault").mkdir()
    (tmp_path / "repos").mkdir()
    return ["--vault", str(tmp_path / "vault"), "--repos-dir", str(tmp_path / "repos"), "--db", str(tmp_path / "none.duckdb")]


def test_report_and_output_file(tmp_path):
    out = tmp_path / "rot.md"
    result = runner.invoke(app, [*_args(tmp_path), "--output", str(out)])
    assert result.exit_code == 0
    assert "# Rot report" in result.stdout
    assert out.read_text().startswith("# Rot report")


def test_dry_run_skips_output_file(tmp_path):
    out = tmp_path / "rot.md"
    runner.invoke(app, [*_args(tmp_path), "--output", str(out), "--dry-run"])
    assert not out.exists()


def test_json_is_pure(tmp_path):
    result = runner.invoke(app, [*_args(tmp_path), "--json"])
    data = json.loads(result.stdout)
    assert data["findings"] == []
    assert data["notes"]
