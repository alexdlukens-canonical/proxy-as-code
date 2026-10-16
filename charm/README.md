# Terrasquid Charm

## Destination port ranges

Destination resources accept an optional `port_ranges` list in addition to the
existing integer `ports` list. For example, `"port_ranges": ["30000-32767"]` allows
an inclusive range without expanding the generated Squid port ACL into thousands
of individual ports. Ports and range endpoints must be between `1` and `65535`,
and each range must have start less than or equal to end.

The API combines both lists. A destination with only ranges does not gain an
implicit default port. When both lists are unset, `null`, or empty, the existing
defaults remain: `443` for `CONNECT`, otherwise `80`.

`port_ranges` defaults to `null`, including for existing database records after
the upgrade migration. Existing integer-only policies render unchanged.
API updates can clear ranges with `[]` or `null`; partial updates that omit
`port_ranges` retain its current value. The Terraform destination resource
also supports optional `port_ranges`, defaulting to unset.

## Persistent credentials

The Django `SECRET_KEY` is stored in the application-owned Juju secret labelled
`terrasquid-django`. All units use the same key, and replacement units retrieve it
from Juju rather than generating a new one. This requires Juju 3 or later.

On upgrade from a revision with a local `/var/lib/terrasquid/secret_key`, the leader
copies its existing key unchanged into the Juju secret before removing the local
file. If storing the secret fails, the file is retained and the hook fails for
retry. An existing Juju secret is never overwritten by a unit's local key.
Followers wait for the shared key before configuring the workload. Previously,
each unit had its own key; adopting the leader's key can invalidate sessions
signed by another unit's old key.

Database credentials remain managed by the PostgreSQL relation. TLS private keys
are already stored in Juju secrets by the certificates library and can be
reissued for replacement units. API keys and superuser passwords are stored as
hashes in PostgreSQL, not recoverable plaintext secrets; retain the original API
key or rotate it if lost. Protect and back up both the Juju controller's secret
storage and PostgreSQL: application secrets survive unit replacement, not
application removal or loss of the controller's secret store.

The workload still needs a local environment file containing the Django key and
database URL. The charm creates it with restricted permissions (0640).

## Unit tests

With Python 3.12 available, install `tox` and `tox-uv`, then run `tox -e unit`
from the `charm/` directory. GitHub Actions runs the same unit suite on pull
requests that change files under `charm/`.

The unit command measures line and branch coverage for all Python code under
`src/`, including the charm and Django application, but not vendored libraries
under `lib/`. It lists uncovered lines in the terminal and writes an HTML report
to `htmlcov/index.html` and a machine-readable report to `coverage.xml`.
Open the HTML report in a browser to inspect uncovered lines and branches.
Generated reports and coverage data are ignored by Git.

To focus on one test class, run `tox -e unit -- -k TestInstall`.
Coverage still includes all of `src/`, so a focused run will report lower overall
coverage. To run without coverage, use `tox -e unit -- --no-cov`.
No minimum coverage threshold is enforced.

GitHub Actions uploads the HTML and XML reports as the `charm-unit-coverage`
artifact, including when tests fail and reports are available. Download the
artifact from the workflow run to inspect coverage.

Charm libraries under `charm/lib/` are vendored dependencies and must be committed
so the tests work from a clean checkout.

To test the workflow locally with act, run
`act pull_request -W .github/workflows/charm-unit-tests.yaml -j unit-tests`
from the repository root. If act reports an unsupported platform, add
`-P ubuntu-24.04=catthehacker/ubuntu:act-24.04` to the command or to your act
configuration file. This runs the job but does not verify GitHub's PR path filter.
## Roll back the applied Squid configuration

Terrasquid retains every complete rendered Squid configuration from the trailing
24 hours. For older history, it retains the latest revision from each UTC day.
If an older revision is pinned, that revision becomes the retained revision for
its day. In Django admin, open **Rendered config histories** and select a version
to inspect the exact configuration that was generated.

To apply a retained version across every unit, set the application-wide pin:

```shell
juju config <application> squid-pinned-config-version=<version>
```

Pinning changes the configuration applied to Squid. It does not rewind sources,
destinations, or ACL rules managed through the API or Terraform. Those resources
remain at their latest state, and writes continue to create new database config
versions.

Check `/api/v1/status/` on each unit after the watcher runs. While pinned,
`applied_config_version` should equal the selected version and may differ from
`db_config_version`.

To resume the latest rendered configuration, clear the pin:

```shell
juju config <application> squid-pinned-config-version=0
```

## Grafana Dashboards

The charm publishes the `Terrasquid Overview` and `Squid Metrics` dashboards through its `cos-agent` relation.

`Squid Metrics` is based on [Grafana dashboard 13582, "9103 - Squid"](https://grafana.com/grafana/dashboards/13582-9103-squid/). It retains the Prometheus Squid exporter coverage from that dashboard, but uses current Grafana panels and the COS-provisioned Prometheus datasource. The upstream export also contained Graylog/Elasticsearch queries and deprecated Grafana panels, which are not available in the Terrasquid COS integration.

## Workload logs

The API, configuration watcher, and Squid exporter write standard output and errors
to `/var/log/terrasquid/api.log`, `/var/log/terrasquid/watcher.log`, and
`/var/log/terrasquid/squid-exporter.log`. Django logs at INFO level and includes
trace and span identifiers when a trace is active. Gunicorn access logs include the
request method, path, status, and duration, but omit query strings and headers.

The charm installs a logrotate policy with daily rotation, a 20 MiB size threshold
checked when logrotate runs, and seven retained rotations. It uses `copytruncate`
because systemd keeps the output files open; a small loss window exists during
copying and truncation. Log files are created with mode 0640 in a mode 0750 directory.
Squid writes access and cache logs under `/var/log/squid`; its package manages rotation.
The generated configuration sets `logfile_rotate 0` to avoid competing with logrotate.

The OpenTelemetry Collector subordinate collects `/var/log/**/*log`. Relate it to
Terrasquid using `cos-agent` and connect the collector's `send-loki-logs` endpoint
to Loki. No separate logging relation on Terrasquid is needed. Custom Squid
configuration can override the logging directives; keep any alternative files in
the collector's configured scrape paths.

## Workload tracing

The existing `cos-agent` relation requests an OTLP/HTTP receiver. When the collector
advertises one, the charm configures Django request tracing and Psycopg 3 database
spans. Tracing initializes after Gunicorn forks its workers. The watcher and
management commands initialize their own exporters and flush them before exiting.

Squid configuration rendering, validation, application, and reload have explicit
spans, including failure status and the configuration version. Squid proxy traffic
does not emit native OpenTelemetry spans; observe it through access logs and exporter
metrics. The asynchronous watcher does not automatically continue an API request's
trace across the database polling boundary.

Tracing uses parent-based sampling with a 10% rate for new root traces and includes
Juju topology resource attributes. Metrics and status requests are excluded.
No request-header or body capture is enabled. HTTPS exporters validate certificates
using the workload's trust store; trust the collector's CA on the machine when using
a private CA.

Connect the collector's `send-traces` endpoint to Tempo's `tracing` endpoint to
retain traces. Receiver changes restart the API to refresh its environment. Removing
the collector relation disables tracing without stopping the API; metrics and
file logging work without a tracing backend.
