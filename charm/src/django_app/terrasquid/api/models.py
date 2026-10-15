"""Django models for the Terrasquid API."""

import ipaddress
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.db import models, transaction
from django.db.models import F
from django.utils import timezone

NAME_PATTERN = r"^[a-zA-Z0-9_-]+$"
SERVICE_PATTERN = r"^[a-zA-Z0-9_-]+$"


class BaseResource(models.Model):
    """Abstract base model shared by all API-managed resources."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.CharField(max_length=255)
    name = models.CharField(max_length=63)
    key_prefix = models.CharField(max_length=8)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SourceACL(BaseResource):
    """A source access control list entry representing a set of CIDRs."""

    cidr = models.JSONField(default=list)
    comment = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        unique_together = [("service", "name")]

    def __str__(self) -> str:
        """Return the string representation."""
        return f"{self.service}/{self.name}"


class DestinationConfig(BaseResource):
    """A destination rule specifying a host/CIDR and the action to apply."""

    class ActionType(models.TextChoices):
        ALLOW = "ALLOW", "Allow"
        DENY = "DENY", "Deny"
        CONNECT = "CONNECT", "Connect (HTTPS CONNECT tunnel)"

    dst = models.TextField()
    type = models.CharField(max_length=10, choices=ActionType.choices)
    ports = models.JSONField(default=list, null=True, blank=True)
    comment = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        unique_together = [("service", "name")]

    def __str__(self) -> str:
        """Return the string representation."""
        return f"{self.service}/{self.name}"

    @property
    def is_cidr(self) -> bool:
        """Return True if the dst field is an IP network (CIDR) rather than a domain."""
        try:
            ipaddress.ip_network(self.dst, strict=False)
            return True
        except ValueError:
            return False

    def effective_ports(self) -> list[int]:
        """Return the configured ports, defaulting by action type when none are set."""
        result: set[int] = set(self.ports or [])
        if not result:
            result = {443} if self.type == self.ActionType.CONNECT else {80}
        return sorted(result)


class DestinationGroup(BaseResource):
    """A globally named collection of reusable destination configurations."""

    comment = models.CharField(max_length=255, blank=True, default="")
    destinations = models.ManyToManyField(DestinationConfig, related_name="groups")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["name"], name="unique_destination_group_name")]

    def __str__(self) -> str:
        """Return the string representation."""
        return self.name


class ACLRule(BaseResource):
    """A Squid ACL rule pairing sources and destinations with a priority."""

    priority = models.IntegerField(default=100)
    comment = models.CharField(max_length=255, blank=True, default="")
    sources = models.ManyToManyField(SourceACL, related_name="rules")
    destinations = models.ManyToManyField(DestinationConfig, related_name="rules")
    destination_groups = models.ManyToManyField(DestinationGroup, related_name="rules", blank=True)

    class Meta:
        unique_together = [("service", "name")]

    def __str__(self) -> str:
        """Return the string representation."""
        return f"{self.service}/{self.name}"

    @property
    def effective_destinations(self) -> list[DestinationConfig]:
        """Return direct and grouped destinations without duplicate entries."""
        destinations = {destination.id: destination for destination in self.destinations.all()}
        for destination_group in self.destination_groups.all():
            for destination in destination_group.destinations.all():
                destinations[destination.id] = destination
        return list(destinations.values())

    @property
    def effective_destination_buckets(self) -> list[dict]:
        """Return effective destinations partitioned into safe Squid access-list buckets."""
        buckets: dict[tuple[str, tuple[int, ...], str], list[DestinationConfig]] = {}
        for destination in self.effective_destinations:
            destination_kind = "dst" if destination.is_cidr else "dstdomain"
            key = (destination.type, tuple(destination.effective_ports()), destination_kind)
            buckets.setdefault(key, []).append(destination)

        return [
            {
                "index": index,
                "type": action_type,
                "ports": ports,
                "destination_kind": destination_kind,
                "destinations": sorted(destinations, key=lambda destination: destination.dst),
            }
            for index, ((action_type, ports, destination_kind), destinations) in enumerate(
                sorted(buckets.items()), start=1
            )
        ]


class ConfigVersion(models.Model):
    """Singleton tracking the current rendered Squid configuration version.

    **Design**: This model maintains a single record (pk=1) storing:
    - The current version number (auto-incremented on each config change)
    - The rendered config string (for comparison and rollback)
    - Timestamp of last update

    **Relationship to RenderedConfigHistory**:
    - ConfigVersion represents the *current* state; RenderedConfigHistory stores *history*.
    - When ConfigVersion is incremented, a new RenderedConfigHistory entry is created.
    - This two-model design enables squid-pinned-config-version to reference historical versions.
    - Without RenderedConfigHistory, pinning would require storing all old configs in ConfigVersion.

    See RenderedConfigHistory for the historical record and pinning mechanism.
    """

    version = models.IntegerField(default=0)
    rendered_config = models.TextField(default="")
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs) -> None:
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls) -> "ConfigVersion":
        """Return the singleton ConfigVersion, creating it if absent."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    @contextmanager
    def locked(cls) -> Iterator["ConfigVersion"]:
        """Serialize policy reads, writes, and publication until transaction completion."""
        with transaction.atomic():
            cls.get()
            yield cls.objects.select_for_update().get(pk=1)

    @classmethod
    def increment(cls, rendered_config: str) -> "ConfigVersion":
        """Bump the version counter, store the new rendered config, and save to history."""
        with cls.locked() as obj:
            cls.objects.filter(pk=1).update(
                version=F("version") + 1,
                rendered_config=rendered_config,
                updated_at=timezone.now(),
            )
            obj.refresh_from_db()
            RenderedConfigHistory.objects.create(version=obj.version, rendered_config=rendered_config)
            RenderedConfigHistory.prune(pinned_version=settings.SQUID_PINNED_CONFIG_VERSION)
            return obj


class RenderedConfigHistory(models.Model):
    """Stores retained rendered Squid configurations, enabling pinning.

    **Design**: Store every config from the trailing 24 hours and one config per
    older UTC day.

    **Use case**: The squid-pinned-config-version charm config option allows operators to
    "freeze" at a specific version. When pinned, the watcher reads the config from this
    table (via ConfigVersion.increment()) instead of re-rendering from the database.

    **Relationship to ConfigVersion**:
    - ConfigVersion.increment() automatically creates a new RenderedConfigHistory entry.
    - This model stores the history; ConfigVersion stores the current pointer.
    - Retained entries are immutable; retention cleanup only deletes superseded entries.

    See ConfigVersion for the current state; this model for historical records.
    """

    version = models.IntegerField(unique=True)
    rendered_config = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-version"]

    def __str__(self) -> str:
        """Return the string representation."""
        return f"v{self.version}"

    @classmethod
    def prune(cls, *, pinned_version: int = 0, now: datetime | None = None) -> int:
        """Delete superseded entries outside the trailing 24-hour window."""
        cutoff = (now or timezone.now()) - timedelta(hours=24)
        old_entries = list(
            cls.objects.filter(created_at__lt=cutoff)
            .only("pk", "version", "created_at")
            .order_by("-created_at", "-version")
        )
        retained_by_day = {}

        for entry in old_entries:
            day = entry.created_at.astimezone(UTC).date()
            retained_by_day.setdefault(day, entry.pk)
            if entry.version == pinned_version:
                retained_by_day[day] = entry.pk

        retained_ids = tuple(retained_by_day.values())
        deleted, _ = cls.objects.filter(created_at__lt=cutoff).exclude(pk__in=retained_ids).delete()
        return deleted
