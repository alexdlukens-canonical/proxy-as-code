"""Unit tests for the SquidAsAServiceCharm ops lifecycle."""

import json
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
            "squid-extra-config": "",
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


class TestCOSAgentRelation:
    """Tests for COS agent telemetry publication."""

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
                "squid-extra-config": "",
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
