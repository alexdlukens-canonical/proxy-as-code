"""Tests for applying rendered configuration and recovering from reload failures."""

import json
import subprocess
from io import StringIO
from unittest.mock import MagicMock

import pytest
from django.core.management import call_command
from terrasquid.api.management.commands import render_squid_config as render_command


@pytest.fixture()
def command(tmp_path, settings, monkeypatch):
    """Redirect configuration and status writes to an isolated directory."""
    monkeypatch.setattr(render_command, "SQUID_CONF", tmp_path / "squid.conf")
    monkeypatch.setattr(render_command, "SQUID_CONF_NEW", tmp_path / "squid.conf.new")
    settings.TERRASQUID_STATUS_FILE = str(tmp_path / "status.json")
    stdout, stderr = StringIO(), StringIO()
    return render_command.Command(stdout=stdout, stderr=stderr), stdout, stderr


@pytest.fixture()
def validate(monkeypatch):
    """Keep configuration validation independent from a local Squid installation."""
    validate = MagicMock(return_value=(True, ""))
    monkeypatch.setattr(render_command, "validate_squid_config", validate)
    return validate


@pytest.fixture()
def reload_squid(monkeypatch):
    """Prevent real systemd reloads."""
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(render_command.subprocess, "run", run)
    return run


def test_unchanged_config_updates_status_without_reload(command, tmp_path, validate, reload_squid):
    """Whitespace-only differences do not trigger validation or a reload."""
    instance, stdout, stderr = command
    original = "http_port 3128\n"
    (tmp_path / "squid.conf").write_text(original)

    instance._apply(original.strip(), 7)

    assert (tmp_path / "squid.conf").read_text() == original
    assert not (tmp_path / "squid.conf.new").exists()
    assert "unchanged" in stdout.getvalue()
    assert not stderr.getvalue()
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["applied_config_version"] == 7
    assert status["last_reload_ok"] is True
    validate.assert_not_called()
    reload_squid.assert_not_called()


def test_validation_failure_preserves_current_config(command, tmp_path, validate, reload_squid):
    """Invalid candidate configurations never replace the working file."""
    instance, _, stderr = command
    (tmp_path / "squid.conf").write_text("working config")
    validate.return_value = (False, "invalid ACL")

    with pytest.raises(SystemExit) as error:
        instance._apply("invalid config", 8)

    assert error.value.code == 1
    assert (tmp_path / "squid.conf").read_text() == "working config"
    assert not (tmp_path / "squid.conf.new").exists()
    assert not (tmp_path / "status.json").exists()
    assert "invalid ACL" in stderr.getvalue()
    reload_squid.assert_not_called()


@pytest.mark.parametrize("existing", [False, True])
def test_successful_apply_removes_backup(command, tmp_path, validate, reload_squid, existing):
    """A validated configuration replaces the old file and records success."""
    instance, stdout, stderr = command
    if existing:
        (tmp_path / "squid.conf").write_text("old config")

    instance._apply("http_port 3128\n", 9)

    assert (tmp_path / "squid.conf").read_text() == "http_port 3128\n"
    assert not (tmp_path / "squid.conf.bak").exists()
    assert not (tmp_path / "squid.conf.new").exists()
    assert "Squid reloaded successfully." in stdout.getvalue()
    assert not stderr.getvalue()
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["applied_config_version"] == 9
    assert status["last_reload_ok"] is True
    validate.assert_called_once_with("http_port 3128\n")
    reload_squid.assert_called_once_with(["systemctl", "reload-or-restart", "squid"], capture_output=True, text=True)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(("stdout", "stderr"), [("", "reload failed\n"), ("reload failed\n", "")])
def test_reload_failure_restores_backup_when_available(
    command, tmp_path, validate, reload_squid, existing, stdout, stderr
):
    """A reload failure is reported and the previous configuration is restored."""
    instance, _, error_output = command
    if existing:
        (tmp_path / "squid.conf").write_text("working config")
    reload_squid.return_value = subprocess.CompletedProcess([], 1, stdout, stderr)

    with pytest.raises(SystemExit) as error:
        instance._apply("new config", 10)

    assert error.value.code == 1
    assert (tmp_path / "squid.conf").read_text() == ("working config" if existing else "new config")
    assert not (tmp_path / "squid.conf.bak").exists()
    assert not (tmp_path / "squid.conf.new").exists()
    assert "Squid reload failed: reload failed" in error_output.getvalue()
    assert ("Restored previous" in error_output.getvalue()) is existing
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["applied_config_version"] == 10
    assert status["last_reload_ok"] is False
    validate.assert_called_once_with("new config")


@pytest.mark.django_db
@pytest.mark.parametrize("output", ["-", "file"])
def test_output_mode_does_not_apply_config(command, tmp_path, monkeypatch, reload_squid, output):
    """Exporting rendered text does not change the live configuration or reload."""
    _, stdout, stderr = command
    rendered = "http_port 3128\n"
    render = MagicMock(return_value=rendered)
    monkeypatch.setattr(render_command, "render_squid_config", render)
    destination = "-" if output == "-" else str(tmp_path / "export.conf")

    call_command("render_squid_config", output=destination, stdout=stdout, stderr=stderr)

    if output == "-":
        assert stdout.getvalue() == rendered
    else:
        assert (tmp_path / "export.conf").read_text() == rendered
        assert f"Wrote config to {destination}" in stdout.getvalue()
    assert not (tmp_path / "squid.conf").exists()
    assert not (tmp_path / "status.json").exists()
    assert not stderr.getvalue()
    render.assert_called_once_with()
    reload_squid.assert_not_called()
