"""Transactional validation and publication of policy changes."""

from rest_framework import status
from rest_framework.exceptions import APIException

from .models import ConfigVersion
from .squid_render import render_squid_config, validate_squid_config


class SquidConfigError(APIException):
    """Raised when Squid configuration validation fails after a write."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "squid_config_invalid"


def publish_policy() -> None:
    """Validate and publish policy while the caller holds the policy write lock."""
    with ConfigVersion.locked() as config_version:
        rendered = render_squid_config(version=config_version.version + 1)
        ok, err = validate_squid_config(rendered)
        if not ok:
            raise SquidConfigError(detail=f"Squid configuration validation failed: {err}")
        ConfigVersion.increment(rendered)


def reconcile_policy() -> bool:
    """Publish changed rendering inputs without racing another policy writer."""
    with ConfigVersion.locked() as config_version:
        rendered = render_squid_config(version=config_version.version)
        if rendered == config_version.rendered_config:
            return False
        publish_policy()
        return True
