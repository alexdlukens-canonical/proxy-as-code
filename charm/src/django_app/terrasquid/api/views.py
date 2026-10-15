"""DRF views implementing the Terrasquid REST API."""

import json
import logging
from pathlib import Path

from django.conf import settings
from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    ACLRule,
    ConfigVersion,
    DestinationConfig,
    DestinationGroup,
    SourceACL,
)
from .permissions import ServiceAPIKeyPermission
from .policy import SquidConfigError, publish_policy
from .serializers import (
    ACLRuleSerializer,
    DestinationConfigSerializer,
    DestinationGroupSerializer,
    SourceACLSerializer,
)

logger = logging.getLogger(__name__)


class StatusView(APIView):
    """Unauthenticated endpoint returning unit sync state."""

    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        """Return the current unit's Squid sync status."""
        config_version = ConfigVersion.get()
        status_data = _load_unit_status()
        return Response(
            {
                "db_config_version": config_version.version,
                "applied_config_version": status_data.get("applied_config_version", 0),
                "last_reload": status_data.get("last_reload"),
                "last_reload_ok": status_data.get("last_reload_ok", False),
                "unit": settings.JUJU_UNIT_NAME,
            }
        )


def _load_unit_status() -> dict:
    """Load unit-local status from disk, returning defaults when absent."""
    path = Path(settings.TERRASQUID_STATUS_FILE)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to parse status file %s: %s", path, e)
    return {}


class ServiceModelViewSet(viewsets.ModelViewSet):
    """Base ViewSet that scopes all querysets to the authenticated service."""

    permission_classes = [ServiceAPIKeyPermission]
    serializer_class = None  # Must be set by subclasses

    def get_queryset(self):
        """Return only resources belonging to the authenticated service."""
        return self.queryset.filter(service=self.request.api_key.name)

    def perform_create(self, serializer) -> None:
        """Inject service and key_prefix from the authenticated API key."""
        serializer.save(
            service=self.request.api_key.name,
            key_prefix=self.request.api_key.prefix,
        )

    def perform_update(self, serializer) -> None:
        """Preserve service and key_prefix on updates."""
        serializer.save(
            service=self.request.api_key.name,
            key_prefix=self.request.api_key.prefix,
        )

    @ConfigVersion.locked()
    def create(self, request: Request, *args, **kwargs) -> Response:
        """Create or return existing resource (idempotent by (service, name))."""
        name = request.data.get("name")
        existing = self.queryset.filter(service=request.api_key.name, name=name).first() if name else None
        if existing:
            return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create_with_squid_validation(serializer)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def perform_create_with_squid_validation(self, serializer) -> None:
        """Save the model and validate Squid config, rolling back on failure."""
        from django.core.exceptions import ValidationError

        with transaction.atomic():
            try:
                self.perform_create(serializer)
            except ValidationError as e:
                raise SquidConfigError(detail=str(e)) from e
            publish_policy()

    def perform_update_with_squid_validation(self, serializer) -> None:
        """Update the model and validate Squid config, rolling back on failure."""
        from django.core.exceptions import ValidationError

        with transaction.atomic():
            try:
                self.perform_update(serializer)
            except ValidationError as e:
                raise SquidConfigError(detail=str(e)) from e
            publish_policy()


class SourceACLViewSet(ServiceModelViewSet):
    """CRUD endpoints for SourceACL resources."""

    queryset = SourceACL.objects.all()
    serializer_class = SourceACLSerializer

    @ConfigVersion.locked()
    def update(self, request: Request, *args, **kwargs) -> Response:
        """Update a SourceACL with Squid config validation."""
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update_with_squid_validation(serializer)
        return Response(serializer.data)

    @ConfigVersion.locked()
    def destroy(self, request: Request, *args, **kwargs) -> Response:
        """Delete a SourceACL, rejecting with 409 if referenced by other resources."""
        instance = self.get_object()
        if instance.rules.exists():
            return Response(
                {"detail": "Cannot delete: resource is referenced by ACL rules."},
                status=status.HTTP_409_CONFLICT,
            )
        with transaction.atomic():
            instance.delete()
            publish_policy()
        return Response(status=status.HTTP_204_NO_CONTENT)


class DestinationConfigViewSet(ServiceModelViewSet):
    """CRUD endpoints for DestinationConfig resources."""

    queryset = DestinationConfig.objects.all()
    serializer_class = DestinationConfigSerializer

    @ConfigVersion.locked()
    def update(self, request: Request, *args, **kwargs) -> Response:
        """Update a DestinationConfig with Squid config validation."""
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update_with_squid_validation(serializer)
        return Response(serializer.data)

    @ConfigVersion.locked()
    def destroy(self, request: Request, *args, **kwargs) -> Response:
        """Delete a DestinationConfig, rejecting with 409 if referenced."""
        instance = self.get_object()
        if instance.rules.exists() or instance.groups.exists():
            return Response(
                {"detail": "Cannot delete: resource is referenced by ACL rules or destination groups."},
                status=status.HTTP_409_CONFLICT,
            )
        with transaction.atomic():
            instance.delete()
            publish_policy()
        return Response(status=status.HTTP_204_NO_CONTENT)


class DestinationGroupViewSet(ServiceModelViewSet):
    """CRUD endpoints for globally readable, owner-managed destination groups."""

    queryset = DestinationGroup.objects.prefetch_related("destinations")
    serializer_class = DestinationGroupSerializer

    def get_queryset(self):
        """Return owner groups for listing or one globally shared group by exact name."""
        queryset = self.queryset
        if self.action == "list":
            name = self.request.query_params.get("name")
            if name:
                return queryset.filter(name=name)
            return queryset.filter(service=self.request.api_key.name)
        return queryset

    @ConfigVersion.locked()
    def create(self, request: Request, *args, **kwargs) -> Response:
        """Create a group or return the owner's existing group with the same name."""
        name = request.data.get("name")
        existing = self.queryset.filter(name=name).first() if name else None
        if existing:
            if existing.service == request.api_key.name:
                return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)
            return Response(
                {"detail": "A destination group with this name already exists."},
                status=status.HTTP_409_CONFLICT,
            )
        return super().create(request, *args, **kwargs)

    @ConfigVersion.locked()
    def update(self, request: Request, *args, **kwargs) -> Response:
        """Update a group with Squid config validation."""
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        if instance.service != request.api_key.name:
            return Response(
                {"detail": "Only the owning service may modify this destination group."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update_with_squid_validation(serializer)
        return Response(serializer.data)

    @ConfigVersion.locked()
    def destroy(self, request: Request, *args, **kwargs) -> Response:
        """Delete an owner-managed group unless ACL rules still reference it."""
        instance = self.get_object()
        if instance.service != request.api_key.name:
            return Response(
                {"detail": "Only the owning service may delete this destination group."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if instance.rules.exists():
            return Response(
                {"detail": "Cannot delete: resource is referenced by ACL rules."},
                status=status.HTTP_409_CONFLICT,
            )
        with transaction.atomic():
            instance.delete()
            publish_policy()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ACLRuleViewSet(ServiceModelViewSet):
    """CRUD endpoints for ACLRule resources."""

    queryset = ACLRule.objects.prefetch_related("sources", "destinations", "destination_groups__destinations")
    serializer_class = ACLRuleSerializer

    @ConfigVersion.locked()
    def update(self, request: Request, *args, **kwargs) -> Response:
        """Update an ACLRule with Squid config validation."""
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update_with_squid_validation(serializer)
        return Response(serializer.data)

    @ConfigVersion.locked()
    def destroy(self, request: Request, *args, **kwargs) -> Response:
        """Delete an ACLRule."""
        instance = self.get_object()
        with transaction.atomic():
            instance.delete()
            publish_policy()
        return Response(status=status.HTTP_204_NO_CONTENT)
