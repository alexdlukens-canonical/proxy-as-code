"""Tests for byte-bounded Squid ACL rendering."""

import pytest
from terrasquid.api.models import ACLRule, DestinationConfig, DestinationGroup, SourceACL
from terrasquid.api.squid_render import MAX_ACL_LINE_BYTES, _any_of_lines, render_squid_config


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
