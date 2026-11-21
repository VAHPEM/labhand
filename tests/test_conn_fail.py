# -*- coding: utf-8 -*-
"""A connection failure is a VMResult(ok=False), never an exception (CLAUDE.md, design
invariants). Every public tool is wrapped by `labhand._result.public_tool`.

(a) every tool the MCP server publishes, with the SSH connect point failing, returns
    ok=False (library call and MCP call) - enumerated from the server, so a new tool that
    forgets the wrapper fails here;
(b) an SFTP session that breaks in the middle of a call;
(c) KeyboardInterrupt / SystemExit still propagate.

VM-free: paramiko's connect is patched, time.sleep is a no-op (the retry ladder still runs
all its attempts, just without the waits), utmctl is faked and never run."""
import asyncio
import os
import time

import paramiko
import pytest

pytest.importorskip("mcp")

from labhand import jobs, linux, linux_ui, mcp_server, remote, shell, utm, windows  # noqa: E402
from labhand._result import VMResult  # noqa: E402

MODULES = (shell, windows, linux, linux_ui, jobs, utm)
SESSION = "xterm-0123abcd"

# Arguments that get each tool past its own argument checks to the network.
ARGS = {
    "run_cmd": {"command": "ver", "host": "windows"},      # the orchestrator's 2026-11-21 case
    "run_script": {"script": "echo hi"},
    "read_file": {"path": "x.txt"},
    "upload_file": {"local_path": __file__},
    "download_file": {"path": "x.bin"},                     # dest: tmp_path, set below
    "win_windows": {}, "win_screenshot": {},
    "win_gui_run": {"program": "notepad.exe"},
    "win_frida": {"script": "send(1)", "program": "notepad.exe"},
    "win_ui_tree": {}, "win_ui_click": {"target": "OK"},
    "win_ui_type": {"text": "hi"}, "win_key": {"keys": "{ENTER}"},
    "win_ui_seq": {"clicks": "OK"},
    "linux_gui_run": {"command": "xterm"},
    "linux_gdb": {"prog": "a.out", "script": "run()"},
    "linux_ghidra": {"path": "a.out"},
    "linux_capa": {"path": "a.out"},
    "linux_yara": {"rules": "r.yar", "target": "a.out"},
    "linux_vol": {"image": "m.raw", "plugin": "linux.pslist"},
    "linux_ui_start": {"command": "xterm"},
    "linux_ui_tree": {"session": SESSION},
    "linux_ui_click": {"session": SESSION, "target": "#1"},
    "linux_ui_type": {"session": SESSION, "text": "hi"},
    "linux_ui_screenshot": {"session": SESSION},
    "linux_ui_stop": {"session": SESSION},
    "job_start": {"name": "j", "command": "sleep 1"},
    "job_poll": {"name": "j"},
    "vm_status": {"host": "kali"},
    "vm_stop": {"host": "kali"},                            # started -> job check over SSH
    "vm_start": {"host": "kali", "timeout": 1},             # stopped -> start -> wait for SSH
    "vm_revert": {"host": "kali", "timeout": 1},
    "vm_snapshot": {"action": "restore", "host": "kali", "name": "s1"},  # job check over SSH
}
UTM_STATE = {"vm_stop": "started", "vm_snapshot": "started", "vm_start": "stopped",
             "vm_revert": "stopped", "vm_status": "started"}
# Tools that never open an SSH connection (utmctl runs on this machine).
LOCAL_ONLY = {"vm_status"}


def _published():
    return sorted(t.name for t in asyncio.run(mcp_server.mcp.list_tools()))


def _library(name):
    for m in MODULES:
        fn = getattr(m, name, None)
        if callable(fn):
            return fn
    raise AssertionError(f"MCP tool {name} has no library function in {MODULES}")


def _mcp_text(name, args):
    res = asyncio.run(mcp_server.mcp.call_tool(name, args))
    if hasattr(res, "content"):
        blocks = list(res.content)
    elif isinstance(res, tuple):
        blocks = list(res[0])
    else:
        blocks = list(res)
    return "\n".join(getattr(b, "text", "") for b in blocks)


@pytest.fixture
def lab(monkeypatch, tmp_path):
    """No sleeps, an empty SSH pool, utmctl faked, utm's state files in tmp_path. Returns
    a setter for the exception the SSH connect point raises, and the list of connects."""
    state = {"exc": OSError(64, "Host is down"), "utm": "started", "utm_exc": None}
    connects = []

    def connect(self, hostname, *a, **k):
        connects.append(hostname)
        raise state["exc"]

    def run_utmctl(exe, args, timeout):
        if state["utm_exc"] is not None:
            raise state["utm_exc"]
        if args[:1] == ["status"]:
            return 0, state["utm"]
        if args[:1] == ["--help"]:
            return 0, "snapshot  manage snapshots"
        return 0, ""

    monkeypatch.setattr(paramiko.SSHClient, "connect", connect)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(remote, "_POOL", {})
    monkeypatch.setattr(utm, "utmctl_path", lambda: "/fake/utmctl")
    monkeypatch.setattr(utm, "_run_utmctl", run_utmctl)
    monkeypatch.setattr(utm, "_vm", lambda host, tool, vm=None: ("kali", "Kali-Test-VM", None))
    monkeypatch.setattr(utm, "_registry", lambda: {"Kali-Test-VM": ["s1"]})
    monkeypatch.setattr(utm, "_SESSIONS", str(tmp_path / "utm-sessions.json"))
    monkeypatch.setattr(utm, "_REGISTRY", str(tmp_path / "utm-snapshots.json"))
    monkeypatch.setenv("LABHAND_DOWNLOAD_DIR", str(tmp_path / "dl"))
    return state, connects


def _args(name, tmp_path):
    a = dict(ARGS[name])
    if name == "download_file":
        a["dest"] = str(tmp_path / "x.bin")
    return a


def test_conn_fail_every_published_tool_is_wrapped_and_covered():
    """A new MCP tool needs an ARGS entry here (so (a) and (c) exercise it) and the
    public_tool decorator on its library function."""
    assert set(_published()) == set(ARGS)
    unwrapped = [n for n in _published() if not getattr(_library(n), "__labhand_tool__", False)]
    assert unwrapped == [], f"not wrapped by labhand._result.public_tool: {unwrapped}"


# --- (a) every published tool, SSH connect failing ------------------------------------
@pytest.mark.parametrize("name", _published())
def test_conn_fail_every_published_tool_returns_ok_false(name, lab, tmp_path):
    state, connects = lab
    state["utm"] = UTM_STATE.get(name, "started")
    fn = _library(name)
    r = fn(**_args(name, tmp_path))                       # must not raise
    assert isinstance(r, VMResult)
    # a tool that already reports its own failure must still sit behind the wrapper
    assert getattr(fn, "__labhand_tool__", False), f"{name} is not wrapped by public_tool"
    if name in LOCAL_ONLY:
        assert connects == []
        return
    assert connects, f"{name}: the call never reached the SSH connect point (fix ARGS)"
    assert r.ok is False, (name, r.summary)
    assert r.summary.startswith(name), r.summary
    assert "Host is down" in r.summary + r.detail, r.summary
    host = ARGS[name].get("host") or ("windows" if name.startswith("win_") else "kali")
    assert host in r.summary, f"{name}: summary does not name the host: {r.summary}"
    # the same through the MCP server: no exception, the failure comes back as text
    connects.clear()
    remote._POOL.clear()
    text = _mcp_text(name, _args(name, tmp_path))
    assert text.startswith(name), text[:200]
    assert connects


@pytest.mark.parametrize("exc", [
    OSError(64, "Host is down"), TimeoutError("timed out"), EOFError(),
    paramiko.SSHException("Error reading SSH protocol banner"),
    paramiko.ssh_exception.NoValidConnectionsError({("192.0.2.1", 22): OSError(61, "refused")}),
], ids=lambda e: e.__class__.__name__)
def test_conn_fail_run_cmd_summary_names_tool_host_error(exc, lab, tmp_path):
    state, _ = lab
    state["exc"] = exc
    r = shell.run_cmd("ver", host="windows")
    assert r.ok is False
    assert r.summary.startswith(f"run_cmd on windows: {exc.__class__.__name__}"), r.summary
    assert not r.summary.endswith(": "), r.summary
    assert "Traceback" in r.detail and "connect" in r.detail
    assert r.image is None and r.waiting is False


def test_conn_fail_default_host_is_named(lab):
    r = linux.linux_capa("a.out")                          # host omitted: first linux host
    assert r.summary.startswith("linux_capa on kali: OSError: [Errno 64] Host is down"), r.summary


def test_conn_fail_retry_ladder_still_runs(lab):
    """The pooled-SSH retry ladder is untouched: a blip is retried before it fails."""
    _, connects = lab
    shell.run_cmd("uname -a")
    assert len(connects) == 3          # remote._client: 3 attempts per connect


# --- (b) the SFTP session breaks in the middle of a call ------------------------------
class _BreakingSFTP:
    """The first file write works, every later one hits a dropped session."""
    def __init__(self, log):
        self.log = log

    def mkdir(self, p):
        raise IOError("exists")

    def symlink(self, a, b):
        raise IOError("exists")

    def file(self, path, mode="r"):
        self.log.append(path)
        if len(self.log) > 1:
            raise EOFError()
        sftp = self

        class _F:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def write(self, data):
                pass

            def read(self):
                return b""
        return _F()

    def remove(self, p):
        raise EOFError()

    def close(self):
        pass


class _Transport:
    def is_active(self):
        return True

    def set_keepalive(self, n):
        pass


class _Client:
    def __init__(self, log):
        self.log = log

    def get_transport(self):
        return _Transport()

    def open_sftp(self):
        return _BreakingSFTP(self.log)

    def exec_command(self, *a, **k):
        raise paramiko.SSHException("SSH session not active")

    def close(self):
        pass


@pytest.mark.parametrize("call,host", [
    (lambda: shell.run_cmd("ver", host="windows"), "windows"),
    (lambda: shell.run_script("echo hi", host="kali"), "kali"),
    (lambda: linux.linux_gdb("a.out", "run()", host="kali"), "kali"),
], ids=["run_cmd-windows", "run_script-kali", "linux_gdb-kali"])
def test_conn_fail_sftp_breaks_mid_call(call, host, monkeypatch):
    log = []
    client = _Client(log)
    monkeypatch.setattr(remote, "_get", lambda h=None, attempts=3: client)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    r = call()                                             # must not raise
    assert isinstance(r, VMResult) and r.ok is False, r
    assert f" on {host}: " in r.summary, r.summary
    assert "EOFError" in r.summary or "SSHException" in r.summary, r.summary
    assert len(log) >= 1                                   # it was mid-call: a write had happened


# --- (c) KeyboardInterrupt / SystemExit are not swallowed -----------------------------
@pytest.mark.parametrize("name", _published())
def test_conn_fail_keyboard_interrupt_propagates(name, lab, tmp_path):
    state, _ = lab
    state["utm"] = UTM_STATE.get(name, "started")
    state["exc"] = KeyboardInterrupt()
    if name in LOCAL_ONLY:
        state["utm_exc"] = KeyboardInterrupt()
    fn = _library(name)
    assert getattr(fn, "__labhand_tool__", False), f"{name} is not wrapped by public_tool"
    with pytest.raises(KeyboardInterrupt):
        fn(**_args(name, tmp_path))


def test_conn_fail_system_exit_propagates(lab):
    state, _ = lab
    state["exc"] = SystemExit(3)
    assert getattr(shell.run_cmd, "__labhand_tool__", False)
    with pytest.raises(SystemExit):
        shell.run_cmd("ver", host="windows")


# --- the wrapper itself ---------------------------------------------------------------
def test_conn_fail_wrapper_passes_results_through_and_is_idempotent():
    from labhand._result import public_tool
    waiting = VMResult(True, "job running", waiting=True)
    image = VMResult(True, "shot", image={"media_type": "image/png", "data": "AA=="})
    failed = VMResult(False, "win_frida: give `program`")
    for res in (waiting, image, failed):
        @public_tool
        def tool(host=None, _res=res):
            return _res
        assert tool() is res                                # same object, untouched
        assert public_tool(tool) is tool                    # no double wrap

    @public_tool
    def boom(x, host=None):
        raise ValueError("bad\nvalue")
    r = boom(1, host="kali")
    assert r.ok is False and r.summary == "boom on kali: ValueError: bad value"
    assert "ValueError" in r.detail and "Traceback" in r.detail


def test_conn_fail_wrapped_signature_is_kept():
    import inspect
    assert list(inspect.signature(shell.run_cmd).parameters) == ["command", "host", "timeout"]
    assert shell.run_cmd.__name__ == "run_cmd"
    assert windows.win_windows.__doc__ == windows.win_windows.__wrapped__.__doc__
