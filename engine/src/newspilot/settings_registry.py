# SPDX-License-Identifier: AGPL-3.0-only
"""Definitions of the settings a site owner can change: type, default, label and help text.

The dashboard (and `newspilot setting list`) shows these to the site owner. Values are stored per tenant in
tenant_settings (see newspilot.settings). The engine itself imposes no publishing or spending limits. Where a limit
makes sense, it is a setting here, off by default, with help text so the owner can make an informed choice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

KINDS = ("int", "bool", "text", "int_map")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")


class InvalidSettingError(ValueError):
    """A value does not fit its setting's definition."""


@dataclass(frozen=True)
class SettingDef:
    key: str
    kind: str
    default: Any
    label: str
    help: str
    group: str
    minimum: int | None = None
    maximum: int | None = None

    def coerce(self, value: Any) -> Any:
        """Validate a value (also accepts strings, as typed on the command line). None means "not set"."""
        if value is None or (isinstance(value, str) and value.strip().lower() in ("", "none", "off", "unlimited")):
            return None
        if self.kind == "int":
            return self._int(value)
        if self.kind == "bool":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in ("1", "true", "yes", "on"):
                return True
            if text in ("0", "false", "no"):
                return False
            raise InvalidSettingError(f"{self.key}: expected yes or no, got {value!r}")
        if self.kind == "text":
            return str(value)
        if self.kind == "int_map":
            if isinstance(value, str):
                value = _parse_map(self.key, value)
            if not isinstance(value, dict):
                raise InvalidSettingError(f"{self.key}: expected category=number pairs")
            out = {}
            for slug, n in value.items():
                if not isinstance(slug, str) or not _SLUG.match(slug):
                    raise InvalidSettingError(f"{self.key}: {slug!r} is not a category slug")
                out[slug] = self._int(n)
            return out
        raise InvalidSettingError(f"{self.key}: unknown kind {self.kind!r}")

    def _int(self, value: Any) -> int:
        if isinstance(value, bool):
            raise InvalidSettingError(f"{self.key}: expected a whole number")
        try:
            n = int(str(value).strip()) if not isinstance(value, int) else value
        except ValueError as e:
            raise InvalidSettingError(f"{self.key}: expected a whole number, got {value!r}") from e
        if self.minimum is not None and n < self.minimum:
            raise InvalidSettingError(f"{self.key}: must be at least {self.minimum}")
        if self.maximum is not None and n > self.maximum:
            raise InvalidSettingError(f"{self.key}: must be at most {self.maximum}")
        return n


def _parse_map(key: str, text: str) -> dict[str, str]:
    """'results=10, analysis=2' -> {'results': '10', 'analysis': '2'}"""
    out = {}
    for part in filter(None, (p.strip() for p in text.split(","))):
        slug, sep, n = part.partition("=")
        if not sep:
            raise InvalidSettingError(f"{key}: write category=number pairs, separated by commas")
        out[slug.strip()] = n.strip()
    return out


GROUPS: dict[str, tuple[str, str]] = {
    "publishing": (
        "Publishing pace",
        "NewsPilot AI applies no publishing limit unless you set one here. Every limit below is off by default.\n\n"
        "Google does not publish a safe number of articles per day. Its spam policies do treat 'scaled content "
        "abuse' as spam: generating many pages mainly to manipulate search rankings, without adding value for "
        "readers. The policy applies whether pages are written by people, by AI or by both, and affected sites "
        "can lose rankings or receive a manual action.\n\n"
        "Fewer articles that each add something new (context, analysis, data, original reporting) are safer than "
        "volume. A sensible approach is to start a new site slowly and raise a limit only while Search Console "
        "shows your pages being indexed and no manual actions. These limits apply to new articles only. "
        "Corrections and updates to articles already live are never delayed.",
    ),
}

_DEFS = [
    SettingDef(
        key="publish.daily_limit",
        kind="int",
        default=None,
        minimum=1,
        group="publishing",
        label="New articles per day (whole site)",
        help="The most new articles to publish in any 24 hours across the whole site. Empty means no limit. "
        "When the limit is reached, approved articles wait and go out as soon as it allows.",
    ),
    SettingDef(
        key="publish.category_daily_limits",
        kind="int_map",
        default={},
        minimum=1,
        group="publishing",
        label="New articles per day, by category",
        help="Limits for individual categories, as category=number pairs (for example: results=6, analysis=2). "
        "Categories not listed have no limit of their own. The site-wide limit still applies.",
    ),
    SettingDef(
        key="publish.source_daily_limit",
        kind="int",
        default=None,
        minimum=1,
        group="publishing",
        label="New articles per day from one source outlet",
        help="The most new articles in any 24 hours whose lead came from the same outlet (website). Empty means "
        "no limit. Leaning heavily on one outlet's reporting is the pattern that looks most like republishing.",
    ),
    SettingDef(
        key="publish.min_gap_minutes",
        kind="int",
        default=None,
        minimum=0,
        maximum=24 * 60,
        group="publishing",
        label="Minutes between new articles",
        help="The shortest time between two new articles going live. Empty means no gap. Spacing articles out "
        "avoids bursts of many pages at once.",
    ),
]

REGISTRY: dict[str, SettingDef] = {d.key: d for d in _DEFS}


def definition(key: str) -> SettingDef | None:
    return REGISTRY.get(key)


def by_group() -> dict[str, list[SettingDef]]:
    out: dict[str, list[SettingDef]] = {}
    for d in _DEFS:
        out.setdefault(d.group, []).append(d)
    return out
