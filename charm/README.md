# Terrasquid Charm

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
