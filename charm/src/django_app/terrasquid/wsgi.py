"""WSGI configuration for the Terrasquid project."""

import os

from django.core.wsgi import get_wsgi_application

from terrasquid.telemetry import configure_tracing

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "terrasquid.settings")

configure_tracing()
application = get_wsgi_application()
