"""Management command: render_squid_config."""

import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from opentelemetry import trace

from terrasquid.api.squid_render import render_squid_config, validate_squid_config

SQUID_CONF = Path("/etc/squid/squid.conf")
SQUID_CONF_NEW = Path("/etc/squid/squid.conf.new")
tracer = trace.get_tracer(__name__)
logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Render the Squid config, validate it, and apply it if changed."""

    help = (
        "Render the Squid config from DB state. "
        "When --output is omitted, applies the config atomically and reloads Squid."
    )

    def add_arguments(self, parser) -> None:
        """Define command arguments."""
        parser.add_argument(
            "--output",
            default=None,
            help="Write the rendered config to this path instead of applying it ('-' for stdout).",
        )

    @tracer.start_as_current_span("squid.configure")
    def handle(self, *args, **options) -> None:
        """Execute the command."""
        from terrasquid.api.models import ConfigVersion, RenderedConfigHistory

        config_version = ConfigVersion.get()

        output = options["output"]
        if output is not None:
            rendered = render_squid_config()
            if output == "-":
                self.stdout.write(rendered)
            else:
                Path(output).write_text(rendered)
                self.stdout.write(f"Wrote config to {output}")
            return

        pinned_version = settings.SQUID_PINNED_CONFIG_VERSION
        RenderedConfigHistory.prune(pinned_version=pinned_version)
        if pinned_version > 0:
            try:
                history_entry = RenderedConfigHistory.objects.get(version=pinned_version)
            except RenderedConfigHistory.DoesNotExist:
                self.stdout.write(f"Pinned config version {pinned_version} has not been rendered yet; skipping.")
                return
            self._apply(history_entry.rendered_config, pinned_version)
            return

        rendered = config_version.rendered_config or render_squid_config(version=config_version.version)
        self._apply(rendered, config_version.version)

    @tracer.start_as_current_span("squid.apply_config")
    def _apply(self, rendered: str, version: int) -> None:
        """Write, validate, diff, atomically replace, and reload Squid."""
        span = trace.get_current_span()
        span.set_attribute("squid.config.version", version)
        logger.info("Checking Squid configuration version %s", version)
        SQUID_CONF_NEW.parent.mkdir(parents=True, exist_ok=True)
        SQUID_CONF_NEW.write_text(rendered)

        if SQUID_CONF.exists() and SQUID_CONF.read_text().strip() == rendered.strip():
            SQUID_CONF_NEW.unlink(missing_ok=True)
            self.stdout.write("Squid config unchanged, skipping reload.")
            self._write_status(version, reload_ok=True)
            return

        ok, err = validate_squid_config(rendered)
        if not ok:
            span.set_status(trace.StatusCode.ERROR, "Squid configuration validation failed")
            SQUID_CONF_NEW.unlink(missing_ok=True)
            self.stderr.write(f"Squid config validation failed: {err}")
            sys.exit(1)

        SQUID_CONF_BAK = SQUID_CONF.with_suffix(".conf.bak")  # noqa: N806
        if SQUID_CONF.exists():
            SQUID_CONF.replace(SQUID_CONF_BAK)
        SQUID_CONF_NEW.replace(SQUID_CONF)
        self.stdout.write(f"Applied new Squid config to {SQUID_CONF}")

        with tracer.start_as_current_span("squid.reload") as reload_span:
            result = subprocess.run(
                ["systemctl", "reload-or-restart", "squid"],
                capture_output=True,
                text=True,
            )
            reload_span.set_attribute("process.exit.code", result.returncode)
            if result.returncode != 0:
                reload_span.set_status(trace.StatusCode.ERROR, "Squid reload failed")
        if result.returncode != 0:
            span.set_status(trace.StatusCode.ERROR, "Squid reload failed")
            self.stderr.write(f"Squid reload failed: {(result.stderr or result.stdout).strip()}")
            if SQUID_CONF_BAK.exists():
                SQUID_CONF_BAK.replace(SQUID_CONF)
                self.stderr.write("Restored previous Squid config from backup.")
            self._write_status(version, reload_ok=False)
            sys.exit(1)
        if SQUID_CONF_BAK.exists():
            SQUID_CONF_BAK.unlink(missing_ok=True)

        self.stdout.write("Squid reloaded successfully.")
        self._write_status(version, reload_ok=True)

    def _write_status(self, version: int, reload_ok: bool) -> None:
        """Persist the applied config version and reload outcome to the status file."""
        status_path = Path(settings.TERRASQUID_STATUS_FILE)
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps(
                {
                    "applied_config_version": version,
                    "last_reload": datetime.now(UTC).isoformat(),
                    "last_reload_ok": reload_ok,
                }
            )
        )
