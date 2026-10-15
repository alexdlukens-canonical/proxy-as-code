"""Tests for API-key management commands and credential lifecycle."""

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework_api_key.models import APIKey

pytestmark = pytest.mark.django_db


def _run_command(command: str, **options) -> str:
    output = StringIO()
    call_command(command, stdout=output, **options)
    return output.getvalue()


class TestAPIKeyCommands:
    """Exercise persisted key state as well as command output."""

    def test_create_returns_a_working_key_and_prefix(self):
        """A newly issued plaintext key authenticates against the stored hash."""
        output = _run_command("create_api_key", name="team-a")
        values = dict(line.split("=", 1) for line in output.splitlines())
        key = APIKey.objects.get(name="team-a")

        assert values["prefix"] == key.prefix
        assert key.is_valid(values["key"])
        assert not key.revoked

    def test_create_rejects_duplicate_active_names(self):
        """Duplicate active names must not issue another credential."""
        APIKey.objects.create_key(name="team-a")

        with pytest.raises(CommandError, match="already exists"):
            _run_command("create_api_key", name="team-a")

        assert APIKey.objects.filter(name="team-a").count() == 1

    def test_create_allows_reusing_a_revoked_name(self):
        """A revoked credential does not reserve the service name."""
        old_key, _ = APIKey.objects.create_key(name="team-a", revoked=True)

        _run_command("create_api_key", name="team-a")

        assert APIKey.objects.filter(name="team-a", revoked=False).count() == 1
        old_key.refresh_from_db()
        assert old_key.revoked

    def test_list_empty(self):
        """An empty key inventory has an explicit message."""
        assert _run_command("list_api_keys") == "No API keys found.\n"

    def test_list_orders_names_and_includes_revocation_without_plaintext(self):
        """Listing reports metadata in name order without disclosing keys."""
        revoked, revoked_plaintext = APIKey.objects.create_key(name="zulu", revoked=True)
        active, active_plaintext = APIKey.objects.create_key(name="alpha")

        output = _run_command("list_api_keys")

        assert output.splitlines() == [
            f"alpha\tprefix={active.prefix}\tcreated={active.created}\tactive",
            f"zulu\tprefix={revoked.prefix}\tcreated={revoked.created}\tREVOKED",
        ]
        assert active_plaintext not in output
        assert revoked_plaintext not in output

    def test_revoke_preserves_other_keys(self):
        """Revocation only disables the named active credential."""
        key, _ = APIKey.objects.create_key(name="team-a")
        other, _ = APIKey.objects.create_key(name="team-b")

        output = _run_command("revoke_api_key", name="team-a")

        key.refresh_from_db()
        other.refresh_from_db()
        assert key.revoked
        assert not other.revoked
        assert output == "Revoked API key 'team-a'.\n"

    @pytest.mark.parametrize("command", ["revoke_api_key", "rotate_api_key"])
    @pytest.mark.parametrize("revoked", [None, True])
    def test_missing_active_key_is_rejected(self, command, revoked):
        """Missing and already revoked credentials cannot be revoked or rotated."""
        if revoked:
            APIKey.objects.create_key(name="team-a", revoked=True)

        with pytest.raises(CommandError, match="No active API key"):
            _run_command(command, name="team-a")

        assert not APIKey.objects.filter(revoked=False).exists()

    def test_rotate_revokes_old_key_and_returns_valid_replacement(self):
        """Rotation issues a different key while preserving the service name."""
        old_key, old_plaintext = APIKey.objects.create_key(name="team-a")

        output = _run_command("rotate_api_key", name="team-a")

        replacement_plaintext = output.strip().split("=", 1)[1]
        replacement = APIKey.objects.get(name="team-a", revoked=False)
        old_key.refresh_from_db()
        assert old_key.revoked
        assert replacement.pk != old_key.pk
        assert replacement_plaintext != old_plaintext
        assert replacement.is_valid(replacement_plaintext)
        assert APIKey.objects.filter(name="team-a").count() == 2

    def test_failed_rotation_keeps_original_key_active(self):
        """Failure to create a replacement must roll back the revocation."""
        old_key, plaintext = APIKey.objects.create_key(name="team-a")

        with (
            patch.object(APIKey.objects, "create_key", side_effect=RuntimeError("Key creation failed")),
            pytest.raises(RuntimeError, match="Key creation failed"),
        ):
            _run_command("rotate_api_key", name="team-a")

        old_key.refresh_from_db()
        assert not old_key.revoked
        assert old_key.is_valid(plaintext)
        assert APIKey.objects.filter(name="team-a").count() == 1
