# -*- coding: utf-8 -*-
"""Record-less setup (the record deleted by hand, a lost write) recognises a settings file an earlier setup
created even when it also holds the mcp__labhand rule `--allow-labhand` adds: beacon's hooks get recorded
again and teardown removes them (and the rule, which that setup added too)."""
import json
import os
import sys

import pytest

from beacon import project as _project
from beacon.project import RECORD, teardown_project

SL = os.path.join(".claude", "settings.local.json")
MCP = ".mcp.json"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.delenv("LABHAND_MAC_MCP_COMMAND", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _proj(tmp_path):
    p = tmp_path / "proj"
    (p / ".claude").mkdir(parents=True)
    return p


def _setup(proj, **kw):
    return _project.setup_project(str(proj), sys.executable, sys.executable, **kw)


def _put(proj, rel, obj):
    (proj / rel).write_text(json.dumps(obj, indent=2) + "\n")


def _get(proj, rel):
    p = proj / rel
    return json.loads(p.read_text()) if p.exists() else None


def _beacon_hooks_left(proj):
    s = _get(proj, SL) or {}
    return "beacon.hooks" in json.dumps(s.get("hooks", {}))


@pytest.mark.parametrize("allow_mac", [False, True])
def test_legacy_labhand_created_file_torn_down(tmp_path, allow_mac):
    proj = _proj(tmp_path)
    _setup(proj, allow_labhand=True, allow_mac=allow_mac)
    os.remove(proj / RECORD)
    _setup(proj, allow_labhand=True, allow_mac=allow_mac)
    rec = _get(proj, RECORD)["files"]["settings.local.json"]
    assert rec["hooks"] and "mcp__labhand" in rec["allow"]
    teardown_project(str(proj))
    assert not (proj / SL).exists() and not (proj / MCP).exists() and not (proj / RECORD).exists()


def test_legacy_labhand_rerun_without_labhand_flag_torn_down(tmp_path):
    # the earlier setup had --allow-labhand, the record-less re-run does not: the rule is still setup's
    proj = _proj(tmp_path)
    _setup(proj, allow_labhand=True)
    os.remove(proj / RECORD)
    _setup(proj)
    teardown_project(str(proj))
    assert not (proj / SL).exists() and not (proj / RECORD).exists()


def test_legacy_labhand_user_content_not_taken(tmp_path):
    # no backup, but the file holds something besides beacon's parts and the rule: nothing proves the file
    # is setup's, so the user's content and the mcp__labhand rule stay (as before)
    proj = _proj(tmp_path)
    _setup(proj, allow_labhand=True)
    s = _get(proj, SL)
    s["model"] = "opus"
    _put(proj, SL, s)
    os.remove(proj / RECORD)
    _setup(proj, allow_labhand=True)
    teardown_project(str(proj))
    s = _get(proj, SL)
    assert s["model"] == "opus" and "mcp__labhand" in s["permissions"]["allow"]


def test_legacy_labhand_user_prior_rule_with_backup_stays(tmp_path):
    # the user had mcp__labhand before setup (setup's .beacon-bak shows it): it stays theirs at teardown
    proj = _proj(tmp_path)
    mine = {"permissions": {"allow": ["mcp__labhand"]}}
    _put(proj, SL, mine)
    _setup(proj, allow_labhand=True)
    os.remove(proj / RECORD)
    _setup(proj, allow_labhand=True)
    assert _beacon_hooks_left(proj)
    teardown_project(str(proj))
    assert _get(proj, SL) == mine and not _beacon_hooks_left(proj)
