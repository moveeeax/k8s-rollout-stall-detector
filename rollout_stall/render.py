"""Turn findings into a table or into JSON."""

from __future__ import annotations

import json
from collections import Counter
from typing import Sequence

from .model import Finding, human_duration

COLUMNS = (
    "NAMESPACE",
    "KIND",
    "NAME",
    "DESIRED",
    "UPDATED",
    "READY",
    "AGE",
    "REASON",
    "DETAIL",
)

DETAIL_WIDTH = 72


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 3].rstrip() + "..."


def render_table(findings: Sequence[Finding], wide: bool = False) -> str:
    rows = []
    for finding in findings:
        detail = finding.message if wide else _truncate(finding.message, DETAIL_WIDTH)
        rows.append(
            [
                finding.namespace,
                finding.kind,
                finding.name,
                str(finding.desired),
                str(finding.updated),
                str(finding.ready),
                human_duration(finding.age_seconds),
                finding.reason,
                detail,
            ]
        )
    widths = [len(column) for column in COLUMNS]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    lines = [
        "  ".join(column.ljust(widths[i]) for i, column in enumerate(COLUMNS)).rstrip()
    ]
    for row in rows:
        lines.append(
            "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


def render_json(findings: Sequence[Finding]) -> str:
    return json.dumps([finding.to_dict() for finding in findings], indent=2)


def summarize(findings: Sequence[Finding]) -> str:
    """One line: how many stalled rollouts, broken down by kind."""
    by_kind = Counter(finding.kind for finding in findings)
    parts = ["%s %d" % (kind, count) for kind, count in sorted(by_kind.items())]
    return "%d stalled rollout%s (%s)" % (
        len(findings),
        "" if len(findings) == 1 else "s",
        ", ".join(parts),
    )
