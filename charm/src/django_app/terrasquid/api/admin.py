from django.contrib import admin

from .models import ACLRule, ConfigVersion, DestinationConfig, RenderedConfigHistory, SourceACL


@admin.register(RenderedConfigHistory)
class RenderedConfigHistoryAdmin(admin.ModelAdmin):
    """Expose retained Squid configurations as immutable history."""

    list_display = ("version", "created_at")
    ordering = ("-version",)
    readonly_fields = ("version", "rendered_config", "created_at")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


admin.site.register(SourceACL)
admin.site.register(DestinationConfig)
admin.site.register(ACLRule)
admin.site.register(ConfigVersion)
