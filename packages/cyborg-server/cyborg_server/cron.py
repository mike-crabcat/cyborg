"""Cron expression parsing, validation, and next-occurrence computation."""

from __future__ import annotations

from datetime import datetime, timedelta


def validate_cron_expression(expression: str) -> str:
    """Validate a simple five-field cron expression."""

    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("Cron expressions must contain exactly five fields")

    ranges = (
        (0, 59),
        (0, 23),
        (1, 31),
        (1, 12),
        (0, 7),
    )
    for part, (lower, upper) in zip(fields, ranges, strict=True):
        for token in part.split(","):
            _validate_cron_token(token, lower, upper)
    return expression


def next_cron_occurrence(expression: str, start: datetime | None = None) -> datetime:
    """Compute the next timestamp matching a five-field cron rule."""

    reference = (start or datetime.now().astimezone()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    minute_values, hour_values, day_values, month_values, weekday_values = [
        _expand_cron_field(field, bounds)
        for field, bounds in zip(
            expression.split(),
            ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7)),
            strict=True,
        )
    ]

    for offset in range(0, 366 * 24 * 60):
        candidate = reference + timedelta(minutes=offset)
        weekday = (candidate.weekday() + 1) % 7
        if 7 in weekday_values:
            weekday_values = set(weekday_values)
            weekday_values.add(0)
        if (
            candidate.minute in minute_values
            and candidate.hour in hour_values
            and candidate.day in day_values
            and candidate.month in month_values
            and weekday in weekday_values
        ):
            return candidate

    raise ValueError("Unable to resolve the next cron occurrence within one year")


def _expand_cron_field(field: str, bounds: tuple[int, int]) -> set[int]:
    lower, upper = bounds
    values: set[int] = set()
    for token in field.split(","):
        values.update(_expand_cron_token(token, lower, upper))
    return values


def _expand_cron_token(token: str, lower: int, upper: int) -> set[int]:
    if token == "*":
        return set(range(lower, upper + 1))
    if token.startswith("*/"):
        step = int(token[2:])
        return set(range(lower, upper + 1, step))
    if "/" in token:
        base, step_text = token.split("/", 1)
        step = int(step_text)
        base_values = sorted(_expand_cron_token(base, lower, upper))
        return set(base_values[::step])
    if "-" in token:
        start_text, end_text = token.split("-", 1)
        start_value = int(start_text)
        end_value = int(end_text)
        return set(range(start_value, end_value + 1))
    return {int(token)}


def _validate_cron_token(token: str, lower: int, upper: int) -> None:
    """Validate a single cron token, raising ValueError on problems."""

    if token == "*":
        return
    if "/" in token:
        base, step = token.split("/", 1)
        if not step.isdigit() or int(step) <= 0:
            raise ValueError("Cron step values must be positive integers")
        _validate_cron_token(base, lower, upper)
        return
    if token.startswith("*/"):
        step = token[2:]
        if not step.isdigit() or int(step) <= 0:
            raise ValueError("Cron step values must be positive integers")
        return
    if "-" in token:
        start, end = token.split("-", 1)
        if not (start.isdigit() and end.isdigit()):
            raise ValueError("Cron ranges must use integers")
        start_value = int(start)
        end_value = int(end)
        if not (lower <= start_value <= upper and lower <= end_value <= upper):
            raise ValueError("Cron values are out of range")
        if start_value > end_value:
            raise ValueError("Cron range start must be <= range end")
        return
    if token.isdigit():
        value = int(token)
        if not lower <= value <= upper:
            raise ValueError("Cron values are out of range")
        return
    raise ValueError(f"Unsupported cron token: {token}")
