# -*- coding: utf-8 -*-
"""linux_gdb's `echo __GDB_RC=$?` marker is the last line of STDOUT. On Kali pwndbg writes a
terminfo warning to stderr, and the old end-anchored search over stdout+stderr missed the
marker: a run killed by its timeout (rc 124) came back ok=True, with the marker left in the
detail (measured live 2026-11-20). VM-free: ssh_exec / SFTP are fakes."""
import pytest

from labhand import linux, remote

WARN = "warning: terminfo: tput: unknown terminal \"dumb\"\n"


@pytest.fixture
def gdb(monkeypatch):
    monkeypatch.setattr(remote, "write_remote", lambda *a, **k: None)
    monkeypatch.setattr(remote, "remove_quiet", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(linux, "_spill", lambda host, text, stem: (text, None), raising=False)
    box = {}

    def run(out, err):
        monkeypatch.setattr(remote, "ssh_exec", lambda cmd, host=None, read_timeout=None: (out, err))
        return linux.linux_gdb("prog", "log('x')", host="kali", gui=False, timeout=5)
    return run


def test_gdb_rc_marker_timeout_with_stderr_is_not_ok(gdb):
    r = gdb("partial line\n__GDB_RC=124\n", WARN)
    assert not r.ok and "KILLED after 5s" in r.summary
    assert "__GDB_RC" not in r.detail and "partial line" in r.detail and "terminfo" in r.detail


def test_gdb_rc_marker_success_with_stderr_is_ok(gdb):
    r = gdb("hello\n__GDB_RC=0\n", WARN)
    assert r.ok and "KILLED" not in r.summary and "__GDB_RC" not in r.detail


def test_gdb_rc_marker_last_marker_wins_and_no_stderr(gdb):
    # the target itself printed a fake marker earlier: the real (last) one decides
    r = gdb("__GDB_RC=0\nmore\n__GDB_RC=124", "")
    assert not r.ok and "__GDB_RC=124" not in r.detail
    r = gdb("ok\n__GDB_RC=0\r\n", "")
    assert r.ok


def test_gdb_rc_unterminated_prompt_timeout_is_not_ok(gdb):
    # the inferior printed a prompt without a newline and blocked on stdin: the wrapper's
    # echo lands on the same line (review R2-F1)
    r = gdb("Password: __GDB_RC=124\n", WARN)
    assert not r.ok and "KILLED after 5s" in r.summary
    assert "__GDB_RC" not in r.detail and "Password:" in r.detail


def test_gdb_rc_unterminated_prompt_success_and_last_wins(gdb):
    r = gdb("Enter: __GDB_RC=0\n", "")
    assert r.ok and "__GDB_RC" not in r.detail and "Enter:" in r.detail
    r = gdb("x __GDB_RC=0 y\nPassword: __GDB_RC=124", "")
    assert not r.ok and "__GDB_RC=124" not in r.detail
