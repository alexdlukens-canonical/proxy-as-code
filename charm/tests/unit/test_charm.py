"""Unit tests for the SquidAsAServiceCharm ops lifecycle."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import ops
import ops.testing
import pytest
from cosl import LZMABase64


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch):
    """Block all subprocess.run calls to prevent systemd/apt interactions during unit tests."""
    mock = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr("subprocess.run", mock)
    return mock


@pytest.fixture()
def ctx():
    """Return an ops testing Context for SquidAsAServiceCharm."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
    from charm import SquidAsAServiceCharm

    return ops.testing.Context(SquidAsAServiceCharm)


@pytest.fixture()
def base_state():
    """Return a minimal State with no relations."""
    return ops.testing.State(
        config={
            "squid-port": 3128,
            "squid-watcher-interval": 30,
            "api-port": 8080,
            "gunicorn-workers": 4,
        }
    )


class TestInstall:
    """Tests for the install event."""

    @patch("charm.SquidAsAServiceCharm._get_or_generate_secret_key", return_value="test-key")
    @patch("charm.SquidAsAServiceCharm._write_systemd_units")
    @patch("pathlib.Path.mkdir")
    def test_install_installs_squid(self, mock_mkdir, mock_units, mock_key, no_subprocess, ctx, base_state):
        """The install event should install Squid and its Prometheus exporter."""
        with ctx(ctx.on.install(), base_state) as mgr:
            mgr.run()
        apt_calls = [c for c in no_subprocess.call_args_list if "apt-get" in str(c)]
        assert len(apt_calls) == 1
        assert "prometheus-squid-exporter" in str(apt_calls[0])

    @patch("charm.SquidAsAServiceCharm._get_or_generate_secret_key", return_value="test-key")
    @patch("charm.SquidAsAServiceCharm._write_systemd_units")
    @patch("pathlib.Path.mkdir")
    def test_install_writes_systemd_units(self, mock_mkdir, mock_units, mock_key, ctx, base_state):
        """The install event should write systemd unit files."""
        with ctx(ctx.on.install(), base_state) as mgr:
            mgr.run()
        mock_units.assert_called_once()


class TestStart:
    """Tests for the start event."""

    def test_start_without_database_waits(self, ctx, base_state):
        """A start event with no database relation should set WaitingStatus."""
        with ctx(ctx.on.start(), base_state) as mgr:
            mgr.run()
            assert isinstance(mgr.charm.unit.status, ops.WaitingStatus)

    @patch("charm.SquidAsAServiceCharm._write_env_file")
    @patch("charm.SquidAsAServiceCharm._database_url", return_value="postgresql://u:p@host/db")
    def test_start_with_database_starts_services(self, mock_db, mock_env, no_subprocess, ctx, base_state):
        """A start event with a database should start all managed services."""
        with ctx(ctx.on.start(), base_state) as mgr:
            mgr.run()
        started = [str(c) for c in no_subprocess.call_args_list]
        assert any("enable" in s for s in started)
        assert any("terrasquid-squid-exporter" in s for s in started)


class TestManagementCommands:
    """Tests for captured management-command results."""

    @pytest.mark.parametrize(
        ("returncode", "stdout", "stderr", "expected"),
        [
            (0, "ok\n", "warning\n", ("ok\n", "")),
            (1, "stdout error\n", "stderr error\n", ("", "stderr error")),
            (1, "stdout error\n", "", ("", "stdout error")),
            (1, "", "", ("", "Management command failed with exit code 1")),
            (-9, "", "", ("", "Management command failed with exit code -9")),
            (1, " \n", "\n", ("", "Management command failed with exit code 1")),
        ],
    )
    def test_captured_result(self, returncode, stdout, stderr, expected, no_subprocess, ctx):
        """Failures always return an error, preferring captured diagnostics."""
        from charm import SquidAsAServiceCharm

        no_subprocess.return_value = subprocess.CompletedProcess(
            args=["python", "manage.py", "shell"], returncode=returncode, stdout=stdout, stderr=stderr
        )

        assert SquidAsAServiceCharm._run_manage_capture(MagicMock(), "shell", "-c", "pass") == expected


class TestPolicyReconciliation:
    """Tests for the charm's serialized configuration publication path."""

    def test_reconciliation_uses_shared_policy_helper(self, ctx):
        """Leader reconciliation must use the same lock and validation as API writes."""
        from charm import SquidAsAServiceCharm

        charm = MagicMock()
        charm.unit.is_leader.return_value = True
        charm._run_manage_capture.return_value = ("stale", "")
        assert SquidAsAServiceCharm._sync_config_version_if_stale(charm)
        command = charm._run_manage_capture.call_args.args
        assert command[:2] == ("shell", "-c")
        assert "reconcile_policy()" in command[2]
        assert "ConfigVersion.increment" not in command[2]

    def test_reconciliation_failure_is_reported(self, ctx, caplog):
        """A failed reconciliation must not look like a successful status check."""
        from charm import SquidAsAServiceCharm

        charm = MagicMock()
        charm.unit.is_leader.return_value = True
        charm._run_manage_capture.return_value = ("", "Squid configuration validation failed")
        assert not SquidAsAServiceCharm._sync_config_version_if_stale(charm)
        assert "Failed to reconcile Squid configuration" in caplog.text

    @pytest.mark.parametrize("returncode", [1, -9])
    def test_silent_command_failure_is_reported(self, returncode, no_subprocess, ctx, caplog):
        """A command failing without output must not make reconciliation succeed."""
        from charm import SquidAsAServiceCharm

        no_subprocess.return_value = subprocess.CompletedProcess(
            args=["python", "manage.py", "shell"], returncode=returncode, stdout="", stderr=""
        )
        charm = MagicMock()
        charm.unit.is_leader.return_value = True
        charm._run_manage_capture.return_value = SquidAsAServiceCharm._run_manage_capture(charm, "shell", "-c", "pass")

        assert not SquidAsAServiceCharm._sync_config_version_if_stale(charm)
        assert f"exit code {returncode}" in caplog.text

    def test_collect_status_blocks_on_reconciliation_failure(self, ctx):
        """Collect-status must preserve an explicit reconciliation error."""
        from charm import SquidAsAServiceCharm

        charm = MagicMock()
        charm.model.relations = {}
        charm.unit.status = ops.ActiveStatus()
        charm._sync_config_version_if_stale.return_value = False
        event = MagicMock()
        SquidAsAServiceCharm._on_collect_unit_status(charm, event)
        event.add_status.assert_called_once_with(ops.BlockedStatus("Failed to reconcile Squid configuration"))


class TestWatcherTimer:
    """Tests for the Squid configuration watcher timer."""

    @pytest.mark.parametrize(("config", "expected"), [({}, "30s"), ({"squid-watcher-interval": 45}, "45s")])
    def test_watcher_timer_uses_configured_interval(self, config, expected, ctx, monkeypatch):
        """The watcher timer should use the configured interval with a 30-second fallback."""
        import charm as charm_module

        write_text = MagicMock()
        monkeypatch.setattr(Path, "write_text", write_text)
        mock_self = MagicMock()
        mock_self.config = config

        charm_module.SquidAsAServiceCharm._write_watcher_timer(mock_self)

        assert f"OnUnitActiveSec={expected}" in write_text.call_args.args[0]


class TestUpgrade:
    """Tests for charm upgrades."""

    @patch("charm.SquidAsAServiceCharm._write_systemd_units")
    @patch("charm.squid.install_squid")
    def test_upgrade_installs_managed_packages(self, mock_install, mock_units, no_subprocess, ctx, base_state):
        """An upgrade installs packages introduced by the new charm revision."""
        with ctx(ctx.on.upgrade_charm(), base_state) as mgr:
            mgr.run()
        mock_install.assert_called_once()
        mock_units.assert_called_once()

    @patch("charm.SquidAsAServiceCharm._write_systemd_units")
    @patch("charm.squid.install_squid")
    def test_upgrade_requests_tracing_on_existing_cos_relation(self, mock_install, mock_units, ctx):
        """Upgrades must request the receiver even before another config-changed event."""
        relation = ops.testing.Relation("cos-agent", remote_app_name="opentelemetry-collector")
        state = ctx.run(ctx.on.upgrade_charm(), ops.testing.State(relations={relation}))
        config = json.loads(state.get_relation(relation.id).local_unit_data["config"])
        assert config["tracing_protocols"] == ["otlp_http"]

    @pytest.mark.parametrize(
        ("leader", "migrated", "fails"),
        [(True, False, False), (True, True, True), (False, True, False), (False, False, False)],
    )
    def test_upgrade_coordinates_migrations(self, leader, migrated, fails, ctx, base_state, no_subprocess):
        """Only leaders migrate; followers defer until the migration flag is set."""
        peer = ops.testing.PeerRelation("squid-aaas-peers", local_app_data={"db-migrated": "true" if migrated else ""})
        state = ops.testing.State(config=base_state.config, leader=leader, relations={peer})
        failure = subprocess.CalledProcessError(1, "migrate") if fails else None
        with (
            patch("charm.SquidAsAServiceCharm._write_systemd_units"),
            patch("charm.SquidAsAServiceCharm._get_or_generate_secret_key", return_value="test-key"),
            patch("charm.SquidAsAServiceCharm._write_env_file"),
            patch("charm.SquidAsAServiceCharm._database_url", return_value="postgresql://db/terrasquid"),
            patch("charm.SquidAsAServiceCharm._sync_config_version_if_stale"),
            patch("charm.SquidAsAServiceCharm._run_manage", side_effect=failure) as manage,
        ):
            result = ctx.run(ctx.on.upgrade_charm(), state)

        calls = [call.args for call in manage.call_args_list]
        if leader:
            assert calls[0] == ("migrate", "--noinput")
            assert result.get_relation(peer.id).local_app_data.get("db-migrated", "") == ("" if fails else "true")
        else:
            assert ("migrate", "--noinput") not in calls
        if not leader and not migrated:
            assert result.deferred
            manage.assert_not_called()
        elif not fails:
            assert calls[-1] == ("collectstatic", "--noinput")
            assert not result.deferred
            assert any(
                call.args[0] == ["systemctl", "restart", "terrasquid-api"] for call in no_subprocess.call_args_list
            )
        else:
            assert any(
                isinstance(status, ops.BlockedStatus) and "Charm upgrade failed" in status.message
                for status in ctx.unit_status_history
            )
            assert ("collectstatic", "--noinput") not in calls
            assert not any(
                call.args[0] == ["systemctl", "restart", "terrasquid-api"] for call in no_subprocess.call_args_list
            )


class TestDatabaseRelation:
    """Tests for database relation lifecycle."""

    @patch("charm.SquidAsAServiceCharm._start_services")
    @patch("charm.SquidAsAServiceCharm._run_manage")
    @patch("charm.SquidAsAServiceCharm._write_env_file")
    def test_database_created_runs_migrate(self, mock_env, mock_manage, mock_start, ctx, base_state):
        """database_created event should invoke migrate and collectstatic on the leader unit."""
        db_rel = ops.testing.Relation(
            "database",
            remote_app_name="postgresql",
            remote_app_data={
                "endpoints": "host:5432",
                "username": "usr",
                "password": "pwd",
                "database": "terrasquid",
            },
        )
        state = ops.testing.State(config=base_state.config, relations={db_rel}, leader=True)
        with ctx(ctx.on.relation_changed(db_rel), state) as mgr:
            mgr.run()
        assert mock_manage.call_count == 2
        assert mock_manage.call_args_list == [
            (("migrate", "--noinput"),),
            (("collectstatic", "--noinput"),),
        ]

    def test_database_relation_broken_stops_gunicorn(self, no_subprocess, ctx, base_state):
        """database_relation_broken should stop the Gunicorn service."""
        db_rel = ops.testing.Relation("database")
        state = ops.testing.State(config=base_state.config, relations={db_rel})
        with ctx(ctx.on.relation_broken(db_rel), state) as mgr:
            mgr.run()
        stopped = [str(c) for c in no_subprocess.call_args_list]
        assert any("stop" in s and "terrasquid-api" in s for s in stopped)


class TestCollectUnitStatus:
    """Tests for collect_unit_status event."""

    def test_no_database_reports_waiting(self, ctx, base_state):
        """Without a database relation the unit status should be Waiting."""
        with ctx(ctx.on.collect_unit_status(), base_state) as mgr:
            mgr.run()
            assert mgr.charm.unit.status == ops.WaitingStatus("Waiting for database relation")

    @patch("charm.SquidAsAServiceCharm._gunicorn_running", return_value=True)
    @patch("charm.SquidAsAServiceCharm._database_url", return_value="postgresql://u:p@host/db")
    def test_gunicorn_running_reports_active(self, mock_db, mock_guni, ctx, base_state):
        """With Gunicorn running the unit status should be Active."""
        with ctx(ctx.on.collect_unit_status(), base_state) as mgr:
            mgr.run()
            assert isinstance(mgr.charm.unit.status, ops.ActiveStatus)

    def test_certificates_without_hostname_blocks(self, ctx):
        """A certificate relation requires an explicit hostname."""
        relation = ops.testing.Relation("certificates")

        state = ctx.run(ctx.on.collect_unit_status(), ops.testing.State(relations={relation}))

        assert isinstance(state.unit_status, ops.BlockedStatus)
        assert "external-hostname" in state.unit_status.message

    def test_inactive_gunicorn_reports_maintenance(self, ctx, base_state):
        """A configured database alone is insufficient to report a healthy API."""
        with (
            patch("charm.SquidAsAServiceCharm._database_url", return_value="postgresql://db/terrasquid"),
            patch("charm.SquidAsAServiceCharm._gunicorn_running", return_value=False),
        ):
            state = ctx.run(ctx.on.collect_unit_status(), base_state)

        assert state.unit_status == ops.MaintenanceStatus("Starting API service")


class TestCOSAgentRelation:
    """Tests for COS agent telemetry publication."""

    def test_certificate_events_refresh_scrape_tls(self, ctx, tmp_path, monkeypatch):
        """Issuance, CA renewal, and denial publish the updated listener configuration."""
        from datetime import timedelta

        from charmlibs.interfaces.tls_certificates import (
            CertificateError,
            generate_ca,
            generate_certificate,
            generate_csr,
            generate_private_key,
        )

        import charm as charm_module

        monkeypatch.setattr(charm_module, "TERRASQUID_CERTS_DIR", tmp_path)
        for name in ("CERT_FILE", "KEY_FILE", "CA_FILE"):
            monkeypatch.setattr(charm_module, name, tmp_path / name)
        monkeypatch.setattr(charm_module.SquidAsAServiceCharm, "_write_gunicorn_config", MagicMock())
        monkeypatch.setattr(charm_module.SquidAsAServiceCharm, "_reload_gunicorn", MagicMock())
        monkeypatch.setattr(charm_module.SquidAsAServiceCharm, "_publish_django_ingress_requirements", MagicMock())
        key = generate_private_key()
        csr = generate_csr(private_key=key, common_name="terrasquid.example.com")
        cos_relation = ops.testing.Relation("cos-agent", remote_app_name="grafana-agent")
        state = ops.testing.State(config={"external-hostname": "terrasquid.example.com"}, relations={cos_relation})
        with ctx(ctx.on.relation_joined(cos_relation), state) as mgr:
            relation = mgr.charm.model.get_relation("cos-agent")
            with patch.object(mgr.charm.certificates, "get_private_key", return_value=key):
                for ca_name in ("initial-ca", "renewed-ca"):
                    ca = generate_ca(private_key=key, validity=timedelta(days=1), common_name=ca_name)
                    certificate = generate_certificate(csr=csr, ca=ca, ca_private_key=key, validity=timedelta(days=1))
                    mgr.charm.certificates.on.certificate_available.emit(
                        certificate=certificate, certificate_signing_request=csr, ca=ca, chain=[ca]
                    )
                    data = json.loads(relation.data[mgr.charm.unit]["config"])
                    api_job = next(
                        job
                        for job in data["metrics_scrape_jobs"]
                        if job["static_configs"][0]["targets"] == ["localhost:8080"]
                    )
                    assert api_job["scheme"] == "https"
                    assert api_job["tls_config"] == {"ca": ca.raw, "server_name": "terrasquid.example.com"}

            mgr.charm.certificates.on.certificate_denied.emit(
                certificate_signing_request=csr,
                error=CertificateError(code=1, name="denied", message="Certificate denied"),
            )
            data = json.loads(relation.data[mgr.charm.unit]["config"])
            assert all("scheme" not in job and "tls_config" not in job for job in data["metrics_scrape_jobs"])
            mgr.run()

    @pytest.mark.parametrize(
        ("has_cert", "has_key", "has_ca", "hostname", "port"),
        [
            (True, True, True, "terrasquid.example.com", 8080),
            (True, True, True, "terrasquid.example.com", 8443),
            (True, True, False, "terrasquid.example.com", 8080),
            (True, True, True, "", 8080),
            (True, False, True, "terrasquid.example.com", 8080),
            (False, True, True, "terrasquid.example.com", 8080),
            (False, False, False, "", 8080),
        ],
    )
    def test_scrape_tls_matches_api_listener(
        self, ctx, tmp_path, monkeypatch, has_cert, has_key, has_ca, hostname, port
    ):
        """Published API jobs verify TLS without requiring agent access to certificate files."""
        import charm as charm_module

        for name, present in (("CERT_FILE", has_cert), ("KEY_FILE", has_key), ("CA_FILE", has_ca)):
            path = tmp_path / name
            monkeypatch.setattr(charm_module, name, path)
            if present:
                path.write_text("CA PEM" if name == "CA_FILE" else name)

        cos_relation = ops.testing.Relation("cos-agent", remote_app_name="grafana-agent")
        state = ops.testing.State(config={"external-hostname": hostname, "api-port": port}, relations={cos_relation})
        with ctx(ctx.on.relation_joined(cos_relation), state) as mgr:
            mgr.run()
            relation = mgr.charm.model.get_relation("cos-agent")
            data = json.loads(relation.data[mgr.charm.unit]["config"])

        jobs = {job["static_configs"][0]["targets"][0]: job for job in data["metrics_scrape_jobs"]}
        api_job = jobs[f"localhost:{port}"]
        assert api_job["metrics_path"] == "/metrics"
        if has_cert and has_key:
            assert api_job["scheme"] == "https"
            expected_tls = {}
            if hostname:
                expected_tls["server_name"] = hostname
            if has_ca:
                expected_tls["ca"] = "CA PEM"
            assert api_job["tls_config"] == expected_tls
        else:
            assert "scheme" not in api_job
            assert "tls_config" not in api_job
        assert "scheme" not in jobs["localhost:9301"]
        assert "tls_config" not in jobs["localhost:9301"]

    def test_relation_advertises_metrics_dashboard_and_alert_rules(self, ctx, base_state):
        """COS relation data must advertise the local metrics scrape target and bundled artifacts."""
        cos_relation = ops.testing.Relation("cos-agent", remote_app_name="grafana-agent")
        state = ops.testing.State(config=base_state.config, relations={cos_relation})
        with ctx(ctx.on.relation_joined(cos_relation), state) as mgr:
            mgr.run()
            relation = mgr.charm.model.get_relation("cos-agent")
            assert relation is not None
            data = json.loads(relation.data[mgr.charm.unit]["config"])

        scrape_targets = {
            job["static_configs"][0]["targets"][0]: job["metrics_path"] for job in data["metrics_scrape_jobs"]
        }
        assert scrape_targets == {"localhost:8080": "/metrics", "localhost:9301": "/metrics"}
        dashboards = [json.loads(LZMABase64.decompress(dashboard)) for dashboard in data["dashboards"]]
        assert {dashboard["title"] for dashboard in dashboards} == {"Terrasquid Overview", "Squid Metrics"}
        assert data["metrics_alert_rules"]["groups"]
        assert data["tracing_protocols"] == ["otlp_http"]

    def test_dashboard_uses_native_squid_exporter_metrics(self):
        """The published Squid dashboard must graph metrics exported by the local exporter."""
        dashboard_path = Path(__file__).parent.parent.parent / "src" / "grafana_dashboards" / "squid.json"
        dashboard = json.loads(dashboard_path.read_text())
        expressions = {target["expr"] for panel in dashboard["panels"] for target in panel["targets"]}
        assert "sum(rate(squid_client_http_requests_total[5m]))" in expressions
        assert "sum(rate(squid_client_http_errors_total[5m]))" in expressions
        assert "sum(rate(squid_server_http_requests_total[5m]))" in expressions

    def test_published_dashboards_use_cos_prometheus_datasource(self):
        """Dashboard panels must explicitly use Grafana's COS-provisioned Prometheus datasource."""
        dashboard_dir = Path(__file__).parent.parent.parent / "src" / "grafana_dashboards"
        for dashboard_path in (dashboard_dir / "terrasquid.json", dashboard_dir / "squid.json"):
            dashboard = json.loads(dashboard_path.read_text())
            assert {panel["datasource"]["uid"] for panel in dashboard["panels"]} == {"${prometheusds}"}


class TestActions:
    """Tests for charm actions."""

    @patch("charm.SquidAsAServiceCharm._run_manage_capture", return_value=("key=abc\nprefix=ab12cd34\n", ""))
    def test_create_key_action_parses_output(self, mock_manage, ctx, base_state):
        """create-key action should return key and prefix from management command output."""
        with ctx(ctx.on.action("create-key", params={"name": "team-a"}), base_state) as mgr:
            mgr.run()

    @patch("charm.SquidAsAServiceCharm._run_manage_capture", return_value=("", "Error: key not found"))
    def test_create_key_action_fails_on_error(self, mock_manage, ctx, base_state):
        """create-key action should fail when the management command errors."""
        with (
            pytest.raises(ops.testing.ActionFailed),
            ctx(ctx.on.action("create-key", params={"name": "team-a"}), base_state) as mgr,
        ):
            mgr.run()

    @patch("charm.SquidAsAServiceCharm._run_manage_capture", return_value=("http_port 3128\n", ""))
    def test_reconfigure_action_runs_render_manage_command(self, mock_manage, ctx, base_state):
        """Reconfigure action should run the render_squid_config management command."""
        with ctx(ctx.on.action("reconfigure"), base_state) as mgr:
            mgr.run()
        mock_manage.assert_called_once_with("render_squid_config")

    @pytest.mark.parametrize(
        ("action", "params", "command", "output", "expected"),
        [
            (
                "create-key",
                {"name": "team-a"},
                "create_api_key",
                "key=abc=123\nprefix=abcd\n",
                {"key": "abc=123", "prefix": "abcd"},
            ),
            (
                "rotate-key",
                {"name": "team-a"},
                "rotate_api_key",
                "notice\n key = replacement \n",
                {"key": "replacement"},
            ),
            ("revoke-key", {"name": "team-a"}, "revoke_api_key", " revoked \n", {"result": "revoked"}),
            ("list-keys", {}, "list_api_keys", " team-a \n", {"keys": "team-a"}),
            ("reconfigure", {}, "render_squid_config", "", {"result": "Squid config applied successfully."}),
        ],
    )
    def test_actions_return_command_results(self, action, params, command, output, expected, ctx, base_state):
        """Actions expose parsed command results without losing embedded equals signs."""
        with patch("charm.SquidAsAServiceCharm._run_manage_capture", return_value=(output, "")) as manage:
            ctx.run(ctx.on.action(action, params=params), base_state)

        args = ("--name", params["name"]) if "name" in params else ()
        manage.assert_called_once_with(command, *args)
        assert ctx.action_results == expected

    @pytest.mark.parametrize(
        ("action", "params"),
        [
            ("revoke-key", {"name": "team-a"}),
            ("rotate-key", {"name": "team-a"}),
            ("list-keys", {}),
            ("reconfigure", {}),
            ("createsuperuser", {"username": "admin"}),
        ],
    )
    def test_actions_report_command_failures(self, action, params, ctx, base_state):
        """Management command errors fail the action instead of returning success."""
        with (
            patch("charm.SquidAsAServiceCharm._run_manage_capture", return_value=("", "command failed")),
            patch("charm.SquidAsAServiceCharm._django_env", return_value={}),
            pytest.raises(ops.testing.ActionFailed, match="command failed"),
        ):
            ctx.run(ctx.on.action(action, params=params), base_state)

    def test_createsuperuser_passes_generated_password_via_environment(self, ctx, base_state):
        """Superuser credentials go through the environment rather than command arguments."""
        with (
            patch("charm.secrets.token_urlsafe", return_value="generated-password"),
            patch("charm.SquidAsAServiceCharm._django_env", return_value={"EXISTING": "value"}),
            patch("charm.SquidAsAServiceCharm._run_manage_capture", return_value=("created", "")) as manage,
        ):
            ctx.run(ctx.on.action("createsuperuser", params={"username": "admin"}), base_state)

        manage.assert_called_once_with(
            "createsuperuser",
            "--noinput",
            "--username",
            "admin",
            "--email",
            "admin@example.com",
            extra_env={"EXISTING": "value", "DJANGO_SUPERUSER_PASSWORD": "generated-password"},
        )
        assert ctx.action_results == {"result": "Superuser 'admin' created.", "password": "generated-password"}


class TestSecretKey:
    """Tests for durable application keys and upgrades from unit-local keys."""

    @pytest.fixture(autouse=True)
    def local_paths(self, ctx, tmp_path, monkeypatch):
        """Keep legacy keys and rendered credentials inside the test directory."""
        import charm as charm_module

        monkeypatch.setattr(charm_module, "TERRASQUID_RUN_DIR", tmp_path)
        monkeypatch.setattr(charm_module, "TERRASQUID_ENV_FILE", tmp_path / "terrasquid.env")
        monkeypatch.setattr(charm_module.SquidAsAServiceCharm, "_write_systemd_units", MagicMock())

    def test_upgrade_migrates_existing_key_unchanged(self, ctx, tmp_path):
        """An upgrade must preserve the exact pre-existing key in an app secret."""
        key_file = tmp_path / "secret_key"
        key_file.write_text("existing-deployment-key\n")
        peer = ops.testing.PeerRelation("squid-aaas-peers")
        with patch("charm.secrets.token_hex") as generate:
            state = ctx.run(ctx.on.upgrade_charm(), ops.testing.State(leader=True, relations={peer}))
        secret = next(secret for secret in state.secrets if secret.label == "terrasquid-django")
        assert secret.owner == "app"
        assert secret.tracked_content == {"secret-key": "existing-deployment-key"}
        assert state.get_relation(peer.id).local_app_data["django-secret-id"] == secret.id
        assert not key_file.exists()
        generate.assert_not_called()

        state = ctx.run(ctx.on.upgrade_charm(), state)
        assert state.get_secret(id=secret.id).tracked_content == {"secret-key": "existing-deployment-key"}
        assert len(state.secrets) == 1

    def test_upgrade_renders_migrated_key_before_migrations(self, ctx, tmp_path):
        """Database migrations and services must use the preserved key on upgrade."""
        (tmp_path / "secret_key").write_text("existing-key")
        peer = ops.testing.PeerRelation("squid-aaas-peers")
        with (
            patch("charm.SquidAsAServiceCharm._database_url", return_value="postgresql://host/db"),
            patch("charm.SquidAsAServiceCharm._sync_config_version_if_stale"),
            patch("charm.SquidAsAServiceCharm._run_manage") as manage,
        ):
            ctx.run(ctx.on.upgrade_charm(), ops.testing.State(leader=True, relations={peer}))
        assert "SECRET_KEY=existing-key\n" in (tmp_path / "terrasquid.env").read_text()
        assert manage.call_args_list[0].args == ("migrate", "--noinput")
        assert (tmp_path / "terrasquid.env").stat().st_mode & 0o777 == 0o640

    def test_new_deployment_generates_only_one_application_key(self, ctx):
        """Repeated leadership events must reuse the application-owned key."""
        peer = ops.testing.PeerRelation("squid-aaas-peers")
        with patch("charm.secrets.token_hex", return_value="generated-key") as generate:
            state = ctx.run(ctx.on.leader_elected(), ops.testing.State(leader=True, relations={peer}))
            state = ctx.run(ctx.on.leader_elected(), state)
        generate.assert_called_once_with(50)
        assert len(state.secrets) == 1
        assert next(iter(state.secrets)).tracked_content == {"secret-key": "generated-key"}

    def test_peer_join_publishes_secret_created_before_relation(self, ctx):
        """Leaders must publish an existing secret when the peer relation becomes available."""
        secret = ops.testing.Secret({"secret-key": "persisted-key"}, owner="app", label="terrasquid-django")
        peer = ops.testing.PeerRelation("squid-aaas-peers", peers_data={1: {}})
        state = ctx.run(
            ctx.on.relation_joined(peer, remote_unit=1),
            ops.testing.State(leader=True, secrets={secret}, relations={peer}),
        )
        assert state.get_relation(peer.id).local_app_data["django-secret-id"] == secret.id
        assert len(state.secrets) == 1

    @pytest.mark.parametrize("leader", [False, True])
    def test_existing_secret_wins_over_local_file(self, leader, ctx, tmp_path):
        """Replacement units and leaders must never overwrite the canonical key."""
        (tmp_path / "secret_key").write_text("old-unit-specific-key")
        secret = ops.testing.Secret({"secret-key": "canonical-key"}, owner="app", label="terrasquid-django")
        peer = ops.testing.PeerRelation("squid-aaas-peers", local_app_data={"django-secret-id": secret.id})
        with ctx(ctx.on.config_changed(), ops.testing.State(leader=leader, secrets={secret}, relations={peer})) as mgr:
            assert mgr.charm._get_or_generate_secret_key() == "canonical-key"
            assert not (tmp_path / "secret_key").exists()

    def test_replacement_unit_reads_key_without_local_file(self, ctx, tmp_path):
        """A fresh follower must reconstruct its environment from Juju alone."""
        secret = ops.testing.Secret({"secret-key": "persisted-key"}, owner="app", label="terrasquid-django")
        peer = ops.testing.PeerRelation("squid-aaas-peers", local_app_data={"django-secret-id": secret.id})
        with (
            patch("charm.SquidAsAServiceCharm._database_url", return_value="postgresql://host/db"),
            patch("charm.SquidAsAServiceCharm._sync_config_version_if_stale"),
        ):
            state = ctx.run(ctx.on.start(), ops.testing.State(secrets={secret}, relations={peer}))
        assert not state.deferred
        assert "SECRET_KEY=persisted-key\n" in (tmp_path / "terrasquid.env").read_text()
        assert not (tmp_path / "secret_key").exists()

    @pytest.mark.parametrize("event_name", ["start", "upgrade_charm", "config_changed"])
    def test_follower_waits_without_generating_or_discarding_key(self, event_name, ctx, tmp_path, no_subprocess):
        """Followers must retain legacy keys and defer work until the leader publishes."""
        key_file = tmp_path / "secret_key"
        key_file.write_text("legacy-follower-key")
        with (
            patch("charm.SquidAsAServiceCharm._database_url", return_value="postgresql://host/db"),
            patch("charm.secrets.token_hex") as generate,
            patch("charm.SquidAsAServiceCharm._run_manage") as manage,
        ):
            state = ctx.run(getattr(ctx.on, event_name)(), ops.testing.State())
        assert state.deferred
        assert state.unit_status == ops.WaitingStatus("Waiting for leader to create application secret")
        assert key_file.read_text() == "legacy-follower-key"
        assert not (tmp_path / "terrasquid.env").exists()
        generate.assert_not_called()
        manage.assert_not_called()
        assert not any("terrasquid-api" in call.args[0] for call in no_subprocess.call_args_list)

    def test_failed_secret_creation_retains_local_key(self, ctx, tmp_path):
        """The migration source must remain available when Juju rejects secret creation."""
        key_file = tmp_path / "secret_key"
        key_file.write_text("existing-key")
        with (
            ctx(ctx.on.config_changed(), ops.testing.State(leader=True)) as mgr,
            patch.object(ops.Application, "add_secret", side_effect=ops.ModelError("secret store unavailable")),
            pytest.raises(ops.ModelError, match="secret store unavailable"),
        ):
            mgr.charm._get_or_generate_secret_key()
        assert key_file.read_text() == "existing-key"

    def test_missing_published_secret_is_not_regenerated(self, ctx, tmp_path):
        """A missing referenced secret must fail visibly rather than rotate credentials."""
        peer = ops.testing.PeerRelation("squid-aaas-peers", local_app_data={"django-secret-id": "secret:missing"})
        with (
            ctx(ctx.on.config_changed(), ops.testing.State(leader=True, relations={peer})) as mgr,
            pytest.raises(ops.SecretNotFoundError),
        ):
            mgr.charm._get_or_generate_secret_key()
        assert not (tmp_path / "secret_key").exists()

    def test_peer_event_replaces_follower_key_and_restarts_api(self, ctx, tmp_path, no_subprocess):
        """Existing followers must adopt the leader's migrated key, even while running."""
        env_file = tmp_path / "terrasquid.env"
        env_file.write_text("SECRET_KEY=old-follower-key\n")
        secret = ops.testing.Secret({"secret-key": "leader-key"}, owner="app", label="terrasquid-django")
        peer = ops.testing.PeerRelation(
            "squid-aaas-peers", local_app_data={"django-secret-id": secret.id}, peers_data={1: {}}
        )
        with (
            patch("charm.SquidAsAServiceCharm._database_url", return_value="postgresql://host/db"),
            patch("charm.SquidAsAServiceCharm._sync_config_version_if_stale"),
        ):
            state = ctx.run(
                ctx.on.relation_changed(peer, remote_unit=1),
                ops.testing.State(
                    secrets={secret},
                    relations={peer},
                    unit_status=ops.WaitingStatus("Waiting for leader to create application secret"),
                ),
            )
        assert "SECRET_KEY=leader-key\n" in env_file.read_text()
        assert isinstance(state.unit_status, ops.ActiveStatus)
        no_subprocess.assert_any_call(["systemctl", "restart", "terrasquid-api"], check=True)


class TestEnvFile:
    """Tests for environment file generation."""

    @patch("charm.SquidAsAServiceCharm._write_watcher_timer")
    @patch("charm.SquidAsAServiceCharm._get_or_generate_secret_key", return_value="s3cr3t")
    @patch("charm.SquidAsAServiceCharm._database_url", return_value="postgresql://u:p@db/terrasquid")
    def test_env_file_contains_database_url(self, mock_db, mock_key, mock_timer, ctx, base_state, tmp_path):
        """The env file should contain DATABASE_URL."""
        import charm as charm_module

        orig_env = charm_module.TERRASQUID_ENV_FILE
        orig_gunicorn = charm_module.GUNICORN_CONF_FILE
        charm_module.TERRASQUID_ENV_FILE = tmp_path / "terrasquid.env"
        charm_module.GUNICORN_CONF_FILE = tmp_path / "gunicorn.conf.py"
        try:
            with ctx(ctx.on.config_changed(), base_state) as mgr:
                mgr.run()
            content = (tmp_path / "terrasquid.env").read_text()
            assert "DATABASE_URL=postgresql://u:p@db/terrasquid" in content
        finally:
            charm_module.TERRASQUID_ENV_FILE = orig_env
            charm_module.GUNICORN_CONF_FILE = orig_gunicorn

    @patch("charm.SquidAsAServiceCharm._write_watcher_timer")
    @patch("charm.SquidAsAServiceCharm._get_or_generate_secret_key", return_value="s3cr3t")
    @patch("charm.SquidAsAServiceCharm._database_url", return_value="postgresql://u:p@db/terrasquid")
    def test_env_file_contains_pinned_config_version(self, mock_db, mock_key, mock_timer, ctx, tmp_path):
        """When squid-pinned-config-version is set, it must appear in the env file."""
        import charm as charm_module

        orig_env = charm_module.TERRASQUID_ENV_FILE
        orig_gunicorn = charm_module.GUNICORN_CONF_FILE
        charm_module.TERRASQUID_ENV_FILE = tmp_path / "terrasquid.env"
        charm_module.GUNICORN_CONF_FILE = tmp_path / "gunicorn.conf.py"
        state = ops.testing.State(
            config={
                "squid-port": 3128,
                "api-port": 8080,
                "gunicorn-workers": 4,
                "squid-pinned-config-version": 5,
            }
        )
        try:
            with ctx(ctx.on.config_changed(), state) as mgr:
                mgr.run()
            content = (tmp_path / "terrasquid.env").read_text()
            assert "SQUID_PINNED_CONFIG_VERSION=5" in content
        finally:
            charm_module.TERRASQUID_ENV_FILE = orig_env
            charm_module.GUNICORN_CONF_FILE = orig_gunicorn

    @patch("charm.SquidAsAServiceCharm._write_watcher_timer")
    @patch("charm.SquidAsAServiceCharm._get_or_generate_secret_key", return_value="s3cr3t")
    @patch("charm.SquidAsAServiceCharm._database_url", return_value="postgresql://u:p@db/terrasquid")
    def test_env_file_defaults_pinned_version_to_zero(self, mock_db, mock_key, mock_timer, ctx, base_state, tmp_path):
        """When squid-pinned-config-version is unset, the env file must default to 0."""
        import charm as charm_module

        orig_env = charm_module.TERRASQUID_ENV_FILE
        orig_gunicorn = charm_module.GUNICORN_CONF_FILE
        charm_module.TERRASQUID_ENV_FILE = tmp_path / "terrasquid.env"
        charm_module.GUNICORN_CONF_FILE = tmp_path / "gunicorn.conf.py"
        try:
            with ctx(ctx.on.config_changed(), base_state) as mgr:
                mgr.run()
            content = (tmp_path / "terrasquid.env").read_text()
            assert "SQUID_PINNED_CONFIG_VERSION=0" in content
        finally:
            charm_module.TERRASQUID_ENV_FILE = orig_env
            charm_module.GUNICORN_CONF_FILE = orig_gunicorn


class TestCertificatesRelation:
    """Tests for TLS certificates relation lifecycle."""

    @patch("charm.SquidAsAServiceCharm._request_certificate")
    def test_relation_joined_requests_certificate(self, mock_req, ctx, base_state):
        """Joining the certificates relation should trigger a certificate request."""
        certs_rel = ops.testing.Relation("certificates")
        state = ops.testing.State(config=base_state.config, relations={certs_rel})
        with ctx(ctx.on.relation_joined(certs_rel), state) as mgr:
            mgr.run()
        mock_req.assert_called_once()

    def test_certificate_available_writes_cert_and_ca_files(self, ctx, tmp_path, monkeypatch):
        """certificate_available handler must write the cert and CA PEM files to disk."""
        import charm as charm_module

        key_file = tmp_path / "terrasquid.key"
        monkeypatch.setattr(charm_module, "TERRASQUID_CERTS_DIR", tmp_path)
        monkeypatch.setattr(charm_module, "CERT_FILE", tmp_path / "terrasquid.crt")
        monkeypatch.setattr(charm_module, "CA_FILE", tmp_path / "ca.crt")
        monkeypatch.setattr(charm_module, "KEY_FILE", key_file)

        cert_pem = "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----"
        ca_pem = "-----BEGIN CERTIFICATE-----\nFAKE-CA\n-----END CERTIFICATE-----"
        key_pem = "-----BEGIN RSA PRIVATE KEY-----\nFAKE-KEY\n-----END RSA PRIVATE KEY-----"

        mock_event = MagicMock()
        mock_event.certificate.raw = cert_pem
        mock_event.ca.raw = ca_pem

        mock_private_key = MagicMock()
        mock_private_key.raw = key_pem

        mock_self = MagicMock()
        mock_self.certificates.get_private_key.return_value = mock_private_key
        charm_module.SquidAsAServiceCharm._on_certificate_available(mock_self, mock_event)

        assert (tmp_path / "terrasquid.crt").read_text() == cert_pem
        assert (tmp_path / "ca.crt").read_text() == ca_pem
        assert key_file.read_bytes() == key_pem.encode()
        mock_self._write_gunicorn_config.assert_called_once()
        mock_self._reload_gunicorn.assert_called_once()

    def test_certificate_denied_removes_files_and_reloads_gunicorn(self, ctx, tmp_path, monkeypatch):
        """certificate_denied handler must delete cert/CA files and reload gunicorn."""
        import charm as charm_module

        cert_file = tmp_path / "terrasquid.crt"
        ca_file = tmp_path / "ca.crt"
        cert_file.write_text("CERT")
        ca_file.write_text("CA")
        monkeypatch.setattr(charm_module, "CERT_FILE", cert_file)
        monkeypatch.setattr(charm_module, "CA_FILE", ca_file)

        mock_event = MagicMock()
        mock_self = MagicMock()
        charm_module.SquidAsAServiceCharm._on_certificate_denied(mock_self, mock_event)

        assert not cert_file.exists()
        assert not ca_file.exists()
        mock_self._write_gunicorn_config.assert_called_once()
        mock_self._reload_gunicorn.assert_called_once()

    def test_certificate_denied_tolerates_missing_files(self, ctx, tmp_path, monkeypatch):
        """certificate_denied handler must not raise when cert files are already absent."""
        import charm as charm_module

        monkeypatch.setattr(charm_module, "CERT_FILE", tmp_path / "missing.crt")
        monkeypatch.setattr(charm_module, "CA_FILE", tmp_path / "missing.ca")

        mock_self = MagicMock()
        charm_module.SquidAsAServiceCharm._on_certificate_denied(mock_self, MagicMock())

        mock_self._write_gunicorn_config.assert_called_once()
        mock_self._reload_gunicorn.assert_called_once()


class TestGunicornConfig:
    """Tests for gunicorn config SSL section generation."""

    def test_ssl_settings_included_when_cert_and_key_exist(self, ctx, tmp_path, monkeypatch):
        """Certfile and keyfile must appear in the gunicorn config when both files are present."""
        import charm as charm_module

        cert_file = tmp_path / "terrasquid.crt"
        key_file = tmp_path / "terrasquid.key"
        cert_file.write_text("CERT")
        key_file.write_text("KEY")

        monkeypatch.setattr(charm_module, "CERT_FILE", cert_file)
        monkeypatch.setattr(charm_module, "KEY_FILE", key_file)
        monkeypatch.setattr(charm_module, "CA_FILE", tmp_path / "missing.ca")
        monkeypatch.setattr(charm_module, "GUNICORN_CONF_FILE", tmp_path / "gunicorn.conf.py")

        mock_self = MagicMock()
        mock_self.config = {"api-port": 8080, "gunicorn-workers": 4}
        charm_module.SquidAsAServiceCharm._write_gunicorn_config(mock_self)

        content = (tmp_path / "gunicorn.conf.py").read_text()
        assert str(cert_file) in content
        assert str(key_file) in content
        assert "ca_certs" not in content

    def test_ssl_settings_include_ca_when_ca_exists(self, ctx, tmp_path, monkeypatch):
        """ca_certs must appear in the gunicorn config when the CA file is present."""
        import charm as charm_module

        cert_file = tmp_path / "terrasquid.crt"
        key_file = tmp_path / "terrasquid.key"
        ca_file = tmp_path / "ca.crt"
        cert_file.write_text("CERT")
        key_file.write_text("KEY")
        ca_file.write_text("CA")

        monkeypatch.setattr(charm_module, "CERT_FILE", cert_file)
        monkeypatch.setattr(charm_module, "KEY_FILE", key_file)
        monkeypatch.setattr(charm_module, "CA_FILE", ca_file)
        monkeypatch.setattr(charm_module, "GUNICORN_CONF_FILE", tmp_path / "gunicorn.conf.py")

        mock_self = MagicMock()
        mock_self.config = {"api-port": 8080, "gunicorn-workers": 4}
        charm_module.SquidAsAServiceCharm._write_gunicorn_config(mock_self)

        content = (tmp_path / "gunicorn.conf.py").read_text()
        assert str(cert_file) in content
        assert str(key_file) in content
        assert str(ca_file) in content

    def test_no_ssl_settings_when_cert_files_absent(self, ctx, tmp_path, monkeypatch):
        """Certfile and keyfile must not appear in the gunicorn config when files are missing."""
        import charm as charm_module

        monkeypatch.setattr(charm_module, "CERT_FILE", tmp_path / "missing.crt")
        monkeypatch.setattr(charm_module, "KEY_FILE", tmp_path / "missing.key")
        monkeypatch.setattr(charm_module, "CA_FILE", tmp_path / "missing.ca")
        monkeypatch.setattr(charm_module, "GUNICORN_CONF_FILE", tmp_path / "gunicorn.conf.py")

        mock_self = MagicMock()
        mock_self.config = {"api-port": 8080, "gunicorn-workers": 4}
        charm_module.SquidAsAServiceCharm._write_gunicorn_config(mock_self)

        content = (tmp_path / "gunicorn.conf.py").read_text()
        assert "certfile" not in content
        assert "keyfile" not in content


class TestWorkloadObservability:
    """Tests for scrapeable logs and optional workload tracing."""

    @pytest.mark.parametrize(
        ("receivers", "expected"),
        [
            ([], ""),
            (
                [{"protocol": {"name": "otlp_http", "type": "http"}, "url": "http://collector:4318"}],
                "http://collector:4318/v1/traces",
            ),
            (
                [{"protocol": {"name": "otlp_http", "type": "http"}, "url": "https://collector:4318/v1/traces"}],
                "https://collector:4318/v1/traces",
            ),
        ],
    )
    def test_receiver_change_updates_environment(self, receivers, expected, ctx, tmp_path, monkeypatch, no_subprocess):
        """Receiver changes must refresh the master's environment, including on non-leader units."""
        import charm as charm_module

        env_file = tmp_path / "terrasquid.env"
        monkeypatch.setattr(charm_module, "TERRASQUID_ENV_FILE", env_file)
        monkeypatch.setattr(charm_module.SquidAsAServiceCharm, "_database_url", lambda _self: "postgresql://host/db")
        monkeypatch.setattr(charm_module.SquidAsAServiceCharm, "_get_or_generate_secret_key", lambda _self: "test-key")
        relation = ops.testing.Relation(
            "cos-agent",
            remote_app_name="opentelemetry-collector",
            remote_units_data={0: {"receivers": json.dumps(receivers)}},
        )
        state = ctx.run(ctx.on.relation_joined(relation), ops.testing.State(relations={relation}))
        state = ctx.run(ctx.on.relation_changed(state.get_relation(relation.id)), state)
        content = env_file.read_text()
        assert f"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT={expected}\n" in content
        assert "OTEL_SERVICE_NAME=terrasquid\n" in content
        assert "OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE=/etc/ssl/certs/ca-certificates.crt\n" in content
        assert "juju_unit=terrasquid/0" in content
        no_subprocess.assert_any_call(["systemctl", "restart", "terrasquid-api"], check=True)

        no_subprocess.reset_mock()
        ctx.run(ctx.on.relation_changed(state.get_relation(relation.id)), state)
        assert not any(
            call.args[0] == ["systemctl", "restart", "terrasquid-api"] for call in no_subprocess.call_args_list
        )

    @pytest.mark.parametrize("event_name", ["relation_departed", "relation_broken"])
    def test_receiver_removal_disables_tracing(self, event_name, ctx, tmp_path, monkeypatch, no_subprocess):
        """Removing the collector must clear stale tracing endpoints and restart Gunicorn."""
        import charm as charm_module

        env_file = tmp_path / "terrasquid.env"
        env_file.write_text("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://old:4318/v1/traces\n")
        monkeypatch.setattr(charm_module, "TERRASQUID_ENV_FILE", env_file)
        monkeypatch.setattr(charm_module.SquidAsAServiceCharm, "_database_url", lambda _self: "postgresql://host/db")
        monkeypatch.setattr(charm_module.SquidAsAServiceCharm, "_get_or_generate_secret_key", lambda _self: "test-key")
        relation = ops.testing.Relation("cos-agent", remote_app_name="opentelemetry-collector")
        ctx.run(getattr(ctx.on, event_name)(relation), ops.testing.State(relations={relation}))
        assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=\n" in env_file.read_text()
        no_subprocess.assert_any_call(["systemctl", "restart", "terrasquid-api"], check=True)

    def test_systemd_output_is_scrapeable(self, ctx, monkeypatch):
        """Every managed workload's standard streams must append to a log file."""
        import charm as charm_module

        written = {}
        monkeypatch.setattr(Path, "write_text", lambda path, content: written.setdefault(str(path), content))
        charm_module.SquidAsAServiceCharm._write_systemd_units(MagicMock())
        for service, filename in (
            ("terrasquid-api", "api"),
            ("terrasquid-watcher", "watcher"),
            ("terrasquid-squid-exporter", "squid-exporter"),
        ):
            content = written[f"/etc/systemd/system/{service}.service"]
            assert f"StandardOutput=append:/var/log/terrasquid/{filename}.log" in content
            assert f"StandardError=append:/var/log/terrasquid/{filename}.log" in content
            assert "UMask=0027" in content

    def test_rotation_preserves_open_output_files(self, ctx, tmp_path, monkeypatch):
        """Rotation must retain the file descriptors systemd keeps open for output."""
        import charm as charm_module

        log_dir = tmp_path / "logs"
        rotation = tmp_path / "logrotate"
        monkeypatch.setattr(charm_module, "TERRASQUID_LOG_DIR", log_dir)
        monkeypatch.setattr(charm_module, "TERRASQUID_LOGROTATE_FILE", rotation)
        charm_module.SquidAsAServiceCharm._write_logging_config(MagicMock())
        assert log_dir.stat().st_mode & 0o777 == 0o750
        for filename in ("api.log", "watcher.log", "squid-exporter.log"):
            assert (log_dir / filename).stat().st_mode & 0o777 == 0o640
        assert f"{log_dir}/*.log" in rotation.read_text()
        assert "copytruncate" in rotation.read_text()
        assert "rotate 7" in rotation.read_text()

    def test_gunicorn_logs_requests_without_queries_and_loads_after_fork(self, ctx, tmp_path, monkeypatch):
        """Gunicorn must log requests without query strings and load tracing after fork."""
        import charm as charm_module

        config_file = tmp_path / "gunicorn.conf.py"
        monkeypatch.setattr(charm_module, "GUNICORN_CONF_FILE", config_file)
        mock_self = MagicMock()
        mock_self.config = {"api-port": 8080, "gunicorn-workers": 4}
        charm_module.SquidAsAServiceCharm._write_gunicorn_config(mock_self)
        config = {}
        exec(compile(config_file.read_text(), str(config_file), "exec"), config)
        assert config["accesslog"] == "-"
        assert config["errorlog"] == "-"
        assert config["preload_app"] is False
        assert "%(U)s" in config["access_log_format"]
        assert "%(q)s" not in config["access_log_format"]
        assert "%(r)s" not in config["access_log_format"]
