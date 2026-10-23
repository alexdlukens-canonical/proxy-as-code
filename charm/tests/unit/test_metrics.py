"""Unit tests for the Terrasquid Prometheus metrics endpoint."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from terrasquid.api.models import ConfigVersion


class TestMetricsEndpoint(TestCase):
    """Tests for unauthenticated Prometheus metrics."""

    def test_rejects_non_loopback_clients(self) -> None:
        """Metrics must not be served to clients outside localhost."""
        response = APIClient().get("/metrics", REMOTE_ADDR="192.0.2.1")

        assert response.status_code == 403

    @override_settings(JUJU_UNIT_NAME="terrasquid/0")
    def test_reports_deployed_squid_configuration_version(self) -> None:
        """Metrics must distinguish the database version from this unit's deployed version."""
        config_version = ConfigVersion.get()
        config_version.version = 7
        config_version.save()

        with TemporaryDirectory() as directory:
            status_file = Path(directory) / "status.json"
            status_file.write_text(json.dumps({"applied_config_version": 5}))
            with (
                self.settings(TERRASQUID_STATUS_FILE=str(status_file)),
                patch("terrasquid.metrics._service_running", return_value=1),
            ):
                response = APIClient().get("/metrics")

        assert response.status_code == 200
        assert b'terrasquid_squid_config_desired_version{unit="terrasquid/0"} 7.0' in response.content
        assert b'terrasquid_squid_config_applied_version{unit="terrasquid/0"} 5.0' in response.content
        assert b'terrasquid_squid_config_version_skew{unit="terrasquid/0"} 2.0' in response.content
