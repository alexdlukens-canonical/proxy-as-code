"""Squid configuration rendering and validation helpers."""

import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.db.models import Prefetch
from jinja2 import Environment, FileSystemLoader
from opentelemetry import trace

tracer = trace.get_tracer(__name__)


def _get_jinja2_env() -> Environment:
    """Create a Jinja2 environment loading from the templates directory."""
    templates_dir = Path(__file__).parent.parent.parent / "templates"
    return Environment(
        loader=FileSystemLoader(str(templates_dir)),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )


@tracer.start_as_current_span("squid.render_config")
def render_squid_config(version: int | None = None) -> str:
    """Render the Squid configuration from the current database state."""
    from .models import ACLRule, ConfigVersion, DestinationConfig, SourceACL

    env = _get_jinja2_env()
    template = env.get_template("squid.conf.j2")

    config_version = ConfigVersion.get()
    if version is None:
        version = config_version.version + 1

    acl_rules = list(
        ACLRule.objects.prefetch_related(
            Prefetch("sources", queryset=SourceACL.objects.order_by("service", "name")),
            "destinations",
            "destination_groups__destinations",
        ).order_by("priority", "created_at", "service", "name")
    )
    port_sets = sorted({tuple(bucket["ports"]) for rule in acl_rules for bucket in rule.effective_destination_buckets})
    action_order = {"DENY": 0, "CONNECT": 1, "ALLOW": 2}
    acl_access_entries = sorted(
        ({"rule": rule, "bucket": bucket} for rule in acl_rules for bucket in rule.effective_destination_buckets),
        key=lambda entry: (
            entry["rule"].priority,
            action_order[entry["bucket"]["type"]],
            entry["rule"].created_at,
            entry["rule"].service,
            entry["rule"].name,
            entry["bucket"]["index"],
        ),
    )

    return template.render(
        squid_port=settings.SQUID_PORT,
        version=version,
        squid_prepend_config=settings.SQUID_PREPEND_CONFIG,
        squid_append_config=settings.SQUID_APPEND_CONFIG,
        squid_default_deny=settings.SQUID_DEFAULT_DENY,
        source_acls=list(SourceACL.objects.order_by("service", "name")),
        destination_configs=list(DestinationConfig.objects.order_by("service", "name")),
        acl_rules=acl_rules,
        acl_access_entries=acl_access_entries,
        port_sets=port_sets,
    )


@tracer.start_as_current_span("squid.validate_config")
def validate_squid_config(config_text: str) -> tuple[bool, str]:
    """Dry-run validate a Squid config string.

    Returns (True, '') on success or (False, error_message) on failure.
    Falls back to (True, '') when the squid binary is not present.
    """
    squid_bin = settings.SQUID_BINARY
    if not Path(squid_bin).exists():
        return True, ""

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / "squid.conf"
            tmp_path.write_text(config_text)
            result = subprocess.run(
                [squid_bin, "-k", "parse", "-f", str(tmp_path)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                trace.get_current_span().set_status(trace.StatusCode.ERROR, "Squid configuration validation failed")
                return False, (result.stderr or result.stdout).strip()
            return True, ""
    except subprocess.TimeoutExpired:
        trace.get_current_span().set_status(trace.StatusCode.ERROR, "Squid configuration validation timed out")
        return False, "Squid config validation timed out."
