

| Index | IS140 |  |  |
| :---- | :---- | :---- | :---- |
| Title | Squid-as-a-Service |  |  |
| **[Type](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | **Author(s)** | **[Status](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | **Created** |
| Implementation | [Alex Lukens](mailto:alex.lukens@canonical.com) | Braindump | 27 Apr 2026 |
|  | **Reviewer(s)** | **Status** | **Date** |
|  | [James Simpson](mailto:james.simpson@canonical.com) | Pending Review | Date |

# Abstract

This spec outlines a proposal for a new service to offer Squid ACLs “as a service” in coordination with service definitions in gh:infrastructure-services.

# Rationale

Currently, adjusting proxy rules requires manual work to identify a subnet, whether an existing ACL exists for a subnet or site, and creation of a merge proposal into the lp:canonical-is-internal-proxy-configs repository.

This requires end users to have a deep understanding of how Squid works, is error-prone, and does not detect drift (if e.g. another user removes your rule in a future MP). This will become increasingly more important as Canonical moves towards fully-isolated networking environments in future Prodstack clouds.

# Specification

## High level overview

Squid-aaS should compose of a Django REST Framework application, a Squid proxy deployment, and an associated Terraform provider. The Django Application and  Squid proxy deployments should be co-located on the same system, such that requests handled by Django can directly modify the Squid configuration file.

A Terraform provider shall be created that exposes resources corresponding to Squid configuration components (source ACLs, destination ACLs, destination domain/subdomains, port definitions). End users will use these Terraform resources to define proxy rules for their service directly in gh:infrastructure-services. These rules will then be automatically validated and applied on the Squid deployment, with a lifetime controlled by the Terraform resources. When an environment is deprovisioned, the associated proxy rules should be removed from Squid.

## Technology Stack

| Component | Technology |
| :---- | :---- |
| Juju charm type / base | Machine charm, `ubuntu@24.04` |
| Charm framework | Python, `ops` |
| HTTP proxy | Squid (`squid` apt package, version as shipped with Ubuntu 24.04) |
| REST API | Django REST Framework |
| API key management | `djangorestframework-api-key` |
| WSGI server | Gunicorn (managed as a systemd service by the charm) |
| Database | PostgreSQL (via `postgresql` Juju charm relation) |
| Terraform provider SDK | `terraform-plugin-framework` |

## Service Workload Application

The `squid-as-a-service` charm is a **machine charm** targeting `ubuntu@24.04`. It co-locates and manages two workloads:

1. **Squid proxy** — installed from the `squid` apt package. The charm owns `/etc/squid/conf.d/terrasquid.conf`. The base `/etc/squid/squid.conf` is managed by the charm and includes this file via an `include` directive. Squid is reloaded with `squid -k reconfigure` after every configuration change, avoiding dropped connections.  
     
2. **Django REST API** — a Django REST Framework application served via Gunicorn. Gunicorn runs as a systemd service (managed by the charm) and binds to `0.0.0.0:<api-port>` to allow load-balanced access in HA deployments.

State is stored in a PostgreSQL database via the `database` Juju relation. The charm supports scaling to HA deployments of 3+ units with a uniform Squid configuration across all units (see [High Availability](#high-availability)).

### **Charm Relations**

| Relation name | Interface | Role | Purpose |
| :---- | :---- | :---- | :---- |
| `database` | `postgresql_client` | requires | Persistent state via PostgreSQL |
| `ingress` | `ingress` | requires | Exposes the Django REST API behind a load balancer |
| `squid-aaas-peers` | `squid-aaas-peers` | peer | Leader election and HA coordination |
| `cos-agent` | `cos_agent` | requires | COS integration (metrics, logs, Grafana dashboard) |

### **Charm Actions**

| Action | Parameters | Description |
| :---- | :---- | :---- |
| `create-key` | `name` (string) | Creates a new named API key; displays the full key once. The key is never stored in plaintext. |
| `revoke-key` | `name` (string) | Marks the named key as revoked. The key is immediately rejected by the API. Existing resources are **not** deleted. |
| `rotate-key` | `name` (string) | Atomically replaces the named key with a new one. The old key is immediately invalid; resources are preserved. Update the key in your secrets manager before the next `terraform apply`. |
| `list-keys` | — | Lists all key names, their 8-character prefix (for identification), creation timestamp, and revoked status. |
| `reconfigure` | — | Manually triggers a Squid config re-render and `squid -k reconfigure` on the current unit. |

### **Charm Configuration**

| Config key | Type | Default | Description |
| :---- | :---- | :---- | :---- |
| `squid-port` | int | `3128` | Squid HTTP proxy listen port |
| `api-port` | int | `8000` | Gunicorn listen port |
| `gunicorn-workers` | int | `4` | Gunicorn worker process count |
| `squid-prepend-config` | string | `""` | Raw Squid configuration inserted before generated source ACLs |
| `squid-append-config` | string | `""` | Raw Squid configuration inserted after generated ACL rules and before the default deny |

## Authentication

### **Overview**

API key management uses [`djangorestframework-api-key`](https://florimondmanca.github.io/djangorestframework-api-key/), which is the standard Django REST Framework library for this purpose. It provides:

- Named, revocable API keys stored as hashed values (the plaintext is shown exactly once at creation and never persisted).  
- A short **prefix** (first 8 characters) stored in plaintext alongside the hash, used to identify which key is being used in logs without exposing the secret.  
- A `HasAPIKey` DRF permission class applied to all API views.

Keys are sent in the standard header:

```
Authorization: Api-Key <key>
```

Multiple named keys can coexist. A single key may be shared across multiple `terraform` root modules / services — the `service` label in the provider config provides the per-service attribution, independent of which key is used.

### **Key Lifecycle**

Keys are managed exclusively via Juju charm actions on the leader unit:

```
# Create a key for a team or deployment pipeline
juju run squid-as-a-service/leader create-key name=ps7-team

# Rotate a key (e.g. after a credential leak) without touching resources
juju run squid-as-a-service/leader rotate-key name=ps7-team

# Revoke a key permanently (e.g. when a team is decommissioned)
juju run squid-as-a-service/leader revoke-key name=ps7-team

# Inspect all keys
juju run squid-as-a-service/leader list-keys
```

Revocation marks a key as revoked in the database; subsequent requests using it receive **HTTP 403**. Existing resources created with the revoked key are **not** automatically deleted, since the same key may have been used for multiple services. An operator can clean up specific services manually via the API or by running `terraform destroy` with the new key and the same `service` label.

### **Service Label**

All resources carry a `service` field (e.g. `ps7-myservice-production`) set via the provider `service` attribute (see [Provider Configuration](#provider-configuration)). This field is:

- Stored on every resource record in the database alongside the key prefix that created it.  
- Surfaced in all API list responses for auditing.  
- Used as the namespace prefix for ACL names in the generated Squid config (`<service>__<user-name>`).  
- Shown in Loki logs alongside every API write event.

The `service` label has no effect on access control — it is purely for attribution and auditing.

### **Key Storage**

Keys must be stored in a secrets manager (e.g. Vault or GitHub Actions secrets) and injected as `var.terrasquid_api_key`. They must not be committed to source control.

## REST API Design

### **Versioning**

All endpoints are prefixed with `/api/v1/`.

### **Endpoints**

| Method | Path | Description |
| :---- | :---- | :---- |
| `GET / POST` | `/api/v1/sources/` | List or create source ACLs |
| `GET / PUT / DELETE` | `/api/v1/sources/{id}/` | Retrieve, update, or delete a source ACL |
| `GET / POST` | `/api/v1/source-groups/` | List or create source groups |
| `GET / PUT / DELETE` | `/api/v1/source-groups/{id}/` | Retrieve, update, or delete a source group |
| `GET / POST` | `/api/v1/destinations/` | List or create destination configurations |
| `GET / PUT / DELETE` | `/api/v1/destinations/{id}/` | Retrieve, update, or delete a destination configuration |
| `GET / POST` | `/api/v1/destination-groups/` | List or create destination groups |
| `GET / PUT / DELETE` | `/api/v1/destination-groups/{id}/` | Retrieve, update, or delete a destination group |
| `GET / POST` | `/api/v1/port-groups/` | List or create port groups |
| `GET / PUT / DELETE` | `/api/v1/port-groups/{id}/` | Retrieve, update, or delete a port group |
| `GET / POST` | `/api/v1/acl-rules/` | List or create ACL rules |
| `GET / PUT / DELETE` | `/api/v1/acl-rules/{id}/` | Retrieve, update, or delete an ACL rule |

List endpoints return only resources belonging to the `service` label of the authenticating caller.

### **Status Endpoint**

`GET /api/v1/status/` — unauthenticated (or token-authenticated for per-unit detail). Returns the current sync state of the responding unit:

```json
{
  "db_config_version": 42,
  "applied_config_version": 42,
  "last_reload": "2026-04-29T10:15:03Z",
  "last_reload_ok": true,
  "unit": "squid-as-a-service/1"
}
```

A divergence between `db_config_version` and `applied_config_version` indicates the unit has not yet applied the latest config (watcher lag or a blocked watcher). Operators can poll this endpoint across all units to confirm sync after a change.

| Field | Rule |
| :---- | :---- |
| Source `src` | Valid IPv4 or IPv6 CIDR (e.g. `10.0.0.0/24`, `2001:db8::/32`) |
| Destination `dst` | Valid domain, wildcard subdomain (leading `.`, e.g. `.example.com`), or CIDR block |
| Port | Integer 1–65535 |
| `type` | One of `ALLOW`, `DENY`, `CONNECT` |
| `name` | Unique within the owning service; pattern `[a-zA-Z0-9_-]+`, max 63 characters |

Validation errors return HTTP 400 with a structured error body.

### **De-duplication**

Within a service's namespace, a `(service, name)` unique constraint prevents duplicate records. If an identical `POST` is received (same `service` \+ `name`), the API returns the existing resource with HTTP 200 rather than creating a duplicate. At config render time, logically identical ACL entries from different services are emitted only once.

## Squid Configuration Management

### **Config Generation**

The Django application maintains a **config version counter** in PostgreSQL, incremented on every create, update, or delete operation. A Jinja2 template renders the full Squid ACL block from the current database state and writes it atomically to `/etc/squid/conf.d/terrasquid.conf`.

The generated file follows this structure:

```
# Generated by squid-as-a-service — DO NOT EDIT MANUALLY
# Config version: <n>  Generated: <timestamp>

# Built-in CONNECT method matcher (emitted once when any CONNECT rule exists)
acl terrasquid_connect_method method CONNECT

# Port groups  (name format: <service>__<user-name>)
acl ps7_myservice__web_ports port 80 443

# <squid-prepend-config charm config value is inserted here>

# Source ACLs  (name format: <service>__<user-name>)
acl ps7_myservice__instance_net src 10.20.0.0/16

# Destination ACLs  (name format: <service>__<user-name>)
acl ps7_myservice__ubuntu_archive dstdomain archive.ubuntu.com
acl ps7_myservice__internal_api dst 10.99.0.0/24

# ACL rules — ordered by priority (ascending), then DENY before CONNECT before ALLOW, then creation time
http_access deny  <source> <dest>                                       # priority 10, DENY
http_access deny  CONNECT !<dest_port_acl>                              # block CONNECT to unlisted ports
http_access allow <source> terrasquid_connect_method <dest>             # priority 100, CONNECT
http_access allow <source> <dest>                                       # priority 100, ALLOW

# <squid-append-config charm config value is inserted here>

# Default deny — must remain last
http_access deny all
```

**ACL name namespacing**: Squid requires ACL names to be globally unique within a config file, but resources from different services may share the same user-supplied `name`. The Jinja2 template therefore renders all ACL names as `<service>__<user-name>` (double-underscore separator; `service` is the sanitised provider `service` attribute with non-alphanumeric characters replaced by `_`). The `name` pattern `[a-zA-Z0-9_-]+` and max 63 characters applies to the user-supplied portion only; the combined name stays within Squid's 200-character ACL name limit.

Access entries are sorted at render time by priority ascending, then `DENY` before `CONNECT` before `ALLOW`, then rule creation time. Service, rule name, and destination bucket index provide deterministic ordering for otherwise equal entries. The default priority of `100` leaves room for operators to insert higher-priority rules with smaller numbers without renumbering.

**CONNECT rule generation detail**: When any `CONNECT`\-type destination exists, the template emits a single `acl terrasquid_connect_method method CONNECT` near the top of the file (idempotent — only once regardless of rule count). Each CONNECT rule is then rendered as:

```
http_access allow <src_acl> terrasquid_connect_method <dst_acl>
```

Squid evaluates the `method CONNECT` sub-ACL first; if the request is not a CONNECT tunnel the line is skipped, so CONNECT rules cannot accidentally match plain HTTP traffic. A guard line `http_access deny CONNECT !<combined_allowed_connect_ports>` is inserted before any CONNECT allow rules to block tunnelling to ports not covered by any active CONNECT destination, preventing open-proxy abuse.

### **Config Validation**

Before any write is committed to PostgreSQL, Django performs a **dry-run config validation** to ensure the resulting Squid configuration is syntactically correct. This gives the Terraform provider immediate, actionable feedback during `terraform apply` rather than a silent failure after the fact.

Validation procedure on each create/update API request:

1. Acquire the **per-unit config write lock** (a PostgreSQL advisory lock keyed on the unit's node ID) to serialise concurrent writes and avoid TOCTOU races between simultaneous `terraform apply` runs from different services.  
2. Render the prospective full config (as if the write had succeeded) into a temporary file, e.g. `/tmp/terrasquid-validate-<uuid>.conf`.  
3. Construct a minimal wrapper squid config that mirrors the base `squid.conf` structure but includes the temp file instead of the live `/etc/squid/conf.d/terrasquid.conf`.  
4. Run `squid -f /tmp/squid-wrapper-<uuid>.conf -k parse`. Squid parses the config and exits with code 0 on success or non-zero on error, without affecting the running process.  
5. If validation fails, release the lock, delete the temp files and return **HTTP 422 Unprocessable Entity** with the raw Squid parse error in the response body. The database is not modified.  
6. If validation passes, commit the database write, increment `ConfigVersion`, issue `NOTIFY terrasquid_config_changed`, release the lock, and delete the temp files.

Example 422 response body:

```json
{
  "error": "squid_config_invalid",
  "detail": "(squid -k parse output) parseConfigFile: /tmp/terrasquid-validate-xxx.conf line 12: ACL name 'duplicate_acl' already defined"
}
```

Delete operations do not require validation (removing a rule can only make the config smaller and cannot introduce syntax errors), but they do hold the advisory lock to prevent racing with a concurrent create/update.

### **Config Reload Flow**

A **config watcher** systemd service runs on each Juju unit. It holds a persistent connection to PostgreSQL and subscribes to the `terrasquid_config_changed` LISTEN channel. After every successful database write, Django issues `NOTIFY terrasquid_config_changed`.

On receipt of a notification the watcher:

1. Reads the current config version from PostgreSQL.  
2. If the version is newer than the last rendered version, re-renders the Jinja2 template to a staging file `/etc/squid/conf.d/terrasquid.conf.staging`.  
3. Runs `squid -f <wrapper-including-staging-file> -k parse` as a safety net. If this fails (e.g. due to a race or an edge case missed by API validation), the watcher logs the error, retains the existing live config, and sets the Juju unit status to `blocked` with the parse error — it does **not** replace the live config or call `squid -k reconfigure`.  
4. On successful parse, renames the staging file atomically over `/etc/squid/conf.d/terrasquid.conf`.  
5. Calls `squid -k reconfigure` and checks its exit code. A non-zero exit (e.g. stale PID file, Squid process crashed) is treated as a watcher failure: the unit status is set to `blocked` with the error, and `terrasquid_watcher_config_validation_failures_total` is incremented to trigger the alert.

**Watcher startup and recovery**: On start (or restart after crash/upgrade), the watcher reads the current `ConfigVersion` from PostgreSQL and compares it to the version recorded in a local state file (e.g. `/var/lib/terrasquid/last_applied_version`). If the DB version is ahead, the watcher performs a full re-render and reload before beginning to LISTEN — ensuring no events are missed during downtime.

**PostgreSQL reconnect**: On connection loss (including primary failover), the watcher reconnects with exponential backoff and re-issues `LISTEN terrasquid_config_changed` immediately on reconnect. After reconnecting it performs the startup version check described above, so any NOTIFYs that fired while it was disconnected are not lost.

This guarantees all units converge to the same configuration within seconds of any write, regardless of which unit handled the originating API request.

## High Availability

In a deployment with 3+ units:

- Each unit hosts an independent Squid proxy instance and Django REST API instance.  
- All Django instances connect to the same PostgreSQL cluster via the `database` Juju relation.  
- An `ingress` relation (or external load balancer) distributes API requests across all units.  
- Squid proxy traffic is similarly load-balanced; clients configure the load balancer VIP/FQDN as their proxy endpoint.  
- Config consistency is maintained via the PostgreSQL LISTEN/NOTIFY mechanism described above — each unit's config watcher independently re-renders and reloads Squid on version change.  
- The Juju peer relation (`squid-aaas-peers`) provides leader election. The leader unit handles charm lifecycle hooks (database migrations, schema upgrades). Non-leader units serve API and proxy traffic normally.  
- **Database migrations** use an additive-only (backward-compatible) strategy: new columns are nullable with defaults, tables are never dropped in the same release that removes code that writes to them. This allows non-leader units to continue serving traffic on the old schema while the leader runs `manage.py migrate`, eliminating the need for coordinated downtime on upgrades.

## Data Models

All models include `created_at` and `updated_at` timestamps. Cascade deletes are enforced at the database level.

| Model | Key fields | Unique constraint | Notes |
| :---- | :---- | :---- | :---- |
| `APIKey` | `id`, `name`, `prefix` (8 chars, plaintext), `hashed_key`, `created`, `revoked`, `expiry_date` | `name`, `prefix` | Managed by `djangorestframework-api-key`. Plaintext never stored after creation. |
| `SourceACL` | `id`, `service` (string), `key_prefix` (FK → APIKey.prefix), `name`, `cidr` (string array) | `(service, name)` | `key_prefix` stored for audit; not used for access control. |
| `SourceGroup` | `id`, `service` (string), `key_prefix`, `name`, `sources (M2M → SourceACL)` | `(service, name)` |  |
| `DestinationConfig` | `id`, `service` (string), `key_prefix`, `name`, `dst`, `type`, `ports` (int array), `port_groups (M2M → PortGroup)` | `(service, name)` | `ports` and `port_groups` merged at render time |
| `DestinationGroup` | `id`, `service` (string), `key_prefix`, `name`, `destinations (M2M → DestinationConfig)` | `(service, name)` |  |
| `PortGroup` | `id`, `service` (string), `key_prefix`, `name`, `ports` (int array) | `(service, name)` |  |
| `ACLRule` | `id`, `service` (string), `key_prefix`, `src (FK → SourceACL, nullable)`, `src_group (FK → SourceGroup, nullable)`, `dst (FK → DestinationConfig, nullable)`, `dst_group (FK → DestinationGroup, nullable)`, `priority` (int, default 100\) | `(service, src, src_group, dst, dst_group)` | Exactly one of `src`/`src_group` and one of `dst`/`dst_group` must be non-null |
| `ConfigVersion` | `id`, `version`, `updated_at` | Singleton (enforced in application logic) | Incremented on every write; triggers `NOTIFY` |

## Terraform Provider Resources

The Terraform provider is implemented using **terraform-plugin-framework** and communicates exclusively with the Django REST API. De-duplication and validation are handled server-side; the provider treats the API as the source of truth.

**Terraform state divergence**: All `Read` functions in the provider must handle HTTP 404 by calling `resp.State.RemoveResource(ctx)` — the standard `terraform-plugin-framework` pattern for resources deleted outside of Terraform. This can occur if resources are manually deleted via the API or if the database is restored from a backup. The next `terraform apply` will then cleanly recreate them.

### **Provider Configuration**

```
terraform {
  required_providers {
    terrasquid = {
      source  = "canonical/terrasquid"
      version = "~> 1.0"
    }
  }
}

provider "terrasquid" {
  endpoint = "https://squid-as-a-service.is.canonical.com"
  api_key  = var.terrasquid_api_key  # created via: juju run squid-as-a-service/leader create-key name=<name>
  service  = "ps7-myservice-production"  # label applied to all resources created by this provider instance
}
```

| Attribute | Required | Description |
| :---- | :---- | :---- |
| `endpoint` | yes | Base URL of the Django REST API |
| `api_key` | yes | Named API key; retrieve from a secrets manager, never hardcode |
| `service` | yes | Service label applied to all resources created by this provider instance. Used for auditing and ACL name namespacing. Pattern: `[a-zA-Z0-9_-]+`. |

### **Resources**

#### terrasquid\_source

```
resource "terrasquid_source" "ps7_instances" {
  name = "ps7-instance-network"
  src  = ["10.20.0.0/16", "10.21.0.0/16"]
}
```

* `name` (required, string): Unique ACL identifier within the service. Pattern: `[a-zA-Z0-9_-]+`.  
* `src` (required, list of strings): One or more IPv4/IPv6 CIDR blocks.

#### terrasquid\_source\_group

```
resource "terrasquid_source_group" "ps7_all" {
  name    = "ps7-all-networks"
  sources = [terrasquid_source.ps7_instances.id]
}
```

* `name` (required, string): Group identifier.  
* `sources` (required, list of strings): IDs of `terrasquid_source` resources.

#### terrasquid\_destination\_configuration

```
resource "terrasquid_destination_configuration" "ubuntu_archive" {
  name  = "ubuntu-archive"
  dst   = "archive.ubuntu.com"
  ports = [80, 443]
  type  = "ALLOW"
}

resource "terrasquid_destination_configuration" "internal_pypi" {
  name        = "internal-pypi"
  dst         = ".pypi.is.canonical.com"   # leading dot = wildcard subdomain match
  port_groups = [terrasquid_port_group.web.id]
  type        = "ALLOW"
}
```

* `name` (required, string): ACL identifier.  
* `dst` (required, string): Destination domain, wildcard subdomain (leading `.`), or CIDR block.  
* `ports` (optional, list of int): Explicit port list. Defaults to `[80]` if neither `ports` nor `port_groups` is specified. For `CONNECT` rules, defaults to `[443]`.  
* `port_groups` (optional, list of strings): IDs of `terrasquid_port_group` resources; merged with `ports` at render time.  
* `type` (required, string): One of:  
  * `ALLOW` — permits plain HTTP (or any proxied request matching the `dst` and port ACLs).  
  * `DENY` — explicitly blocks matching traffic.  
  * `CONNECT` — permits TCP tunnelling via the HTTP `CONNECT` method. This is how HTTPS and other arbitrary TCP traffic travel through an HTTP proxy: the client asks Squid to open a raw TCP tunnel to `<host>:<port>`, and Squid forwards the byte stream without inspecting it. Use this type for HTTPS endpoints or any non-HTTP protocol that needs to traverse the proxy. Default port `443`; add other ports (e.g. `5432`, `6443`) to proxy additional TCP services.

#### terrasquid\_destination\_group

```
resource "terrasquid_destination_group" "openstack_services" {
  name         = "ps7-openstack-services"
  destinations = [
    terrasquid_destination_configuration.ubuntu_archive.id,
    terrasquid_destination_configuration.internal_pypi.id,
  ]
}
```

* `name` (required, string): Group identifier.  
* `destinations` (required, list of strings): IDs of `terrasquid_destination_configuration` resources.

#### terrasquid\_acl\_rule

This is the core resource, roughly analogous to a single Squid `http_access` line.

```
# Rule from a single source to a single destination
resource "terrasquid_acl_rule" "ps7_to_archive" {
  src      = terrasquid_source.ps7_instances.id
  dst      = terrasquid_destination_configuration.ubuntu_archive.id
  priority = 100  # optional, default 100
}

# Rule from a source group to a destination group
resource "terrasquid_acl_rule" "ps7_to_openstack" {
  src_group = terrasquid_source_group.ps7_all.id
  dst_group = terrasquid_destination_group.openstack_services.id
}

# Explicit DENY at a higher priority (lower number = evaluated first)
resource "terrasquid_acl_rule" "block_malware_sites" {
  src_group = terrasquid_source_group.ps7_all.id
  dst       = terrasquid_destination_configuration.blocked_domains.id
  priority  = 10
}
```

* `src` (optional, string): ID of a `terrasquid_source` resource. Exactly one of `src` or `src_group` must be set.  
* `src_group` (optional, string): ID of a `terrasquid_source_group` resource. Exactly one of `src` or `src_group` must be set.  
* `dst` (optional, string): ID of a `terrasquid_destination_configuration`. Exactly one of `dst` or `dst_group` must be set.  
* `dst_group` (optional, string): ID of a `terrasquid_destination_group`. Exactly one of `dst` or `dst_group` must be set.  
* `priority` (optional, int): Determines the position of this rule in the rendered Squid config. Lower values are written first and evaluated first by Squid. Defaults to `100`. Rules at equal priority are ordered by destination type (`DENY`, `CONNECT`, `ALLOW`), then creation time. Service and name provide deterministic ordering for otherwise equal rules.

#### terrasquid\_port\_group

```
resource "terrasquid_port_group" "web" {
  name  = "web-ports"
  ports = [80, 443, 8080, 8443]
}
```

* `name` (required, string): Group identifier.  
* `ports` (required, list of int): Port numbers in range 1–65535.

### **Data Sources**

#### terrasquid\_source\_group (data)

```
data "terrasquid_source_group" "shared_infra" {
  name = "is-shared-infrastructure"
}
```

Looks up an existing source group by name (may be owned by a different service). Allows cross-service reference without duplicating shared definitions.

#### terrasquid\_destination\_group (data)

```
data "terrasquid_destination_group" "canonical_repos" {
  name = "canonical-package-repos"
}
```

Looks up an existing destination group by name for cross-service reference.

## Integrations

Cloud Console integration (proxy rule view/create/delete) is **out of scope** for this spec and deferred to a follow-up. The REST API is designed to be consumable by Cloud Console in future; the admin token auth model and versioned `/api/v1/` prefix should be preserved to avoid breaking changes. This integration would be especially valuable for PS7+ projects where proxy rules are a hard requirement for external connectivity.

## infrastructure-services integration

To incorporate use into infrastructure-services, we will need to update templates to utilize the terrasquid provider. There should be a default set of proxy rules that are automatically applied to new environments, a flag to disable default proxy rules (include\_default\_proxy\_rules=false, and a way to add additional proxy rules via the service definition (a proxy\_rules field.

Existing environments use the typical prodstack proxy e.g. [http://egress.ps7.internal:3128](http://egress.ps7.internal:3128). The new squid proxy must be “opt-in” via a flag use\_proxy\_provider in the service definition. For future clouds, this can potentially become the default.

For proof of concept, a separate service class should be created to avoid altering logic in the default “machine\_model” service class.

## Observability

The charm integrates with the [Canonical Observability Stack (COS)](https://charmhub.io/topics/canonical-observability-stack) via the **`cos-agent` subordinate charm**, which is the standard approach for machine charms and handles Prometheus scrape config, Loki log forwarding, and Grafana dashboard registration automatically.

### **Metrics**

The [boynux/squid-exporter](https://github.com/boynux/squid-exporter) is deployed alongside Squid to translate Squid's internal `cache_object` manager interface into Prometheus-compatible metrics. `cos-agent` scrapes the exporter's HTTP endpoint and forwards metrics to the COS Prometheus instance.

Squid is configured in **forward/filtering mode only** (`cache deny all` in `squid.conf`); caching is not a goal of this service.

Key metrics to surface:

| Metric | Description |
| :---- | :---- |
| `squid_client_http_requests_total` | Total requests received by Squid |
| `squid_client_http_errors_total` | Total client-side errors (including 403 Forbidden) |
| `squid_client_http_kbytes_in/out_total` | Throughput (bytes in/out) |
| `squid_server_http_requests_total` | Upstream requests made by Squid |
| `squid_up` | Squid process health |
| `terrasquid_api_config_validation_failures_total` | Counter incremented by Django each time a pre-commit `squid -k parse` fails (HTTP 422 returned to caller) |
| `terrasquid_watcher_config_validation_failures_total` | Counter incremented by the config watcher each time the pre-rename `squid -k parse` fails (live config preserved) |

The two `terrasquid_*` counters are exposed by a lightweight Prometheus metrics endpoint built into the Django application (`/metrics`, scrape port configurable via charm config `metrics-port`, default `9090`). `cos-agent` scrapes this endpoint alongside the squid-exporter endpoint.

### **Logs**

The following log sources are forwarded to Loki via `cos-agent`:

| Source | Path / unit | Content |
| :---- | :---- | :---- |
| Squid access log | `/var/log/squid/access.log` | Per-request log: client IP, destination, HTTP status, bytes. Primary source for 403 analysis and traffic auditing. |
| Squid cache log | `/var/log/squid/cache.log` | Squid daemon events, startup/shutdown, config reload confirmations. |
| Django / Gunicorn | journald unit `gunicorn-terrasquid` | API request log, application errors, key auth events. Validation failure events are logged here at `ERROR` level with the full `squid -k parse` output, the service label, and the API key prefix of the request. |
| Config watcher | journald unit `terrasquid-watcher` | Config render events, validation failures, reload confirmations. Watcher validation failures are logged at `CRITICAL` level. |

### **Grafana Dashboard**

A Grafana dashboard is bundled in the charm (the standard `cos-agent` approach — JSON stored under `src/grafana_dashboards/`). It provides:

- **Request rate and error rate** — total requests/s and errors/s over time.  
- **Traffic volume** — bytes in/out per unit.  
- **Denied requests** — count and rate of HTTP 403 responses, filterable by client subnet and destination host. This is the primary self-service tool for users diagnosing misconfigured proxy rules.  
- **Top denied destinations** — table of most-frequently blocked host/subnet pairs, useful for identifying missing rules.  
- **API activity** — Django REST API request counts and error rates (from Gunicorn logs), to monitor Terraform provider usage.  
- **Config validation failures** — two panels side by side: API-layer validation failures (`terrasquid_api_config_validation_failures_total` rate) and watcher-layer validation failures (`terrasquid_watcher_config_validation_failures_total` rate). Both panels link to a pre-filtered Loki log view showing the associated `squid -k parse` error output.

### **Alert Rules**

Alert rules are bundled in the charm alongside the dashboard (stored under `src/prometheus_alert_rules/`). `cos-agent` registers them with the COS Prometheus/Alertmanager instance.

```
groups:
  - name: terrasquid
    rules:
      - alert: TerrasquidAPIValidationFailures
        expr: increase(terrasquid_api_config_validation_failures_total[5m]) > 0
        for: 0m
        labels:
          severity: warning
        annotations:
          summary: "Squid config validation failure on API write (unit {{ $labels.juju_unit }})"
          description: >
            A Terraform apply submitted a rule that failed squid -k parse validation.
            The write was rejected (HTTP 422). Check Loki (gunicorn-terrasquid) for the
            full parse error, the service label, and the API key prefix of the request.

      - alert: TerrasquidWatcherValidationFailure
        expr: increase(terrasquid_watcher_config_validation_failures_total[5m]) > 0
        for: 0m
        labels:
          severity: critical
        annotations:
          summary: "Squid config watcher validation failure — live config NOT updated (unit {{ $labels.juju_unit }})"
          description: >
            The config watcher rendered a new config that failed squid -k parse.
            The live squid configuration has NOT been updated; proxy rules may be out of sync.
            Check Loki (terrasquid-watcher) for the full parse error.
            Manual intervention may be required: run 'juju run squid-as-a-service/<N> reconfigure'
            after resolving the issue.
```

The watcher failure alert is `critical` because it indicates a divergence between the database state and the running Squid configuration — proxy rules that were accepted by the API are not yet in effect.

### **Charm Relation**

```
# In metadata.yaml / charmcraft.yaml
requires:
  cos-agent:
    interface: cos_agent
```

The `cos-agent` subordinate must be deployed and related to the `squid-as-a-service` application:

```
juju deploy cos-agent
juju integrate squid-as-a-service cos-agent
```

## Stretch Goals

The following features are **not** in scope for the initial release but are desirable enhancements for future iterations.

### **Web UI**

A read-only (initially) web interface served by the Django application at `/ui/`. Styled with the [Vanilla framework](https://vanillaframework.io/) (Canonical's open-source CSS framework) to match the IS design language and remain dependency-light (no React/Vue required).

#### Rule Browser

A paginated table view of all ACL rules, sources, destinations, and port groups visible to the authenticated user. Features:

- Filter by service label, rule type (ALLOW / DENY / CONNECT), source CIDR, or destination.  
- Sortable columns (priority, created date, service).  
- Click-through to a detail page for each resource, showing the rendered Squid ACL lines that the resource contributes.  
- Uses the existing `/api/v1/` REST endpoints; no additional backend work required beyond CSRF-exempt session auth for the UI.

Vanilla components used: `p-table`, `p-pagination`, `p-search-box`, `p-tabs` (for switching between resource types).

#### Access Check Tool

An interactive form at `/ui/check/` that lets an operator answer the question: **"Would this request be allowed through the proxy?"**

Inputs:

- Source IP address  
- Destination host or IP  
- Port  
- Method (`GET` / `CONNECT`)

On submit, the backend evaluates the current in-memory rule set (same logic used at render time) and returns:

- **Permitted** / **Denied** — with the matching `http_access` line highlighted.  
- The service label and rule name that caused the match (or `default deny` if no rule matched).  
- A diff view showing which rules were evaluated and in what order before the match was found.

This replaces the current workflow of reading the raw Squid config file and manually tracing ACL evaluation order.

Vanilla components used: `p-form`, `p-notification` (green / red outcome banner), `p-code-snippet` (rendered Squid line), `p-accordion` (rule evaluation trace).

#### Key Management UI (operator-only)

A simple admin page (behind an operator-scoped session or the existing API key auth) to list API keys (names, prefixes, creation dates, revoked status) — surfacing the same data as the `list-keys` charm action without requiring Juju CLI access. Key creation and revocation remain charm-action-only to keep the trust boundary clear; this page is read-only.

### **Terraform `plan`\-time Access Check**

A `terrasquid_access_check` **data source** in the Terraform provider that evaluates a hypothetical request against the current rule set at `terraform plan` time and fails the plan if the request would be denied. Useful as a CI gate to verify that the rules being applied will actually permit the intended traffic before `terraform apply` runs.

```
data "terrasquid_access_check" "verify_apt" {
  src    = "10.20.0.1"
  dst    = "archive.ubuntu.com"
  port   = 80
  method = "GET"
}
# plan fails if the above would be denied by the current rule set
```

Backed by a new unauthenticated (or key-authenticated) endpoint `POST /api/v1/check/` that accepts `{src, dst, port, method}` and returns `{permitted: bool, matched_rule: <id or null>}`.

### **Cloud Console Integration**

Surface proxy rule management inside the IS Cloud Console (currently deferred per the [Integrations](#integrations) section). With the Web UI groundwork in place, Cloud Console could embed the Vanilla-styled rule browser in an iframe or consume the same `/api/v1/` endpoints directly, allowing PS7+ project teams to manage proxy rules without Terraform or Juju CLI access.

### **Rule Expiry**

An optional `expires_at` (RFC 3339 timestamp) field on `ACLRule`. A background task (Celery beat or a systemd timer) removes expired rules and triggers a config re-render. Useful for temporary allow-listing during incident response or maintenance windows.

**Compatibility note — API-direct use only.** Rule expiry is intentionally scoped to rules created directly via the REST API (e.g. by an operator during an incident) and is **not supported for Terraform-managed rules**. The reason is a fundamental incompatibility with Terraform's reconciliation model: when the expiry task deletes a rule, the resource still exists in Terraform state. On the next `terraform plan`, the provider's `Read` function receives a 404, removes the resource from state (per the standard `RemoveResource` pattern), and Terraform immediately schedules a `+ create` to restore it — defeating the expiry entirely.

To enforce this, the API should reject `expires_at` on any `POST /api/v1/acl-rules/` request that carries a valid API key (i.e. Terraform-originated requests), returning **HTTP 400**. Expiry-capable rules must be created via a separate operator-scoped mechanism (e.g. a dedicated endpoint or a charm action) that does not issue Terraform-compatible resource IDs, making it clear they are outside the Terraform lifecycle.

### **Audit Log Export**

A `GET /api/v1/audit/` endpoint (operator-scoped) that returns a paginated, filterable log of every create / update / delete operation, including the timestamp, service label, API key prefix, resource type, resource ID, and a before/after diff of the resource fields. Complements the Loki logs with a structured, queryable history.

# Spec History and Changelog

Please be thorough when recording changes and progress with the spec itself and the work resulting from it. Record every meeting, attendees and conclusions from the meeting.

| Author(s) | Status | Date | Comment |
| :---- | :---- | :---- | :---- |
| [Alex Lukens](mailto:alex.lukens@canonical.com) | Braindump | 29 Apr 2026 | Brain dump |
| Person | Drafting | Date | Initial review, comments |
| Person | Approved | Date |  |
|  |  |  |  |

