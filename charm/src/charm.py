#!/usr/bin/env python3
"""Terrasquid (Squid-as-a-Service) Juju machine charm."""

import json
import logging
import os
import secrets
import subprocess
import textwrap
from datetime import UTC, datetime
from ipaddress import IPv4Address
from pathlib import Path
from urllib.parse import quote

import ops
from charmlibs.interfaces.tls_certificates import (
    CertificateAvailableEvent,
    CertificateDeniedEvent,
    CertificateRequestAttributes,
    TLSCertificatesRequiresV4,
)
from charms.data_platform_libs.v0.data_interfaces import (
    DatabaseCreatedEvent,
    DatabaseEndpointsChangedEvent,
    DatabaseRequires,
)
from charms.grafana_agent.v0.cos_agent import COSAgentProvider, ProtocolNotFoundError
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer

import squid

logger = logging.getLogger(__name__)

CHARM_DIR = Path(__file__).parent.parent
VENV_BIN = CHARM_DIR / "venv" / "bin"
DJANGO_APP_DIR = CHARM_DIR / "src" / "django_app"

TERRASQUID_ENV_FILE = Path("/etc/terrasquid/terrasquid.env")
TERRASQUID_STATUS_FILE = Path("/var/lib/terrasquid/status.json")
TERRASQUID_RUN_DIR = Path("/var/lib/terrasquid")
TERRASQUID_CERTS_DIR = Path("/etc/terrasquid/certs")
TERRASQUID_LOG_DIR = Path("/var/log/terrasquid")
TERRASQUID_LOGROTATE_FILE = Path("/etc/logrotate.d/terrasquid")

CERT_FILE = TERRASQUID_CERTS_DIR / "terrasquid.crt"
KEY_FILE = TERRASQUID_CERTS_DIR / "terrasquid.key"
CA_FILE = TERRASQUID_CERTS_DIR / "ca.crt"

GUNICORN_SERVICE = "terrasquid-api"
GUNICORN_CONF_FILE = Path("/etc/terrasquid/gunicorn.conf.py")
SQUID_WATCHER_SERVICE = "terrasquid-watcher"
SQUID_WATCHER_TIMER = "terrasquid-watcher.timer"
SQUID_EXPORTER_PORT = 9301


class SquidAsAServiceCharm(ops.CharmBase):
    """Terrasquid Juju charm managing Gunicorn + Squid on Ubuntu 24.04."""

    def __init__(self, *args):
        super().__init__(*args)

        self.database = DatabaseRequires(self, relation_name="database", database_name="terrasquid")
        _hostname = self.config.get("external-hostname", "")
        _cert_requests = (
            [CertificateRequestAttributes(common_name=_hostname, sans_dns=[_hostname])] if _hostname else []
        )
        self.certificates = TLSCertificatesRequiresV4(self, "certificates", certificate_requests=_cert_requests)
        self.django_ingress = IngressPerAppRequirer(self, relation_name="django-ingress")
        self.squid_ingress = IngressPerAppRequirer(self, relation_name="squid-ingress")
        self.cos_agent = COSAgentProvider(
            self,
            metrics_rules_dir="./src/prometheus_alert_rules",
            dashboard_dirs=["./src/grafana_dashboards"],
            scrape_configs=self._cos_agent_scrape_configs,
            tracing_protocols=["otlp_http"],
            refresh_events=[self.on.config_changed, self.on.upgrade_charm],
        )

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.stop, self._on_stop)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_unit_status)

        self.framework.observe(self.database.on.database_created, self._on_database_created)
        self.framework.observe(self.database.on.endpoints_changed, self._on_database_endpoints_changed)
        self.framework.observe(self.on.database_relation_broken, self._on_database_relation_broken)

        self.framework.observe(self.on.certificates_relation_joined, self._on_certificates_relation_joined)
        self.framework.observe(self.certificates.on.certificate_available, self._on_certificate_available)
        self.framework.observe(self.certificates.on.certificate_denied, self._on_certificate_denied)

        self.framework.observe(self.on.django_ingress_relation_joined, self._on_django_ingress_relation_joined)
        self.framework.observe(self.on.squid_ingress_relation_joined, self._on_squid_ingress_relation_joined)
        self.framework.observe(self.on.leader_elected, self._on_leader_elected)

        self.framework.observe(self.on.squid_aaas_peers_relation_changed, self._on_peers_relation_changed)
        self.framework.observe(self.on.cos_agent_relation_changed, self._on_cos_agent_relation_changed)
        self.framework.observe(self.on.cos_agent_relation_departed, self._on_cos_agent_relation_changed)
        self.framework.observe(self.on.cos_agent_relation_broken, self._on_cos_agent_relation_changed)

        self.framework.observe(self.on.create_key_action, self._on_create_key_action)
        self.framework.observe(self.on.revoke_key_action, self._on_revoke_key_action)
        self.framework.observe(self.on.rotate_key_action, self._on_rotate_key_action)
        self.framework.observe(self.on.list_keys_action, self._on_list_keys_action)
        self.framework.observe(self.on.reconfigure_action, self._on_reconfigure_action)
        self.framework.observe(self.on.createsuperuser_action, self._on_createsuperuser_action)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _on_install(self, _event: ops.InstallEvent) -> None:
        self.unit.status = ops.MaintenanceStatus("Installing Squid")
        squid.install_squid()
        TERRASQUID_RUN_DIR.mkdir(parents=True, exist_ok=True)
        TERRASQUID_CERTS_DIR.mkdir(parents=True, exist_ok=True)
        TERRASQUID_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._get_or_generate_secret_key()
        self._write_systemd_units()
        subprocess.run(["systemctl", "daemon-reload"], check=True)

    def _on_config_changed(self, _event: ops.ConfigChangedEvent) -> None:
        if not self._database_url():
            return
        self._request_certificate()
        self._write_gunicorn_config()
        self._write_watcher_timer()
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "reload-or-restart", SQUID_WATCHER_TIMER], capture_output=True)
        self._write_env_file()
        self._reload_gunicorn()
        subprocess.run(["systemctl", "restart", squid.SQUID_EXPORTER_SERVICE], check=True)
        if self._gunicorn_running():
            self._open_ports()
        self._publish_django_ingress_requirements()
        self._publish_squid_ingress_requirements()

    def _on_start(self, _event: ops.StartEvent) -> None:
        if not self._database_url():
            self.unit.status = ops.WaitingStatus("Waiting for database relation")
            return
        self._start_services()

    def _on_stop(self, _event: ops.StopEvent) -> None:
        for svc in (GUNICORN_SERVICE, SQUID_WATCHER_SERVICE, squid.SQUID_EXPORTER_SERVICE, squid.SQUID_SERVICE):
            subprocess.run(["systemctl", "stop", svc], capture_output=True)
        self.unit.set_ports()

    def _on_upgrade_charm(self, event: ops.UpgradeCharmEvent) -> None:
        squid.install_squid()
        self._write_systemd_units()
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        if not self._database_url():
            return
        self._write_env_file()
        try:
            if self.unit.is_leader():
                self._clear_migration_flag()
                self._run_manage("migrate", "--noinput")
                self._set_migration_flag()
            elif not self._migration_complete():
                self.unit.status = ops.WaitingStatus("Waiting for leader to complete migrations")
                event.defer()
                return
            self._run_manage("collectstatic", "--noinput")
            subprocess.run(["systemctl", "restart", GUNICORN_SERVICE], check=True)
            subprocess.run(["systemctl", "enable", "--now", squid.SQUID_EXPORTER_SERVICE], check=True)
            self._reload_squid_exporter()
        except subprocess.CalledProcessError as e:
            logger.error("Management command failed during charm upgrade: %s", e)
            self.unit.status = ops.BlockedStatus("Charm upgrade failed — check logs")
            return

    def _on_collect_unit_status(self, event: ops.CollectStatusEvent) -> None:
        if self.model.relations.get("certificates") and not self.config.get("external-hostname"):
            event.add_status(
                ops.BlockedStatus("external-hostname config required when certificates relation is configured")
            )
            return
        if not self._database_url():
            event.add_status(ops.WaitingStatus("Waiting for database relation"))
            return
        if not self._gunicorn_running():
            event.add_status(ops.MaintenanceStatus("Starting API service"))
            return
        self._sync_config_version_if_stale()
        api_status = "up"
        squid_status = "up" if squid.squid_service_running() else "down"
        config_version = self._read_applied_config_version()
        tls_status = "enabled" if CERT_FILE.exists() else "disabled"
        event.add_status(
            ops.ActiveStatus(
                f"api: {api_status} | squid: {squid_status} | tls: {tls_status} | config v{config_version}"
            )
        )

    # ── Ingress relations ─────────────────────────────────────────────────────

    def _on_django_ingress_relation_joined(self, _event: ops.RelationJoinedEvent) -> None:
        self._publish_django_ingress_requirements()

    def _on_squid_ingress_relation_joined(self, _event: ops.RelationJoinedEvent) -> None:
        self._publish_squid_ingress_requirements()

    def _on_leader_elected(self, _event: ops.LeaderElectedEvent) -> None:
        self._publish_django_ingress_requirements()
        self._publish_squid_ingress_requirements()

    # ── Database relation ─────────────────────────────────────────────────────

    def _on_database_created(self, event: DatabaseCreatedEvent) -> None:
        self.unit.status = ops.MaintenanceStatus("Configuring database")
        self._write_env_file()
        try:
            if self.unit.is_leader():
                self._run_manage("migrate", "--noinput")
                self._run_manage("collectstatic", "--noinput")
                self._set_migration_flag()
                self._start_services()
            elif self._migration_complete():
                self._run_manage("collectstatic", "--noinput")
                self._start_services()
            else:
                self.unit.status = ops.WaitingStatus("Waiting for leader to complete migrations")
                event.defer()
                return
        except subprocess.CalledProcessError as e:
            logger.error("Management command failed during database setup: %s", e)
            self.unit.status = ops.BlockedStatus("Database setup failed — check logs")
            return
        # Re-publish ingress requirements now that ports are open.
        self._publish_django_ingress_requirements()
        self._publish_squid_ingress_requirements()

    def _on_database_endpoints_changed(self, event: DatabaseEndpointsChangedEvent) -> None:
        self._write_env_file()
        self._reload_gunicorn()

    def _on_database_relation_broken(self, _event: ops.RelationBrokenEvent) -> None:
        self.unit.status = ops.WaitingStatus("Database relation removed")
        subprocess.run(["systemctl", "stop", GUNICORN_SERVICE], capture_output=True)

    # ── Peer relation ─────────────────────────────────────────────────────────

    def _on_peers_relation_changed(self, _event: ops.RelationChangedEvent) -> None:
        if self.unit.is_leader():
            return
        if not self._database_url():
            return
        if self._migration_complete() and not self._gunicorn_running():
            try:
                self._run_manage("collectstatic", "--noinput")
            except subprocess.CalledProcessError as e:
                logger.error("collectstatic failed: %s", e)
                self.unit.status = ops.BlockedStatus("collectstatic failed — check logs")
                return
            self._start_services()

    # ── Certificates relation ─────────────────────────────────────────────────

    def _on_certificates_relation_joined(self, event: ops.RelationJoinedEvent) -> None:
        """Generate a CSR and request a certificate."""
        self._request_certificate()

    def _on_certificate_available(self, event: CertificateAvailableEvent) -> None:
        """Handle certificate availability."""
        TERRASQUID_CERTS_DIR.mkdir(parents=True, exist_ok=True)
        CERT_FILE.write_text(event.certificate.raw)
        CA_FILE.write_text(event.ca.raw)
        private_key = self.certificates.get_private_key()
        if private_key:
            old_umask = os.umask(0o177)
            try:
                KEY_FILE.write_bytes(private_key.raw.encode())
            finally:
                os.umask(old_umask)
        self._write_gunicorn_config()
        self._reload_gunicorn()
        self._publish_django_ingress_requirements()
        logger.info("Certificate available and Gunicorn reloaded")

    def _on_certificate_denied(self, event: CertificateDeniedEvent) -> None:
        """Handle certificate denial by removing stale cert files and reconfiguring."""
        logger.warning("Certificate denied: %s", event.error)
        for f in (CERT_FILE, CA_FILE):
            if f.exists():
                f.unlink()
        self._write_gunicorn_config()
        self._reload_gunicorn()
        self._publish_django_ingress_requirements()

    def _request_certificate(self) -> None:
        """Sync the TLS certificate request with the relation data."""
        if not self.model.relations.get("certificates"):
            return
        if not self.config.get("external-hostname"):
            return
        self.certificates.sync()

    # ── Actions ──────────────────────────────────────────────────────────────

    def _on_create_key_action(self, event: ops.ActionEvent) -> None:
        name = event.params["name"]
        out, err = self._run_manage_capture("create_api_key", "--name", name)
        if err:
            event.fail(err)
            return
        result = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip()
        event.set_results(result)

    def _on_revoke_key_action(self, event: ops.ActionEvent) -> None:
        name = event.params["name"]
        out, err = self._run_manage_capture("revoke_api_key", "--name", name)
        if err:
            event.fail(err)
        else:
            event.set_results({"result": out.strip()})

    def _on_rotate_key_action(self, event: ops.ActionEvent) -> None:
        name = event.params["name"]
        out, err = self._run_manage_capture("rotate_api_key", "--name", name)
        if err:
            event.fail(err)
            return
        result = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip()
        event.set_results(result)

    def _on_list_keys_action(self, event: ops.ActionEvent) -> None:
        out, err = self._run_manage_capture("list_api_keys")
        if err:
            event.fail(err)
        else:
            event.set_results({"keys": out.strip()})

    def _on_reconfigure_action(self, event: ops.ActionEvent) -> None:
        out, err = self._run_manage_capture("render_squid_config")
        if err:
            event.fail(f"Squid reconfigure failed: {err}")
        else:
            event.set_results({"result": out.strip() or "Squid config applied successfully."})

    def _on_createsuperuser_action(self, event: ops.ActionEvent) -> None:
        username = event.params["username"]
        email = event.params.get("email", "admin@example.com")
        password = secrets.token_urlsafe(16)
        env = self._django_env()
        env["DJANGO_SUPERUSER_PASSWORD"] = password
        out, err = self._run_manage_capture(
            "createsuperuser",
            "--noinput",
            "--username",
            username,
            "--email",
            email,
            extra_env=env,
        )
        if err and "already exists" not in err:
            event.fail(err)
        else:
            event.set_results({"result": f"Superuser '{username}' created.", "password": password})

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _on_cos_agent_relation_changed(self, event: ops.RelationEvent) -> None:
        """Refresh workload processes when the optional tracing receiver changes."""
        if not self._database_url():
            return
        previous = TERRASQUID_ENV_FILE.read_text() if TERRASQUID_ENV_FILE.exists() else ""
        endpoint = (
            "" if isinstance(event, (ops.RelationDepartedEvent, ops.RelationBrokenEvent)) else self._tracing_endpoint()
        )
        self._write_env_file(tracing_endpoint=endpoint)
        if TERRASQUID_ENV_FILE.read_text() != previous and self._gunicorn_running():
            # HUP reloads workers but does not refresh the master's systemd environment.
            subprocess.run(["systemctl", "restart", GUNICORN_SERVICE], check=True)

    def _tracing_endpoint(self) -> str:
        """Return the negotiated OTLP/HTTP receiver, or disable optional tracing."""
        if not self.cos_agent.is_ready():
            return ""
        try:
            endpoint = self.cos_agent.get_tracing_endpoint("otlp_http").rstrip("/")
        except ProtocolNotFoundError:
            logger.info("COS agent has no OTLP/HTTP receiver; workload tracing is disabled")
            return ""
        return endpoint if endpoint.endswith("/v1/traces") else f"{endpoint}/v1/traces"

    def _database_url(self) -> str:
        """Build a DATABASE_URL from the data provided by the postgresql relation."""
        if not self.database.relations:
            return ""
        relation_id = self.database.relations[0].id
        data = self.database.fetch_relation_data().get(relation_id, {})
        endpoints = data.get("endpoints", "")
        username = data.get("username", "")
        password = data.get("password", "")
        database = data.get("database", "terrasquid")
        if not (endpoints and username):
            return ""
        host_port = endpoints.split(",")[0]
        return f"postgresql://{username}:{quote(password, safe='')}@{host_port}/{database}"

    def _write_env_file(self, *, tracing_endpoint: str | None = None) -> None:
        """Write environment variables to /etc/terrasquid/terrasquid.env."""
        db_url = self._database_url()
        secret_key = self._get_or_generate_secret_key()
        squid_port = self.config.get("squid-port", 3128)
        squid_prepend_config = self.config.get("squid-prepend-config", "")
        squid_append_config = self.config.get("squid-append-config", "")
        squid_default_deny = self.config.get("squid-default-deny", True)
        squid_pinned_config_version = self.config.get("squid-pinned-config-version", 0)
        if tracing_endpoint is None:
            tracing_endpoint = self._tracing_endpoint()
        resource_attributes = ",".join(
            f"{key}={value}"
            for key, value in {
                "juju_model": self.model.name,
                "juju_model_uuid": self.model.uuid,
                "juju_application": self.app.name,
                "juju_unit": self.unit.name,
                "juju_charm": self.meta.name,
                "service.instance.id": f"{self.model.uuid}/{self.unit.name}",
            }.items()
        )

        content = textwrap.dedent(f"""\
            DATABASE_URL={db_url}
            SECRET_KEY={secret_key}
            ALLOWED_HOSTS=*
            DJANGO_SETTINGS_MODULE=terrasquid.settings
            DJANGO_ADMIN_ENABLED=true
            JUJU_UNIT_NAME={self.unit.name}
            SQUID_PORT={squid_port}
            SQUID_PREPEND_CONFIG={squid_prepend_config}
            SQUID_APPEND_CONFIG={squid_append_config}
            SQUID_DEFAULT_DENY={"true" if squid_default_deny else "false"}
            SQUID_PINNED_CONFIG_VERSION={squid_pinned_config_version}
            TERRASQUID_STATUS_FILE={TERRASQUID_STATUS_FILE}
            OTEL_SERVICE_NAME={self.app.name}
            OTEL_RESOURCE_ATTRIBUTES={resource_attributes}
            OTEL_EXPORTER_OTLP_TRACES_ENDPOINT={tracing_endpoint}
            OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE=/etc/ssl/certs/ca-certificates.crt
            OTEL_EXPORTER_OTLP_TIMEOUT=5
            OTEL_TRACES_SAMPLER=parentbased_traceidratio
            OTEL_TRACES_SAMPLER_ARG=0.1
        """)
        TERRASQUID_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        TERRASQUID_ENV_FILE.write_text(content)
        os.chmod(TERRASQUID_ENV_FILE, 0o640)

    def _get_or_generate_secret_key(self) -> str:
        """Return a stable SECRET_KEY, generating one on first call."""
        key_file = TERRASQUID_RUN_DIR / "secret_key"
        if key_file.exists():
            return key_file.read_text().strip()
        key = secrets.token_hex(50)
        TERRASQUID_RUN_DIR.mkdir(parents=True, exist_ok=True)
        old_umask = os.umask(0o177)
        try:
            key_file.write_text(key)
        finally:
            os.umask(old_umask)
        return key

    def _django_env(self) -> dict:
        """Return a dict with PYTHONPATH and other Django env variables set."""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(DJANGO_APP_DIR)
        if TERRASQUID_ENV_FILE.exists():
            for line in TERRASQUID_ENV_FILE.read_text().splitlines():
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
        return env

    def _run_manage(self, *args: str) -> None:
        """Run a Django management command, raising on failure."""
        logger.info("Running manage.py %s", " ".join(args))
        cmd = [str(VENV_BIN / "python"), str(DJANGO_APP_DIR / "manage.py"), *args]
        subprocess.run(cmd, check=True, env=self._django_env(), cwd=str(DJANGO_APP_DIR))

    def _run_manage_capture(self, *args: str, extra_env: dict | None = None) -> tuple[str, str]:
        """Run a Django management command and return (stdout, stderr)."""
        cmd = [str(VENV_BIN / "python"), str(DJANGO_APP_DIR / "manage.py"), *args]
        env = self._django_env()
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(DJANGO_APP_DIR),
        )
        if result.returncode != 0:
            return "", result.stderr.strip() or result.stdout.strip()
        return result.stdout, ""

    def _write_gunicorn_config(self) -> None:
        """Write /etc/terrasquid/gunicorn.conf.py from current charm config."""
        api_port = self.config.get("api-port", 8080)
        workers = self.config.get("gunicorn-workers", 4)

        ssl_config = ""
        if CERT_FILE.exists() and KEY_FILE.exists():
            ssl_config = f'certfile = "{CERT_FILE}"\nkeyfile = "{KEY_FILE}"\n'
            if CA_FILE.exists():
                ssl_config += f'ca_certs = "{CA_FILE}"\n'

        content = (
            f'bind = "[::]:{api_port}"\nworkers = {workers}\ntimeout = 120\nworker_class = "sync"\n'
            'preload_app = False\naccesslog = "-"\nerrorlog = "-"\ncapture_output = True\n'
            "access_log_format = '%(h)s %(m)s %(U)s %(s)s %(L)s'\n"
        ) + ssl_config
        GUNICORN_CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
        GUNICORN_CONF_FILE.write_text(content)

    def _write_systemd_units(self) -> None:
        """Write systemd unit files for Gunicorn and the config watcher."""
        self._write_logging_config()
        self._write_gunicorn_config()
        gunicorn_unit = textwrap.dedent(f"""\
            [Unit]
            Description=Terrasquid REST API (Gunicorn)
            After=network.target postgresql.service
            Wants=network.target

            [Service]
            Type=notify
            EnvironmentFile={TERRASQUID_ENV_FILE}
            Environment=PYTHONPATH={DJANGO_APP_DIR}
            WorkingDirectory={DJANGO_APP_DIR}
            ExecStart={VENV_BIN}/python -m gunicorn \\
                --config {GUNICORN_CONF_FILE} \\
                terrasquid.wsgi:application
            Restart=on-failure
            RestartSec=5s
            UMask=0027
            StandardOutput=append:{TERRASQUID_LOG_DIR}/api.log
            StandardError=append:{TERRASQUID_LOG_DIR}/api.log

            [Install]
            WantedBy=multi-user.target
        """)
        Path(f"/etc/systemd/system/{GUNICORN_SERVICE}.service").write_text(gunicorn_unit)

        watcher_unit = textwrap.dedent(f"""\
            [Unit]
            Description=Terrasquid Squid config version watcher
            After={GUNICORN_SERVICE}.service

            [Service]
            Type=oneshot
            EnvironmentFile={TERRASQUID_ENV_FILE}
            Environment=PYTHONPATH={DJANGO_APP_DIR}
            WorkingDirectory={DJANGO_APP_DIR}
            ExecStart={VENV_BIN}/python {DJANGO_APP_DIR}/manage.py render_squid_config
            UMask=0027
            StandardOutput=append:{TERRASQUID_LOG_DIR}/watcher.log
            StandardError=append:{TERRASQUID_LOG_DIR}/watcher.log
        """)
        Path(f"/etc/systemd/system/{SQUID_WATCHER_SERVICE}.service").write_text(watcher_unit)

        self._write_watcher_timer()

        exporter_unit = textwrap.dedent(f"""\
            [Unit]
            Description=Terrasquid Squid Prometheus exporter
            After={squid.SQUID_SERVICE}.service
            Wants={squid.SQUID_SERVICE}.service

            [Service]
            EnvironmentFile={TERRASQUID_ENV_FILE}
            ExecStart=/usr/bin/prometheus-squid-exporter \
                --squid-hostname localhost \
                --squid-port $SQUID_PORT \
                --listen 127.0.0.1:{SQUID_EXPORTER_PORT}
            Restart=on-failure
            RestartSec=5s
            UMask=0027
            StandardOutput=append:{TERRASQUID_LOG_DIR}/squid-exporter.log
            StandardError=append:{TERRASQUID_LOG_DIR}/squid-exporter.log

            [Install]
            WantedBy=multi-user.target
        """)
        Path(f"/etc/systemd/system/{squid.SQUID_EXPORTER_SERVICE}.service").write_text(exporter_unit)

    def _write_logging_config(self) -> None:
        """Rotate file-backed service output without invalidating systemd's file descriptors."""
        TERRASQUID_LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o750)
        for filename in ("api.log", "watcher.log", "squid-exporter.log"):
            (TERRASQUID_LOG_DIR / filename).touch(mode=0o640, exist_ok=True)
        TERRASQUID_LOGROTATE_FILE.write_text(
            textwrap.dedent(f"""\
                {TERRASQUID_LOG_DIR}/*.log {{
                    daily
                    maxsize 20M
                    rotate 7
                    missingok
                    notifempty
                    compress
                    delaycompress
                    copytruncate
                    su root root
                }}
            """)
        )

    def _write_watcher_timer(self) -> None:
        """Write the systemd timer for periodic Squid configuration checks."""
        watcher_interval = int(self.config.get("squid-watcher-interval", 30))
        watcher_timer = textwrap.dedent(f"""\
            [Unit]
            Description=Terrasquid Squid config watcher timer
            After={GUNICORN_SERVICE}.service

            [Timer]
            OnBootSec=10s
            OnUnitActiveSec={watcher_interval}s
            Unit={SQUID_WATCHER_SERVICE}.service

            [Install]
            WantedBy=timers.target
        """)
        Path(f"/etc/systemd/system/{SQUID_WATCHER_TIMER}").write_text(watcher_timer)

    def _start_services(self) -> None:
        """Enable and start all managed systemd services."""
        subprocess.run(["systemctl", "enable", "--now", GUNICORN_SERVICE], check=True)
        subprocess.run(["systemctl", "enable", "--now", SQUID_WATCHER_TIMER], check=True)
        subprocess.run(["systemctl", "enable", "--now", squid.SQUID_SERVICE], check=True)
        subprocess.run(["systemctl", "enable", "--now", squid.SQUID_EXPORTER_SERVICE], check=True)
        self._open_ports()

    def _reload_gunicorn(self) -> None:
        """Send SIGHUP to Gunicorn to reload workers."""
        subprocess.run(["systemctl", "reload-or-restart", GUNICORN_SERVICE], capture_output=True)

    def _reload_squid_exporter(self) -> None:
        """Restart the exporter after its Squid listener configuration changes."""
        subprocess.run(["systemctl", "reload-or-restart", squid.SQUID_EXPORTER_SERVICE], capture_output=True)

    def _gunicorn_running(self) -> bool:
        """Return True if the Gunicorn service is active."""
        result = subprocess.run(["systemctl", "is-active", "--quiet", GUNICORN_SERVICE], capture_output=True)
        return result.returncode == 0

    def _publish_django_ingress_requirements(self) -> None:
        """Publish ingress requirements for the Django API.

        App-level data (port, scheme) is written only by the leader; the library
        enforces this internally. Every unit must call this so it can publish its
        own host/IP, allowing the ingress provider to load-balance across all units.
        """
        api_port = int(self.config.get("api-port", 8080))
        scheme = "https" if CERT_FILE.exists() else "http"
        ip = self._unit_ipv4_address("django-ingress")
        self.django_ingress.provide_ingress_requirements(port=api_port, scheme=scheme, ip=ip)

    def _publish_squid_ingress_requirements(self) -> None:
        """Publish ingress requirements for Squid, all units."""
        squid_port = int(self.config.get("squid-port", 3128))
        ip = self._unit_ipv4_address("squid-ingress")
        self.squid_ingress.provide_ingress_requirements(port=squid_port, ip=ip)

    def _cos_agent_scrape_configs(self) -> list[dict]:
        """Build the local Prometheus scrape target from the current API port."""
        return [
            {
                "metrics_path": "/metrics",
                "static_configs": [{"targets": [f"localhost:{int(self.config.get('api-port', 8080))}"]}],
            },
            {
                "metrics_path": "/metrics",
                "static_configs": [{"targets": [f"localhost:{SQUID_EXPORTER_PORT}"]}],
            },
        ]

    def _unit_ipv4_address(self, relation_name: str) -> str | None:
        """Return the unit's IPv4 bind address for the given ingress relation.

        The ingress library defaults to the binding's first bind address, which
        may be IPv6. Explicitly select an IPv4 address so the ingress provider
        addresses the unit over IPv4.
        """
        binding = self.model.get_binding(relation_name)
        if binding is None:
            return None
        for interface in binding.network.interfaces:
            if isinstance(interface.address, IPv4Address):
                return str(interface.address)
        return None

    def _open_ports(self) -> None:
        squid_port = int(self.config.get("squid-port", 3128))
        api_port = int(self.config.get("api-port", 8080))
        self.unit.set_ports(
            ops.Port("tcp", squid_port),
            ops.Port("tcp", api_port),
        )

    def _set_migration_flag(self) -> None:
        """Write the db-migrated flag to peer application data (leader only)."""
        rel = self.model.get_relation("squid-aaas-peers")
        if rel is None:
            return
        rel.data[self.app]["db-migrated"] = "true"

    def _clear_migration_flag(self) -> None:
        """Clear the db-migrated flag so followers wait for the new migration to complete."""
        rel = self.model.get_relation("squid-aaas-peers")
        if rel is None:
            return
        rel.data[self.app]["db-migrated"] = ""

    def _migration_complete(self) -> bool:
        """Return True if the leader has signalled that migrations are complete."""
        rel = self.model.get_relation("squid-aaas-peers")
        if rel is None:
            return True
        return rel.data[self.app].get("db-migrated") == "true"

    def _sync_config_version_if_stale(self) -> None:
        """Re-render the Squid config and bump ConfigVersion if DB state diverges from the stored render."""
        if not self.unit.is_leader():
            return
        out, err = self._run_manage_capture(
            "shell",
            "-c",
            (
                "from terrasquid.api.models import ConfigVersion;"
                "from terrasquid.api.squid_render import render_squid_config;"
                "cv = ConfigVersion.get();"
                "rendered = render_squid_config(version=cv.version);"
                "changed = cv.rendered_config != rendered;"
                "changed and ConfigVersion.increment(render_squid_config());"
                "print('stale' if changed else 'ok')"
            ),
        )
        if out.strip() == "stale":
            logger.info("Config version bumped: DB state diverged from stored render (e.g. admin edit)")

    def _read_applied_config_version(self) -> int:
        if not TERRASQUID_STATUS_FILE.exists():
            return 0
        try:
            data = json.loads(TERRASQUID_STATUS_FILE.read_text())
            return data.get("applied_config_version", 0)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to read applied config version from %s: %s", TERRASQUID_STATUS_FILE, e)
            return 0

    def _db_config_version(self) -> int:
        """Query the current config version from the database."""
        out, err = self._run_manage_capture(
            "shell",
            "-c",
            "from terrasquid.api.models import ConfigVersion; print(ConfigVersion.get().version)",
        )
        try:
            return int(out.strip())
        except (ValueError, TypeError):
            return 0

    def _update_unit_status(self, applied_version: int) -> None:
        """Write the applied config version and reload timestamp to the status file."""
        status = {
            "applied_config_version": applied_version,
            "last_reload": datetime.now(UTC).isoformat(),
            "last_reload_ok": True,
        }
        TERRASQUID_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        TERRASQUID_STATUS_FILE.write_text(json.dumps(status))


if __name__ == "__main__":
    ops.main(SquidAsAServiceCharm)
