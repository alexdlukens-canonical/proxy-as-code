"""Unit tests for the Terrasquid Prometheus metrics endpoint."""

import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from terrasquid.api.models import ConfigVersion


@override_settings(METRICS_USERNAME="metrics", METRICS_PASSWORD="test-password")
class TestMetricsEndpoint(TestCase):
    """Tests for authenticated Prometheus metrics."""

    def test_rejects_unauthenticated_clients(self) -> None:
        """Local and remote clients must authenticate, regardless of forwarded headers."""
        for address in ("127.0.0.1", "::1", "192.0.2.1"):
            for path in ("/metrics", "/metrics/", "/metrics?test=1"):
                response = APIClient().get(path, REMOTE_ADDR=address, HTTP_X_FORWARDED_FOR="127.0.0.1")
                assert response.status_code == 401
                assert response["WWW-Authenticate"] == 'Basic realm="metrics"'

    def test_rejects_invalid_authorization(self) -> None:
        """Malformed headers and incorrect credentials must never expose metrics."""
        for authorization in (
            "Basic",
            "Basic !!!!",
            "Basic /w==",
            "Bearer token",
            "Basic " + base64.b64encode(b"metrics:wrong").decode(),
            "Basic " + base64.b64encode(b"wrong:test-password").decode(),
        ):
            response = APIClient().get("/metrics", HTTP_AUTHORIZATION=authorization)
            assert response.status_code == 401

    def test_fails_closed_without_credentials(self) -> None:
        """Unconfigured credentials must disable metrics with an explicit error."""
        for credentials in (
            {"METRICS_USERNAME": "", "METRICS_PASSWORD": ""},
            {"METRICS_USERNAME": "metrics", "METRICS_PASSWORD": ""},
            {"METRICS_USERNAME": "", "METRICS_PASSWORD": "test-password"},
        ):
            with self.settings(**credentials), self.assertLogs("terrasquid.middleware", level="ERROR"):
                response = APIClient().get("/metrics")
            assert response.status_code == 503

    def test_does_not_restrict_other_endpoints(self) -> None:
        """Public health checks must remain accessible without metrics credentials."""
        response = APIClient().get("/api/v1/status/", REMOTE_ADDR="192.0.2.1")
        assert response.status_code == 200

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
                authorization = "Basic " + base64.b64encode(b"metrics:test-password").decode()
                response = APIClient().get("/metrics", REMOTE_ADDR="192.0.2.1", HTTP_AUTHORIZATION=authorization)

        assert response.status_code == 200
        assert b'terrasquid_squid_config_desired_version{unit="terrasquid/0"} 7.0' in response.content
        assert b'terrasquid_squid_config_applied_version{unit="terrasquid/0"} 5.0' in response.content
        assert b'terrasquid_squid_config_version_skew{unit="terrasquid/0"} 2.0' in response.content
