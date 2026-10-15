"""Workload tracing, OTLP export, and log correlation tests."""

import grp
import io
import logging
import os
import pwd
import shutil
import subprocess
import sys
import textwrap
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from terrasquid import telemetry


def test_tracing_is_optional(monkeypatch):
    """Without a negotiated receiver there must be no exporter or instrumentation."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    with patch.object(telemetry, "TracerProvider") as provider:
        assert telemetry.configure_tracing() is None
        provider.assert_not_called()


def test_log_correlation():
    """Application log lines must contain the active trace and span identifiers."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    formatter = settings.LOGGING["formatters"]["standard"]
    handler.setFormatter(logging.Formatter(fmt=formatter["format"], style=formatter["style"]))
    handler.addFilter(telemetry.TraceContextFilter())
    record = logging.LogRecord("terrasquid", logging.INFO, __file__, 1, "test message", (), None)
    context = trace.SpanContext(trace_id=0x123, span_id=0x456, is_remote=False)
    with trace.use_span(trace.NonRecordingSpan(context)):
        handler.handle(record)
    assert "trace_id=00000000000000000000000000000123" in stream.getvalue()
    assert "span_id=0000000000000456" in stream.getvalue()
    assert "test message" in stream.getvalue()


@pytest.fixture()
def otlp_receiver():
    """Receive real trace exports without sending telemetry outside the test process."""
    batches = []

    class Receiver(BaseHTTPRequestHandler):
        """Receive protobuf trace exports on a loopback-only test endpoint."""

        def do_POST(self):  # noqa: N802
            """Decode the exact OTLP payload sent by the production exporter."""
            assert self.path == "/v1/traces"
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            batches.append(ExportTraceServiceRequest.FromString(payload))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            """Suppress the HTTP server's access log."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/traces", batches
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.fixture()
def tracing_environment(otlp_receiver):
    """Configure a standalone workload process against the local receiver."""
    endpoint, _batches = otlp_receiver
    return {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parents[2] / "src" / "django_app"),
        "DJANGO_SETTINGS_MODULE": "terrasquid.settings",
        "DATABASE_URL": "sqlite:///:memory:",
        "DEBUG": "true",
        "OTEL_SERVICE_NAME": "terrasquid-test",
        "OTEL_RESOURCE_ATTRIBUTES": "juju_model=test,juju_unit=terrasquid/0",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": endpoint,
        "OTEL_TRACES_SAMPLER": "always_on",
    }


@pytest.mark.parametrize("outcome", ["success", "unchanged", "validation_error", "reload_error"])
def test_workload_exports_django_and_squid_spans(tmp_path, outcome, otlp_receiver, tracing_environment):
    """A real Django request must export correlated spans through an OTLP/HTTP receiver."""
    _endpoint, batches = otlp_receiver
    code = textwrap.dedent("""\
        import logging
        import subprocess
        import sys
        import types
        from pathlib import Path
        from unittest.mock import patch
        import django
        from django.core.management import call_command
        from django.http import JsonResponse
        from django.test import Client, override_settings
        from django.urls import path
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
        from terrasquid.telemetry import configure_tracing

        provider = configure_tracing()
        assert provider is configure_tracing()
        assert PsycopgInstrumentor().is_instrumented_by_opentelemetry
        django.setup()
        call_command("migrate", verbosity=0)
        from terrasquid.api.management.commands import render_squid_config as command
        from terrasquid.api.squid_render import render_squid_config

        directory = Path(sys.argv[1])
        outcome = sys.argv[2]
        squid_binary = directory / "squid"
        squid_binary.touch()
        squid_conf = directory / "squid.conf"
        urls = types.ModuleType("telemetry_test_urls")

        def configure(request):
            logging.getLogger("terrasquid.test").info("configuring proxy")
            rendered = render_squid_config()
            assert "access_log daemon:/var/log/squid/access.log squid" in rendered
            assert "cache_log /var/log/squid/cache.log" in rendered
            assert "logfile_rotate 0" in rendered
            if outcome == "unchanged":
                squid_conf.write_text(rendered)
            results = [subprocess.CompletedProcess([], 1 if outcome == "validation_error" else 0,
                                                  stdout="", stderr="parse failure")]
            results.append(subprocess.CompletedProcess([], 1 if outcome == "reload_error" else 0,
                                                        stdout="", stderr="reload failure"))
            command_instance = command.Command()
            with patch.object(command, "SQUID_CONF", squid_conf), \\
                 patch.object(command, "SQUID_CONF_NEW", directory / "squid.conf.new"), \\
                 patch("subprocess.run", side_effect=results):
                try:
                    command_instance.handle(output=None)
                except SystemExit as error:
                    assert error.code == 1
                    return JsonResponse({}, status=500)
            return JsonResponse({})

        urls.urlpatterns = [
            path("work/", configure),
            path("metrics", lambda request: JsonResponse({})),
            path("api/v1/status/", lambda request: JsonResponse({})),
        ]
        with override_settings(ROOT_URLCONF=urls, SQUID_BINARY=str(squid_binary),
                               TERRASQUID_STATUS_FILE=str(directory / "status.json")):
            client = Client()
            response = client.get("/work/", HTTP_AUTHORIZATION="Api-Key do-not-export",
                                  HTTP_TRACEPARENT="00-11111111111111111111111111111111-2222222222222222-01")
            assert response.status_code == (500 if outcome.endswith("error") else 200)
            assert client.get("/metrics?test=1").status_code == 200
            assert client.get("/api/v1/status/").status_code == 200
        provider.shutdown()
    """)
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), outcome],
        env=tracing_environment,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert batches, "The workload did not export any traces"
    resources = [resource for batch in batches for resource in batch.resource_spans]
    attributes = {attribute.key: attribute.value.string_value for attribute in resources[0].resource.attributes}
    assert attributes["service.name"] == "terrasquid-test"
    assert attributes["juju_unit"] == "terrasquid/0"
    spans = [span for resource in resources for scope in resource.scope_spans for span in scope.spans]
    by_name = {span.name: span for span in spans}
    assert "GET work/" in by_name
    assert {"squid.render_config", "squid.configure", "squid.apply_config"} <= by_name.keys()
    assert all("metrics" not in name and "status" not in name for name in by_name)
    assert all(span.trace_id.hex() == "11111111111111111111111111111111" for span in spans)
    assert by_name["squid.apply_config"].parent_span_id == by_name["squid.configure"].span_id
    if outcome != "unchanged":
        assert "squid.validate_config" in by_name
    if outcome in ("success", "reload_error"):
        assert "squid.reload" in by_name
    if outcome.endswith("error"):
        assert by_name["squid.apply_config"].status.code == 2
        failed_span = "squid.reload" if outcome == "reload_error" else "squid.validate_config"
        assert by_name[failed_span].status.code == 2
    assert "trace_id=11111111111111111111111111111111" in result.stderr
    assert "do-not-export" not in str(batches) + result.stderr


def test_management_command_flushes_traces(tmp_path, otlp_receiver, tracing_environment):
    """A short-lived management process must export spans before it exits."""
    _endpoint, batches = otlp_receiver
    manage = Path(__file__).parents[2] / "src" / "django_app" / "manage.py"
    env = {**tracing_environment, "DATABASE_URL": f"sqlite:///{tmp_path / 'django.db'}"}
    migrate_env = {**env, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": ""}
    subprocess.run(
        [sys.executable, str(manage), "migrate", "--noinput"],
        env=migrate_env,
        capture_output=True,
        text=True,
        timeout=45,
        check=True,
    )
    result = subprocess.run(
        [sys.executable, str(manage), "render_squid_config", "--output", "-"],
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        check=True,
    )
    assert "http_port [::]:3128" in result.stdout
    names = {
        span.name
        for batch in batches
        for resource in batch.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    }
    assert {"squid.configure", "squid.render_config"} <= names


@pytest.mark.skipif(shutil.which("logrotate") is None, reason="logrotate is installed on the workload machine")
def test_logrotate_preserves_open_service_output(tmp_path, monkeypatch):
    """Rotation must preserve writes to an already-open service output file."""
    import charm

    config = tmp_path / "logrotate.conf"
    monkeypatch.setattr(charm, "TERRASQUID_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(charm, "TERRASQUID_LOGROTATE_FILE", config)
    charm.SquidAsAServiceCharm._write_logging_config(MagicMock())
    user = pwd.getpwuid(os.geteuid()).pw_name
    group = grp.getgrgid(os.getegid()).gr_name
    # The workload runs as root, but unit tests must not require elevated privileges.
    config.write_text(config.read_text().replace("su root root", f"su {user} {group}"))
    logfile = tmp_path / "logs" / "api.log"
    logfile.write_text("before rotation\n")
    with logfile.open("a") as service_output:
        result = subprocess.run(
            ["logrotate", "--force", "--state", str(tmp_path / "state"), str(config)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        service_output.write("after rotation\n")
    assert logfile.read_text() == "after rotation\n"
    assert logfile.with_name("api.log.1").read_text() == "before rotation\n"
