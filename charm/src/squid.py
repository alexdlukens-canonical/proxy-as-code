"""Squid process and configuration management helpers."""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

SQUID_SERVICE = "squid"
SQUID_EXPORTER_SERVICE = "terrasquid-squid-exporter"
SQUID_CONF_PATH = Path("/etc/squid/squid.conf")
SQUID_SPOOL_DIR = Path("/var/spool/squid")


def install_squid() -> None:
    """Install Squid, its Prometheus exporter, and log rotation support."""
    subprocess.run(["apt-get", "install", "-y", "squid", "prometheus-squid-exporter", "logrotate"], check=True)


def write_squid_config(config_text: str, path: Path = SQUID_CONF_PATH) -> None:
    """Write rendered config to disk atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(config_text)
    tmp.rename(path)
    logger.info("Wrote Squid config to %s", path)


def reload_squid() -> tuple[bool, str]:
    """Send SIGHUP to Squid (reconfigure in-place).

    Returns (success, error_message).
    """
    result = subprocess.run(
        ["systemctl", "reload-or-restart", SQUID_SERVICE],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    return True, ""


def squid_service_running() -> bool:
    """Return True if the Squid systemd service is active."""
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", SQUID_SERVICE],
        capture_output=True,
    )
    return result.returncode == 0
