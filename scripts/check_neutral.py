# SPDX-License-Identifier: AGPL-3.0-only
"""Fail if the core engine mentions a specific subject, market or product category.

The engine has to work for any publisher. Subject-specific behaviour belongs in site profiles and plugins,
which live outside the core. This check catches the obvious cases, such as code or prompts copied from a
site built for one subject. It is a tripwire, not a full review.

If a line legitimately needs one of these words (for example in a generic list of example sections in the
docs), add the marker "neutral: ok" in a comment on that line.

Usage: python scripts/check_neutral.py [path ...]   (default: engine/)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Subject terms that should never appear in the core. Keep each to a clear, unambiguous word.
DENYLIST = [
    # sport
    "cricket", "ipl", "icc", "bcci", "odi", "t20", "football", "soccer", "fifa", "nba", "nfl", "rugby",
    "tennis", "olympics", "batsman", "wicket",
    # consumer technology
    "smartphone", "gadget", "chipset", "snapdragon", "unboxing", "spec sheet",
    # one market
    "price in india",
]
PATTERN = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in DENYLIST) + r")\b", re.IGNORECASE)
SUFFIXES = {".py", ".sql", ".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".jinja", ".j2", ".html"}
SKIP_DIRS = {".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", "build", "dist"}
MARKER = "neutral: ok"


def scan(root: Path) -> list[str]:
    problems = []
    files = [root] if root.is_file() else sorted(root.rglob("*"))
    for path in files:
        if not path.is_file() or path.suffix not in SUFFIXES or SKIP_DIRS & set(path.parts):
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if MARKER in line:
                continue
            for match in PATTERN.finditer(line):
                problems.append(f"{path}:{lineno}: subject-specific term {match.group(0)!r}")
    return problems


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv] or [Path("engine")]
    problems = [p for root in roots if root.exists() for p in scan(root)]
    for p in problems:
        print(p)
    if problems:
        print(f"\n{len(problems)} subject-specific term(s) in the core. Move this behaviour to a site profile or "
              "plugin, or mark a legitimate line with 'neutral: ok'.")
        return 1
    print("check_neutral: the core is subject-neutral.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
