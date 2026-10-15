#!/usr/bin/env python3
"""Django management script for the Terrasquid application."""

import os
import sys


def main() -> None:
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "terrasquid.settings")
    from terrasquid.telemetry import configure_tracing

    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    provider = configure_tracing()
    try:
        execute_from_command_line(sys.argv)
    finally:
        if provider is not None:
            provider.shutdown()


if __name__ == "__main__":
    main()
