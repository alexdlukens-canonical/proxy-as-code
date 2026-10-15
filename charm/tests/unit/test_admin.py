"""Unit tests for the Terrasquid Django admin."""

from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import path
from terrasquid.api.admin import RenderedConfigHistoryAdmin
from terrasquid.api.models import ACLRule, ConfigVersion, DestinationConfig, RenderedConfigHistory, SourceACL

urlpatterns = [path("admin/", admin.site.urls)]


@override_settings(ROOT_URLCONF=__name__)
class TestRenderedConfigHistoryAdmin(TestCase):
    """Tests for immutable rendered configuration history in Django admin."""

    def setUp(self) -> None:
        self.model_admin = admin.site._registry[RenderedConfigHistory]
        self.user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="password",
        )
        self.request = RequestFactory().get("/admin/")
        self.request.user = self.user

    def test_history_is_registered_read_only(self) -> None:
        assert isinstance(self.model_admin, RenderedConfigHistoryAdmin)
        assert self.model_admin.list_display == ("version", "created_at")
        assert self.model_admin.ordering == ("-version",)
        assert self.model_admin.readonly_fields == ("version", "rendered_config", "created_at")

    def test_history_permissions_allow_view_only(self) -> None:
        history = RenderedConfigHistory.objects.create(version=1, rendered_config="# config v1")

        assert self.model_admin.has_view_permission(self.request, history)
        assert not self.model_admin.has_add_permission(self.request)
        assert not self.model_admin.has_change_permission(self.request, history)
        assert not self.model_admin.has_delete_permission(self.request, history)

    def test_history_detail_displays_full_rendered_config(self) -> None:
        history = RenderedConfigHistory.objects.create(
            version=1,
            rendered_config="# Config version: 1\nhttp_access deny all",
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/admin/api/renderedconfighistory/{history.pk}/change/")

        assert response.status_code == 200
        self.assertContains(response, "# Config version: 1")
        self.assertContains(response, "http_access deny all")
        self.assertNotContains(response, "Save and continue editing")
        self.assertNotContains(response, "Delete")


@override_settings(ROOT_URLCONF=__name__)
class TestPolicyResourceAdmin(TestCase):
    """Admin writes must validate and publish policy atomically."""

    def setUp(self) -> None:
        self.user = get_user_model().objects.create_superuser(username="policy-admin", password="test-password")
        self.client.force_login(self.user)
        self.source = SourceACL.objects.create(
            service="admin-test", name="source", key_prefix="test1234", cidr=["10.0.0.0/8"]
        )
        self.change_url = f"/admin/api/sourceacl/{self.source.pk}/change/"
        self.data = {
            "service": self.source.service,
            "name": self.source.name,
            "key_prefix": self.source.key_prefix,
            "cidr": '["192.168.0.0/16"]',
            "comment": "updated",
            "_save": "Save",
        }

    def test_current_version_is_read_only(self) -> None:
        """Operators must not edit or delete the singleton used for locking."""
        request = RequestFactory().get("/admin/")
        request.user = self.user
        model_admin = admin.site._registry[ConfigVersion]
        assert model_admin.has_view_permission(request)
        assert not model_admin.has_add_permission(request)
        assert not model_admin.has_change_permission(request, ConfigVersion.get())
        assert not model_admin.has_delete_permission(request, ConfigVersion.get())

    @patch("terrasquid.api.policy.validate_squid_config", return_value=(True, ""))
    def test_change_publishes_policy(self, validate) -> None:
        """An admin edit immediately publishes the validated configuration."""
        response = self.client.post(self.change_url, self.data)
        assert response.status_code == 302
        self.source.refresh_from_db()
        assert self.source.cidr == ["192.168.0.0/16"]
        config = ConfigVersion.get()
        assert config.version == 1
        assert "192.168.0.0/16" in config.rendered_config
        assert RenderedConfigHistory.objects.get(version=1).rendered_config == config.rendered_config
        validate.assert_called_once_with(config.rendered_config)

    @patch("terrasquid.api.policy.validate_squid_config", return_value=(False, "syntax error"))
    def test_invalid_change_rolls_back_and_reports_error(self, validate) -> None:
        """Invalid admin edits must not leave policy or publication changes behind."""
        response = self.client.post(self.change_url, self.data, follow=True)
        self.assertContains(response, "Squid configuration validation failed: syntax error")
        self.source.refresh_from_db()
        assert self.source.cidr == ["10.0.0.0/8"]
        assert ConfigVersion.get().version == 0
        assert not RenderedConfigHistory.objects.exists()
        validate.assert_called_once()

    @patch("terrasquid.api.policy.validate_squid_config", return_value=(True, ""))
    def test_bulk_delete_publishes_once(self, validate) -> None:
        """The admin delete-selected action publishes one complete revision."""
        response = self.client.post(
            "/admin/api/sourceacl/",
            {"action": "delete_selected", "_selected_action": [str(self.source.pk)], "post": "yes"},
        )
        assert response.status_code == 302
        assert not SourceACL.objects.exists()
        assert ConfigVersion.get().version == 1
        assert RenderedConfigHistory.objects.count() == 1
        validate.assert_called_once()

    @patch("terrasquid.api.policy.validate_squid_config", return_value=(False, "syntax error"))
    def test_invalid_delete_rolls_back(self, validate) -> None:
        """A failed admin deletion must retain the resource and report the error."""
        response = self.client.post(f"/admin/api/sourceacl/{self.source.pk}/delete/", {"post": "yes"}, follow=True)
        self.assertContains(response, "Squid configuration validation failed: syntax error")
        assert SourceACL.objects.filter(pk=self.source.pk).exists()
        assert ConfigVersion.get().version == 0
        assert not RenderedConfigHistory.objects.exists()
        validate.assert_called_once()

    @patch("terrasquid.api.policy.validate_squid_config", return_value=(True, ""))
    def test_relationship_changes_are_published_after_save(self, validate) -> None:
        """The published admin revision must include newly saved many-to-many fields."""
        destination = DestinationConfig.objects.create(
            service="admin-test", name="destination", key_prefix="test1234", dst="example.com", type="ALLOW"
        )
        response = self.client.post(
            "/admin/api/aclrule/add/",
            {
                "service": "admin-test",
                "name": "admin-rule",
                "key_prefix": "test1234",
                "priority": 100,
                "sources": [str(self.source.pk)],
                "destinations": [str(destination.pk)],
                "_save": "Save",
            },
        )
        assert response.status_code == 302
        rule = ACLRule.objects.get(name="admin-rule")
        assert list(rule.sources.all()) == [self.source]
        assert list(rule.destinations.all()) == [destination]
        config = ConfigVersion.get()
        assert "http_access allow src__test1234__source dst__test1234__destination" in config.rendered_config
        assert RenderedConfigHistory.objects.get(version=1).rendered_config == config.rendered_config
        validate.assert_called_once()
