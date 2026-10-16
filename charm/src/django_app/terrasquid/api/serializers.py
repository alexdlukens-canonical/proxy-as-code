"""DRF serializers for all Terrasquid API resource types."""

import ipaddress
import re

from rest_framework import serializers

from .models import (
    ACLRule,
    DestinationConfig,
    DestinationGroup,
    SourceACL,
)
from .validators import PORT_RANGE_PATTERN, validate_port_ranges, validate_ports


class BaseResourceSerializer(serializers.ModelSerializer):
    """Read-only base fields common to all resource serializers."""

    id = serializers.UUIDField(read_only=True)
    service = serializers.CharField(read_only=True)
    key_prefix = serializers.CharField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def validate_comment(self, value: str) -> str:
        """Reject comments that could introduce extra Squid configuration lines."""
        if "\r" in value or "\n" in value:
            raise serializers.ValidationError("Comments must be a single line.")
        return value


class SourceACLSerializer(BaseResourceSerializer):
    """Serializer for SourceACL resources."""

    class Meta:
        model = SourceACL
        fields = ["id", "service", "name", "key_prefix", "cidr", "comment", "created_at", "updated_at"]

    def validate_cidr(self, value: list) -> list:
        """Validate each entry is a valid IPv4 or IPv6 CIDR."""
        for entry in value:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError as exc:
                raise serializers.ValidationError(f"'{entry}' is not a valid CIDR address.") from exc
        return value


class DestinationConfigSerializer(BaseResourceSerializer):
    """Serializer for DestinationConfig resources."""

    ports = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=65535),
        required=False,
        allow_null=True,
        validators=[validate_ports],
    )
    port_ranges = serializers.ListField(
        child=serializers.RegexField(rf"^{PORT_RANGE_PATTERN}$", trim_whitespace=False),
        required=False,
        allow_null=True,
        validators=[validate_port_ranges],
        help_text=(
            "Inclusive start-end ranges with endpoints between 1 and 65535 and start <= end. "
            "Combined with ports; defaults apply only when both lists are empty or unset."
        ),
    )

    class Meta:
        model = DestinationConfig
        fields = [
            "id",
            "service",
            "name",
            "key_prefix",
            "dst",
            "type",
            "ports",
            "port_ranges",
            "comment",
            "created_at",
            "updated_at",
        ]

    def validate_dst(self, value: str) -> str:
        """Validate that dst is a valid CIDR or a plausible hostname/domain."""
        try:
            ipaddress.ip_network(value, strict=False)
            return value
        except ValueError:
            pass
        if re.match(
            r"^(\*?\.)?[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$",
            value,
        ):
            return value
        raise serializers.ValidationError(f"'{value}' is not a valid CIDR or hostname.")


class DestinationGroupSerializer(BaseResourceSerializer):
    """Serializer for globally reusable destination groups."""

    destinations = serializers.PrimaryKeyRelatedField(
        many=True, queryset=DestinationConfig.objects.all(), pk_field=serializers.UUIDField()
    )

    class Meta:
        model = DestinationGroup
        fields = [
            "id",
            "service",
            "name",
            "key_prefix",
            "destinations",
            "comment",
            "created_at",
            "updated_at",
        ]

    def validate_destinations(self, value: list) -> list:
        """Require a group to contain at least one destination owned by its creator."""
        if not value:
            raise serializers.ValidationError("At least one destination is required.")
        service = self.context["request"].api_key.name
        for destination in value:
            if destination.service != service:
                raise serializers.ValidationError(
                    f"Destination '{destination.name}' belongs to service '{destination.service}', "
                    f"not the authenticated service '{service}'."
                )
        return value


class ACLRuleSerializer(BaseResourceSerializer):
    """Serializer for ACLRule resources."""

    sources = serializers.PrimaryKeyRelatedField(
        many=True, queryset=SourceACL.objects.all(), pk_field=serializers.UUIDField()
    )
    destinations = serializers.PrimaryKeyRelatedField(
        many=True, queryset=DestinationConfig.objects.all(), pk_field=serializers.UUIDField(), required=False
    )
    destination_groups = serializers.PrimaryKeyRelatedField(
        many=True, queryset=DestinationGroup.objects.all(), pk_field=serializers.UUIDField(), required=False
    )

    class Meta:
        model = ACLRule
        fields = [
            "id",
            "service",
            "name",
            "key_prefix",
            "priority",
            "comment",
            "sources",
            "destinations",
            "destination_groups",
            "created_at",
            "updated_at",
        ]

    def validate_sources(self, value: list) -> list:
        """Require at least one source, all belonging to the authenticated service."""
        if not value:
            raise serializers.ValidationError("At least one source is required.")
        service = self.context["request"].api_key.name
        for source_acl in value:
            if source_acl.service != service:
                raise serializers.ValidationError(
                    f"Source '{source_acl.name}' belongs to service '{source_acl.service}', "
                    f"not the authenticated service '{service}'."
                )
        return value

    def validate_destinations(self, value: list) -> list:
        """Ensure direct destinations belong to the authenticated service."""
        service = self.context["request"].api_key.name
        for destination in value:
            if destination.service != service:
                raise serializers.ValidationError(
                    f"Destination '{destination.name}' belongs to service '{destination.service}', "
                    f"not the authenticated service '{service}'."
                )
        return value

    def validate(self, attrs: dict) -> dict:
        """Require at least one direct destination or globally readable destination group."""
        if self.instance is None:
            destinations = attrs.get("destinations", [])
            destination_groups = attrs.get("destination_groups", [])
        else:
            destinations = attrs.get("destinations", self.instance.destinations.all())
            destination_groups = attrs.get("destination_groups", self.instance.destination_groups.all())
        if not destinations and not destination_groups:
            raise serializers.ValidationError("At least one destination or destination group is required.")
        return attrs
