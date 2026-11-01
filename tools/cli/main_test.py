"""Smoke tests for tools/cli/main.py."""

from unittest.mock import patch

import pytest  # noqa: F401
from typer.testing import CliRunner

from tools.cli.main import app, main


class TestMainHelp:
    def test_help_exits_zero(self):
        """Top-level --help returns exit code 0 and shows the app description."""
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "Token-efficient" in result.output

    def test_knowledge_subcommand_help_exits_zero(self):
        """knowledge --help returns exit code 0 without real auth."""
        runner = CliRunner()
        result = runner.invoke(app, ["knowledge", "--help"])
        assert result.exit_code == 0
        assert "search" in result.output
        assert "note" in result.output


class TestMainFunction:
    def test_main_delegates_to_app(self):
        """main() simply calls app() — verify the delegation."""
        with patch("tools.cli.main.app") as mock_app:
            main()
        mock_app.assert_called_once_with()

    def test_main_passes_through_app_return_value(self):
        """main() propagates whatever app() returns (normally None)."""
        with patch("tools.cli.main.app", return_value=None):
            result = main()
        assert result is None


class TestSubcommandRegistration:
    def test_knowledge_subcommand_appears_in_top_level_help(self):
        """'knowledge' sub-group is listed in the top-level help text."""
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "knowledge" in result.output

    def test_dead_letters_subcommand_listed_under_knowledge(self):
        """'dead-letters' command appears when listing knowledge subcommands."""
        runner = CliRunner()
        result = runner.invoke(app, ["knowledge", "--help"])
        assert result.exit_code == 0
        assert "dead-letters" in result.output

    def test_replay_subcommand_listed_under_knowledge(self):
        """'replay' command appears when listing knowledge subcommands."""
        runner = CliRunner()
        result = runner.invoke(app, ["knowledge", "--help"])
        assert result.exit_code == 0
        assert "replay" in result.output

    def test_no_args_prints_help(self):
        """Invoking with no arguments prints the help text (no_args_is_help=True)."""
        runner = CliRunner()
        result = runner.invoke(app, [])
        # no_args_is_help=True: help is printed (exit 0), not an error
        assert "knowledge" in result.output
        assert "Token-efficient" in result.output

    def test_unknown_subcommand_exits_nonzero(self):
        """An unrecognised subcommand exits with a non-zero code."""
        runner = CliRunner()
        result = runner.invoke(app, ["does-not-exist"])
        assert result.exit_code != 0
