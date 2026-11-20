# -*- coding: utf-8 -*-
"""linux_gdb passes an absolute `prog` to gdb unchanged; a relative one resolves in the
workdir with "./". Before, "./" was prefixed to every path, so "/usr/bin/sleep" became
".//usr/bin/sleep" (gdb: "No executable file specified", ok=True; reproduced live
2026-11-21). VM-free: ssh_exec / SFTP are fakes that record the command."""
import shlex

import pytest

from labhand import linux, remote


@pytest.fixture
def gdb_cmd(monkeypatch):
    monkeypatch.setattr(remote, "write_remote", lambda *a, **k: None)
    monkeypatch.setattr(remote, "remove_quiet", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(linux, "_spill", lambda host, text, stem: (text, None), raising=False)
    seen = []

    def fake_exec(cmd, host=None, read_timeout=None):
        seen.append(cmd)
        return ("ok\n__GDB_RC=0\n", "")
    monkeypatch.setattr(remote, "ssh_exec", fake_exec)

    def run(prog, args="", gui=False):
        r = linux.linux_gdb(prog, "run()\n", args=args, host="kali", gui=gui, timeout=30)
        assert r.ok
        # unwrap `bash -c '<inner>'` (and the xvfb-run `bash -c '<gdb_cmd>'` when gui)
        inner = shlex.split(seen[-1])[2]
        if gui:
            toks = shlex.split(inner.split(" 2>/dev/null; ", 1)[1])
            inner = toks[toks.index("-c", toks.index("bash")) + 1].rstrip(";")
        gdb = inner.split("--args ", 1)[1].split("; echo __GDB_RC")[0]
        return shlex.split(gdb)
    return run


def test_gdb_abs_prog_passed_unchanged(gdb_cmd):
    assert gdb_cmd("/usr/bin/sleep", args="1") == ["/usr/bin/sleep", "1"]


def test_gdb_abs_prog_with_space_quoted(gdb_cmd):
    assert gdb_cmd("/tmp/my dir/a.out") == ["/tmp/my dir/a.out"]


def test_gdb_abs_prog_gui_path(gdb_cmd):
    assert gdb_cmd("/usr/bin/sleep", args="1", gui=True) == ["/usr/bin/sleep", "1"]


def test_gdb_abs_prog_relative_still_in_workdir(gdb_cmd):
    assert gdb_cmd("chall") == ["./chall"]
    assert gdb_cmd("sub/my chall", args=["a b"]) == ["./sub/my chall", "a b"]
