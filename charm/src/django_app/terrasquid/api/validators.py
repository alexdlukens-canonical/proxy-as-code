"""Validation for destination port restrictions shared by the API and admin."""

import re

from django.core.exceptions import ValidationError

PORT_RANGE_PATTERN = r"[0-9]{1,5}-[0-9]{1,5}"


def validate_ports(value: list[int] | None) -> None:
    """Require integer ports within the TCP port bounds."""
    if value is None:
        return
    if not isinstance(value, list) or any(type(port) is not int or not 1 <= port <= 65535 for port in value):
        raise ValidationError("Ports must be a list of integers between 1 and 65535.")


def validate_port_ranges(value: list[str] | None) -> None:
    """Require inclusive start-end ranges within the TCP port bounds."""
    if value is None:
        return
    if not isinstance(value, list):
        raise ValidationError("Port ranges must be a list of strings in start-end format.")
    for port_range in value:
        if not isinstance(port_range, str) or re.fullmatch(PORT_RANGE_PATTERN, port_range) is None:
            raise ValidationError("Port ranges must use start-end format, for example 30000-32767.")
        start, end = map(int, port_range.split("-"))
        if not 1 <= start <= end <= 65535:
            raise ValidationError("Port range endpoints must be between 1 and 65535 with start <= end.")
