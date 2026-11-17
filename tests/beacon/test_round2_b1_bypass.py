"""Review P4 (round 2 of B1): the guard matched raw text, so quoting, `-mbeacon`, `--`, a repeated `--run`
and `${IFS}` got a human-only / session-starting subcommand past it; and the CLI's own refusal checked only
BEACON_RUN, so a worker that popped that one variable started a nested supervised Claude Code. Ordinary
commands that merely mention the words must stay allowed (review F2)."""
import io
import sys

import pytest

from beacon import cli, hooks
from beacon.hooks import guard


def _refused(cmd):
    reason = guard("Bash", {"command": cmd}, [], {})
    return reason is not None and ("for the human" in reason or "another Claude Code" in reason)


# The spellings the review's probe showed getting past the guard (each was "allowed" on the round-1 code).
BYPASSES = [
    ".claude/beacon 'claude'",
    ".claude/beacon 'watch' --cwd .",
    '.claude/beacon "watch" --cwd .',
    '.claude/beacon "claude" --run /r',
    ".claude/beacon w\\atch --cwd .",
    "python3 -mbeacon watch --cwd .",
    "python3 -Imbeacon watch",
    "python3 -Im beacon claude",
    "python3 -m beacon -- watch --cwd .",
    "python3 -m beacon --run /r --run /r watch",
    "python3 -m beacon --run '/a b' watch",
    "python3 -m 'beacon' watch --cwd .",
    "beacon${IFS}watch --cwd .",
    ".claude/beacon$IFS'claude'",
    ".claude/beacon\twatch",
    "python3 -m beacon.cli\twatch",
    "/Users/x/proj/.claude/beacon watch",
    "../proj/.claude/beacon claude",
    "PYTHONPATH=. python3 -I -m beacon watch",
    "python3 beacon/cli.py watch --cwd .",
    "python3 ./beacon/__main__.py claude",
    "python3 -c 'from beacon.cli import main; main([\"watch\",\"--cwd\",\".\"])'",
    "B=.claude/beacon; $B watch --cwd .",
    "echo watch | xargs .claude/beacon",
    "printf '%s' claude | xargs -I{} .claude/beacon {}",
    "eval '.claude/beacon watch'",
    "bash -lc \".claude/beacon 'demo' --auto\"",
    "true && .claude/beacon \\\n  eval suite.json",
    "x=$(.claude/beacon 'claude')",
    "uv run python -m beacon watch",
    "python3 -Xfrozen_modules=off -m beacon watch",
    "python3 -W ignore -m beacon.cli eval x",
    "sh .claude/beacon watch",
    "exec -a foo .claude/beacon watch",
    "python3 -Ic \"from beacon.cli import main; main(['watch'])\"",
    "script -q /dev/null python3 -m beacon 'watch' --cwd .",
    "timeout 5 .claude/beacon 'eval' s.json",
    "env -i PATH=/bin .claude/beacon 'watch'",
    "sudo -u me .claude/beacon \"verify\" P1",
    "'claude' -p hi",
    "/usr/local/bin/'claude' --version",
]


@pytest.mark.parametrize("cmd", BYPASSES)
def test_round2_b1_bypass_spellings_are_refused(cmd):
    assert _refused(cmd), cmd


# Ordinary work that mentions claude / watch / demo / eval / beacon without running a human command.
ORDINARY = [
    "grep 'beacon watch' README.md",
    "grep -n 'beacon watch' beacon/README.md",
    "git commit -m 'beacon claude guard'",
    'git commit -m "fix: beacon eval and beacon demo refusal"',
    "git log --grep 'beacon claude'",
    "ls beacon demo.py",
    "echo hi # beacon eval",
    "echo 'run .claude/beacon watch from a terminal'",
    "python3 -m pytest tests/beacon -k 'watch or eval'",
    "python3 -c 'print(\"watch\")'",
    "cat .claude/beacon",
    "python3 -m beacon status",
    ".claude/beacon 'status'",
    "python3 -mbeacon log -n 20",
    "python3 -m beacon -- transcript",
    "xargs -a files.txt grep -n watch",
    "sed -n 1,40p beacon/watchdog.py",
    "find . -name 'claude*'",
    "script -q /tmp/typescript ls -la",
]


@pytest.mark.parametrize("cmd", ORDINARY)
def test_round2_b1_bypass_ordinary_work_is_allowed(cmd):
    assert guard("Bash", {"command": cmd}, [], {}) is None, cmd


def test_round2_b1_bypass_guard_classifies_from_the_single_constant(monkeypatch):
    # the normalised matcher reads HUMAN_COMMANDS: a command added there is refused in every spelling
    monkeypatch.setattr(hooks, "HUMAN_COMMANDS", hooks.HUMAN_COMMANDS + ("frobnicate",))
    assert hooks.human_cli_hit(".claude/beacon 'frobnicate'") == "frobnicate"
    assert hooks.human_cli_hit("python3 -mbeacon -- frobnicate") == "frobnicate"


@pytest.mark.parametrize("marker", cli.SUPERVISED_MARKERS)
@pytest.mark.parametrize("argv", [["claude"], ["watch", "--cwd", ".", "--dry-run"], ["demo", "/nonexistent/x"],
                                  ["eval", "suite.json", "--out", "/tmp/x", "--dry-run"]])
def test_round2_b1_bypass_cli_refuses_under_any_single_marker(make_run, marker, argv, monkeypatch, capsys):
    """Only ONE marker set (the worker popped the others, e.g. os.environ.pop('BEACON_RUN')), no TTY."""
    L = make_run()
    for v in cli.SUPERVISED_MARKERS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv(marker, L.dir if marker == "BEACON_RUN" else "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    called = []
    monkeypatch.setattr(cli.os, "execvpe", lambda *a: called.append(a))
    import beacon.demo
    import beacon.evalkit
    import beacon.watchdog
    monkeypatch.setattr(beacon.watchdog, "watch", lambda *a, **k: called.append(a) or {})
    monkeypatch.setattr(beacon.demo, "make_kit", lambda *a, **k: called.append(a))
    monkeypatch.setattr(beacon.evalkit, "load_suite", lambda *a, **k: called.append(a) or [])
    rc = cli.main(argv + (["--run", L.dir] if argv[0] in ("claude", "watch") else []))
    err = capsys.readouterr().err
    assert rc != 0
    assert "inside a supervised" in err and marker in err
    assert called == []


def test_round2_b1_bypass_cli_popped_run_with_claudecode_is_refused(make_run, monkeypatch, capsys):
    """The review's demo: a worker pops BEACON_RUN and calls main() with argv claude --run R in its own process;
    Claude Code's CLAUDECODE is still inherited."""
    L = make_run(permission_mode="bypassPermissions")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    called = []
    monkeypatch.setattr(cli.os, "execvpe", lambda *a: called.append(a))
    monkeypatch.setattr(sys, "argv", ["b", "claude", "--claude", "/bin/true", "--run", L.dir])
    assert cli.main() != 0
    assert called == []
    assert "CLAUDECODE is set" in capsys.readouterr().err


def test_round2_b1_bypass_cli_allows_no_marker_or_a_terminal(make_run, tmp_path, monkeypatch, capsys):
    L = make_run()
    for v in cli.SUPERVISED_MARKERS:
        monkeypatch.delenv(v, raising=False)
    # the orchestrator: no marker, no terminal
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["watch", "--run", L.dir, "--cwd", str(tmp_path), "--dry-run"]) == 0

    class _Tty(io.StringIO):
        def isatty(self):
            return True
    # a human in a terminal with every marker exported
    monkeypatch.setattr(sys, "stdin", _Tty(""))
    monkeypatch.setenv("BEACON_RUN", L.dir)
    monkeypatch.setenv("BEACON_WATCHDOG", "1")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli.main(["watch", "--cwd", str(tmp_path), "--dry-run"]) == 0
    # a closed stdin counts as no terminal, not as a crash
    monkeypatch.setattr(sys, "stdin", None)
    assert cli.main(["watch", "--cwd", str(tmp_path), "--dry-run"]) != 0
