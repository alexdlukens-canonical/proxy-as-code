| Index | IS140 |  |  |
| :---- | :---- | :---- | :---- |
| Title | Proxy as Code |  |  |
| [**Type**](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing) | **Author(s)** | [**Status**](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing) | **Created** |
| Implementation | [Alex Lukens](mailto:alex.lukens@canonical.com) | Braindump | 27 Apr 2026 |
|  | **Reviewer(s)** | **Status** | **Date** |
|  | [James Simpson](mailto:james.simpson@canonical.com) | Pending Review | Date |

# Abstract

Proxy as Code offers Squid access policy as a service, managed through service definitions in infrastructure-services. TerraSquid combines a Juju machine charm, a REST API, and a Terraform provider so proxy rules follow the lifecycle of the services that need them.

This document describes the implemented design as reviewed on 15 Oct 2026. Known gaps and future proposals are identified separately; they are not guarantees of the current implementation. Detailed API routes, schemas, and generated Squid syntax belong in the implementation's reference documentation rather than this spec.

# Rationale

The previous proxy configuration workflow requires users to identify source subnets, understand existing Squid ACLs, and submit changes to lp:canonical-is-internal-proxy-configs. That workflow requires detailed Squid knowledge and does not reconcile a service's desired policy when another change removes or modifies a rule.

TerraSquid lets users declare proxy access alongside their infrastructure. Terraform manages the associated policy resources and detects drift when plans are refreshed. This is particularly important for isolated environments where proxy access is required for external connectivity.

# Specification

The following sections describe the current architecture and its user-visible behavior.

## High level overview

The `terrasquid` charm runs Squid and a Django REST Framework application on Ubuntu 24.04 machines. Gunicorn serves the API, PostgreSQL stores policy and rendered configuration history, and a local systemd timer applies configuration changes to Squid.

The Terraform provider manages policy through the API. infrastructure-services renders provider resources from service definitions, including model-specific rules and references to reusable destination groups. Removing a service through its Terraform lifecycle removes the policy resources owned by that Terraform root module; shared groups remain independently managed.

The intended service is a forward proxy with access filtering, not a caching service. Explicitly disabling caching in the default generated configuration remains a gap.

## Workloads and charm integration

The charm manages the complete Squid configuration and the API, configuration watcher, and Squid exporter services. Configuration options cover proxy/API ports, API workers, the watcher interval, raw prepend/append configuration, default-deny behavior, configuration pinning, and the external hostname.

The charm integrates with:

- PostgreSQL through the `database` relation.
- A certificate provider through `certificates`.
- Optional API and proxy ingress through `django-ingress` and `squid-ingress`.
- Peer units through `squid-aaas-peers`.
- A COS agent through the provided `cos-agent` endpoint.

The API port defaults to 8080 and the proxy port to 3128. See [charm metadata](../charm/charmcraft.yaml) for the maintained configuration, action, and relation definitions.

## Authentication and ownership

API access uses named, revocable keys managed with `djangorestframework-api-key`. Resource requests carry the key in the `Authorization: Api-Key <key>` header. Plaintext keys are returned at creation and must be stored in a secrets manager, not in service definitions or source control.

The API key's **name** is the service ownership namespace. The API populates each resource's `service` field from that name; callers cannot choose a separate provider service label. Ordinary resource access is scoped to the namespace, and direct source/destination references must belong to it.

Multiple Terraform root modules can use one key, but they then share a namespace rather than gaining independent API ownership. Terraform state still determines which resources each root module manages.

The `create-key`, `list-keys`, `revoke-key`, and `rotate-key` charm actions manage keys. Rotation revokes the old key and creates a replacement with the same name, preserving access to existing resources. Revocation does not delete policy or immediately remove the associated proxy access. A differently named key is not a replacement for the old ownership namespace. Leader targeting is an operational convention, not a restriction enforced by these action handlers.

Destination groups provide an explicit sharing exception: their names are globally unique within a TerraSquid deployment. Authenticated clients can retrieve a group by its exact name or ID and reference it from their own ACL rules. Only the owning namespace can modify or delete the group.

## Policy model and Terraform

The implemented policy model consists of four resource types:

| Resource | Purpose |
| :---- | :---- |
| `terrasquid_source_acl` | One or more source CIDRs |
| `terrasquid_destination_config` | A destination, action type, and ports |
| `terrasquid_destination_group` | A reusable collection of destination configurations |
| `terrasquid_acl_rule` | Connections from source ACLs to direct destinations, destination groups, or both |

ACL rules have a name, source membership, and at least one direct destination or destination group. Sources, destinations, and group references are sets rather than exclusive singular alternatives. Source groups and port groups are not part of the current implementation.

Destination actions are `ALLOW`, `DENY`, and `CONNECT`. `CONNECT` permits TCP tunneling through the HTTP proxy, including HTTPS. Effective default ports are 443 for `CONNECT` and 80 for other actions. Rules are ordered by priority, with lower values first; at equal priority, deny rules precede CONNECT and allow rules. The generated configuration ends with a deny-all rule unless the operator disables it.

The provider exposes destination-group lookup and deployment status as data sources. Resource refresh removes missing remote objects from Terraform state so a later apply can recreate them. Status lookup reports the responding unit's state; it is not an access-policy simulation or a guarantee that all units have converged.

Referenced sources, destinations, and groups cannot be deleted until their policy references are removed. Same-name creation returns an existing resource in the owning namespace without comparing the new payload; it is not an update operation or semantic deduplication across services.

See the [provider reference](../terraform/docs/index.md) and resource references for [source ACLs](../terraform/docs/resources/source_acl.md), [destinations](../terraform/docs/resources/destination_config.md), [destination groups](../terraform/docs/resources/destination_group.md), and [ACL rules](../terraform/docs/resources/acl_rule.md). API consumers can use the deployment's generated OpenAPI and Swagger documentation. This spec does not prescribe an unverified registry namespace or release version.

## Configuration validation and application

API and Django admin policy changes are serialized deployment-wide through a PostgreSQL row lock on the configuration-version singleton. The lock is acquired before resource reads, reference checks, and validation, and held until commit. The application renders the proposed configuration and uses Squid's parser to validate it before atomically committing the policy, a unique increasing configuration version, and an immutable history entry. Failed parsing rolls back the write, version, and history. Deletes also pass through configuration validation. Concurrent edits to the same resource use last-writer-wins semantics in database serialization order; stale client intent is not rejected. Parser validation depends on Squid being installed; the current helper skips it when Squid is unavailable.

Charm reconciliation uses the same lock and validation path when rendering inputs change. Policy writes wait while another writer renders and validates configuration; ordinary status reads and watchers consuming committed configurations do not take the write lock. These guarantees require PostgreSQL and do not apply to SQLite development databases or direct database edits.

Each unit checks for configuration changes using a systemd timer, every 30 seconds by default. The watcher normally applies the stored rendered configuration from PostgreSQL. Configuration propagation is therefore polling-based and asynchronous, not driven by PostgreSQL LISTEN/NOTIFY.

Before replacing the local configuration, the watcher validates changed content. Unchanged files skip reload. Reload failure attempts to restore the previous configuration file. Application uses a reload-or-restart operation, so uninterrupted connections are not guaranteed.

The `reconfigure` action invokes the same application path on the selected unit. It honors configuration pinning and does not force a fresh render or a reload when the configuration file is unchanged.

Raw prepend/append configuration allows operator customization, but the charm does not implement the previously proposed synchronous hook-time parse-and-block gate. Generated ACL identifiers and local file-handling details are internal implementation choices, not public interfaces.

### Configuration history and pinning

PostgreSQL retains full rendered configurations: every revision from the trailing 24 hours and one revision per older UTC day. The active pinned revision is preserved.

The `squid-pinned-config-version` option selects a retained version for all units to apply. A value of 0 selects the latest configuration. Pinning does not rewind API resources or prevent new database versions from being created. Selecting an unavailable version causes the watcher to skip updates until that revision is available; it does not reconstruct a deleted revision.

History supports reviewing previous configuration and controlling what is applied. Pinning is not a Terraform rollback or a database restore.

## High availability

Multiple units share PostgreSQL while each runs its own API and Squid processes. Units independently apply rendered configuration on their polling interval. Ingress or external load balancing provides client access; shared state alone does not provide proxy traffic failover.

The leader runs database migrations. The current implementation does not establish additive-only schema changes, backward-compatible rolling upgrades, or zero-downtime restarts. These remain compatibility requirements to resolve rather than supported guarantees.

## infrastructure-services integration

Compute service definitions select a proxy through the `proxy` section. A `deployment` value of `default`, including when omitted from a supplied proxy block, selects the cloud's legacy egress proxy. That mode cannot include `rules` or `groups`. Omitting the entire proxy block preserves the existing model/cloud behavior.

Set a named deployment to use TerraSquid. The deployment must be a machine-model service in the same cloud with a TerraSquid application and exactly one ingress address. infrastructure-services configures the provider for that deployment, retrieves its API key from Vault, and configures the model's Juju and snap proxy settings.

### Model-specific rules

The `rules` list defines destinations specific to the compute service. Each rule requires a unique `name` and a `destination`; `type` accepts `ALLOW`, `DENY`, or `CONNECT` and defaults to `ALLOW`. If `ports` is omitted, `CONNECT` rules use port 443 and other rules use port 80.

```yaml
proxy:
  deployment: stg-terrasquid-ps7
  rules:
    - name: archive
      destination: archive.ubuntu.com
      type: ALLOW
      ports: [80]
    - name: snapstore
      destination: api.snapcraft.io
      type: CONNECT
    - name: terraform
      destination: registry.terraform.io
      type: CONNECT
      ports: [443]
  groups:
    - proxy-group-stg-terrasquid-ps7-general-sites
```

infrastructure-services creates a source ACL for the model's IPv4 subnet, a destination configuration for each inline rule, and ACL rules connecting that subnet to the destinations.

### Shared proxy groups

Reusable destination policy belongs in a separate network service with `service_class: proxy_rule_group` and a name prefixed with `proxy-group-`. The service selects a TerraSquid deployment and defines its own rules with the same action and port defaults.

```yaml
service_primitive: network
service_class: proxy_rule_group
name: proxy-group-stg-terrasquid-ps7-general-sites
deployment: stg-terrasquid-ps7
rules:
  - name: api-charmhub-io-https
    destination: api.charmhub.io
    type: CONNECT
  - name: ubuntu-archive-http
    destination: archive.ubuntu.com
    type: ALLOW
```

The group's Terraform root module creates the destination configurations and a destination group named after the service definition. A compute service references that name in `proxy.groups`; its root module looks up the existing group and creates an ACL from the model's subnet to the group, without recreating the group's destinations.

References are validated as `proxy_rule_group` services targeting the same deployment. The shared group must be applied before a consumer can look it up. Defining a group alone grants no source access; consumers must reference it. Changes to group policy affect all referencing consumers, while deleting a consumer leaves the independently managed group intact.

## Operations and observability

The charm publishes Prometheus scrape configuration, dashboards, and alert rules through COS integration. Current telemetry covers Squid exporter statistics, API response metrics, service health, configuration versions, and reload outcome. Django metrics share the API listener; the Squid exporter has its own listener.

Bundled alerts cover service health, version skew, and reload failures. Detailed denied-destination analysis, dedicated configuration-validation failure counters, and dashboard links to corresponding Loki events are not implemented. Collection of application and Squid log files must be verified in the deployed logging setup; this spec does not assume automatic forwarding of every source.

An unauthenticated status endpoint reports the responding unit's database version, locally recorded application version, reload outcome, timestamp, and unit identity. Pinning can intentionally produce version divergence. Current status bookkeeping also has failure-reporting limitations described below, so matching version numbers alone do not prove successful application.

The Django admin interface supports resource administration and read-only rendered-configuration history. The `createsuperuser` charm action provisions an administrator. This existing operator interface is distinct from the proposed end-user rule browser.

## Known gaps and unresolved requirements

The following findings remain visible requirements or design decisions. Documenting them here does not imply they have been implemented or that the intended guarantees have been waived.

| Area | Current limitation and required follow-up |
| :---- | :---- |
| Readiness and failure reporting | Watcher failures do not reliably set Juju status to blocked. Failed reloads can record an attempted version as applied, and unchanged-file checks update the reload timestamp without reloading. Define and implement truthful applied-version and readiness semantics. |
| Upgrade compatibility | Migration history includes removed fields/models, and API services restart during upgrades. Define supported upgrade paths and verify compatibility and availability rather than claiming zero downtime. |
| Validation | API field validation does not uniformly enforce the advertised name, port, and destination constraints. Parser validation is skipped without Squid, and raw charm configuration lacks a synchronous validation gate. Define consistent rejection behavior. |
| Observability and audit | Dedicated validation-failure telemetry, detailed denial dashboards, verified log forwarding, and per-mutation audit records are incomplete or absent. Define the required operational coverage. |
| Caching | The service is intended for forwarding/filtering, but the default configuration does not explicitly prohibit caching. Decide and enforce the policy. |
| Release identity | Confirm the published provider source and supported versions in release documentation rather than relying on the previous placeholder registry example. |

## Future work

The following capabilities are proposals, not current interfaces or release guarantees:

- **End-user rule browser:** provide policy search and explanation without requiring direct database administration. Authentication and authorization need a separate design.
- **Access-policy checks:** explain whether a source, destination, port, and method would match current policy. A future Terraform data source could expose this capability, but checking persisted rules would not validate unapplied planned changes.
- **Cloud Console integration:** expose policy management through the console using supported API ownership boundaries.
- **Temporary policy:** consider expiry for explicitly unmanaged/operator policy. Terraform would recreate expired resources that remain declared, and API-key authentication cannot distinguish Terraform from other API clients.
- **Audit export:** provide an operator-authorized, queryable history of policy mutations. Retained rendered configurations are not a substitute for attributed before/after audit records.

# Spec history and changelog

Record specification changes and review decisions here. Meeting entries should include attendees and conclusions.

| Author(s) | Status | Date | Comment |
| :---- | :---- | :---- | :---- |
| [Alex Lukens](mailto:alex.lukens@canonical.com) | Braindump | 29 Apr 2026 | Brain dump |
| Person | Drafting | Date | Initial review, comments |
| Person | Approved | Date |  |
| Copilot | Implementation audit | 15 Oct 2026 | Aligned the spec with current ownership, policy resources, polling, history, and infrastructure-services integration; removed duplicated reference details and separated known gaps from future work. |
