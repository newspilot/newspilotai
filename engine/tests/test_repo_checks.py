# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the repository's own checks (scripts/check_neutral.py) and the CLI's offline commands."""

from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

from newspilot.cli import main

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("check_neutral", ROOT / "scripts" / "check_neutral.py")
check_neutral = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_neutral)


def test_neutral_check_flags_subject_terms(tmp_path):
    bad = tmp_path / "prompt.txt"
    bad.write_text("You are a CRICKET reporter.\n")  # neutral: ok (the check must catch this word)
    problems = check_neutral.scan(tmp_path)
    assert len(problems) == 1 and "'CRICKET'" in problems[0]  # neutral: ok


def test_neutral_check_respects_marker_and_word_boundaries(tmp_path):
    (tmp_path / "ok.md").write_text("Example sections: Cricket, Politics  <!-- neutral: ok -->\nfootballer\n")
    assert check_neutral.scan(tmp_path) == []


def test_engine_core_is_neutral():
    assert check_neutral.scan(ROOT / "engine" / "src") == []


def test_cli_new_master_key(capsys):
    assert main(["secret", "new-master-key"]) == 0
    key = capsys.readouterr().out.strip()
    assert len(base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))) == 32


def test_cli_settings_list_shows_help_and_no_limits_by_default():
    from newspilot import settings_registry
    from newspilot.cli import render_settings
    from newspilot.settings import SettingView

    out = render_settings([SettingView(d, d.default, False) for d in settings_registry.REGISTRY.values()])
    assert "PUBLISHING PACE" in out and "Google" in out
    assert out.count("current: not set (no limit)") == len(settings_registry.by_group()["publishing"])
