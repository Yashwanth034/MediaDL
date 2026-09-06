from typer.testing import CliRunner

from mediadl import __version__
from mediadl.cli.app import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert f"MediaDL {__version__}" in result.stdout


def test_help() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Download single videos, playlists, and channels cleanly." in result.stdout
    assert "--version" in result.stdout
