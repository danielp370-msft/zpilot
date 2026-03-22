"""Tests for `zpilot install-zellij-plugin` CLI command."""

import sys

sys.path.insert(0, "src")

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from zpilot.cli import main


@pytest.fixture
def runner():
    return CliRunner(mix_stderr=False)


class TestInstallZellijPluginHelp:
    def test_help(self, runner):
        result = runner.invoke(main, ["install-zellij-plugin", "--help"])
        assert result.exit_code == 0
        assert "WASM" in result.output or "wasm" in result.output.lower()
        assert "--source" in result.output
        assert "--config" in result.output


class TestInstallZellijPlugin:
    def test_install_copies_wasm(self, runner, tmp_path):
        """Plugin file is copied to ~/.config/zellij/plugins/."""
        wasm_src = tmp_path / "zpilot_zellij_plugin.wasm"
        wasm_src.write_bytes(b"fake-wasm-content")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        plugin_dir = fake_home / ".config" / "zellij" / "plugins"

        with patch("pathlib.Path.home", return_value=fake_home):
            result = runner.invoke(main, [
                "install-zellij-plugin", "--source", str(wasm_src),
            ])

        assert result.exit_code == 0
        assert "Installed" in result.output or "✅" in result.output
        dest = plugin_dir / "zpilot_zellij_plugin.wasm"
        assert dest.exists()
        assert dest.read_bytes() == b"fake-wasm-content"

    def test_install_creates_plugin_dir(self, runner, tmp_path):
        """Plugin directory is created if it doesn't exist."""
        wasm_src = tmp_path / "zpilot_zellij_plugin.wasm"
        wasm_src.write_bytes(b"wasm-data")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        plugin_dir = fake_home / ".config" / "zellij" / "plugins"
        assert not plugin_dir.exists()

        with patch("pathlib.Path.home", return_value=fake_home):
            result = runner.invoke(main, [
                "install-zellij-plugin", "--source", str(wasm_src),
            ])

        assert result.exit_code == 0
        assert plugin_dir.exists()

    def test_install_wasm_not_found(self, runner, tmp_path):
        """Error if WASM file doesn't exist at any search path."""
        fake_pkg_dir = tmp_path / "pkg"
        fake_pkg_dir.mkdir()
        # __file__ points to a non-existent wasm in pkg dir and fallback
        with patch("zpilot.cli.__file__", str(fake_pkg_dir / "cli.py")):
            result = runner.invoke(main, ["install-zellij-plugin"])

        assert result.exit_code != 0
        assert "not found" in result.stderr or "❌" in result.stderr

    def test_install_auto_finds_in_package_dir(self, runner, tmp_path):
        """Auto-finds WASM in the zpilot package directory."""
        fake_pkg_dir = tmp_path / "src" / "zpilot"
        fake_pkg_dir.mkdir(parents=True)
        wasm_file = fake_pkg_dir / "zpilot_zellij_plugin.wasm"
        wasm_file.write_bytes(b"pkg-wasm")
        cli_file = fake_pkg_dir / "cli.py"
        cli_file.write_text("")

        fake_home = tmp_path / "home"
        fake_home.mkdir()

        with patch("zpilot.cli.__file__", str(cli_file)), \
             patch("pathlib.Path.home", return_value=fake_home):
            result = runner.invoke(main, ["install-zellij-plugin"])

        assert result.exit_code == 0
        dest = fake_home / ".config" / "zellij" / "plugins" / "zpilot_zellij_plugin.wasm"
        assert dest.exists()
        assert dest.read_bytes() == b"pkg-wasm"

    def test_install_fallback_to_build_dir(self, runner, tmp_path):
        """Falls back to zpilot-zellij-plugin build directory."""
        fake_pkg_dir = tmp_path / "src" / "zpilot"
        fake_pkg_dir.mkdir(parents=True)
        cli_file = fake_pkg_dir / "cli.py"
        cli_file.write_text("")

        # Create fallback build path
        build_dir = (
            tmp_path
            / "zpilot-zellij-plugin"
            / "target"
            / "wasm32-wasip1"
            / "release"
        )
        build_dir.mkdir(parents=True)
        wasm_file = build_dir / "zpilot_zellij_plugin.wasm"
        wasm_file.write_bytes(b"build-wasm")

        fake_home = tmp_path / "home"
        fake_home.mkdir()

        with patch("zpilot.cli.__file__", str(cli_file)), \
             patch("pathlib.Path.home", return_value=fake_home):
            result = runner.invoke(main, ["install-zellij-plugin"])

        assert result.exit_code == 0
        dest = fake_home / ".config" / "zellij" / "plugins" / "zpilot_zellij_plugin.wasm"
        assert dest.exists()
        assert dest.read_bytes() == b"build-wasm"


class TestInstallZellijPluginConfig:
    def test_config_creates_new_config(self, runner, tmp_path):
        """--config creates config.kdl if it doesn't exist."""
        wasm_src = tmp_path / "zpilot_zellij_plugin.wasm"
        wasm_src.write_bytes(b"wasm")

        fake_home = tmp_path / "home"
        fake_home.mkdir()

        with patch("pathlib.Path.home", return_value=fake_home):
            result = runner.invoke(main, [
                "install-zellij-plugin", "--source", str(wasm_src), "--config",
            ])

        assert result.exit_code == 0
        config_path = fake_home / ".config" / "zellij" / "config.kdl"
        assert config_path.exists()
        content = config_path.read_text()
        assert "zpilot" in content
        assert "plugins" in content

    def test_config_updates_existing_config(self, runner, tmp_path):
        """--config appends plugin block to existing config."""
        wasm_src = tmp_path / "zpilot_zellij_plugin.wasm"
        wasm_src.write_bytes(b"wasm")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        config_dir = fake_home / ".config" / "zellij"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "config.kdl"
        config_path.write_text("theme \"catppuccin\"\n")

        with patch("pathlib.Path.home", return_value=fake_home):
            result = runner.invoke(main, [
                "install-zellij-plugin", "--source", str(wasm_src), "--config",
            ])

        assert result.exit_code == 0
        content = config_path.read_text()
        assert "catppuccin" in content  # original preserved
        assert "zpilot" in content  # plugin added

    def test_config_skips_if_already_present(self, runner, tmp_path):
        """--config doesn't duplicate if zpilot already referenced."""
        wasm_src = tmp_path / "zpilot_zellij_plugin.wasm"
        wasm_src.write_bytes(b"wasm")

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        config_dir = fake_home / ".config" / "zellij"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "config.kdl"
        config_path.write_text('plugins {\n    zpilot location="file:foo"\n}\n')

        with patch("pathlib.Path.home", return_value=fake_home):
            result = runner.invoke(main, [
                "install-zellij-plugin", "--source", str(wasm_src), "--config",
            ])

        assert result.exit_code == 0
        assert "already references" in result.output or "ℹ️" in result.output
