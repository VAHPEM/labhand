# -*- coding: utf-8 -*-
"""Teardown removes only what setup added; a rule, hook or server the user already had stays.

R19 F3: teardown stripped a deny rule the user had before setup whenever it equalled one of beacon's
(`Edit(~/.claude/agents/**)`, `Bash(sqlite3 *)`, ...). Setup now records what it added because it was absent
(`.claude/beacon-setup.json`, written before any wired file) and teardown removes exactly that."""
import json
import os
import sys

import pytest

from beacon import hooks
from beacon.project import RECORD, setup_project, teardown_project

SL = os.path.join(".claude", "settings.local.json")


def _proj(tmp_path):
    p = tmp_path / "proj"
    (p / ".claude").mkdir(parents=True)
    return p


def _setup(proj, **kw):
    return setup_project(str(proj), sys.executable, sys.executable, **kw)


def _put(proj, rel, obj):
    (proj / rel).write_text(json.dumps(obj, indent=2) + "\n")


def _get(proj, rel):
    p = proj / rel
    return json.loads(p.read_text()) if p.exists() else None


def _edit_mid_run(proj, rel=SL):
    """A change made during the run, so teardown cannot just restore the backup byte for byte."""
    s = _get(proj, rel)
    s["model"] = "opus"
    _put(proj, rel, s)


def _bytes(proj):
    out = {}
    for root, _, files in os.walk(proj):
        for f in files:
            p = os.path.join(root, f)
            out[os.path.relpath(p, proj)] = open(p, "rb").read()
    return out


# ---------------------------------------------------------------- R19's F3 demo, inverted
@pytest.mark.parametrize("rule", ["Edit(~/.claude/agents/**)", "Edit(**/.claude/agents/**)", "Bash(sqlite3 *)",
                                  "Edit(**/.mcp.json)", "Bash(.claude/beacon answer *)"])
def test_beacon_userrules_own_deny_rule_kept(tmp_path, rule):
    proj = _proj(tmp_path)
    _put(proj, SL, {"permissions": {"deny": [rule]}})
    _setup(proj)
    _edit_mid_run(proj)
    changes = teardown_project(str(proj))
    after = _get(proj, SL)
    assert after == {"permissions": {"deny": [rule]}, "model": "opus"}, changes
    assert f"{SL}: kept denied {rule} (it was there before setup)" in changes
    assert not (proj / RECORD).exists()


def test_beacon_userrules_beacon_added_rules_still_removed(tmp_path):
    """The other side: what setup added goes, so a precise teardown is not a loose one."""
    proj = _proj(tmp_path)
    _put(proj, SL, {"permissions": {"deny": ["Bash(sqlite3 *)"], "allow": ["Read"]}})
    _setup(proj)
    s = _get(proj, SL)
    assert "Edit(~/.claude/agents/**)" in s["permissions"]["deny"] and s["hooks"]
    _edit_mid_run(proj)
    teardown_project(str(proj))
    assert _get(proj, SL) == {"permissions": {"deny": ["Bash(sqlite3 *)"], "allow": ["Read"]}, "model": "opus"}


# ---------------------------------------------------------------- allow rules, hooks, servers, keys
def test_beacon_userrules_own_allow_rule_kept(tmp_path):
    proj = _proj(tmp_path)
    _put(proj, SL, {"permissions": {"allow": ["mcp__beacon", "mcp__beacon__ledger_view"]}})
    _setup(proj, allow_labhand=True)
    assert "mcp__labhand" in _get(proj, SL)["permissions"]["allow"]
    _edit_mid_run(proj)
    changes = teardown_project(str(proj))
    assert _get(proj, SL) == {"permissions": {"allow": ["mcp__beacon", "mcp__beacon__ledger_view"]},
                              "model": "opus"}, changes


def test_beacon_userrules_labhand_allow_setup_added_is_removed(tmp_path):
    proj = _proj(tmp_path)
    _put(proj, SL, {"permissions": {"allow": ["Read"]}})
    _setup(proj, allow_labhand=True)
    _edit_mid_run(proj)
    teardown_project(str(proj))
    assert _get(proj, SL) == {"permissions": {"allow": ["Read"]}, "model": "opus"}


def test_beacon_userrules_approvals_added_during_run_removed(tmp_path):
    """mcp__beacon__* approvals Claude Code wrote during the run are beacon's; the user's earlier one stays."""
    proj = _proj(tmp_path)
    _put(proj, SL, {"permissions": {"allow": ["mcp__beacon__ask"]}})
    _setup(proj)
    s = _get(proj, SL)
    s["permissions"]["allow"].append("mcp__beacon__set_status")
    _put(proj, SL, s)
    teardown_project(str(proj))
    assert _get(proj, SL) == {"permissions": {"allow": ["mcp__beacon__ask"]}}


def test_beacon_userrules_own_beacon_hook_restored_as_it_was(tmp_path):
    """The user ran beacon's Stop hook from somewhere else before setup: setup rewrites its command (paths
    may have moved), teardown puts the user's own hook back exactly, and keeps their other hooks."""
    proj = _proj(tmp_path)
    mine = {"type": "command", "command": "/opt/py -m beacon.hooks stop", "timeout": 99}
    other = {"type": "command", "command": "echo mine"}
    _put(proj, SL, {"hooks": {"Stop": [{"hooks": [mine]}], "PreToolUse": [{"matcher": "Bash", "hooks": [other]}]}})
    _setup(proj)
    s = _get(proj, SL)
    assert s["hooks"]["Stop"][0]["hooks"][0] != mine
    _edit_mid_run(proj)
    changes = teardown_project(str(proj))
    assert _get(proj, SL) == {"hooks": {"Stop": [{"hooks": [mine]}],
                                        "PreToolUse": [{"matcher": "Bash", "hooks": [other]}]},
                              "model": "opus"}, changes
    assert f"{SL}: restored your Stop hook as it was before setup" in changes


def test_beacon_userrules_own_identical_hook_kept(tmp_path):
    """A user's beacon hook that already equals what setup writes: setup leaves it, teardown keeps it."""
    proj = _proj(tmp_path)
    _setup(proj)
    wired = _get(proj, SL)
    stop = wired["hooks"]["Stop"]
    teardown_project(str(proj))
    _put(proj, SL, {"hooks": {"Stop": stop}})
    _setup(proj)
    _edit_mid_run(proj)
    teardown_project(str(proj))
    assert _get(proj, SL) == {"hooks": {"Stop": stop}, "model": "opus"}


def test_beacon_userrules_own_mcp_server_kept(tmp_path):
    proj = _proj(tmp_path)
    mine = {"command": "/opt/py", "args": ["-m", "beacon.mcp_server"]}
    _put(proj, ".mcp.json", {"mcpServers": {"beacon": mine, "x": {"command": "x"}}})
    _setup(proj)
    assert _get(proj, ".mcp.json")["mcpServers"]["beacon"] != mine
    m = _get(proj, ".mcp.json")
    m["mcpServers"]["y"] = {"command": "y"}
    _put(proj, ".mcp.json", m)
    teardown_project(str(proj))
    assert _get(proj, ".mcp.json") == {"mcpServers": {"beacon": mine, "x": {"command": "x"}, "y": {"command": "y"}}}


def test_beacon_userrules_own_enabled_server_and_key_kept(tmp_path):
    proj = _proj(tmp_path)
    _put(proj, SL, {"enabledMcpjsonServers": ["beacon"], "respondToBashCommands": True})
    _setup(proj, quiet_shell=True)
    assert _get(proj, SL)["respondToBashCommands"] is False
    _edit_mid_run(proj)
    teardown_project(str(proj))
    assert _get(proj, SL) == {"enabledMcpjsonServers": ["beacon"], "respondToBashCommands": True, "model": "opus"}


# ---------------------------------------------------------------- record lifecycle
def test_beacon_userrules_byte_exact_roundtrip_and_record_removed(tmp_path):
    proj = _proj(tmp_path)
    (proj / SL).write_bytes(b'{"permissions": {"deny": ["Bash(sqlite3 *)"]}}')
    (proj / ".mcp.json").write_bytes(b'{ "mcpServers" : {} }\n\n')
    before = _bytes(proj)
    _setup(proj)
    assert (proj / RECORD).exists()
    teardown_project(str(proj))
    assert _bytes(proj) == before


def test_beacon_userrules_second_setup_keeps_the_first_record(tmp_path):
    """A re-run of setup over a wired project must not take beacon's rules for the user's."""
    proj = _proj(tmp_path)
    _put(proj, SL, {"permissions": {"deny": ["Bash(sqlite3 *)"]}})
    _setup(proj)
    first = json.loads((proj / RECORD).read_text())
    _setup(proj)
    assert json.loads((proj / RECORD).read_text()) == first
    _edit_mid_run(proj)
    teardown_project(str(proj))
    assert _get(proj, SL) == {"permissions": {"deny": ["Bash(sqlite3 *)"]}, "model": "opus"}


def test_beacon_userrules_interrupted_setup_record_written_first(tmp_path, monkeypatch):
    """Setup stops after writing the record and before the wired files: teardown then has a record that
    covers everything, removes nothing of the user's and leaves no record behind."""
    import beacon.project as bp
    proj = _proj(tmp_path)
    _put(proj, SL, {"permissions": {"deny": ["Edit(~/.claude/agents/**)"]}})
    real = bp._write
    calls = []

    def boom(path, *a, **kw):
        calls.append(path)
        if not path.endswith("beacon-setup.json"):
            raise OSError("disk full")
        return real(path, *a, **kw)

    monkeypatch.setattr(bp, "_write", boom)
    with pytest.raises(OSError):
        _setup(proj)
    assert calls[0].endswith("beacon-setup.json") and (proj / RECORD).exists()
    monkeypatch.setattr(bp, "_write", real)
    teardown_project(str(proj))
    assert _get(proj, SL) == {"permissions": {"deny": ["Edit(~/.claude/agents/**)"]}}
    assert not (proj / RECORD).exists()


def test_beacon_userrules_interrupted_setup_then_rerun(tmp_path, monkeypatch):
    """An interrupted setup is run again, then torn down: the user's rule stays."""
    import beacon.project as bp
    proj = _proj(tmp_path)
    _put(proj, SL, {"permissions": {"deny": ["Bash(sqlite3 *)"]}})
    real = bp._write

    def boom(path, *a, **kw):
        if path.endswith(".mcp.json"):
            raise OSError("disk full")
        return real(path, *a, **kw)

    monkeypatch.setattr(bp, "_write", boom)
    with pytest.raises(OSError):
        _setup(proj)
    monkeypatch.setattr(bp, "_write", real)
    _setup(proj)
    _edit_mid_run(proj)
    teardown_project(str(proj))
    assert _get(proj, SL) == {"permissions": {"deny": ["Bash(sqlite3 *)"]}, "model": "opus"}


def test_beacon_userrules_interrupted_teardown_stale_record(tmp_path, monkeypatch):
    """Teardown stripped the files but stopped before removing the record; the user then adds a rule equal
    to one of beacon's and sets up again: the stale record must not claim it."""
    import beacon.project as bp
    proj = _proj(tmp_path)
    _setup(proj)
    real_remove = os.remove

    def no_record_removal(p, *a, **kw):
        if str(p).endswith("beacon-setup.json"):
            raise OSError(13, "Permission denied")
        return real_remove(p, *a, **kw)

    monkeypatch.setattr(bp.os, "remove", no_record_removal)
    changes = teardown_project(str(proj))
    monkeypatch.setattr(bp.os, "remove", real_remove)
    assert any("kept setup's record" in c for c in changes) and (proj / RECORD).exists()
    _put(proj, SL, {"permissions": {"deny": ["Bash(sqlite3 *)"]}})
    _setup(proj)
    _edit_mid_run(proj)
    teardown_project(str(proj))
    assert _get(proj, SL) == {"permissions": {"deny": ["Bash(sqlite3 *)"]}, "model": "opus"}


def test_beacon_userrules_legacy_wiring_without_record(tmp_path):
    """A project wired by a setup older than the record (no record, its .beacon-bak there): setup infers
    what was the user's from that backup, so the user's rule still stays at teardown."""
    proj = _proj(tmp_path)
    _put(proj, SL, {"permissions": {"deny": ["Bash(sqlite3 *)"]}})
    _setup(proj)
    os.remove(proj / RECORD)                  # what an older setup left: wired file + backup, no record
    assert (proj / (SL + ".beacon-bak")).exists()
    _setup(proj)
    _edit_mid_run(proj)
    teardown_project(str(proj))
    assert _get(proj, SL) == {"permissions": {"deny": ["Bash(sqlite3 *)"]}, "model": "opus"}


def test_beacon_userrules_legacy_created_file_without_record_is_torn_down(tmp_path):
    """An older setup created the files (no backup, no record): a re-run of setup recognises them as
    beacon's and teardown removes them, leaving nothing behind."""
    proj = _proj(tmp_path)
    _setup(proj)
    os.remove(proj / RECORD)
    _setup(proj)
    teardown_project(str(proj))
    assert not (proj / SL).exists() and not (proj / ".mcp.json").exists() and not (proj / RECORD).exists()


def test_beacon_userrules_broken_record_is_an_error(tmp_path):
    proj = _proj(tmp_path)
    _setup(proj)
    (proj / RECORD).write_text('{"something": "else"}\n')
    with pytest.raises(ValueError, match="not beacon setup's record"):
        teardown_project(str(proj))


def test_beacon_userrules_record_is_guarded(tmp_path):
    """Workers must not rewrite the record (it decides what teardown removes)."""
    rec = str(tmp_path / ".claude" / "beacon-setup.json")
    assert hooks.guard("Write", {"file_path": rec}, [], {}) is not None
    assert hooks.guard("Edit", {"file_path": rec}, [], {}) is not None
    assert hooks.guard("Bash", {"command": f"echo '{{}}' > {rec}"}, [], {}) is not None
    from beacon.project import PROTECT_DENY
    assert "Edit(**/.claude/beacon-setup.json)" in PROTECT_DENY
