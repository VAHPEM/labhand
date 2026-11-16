# -*- coding: utf-8 -*-
"""Only the main session talks to beacon: the PreToolUse hook denies every mcp__beacon__ call of a subagent.

Measured (orchestrator): a subagent's PreToolUse input carries agent_id / agent_type, and a hook deny blocks
a subagent's MCP call. The main session has no agent_id (an agent-option session carries agent_type only)."""
import json
import os

from beacon.hooks import SUBAGENT_DENIED, guard, protected_paths

from test_hooks import run_hook

BEACON_TOOLS = ("mcp__beacon__ledger_view", "mcp__beacon__attach_evidence", "mcp__beacon__set_status",
                "mcp__beacon__propose_done", "mcp__beacon__add_node", "mcp__beacon__ask")


def _decision(out):
    return json.loads(out)["hookSpecificOutput"] if out else None


def test_beacon_subagent_guard_denies_every_beacon_tool_through_the_hook(make_run):
    L = make_run()
    for tool in BEACON_TOOLS:
        rc, out, _ = run_hook("pre-tool-use", {"session_id": "s", "tool_name": tool, "tool_input": {},
                                               "agent_id": "a1b2c3", "agent_type": "general-purpose"}, run=L.dir)
        d = _decision(out)
        assert rc == 0 and d["permissionDecision"] == "deny", tool
        assert d["permissionDecisionReason"] == SUBAGENT_DENIED
    with open(os.path.join(L.dir, "denials.jsonl"), encoding="utf-8") as fh:
        assert sum(1 for _ in fh) == len(BEACON_TOOLS)          # each denial is logged


def test_beacon_subagent_guard_main_session_unaffected(make_run):
    L = make_run()
    for extra in ({}, {"agent_type": "my-agent"}, {"agent_id": ""}, {"agent_id": None}, {"agent_id": "  "}):
        rc, out, _ = run_hook("pre-tool-use", {"session_id": "s", "tool_name": "mcp__beacon__ledger_view",
                                               "tool_input": {}, **extra}, run=L.dir)
        assert rc == 0 and out == "", extra


def test_beacon_subagent_guard_other_tools_of_a_subagent_still_run(make_run):
    L = make_run()
    for tool, ti in (("Bash", {"command": "ls -la"}), ("Read", {"file_path": "/etc/hosts"}),
                     ("mcp__labhand__run_cmd", {"command": "uname -a"})):
        rc, out, _ = run_hook("pre-tool-use", {"session_id": "s", "tool_name": tool, "tool_input": ti,
                                               "agent_id": "a1", "agent_type": "Explore"}, run=L.dir)
        assert rc == 0 and out == "", tool


def test_beacon_subagent_guard_existing_rules_still_apply_to_subagents(make_run):
    L = make_run()
    rc, out, _ = run_hook("pre-tool-use", {"tool_name": "Bash", "agent_id": "a1",
                                           "tool_input": {"command": f"sqlite3 {L.path} 'update nodes'"}}, run=L.dir)
    assert _decision(out)["permissionDecision"] == "deny"


def test_beacon_subagent_guard_function(make_run):
    L = make_run()
    roots = protected_paths(L.dir)
    assert guard("mcp__beacon__set_status", {}, roots, {}, agent_id="x") == SUBAGENT_DENIED
    assert guard("mcp__beacon__set_status", {}, roots, {}) is None
    assert guard("mcp__beacon__set_status", {}, roots, {}, agent_id=None) is None
    assert guard("mcp__beaconish__x", {}, roots, {}, agent_id="x") is None    # prefix is mcp__beacon__ exactly


def test_beacon_subagent_guard_unsupervised_session_passes(make_run):
    rc, out, _ = run_hook("pre-tool-use", {"tool_name": "mcp__beacon__ledger_view", "tool_input": {},
                                           "agent_id": "a1"})
    assert rc == 0 and out == ""


def test_beacon_subagent_guard_documented():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "beacon", "WORKER.md"), encoding="utf-8") as fh:
        assert "A subagent (Agent tool) cannot call the beacon tools" in fh.read()
    with open(os.path.join(root, "beacon", "README.md"), encoding="utf-8") as fh:
        assert "agent_id" in fh.read()
