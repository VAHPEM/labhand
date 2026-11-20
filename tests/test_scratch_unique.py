# -*- coding: utf-8 -*-
"""Per-call scratch files (a script, a command file, a launcher, a config, a screenshot, the
session-1 scheduled task) must be unique per CALL and removed after it, on every path.

Found 2026-11-20 from the code: several tools wrote to a FIXED name in the host workdir
(`_script.sh`, `_win_cmd.txt`, `_timeout_run.py`, `exploit.py`, `_gdb_user.py`, `_frida.js`,
`_gui_task.ps1`, `_screen.png`, `_xshot.png`, `_viewconv_in`, ...) and then executed or read
it. ccbridge runs several labhand MCP servers at once, so two calls on one host raced: B's
write landed between A's write and A's exec, and A ran (and reported) B's script.

VM-free: SFTP is a fake in-memory filesystem per host (so `write_remote`, `read_bytes` and
the cleanup run as real code), `ssh_exec` is a recorder that also checks what the remote
command would see at exec time. Each test runs the tool twice and asserts two different
names, that the reader got the real name, and that nothing per-call is left afterwards, on
success, on a timeout and on a connection failure.
"""
import json
import os
import re
import subprocess
import sys

import pytest

from labhand import images, linux, remote, shell, windows

WD = remote._wd()
WINHOME = "C:\\Users\\test"
WINS = WINHOME + "\\" + remote._wdw()
# one scratch name: _lh_<stem>_<time_ns>_<pid>_<rand8>[.ext]
SCRATCH = re.compile(r"_lh_[A-Za-z0-9._-]*?_\d{10,}_\d+_[0-9a-f]{8}(?:\.[A-Za-z0-9]+)?")
TOKEN = re.compile(r"\d{10,}_\d+_[0-9a-f]{8}")


# ----------------------------------------------------------------------------- fakes
class _File:
    def __init__(self, fs, path, mode):
        self.fs, self.path, self.mode = fs, path, mode
        if "r" in mode and path not in fs.files:
            raise IOError(2, "No such file", path)
        self.buf = []

    def write(self, data):
        self.buf.append(data.encode() if isinstance(data, str) else bytes(data))

    def read(self, *a):
        return self.fs.files[self.path]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        if "w" in self.mode:
            self.fs.files[self.path] = b"".join(self.buf)
            self.fs.written.append(self.path)


class FakeSFTP:
    """The few SFTP calls labhand makes, over a dict path -> bytes."""

    def __init__(self):
        self.files, self.written, self.removed = {}, [], []

    def file(self, path, mode="r"):
        return _File(self, path, mode)

    open = file

    def remove(self, path):
        if path not in self.files:
            raise IOError(2, "No such file", path)
        del self.files[path]
        self.removed.append(path)

    def mkdir(self, p):
        raise IOError("exists")

    def symlink(self, a, b):
        raise IOError("exists")

    def close(self):
        pass

    def scratch_left(self):
        return sorted(p for p in self.files if SCRATCH.search(p.rsplit("/", 1)[-1]))

    def text(self, name):
        return self.files[f"{WD}/{name}"].decode()

    def has(self, name):
        return f"{WD}/{name}" in self.files


class Lab:
    """Per-host fake filesystems + an ssh_exec recorder with a per-test responder."""

    def __init__(self, monkeypatch):
        self.fs = {"kali": FakeSFTP(), "windows": FakeSFTP()}
        self.cmds = []
        self.respond = lambda host, cmd: ("", "")
        monkeypatch.setattr(remote, "_sftp_op",
                            lambda host, fn, **kw: fn(self.fs[remote._key(host)]))
        monkeypatch.setattr(remote, "ssh_exec", self._ssh)
        monkeypatch.setitem(remote._WIN_HOME, "windows", WINHOME)

    def _ssh(self, cmd, host=None, read_timeout=None):
        host = remote._key(host)
        self.cmds.append((host, cmd))
        return self.respond(host, cmd)


@pytest.fixture
def lab(monkeypatch):
    return Lab(monkeypatch)


def _names(text):
    return [m.group(0) for m in SCRATCH.finditer(text)]


def _boom(*a, **k):
    raise EOFError("connection lost")


# ----------------------------------------------------------------- the shared helper
def test_scratch_unique_name_helper_shape_and_uniqueness():
    names = {remote.scratch_name("script", "sh") for _ in range(2000)}
    assert len(names) == 2000
    for n in list(names)[:50]:
        assert SCRATCH.fullmatch(n), n
        assert re.fullmatch(r"[A-Za-z0-9._-]+", n)      # no quoting needed anywhere
    assert remote.scratch_name("a/b c;$(x)", "p y").startswith("_lh_a_b_c___x_")
    assert TOKEN.fullmatch(remote.scratch_token())


def test_scratch_unique_two_real_processes():
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from labhand import remote; "
            "print(remote.scratch_name('script', 'sh'))")
    root = os.path.dirname(os.path.dirname(os.path.abspath(remote.__file__)))
    procs = [subprocess.Popen([sys.executable, "-c", code, root], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(2)]
    outs = []
    for p in procs:
        o, e = p.communicate(timeout=60)
        assert p.returncode == 0, e
        outs.append(o.strip())
    assert outs[0] != outs[1] and all(SCRATCH.fullmatch(o) for o in outs), outs


# ---------------------------------------------------------- shell.run_script (linux)
@pytest.mark.parametrize("kind,ext,runner", [("bash", "sh", "bash"), ("python3", "py", None)])
def test_scratch_unique_shell_run_script_linux(lab, kind, ext, runner):
    fs = lab.fs["kali"]
    seen = []

    def respond(host, cmd):
        (name,) = [n for n in _names(cmd) if n.endswith("." + ext)]
        seen.append((name, fs.text(name)))           # the file exists at exec time
        return f"ran {name}\n__RC=0", ""
    lab.respond = respond
    r1 = shell.run_script(f"echo one  # {kind}", host="kali", kind=kind)
    r2 = shell.run_script(f"echo two  # {kind}", host="kali", kind=kind)
    assert r1.ok and r2.ok
    assert seen[0][0] != seen[1][0]
    assert seen[0][1].startswith("echo one") and seen[1][1].startswith("echo two")
    if runner:
        assert f"{runner} {seen[0][0]}" in lab.cmds[0][1]
    assert fs.scratch_left() == []

    # timeout (rc 124) and a lost connection: the file is gone afterwards too
    lab.respond = lambda h, c: ("__RC=124", "")
    assert not shell.run_script("sleep 99", host="kali", kind=kind).ok
    lab.respond = _boom
    with pytest.raises(EOFError):
        shell.run_script("echo x", host="kali", kind=kind)
    assert fs.scratch_left() == []
    assert len({p for p in fs.written}) == len(fs.written) == 4


# -------------------------------------------- Windows: run_cmd / run_script launcher
def _win_launch(lab):
    """A responder playing the Windows launcher: parse `"py" "<launcher>" N "<cmdfile>"`,
    check both files exist and hold what they must, return the command it would run."""
    fs = lab.fs["windows"]
    seen = []

    def respond(host, cmd):
        m = re.fullmatch(r'"[^"]+" "(?P<run>[^"]+)" (?P<t>\d+) "(?P<cmd>[^"]+)"', cmd)
        assert m, cmd
        run = m["run"]
        assert run.startswith(WINS + "\\")
        run_name = run[len(WINS) + 1:]
        assert fs.text(run_name) == remote._WIN_LAUNCHER
        cmdline = fs.text(m["cmd"])
        seen.append((run_name, m["cmd"], cmdline))
        return f"{cmdline}\n__RC=0", ""
    return respond, seen


def test_scratch_unique_win_run_cmd(lab):
    respond, seen = _win_launch(lab)
    lab.respond = respond
    assert shell.run_cmd("echo one", host="windows").ok
    assert shell.run_cmd("echo two", host="windows").ok
    (r1, c1, l1), (r2, c2, l2) = seen
    assert r1 != r2 and c1 != c2 and SCRATCH.fullmatch(r1) and SCRATCH.fullmatch(c1)
    assert (l1, l2) == ("echo one", "echo two")
    assert lab.fs["windows"].scratch_left() == []
    lab.respond = lambda h, c: ("__TIMEOUT_KILLED__\n__RC=124", "")
    assert not shell.run_cmd("ping -n 99 x", host="windows").ok
    lab.respond = _boom
    with pytest.raises(EOFError):
        shell.run_cmd("echo x", host="windows")
    assert lab.fs["windows"].scratch_left() == []


@pytest.mark.parametrize("kind,ext", [("powershell", "ps1"), ("cmd", "bat"), ("python3", "py")])
def test_scratch_unique_win_run_script(lab, kind, ext):
    fs = lab.fs["windows"]
    respond, seen = _win_launch(lab)
    scripts = []

    def resp(host, cmd):
        o = respond(host, cmd)
        (name,) = [n for n in _names(seen[-1][2]) if n.endswith("." + ext)]
        scripts.append((name, fs.text(name)))
        return o
    lab.respond = resp
    assert shell.run_script("Write-Output 1", host="windows", kind=kind).ok
    assert shell.run_script("Write-Output 2", host="windows", kind=kind).ok
    assert scripts[0][0] != scripts[1][0]
    assert [s for _, s in scripts] == ["Write-Output 1", "Write-Output 2"]
    lab.respond = _boom
    with pytest.raises(EOFError):
        shell.run_script("x", host="windows", kind=kind)
    assert fs.scratch_left() == []


def test_scratch_unique_win_launcher_reads_the_named_command_file(tmp_path):
    """The real launcher, run locally: it must run the command file named in argv[2]
    (not a fixed `_win_cmd.txt`) from its own directory."""
    launcher = remote.scratch_name("winrun", "py")
    cmdfile = remote.scratch_name("wincmd", "txt")
    (tmp_path / launcher).write_text(remote._WIN_LAUNCHER, encoding="utf-8")
    (tmp_path / cmdfile).write_text("echo launched-ok", encoding="utf-8")
    (tmp_path / "_win_cmd.txt").write_text("echo WRONG-FIXED-FILE", encoding="utf-8")
    p = subprocess.run([sys.executable, str(tmp_path / launcher), "20", cmdfile],
                       capture_output=True, text=True, timeout=60, cwd=str(tmp_path.parent))
    assert "launched-ok" in p.stdout and "WRONG" not in p.stdout, (p.stdout, p.stderr)
    assert "__RC=0" in p.stdout


# --------------------------------------------------------------- remote.run_script
@pytest.mark.parametrize("host", ["kali", "windows"])
def test_scratch_unique_remote_run_script(lab, host):
    fs = lab.fs[host]
    seen = []
    if host == "windows":
        respond, wseen = _win_launch(lab)

        def resp(h, cmd):
            o = respond(h, cmd)
            (n,) = [x for x in _names(wseen[-1][2]) if "exploit" in x]
            seen.append((n, fs.text(n)))
            return o
    else:
        def resp(h, cmd):
            (n,) = [x for x in _names(cmd) if "exploit" in x]
            seen.append((n, fs.text(n)))
            return "ok\n__RC=0", ""
    lab.respond = resp
    remote.run_script("print(1)", host=host)
    remote.run_script("print(2)", host=host)
    assert seen[0][0] != seen[1][0] and not fs.has("exploit.py")
    assert [s for _, s in seen] == ["print(1)", "print(2)"]
    lab.respond = _boom
    with pytest.raises(EOFError):
        remote.run_script("print(3)", host=host)
    assert fs.scratch_left() == []


# ------------------------------------------------------------------------ linux_gdb
def test_scratch_unique_linux_gdb(lab):
    fs = lab.fs["kali"]
    seen = []

    def respond(host, cmd):
        run = re.search(r"-x '?(_lh_gdbrun_[^ ']+)", cmd).group(1)
        body = fs.text(run)
        # the prelude opens the per-call user script by its real name
        user = re.search(r"with open\('(_lh_gdbuser_[^']+)', 'r'\)", body).group(1)
        seen.append((run, user, fs.text(user)))
        return "hello\n__GDB_RC=0", ""
    lab.respond = respond
    assert linux.linux_gdb("a.out", "log('one')", host="kali").ok
    assert linux.linux_gdb("a.out", "log('two')", host="kali").ok
    (r1, u1, s1), (r2, u2, s2) = seen
    assert r1 != r2 and u1 != u2 and (s1, s2) == ("log('one')", "log('two')")
    assert not fs.has("_gdb_user.py") and not fs.has("_gdb_run.py")
    lab.respond = lambda h, c: ("__GDB_RC=124", "")
    assert not linux.linux_gdb("a.out", "run()", host="kali").ok
    lab.respond = _boom
    with pytest.raises(EOFError):
        linux.linux_gdb("a.out", "run()", host="kali")
    assert fs.scratch_left() == []


# -------------------------------------------------------------------- linux_gui_run
def test_scratch_unique_linux_gui_run(lab):
    fs = lab.fs["kali"]
    shots = []

    def respond(host, cmd):
        (shot,) = [n for n in _names(cmd) if n.endswith(".png")]
        shots.append(shot)
        fs.files[f"{WD}/{shot}"] = b"not really a png"     # `import` wrote it
        return "XSHOT_OK\n__RC=0", ""
    lab.respond = respond
    linux.linux_gui_run("xterm", host="kali")
    linux.linux_gui_run("xterm", host="kali")
    assert shots[0] != shots[1] and fs.scratch_left() == []
    lab.respond = lambda h, c: ("__RC=124", "")                 # no screenshot
    assert not linux.linux_gui_run("xterm", host="kali").ok
    assert fs.scratch_left() == []


# --------------------------------------------- Windows session 1 (the GUI task path)
def _session1(lab, out_text="SCREEN 10x10 -> x"):
    """A responder playing the session-1 driver: read the driver file, check it names the
    per-call task file / out / done files and task name, create the screenshot the body
    asks for, and print what the task wrote."""
    fs = lab.fs["windows"]
    seen = []

    def respond(host, cmd):
        if cmd.startswith("if not exist") or cmd.startswith("schtasks"):
            return "", ""
        drv = re.search(r'-File "([^"]+)"', cmd).group(1)
        drv_name = drv[len(WINS) + 1:]
        body = fs.text(drv_name)
        task = re.search(r'\$T = "([^"]+)"', body).group(1)
        taskfile = re.search(r"-File \$S\\(_lh_guitask_\S+\.ps1)", body).group(1)
        out = re.search(r'\$OUT = "\$S\\(_lh_guiout_[^"]+)"', body).group(1)
        done = re.search(r'\$DONE = "\$S\\(_lh_guidone_[^"]+)"', body).group(1)
        tbody = fs.text(taskfile)
        assert f"$S\\{out}" in tbody and f"$S\\{done}" in tbody
        shot = re.search(r'Save\("\$S\\(_lh_screen_[^"]+)"', tbody)
        if shot:
            fs.files[f"{WD}/{shot.group(1)}"] = b"png?"
        fs.files[f"{WD}/{out}"] = b"out"                      # what the task left
        fs.files[f"{WD}/{done}"] = b"done"
        seen.append(dict(task=task, drv=drv_name, taskfile=taskfile, out=out, done=done,
                         shot=shot.group(1) if shot else None))
        return out_text + "\n__GUI_END__", ""
    return respond, seen


def test_scratch_unique_run_in_session1(lab):
    fs = lab.fs["windows"]
    respond, seen = _session1(lab, "hello")
    lab.respond = respond
    assert remote.run_in_session1("Write-Output 1", host="windows") == ("hello", False)
    assert remote.run_in_session1("Write-Output 2", host="windows", task="labhand_key")[0] == "hello"
    a, b = seen
    for k in ("task", "drv", "taskfile", "out", "done"):
        assert a[k] != b[k], k
    assert TOKEN.search(a["task"]) and a["task"].startswith("labhand_gui_")
    assert b["task"].startswith("labhand_key_")
    assert fs.scratch_left() == []
    assert not any(c.startswith("schtasks") for _, c in lab.cmds)   # the driver deleted it

    # connection lost while the driver runs: files removed AND the task deleted from here
    calls = []

    def lost(host, cmd):
        calls.append(cmd)
        if "-File" in cmd:
            respond(host, cmd)
            raise EOFError("connection lost")
        return "", ""
    lab.respond = lost
    with pytest.raises(EOFError):
        remote.run_in_session1("Write-Output 3", host="windows")
    task = seen[-1]["task"]
    assert any(c.startswith("schtasks /end") and f'/tn "{task}"' in c
               and f'schtasks /delete /tn "{task}" /f' in c for c in calls), calls
    assert fs.scratch_left() == []

    # the driver's SSH read timed out (no __GUI_END__): same cleanup
    lab.respond = lambda h, c: ("", "(TIMEOUT after 105s - ...)") if "-File" in c else ("", "")
    remote.run_in_session1("Start-Sleep 999", host="windows", timeout=60)
    assert any(c.startswith("schtasks /end") for _, c in lab.cmds[-2:])
    assert fs.scratch_left() == []


def test_scratch_unique_win_screenshot_and_gui_run(lab):
    fs = lab.fs["windows"]
    respond, seen = _session1(lab)
    lab.respond = respond
    windows.win_screenshot(host="windows")
    windows.win_screenshot(host="windows")
    windows.win_gui_run("C:\\x.exe", host="windows")
    shots = [s["shot"] for s in seen]
    assert len(set(shots)) == 3 and all(SCRATCH.fullmatch(s) for s in shots)
    assert not fs.has("_screen.png")
    assert fs.scratch_left() == []
    lab.respond = _boom
    assert not windows.win_screenshot(host="windows").ok      # VMResult(ok=False), no raise
    assert not windows.win_gui_run("C:\\x.exe", host="windows").ok
    assert fs.scratch_left() == []


# ------------------------------------------------------------------------ win_frida
def test_scratch_unique_win_frida_no_desktop(lab):
    fs = lab.fs["windows"]
    seen = []

    def respond(host, cmd):
        m = re.fullmatch(r'"[^"]+" "(?P<drv>[^"]+)" "(?P<cfg>[^"]+)"', cmd)
        assert m, cmd
        drv = m["drv"][len(WINS) + 1:]
        assert fs.text(drv) == windows._FRIDA_DRIVER
        cfg = json.loads(fs.text(m["cfg"]))
        js = cfg["js_path"][len(WINS) + 1:]
        src = fs.text(js)
        log = re.search(r"var LOG_PATH='([^']+)'", src).group(1).rsplit("/", 1)[-1]
        fs.files[f"{WD}/{log}"] = b"logged line\n"          # the hook's log() wrote it
        seen.append(dict(drv=drv, cfg=m["cfg"], js=js, log=log, src=src))
        return "FRIDA_VERSION=17\nSCRIPT_LOADED=yes\nSEND_COUNT=1\n__FRIDA_END__", ""
    lab.respond = respond
    r1 = windows.win_frida("send('one')", pid=1234, host="windows")
    r2 = windows.win_frida("send('two')", pid=1234, host="windows")
    assert r1.ok and r2.ok and "logged line" in r1.detail
    a, b = seen
    for k in ("drv", "cfg", "js", "log"):
        assert a[k] != b[k] and SCRATCH.fullmatch(a[k]), k
    assert a["src"].endswith("send('one')") and b["src"].endswith("send('two')")
    assert fs.scratch_left() == []
    assert not any(c.startswith("del ") for _, c in lab.cmds)  # no stale-log delete needed
    lab.respond = _boom
    assert not windows.win_frida("send(1)", pid=1234, host="windows").ok
    lab.respond = lambda h, c: ("FRIDA_VERSION=17\nSCRIPT_LOADED=yes", "(TIMEOUT after 120s)")
    assert not windows.win_frida("send(1)", pid=1234, host="windows").ok
    assert fs.scratch_left() == []


def test_scratch_unique_win_frida_drive_and_desktop(lab):
    fs = lab.fs["windows"]
    respond, seen = _session1(lab, "SCRIPT_LOADED=yes\nSEND_COUNT=1\n__FRIDA_END__")
    frida_cmds = []

    def resp(host, cmd):
        o = respond(host, cmd)
        if seen and "-File" in cmd:
            tbody = fs.text(seen[-1]["taskfile"])
            m = re.search(r'& "[^"]+" "(?P<drv>[^"]+)" "(?P<cfg>[^"]+)" 2>&1', tbody)
            cfg = json.loads(fs.text(m["cfg"]))
            assert fs.has(cfg["drive_ps1"][len(WINS) + 1:])
            frida_cmds.append((m["cfg"], cfg["drive_ps1"]))
        return o
    lab.respond = resp
    windows.win_frida("send(1)", pid=1234, drive="OK", host="windows")
    windows.win_frida("send(1)", pid=1234, drive="OK", host="windows")
    assert frida_cmds[0] != frida_cmds[1]
    assert seen[0]["task"].startswith("labhand_frida_") and seen[0]["task"] != seen[1]["task"]
    assert fs.scratch_left() == []


def test_scratch_unique_frida_driver_reads_the_named_config(tmp_path):
    """The real driver, run locally: it must load the config named in argv[1]."""
    drv = remote.scratch_name("frida_drv", "py")
    cfg = remote.scratch_name("frida_cfg", "json")
    (tmp_path / drv).write_text(windows._FRIDA_DRIVER, encoding="utf-8")
    (tmp_path / cfg).write_text(json.dumps({"js_path": str(tmp_path / "none.js"),
                                            "max_lines": 5}), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH="")
    p = subprocess.run([sys.executable, "-I", str(tmp_path / drv), cfg], capture_output=True,
                       text=True, timeout=60, cwd=str(tmp_path.parent), env=env)
    out = p.stdout + p.stderr
    assert "_frida_cfg.json" not in out and "FileNotFoundError" not in out.split("none.js")[0], out
    assert ("FRIDA_MISSING" in out) or ("FRIDA_VERSION" in out), out


# ---------------------------------------------------- images: ImageMagick conversion
def test_scratch_unique_image_conversion(lab, monkeypatch):
    fs = lab.fs["kali"]
    monkeypatch.setitem(sys.modules, "PIL", None)               # force the no-Pillow path
    seen = []

    def respond(host, cmd):
        src, dst = _names(cmd)
        assert fs.text(src) == "BM-raw"
        fs.files[f"{WD}/{dst}"] = b"\x89PNG converted"
        seen.append((src, dst))
        return "", ""
    lab.respond = respond
    m1 = images._prepare_image(b"BM-raw", "bmp")
    m2 = images._prepare_image(b"BM-raw", "bmp")
    assert m1[0] == m2[0] == "image/png"
    assert seen[0][0] != seen[1][0] and seen[0][1] != seen[1][1]
    assert fs.scratch_left() == []
    lab.respond = _boom
    assert images._prepare_image(b"BM-raw", "bmp")[0] is None
    assert fs.scratch_left() == []


# ------------------------------------------- shell: scratch files are not "new files"
def test_scratch_unique_not_reported_as_written_files():
    text = ("out\n__NEWFILE__ _lh_script_1763650000123456789_42_9f3a1c0e.sh\t10\n"
            "__NEWFILE__ result.bin\t5\n")
    clean, found = shell._peel_newfiles(text)
    assert found == [("result.bin", 5)]


def test_scratch_unique_cleanup_is_one_quick_attempt(monkeypatch):
    """Cleanup on a dead host: one connect attempt, never an exception."""
    tries = []

    def client(addr, user, attempts=3):
        tries.append(attempts)
        raise EOFError("unreachable")
    monkeypatch.setattr(remote, "_POOL", {})
    monkeypatch.setattr(remote, "_client", client)
    remote.remove_quiet(["_lh_x_1763650000123456789_1_0123abcd.sh"], host="kali")
    assert tries == [1]


# ------------------------------- Windows: the generated cmd.exe / PowerShell text itself
SAFE = re.compile(r"[A-Za-z0-9._-]+")      # no cmd.exe / PowerShell metacharacter possible


def test_scratch_unique_windows_generated_text_quotes_and_deletes(lab):
    """What Windows actually receives, checked as text (no Windows VM in the unit tests):
    every per-call name is metacharacter-free, double-quoted on the cmd.exe line and in the
    PowerShell bodies, and every per-call file is deleted (SFTP) and the task deleted."""
    fs = lab.fs["windows"]
    # run_cmd: launcher and command file both double-quoted on the cmd.exe line
    lab.respond = lambda h, c: ("x\n__RC=0", "")
    shell.run_cmd("echo q", host="windows")
    (_, line), = [hc for hc in lab.cmds if hc[0] == "windows"][-1:]
    m = re.fullmatch(r'"[^"]+" "(?P<run>[^"]+)" \d+ "(?P<cmd>[^"]+)"', line)
    assert m, line
    run_name = m["run"][len(WINS) + 1:]
    assert SAFE.fullmatch(run_name) and SAFE.fullmatch(m["cmd"])
    assert {f"{WD}/{run_name}", f"{WD}/{m['cmd']}"} <= set(fs.removed)

    # session 1: driver + task wrapper text
    bodies = {}

    def resp(host, cmd):
        if "-File" in cmd:
            drv = re.search(r'-File "([^"]+)"', cmd).group(1)[len(WINS) + 1:]
            bodies["drv_name"], bodies["drv"] = drv, fs.text(drv)
            tf = re.search(r"-File \$S\\(\S+\.ps1)", bodies["drv"]).group(1)
            bodies["task_name"], bodies["task"] = tf, fs.text(tf)
            return "__GUI_END__", ""
        return "", ""
    lab.respond = resp
    remote.run_in_session1("Write-Output 1", host="windows", task="my task!")
    d, t = bodies["drv"], bodies["task"]
    task = re.search(r'^\$T = "([^"]+)"$', d, re.M).group(1)
    out = re.search(r'^\$OUT = "\$S\\([^"]+)"$', d, re.M).group(1)
    done = re.search(r'^\$DONE = "\$S\\([^"]+)"$', d, re.M).group(1)
    for n in (task, out, done, bodies["task_name"], bodies["drv_name"]):
        assert SAFE.fullmatch(n), n
    assert task.startswith("my_task__") and TOKEN.search(task)
    assert "Remove-Item -Force -ErrorAction SilentlyContinue $OUT,$DONE" in d
    assert "schtasks /create /tn $T " in d and "schtasks /delete /tn $T /f" in d
    assert f'Out-File -FilePath "$S\\{out}"' in t and f'Out-File -FilePath "$S\\{done}"' in t
    for n in (out, done, bodies["task_name"], bodies["drv_name"]):
        assert f"{WD}/{n}" in fs.removed or not fs.has(n), n
    # the fallback (driver did not finish) deletes the task quoted on the cmd.exe line
    lab.respond = lambda h, c: ("", "(TIMEOUT)") if "-File" in c else ("", "")
    remote.run_in_session1("x", host="windows")
    last = lab.cmds[-1][1]
    assert re.fullmatch(r'schtasks /end /tn "[^"]+" >nul 2>&1 & '
                        r'schtasks /delete /tn "[^"]+" /f >nul 2>&1', last), last

    # screenshot: the per-call name inside the double-quoted Save() path
    body = windows._ps_screenshot(remote.scratch_name("screen", "png"))
    shot = re.search(r'\$bmp\.Save\("\$S\\([^"]+)"', body).group(1)
    assert SAFE.fullmatch(shot) and SCRATCH.fullmatch(shot) and "_screen.png" not in body

    # win_frida without desktop: driver path and config name double-quoted
    lab.respond = lambda h, c: ("SCRIPT_LOADED=yes\n__FRIDA_END__", "")
    windows.win_frida("send(1)", pid=1, host="windows")
    line = lab.cmds[-1][1]
    m = re.fullmatch(r'"[^"]+" "(?P<drv>[^"]+)" "(?P<cfg>[^"]+)"', line)
    assert m and SAFE.fullmatch(m["cfg"]) and SAFE.fullmatch(m["drv"][len(WINS) + 1:]), line
    assert fs.scratch_left() == []
