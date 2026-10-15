from django.contrib import admin, messages
from django.http import HttpResponseRedirect

from .models import ACLRule, ConfigVersion, DestinationConfig, RenderedConfigHistory, SourceACL
from .policy import SquidConfigError, publish_policy


class ReadOnlyConfigAdmin(admin.ModelAdmin):
    """Prevent manual changes to published configuration records."""

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(RenderedConfigHistory)
class RenderedConfigHistoryAdmin(ReadOnlyConfigAdmin):
    """Expose retained Squid configurations as immutable history."""

    list_display = ("version", "created_at")
    ordering = ("-version",)
    readonly_fields = ("version", "rendered_config", "created_at")


@admin.register(ConfigVersion)
class ConfigVersionAdmin(ReadOnlyConfigAdmin):
    """Expose the current version without allowing publication to be bypassed."""

    list_display = ("version", "updated_at")
    readonly_fields = ("version", "rendered_config", "updated_at")


class PolicyResourceAdmin(admin.ModelAdmin):
    """Serialize admin mutations and publish only validated policy."""

    def _policy_view(self, request, view, *args, **kwargs):
        if request.method != "POST":
            return view(request, *args, **kwargs)
        try:
            with ConfigVersion.locked():
                return view(request, *args, **kwargs)
        except SquidConfigError as error:
            self.message_user(request, str(error.detail), level=messages.ERROR)
            return HttpResponseRedirect(request.path)

    def changeform_view(self, request, *args, **kwargs):
        return self._policy_view(request, super().changeform_view, *args, **kwargs)

    def delete_view(self, request, *args, **kwargs):
        return self._policy_view(request, super().delete_view, *args, **kwargs)

    def changelist_view(self, request, *args, **kwargs):
        return self._policy_view(request, super().changelist_view, *args, **kwargs)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        publish_policy()

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        publish_policy()

    def delete_queryset(self, request, queryset):
        super().delete_queryset(request, queryset)
        publish_policy()


admin.site.register(SourceACL, PolicyResourceAdmin)
admin.site.register(DestinationConfig, PolicyResourceAdmin)
admin.site.register(ACLRule, PolicyResourceAdmin)
