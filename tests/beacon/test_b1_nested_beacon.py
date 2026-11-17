"""B1: a worker must not be able to launch a nested supervised Claude Code (`beacon claude` / `watch` /
`demo` / `eval`), and the human-only list has one source of truth (hooks.HUMAN_COMMANDS)."""
import io
import sys

import pytest

from beacon import cli, hooks, project
from beacon.hooks import guard

# What the spec requires, written out independently of the constant under test.
EXPECTED_HUMAN = ("answer", "verify", "set-verify", "reopen", "config", "lighthouse", "init",
                  "setup-project", "teardown-project", "claude", "watch", "demo", "eval")
EXPECTED_WORKER = ("status", "report", "graph", "check", "hooks-config", "log", "transcript",
                   "calibrate-refusals", "doctor")


def _refused(reason):
    return reason is not None and "for the human" in reason


def _subcommands():
    ap = cli.build_parser()
    sub = next(a for a in ap._actions if a.__class__.__name__ == "_SubParsersAction")
    return set(sub.choices)


NESTED = [
    ".claude/beacon claude",
    ".claude/beacon claude --run /r --model opus",
    "./.claude/beacon watch --cwd .",
    "python3 -m beacon watch --cwd .",
    "python3 -m beacon.cli watch --cwd . --run /r",
    "python3 -m beacon.__main__ claude",
    "PYTHONPATH=/x python3 -m beacon --run /r claude",
    "python3 -m beacon --run=/r watch --cwd .",
    "cd /p && .claude/beacon demo /tmp/d --auto",
    "nohup python3 -m beacon eval suite.json --out /tmp/e &",
    "beacon watch --cwd /p",
    "sh -c '.claude/beacon claude'",
]


@pytest.mark.parametrize("cmd", NESTED)
def test_b1_nested_beacon_guard_refuses_session_starters(cmd):
    assert hooks.HUMAN_CLI.search(cmd), cmd
    assert _refused(guard("Bash", {"command": cmd}, [], {}))


@pytest.mark.parametrize("c", EXPECTED_HUMAN)
def test_b1_nested_beacon_guard_refuses_every_human_command(c):
    for cmd in (f".claude/beacon {c} x", f"python3 -m beacon {c}", f"python3 -m beacon.cli {c} --run /r"):
        assert _refused(guard("Bash", {"command": cmd}, [], {})), cmd


@pytest.mark.parametrize("c", EXPECTED_WORKER)
def test_b1_nested_beacon_guard_allows_worker_commands(c):
    for cmd in (f".claude/beacon {c}", f"python3 -m beacon {c} --run /r", f"python3 -m beacon.cli {c}"):
        assert guard("Bash", {"command": cmd}, [], {}) is None, cmd


@pytest.mark.parametrize("cmd", [
    "grep -rn watch beacon/", "pytest tests/beacon -k eval", "ls beacon/demo.py", "cat beacon/watchdog.py",
    "python3 -m pytest tests/beacon/test_demo.py", "git diff -- beacon/cli.py",
])
def test_b1_nested_beacon_guard_allows_ordinary_work(cmd):
    assert guard("Bash", {"command": cmd}, [], {}) is None, cmd


def test_b1_nested_beacon_every_subcommand_is_classified_by_the_single_constant():
    subs = _subcommands()
    human, worker = set(hooks.HUMAN_COMMANDS), set(hooks.WORKER_COMMANDS)
    assert not human & worker
    assert subs == human | worker, (subs - human - worker, (human | worker) - subs)
    assert human == set(EXPECTED_HUMAN) and worker == set(EXPECTED_WORKER)
    assert set(hooks.NESTED_SESSION_COMMANDS) == {"claude", "watch", "demo", "eval"}


def test_b1_nested_beacon_deny_rules_and_message_come_from_the_constant():
    rules = {r for r in project.PROTECT_DENY if r.startswith("Bash(.claude/beacon ")}
    assert rules == {f"Bash(.claude/beacon {c} *)" for c in EXPECTED_HUMAN}
    assert rules == {f"Bash(.claude/beacon {c} *)" for c in hooks.HUMAN_COMMANDS}
    msg = guard("Bash", {"command": "python3 -m beacon answer 1 yes"}, [], {})
    for c in EXPECTED_HUMAN:
        assert c in msg


@pytest.mark.parametrize("argv", [["claude"], ["watch", "--cwd", ".", "--dry-run"], ["demo", "/nonexistent/x"],
                                  ["eval", "suite.json", "--out", "/tmp/x", "--dry-run"]])
def test_b1_nested_beacon_cli_refuses_inside_a_supervised_session(make_run, argv, monkeypatch, capsys):
    L = make_run()
    monkeypatch.setenv("BEACON_RUN", L.dir)                      # inherited from the supervised session
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))          # Claude Code's Bash tool: no TTY
    called = []
    monkeypatch.setattr(cli.os, "execvpe", lambda *a: called.append(a))
    import beacon.demo
    import beacon.evalkit
    import beacon.watchdog
    monkeypatch.setattr(beacon.watchdog, "watch", lambda *a, **k: called.append(a) or {})
    monkeypatch.setattr(beacon.demo, "make_kit", lambda *a, **k: called.append(a))
    monkeypatch.setattr(beacon.evalkit, "load_suite", lambda *a, **k: called.append(a) or [])
    rc = cli.main(argv)
    out = capsys.readouterr()
    assert rc != 0
    assert "inside a supervised" in out.err
    assert called == [] and "BEACON_WATCHDOG" not in out.out


class _Tty(io.StringIO):
    def isatty(self):
        return True


def test_b1_nested_beacon_cli_allows_a_human_terminal_and_the_orchestrator(make_run, tmp_path, monkeypatch, capsys):
    L = make_run()
    # a human in a terminal, BEACON_RUN exported
    monkeypatch.setenv("BEACON_RUN", L.dir)
    monkeypatch.setattr(sys, "stdin", _Tty(""))
    assert cli.main(["watch", "--cwd", str(tmp_path), "--dry-run"]) == 0
    assert "BEACON_WATCHDOG=1" in capsys.readouterr().out
    # the orchestrator: no terminal, but no inherited BEACON_RUN either (it passes --run)
    monkeypatch.delenv("BEACON_RUN")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["watch", "--run", L.dir, "--cwd", str(tmp_path), "--dry-run"]) == 0
    called = []
    monkeypatch.setattr(cli.os, "execvpe", lambda *a: called.append(a))
    assert cli.main(["claude", "--run", L.dir]) == 0
    assert called and called[0][2]["BEACON_RUN"] == L.dir
    # worker-side commands still work inside a supervised session
    monkeypatch.setenv("BEACON_RUN", L.dir)
    assert cli.main(["status"]) in (0, None)
