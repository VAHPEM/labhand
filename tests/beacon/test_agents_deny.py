# -*- coding: utf-8 -*-
"""Agent definitions are off-limits during a run: one can carry `permissionMode: bypassPermissions`.

setup-project adds deny rules for the project's .claude/agents and the user's ~/.claude/agents (teardown
removes them again), and the PreToolUse guard refuses writes there through Write/Edit, Bash and other MCP
servers' tools."""
import json
import os
import sys

from beacon.hooks import AGENTS_DENIED, guard, protected_paths
from beacon.project import PROTECT_DENY, setup_project, teardown_project

from test_hooks import run_hook

RULES = ("Edit(**/.claude/agents/**)", "Edit(~/.claude/agents/**)")


def test_beacon_agents_deny_rules_in_protect_deny():
    for r in RULES:
        assert r in PROTECT_DENY


def test_beacon_agents_deny_setup_adds_and_teardown_removes(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    changes = setup_project(str(proj), sys.executable, sys.executable)
    deny = json.loads((proj / ".claude" / "settings.local.json").read_text())["permissions"]["deny"]
    for r in RULES:
        assert r in deny and f"denied {r}" in "\n".join(changes)
    teardown_project(str(proj))
    assert not (proj / ".claude" / "settings.local.json").exists()


def test_beacon_agents_deny_teardown_keeps_users_own_deny_rules(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.local.json").write_text(json.dumps({"permissions": {"deny": ["Bash(rm *)"]}}))
    setup_project(str(proj), sys.executable, sys.executable)
    s = json.loads((proj / ".claude" / "settings.local.json").read_text())
    assert all(r in s["permissions"]["deny"] for r in RULES)
    s["model"] = "opus"                                            # an edit during the run: no restore
    (proj / ".claude" / "settings.local.json").write_text(json.dumps(s))
    teardown_project(str(proj))
    after = json.loads((proj / ".claude" / "settings.local.json").read_text())
    assert after["permissions"]["deny"] == ["Bash(rm *)"] and after["model"] == "opus"


def test_beacon_agents_deny_guard_write_tools(make_run, tmp_path):
    L = make_run()
    roots = protected_paths(L.dir)
    home = os.path.expanduser("~")
    for p in (str(tmp_path / "proj" / ".claude" / "agents" / "evil.md"),
              os.path.join(home, ".claude", "agents", "evil.md"),
              "~/.claude/agents/evil.md",
              str(tmp_path / "proj" / ".Claude" / "Agents" / "x.md"),            # case-insensitive FS
              str(tmp_path / "proj" / ".claude" / "agents" / "sub" / "x.md")):
        for tool in ("Write", "Edit", "MultiEdit"):
            assert guard(tool, {"file_path": p}, roots, {}) == AGENTS_DENIED, (tool, p)
    assert guard("Write", {"file_path": str(tmp_path / "proj" / "agents" / "x.md")}, roots, {}) is None
    assert guard("Write", {"file_path": str(tmp_path / "proj" / ".claude" / "agents-notes.md")}, roots, {}) is None


def test_beacon_agents_deny_guard_symlink_into_agents(make_run, tmp_path):
    L = make_run()
    agents = tmp_path / "proj" / ".claude" / "agents"
    agents.mkdir(parents=True)
    os.symlink(agents, tmp_path / "innocent")
    assert guard("Write", {"file_path": str(tmp_path / "innocent" / "x.md")}, protected_paths(L.dir), {}) \
        == AGENTS_DENIED


def test_beacon_agents_deny_guard_bash_and_other_mcp(make_run):
    L = make_run()
    roots = protected_paths(L.dir)
    assert guard("Bash", {"command": "cat > .claude/agents/x.md <<'E'\npermissionMode: bypassPermissions\nE"},
                 roots, {}) == AGENTS_DENIED
    assert guard("Bash", {"command": "cp x.md ~/.Claude/Agents/"}, roots, {}) == AGENTS_DENIED
    assert guard("mcp__fs__write_file", {"path": "/p/.claude/agents/x.md", "content": "x"}, roots, {}) \
        == AGENTS_DENIED
    assert guard("Bash", {"command": "ls agents/"}, roots, {}) is None
    assert guard("mcp__beacon__attach_evidence", {"content": "see .claude/agents"}, roots, {}) is None


def test_beacon_agents_deny_through_the_hook(make_run):
    L = make_run()
    rc, out, _ = run_hook("pre-tool-use", {"tool_name": "Write", "tool_input": {
        "file_path": "/tmp/p/.claude/agents/a.md", "content": "permissionMode: bypassPermissions"}}, run=L.dir)
    d = json.loads(out)["hookSpecificOutput"]
    assert rc == 0 and d["permissionDecision"] == "deny" and d["permissionDecisionReason"] == AGENTS_DENIED
