# -*- coding: utf-8 -*-
"""linux_gui_run / linux_gdb take the workdir display lock linux_ui_start holds.

`xvfb-run -a` picks the first display without an X lock, then starts Xvfb: two starts at the
same instant can pick the same display and the second Xvfb fails. linux_ui_start serialises
its starts with flock on the workdir directory (fd 9) until its display is up;
linux.py's xvfb-run calls now take that same lock and release it once xvfb-run's Xvfb is
up (first thing inside xvfb-run). VM-free: the generated shell runs under a local bash with
fake flock / xvfb-run that log what they see (the inode fd 9 points at, open or closed).
"""
import inspect
import os
import stat
import subprocess
import sys

import pytest

from labhand import linux, linux_ui, remote

FD9 = ('"$PY" -c "import os,sys\ntry: print(os.fstat(9).st_ino)\n'
       'except OSError: print(\'closed\')"')


def _bin(tmp, **scripts):
    d = tmp / "bin"
    d.mkdir(exist_ok=True)
    for name, body in scripts.items():
        p = d / name.replace("_", "-")
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return d


FAKE_FLOCK = ("#!/bin/bash\n"
              f'echo "flock $* fd9=$({FD9})" >> "$FAKE_LOG"\n')
FAKE_XVFB = ("#!/bin/bash\n"
             f'echo "xvfb-run $1 $2 [$3] fd9=$({FD9})" >> "$FAKE_LOG"\n'
             'shift 3; exec "$@"\n')
APP = f'echo "app fd9=$({FD9})" >> "$FAKE_LOG"; echo APP_RAN'


def _run(cmd, tmp, flock=True, workdir=True):
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    if workdir:
        (home / "samples").mkdir(exist_ok=True)
    scripts = {"xvfb_run": FAKE_XVFB}
    if flock:
        scripts["flock"] = FAKE_FLOCK
    b = _bin(tmp, **scripts)
    for tool in ("bash",):
        if not (b / tool).exists():
            os.symlink("/bin/bash", b / tool)
    env = {"PATH": str(b), "HOME": str(home), "FAKE_LOG": str(tmp / "log"),
           "PY": sys.executable}
    r = subprocess.run(["/bin/bash", "-c", cmd], env=env, cwd=str(home),
                       capture_output=True, text=True, timeout=30)
    log = (tmp / "log").read_text().splitlines() if (tmp / "log").exists() else []
    return r, log


@pytest.fixture(autouse=True)
def _wd(monkeypatch):
    monkeypatch.delenv("LABHAND_WORKDIR", raising=False)


def test_display_lock_order_and_target(tmp_path):
    """lock the workdir (same inode linux_ui_start locks as $WD) -> xvfb-run -> unlock
    inside xvfb-run -> the program runs with fd 9 closed."""
    r, log = _run(linux._xvfb_run_locked("800x600x16", APP), tmp_path)
    assert "APP_RAN" in r.stdout, (r.stdout, r.stderr)
    ino = str(os.stat(tmp_path / "home" / "samples").st_ino)
    assert [l.split(" fd9=")[0] for l in log] == [
        "flock -w 30 9", "xvfb-run -a -s [-screen 0 800x600x16]", "flock -u 9", "app"], log
    assert log[0].endswith("fd9=" + ino)          # locked: the workdir directory itself
    assert log[1].endswith("fd9=" + ino)          # xvfb-run starts while it is held
    assert log[2].endswith("fd9=" + ino)          # released inside xvfb-run, display up
    assert log[3].endswith("fd9=closed")          # the program does not inherit it


def test_display_lock_same_directory_as_linux_ui(tmp_path):
    """linux_ui_start locks "$WD" = $(pwd) after `cd <workdir>` from the login dir (home);
    the shared lock is "$HOME"/<workdir>, the same directory, also for a custom workdir."""
    os.environ["LABHAND_WORKDIR"] = "lab/w-1"
    try:
        cmd = linux._xvfb_run_locked("1x1x8", "true")
        assert 'exec 9<"$HOME"/lab/w-1;' in cmd
        assert "cd lab/w-1 || exit 9\nWD=$(pwd)" in linux_ui._wrap("", 5)
        assert 'exec 9<"$WD"\nflock -w 30 9' in inspect.getsource(linux_ui)
    finally:
        del os.environ["LABHAND_WORKDIR"]


def test_display_lock_without_flock_still_runs(tmp_path):
    r, log = _run(linux._xvfb_run_locked("800x600x16", APP), tmp_path, flock=False)
    assert "APP_RAN" in r.stdout, (r.stdout, r.stderr)
    assert [l.split(" fd9=")[0] for l in log] == ["xvfb-run -a -s [-screen 0 800x600x16]",
                                                  "app"]


def test_display_lock_without_workdir_still_runs(tmp_path):
    r, log = _run(linux._xvfb_run_locked("800x600x16", APP), tmp_path, workdir=False)
    assert "APP_RAN" in r.stdout, (r.stdout, r.stderr)
    assert not any(l.startswith("flock -w") for l in log), log


def test_display_lock_inner_quoting(tmp_path):
    """the inner command keeps its quotes and metacharacters (no injection by the wrap)."""
    inner = APP + "; printf '%s\\n' \"a'b; \\$x\""
    r, log = _run(linux._xvfb_run_locked("800x600x16", inner), tmp_path)
    assert "a'b; $x" in r.stdout, (r.stdout, r.stderr)


# ------------------------------------------------- both linux.py tools use the shared lock
@pytest.fixture
def seen(monkeypatch):
    cmds = []
    monkeypatch.setattr(remote, "write_remote", lambda *a, **k: None)
    monkeypatch.setattr(remote, "remove_quiet", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(remote, "read_bytes", lambda *a, **k: b"x", raising=False)

    def fake_exec(cmd, host=None, read_timeout=None):
        cmds.append(cmd)
        return ("XSHOT_OK\n__GDB_RC=0\n__RC=0\n", "")
    monkeypatch.setattr(remote, "ssh_exec", fake_exec)
    return cmds


def _lock_in(cmd):
    return 'flock -w 30 9' in cmd and 'flock -u 9' in cmd and 'exec 9<"$HOME"/samples' in cmd


def test_display_lock_linux_gui_run(seen):
    linux.linux_gui_run("xterm", host="kali")
    assert seen and _lock_in(seen[-1]), seen


def test_display_lock_linux_gdb_gui(seen):
    linux.linux_gdb("chall", "run()\n", host="kali", gui=True, timeout=30)
    assert seen and _lock_in(seen[-1]), seen


def test_display_lock_linux_gdb_nogui_untouched(seen):
    linux.linux_gdb("chall", "run()\n", host="kali", gui=False, timeout=30)
    assert seen and "flock" not in seen[-1] and "xvfb-run" not in seen[-1]


def test_display_lock_gdb_read_timeout_covers_lock_wait(monkeypatch):
    """the lock wait (up to 30 s) comes before gdb's own timeout: the SSH read timeout of a
    gui run leaves room for both, so paramiko does not give up on a call still in time."""
    got = []
    monkeypatch.setattr(remote, "write_remote", lambda *a, **k: None)
    monkeypatch.setattr(remote, "remove_quiet", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(remote, "ssh_exec",
                        lambda cmd, host=None, read_timeout=None: got.append(read_timeout)
                        or ("__GDB_RC=0\n", ""))
    linux.linux_gdb("chall", "run()\n", host="kali", gui=True, timeout=100)
    assert got[-1] >= 100 + 30 + 30
