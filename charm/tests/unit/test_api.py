"""Unit tests for the Terrasquid Django REST API."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_api_key.models import APIKey


def _make_client() -> tuple[APIClient, APIKey, str]:
    """Create an API key and return (client, api_key_instance, raw_key)."""
    api_key, key = APIKey.objects.create_key(name="test-service")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Api-Key {key}")
    return client, api_key, key


class TestStatusEndpoint(TestCase):
    """Tests for GET /api/v1/status/."""

    def test_status_unauthenticated(self) -> None:
        """Status endpoint must be accessible without authentication."""
        client = APIClient()
        response = client.get("/api/v1/status/")
        assert response.status_code == 200
        data = response.json()
        assert "db_config_version" in data
        assert "applied_config_version" in data
        assert "last_reload_ok" in data
        assert "unit" in data

    def test_status_returns_correct_shape(self) -> None:
        """Status response must include all fields from the OpenAPI contract."""
        client = APIClient()
        response = client.get("/api/v1/status/")
        data = response.json()
        required_fields = {"db_config_version", "applied_config_version", "last_reload_ok", "unit"}
        assert required_fields.issubset(set(data.keys()))


class TestSourceACLEndpoints(TestCase):
    """Tests for /api/v1/sources/ CRUD."""

    def setUp(self) -> None:
        self.client, self.api_key, self.raw_key = _make_client()

    def test_list_empty(self) -> None:
        """GET /sources/ returns an empty list when no resources exist."""
        response = self.client.get("/api/v1/sources/")
        assert response.status_code == 200
        assert response.json() == []

    def test_create_source_acl(self) -> None:
        """POST /sources/ creates a new SourceACL and returns 201."""
        with (
            patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, "")),
            patch("terrasquid.api.squid_render.render_squid_config", return_value="# config"),
        ):
            response = self.client.post(
                "/api/v1/sources/",
                {"name": "corp-vpn", "cidr": ["10.0.0.0/8", "192.168.0.0/16"]},
                format="json",
            )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "corp-vpn"
        assert data["service"] == "test-service"
        assert "10.0.0.0/8" in data["cidr"]
        assert data["comment"] == ""

    def test_create_source_acl_with_comment(self) -> None:
        """POST /sources/ accepts and returns a single-line comment."""
        with (
            patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, "")),
            patch("terrasquid.api.squid_render.render_squid_config", return_value="# config"),
        ):
            response = self.client.post(
                "/api/v1/sources/",
                {"name": "documented-src", "cidr": ["10.0.0.0/8"], "comment": "Corporate VPN"},
                format="json",
            )
        assert response.status_code == 201
        assert response.json()["comment"] == "Corporate VPN"

    def test_create_source_acl_rejects_multiline_comment(self) -> None:
        """POST /sources/ rejects comments that would add Squid config lines."""
        response = self.client.post(
            "/api/v1/sources/",
            {"name": "multiline-src", "cidr": ["10.0.0.0/8"], "comment": "first\nsecond"},
            format="json",
        )
        assert response.status_code == 400
        assert "comment" in response.json()

    def test_create_idempotent_returns_200(self) -> None:
        """Duplicate POST with same (service, name) returns 200 with existing resource."""
        with (
            patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, "")),
            patch("terrasquid.api.squid_render.render_squid_config", return_value="# config"),
        ):
            self.client.post(
                "/api/v1/sources/",
                {"name": "corp-vpn", "cidr": ["10.0.0.0/8"]},
                format="json",
            )
            response = self.client.post(
                "/api/v1/sources/",
                {"name": "corp-vpn", "cidr": ["10.0.0.0/8"]},
                format="json",
            )
        assert response.status_code == 200

    def test_unauthenticated_post_returns_403(self) -> None:
        """Unauthenticated POST must be rejected with 403."""
        anon = APIClient()
        response = anon.post(
            "/api/v1/sources/",
            {"name": "corp-vpn", "cidr": ["10.0.0.0/8"]},
            format="json",
        )
        assert response.status_code == 403

    def test_invalid_cidr_returns_400(self) -> None:
        """POST with invalid CIDR returns 400 with field errors."""
        response = self.client.post(
            "/api/v1/sources/",
            {"name": "bad-src", "cidr": ["not-a-cidr"]},
            format="json",
        )
        assert response.status_code == 400

    def test_get_source_acl(self) -> None:
        """GET /sources/{id}/ returns the resource for the authenticated service."""
        with (
            patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, "")),
            patch("terrasquid.api.squid_render.render_squid_config", return_value="# config"),
        ):
            create_resp = self.client.post(
                "/api/v1/sources/",
                {"name": "my-src", "cidr": ["172.16.0.0/12"]},
                format="json",
            )
        src_id = create_resp.json()["id"]
        response = self.client.get(f"/api/v1/sources/{src_id}/")
        assert response.status_code == 200
        assert response.json()["id"] == src_id

    def test_get_nonexistent_returns_404(self) -> None:
        """GET /sources/{unknown_id}/ returns 404."""
        response = self.client.get(f"/api/v1/sources/{uuid.uuid4()}/")
        assert response.status_code == 404

    def test_delete_source_acl(self) -> None:
        """DELETE /sources/{id}/ removes the resource and returns 204."""
        with (
            patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, "")),
            patch("terrasquid.api.squid_render.render_squid_config", return_value="# config"),
        ):
            create_resp = self.client.post(
                "/api/v1/sources/",
                {"name": "del-src", "cidr": ["1.2.3.4/32"]},
                format="json",
            )
            src_id = create_resp.json()["id"]
            response = self.client.delete(f"/api/v1/sources/{src_id}/")
        assert response.status_code == 204

    def test_service_isolation(self) -> None:
        """Resources created by one service are not visible to another."""
        with (
            patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, "")),
            patch("terrasquid.api.squid_render.render_squid_config", return_value="# config"),
        ):
            self.client.post(
                "/api/v1/sources/",
                {"name": "private-src", "cidr": ["10.0.0.0/8"]},
                format="json",
            )
        other_key, other_raw = APIKey.objects.create_key(name="other-service")
        other_client = APIClient()
        other_client.credentials(HTTP_AUTHORIZATION=f"Api-Key {other_raw}")
        response = other_client.get("/api/v1/sources/")
        assert response.status_code == 200
        assert response.json() == []


class TestDestinationConfigEndpoints(TestCase):
    """Tests for /api/v1/destinations/ CRUD."""

    def setUp(self) -> None:
        self.client, self.api_key, self.raw_key = _make_client()

    def test_create_destination_config(self) -> None:
        """POST /destinations/ creates an ALLOW DestinationConfig."""
        with (
            patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, "")),
            patch("terrasquid.api.squid_render.render_squid_config", return_value="# config"),
        ):
            response = self.client.post(
                "/api/v1/destinations/",
                {
                    "name": "ubuntu-archive",
                    "dst": "archive.ubuntu.com",
                    "type": "ALLOW",
                    "comment": "Ubuntu package mirror",
                },
                format="json",
            )
        assert response.status_code == 201
        data = response.json()
        assert data["dst"] == "archive.ubuntu.com"
        assert data["type"] == "ALLOW"
        assert data["comment"] == "Ubuntu package mirror"

    def test_delete_referenced_destination_returns_409(self) -> None:
        """DELETE on a destination referenced by an ACL rule should return 409."""
        from terrasquid.api.models import ACLRule, DestinationConfig, SourceACL

        src = SourceACL.objects.create(
            service="test-service",
            name="test-src",
            key_prefix="ab12cd34",
            cidr=["10.0.0.0/8"],
        )
        dst = DestinationConfig.objects.create(
            service="test-service",
            name="locked-dst",
            key_prefix="ab12cd34",
            dst="example.com",
            type="ALLOW",
        )
        rule = ACLRule.objects.create(
            service="test-service",
            name="test-rule",
            key_prefix="ab12cd34",
        )
        rule.sources.add(src)
        rule.destinations.add(dst)
        with patch("terrasquid.api.squid_render.render_squid_config", return_value="# config"):
            response = self.client.delete(f"/api/v1/destinations/{dst.id}/")
        assert response.status_code == 409


class TestDestinationGroupEndpoints(TestCase):
    """Tests for globally readable, owner-managed destination groups."""

    def setUp(self) -> None:
        self.client, self.api_key, self.raw_key = _make_client()
        from terrasquid.api.models import DestinationConfig

        self.destination = DestinationConfig.objects.create(
            service="test-service",
            name="github",
            key_prefix="ab12cd34",
            dst="github.com",
            type="CONNECT",
            ports=[22, 443],
        )
        other_key, other_raw = APIKey.objects.create_key(name="other-service")
        self.other_client = APIClient()
        self.other_client.credentials(HTTP_AUTHORIZATION=f"Api-Key {other_raw}")

    def test_group_is_readable_and_referenceable_by_another_service(self) -> None:
        """Any authenticated service can resolve a group and attach it to a local rule."""
        from terrasquid.api.models import SourceACL

        source = SourceACL.objects.create(
            service="other-service",
            name="other-source",
            key_prefix="dc34ab12",
            cidr=["10.0.0.0/8"],
        )
        with (
            patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, "")),
            patch("terrasquid.api.squid_render.render_squid_config", return_value="# config"),
        ):
            create_response = self.client.post(
                "/api/v1/destination-groups/",
                {"name": "common-sites-cloud-access", "destinations": [str(self.destination.id)]},
                format="json",
            )
            group_id = create_response.json()["id"]
            lookup_response = self.other_client.get("/api/v1/destination-groups/?name=common-sites-cloud-access")
            rule_response = self.other_client.post(
                "/api/v1/acl-rules/",
                {
                    "name": "allow-common-sites",
                    "sources": [str(source.id)],
                    "destination_groups": [group_id],
                },
                format="json",
            )
        assert create_response.status_code == 201
        assert lookup_response.status_code == 200
        assert lookup_response.json()[0]["id"] == group_id
        assert rule_response.status_code == 201
        assert rule_response.json()["destination_groups"] == [group_id]

    def test_group_listing_does_not_enumerate_other_services(self) -> None:
        """A consumer must provide a group name to resolve another service's group."""
        with (
            patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, "")),
            patch("terrasquid.api.squid_render.render_squid_config", return_value="# config"),
        ):
            self.client.post(
                "/api/v1/destination-groups/",
                {"name": "common-sites-cloud-access", "destinations": [str(self.destination.id)]},
                format="json",
            )

        response = self.other_client.get("/api/v1/destination-groups/")

        assert response.status_code == 200
        assert response.json() == []

    def test_non_owner_cannot_modify_or_delete_group(self) -> None:
        """Only the service that created a group may alter its lifecycle."""
        with (
            patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, "")),
            patch("terrasquid.api.squid_render.render_squid_config", return_value="# config"),
        ):
            response = self.client.post(
                "/api/v1/destination-groups/",
                {"name": "common-sites-cloud-access", "destinations": [str(self.destination.id)]},
                format="json",
            )
        group_id = response.json()["id"]
        update_response = self.other_client.put(
            f"/api/v1/destination-groups/{group_id}/",
            {"name": "common-sites-cloud-access", "destinations": [str(self.destination.id)]},
            format="json",
        )
        delete_response = self.other_client.delete(f"/api/v1/destination-groups/{group_id}/")
        assert update_response.status_code == 403
        assert delete_response.status_code == 403

    def test_global_group_name_conflict_returns_409(self) -> None:
        """A globally unique group name cannot be claimed by another service."""
        with (
            patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, "")),
            patch("terrasquid.api.squid_render.render_squid_config", return_value="# config"),
        ):
            self.client.post(
                "/api/v1/destination-groups/",
                {"name": "common-sites-cloud-access", "destinations": [str(self.destination.id)]},
                format="json",
            )
            response = self.other_client.post(
                "/api/v1/destination-groups/",
                {"name": "common-sites-cloud-access", "destinations": [str(self.destination.id)]},
                format="json",
            )
        assert response.status_code == 409


class TestACLRuleEndpoints(TestCase):
    """Tests for /api/v1/acl-rules/ CRUD."""

    def setUp(self) -> None:
        self.client, self.api_key, self.raw_key = _make_client()
        from terrasquid.api.models import DestinationConfig, SourceACL

        with (
            patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, "")),
            patch("terrasquid.api.squid_render.render_squid_config", return_value="# config"),
        ):
            self.src = SourceACL.objects.create(
                service="test-service",
                name="my-src",
                key_prefix="ab12cd34",
                cidr=["10.0.0.0/8"],
            )
            self.dst = DestinationConfig.objects.create(
                service="test-service",
                name="my-dst",
                key_prefix="ab12cd34",
                dst="example.com",
                type="ALLOW",
            )

    def test_create_acl_rule(self) -> None:
        """POST /acl-rules/ creates a rule linking sources and destinations."""
        with (
            patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, "")),
            patch("terrasquid.api.squid_render.render_squid_config", return_value="# config"),
        ):
            response = self.client.post(
                "/api/v1/acl-rules/",
                {
                    "name": "allow-corp-to-ubuntu",
                    "sources": [str(self.src.id)],
                    "destinations": [str(self.dst.id)],
                    "comment": "Permit corporate users",
                },
                format="json",
            )
        assert response.status_code == 201
        data = response.json()
        assert data["sources"] == [str(self.src.id)]
        assert data["destinations"] == [str(self.dst.id)]
        assert data["comment"] == "Permit corporate users"

    def test_acl_rule_requires_at_least_one_source(self) -> None:
        """ACL rule with an empty sources list returns 400."""
        response = self.client.post(
            "/api/v1/acl-rules/",
            {"name": "bad-rule", "sources": [], "destinations": [str(self.dst.id)]},
            format="json",
        )
        assert response.status_code == 400

    def test_acl_rule_requires_at_least_one_destination(self) -> None:
        """ACL rule with no destinations returns 400."""
        response = self.client.post(
            "/api/v1/acl-rules/",
            {"name": "bad-rule2", "sources": [str(self.src.id)]},
            format="json",
        )
        assert response.status_code == 400

    def test_acl_rule_patch_cannot_remove_its_last_destination_reference(self) -> None:
        """A partial update cannot leave a rule without a destination or group."""
        with (
            patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, "")),
            patch("terrasquid.api.squid_render.render_squid_config", return_value="# config"),
        ):
            create_response = self.client.post(
                "/api/v1/acl-rules/",
                {
                    "name": "allow-corp-to-ubuntu",
                    "sources": [str(self.src.id)],
                    "destinations": [str(self.dst.id)],
                },
                format="json",
            )
            response = self.client.patch(
                f"/api/v1/acl-rules/{create_response.json()['id']}/",
                {"destinations": []},
                format="json",
            )

        assert create_response.status_code == 201
        assert response.status_code == 400

    def test_acl_rule_rejects_foreign_source(self) -> None:
        """ACL rule referencing a source from another service returns 400."""
        from terrasquid.api.models import SourceACL

        foreign = SourceACL.objects.create(
            service="other-service",
            name="foreign-src",
            key_prefix="ffffffff",
            cidr=["10.0.0.0/8"],
        )
        response = self.client.post(
            "/api/v1/acl-rules/",
            {
                "name": "cross-service",
                "sources": [str(foreign.id)],
                "destinations": [str(self.dst.id)],
            },
            format="json",
        )
        assert response.status_code == 400

    def test_rendered_config_includes_acl_rule(self) -> None:
        """When an ACL rule is created, the rendered config must include its http_access statement."""
        from terrasquid.api.models import ConfigVersion

        with (
            patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, "")),
            patch("terrasquid.api.squid_render.render_squid_config") as mock_render,
        ):
            mock_render.return_value = "# config\nhttp_access allow src__test-service__my-src dst__test-service__my-dst"
            response = self.client.post(
                "/api/v1/acl-rules/",
                {
                    "name": "allow-corp-to-ubuntu",
                    "sources": [str(self.src.id)],
                    "destinations": [str(self.dst.id)],
                },
                format="json",
            )
        assert response.status_code == 201
        config = ConfigVersion.get().rendered_config
        assert "http_access" in config
        assert "src__ab12cd34__my-src" in config
        assert "dst__ab12cd34__my-dst" in config


class TestSquidConfigValidation(TestCase):
    """Tests for the Squid config dry-run validation behaviour."""

    def setUp(self) -> None:
        self.client, self.api_key, self.raw_key = _make_client()

    @override_settings(SQUID_DEFAULT_DENY=True)
    def test_render_includes_default_deny_when_enabled(self) -> None:
        """The final deny-all rule is rendered when the option is enabled."""
        from terrasquid.api.squid_render import render_squid_config

        assert render_squid_config().rstrip().endswith("http_access deny all")

    @override_settings(SQUID_DEFAULT_DENY=False)
    def test_render_omits_default_deny_when_disabled(self) -> None:
        """The final deny-all rule is omitted when the option is disabled."""
        from terrasquid.api.squid_render import render_squid_config

        assert "http_access deny all" not in render_squid_config()

    def test_render_uses_api_key_prefix_for_acl_names(self) -> None:
        """Render compact ACL identifiers independently of the API key's friendly name."""
        from terrasquid.api.models import ACLRule, DestinationConfig, SourceACL
        from terrasquid.api.squid_render import render_squid_config

        source = SourceACL.objects.create(
            service="terrasquid-admin-key",
            name="stg-terrasquid-ps7-client",
            key_prefix="ab12cd34",
            cidr=["10.0.0.0/8"],
        )
        destination = DestinationConfig.objects.create(
            service="terrasquid-admin-key",
            name="stg-terrasquid-ps7-client-terraform",
            key_prefix="ab12cd34",
            dst="registry.terraform.io",
            type="CONNECT",
            ports=[443],
        )
        rule = ACLRule.objects.create(
            service="terrasquid-admin-key",
            name="stg-terrasquid-ps7-client-terraform",
            key_prefix="ab12cd34",
        )
        rule.sources.add(source)
        rule.destinations.add(destination)

        config = render_squid_config()

        assert "acl port__443 port 443" in config
        assert config.count("acl CONNECT method CONNECT") == 1
        assert (
            "http_access allow src__ab12cd34__stg-terrasquid-ps7-client "
            "CONNECT dst__ab12cd34__stg-terrasquid-ps7-client-terraform port__443"
        ) in config
        assert "dstport__" not in config

    def test_render_expands_destination_groups_without_duplicates(self) -> None:
        """Direct and grouped references to one destination render one access rule."""
        from terrasquid.api.models import ACLRule, DestinationConfig, DestinationGroup, SourceACL
        from terrasquid.api.squid_render import render_squid_config

        source = SourceACL.objects.create(
            service="consumer-service",
            name="local-source",
            key_prefix="ab12cd34",
            cidr=["10.0.0.0/8"],
        )
        destination = DestinationConfig.objects.create(
            service="owner-service",
            name="github",
            key_prefix="dc34ab12",
            dst="github.com",
            type="CONNECT",
            ports=[443],
        )
        group = DestinationGroup.objects.create(
            service="owner-service",
            name="common-sites-cloud-access",
            key_prefix="dc34ab12",
        )
        group.destinations.add(destination)
        rule = ACLRule.objects.create(
            service="consumer-service",
            name="allow-common-sites",
            key_prefix="ab12cd34",
        )
        rule.sources.add(source)
        rule.destinations.add(destination)
        rule.destination_groups.add(group)
        second_rule = ACLRule.objects.create(
            service="consumer-service",
            name="allow-common-sites-again",
            key_prefix="ab12cd34",
        )
        second_rule.sources.add(source)
        second_rule.destination_groups.add(group)

        config = render_squid_config()

        assert (
            config.splitlines().count(
                "http_access allow src__ab12cd34__local-source CONNECT "
                "dst_group__dc34ab12__common-sites-cloud-access__1 port__443"
            )
            == 2
        )
        assert config.count("acl dst_group__dc34ab12__common-sites-cloud-access__1 any-of") == 1
        assert "acl rule_dst__ab12cd34__allow-common-sites__1" not in config
        assert "acl rule_dst__ab12cd34__allow-common-sites-again__1" not in config

    def test_render_partitions_destination_groups_by_access_policy(self) -> None:
        """Only compatible destinations share a Squid ACL and access rule."""
        from terrasquid.api.models import ACLRule, DestinationConfig, DestinationGroup, SourceACL
        from terrasquid.api.squid_render import render_squid_config

        source = SourceACL.objects.create(
            service="consumer-service",
            name="local-source",
            key_prefix="ab12cd34",
            cidr=["10.0.0.0/8"],
        )
        destinations = [
            DestinationConfig.objects.create(
                service="owner-service",
                name="github",
                key_prefix="dc34ab12",
                dst="github.com",
                type="CONNECT",
                ports=[443],
            ),
            DestinationConfig.objects.create(
                service="owner-service",
                name="google",
                key_prefix="dc34ab12",
                dst="google.com",
                type="CONNECT",
                ports=[443],
            ),
            DestinationConfig.objects.create(
                service="owner-service",
                name="ssh",
                key_prefix="dc34ab12",
                dst="ssh.example.com",
                type="CONNECT",
                ports=[22, 443],
            ),
            DestinationConfig.objects.create(
                service="owner-service",
                name="blocked",
                key_prefix="dc34ab12",
                dst="blocked.example.com",
                type="DENY",
                ports=[443],
            ),
            DestinationConfig.objects.create(
                service="owner-service",
                name="network",
                key_prefix="dc34ab12",
                dst="192.0.2.0/24",
                type="CONNECT",
                ports=[443],
            ),
        ]
        group = DestinationGroup.objects.create(
            service="owner-service", name="common-sites-cloud-access", key_prefix="dc34ab12"
        )
        group.destinations.add(*destinations)
        rule = ACLRule.objects.create(service="consumer-service", name="allow-common-sites", key_prefix="ab12cd34")
        rule.sources.add(source)
        rule.destination_groups.add(group)

        config = render_squid_config()

        assert ("any-of dst__dc34ab12__github dst__dc34ab12__google dst__dc34ab12__network") in config
        assert "any-of dst__dc34ab12__ssh" in config
        assert "any-of dst__dc34ab12__blocked" in config
        assert "acl rule_dst__ab12cd34__allow-common-sites__" not in config
        assert (
            config.count(
                "http_access allow src__ab12cd34__local-source CONNECT dst_group__dc34ab12__common-sites-cloud-access__"
            )
            == 2
        )
        assert (
            config.count(
                "http_access deny src__ab12cd34__local-source dst_group__dc34ab12__common-sites-cloud-access__"
            )
            == 1
        )

    def test_render_declares_all_rule_acls_before_access_rules(self) -> None:
        """Declare every generated rule ACL before source access statements can use it."""
        from terrasquid.api.models import ACLRule, DestinationConfig, SourceACL
        from terrasquid.api.squid_render import render_squid_config

        for index in range(2):
            source = SourceACL.objects.create(
                service="test-service",
                name=f"source-{index}",
                key_prefix="ab12cd34",
                cidr=[f"10.0.{index}.0/24"],
            )
            destination = DestinationConfig.objects.create(
                service="test-service",
                name=f"destination-{index}",
                key_prefix="ab12cd34",
                dst=f"destination-{index}.example.com",
                type="ALLOW",
            )
            rule = ACLRule.objects.create(
                service="test-service",
                name=f"rule-{index}",
                key_prefix="ab12cd34",
                priority=index,
            )
            rule.sources.add(source)
            rule.destinations.add(destination)
            rule.destinations.add(
                DestinationConfig.objects.create(
                    service="test-service",
                    name=f"second-destination-{index}",
                    key_prefix="ab12cd34",
                    dst=f"second-destination-{index}.example.com",
                    type="ALLOW",
                )
            )

        config = render_squid_config()
        rule_acl_positions = [config.index(f"acl rule_dst__ab12cd34__rule-{index}__1") for index in range(2)]

        assert max(rule_acl_positions) < config.index("http_access allow src__ab12cd34__source-")

    def test_render_orders_sources_stably_within_rule(self) -> None:
        """Render a rule's source access statements in stable name order."""
        from terrasquid.api.models import ACLRule, DestinationConfig, SourceACL
        from terrasquid.api.squid_render import render_squid_config

        sources = [
            SourceACL.objects.create(
                service="test-service",
                name=name,
                key_prefix="ab12cd34",
                cidr=[cidr],
            )
            for name, cidr in (("z-source", "10.0.0.0/24"), ("a-source", "10.0.1.0/24"))
        ]
        destination = DestinationConfig.objects.create(
            service="test-service",
            name="destination",
            key_prefix="ab12cd34",
            dst="destination.example.com",
            type="ALLOW",
        )
        rule = ACLRule.objects.create(service="test-service", name="rule", key_prefix="ab12cd34")
        rule.sources.add(*sources)
        rule.destinations.add(destination)

        config = render_squid_config()

        assert ("acl rule_src__ab12cd34__rule any-of src__ab12cd34__a-source src__ab12cd34__z-source") in config
        assert config.count("http_access allow rule_src__ab12cd34__rule") == 1

    def test_render_orders_rules_by_priority_type_then_creation_time(self) -> None:
        """Order access rules by ascending priority, action type, then creation time."""
        from terrasquid.api.models import ACLRule, DestinationConfig, SourceACL
        from terrasquid.api.squid_render import render_squid_config

        source = SourceACL.objects.create(
            service="test-service",
            name="source",
            key_prefix="ab12cd34",
            cidr=["10.0.0.0/24"],
        )
        cases = (
            ("created-first-allow", 100, "ALLOW"),
            ("priority-99-allow", 99, "ALLOW"),
            ("created-second-allow", 100, "ALLOW"),
            ("priority-100-connect", 100, "CONNECT"),
            ("priority-100-deny", 100, "DENY"),
        )
        for name, priority, action_type in cases:
            destination = DestinationConfig.objects.create(
                service="test-service",
                name=name,
                key_prefix="ab12cd34",
                dst=f"{name}.example.com",
                type=action_type,
            )
            rule = ACLRule.objects.create(
                service="test-service",
                name=name,
                key_prefix="ab12cd34",
                priority=priority,
            )
            rule.sources.add(source)
            rule.destinations.add(destination)

        config = render_squid_config()
        access_lines = [line for line in config.splitlines() if line.startswith("http_access ") and "src__" in line]

        assert [line.split(" dst__ab12cd34__", 1)[1].split(" port__", 1)[0] for line in access_lines] == [
            "priority-99-allow",
            "priority-100-deny",
            "priority-100-connect",
            "created-first-allow",
            "created-second-allow",
        ]
        connect_lines = [line for line in access_lines if " CONNECT " in line]
        assert len(connect_lines) == 1
        assert "dst__ab12cd34__priority-100-connect" in connect_lines[0]

    def test_render_places_comments_before_their_acl_blocks(self) -> None:
        """Configured comments label their source, destination, and logical rule exactly once."""
        from terrasquid.api.models import ACLRule, DestinationConfig, SourceACL
        from terrasquid.api.squid_render import render_squid_config

        source = SourceACL.objects.create(
            service="test-service",
            name="commented-source",
            key_prefix="ab12cd34",
            cidr=["10.0.0.0/8"],
            comment="Source networks",
        )
        destination = DestinationConfig.objects.create(
            service="test-service",
            name="commented-destination",
            key_prefix="ab12cd34",
            dst="example.com",
            type="ALLOW",
            comment="Allowed website",
        )
        rule = ACLRule.objects.create(
            service="test-service",
            name="commented-rule",
            key_prefix="ab12cd34",
            comment="Permit source to website",
        )
        rule.sources.add(source)
        rule.destinations.add(destination)

        config = render_squid_config()

        assert config.index("# Source networks") < config.index("acl src__ab12cd34__commented-source")
        assert config.index("# Allowed website") < config.index("acl dst__ab12cd34__commented-destination")
        assert config.index("# Permit source to website") < config.index(
            "http_access allow src__ab12cd34__commented-source"
        )
        assert config.count("# Permit source to website") == 1

    def test_squid_validation_failure_returns_422(self) -> None:
        """When Squid config validation fails, the API returns 422 and rolls back."""
        with (
            patch("terrasquid.api.policy.render_squid_config", return_value="bad config"),
            patch(
                "terrasquid.api.policy.validate_squid_config",
                return_value=(False, "syntax error"),
            ),
        ):
            response = self.client.post(
                "/api/v1/sources/",
                {"name": "will-fail", "cidr": ["10.0.0.0/8"]},
                format="json",
            )
        assert response.status_code == 422

    def test_resource_not_persisted_on_squid_failure(self) -> None:
        """On Squid validation failure, the resource must not remain in the database."""
        from terrasquid.api.models import SourceACL

        with (
            patch("terrasquid.api.policy.render_squid_config", return_value="bad"),
            patch(
                "terrasquid.api.policy.validate_squid_config",
                return_value=(False, "error"),
            ),
        ):
            self.client.post(
                "/api/v1/sources/",
                {"name": "rolled-back", "cidr": ["10.0.0.0/8"]},
                format="json",
            )
        assert not SourceACL.objects.filter(name="rolled-back").exists()


class TestConfigVersionHistory(TestCase):
    """Tests for RenderedConfigHistory and config version pinning."""

    def test_increment_saves_to_history(self) -> None:
        """ConfigVersion.increment() must also create a RenderedConfigHistory entry."""
        from terrasquid.api.models import ConfigVersion, RenderedConfigHistory

        ConfigVersion.increment("# config v1")
        assert RenderedConfigHistory.objects.filter(version=1, rendered_config="# config v1").exists()

    def test_increment_creates_distinct_history_versions(self) -> None:
        """Calling increment twice preserves separate immutable history entries."""
        from terrasquid.api.models import ConfigVersion, RenderedConfigHistory

        ConfigVersion.increment("# config v1")
        ConfigVersion.increment("# config v2")
        assert RenderedConfigHistory.objects.count() == 2
        assert RenderedConfigHistory.objects.get(version=2).rendered_config == "# config v2"

    def test_duplicate_history_version_rolls_back_increment(self) -> None:
        """A conflicting history entry must not be overwritten or advance the version."""
        from django.db import IntegrityError
        from terrasquid.api.models import ConfigVersion, RenderedConfigHistory

        RenderedConfigHistory.objects.create(version=1, rendered_config="# original")
        with self.assertRaises(IntegrityError):
            ConfigVersion.increment("# replacement")

        assert ConfigVersion.get().version == 0
        assert RenderedConfigHistory.objects.get(version=1).rendered_config == "# original"

    @override_settings(SQUID_PINNED_CONFIG_VERSION=7)
    def test_increment_prunes_history_without_removing_active_pin(self) -> None:
        """Each new version must enforce retention while protecting the active pin."""
        from terrasquid.api.models import ConfigVersion, RenderedConfigHistory

        with patch.object(RenderedConfigHistory, "prune") as prune:
            ConfigVersion.increment("# config v1")

        prune.assert_called_once_with(pinned_version=7)

    def test_prune_keeps_recent_versions_and_latest_per_older_utc_day(self) -> None:
        """Retention keeps all trailing-day entries and one latest entry per older day."""
        from terrasquid.api.models import RenderedConfigHistory

        now = datetime(2026, 9, 18, 12, tzinfo=UTC)
        entries = [
            RenderedConfigHistory.objects.create(version=version, rendered_config=f"# config v{version}")
            for version in range(1, 7)
        ]
        timestamps = [
            now - timedelta(hours=2),
            now - timedelta(hours=23),
            datetime(2026, 9, 16, 20, tzinfo=UTC),
            datetime(2026, 9, 16, 8, tzinfo=UTC),
            datetime(2026, 9, 15, 20, tzinfo=UTC),
            datetime(2026, 9, 15, 8, tzinfo=UTC),
        ]
        for entry, created_at in zip(entries, timestamps, strict=True):
            RenderedConfigHistory.objects.filter(pk=entry.pk).update(created_at=created_at)

        deleted = RenderedConfigHistory.prune(now=now)

        assert deleted == 2
        assert set(RenderedConfigHistory.objects.values_list("version", flat=True)) == {1, 2, 3, 5}

    def test_prune_uses_pinned_version_as_older_day_representative(self) -> None:
        """Retention must not remove the currently pinned historical config."""
        from terrasquid.api.models import RenderedConfigHistory

        now = datetime(2026, 9, 18, 12, tzinfo=UTC)
        latest = RenderedConfigHistory.objects.create(version=1, rendered_config="# latest that day")
        pinned = RenderedConfigHistory.objects.create(version=2, rendered_config="# pinned")
        RenderedConfigHistory.objects.filter(pk=latest.pk).update(created_at=datetime(2026, 9, 16, 20, tzinfo=UTC))
        RenderedConfigHistory.objects.filter(pk=pinned.pk).update(created_at=datetime(2026, 9, 16, 8, tzinfo=UTC))

        RenderedConfigHistory.prune(now=now, pinned_version=pinned.version)

        assert set(RenderedConfigHistory.objects.values_list("version", flat=True)) == {2}


class TestRenderSquidConfigPinning(TestCase):
    """Tests for the render_squid_config management command with version pinning."""

    def _run_command(self, stdout=None, stderr=None):
        from io import StringIO

        from django.core.management import call_command

        out = stdout or StringIO()
        err = stderr or StringIO()
        call_command("render_squid_config", stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    @patch("terrasquid.api.squid_render.render_squid_config", return_value="# config v1")
    @patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, ""))
    @patch("django.conf.settings.SQUID_PINNED_CONFIG_VERSION", 0)
    def test_unpinned_applies_latest(self, mock_validate, mock_render) -> None:
        """When no version is pinned, the command applies the latest config."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as p

        from terrasquid.api.models import ConfigVersion

        ConfigVersion.increment("# config v1")
        with tempfile.TemporaryDirectory() as tmpdir:
            squid_conf = Path(tmpdir) / "squid.conf"
            squid_conf_new = Path(tmpdir) / "squid.conf.new"
            status_file = Path(tmpdir) / "status.json"
            with (
                p(
                    "terrasquid.api.management.commands.render_squid_config.SQUID_CONF",
                    squid_conf,
                ),
                p(
                    "terrasquid.api.management.commands.render_squid_config.SQUID_CONF_NEW",
                    squid_conf_new,
                ),
                p("django.conf.settings.TERRASQUID_STATUS_FILE", str(status_file)),
                p("subprocess.run", return_value=__import__("subprocess").CompletedProcess([], 0)),
            ):
                out, err = self._run_command()
        assert "unchanged" in out or "Applied" in out or "reloaded" in out.lower()
        assert not err

    @patch("django.conf.settings.SQUID_PINNED_CONFIG_VERSION", 2)
    def test_pinned_version_not_yet_rendered_skips(self) -> None:
        """When pinned to a version that has not been rendered yet, the command skips."""
        from terrasquid.api.models import ConfigVersion, RenderedConfigHistory

        ConfigVersion.increment("# config v1")
        assert not RenderedConfigHistory.objects.filter(version=2).exists()

        out, err = self._run_command()
        assert "skipping" in out.lower()
        assert not err

    @patch("terrasquid.api.squid_render.validate_squid_config", return_value=(True, ""))
    @patch("django.conf.settings.SQUID_PINNED_CONFIG_VERSION", 1)
    def test_pinned_version_applies_correct_config(self, mock_validate) -> None:
        """When pinned, the command applies the config stored for that exact version."""
        from pathlib import Path
        from unittest.mock import patch as p

        from terrasquid.api.models import ConfigVersion

        ConfigVersion.increment("# config v1 - pinned target")
        ConfigVersion.increment("# config v2 - should not be applied")

        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            squid_conf = Path(tmpdir) / "squid.conf"
            squid_conf_new = Path(tmpdir) / "squid.conf.new"
            status_file = Path(tmpdir) / "status.json"
            with (
                p(
                    "terrasquid.api.management.commands.render_squid_config.SQUID_CONF",
                    squid_conf,
                ),
                p(
                    "terrasquid.api.management.commands.render_squid_config.SQUID_CONF_NEW",
                    squid_conf_new,
                ),
                p("django.conf.settings.TERRASQUID_STATUS_FILE", str(status_file)),
                p("subprocess.run", return_value=__import__("subprocess").CompletedProcess([], 0)),
            ):
                out, err = self._run_command()

        assert not err
