"""Tests for Squid filesystem, service, and validation helpers."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from terrasquid.api.squid_render import validate_squid_config

import squid


@pytest.fixture()
def run_process(monkeypatch):
    """Prevent tests from invoking real system services or Squid."""
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(subprocess, "run", run)
    return run


@pytest.fixture()
def squid_binary(settings, tmp_path):
    """Provide an existing binary path without requiring a Squid installation."""
    binary = tmp_path / "squid"
    binary.touch()
    settings.SQUID_BINARY = str(binary)
    return binary


def test_install_squid_installs_required_packages(run_process):
    """Installation includes the exporter and log rotation support."""
    squid.install_squid()

    run_process.assert_called_once_with(
        ["apt-get", "install", "-y", "squid", "prometheus-squid-exporter", "logrotate"], check=True
    )


def test_install_squid_propagates_package_failure(run_process):
    """Package installation failures must not look like success."""
    run_process.side_effect = subprocess.CalledProcessError(1, "apt-get")

    with pytest.raises(subprocess.CalledProcessError):
        squid.install_squid()


@pytest.mark.parametrize("existing", [False, True])
def test_write_config_atomically_replaces_target(tmp_path, existing):
    """Writing creates parent directories and replaces any previous configuration."""
    path = tmp_path / "squid" / "squid.conf"
    if existing:
        path.parent.mkdir()
        path.write_text("old config")

    squid.write_squid_config("http_port 3128\n", path)

    assert path.read_text() == "http_port 3128\n"
    assert not path.with_suffix(".tmp").exists()


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    [
        (0, "", "", (True, "")),
        (1, "fallback\n", " reload failed\n", (False, "reload failed")),
        (1, " fallback\n", "", (False, "fallback")),
    ],
)
def test_reload_reports_process_result(run_process, returncode, stdout, stderr, expected):
    """Reload failures report stderr, falling back to stdout when necessary."""
    run_process.return_value = subprocess.CompletedProcess([], returncode, stdout, stderr)

    assert squid.reload_squid() == expected
    run_process.assert_called_once_with(["systemctl", "reload-or-restart", "squid"], capture_output=True, text=True)


@pytest.mark.parametrize(("returncode", "expected"), [(0, True), (3, False)])
def test_service_running_checks_systemd(run_process, returncode, expected):
    """Only a successful is-active result means Squid is running."""
    run_process.return_value = subprocess.CompletedProcess([], returncode)

    assert squid.squid_service_running() is expected
    run_process.assert_called_once_with(["systemctl", "is-active", "--quiet", "squid"], capture_output=True)


def test_validation_skips_missing_binary(settings, tmp_path, run_process):
    """Validation is optional on hosts without the Squid binary."""
    settings.SQUID_BINARY = str(tmp_path / "missing-squid")

    assert validate_squid_config("http_port 3128\n") == (True, "")
    run_process.assert_not_called()


def test_validation_parses_temporary_config_and_cleans_it_up(squid_binary, run_process):
    """Squid parses the supplied text from a temporary file that is then removed."""
    config = "http_port 3128\n"

    def parse_config(command, **kwargs):
        assert command[:4] == [str(squid_binary), "-k", "parse", "-f"]
        assert Path(command[4]).read_text() == config
        assert kwargs == {"capture_output": True, "text": True, "timeout": 10}
        return subprocess.CompletedProcess(command, 0, "", "")

    run_process.side_effect = parse_config

    assert validate_squid_config(config) == (True, "")
    temporary_config = Path(run_process.call_args.args[0][4])
    assert not temporary_config.parent.exists()


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [("fallback\n", " invalid config\n", "invalid config"), (" parse failed\n", "", "parse failed")],
)
def test_validation_reports_parse_failure(squid_binary, run_process, stdout, stderr, expected):
    """Invalid configurations return the parser's diagnostic."""
    run_process.return_value = subprocess.CompletedProcess([], 1, stdout, stderr)

    assert validate_squid_config("invalid") == (False, expected)
    assert run_process.call_args.args[0][0] == str(squid_binary)
    assert not Path(run_process.call_args.args[0][4]).exists()


def test_validation_reports_timeout(squid_binary, run_process):
    """A hung parser returns a clear failure and removes the temporary file."""
    run_process.side_effect = subprocess.TimeoutExpired(str(squid_binary), 10)

    assert validate_squid_config("http_port 3128\n") == (False, "Squid config validation timed out.")
    assert not Path(run_process.call_args.args[0][4]).exists()
