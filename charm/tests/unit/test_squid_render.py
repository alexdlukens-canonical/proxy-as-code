"""Tests for byte-bounded Squid ACL rendering."""

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from terrasquid.api.models import ACLRule, DestinationConfig, DestinationGroup, SourceACL
from terrasquid.api.squid_render import MAX_ACL_LINE_BYTES, _any_of_lines, render_squid_config
from terrasquid.api.validators import validate_port_ranges, validate_ports


@pytest.mark.parametrize("action,default", [("ALLOW", 80), ("DENY", 80), ("CONNECT", 443)])
@pytest.mark.parametrize("ports,ranges", [(None, None), ([], []), ([443, 80, 443], None), ([], ["30000-32767"])])
def test_effective_ports(action, default, ports, ranges):
    """Legacy defaults and numeric ordering survive adding optional ranges."""
    destination = DestinationConfig(type=action, ports=ports, port_ranges=ranges)
    expected = [80, 443] if ports else ranges or [default]
    assert destination.effective_ports() == expected


@pytest.mark.parametrize(
    "validator,value",
    [
        (validate_ports, [True]),
        (validate_ports, ["443"]),
        (validate_ports, [0]),
        (validate_ports, [65536]),
        (validate_ports, {}),
        (validate_port_ranges, ["0-80"]),
        (validate_port_ranges, ["80-65536"]),
        (validate_port_ranges, ["90-80"]),
        (validate_port_ranges, ["80-90\n"]),
        (validate_port_ranges, [80]),
        (validate_port_ranges, {}),
    ],
)
def test_model_port_validation(validator, value):
    """Model validators also reject unsafe restrictions outside the REST API."""
    with pytest.raises(ValidationError):
        validator(value)


@pytest.mark.django_db(transaction=True)
def test_port_ranges_migration_preserves_existing_destinations():
    """Upgrading existing destinations leaves ports intact and ranges unset."""
    previous = [("api", "0007_seed_config_version")]
    current = [("api", "0008_destinationconfig_port_ranges")]
    executor = MigrationExecutor(connection)
    executor.migrate(previous)
    try:
        old_destination = executor.loader.project_state(previous).apps.get_model("api", "DestinationConfig")
        originals = [
            old_destination.objects.create(
                service="upgrade",
                key_prefix="ab12cd34",
                name=f"legacy-{index}",
                dst="example.com",
                type=action,
                ports=ports,
            )
            for index, (action, ports) in enumerate([("ALLOW", None), ("CONNECT", []), ("DENY", [443, 80])])
        ]
        MigrationExecutor(connection).migrate(current)
        for original, expected in zip(originals, [[80], [443], [80, 443]], strict=True):
            destination = DestinationConfig.objects.get(pk=original.pk)
            assert destination.ports == original.ports
            assert destination.port_ranges is None
            assert destination.effective_ports() == expected
    finally:
        MigrationExecutor(connection).migrate(current)


@pytest.mark.django_db
@pytest.mark.parametrize("grouped", [False, True])
def test_render_port_ranges(grouped):
    """Direct and grouped destinations keep distinct port restrictions and defaults."""
    source = SourceACL.objects.create(service="test", key_prefix="ab12cd34", name="source", cidr=["10.0.0.0/8"])
    rule = ACLRule.objects.create(service="test", key_prefix="ab12cd34", name="ranges")
    rule.sources.add(source)
    destinations = [
        DestinationConfig.objects.create(
            service="test",
            key_prefix="ab12cd34",
            name=name,
            dst=f"{name}.example.com",
            type="ALLOW",
            ports=ports,
            port_ranges=ranges,
        )
        for name, ports, ranges in [
            ("legacy", [443], None),
            ("default", [], None),
            ("ranges", [], ["30000-32767"]),
            ("combined", [443, 80], ["30000-32767", "30000-32767"]),
        ]
    ]
    if grouped:
        group = DestinationGroup.objects.create(service="test", key_prefix="ab12cd34", name="ranges")
        group.destinations.add(*destinations)
        rule.destination_groups.add(group)
    else:
        rule.destinations.add(*destinations)
    config = render_squid_config()
    port_lines = [line for line in config.splitlines() if line.startswith("acl port__")]
    assert port_lines == [
        "acl port__80 port 80",
        "acl port__80_443_30000-32767 port 80 443 30000-32767",
        "acl port__443 port 443",
        "acl port__30000-32767 port 30000-32767",
    ]
    assert len(rule.effective_destination_buckets) == 4
    access_lines = [line for line in config.splitlines() if line.startswith("http_access allow src__")]
    assert len(access_lines) == 4
    assert all(any(line.endswith(f" {port_line.split()[1]}") for line in access_lines) for port_line in port_lines)
    assert render_squid_config() == config
    for destination in destinations[:2]:
        destination.port_ranges = []
        destination.save()
    assert render_squid_config() == config


@pytest.mark.parametrize("names", [[], ["first"], ["first", "second"]])
def test_short_any_of_lines(names):
    """Small groups keep their original representation."""
    expected = [f"acl group any-of {' '.join(names)}"] if names else []
    assert _any_of_lines(names, "group") == expected


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_any_of_line_byte_boundary(offset):
    """The full prefix and separating spaces count toward the line limit."""
    prefix = "acl group any-of "
    first = "a" * (MAX_ACL_LINE_BYTES - len(prefix) - 2 + offset)
    lines = _any_of_lines([first, "b"], "group")

    assert len(lines) == (2 if offset > 0 else 1)
    assert all(len(line.encode("utf-8")) <= MAX_ACL_LINE_BYTES for line in lines)
    assert [name for line in lines for name in line.split()[3:]] == [first, "b"]
    if offset == 0:
        assert len(lines[0].encode("utf-8")) == MAX_ACL_LINE_BYTES


def test_any_of_counts_utf8_bytes():
    """Multibyte characters cannot make the encoded line overflow."""
    names = ["\u00e9" * 2100, "\u00e9" * 2100]
    lines = _any_of_lines(names, "group")

    assert len(lines) == 2
    assert all(len(line.encode("utf-8")) <= MAX_ACL_LINE_BYTES for line in lines)
    assert [name for line in lines for name in line.split()[3:]] == names


def test_any_of_rejects_oversized_reference():
    """An unsplittable reference raises an explicit rendering error."""
    with pytest.raises(ValueError, match="cannot fit on an any-of line"):
        _any_of_lines(["a" * MAX_ACL_LINE_BYTES], "group")


@pytest.mark.django_db
def test_render_splits_all_any_of_groups():
    """Large source, destination, and reusable groups retain every reference."""
    sources = SourceACL.objects.bulk_create(
        [
            SourceACL(
                service="test",
                key_prefix="ab12cd34",
                name=f"source-{index:03d}-" + "s" * 52,
                cidr=["10.0.0.0/8"],
            )
            for index in range(120)
        ]
    )
    destinations = DestinationConfig.objects.bulk_create(
        [
            DestinationConfig(
                service="test",
                key_prefix="ab12cd34",
                name=f"dest-{index:03d}-" + "d" * 54,
                dst=f"site-{index}.example.com",
                type="ALLOW",
            )
            for index in range(240)
        ]
    )
    group = DestinationGroup.objects.create(service="test", key_prefix="ab12cd34", name="sites")
    group.destinations.add(*destinations[:120])
    rule = ACLRule.objects.create(service="test", key_prefix="ab12cd34", name="rule")
    rule.sources.add(*sources)
    rule.destinations.add(*destinations[120:])
    rule.destination_groups.add(group)

    config = render_squid_config()
    expected_members = {
        "dst_group__ab12cd34__sites__1": [f"dst__ab12cd34__{item.name}" for item in destinations[:120]],
        "rule_src__ab12cd34__rule": [f"src__ab12cd34__{item.name}" for item in sources],
        "rule_dst__ab12cd34__rule__1": sorted(
            ["dst_group__ab12cd34__sites__1"] + [f"dst__ab12cd34__{item.name}" for item in destinations[120:]]
        ),
    }
    for acl_name, members in expected_members.items():
        lines = [line for line in config.splitlines() if line.startswith(f"acl {acl_name} any-of ")]
        assert len(lines) > 1
        assert all(len(line.encode("utf-8")) <= MAX_ACL_LINE_BYTES for line in lines)
        assert [name for line in lines for name in line.split()[3:]] == members
    assert config.count("http_access allow rule_src__ab12cd34__rule rule_dst__ab12cd34__rule__1 port__80") == 1
