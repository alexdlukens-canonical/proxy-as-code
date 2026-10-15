"""Tests for transactional policy publication and reconciliation."""

from unittest.mock import patch

from django.test import TestCase
from terrasquid.api.models import ConfigVersion, RenderedConfigHistory, SourceACL
from terrasquid.api.policy import SquidConfigError, publish_policy, reconcile_policy


class TestPolicyPublication(TestCase):
    """Policy, version, and history must commit or roll back together."""

    @patch("terrasquid.api.policy.validate_squid_config", return_value=(True, ""))
    def test_reconcile_publishes_once(self, validate) -> None:
        """An unchanged policy must not cause repeated reconciliation versions."""
        assert reconcile_policy()
        config = ConfigVersion.get()
        assert config.version == 1
        assert RenderedConfigHistory.objects.get(version=1).rendered_config == config.rendered_config
        assert not reconcile_policy()
        assert ConfigVersion.get().version == 1
        validate.assert_called_once_with(config.rendered_config)

    @patch("terrasquid.api.policy.validate_squid_config", return_value=(False, "syntax error"))
    def test_failed_mutation_rolls_back_all_state(self, validate) -> None:
        """Failed validation cannot publish policy, history, or a new version."""
        with self.assertRaises(SquidConfigError), ConfigVersion.locked():
            SourceACL.objects.create(service="policy-test", name="invalid", key_prefix="test1234", cidr=["10.0.0.0/8"])
            publish_policy()

        assert not SourceACL.objects.exists()
        assert ConfigVersion.get().version == 0
        assert not RenderedConfigHistory.objects.exists()
        validate.assert_called_once()

    @patch("terrasquid.api.policy.validate_squid_config", return_value=(False, "syntax error"))
    def test_failed_reconciliation_does_not_publish(self, validate) -> None:
        """Reconciliation must validate charm rendering inputs before publication."""
        with self.assertRaises(SquidConfigError):
            reconcile_policy()
        assert ConfigVersion.get().version == 0
        assert not RenderedConfigHistory.objects.exists()
        validate.assert_called_once()
