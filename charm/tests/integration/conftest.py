"""Pytest fixtures for Terrasquid integration tests."""

import json
import os
import secrets
import subprocess
from pathlib import Path

import jubilant
import pytest

CHARM_DIR = Path(__file__).parent.parent.parent
JUJU_BIN = "/snap/juju/current/bin/juju"


@pytest.fixture()
def metrics_auth():
    """Read a unit's rendered metrics credentials without placing them in command arguments."""

    def read_credentials(juju: jubilant.Juju, unit: str) -> tuple[str, str]:
        code = (
            "import json; from pathlib import Path; "
            "env = dict(line.split('=', 1) for line in "
            "Path('/etc/terrasquid/terrasquid.env').read_text().splitlines()); "
            "print(json.dumps([env['METRICS_USERNAME'], env['METRICS_PASSWORD']]))"
        )
        result = subprocess.run(
            [JUJU_BIN, "exec", "--model", juju.model, "--unit", unit, "--", "python3", "-c", code],
            capture_output=True,
            text=True,
            check=True,
        )
        username, password = json.loads(result.stdout)
        return username, password

    return read_credentials


@pytest.fixture(scope="module")
def juju():
    """Provide a module-scoped Juju instance.

    Uses the snap-unconfined juju binary to avoid PATH/confinement issues.
    Reuses the model when JUJU_MODEL env var is set, otherwise creates a
    temporary model and tears it down after the test module completes.
    """
    model = os.environ.get("JUJU_MODEL")
    if model:
        juju_instance = jubilant.Juju(model=model, cli_binary=JUJU_BIN)
        juju_instance.model_config({"update-status-hook-interval": "1m"})
        yield juju_instance
    else:
        model_name = "jubilant-" + secrets.token_hex(4)
        juju_instance = jubilant.Juju(cli_binary=JUJU_BIN)
        juju_instance.add_model(model_name)
        juju_instance.model_config({"update-status-hook-interval": "1m"})
        try:
            yield juju_instance
        finally:
            juju_instance.destroy_model(model_name, destroy_storage=True)


@pytest.fixture(scope="module")
def deployed_charms(juju):
    """Deploy PostgreSQL and the Terrasquid charm, then integrate them.

    The fixture blocks until both applications are in active/idle state.
    Returns a dict with 'saas_app' and 'pg_app' keys.
    """
    charm_path = _find_charm_file()

    juju.deploy("postgresql", channel="16/stable", base="ubuntu@24.04")
    juju.deploy(
        charm_path,
        app="terrasquid",
        base="ubuntu@24.04",
        config={
            "api-port": 8080,
            "squid-port": 3128,
            "gunicorn-workers": 2,
        },
    )
    juju.integrate("terrasquid:database", "postgresql:database")

    juju.wait(jubilant.all_active, timeout=450)

    return {"saas_app": "terrasquid", "pg_app": "postgresql"}


@pytest.fixture(scope="module")
def deployed_charms_with_tls(juju, deployed_charms):
    """Extend the base deployment with self-signed-certificates integrated to terrasquid.

    Deploys the self-signed-certificates charm, integrates it on the certificates
    relation, and waits for all applications to return to active/idle.
    Returns the same dict as deployed_charms plus a 'tls_app' key.
    """
    saas_app = deployed_charms["saas_app"]
    status = juju.status()
    unit_address = status.apps[saas_app].units[f"{saas_app}/0"].public_address
    juju.config(saas_app, {"external-hostname": unit_address})

    juju.deploy(
        "self-signed-certificates",
        channel="1/stable",
        base="ubuntu@24.04",
    )
    juju.integrate(f"{saas_app}:certificates", "self-signed-certificates:certificates")
    juju.wait(jubilant.all_active, timeout=300)

    def tls_enabled(status):
        unit = status.apps[saas_app].units[f"{saas_app}/0"]
        return "tls: enabled" in unit.workload_status.message

    juju.wait(tls_enabled, timeout=120)
    return {**deployed_charms, "tls_app": "self-signed-certificates"}


@pytest.fixture(scope="module")
def deployed_charms_with_otelcol(juju, deployed_charms):
    """Relate Terrasquid to the OpenTelemetry Collector subordinate."""
    saas_app = deployed_charms["saas_app"]
    otelcol_app = "opentelemetry-collector"
    juju.deploy(
        otelcol_app,
        app=otelcol_app,
        channel="2/stable",
        base="ubuntu@24.04",
    )
    juju.integrate(f"{saas_app}:cos-agent", f"{otelcol_app}:cos-agent")

    def terrasquid_active_idle(status):
        unit = status.apps[saas_app].units[f"{saas_app}/0"]
        return unit.workload_status.current == "active" and unit.juju_status.current == "idle"

    juju.wait(terrasquid_active_idle, timeout=450)
    return {**deployed_charms, "otelcol_app": otelcol_app}


def _find_charm_file() -> str:
    """Locate the built .charm file in the charm directory."""
    charm_files = list(CHARM_DIR.glob("*.charm"))
    if not charm_files:
        raise FileNotFoundError(f"No .charm file found in {CHARM_DIR}")
    return str(charm_files[0])
