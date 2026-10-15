"""Squid configuration rendering and validation helpers."""

import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.db.models import Prefetch
from jinja2 import Environment, FileSystemLoader
from opentelemetry import trace

from .models import port_sort_key

tracer = trace.get_tracer(__name__)

# Squid reads physical lines into an 8192-byte buffer, including newline and NUL.
MAX_ACL_LINE_BYTES = 8190


def _any_of_lines(acl_names: list[str], acl_name: str) -> list[str]:
    """Split an OR group into byte-bounded definitions of the same ACL."""
    prefix = f"acl {acl_name} any-of"
    prefix_bytes = len(prefix.encode("utf-8"))
    lines = []
    members = []
    line_bytes = prefix_bytes
    for name in acl_names:
        member_bytes = 1 + len(name.encode("utf-8"))
        if prefix_bytes + member_bytes > MAX_ACL_LINE_BYTES:
            raise ValueError(f"ACL reference {name!r} cannot fit on an any-of line for {acl_name!r}")
        if line_bytes + member_bytes > MAX_ACL_LINE_BYTES:
            lines.append(f"{prefix} {' '.join(members)}")
            members = []
            line_bytes = prefix_bytes
        members.append(name)
        line_bytes += member_bytes
    if members:
        lines.append(f"{prefix} {' '.join(members)}")
    return lines


def _get_jinja2_env() -> Environment:
    """Create a Jinja2 environment loading from the templates directory."""
    templates_dir = Path(__file__).parent.parent.parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )
    env.filters["any_of_lines"] = _any_of_lines
    return env


def _destination_acl_name(destination) -> str:
    return f"dst__{destination.key_prefix}__{destination.name}"


def _destination_group_acl_name(group, index: int) -> str:
    return f"dst_group__{group.key_prefix}__{group.name}__{index}"


def _source_acl_name(source) -> str:
    return f"src__{source.key_prefix}__{source.name}"


def _build_acl_render_plan(acl_rules: list) -> tuple[list[dict], list[dict]]:
    referenced_groups = {group.id: group for rule in acl_rules for group in rule.destination_groups.all()}
    destination_group_entries = []
    group_buckets_by_id = {}
    for group in sorted(referenced_groups.values(), key=lambda item: (item.service, item.name)):
        grouped_destinations = {}
        for destination in group.destinations.all():
            key = (destination.type, tuple(destination.effective_ports()))
            grouped_destinations.setdefault(key, {})[destination.id] = destination

        buckets = []
        for index, (key, destinations_by_id) in enumerate(
            sorted(grouped_destinations.items(), key=lambda item: (item[0][0], port_sort_key(item[0][1]))),
            start=1,
        ):
            destinations = sorted(
                destinations_by_id.values(),
                key=lambda item: (item.service, item.name),
            )
            buckets.append(
                {
                    "key": key,
                    "acl_name": _destination_group_acl_name(group, index),
                    "destination_ids": frozenset(destinations_by_id),
                    "destination_acl_names": [_destination_acl_name(destination) for destination in destinations],
                }
            )
        destination_group_entries.append({"group": group, "buckets": buckets})
        group_buckets_by_id[group.id] = {bucket["key"]: bucket for bucket in buckets}

    rule_entries = []
    for rule in acl_rules:
        source_acl_names = [_source_acl_name(source) for source in rule.sources.all()]
        source_acl_name = (
            source_acl_names[0] if len(source_acl_names) == 1 else f"rule_src__{rule.key_prefix}__{rule.name}"
        )
        references_by_key = {}
        covered_destination_ids = set()
        for group in sorted(rule.destination_groups.all(), key=lambda item: (item.service, item.name)):
            for key, bucket in group_buckets_by_id[group.id].items():
                references_by_key.setdefault(key, []).append(bucket["acl_name"])
                covered_destination_ids.update(bucket["destination_ids"])

        for destination in rule.destinations.all():
            if destination.id in covered_destination_ids:
                continue
            key = (destination.type, tuple(destination.effective_ports()))
            references_by_key.setdefault(key, []).append(_destination_acl_name(destination))

        buckets = []
        for index, ((action_type, ports), acl_names) in enumerate(
            sorted(references_by_key.items(), key=lambda item: (item[0][0], port_sort_key(item[0][1]))),
            start=1,
        ):
            destination_acl_names = sorted(set(acl_names))
            buckets.append(
                {
                    "index": index,
                    "type": action_type,
                    "ports": ports,
                    "destination_acl_name": (
                        destination_acl_names[0]
                        if len(destination_acl_names) == 1
                        else f"rule_dst__{rule.key_prefix}__{rule.name}__{index}"
                    ),
                    "destination_acl_names": destination_acl_names,
                }
            )
        rule_entries.append(
            {
                "rule": rule,
                "source_acl_name": source_acl_name,
                "source_acl_names": source_acl_names,
                "buckets": buckets,
            }
        )

    return destination_group_entries, rule_entries


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
    destination_group_entries, rule_entries = _build_acl_render_plan(acl_rules)
    port_sets = sorted(
        {tuple(bucket["ports"]) for entry in rule_entries for bucket in entry["buckets"]}, key=port_sort_key
    )
    action_order = {"DENY": 0, "CONNECT": 1, "ALLOW": 2}
    acl_access_entries = sorted(
        (
            {
                "rule": entry["rule"],
                "source_acl_name": entry["source_acl_name"],
                "bucket": bucket,
            }
            for entry in rule_entries
            for bucket in entry["buckets"]
        ),
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
        destination_group_entries=destination_group_entries,
        rule_entries=rule_entries,
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
