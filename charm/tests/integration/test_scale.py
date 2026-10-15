"""Scale integration test: 1000 source rules applied to Squid config.

This module deploys a dedicated, isolated Juju model to guarantee that no
resources from other test modules bleed into the generated Squid configuration
file being verified.
"""

import re
import secrets
import subprocess
import time
from pathlib import Path

import jubilant
import pytest
import requests

CHARM_DIR = Path(__file__).parent.parent.parent
JUJU_BIN = "/snap/juju/current/bin/juju"

_SAAS_APP = "terrasquid"
_UNIT = f"{_SAAS_APP}/0"
_API_PORT = 8080

# Absolute paths inside the Juju unit for the deployed charm.
_UNIT_AGENT_DIR = f"/var/lib/juju/agents/unit-{_SAAS_APP.replace('-', '-')}-0"
_UNIT_CHARM_DIR = f"{_UNIT_AGENT_DIR}/charm"
_UNIT_VENV_PYTHON = f"{_UNIT_CHARM_DIR}/venv/bin/python"
_UNIT_MANAGE_PY = f"{_UNIT_CHARM_DIR}/src/django_app/manage.py"
_UNIT_DJANGO_APP = f"{_UNIT_CHARM_DIR}/src/django_app"
_UNIT_ENV_FILE = "/etc/terrasquid/terrasquid.env"
_SQUID_CONF = "/etc/squid/squid.conf"

SOURCE_COUNT = 1000
_ACL_KEY_PREFIX = re.compile(r"\b(?P<kind>src|dst|dst_group|rule_src|rule_dst)__[A-Za-z0-9]+__")

# Local snapshot file — persists across runs so the config can be diffed.
_SNAPSHOT_PATH = CHARM_DIR / "tests" / "integration" / "squid_scale_snapshot.conf"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def juju_model_deployed():
    """Provide a module-scoped Juju instance in a fresh, isolated model."""
    model_name = "scale-" + secrets.token_hex(4)
    j = jubilant.Juju(cli_binary=JUJU_BIN)
    j.add_model(model_name)
    try:
        yield j
    finally:
        j.destroy_model(model_name, destroy_storage=True)


@pytest.fixture(scope="module")
def deploy_charms(juju_model_deployed):
    """Deploy PostgreSQL and the Terrasquid charm into the isolated scale model."""
    charm_files = list(CHARM_DIR.glob("*.charm"))
    if not charm_files:
        raise FileNotFoundError(f"No .charm file found in {CHARM_DIR}")
    charm_path = str(charm_files[0])

    juju_model_deployed.deploy("postgresql", channel="16/stable", base="ubuntu@24.04")
    juju_model_deployed.deploy(
        charm_path,
        app=_SAAS_APP,
        base="ubuntu@24.04",
        config={"api-port": _API_PORT, "squid-port": 3128, "gunicorn-workers": 2},
    )
    juju_model_deployed.integrate(f"{_SAAS_APP}:database", "postgresql:database")
    juju_model_deployed.wait(jubilant.all_active, timeout=300)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _unit_address(juju: jubilant.Juju) -> str:
    status = juju.status()
    return status.apps[_SAAS_APP].units[_UNIT].public_address


def _api_url(address: str) -> str:
    return f"http://{address}:{_API_PORT}/api/v1"


def _create_api_key(juju: jubilant.Juju, name: str) -> str:
    task = juju.run(_UNIT, "create-key", params={"name": name})
    assert task.success, f"create-key failed: {task.results}"
    return task.results["key"]


def _juju_exec(model: str, command: str) -> str:
    """Run an arbitrary shell command on the Terrasquid unit via `juju exec`."""
    result = subprocess.run(
        [JUJU_BIN, "exec", "--model", model, "--unit", _UNIT, "--", "bash", "-c", command],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _wait_config_applied(address: str, expected_version: int, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(f"{_api_url(address)}/status/", timeout=10)
            data = resp.json()
            if data["applied_config_version"] >= expected_version and data["last_reload_ok"]:
                return
        except requests.RequestException:
            pass
        time.sleep(5)
    raise TimeoutError(f"Squid config version {expected_version} was not applied within {timeout}s")


def _read_unit_squid_conf(model: str) -> str:
    return _juju_exec(model, f"cat {_SQUID_CONF}")


def _canonical_squid_conf(config: str) -> str:
    """Remove deployment-specific ACL prefixes without changing rendered rule order."""
    return "".join(
        _ACL_KEY_PREFIX.sub(lambda match: f"{match['kind']}__KEY_PREFIX__", line)
        for line in config.splitlines(keepends=True)
    )


def _assert_squid_conf_matches_snapshot(config: str, snapshot_path: Path) -> None:
    canonical_config = _canonical_squid_conf(config)
    if snapshot_path.exists():
        snapshot = _canonical_squid_conf(snapshot_path.read_text())
        if canonical_config != snapshot:
            new_path = snapshot_path.with_suffix(snapshot_path.suffix + ".new")
            new_path.write_text(canonical_config)
        assert canonical_config == snapshot, (
            f"The Squid config differs from the previous run's snapshot at {snapshot_path}."
        )
    else:
        snapshot_path.write_text(canonical_config)


# ── Test ──────────────────────────────────────────────────────────────────────


def test_canonical_squid_conf_preserves_rule_order() -> None:
    """Canonicalization replaces key prefixes without reordering access rules."""
    config = """\
acl rule_src__prefix__sources any-of src__prefix__a-source src__prefix__z-source
acl dst_group__prefix__sites__1 any-of dst__prefix__destination
http_access allow src__prefix__a-source rule_dst__prefix__priority-99__1 port__80
http_access allow src__prefix__z-source rule_dst__prefix__priority-100__1 port__80
http_access allow rule_src__prefix__sources dst_group__prefix__sites__1 port__80
"""

    canonical_config = _canonical_squid_conf(config)
    assert "acl rule_src__KEY_PREFIX__sources any-of src__KEY_PREFIX__a-source src__KEY_PREFIX__z-source" in (
        canonical_config
    )
    assert "acl dst_group__KEY_PREFIX__sites__1 any-of dst__KEY_PREFIX__destination" in canonical_config
    access_lines = [line for line in canonical_config.splitlines() if line.startswith("http_access")]

    assert access_lines == [
        "http_access allow src__KEY_PREFIX__a-source rule_dst__KEY_PREFIX__priority-99__1 port__80",
        "http_access allow src__KEY_PREFIX__z-source rule_dst__KEY_PREFIX__priority-100__1 port__80",
        "http_access allow rule_src__KEY_PREFIX__sources dst_group__KEY_PREFIX__sites__1 port__80",
    ]


def test_1000_source_rules_squid_config_matches(juju_model_deployed, deploy_charms):
    """Create 1000 source ACLs and verify Squid config determinism.

    The test uses its own isolated Juju model (via the juju_model_deployed fixture) so
    that no resources from parallel test modules affect the generated file.
    """
    _ = deploy_charms
    address = _unit_address(juju_model_deployed)
    base = _api_url(address)
    key = _create_api_key(juju_model_deployed, "scale-test")
    headers = {"Authorization": f"Api-Key {key}"}
    created_source_ids: list[str] = []

    try:
        for i in range(SOURCE_COUNT):
            cidr = f"10.{i // 256}.{i % 256}.0/24"
            resp = requests.post(
                f"{base}/sources/",
                json={"name": f"src-{i:04d}", "cidr": [cidr]},
                headers=headers,
                timeout=15,
            )
            assert resp.status_code in (200, 201), (
                f"Unexpected status {resp.status_code} creating source {i}: {resp.text}"
            )
            created_source_ids.append(resp.json()["id"])

        final_status = requests.get(f"{base}/status/", timeout=10).json()
        db_version = final_status["db_config_version"]

        _wait_config_applied(address, expected_version=db_version)

        model = juju_model_deployed.model
        file_on_disk = _read_unit_squid_conf(model)
        _assert_squid_conf_matches_snapshot(file_on_disk, _SNAPSHOT_PATH)

    finally:
        # Clean up the created sources so they don't affect other tests or future runs.
        for source_id in created_source_ids:
            requests.delete(f"{base}/sources/{source_id}/", headers=headers, timeout=10)

        # Wait for the post-delete db version rather than assuming every delete applied.
        final_status = requests.get(f"{base}/status/", timeout=10).json()
        _wait_config_applied(address, expected_version=final_status["db_config_version"])

        # ensure there are no leftover sources in the database
        resp = requests.get(f"{base}/sources/", headers=headers, timeout=10)
        assert resp.status_code == 200, f"Failed to list sources during cleanup: {resp.text}"
        sources = resp.json()
        assert len(sources) == 0, f"Expected all sources to be deleted, but found {len(sources)} remaining: {sources}"


def test_create_all_resources(juju_model_deployed, deploy_charms):
    """Create all resource types at scale and verify Squid config determinism.

    This creates 200 of each primary resource type plus extra ACL rule variants,
    then verifies the rendered Squid config is stable across runs.
    """
    _ = deploy_charms
    address = _unit_address(juju_model_deployed)
    base = _api_url(address)
    key = _create_api_key(juju_model_deployed, "all-resources-test")
    headers = {"Authorization": f"Api-Key {key}"}

    resource_count = 200
    created_source_ids = []
    created_dest_ids = []
    created_rule_ids = []

    try:
        # Create 200 sources
        for i in range(resource_count):
            name = f"src-{i:03d}"
            cidr = f"10.{i // 256}.{i % 256}.0/24"
            resp = requests.post(
                f"{base}/sources/",
                json={"name": name, "cidr": [cidr]},
                headers=headers,
                timeout=15,
            )
            assert resp.status_code in (200, 201), (
                f"Unexpected status {resp.status_code} creating source {i}: {resp.text}"
            )

            created_source_ids.append(resp.json()["id"])

        # Create 200 destinations
        for i in range(resource_count):
            name = f"dst-{i:03d}"
            fqdn = f"dest{i}.example.com"
            resp = requests.post(
                f"{base}/destinations/",
                json={"name": name, "dst": fqdn, "type": "ALLOW"},
                headers=headers,
                timeout=15,
            )
            assert resp.status_code in (200, 201), (
                f"Unexpected status {resp.status_code} creating destination {i}: {resp.text}"
            )
            created_dest_ids.append(resp.json()["id"])

        # Create 200 rules
        for i in range(resource_count):
            rule_name = f"rule-{i:03d}"
            resp = requests.post(
                f"{base}/acl-rules/",
                json={
                    "name": rule_name,
                    "sources": [created_source_ids[i]],
                    "destinations": [created_dest_ids[i]],
                },
                headers=headers,
                timeout=15,
            )
            assert resp.status_code in (200, 201), (
                f"Unexpected status {resp.status_code} creating rule {i}: {resp.text}"
            )
            rule_data = resp.json()
            rule_id = rule_data.get("id") or rule_name
            created_rule_ids.append(rule_id)

        # Create some rules referencing multiple sources and destinations
        for i in range(10):
            rule_name = f"rule-multi-{i:02d}"
            resp = requests.post(
                f"{base}/acl-rules/",
                json={
                    "name": rule_name,
                    "sources": [created_source_ids[i], created_source_ids[i + 1]],
                    "destinations": [created_dest_ids[i], created_dest_ids[i + 1]],
                },
                headers=headers,
                timeout=15,
            )
            assert resp.status_code in (200, 201), (
                f"Unexpected status {resp.status_code} creating multi-reference rule {i}: {resp.text}"
            )
            rule_data = resp.json()
            rule_id = rule_data.get("id") or rule_name
            created_rule_ids.append(rule_id)

        # Create some deny rules as well
        for i in range(10):
            rule_name = f"rule-deny-{i:02d}"
            resp = requests.post(
                f"{base}/acl-rules/",
                json={
                    "name": rule_name,
                    "sources": [created_source_ids[i + 10]],
                    "destinations": [created_dest_ids[i + 10]],
                    "priority": 200,
                },
                headers=headers,
                timeout=15,
            )
            assert resp.status_code in (200, 201), (
                f"Unexpected status {resp.status_code} creating deny rule {i}: {resp.text}"
            )
            rule_data = resp.json()
            rule_id = rule_data.get("id") or rule_name
            created_rule_ids.append(rule_id)

        final_status = requests.get(f"{base}/status/", timeout=10).json()
        db_version = final_status["db_config_version"]

        _wait_config_applied(address, expected_version=db_version)

        model = juju_model_deployed.model
        file_on_disk = _read_unit_squid_conf(model)

        # Verify the config is deterministic by checking it matches a snapshot
        snapshot_path = CHARM_DIR / "tests" / "integration" / "squid_all_resources_snapshot.conf"
        _assert_squid_conf_matches_snapshot(file_on_disk, snapshot_path)

    finally:
        # Clean up all created rules
        for rule_id in created_rule_ids:
            requests.delete(f"{base}/acl-rules/{rule_id}/", headers=headers, timeout=10)

        # Clean up all created destinations
        for dest_id in created_dest_ids:
            requests.delete(f"{base}/destinations/{dest_id}/", headers=headers, timeout=10)

        # Clean up all created sources
        for source_id in created_source_ids:
            requests.delete(f"{base}/sources/{source_id}/", headers=headers, timeout=10)

        # Wait for all deletions to be applied
        final_status = requests.get(f"{base}/status/", timeout=10).json()
        db_version = final_status["db_config_version"]
        _wait_config_applied(address, expected_version=db_version)

        # Verify all resources are deleted
        resp = requests.get(f"{base}/sources/", headers=headers, timeout=10)
        assert resp.status_code == 200, f"Failed to list sources during cleanup: {resp.text}"
        sources = resp.json()
        assert len(sources) == 0, f"Expected all sources to be deleted, but found {len(sources)} remaining"

        resp = requests.get(f"{base}/destinations/", headers=headers, timeout=10)
        assert resp.status_code == 200, f"Failed to list destinations during cleanup: {resp.text}"
        destinations = resp.json()
        assert len(destinations) == 0, (
            f"Expected all destinations to be deleted, but found {len(destinations)} remaining"
        )

        resp = requests.get(f"{base}/acl-rules/", headers=headers, timeout=10)
        assert resp.status_code == 200, f"Failed to list rules during cleanup: {resp.text}"
        rules = resp.json()
        assert len(rules) == 0, f"Expected all rules to be deleted, but found {len(rules)} remaining"
