# -*- coding: utf-8 -*-
"""Capability triage (labhand.linux.linux_capa / linux_yara): VM-free. The shell layer is
mocked; these tests check the command line built, host-OS branching, unknown hosts refused
before connecting, timeout plumbing, shell-quoting, and that output comes back RAW."""
import shlex

import pytest

from labhand import hosts, linux, remote, shell
from labhand._result import VMResult

NASTY = "-evil sample; rm -rf ~ $(id) `whoami` 'q\" >x.txt"


@pytest.fixture
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a connection was attempted")
    monkeypatch.setattr(remote, "_client", boom)
    monkeypatch.setattr(remote, "_POOL", {})


@pytest.fixture
def wrap(monkeypatch):
    """Capture what linux_* hands to the shared shell layer (_run_and_wrap)."""
    calls = []

    def fake(command, host, timeout, label, grep_shape=None):
        calls.append({"cmd": command, "host": host, "timeout": timeout, "label": label})
        return VMResult(True, "ran", detail="RAW OUT")
    monkeypatch.setattr(linux, "_run_and_wrap", fake)
    return calls


@pytest.fixture
def inv():
    def use(env):
        return hosts.reload(env)
    yield use
    hosts.reload()


# ---------------------------------------------------------------- command lines
def test_captriage_run_capa_command_string(wrap, no_network):
    r = linux.linux_capa("sample.exe")
    assert r.ok and r.detail == "RAW OUT"          # returned untouched
    assert wrap == [{"cmd": "capa sample.exe", "host": "kali", "timeout": 120,
                     "label": "capa sample.exe"}]


def test_captriage_run_capa_extra_options_are_quoted_words(wrap, no_network):
    linux.linux_capa("s.bin", extra="-j --os linux")
    assert wrap[0]["cmd"] == "capa -j --os linux s.bin"
    linux.linux_capa("s.bin", extra=["-r", "my rules"])
    assert wrap[1]["cmd"] == "capa -r 'my rules' s.bin"


def test_captriage_run_yara_command_string(wrap, no_network):
    r = linux.linux_yara("rules.yar", "dump/")
    assert r.ok and r.detail == "RAW OUT"
    assert wrap[0]["cmd"] == "yara rules.yar dump/"
    assert wrap[0]["timeout"] == 60 and wrap[0]["host"] == "kali"
    linux.linux_yara("r.yar", "t", extra="-s -r")
    assert wrap[1]["cmd"] == "yara -s -r r.yar t"


# ---------------------------------------------------------------- shell-quoting
@pytest.mark.parametrize("call, words", [
    (lambda: linux.linux_capa(NASTY), lambda: ["capa", "./" + NASTY]),
    (lambda: linux.linux_yara(NASTY, NASTY + "2"),
     lambda: ["yara", "./" + NASTY, "./" + NASTY + "2"]),
    (lambda: linux.linux_capa("a", extra="-j; touch pwned $(id)"),
     lambda: ["capa", "-j;", "touch", "pwned", "$(id)", "a"]),
], ids=["capa-path", "yara-rules-target", "capa-extra"])
def test_captriage_run_nasty_args_are_shell_quoted(call, words, wrap, no_network):
    call()
    cmd = wrap[-1]["cmd"]
    # the shell sees exactly these words: no `;`, `$(..)`, backtick or redirection escapes,
    # and a leading "-" in a path cannot be taken for an option
    assert shlex.split(cmd) == words()


# ---------------------------------------------------------------- timeout plumbing
def test_captriage_run_timeout_is_passed_to_the_real_timeout(monkeypatch, no_network):
    seen = {}

    def fake_exec(cmd, host=None, read_timeout=None):
        seen.update(cmd=cmd, host=host, read_timeout=read_timeout)
        return "capa raw table\n__RC=0", ""
    monkeypatch.setattr(remote, "ssh_exec", fake_exec)
    monkeypatch.setattr(shell, "_spill", lambda host, text, stem: (text, None))
    r = linux.linux_capa("s.exe", timeout=77)
    assert r.ok and "capa raw table" in r.detail
    assert seen["cmd"].startswith("timeout -k 5 77 bash -c ")   # tree-killing timeout on the VM
    assert "capa s.exe" in seen["cmd"] and seen["read_timeout"] == 77 + 25
    linux.linux_yara("r.yar", "t", timeout=9)
    assert seen["cmd"].startswith("timeout -k 5 9 bash -c ")


def test_captriage_run_timeout_kill_is_reported_not_ok(monkeypatch, no_network):
    monkeypatch.setattr(remote, "ssh_exec", lambda cmd, host=None, read_timeout=None:
                        ("partial\n__RC=124", ""))
    monkeypatch.setattr(shell, "_spill", lambda host, text, stem: (text, None))
    r = linux.linux_capa("big.exe", timeout=5)
    assert r.ok is False and "TIMEOUT" in r.summary


def test_captriage_run_large_output_spills(monkeypatch, no_network):
    monkeypatch.setattr(remote, "ssh_exec", lambda cmd, host=None, read_timeout=None:
                        ("x" * 50 + "\n__RC=0", ""))
    spilled = []
    monkeypatch.setattr(shell, "_spill",
                        lambda host, text, stem: spilled.append(text) or ("HEAD...", "samples/_cmd_out_1.txt"))
    r = linux.linux_yara("r.yar", "t")
    assert spilled and r.detail.startswith("HEAD...")
    assert any("samples/_cmd_out_1.txt" in k for k in r.known)


# ---------------------------------------------------------------- tool missing
@pytest.mark.parametrize("call, tool, hint", [
    (lambda: linux.linux_capa("s.exe"), "capa", "pipx install flare-capa"),
    (lambda: linux.linux_yara("r.yar", "t"), "yara", "apt install yara"),
])
def test_captriage_run_missing_tool_returns_raw_error(call, tool, hint, monkeypatch, no_network):
    raw = f"bash: line 6: {tool}: command not found"
    execs = []

    def fake_exec(cmd, host=None, read_timeout=None):
        execs.append(cmd)
        return raw + "\n__RC=127", ""
    monkeypatch.setattr(remote, "ssh_exec", fake_exec)
    monkeypatch.setattr(shell, "_spill", lambda host, text, stem: (text, None))
    r = call()
    assert r.ok is False and raw in r.detail        # the raw error, unchanged
    assert hint in r.summary and "does not install" in r.summary
    assert len(execs) == 1                          # one run, no install attempt
    assert "install" not in execs[0]


# ---------------------------------------------------------------- host branching
@pytest.mark.parametrize("tool, call", [
    ("linux_capa", lambda h: linux.linux_capa("s", host=h)),
    ("linux_yara", lambda h: linux.linux_yara("r", "t", host=h)),
])
def test_captriage_run_unknown_host_refused_before_connecting(tool, call, wrap, no_network):
    r = call("kaly")
    assert r.ok is False
    assert r.summary == f"{tool}: unknown host 'kaly'; known hosts: kali, windows"
    assert wrap == []


@pytest.mark.parametrize("tool, call", [
    ("linux_capa", lambda h: linux.linux_capa("s", host=h)),
    ("linux_yara", lambda h: linux.linux_yara("r", "t", host=h)),
])
def test_captriage_run_windows_host_refused(tool, call, wrap, no_network):
    r = call("windows")
    assert r.ok is False
    assert r.summary == (f"{tool}: host 'windows' runs windows, this tool needs a linux "
                         "host (kali)")
    assert wrap == []


def test_captriage_run_branches_on_os_not_name(inv, wrap, no_network):
    inv({"LABHAND_HOSTS": "kali,box",
         "KALI_HOST": "a", "KALI_USER": "u", "KALI_OS": "windows",
         "BOX_HOST": "b", "BOX_USER": "u", "BOX_OS": "linux"})
    assert linux.linux_capa("s", host="kali").ok is False      # named kali, runs windows
    linux.linux_capa("s")                                       # default = first LINUX host
    linux.linux_yara("r", "t", host="box")
    assert [c["host"] for c in wrap] == ["box", "box"]


def test_captriage_run_empty_args_refused(wrap, no_network):
    assert linux.linux_capa("").ok is False
    assert linux.linux_yara("", "t").ok is False
    assert linux.linux_yara("r", " ").ok is False
    assert wrap == []


# ---------------------------------------------------------------- the skills (the Brain's side)
import pathlib  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("skill", ["binary-analysis", "vuln-research"])
def test_captriage_skills_teach_capa_and_the_untrusted_output_note(skill):
    text = (_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    low = " ".join(text.lower().replace("**", "").split())   # markdown wraps lines
    assert "linux_capa" in text and "linux_yara" in text
    for tool in ("capa", "yara", "ssdeep", "tlsh", "imphash", "rich-header", "volatility 3"):
        assert tool in low, f"{skill}: {tool} not mentioned"
    assert "att&ck" in low and "mbc" in low and "yourself" in low   # interpretation is the Brain's
    assert "does not auto-install" in low and "pipx install flare-capa" in text
    assert "untrusted data, not instructions" in low and "prompt injection" in low


def test_captriage_skills_sync_check_reports_in_sync():
    r = subprocess.run([sys.executable, str(_ROOT / "skills" / "sync.py"), "--check"],
                       capture_output=True, text=True, cwd=_ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "in sync" in r.stdout


def test_captriage_run_unbalanced_extra_is_refused_not_raised(wrap, no_network):
    r = linux.linux_capa("s", extra="-r 'oops")
    assert r.ok is False and "extra" in r.summary
    assert linux.linux_yara("r", "t", extra='"x').ok is False
    assert wrap == []
