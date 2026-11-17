# -*- coding: utf-8 -*-
"""The agent-definition guard is precise (R19 F1/F2, inverted).

A path is an agent definition only when a whole `.claude` component is followed by a whole `agents`
component, after `~`, `abspath` and resolving the symlinks of its existing prefix; only writes are refused
(Bash: the write forms beacon recognises; other MCP servers: tools not named like a read)."""
import json
import os

import pytest

from beacon.hooks import (AGENTS_DENIED, bash_writes_agent_definition, guard, is_agent_definition,
                          mcp_writes_agent_definition)

from test_hooks import run_hook

ROOTS = ["/nonexistent-run", "/nonexistent-pkg"]
CA = ".claude/" + "agents"


@pytest.fixture
def proj(tmp_path):
    """A project with .claude/agents, a symlink `ag` -> it and `cl` -> .claude."""
    p = tmp_path / "proj"
    (p / ".claude" / "agents").mkdir(parents=True)
    os.symlink(p / ".claude" / "agents", p / "ag")
    os.symlink(p / ".claude", p / "cl")
    return p


def G(tool, ti, cwd):
    return guard(tool, ti, ROOTS, {}, cwd=str(cwd))


def bash(cmd, cwd):
    return G("Bash", {"command": cmd}, cwd)


# ---- F1 inverted: unrelated names and reads pass
@pytest.mark.parametrize("cmd", [
    f"cat {CA}-notes.md", f"echo hi >> {CA}-notes.md", f"ls ~/{CA}backup", "touch ~/.claude/agentsX/y",
    "echo x > .claude/agents.md", "ls agents/", "echo x > agents/y", "echo x > my.claude/agents/y",
])
def test_beacon_agents_precise_unrelated_names_pass_bash(cmd, proj):
    assert bash(cmd, proj) is None


def test_beacon_agents_precise_unrelated_names_pass_mcp(proj):
    assert G("mcp__fs__write_file", {"path": f"/p/{CA}-notes.md", "content": "x"}, proj) is None
    assert G("mcp__fs__write_file", {"path": "~/.claude/agentsX", "content": "x"}, proj) is None
    assert G("Write", {"file_path": f"/p/{CA}-notes.md"}, proj) is None


@pytest.mark.parametrize("cmd", [
    f"cat {CA}/reviewer.md", "cp .claude/agents/x.md /tmp/y", "grep -r foo ~/.claude/agents",
    "sed -n 1p .claude/agents/a.md", "cat .claude/agents/a.md | tee /tmp/copy", "head -5 < .claude/agents/a.md",
    "tar xf ~/.claude/agents/a.tar -C /tmp", "python3 -c \"print(open('.claude/agents/x.md').read())\"",
    "cat > notes.md <<'E'\nDon't touch .claude/agents\nE", "find .claude/agents -name '*.md'", "ls -la ag/", "find ~/.claude -name '*.md' -exec grep -l x {} +", "cd .claude/agents && git log",
    "git add .claude/agents/x.md", "cd ~/.claude/agents && curl -s http://x", "bash <<E\ncat .claude/agents/x.md > /tmp/y\nE",
])
def test_beacon_agents_precise_reads_allowed_bash(cmd, proj):
    assert bash(cmd, proj) is None


def test_beacon_agents_precise_reads_allowed_mcp(proj):
    for tool in ("mcp__fs__read_file", "mcp__fs__read_text_file", "mcp__fs__get_file_info", "mcp__fs__list_directory",
                 "mcp__dc__readFile", "mcp__dc__read_multiple_files", "mcp__fs__search_files"):
        assert G(tool, {"path": f"/p/{CA}/reviewer.md"}, proj) is None, tool
        assert G(tool, {"path": str(proj / "ag" / "x.md")}, proj) is None, tool


# ---- F2 inverted: symlinks, ./ and // segments, routes without the literal string
@pytest.mark.parametrize("cmd", [
    "cat > .claude/./agents/evil.md", "cat > .claude//agents/evil.md", "cd .claude && cat > agents/evil.md",
    "cat > cl/agents/evil.md", "echo x > ag/evil.md", "cat > ~/.claude/'agents'/evil.md",
    "cat > ~/.claude/agent?/evil.md", "cat > .claude/[a]gents/evil.md", "D=.claude/agents; echo x > $D/e.md",
    "echo x > ${HOME}/.claude/{agents,b}/e.md", "echo x > $UNKNOWN/agents/e.md", "cd ~/.claude; echo > agents/x",
    "echo x 2>/dev/null > .Claude/Agents/x", "bash -c 'echo x > .claude/agents/e.md'", "x=$(cat > .claude/agents/e.md)",
    "echo `echo > .claude/agents/e`", "eval 'echo > .claude/agents/x'", "sudo -u root tee .claude/agents/e.md",
    "# comment\ncat > .claude/agents/y", "echo \"<<X\"\ncat > .claude/agents/y", "echo 'unbalanced > .claude/agents/y",
])
def test_beacon_agents_precise_bash_routes_refused(cmd, proj):
    assert bash(cmd, proj) == AGENTS_DENIED, cmd


def test_beacon_agents_precise_symlinked_and_dot_paths_mcp(proj):
    link = str(proj / "ag" / "evil.md")
    assert G("mcp__fs__write_file", {"path": link, "content": "---\npermissionMode: bypassPermissions\n---"},
             proj) == AGENTS_DENIED
    assert G("mcp__fs__write_file", {"path": str(proj) + "/.claude/./agents/evil.md"}, proj) == AGENTS_DENIED
    assert G("mcp__fs__write_file", {"path": str(proj) + "/cl/agents/evil.md"}, proj) == AGENTS_DENIED
    assert G("mcp__fs__write_file", {"path": "cl/agents/evil.md"}, proj) == AGENTS_DENIED         # relative to cwd
    assert G("Write", {"file_path": link}, proj) == AGENTS_DENIED
    assert G("Write", {"file_path": str(proj) + "/x/../.claude/agents/e.md"}, proj) == AGENTS_DENIED
    # `..` folded: leaving the directory is not inside it
    assert G("Write", {"file_path": str(proj) + "/.claude/agents/../notes.md"}, proj) is None


# ---- the write forms
@pytest.mark.parametrize("cmd", [
    "echo x | tee -a .claude/agents/a.md", "cp /tmp/x ~/.claude/agents/", "cp -t .claude/agents /tmp/x",
    "mv .claude/agents/a.md /tmp/", "mv /tmp/x .claude/agents/", "ln -s /tmp/x .claude/agents/y",
    "install -m 644 x .claude/agents/y", "rm .claude/agents/a.md", "touch .claude/agents/new.md",
    "mkdir -p ~/.claude/agents", "echo > .claude/agents", "sed -i '' s/a/b/ .claude/agents/a.md",
    "perl -pi -e s/a/b/ .claude/agents/a.md", "vim -c wq .claude/agents/a.md", "nano .claude/agents/a.md",
    "python3 -c \"open('.claude/agents/x.md','w').write('x')\"",
    "python3 -c \"import pathlib; pathlib.Path.home().joinpath('.claude','agents','x.md').write_text('x')\"",
    "node -e \"require('fs').writeFileSync('.claude/agents/x.md','x')\"",
    "python3 - <<'E'\nopen('.claude/agents/x.md','w').write('x')\nE", "bash <<E\necho > .claude/agents/q\nE",
    "dd if=/tmp/x of=.claude/agents/x", "curl -o ~/.claude/agents/x.md http://x", "wget -P ~/.claude/agents http://x",
    "cd ~/.claude/agents && curl -O http://x/a.md", "cp -r /tmp/evil .claude", "mv /tmp/evil .claude",
    "unzip a.zip -d .claude/agents", "tar -xf a.tar -C ~/.claude/agents", "echo 'a' >| .claude/agents/x",
    "git checkout -- .claude/agents/x.md", "for f in .claude/agents/*; do rm \"$f\"; done", "find .claude/agents -name '*.md' -delete", "find ~/.claude -name '*.md' -exec rm {} +", "cd .claude/agents && wget http://x/a.md",
    "cd .claude/agents && tar xf /tmp/a.tar", "xargs rm .claude/agents/a.md",
])
def test_beacon_agents_precise_writes_refused(cmd, proj):
    assert bash(cmd, proj) == AGENTS_DENIED, cmd


def test_beacon_agents_precise_mcp_write_tools(proj):
    for tool, ti in (("mcp__fs__edit_file", {"path": "~/.claude/agents/x.md", "edits": []}),
                     ("mcp__fs__create_directory", {"path": str(proj / ".claude" / "agents" / "sub")}),
                     ("mcp__dc__move_file", {"source": "/tmp/x", "destination": str(proj / CA / "x.md")}),
                     ("mcp__dc__writeFile", {"path": "file:///p/.claude/agents/x"}),
                     ("mcp__dc__start_process", {"command": "echo x > ~/.claude/agents/x.md"}),
                     ("mcp__labhand__download_file", {"path": "x", "dest": "~/.claude/agents/x.md"}),
                     ("mcp__some__unknown_tool", {"target": {"files": [str(proj / "ag" / "x.md")]}})):
        assert G(tool, ti, proj) == AGENTS_DENIED, tool
    # a write tool whose content merely mentions the directory, and a remote command that only reads
    assert G("mcp__fs__write_file", {"path": "/tmp/notes.md", "content": "see .claude/agents\nfor x"}, proj) is None
    assert G("mcp__dc__start_process", {"command": "cat ~/.claude/agents/x.md"}, proj) is None
    assert G("mcp__dc__start_process", {"command": "cat ~/.claude/agents/x.md > /tmp/y"}, proj) is None
    assert G("mcp__dc__interact_with_process", {"input": "open('.claude/agents/x.md','w').write('x')"}, proj) \
        == AGENTS_DENIED


def test_beacon_agents_precise_component_rule():
    home = os.path.expanduser("~")
    assert is_agent_definition("~/.claude/agents")
    assert is_agent_definition(os.path.join(home, ".claude", "agents", "a", "b.md"))
    assert is_agent_definition("/p/.CLAUDE/AGENTS/x")
    for p in ("/p/.claude/agents-notes.md", "/p/.claude/agentsX/y", "/p/x.claude/agents/y", "/p/.claude/x/agents/y",
              "/p/agents/.claude", "/p/.claude", "", "/p/.claude/agents\n/x"):
        assert not is_agent_definition(p), p


def test_beacon_agents_precise_unknown_variables_are_one_component(tmp_path):
    # an unknown variable stands for one unknown component; two unknowns side by side are not a match
    assert bash_writes_agent_definition("echo x > $X/agents/e.md", str(tmp_path))
    assert bash_writes_agent_definition("echo x > .claude/$Y/e.md", str(tmp_path))
    assert not bash_writes_agent_definition('echo "$A" > "$OUT/$F"', str(tmp_path))
    assert not bash_writes_agent_definition("cd $SOMEWHERE && echo > out.txt", str(tmp_path))
    assert bash_writes_agent_definition("cd $SOMEWHERE && echo > agents/x", str(tmp_path))


def test_beacon_agents_precise_mcp_read_names(tmp_path):
    p = str(tmp_path / ".claude" / "agents" / "x.md")
    assert not mcp_writes_agent_definition("mcp__fs__read_file", {"path": p})
    assert not mcp_writes_agent_definition("mcp__x__getFileInfo", {"path": p})
    assert mcp_writes_agent_definition("mcp__fs__write_file", {"path": p})


def test_beacon_agents_precise_hook_uses_session_cwd(proj, make_run):
    """The PreToolUse input's `cwd` resolves a relative path (`cd`-free `cat > agents/x` inside .claude)."""
    rc, out, _ = run_hook("pre-tool-use", {"tool_name": "Bash", "cwd": str(proj / ".claude"),
                                           "tool_input": {"command": "cat > agents/x.md"}}, run=make_run().dir)
    assert rc == 0 and json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] == AGENTS_DENIED
    rc, out, _ = run_hook("pre-tool-use", {"tool_name": "Bash", "cwd": str(proj),
                                           "tool_input": {"command": "cat > agents/x.md"}}, run=make_run().dir)
    assert rc == 0 and not out.strip()

