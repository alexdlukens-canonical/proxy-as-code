# Terrasquid Charm

## Unit tests

With Python 3.12 available, install `tox` and `tox-uv`, then run `tox -e unit`
from the `charm/` directory. GitHub Actions runs the same unit suite on pull
requests that change files under `charm/`.

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
