"""Parse simple log lines like '2026-10-05 12:00:01 ERROR disk full'."""

from dataclasses import dataclass


@dataclass
class Entry:
    """One parsed log line."""

    date: str
    time: str
    level: str
    message: str


def parse_line(line: str) -> Entry:
    """Split a line into date, time, level and message."""
    date, time, level, message = line.split(" ", 3)
    return Entry(date, time, level, message)


def count_levels(lines: list[str]) -> dict[str, int]:
    """How many lines there are per level."""
    counts: dict[str, int] = {}
    for line in lines:
        entry = parse_line(line)
        counts[entry.level] = counts.get(entry.level, 0) + 1
    return counts


def errors(lines: list[str]) -> list[str]:
    """The messages of ERROR lines."""
    return [parse_line(line).message for line in lines if "ERROR" in line]
