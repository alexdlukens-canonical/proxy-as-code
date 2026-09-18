"""Unit tests for the Terrasquid Django admin."""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import path
from terrasquid.api.admin import RenderedConfigHistoryAdmin
from terrasquid.api.models import RenderedConfigHistory

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
