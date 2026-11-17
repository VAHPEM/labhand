# -*- coding: utf-8 -*-
"""labhand.linux.linux_vol (Volatility 3) and linux_yara(engine=...) (YARA / YARA-X):
VM-free. The shell layer is mocked exactly like tests/test_captriage.py; no SSH is opened."""
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


def _missing(monkeypatch, tool):
    raw = f"bash: line 6: {tool}: command not found"
    execs = []

    def fake_exec(cmd, host=None, read_timeout=None):
        execs.append(cmd)
        return raw + "\n__RC=127", ""
    monkeypatch.setattr(remote, "ssh_exec", fake_exec)
    monkeypatch.setattr(shell, "_spill", lambda host, text, stem: (text, None))
    return raw, execs


# ================================================================ linux_vol
def test_linux_vol_command_string(wrap, no_network):
    r = linux.linux_vol("mem.raw", "linux.pslist.PsList")
    assert r.ok and r.detail == "RAW OUT" and r.summary == "ran"     # returned untouched
    assert wrap[0]["cmd"] == "vol -f mem.raw linux.pslist.PsList"
    assert wrap[0]["host"] == "kali" and wrap[0]["timeout"] == 300


def test_linux_vol_extra_goes_after_plugin_as_quoted_words(wrap, no_network):
    linux.linux_vol("mem.raw", "linux.pslist.PsList", extra="--pid 1 --dump")
    assert wrap[0]["cmd"] == "vol -f mem.raw linux.pslist.PsList --pid 1 --dump"
    linux.linux_vol("mem.raw", "linux.bash.Bash", extra=["--x", "a b"])
    assert wrap[1]["cmd"] == "vol -f mem.raw linux.bash.Bash --x 'a b'"


def test_linux_vol_timeout_plumbed(wrap, no_network):
    linux.linux_vol("m", "p", timeout=42)
    linux.linux_vol("m", "p", timeout="bad")
    assert [c["timeout"] for c in wrap] == [42, 300]


def test_linux_vol_timeout_is_the_real_tree_killing_timeout(monkeypatch, no_network):
    seen = {}

    def fake_exec(cmd, host=None, read_timeout=None):
        seen.update(cmd=cmd, read_timeout=read_timeout)
        return "PID\tPPID\n1\t0\n__RC=0", ""
    monkeypatch.setattr(remote, "ssh_exec", fake_exec)
    monkeypatch.setattr(shell, "_spill", lambda host, text, stem: (text, None))
    r = linux.linux_vol("mem.raw", "linux.pslist.PsList", timeout=33)
    assert r.ok and "PID\tPPID\n1\t0" in r.detail
    assert seen["cmd"].startswith("timeout -k 5 33 bash -c ")
    assert "vol -f mem.raw linux.pslist.PsList" in seen["cmd"]


@pytest.mark.parametrize("kw, words", [
    (dict(image=NASTY, plugin="linux.pslist.PsList"),
     ["vol", "-f", "./" + NASTY, "linux.pslist.PsList"]),
    (dict(image="m", plugin="x; touch pwned $(id) `w`"),
     ["vol", "-f", "m", "x; touch pwned $(id) `w`"]),
    (dict(image="m", plugin="p", extra="--pid 1; touch pwned $(id) >o"),
     ["vol", "-f", "m", "p", "--pid", "1;", "touch", "pwned", "$(id)", ">o"]),
], ids=["image", "plugin", "extra"])
def test_linux_vol_nasty_args_are_shell_quoted(kw, words, wrap, no_network):
    linux.linux_vol(**kw)
    assert shlex.split(wrap[-1]["cmd"]) == words


def test_linux_vol_plugin_that_looks_like_an_option_is_refused(wrap, no_network):
    r = linux.linux_vol("m", "-o /tmp/x")
    assert r.ok is False and "plugin" in r.summary
    assert wrap == []


def test_linux_vol_empty_and_bad_extra_refused(wrap, no_network):
    assert linux.linux_vol("", "p").ok is False
    assert linux.linux_vol("m", " ").ok is False
    r = linux.linux_vol("m", "p", extra="--x 'oops")
    assert r.ok is False and "extra" in r.summary
    assert wrap == []


def test_linux_vol_unknown_host_refused_before_connecting(wrap, no_network):
    r = linux.linux_vol("m", "p", host="kaly")
    assert r.ok is False
    assert r.summary == "linux_vol: unknown host 'kaly'; known hosts: kali, windows"
    assert wrap == []


def test_linux_vol_windows_host_refused(wrap, no_network):
    r = linux.linux_vol("m", "p", host="windows")
    assert r.ok is False
    assert r.summary == ("linux_vol: host 'windows' runs windows, this tool needs a linux "
                         "host (kali)")
    assert wrap == []


def test_linux_vol_branches_on_os_not_name(inv, wrap, no_network):
    inv({"LABHAND_HOSTS": "kali,box",
         "KALI_HOST": "a", "KALI_USER": "u", "KALI_OS": "windows",
         "BOX_HOST": "b", "BOX_USER": "u", "BOX_OS": "linux"})
    assert linux.linux_vol("m", "p", host="kali").ok is False
    linux.linux_vol("m", "p")
    assert [c["host"] for c in wrap] == ["box"]


def test_linux_vol_not_installed_returns_raw_error(monkeypatch, no_network):
    raw, execs = _missing(monkeypatch, "vol")
    r = linux.linux_vol("mem.raw", "linux.pslist.PsList")
    assert r.ok is False and raw in r.detail
    assert "pipx install volatility3" in r.summary and "does not install" in r.summary
    assert len(execs) == 1 and "install" not in execs[0]


def test_linux_vol_label(wrap, no_network):
    linux.linux_vol("mem.raw", "linux.pslist.PsList")
    assert wrap[0]["label"] == "vol linux.pslist.PsList mem.raw"


# ---------------------------------------------------------------- linux_vol vol_opts
def test_linux_vol_vol_opts_go_before_f(wrap, no_network):
    r = linux.linux_vol("mem.raw", "linux.pslist.PsList", vol_opts="--offline -r json")
    assert r.ok and r.detail == "RAW OUT"
    assert wrap[0]["cmd"] == "vol --offline -r json -f mem.raw linux.pslist.PsList"
    linux.linux_vol("mem.raw", "linux.pslist.PsList", vol_opts=["-s", "my syms"],
                    extra="--pid 1")
    assert wrap[1]["cmd"] == "vol -s 'my syms' -f mem.raw linux.pslist.PsList --pid 1"


def test_linux_vol_vol_opts_default_is_unchanged(wrap, no_network):
    linux.linux_vol("mem.raw", "linux.pslist.PsList")
    linux.linux_vol("mem.raw", "linux.pslist.PsList", vol_opts="")
    linux.linux_vol("mem.raw", "linux.pslist.PsList", extra="--pid 1", vol_opts=None)
    assert [c["cmd"] for c in wrap] == ["vol -f mem.raw linux.pslist.PsList"] * 2 + [
        "vol -f mem.raw linux.pslist.PsList --pid 1"]


def test_linux_vol_vol_opts_nasty_are_shell_quoted(wrap, no_network):
    linux.linux_vol("m", "p", vol_opts="--offline; touch pwned $(id) `w` >o")
    cmd = wrap[0]["cmd"]
    assert shlex.split(cmd) == ["vol", "--offline;", "touch", "pwned", "$(id)", "`w`", ">o",
                                "-f", "m", "p"]
    assert cmd.startswith("vol '--offline;' touch pwned '$(id)' '`w`' '>o' -f m p")


def test_linux_vol_vol_opts_bad_quote_refused(wrap, no_network):
    r = linux.linux_vol("m", "p", vol_opts="--offline 'oops")
    assert r.ok is False and "vol_opts" in r.summary
    assert wrap == []


# ================================================================ linux_yara engine
def test_yara_engine_default_is_unchanged(wrap, no_network):
    linux.linux_yara("rules.yar", "dump/")
    linux.linux_yara("r.yar", "t", extra="-s -r")
    linux.linux_yara("r.yar", "t", engine="yara")
    assert [c["cmd"] for c in wrap] == ["yara rules.yar dump/", "yara -s -r r.yar t",
                                        "yara r.yar t"]
    assert wrap[0]["label"] == "yara rules.yar dump/" and wrap[0]["timeout"] == 60


def test_yara_engine_yarax_runs_yr_scan(wrap, no_network):
    r = linux.linux_yara("rules.yar", "dump/", engine="yarax")
    assert r.ok and r.detail == "RAW OUT"
    linux.linux_yara("r.yar", "t", extra="-s --threads 2", engine="yarax")
    assert [c["cmd"] for c in wrap] == ["yr scan rules.yar dump/",
                                        "yr scan -s --threads 2 r.yar t"]
    assert wrap[0]["host"] == "kali" and wrap[0]["timeout"] == 60


def test_yara_engine_yarax_nasty_args_are_shell_quoted(wrap, no_network):
    linux.linux_yara(NASTY, NASTY + "2", extra="-s; id", engine="yarax")
    assert shlex.split(wrap[-1]["cmd"]) == ["yr", "scan", "-s;", "id",
                                            "./" + NASTY, "./" + NASTY + "2"]


@pytest.mark.parametrize("engine", ["yr", "YARA", "yara-x", "", None, "yara; id", 1,
                                    ["yara"]])
def test_yara_engine_invalid_refused_naming_allowed(engine, wrap, no_network):
    r = linux.linux_yara("r.yar", "t", engine=engine)      # must not raise (list -> unhashable)
    assert r.ok is False
    assert "unknown engine" in r.summary and "allowed: yara, yarax" in r.summary
    assert wrap == []


def test_yara_engine_yarax_label(wrap, no_network):
    linux.linux_yara("rules.yar", "dump/", engine="yarax")
    assert wrap[0]["label"] == "yr scan rules.yar dump/"


def test_yara_engine_yarax_unknown_and_windows_host_refused(wrap, no_network):
    assert linux.linux_yara("r", "t", host="kaly", engine="yarax").summary == (
        "linux_yara: unknown host 'kaly'; known hosts: kali, windows")
    assert linux.linux_yara("r", "t", host="windows", engine="yarax").ok is False
    assert wrap == []


def test_yara_engine_yarax_not_installed_returns_raw_error(monkeypatch, no_network):
    raw, execs = _missing(monkeypatch, "yr")
    r = linux.linux_yara("r.yar", "t", engine="yarax")
    assert r.ok is False and raw in r.detail
    assert "cargo install yara-x-cli" in r.summary and "does not install" in r.summary
    assert len(execs) == 1 and "install" not in execs[0] and "yr scan r.yar t" in execs[0]
