# -*- coding: utf-8 -*-
"""labhand.linux — Linux-host tools: a GUI program under Xvfb, scripted gdb, capa / YARA / YARA-X / Volatility 3, Ghidra headless (raw output)."""
import base64, re, shlex, uuid
from . import remote, hosts
from .hosts import workdir as _wd, workdir_win as _wdw  # noqa: F401
from ._result import VMResult, _spill, _find_match  # noqa
from .safety import _security_block
from .images import _prepare_image
from .shell import _run_and_wrap, _wrap_cmd, _peel_rc, _peel_newfiles


def linux_gui_run(command, wait=5, screen="1280x800x24", timeout=60, host=None):
    h, err = hosts.check(host, "linux_gui_run", "linux")
    if err:
        return err
    host = h.name
    blocked = _security_block(command)
    if blocked:
        return VMResult(False, f"blocked: this command touches {blocked}")
    # per-call screenshot file, removed after the call (a fixed `_xshot.png` let two
    # labhand processes read each other's screenshot, found 2026-11-20)
    shot = remote.scratch_name("xshot", "png")
    inner = (f"{command} & sleep {int(wait)}; "
             "(xdotool search --onlyvisible --name '.' getwindowname %@ 2>/dev/null "
             "| sed 's/^/XWIN /' | head -40); "
             f"import -window root {shlex.quote(shot)} 2>&1 && echo XSHOT_OK")
    cmd = ("xvfb-run -a -s " + shlex.quote("-screen 0 " + str(screen)) + " bash -c "
           + shlex.quote(inner))
    try:
        res = _run_and_wrap(cmd, host, timeout, f"xvfb({command[:40]})")
        if "XSHOT_OK" not in (res.detail or ""):
            return VMResult(False, "linux_gui_run: no screenshot was produced - "
                                     f"output: {(res.detail or '')[:400]}")
        try:
            raw = remote.read_bytes(f"{_wd()}/{shot}", host=host)
        except Exception as e:  # noqa: BLE001
            return VMResult(False, f"linux_gui_run: could not read the screenshot back ({e})")
    finally:
        remote.remove_quiet([shot], host=host)
    media, b64, note = _prepare_image(raw, "png")
    if not media:
        return VMResult(False, f"linux_gui_run: screenshot unusable ({note})")
    return VMResult(True, f"ran {command[:40]} under Xvfb{note}; picture on your next turn",
                      # res.detail already came through _spill in _run_and_wrap:
                      # head + pointer + tail. Truncating it AGAIN here could cut the
                      # pointer off the end and strand the full output.
                      detail=(res.detail or ""),
                      known=[f"linux_gui_run({command[:40]}) -> {(res.detail or '')[:300]}"],
                      image={"media_type": media, "data": b64})


# --- Linux: gdb, the scriptable debugger --------------------------------------------
# The Linux twin of win_frida. FLARE-On ch9 (neon_outrun, 2026-09-27) needed it and had
# no tool: a 20MB Tauri/Rust/V8 desktop app whose real logic (a script decrypted on the
# heap, and three V8 built-ins - sort/Math.min/replaceAll - OVERRIDDEN by obfuscated
# native code) is only reachable at RUNTIME. Static r2/Ghidra reading stalled; the flag
# came from breaking in gdb, dumping the decrypted script, and measuring the native
# functions as black boxes. win_frida is Windows-only, linux_gui_run only screenshots.
# This runs a gdb-python script the Brain writes, on Kali, under Xvfb (so a GUI/webkit
# binary can actually start), with ASLR disabled so a PIE always loads at the SAME base.
# MEASURED in-cloud on the real ch9 binary (2026-09-27): with `set disable-randomization
# on`, the PIE base is a STABLE 0x555555554000 every run - so the Brain can hardcode
# base+RVA without leaking it first. Deterministic, $0, raw output, no interpretation.

# The prelude is prepended to every run: it silences the debugger's own noise, keeps gdb
# on the parent across webkit's fork()s, and hands the Brain the small set of helpers
# that actually solved ch9 (read/write inferior memory, read a register, base+RVA).
_GDB_PRELUDE = r'''# gdb prelude (muscle-side; no interpretation - just plumbing + helpers)
import gdb, struct, sys, re as _re

def _flush(s):
    sys.stdout.write(s); sys.stdout.flush()

def log(*a):
    # one raw line to captured stdout; survives even from inside a Breakpoint.stop()
    _flush(" ".join(str(x) for x in a) + "\n")

gdb.execute("set pagination off")
gdb.execute("set confirm off")
gdb.execute("set disable-randomization on")   # PIE base is then a stable 0x555555554000
gdb.execute("set breakpoint pending on")      # allow hooks set before the inferior loads
gdb.execute("set follow-fork-mode parent")    # webkit forks helper procs - stay on main
gdb.execute("set detach-on-fork on")
for _s in ("SIGSEGV", "SIGUSR1", "SIGUSR2", "SIGPIPE", "SIGCHLD"):
    try:
        gdb.execute("handle %s nostop noprint pass" % _s)
    except gdb.error:
        pass

_INF = gdb.selected_inferior
def rd(addr, n):
    return bytes(_INF().read_memory(int(addr), int(n)))
def wr(addr, data):
    _INF().write_memory(int(addr), bytes(data))
def u64(addr): return struct.unpack("<Q", rd(addr, 8))[0]
def u32(addr): return struct.unpack("<I", rd(addr, 4))[0]
def f64(addr): return struct.unpack("<d", rd(addr, 8))[0]
def reg(name): return int(gdb.parse_and_eval("$" + str(name)))
def cstr(addr, maxn=256):
    out = bytearray()
    a = int(addr)
    while len(out) < maxn:
        b = rd(a, 1)
        if b == b"\x00": break
        out += b; a += 1
    return out.decode("utf-8", "replace")

_BASE = [None]
def base():
    # load base of the MAIN executable (== 0x555555554000 with ASLR off, but resolved
    # for real from the memory map so it is correct even if that ever changes).
    if _BASE[0] is None:
        if not gdb.selected_inferior().pid:
            raise gdb.error("base()/at() need a running process - call starti() "
                            "(or run()) BEFORE reading the base or setting an RVA hook.")
        prog = gdb.current_progspace().filename or ""
        tail = prog.split("/")[-1]
        for line in gdb.execute("info proc mappings", to_string=True).splitlines():
            p = line.split()
            if len(p) >= 5 and p[-1].split("/")[-1] == tail:
                _BASE[0] = int(p[0], 16); break
    return _BASE[0]
def at(rva):
    # base + file-offset/RVA. The Brain works in offsets from r2/objdump; this maps one
    # to the live address. (0x555555554000 + rva when ASLR is off.)
    return base() + int(rva)

def hook(where, fn, once=False):
    # Break, run fn(bp) when hit, then CONTINUE (fn returns True to STOP instead).
    # `where`: an int is treated as an RVA (base is added); a str is a gdb location
    #   ("*0x555555....", "*main", "func", "file.c:42").
    loc = ("*%d" % at(where)) if isinstance(where, int) else str(where)
    class _H(gdb.Breakpoint):
        def stop(self):
            try:
                stop = bool(fn(self))
            except Exception:
                import traceback; traceback.print_exc(); stop = True
            if once and not stop:
                self.enabled = False
            return stop
    return _H(loc, gdb.BP_BREAKPOINT)

def starti(): gdb.execute("starti")   # stop at the very first instruction
def cont():   gdb.execute("continue")
def run():    gdb.execute("run")      # start and run (breakpoints still fire)
'''


# Tail appended after the Brain's script is exec'd - so its OWN line numbers show up in a
# traceback (as "brain_gdb_script"), not offset by the prelude length. __GDB_USER__ is
# replaced by the per-call name of the Brain's script file (repr-quoted).
_GDB_RUN_TAIL = (
    "\ntry:\n"
    "    with open(__GDB_USER__, 'r') as _f:\n"
    "        _src = _f.read()\n"
    "    exec(compile(_src, 'brain_gdb_script', 'exec'), globals())\n"
    "except Exception:\n"
    "    import traceback as _tb; _tb.print_exc()\n"
    "    log('__GDB_USER_EXC__')\n"
)


def linux_gdb(prog, script, args="", gui=True, screen="1280x800x24",
              max_lines=800, timeout=120, host=None):
    h, err = hosts.check(host, "linux_gdb", "linux")
    if err:
        return err
    host = h.name
    if not (script or "").strip():
        return VMResult(False, "linux_gdb: `script` is empty - pass a gdb-python "
                                 "snippet, e.g. hook(0x1234, lambda b: log(reg('rdi'))); run()")
    blocked = _security_block(script)
    if blocked:
        return VMResult(False, f"linux_gdb: refused - {blocked}")
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = 120
    if isinstance(args, (list, tuple)):
        argstr = " ".join(shlex.quote(str(a)) for a in args)
    else:
        argstr = str(args or "")

    # Write the driver (prelude + exec-tail) and the Brain's script as SEPARATE files, so
    # a Python error in the Brain's code reports ITS line number, not a prelude-shifted one.
    # Both are per-call (`_lh_gdbrun_<token>.py`, `_lh_gdbuser_<token>.py`) and removed
    # after the call: fixed names let two labhand processes run each other's gdb script
    # (found 2026-11-20).
    run_name = remote.scratch_name("gdbrun", "py")
    user_name = remote.scratch_name("gdbuser", "py")

    # gdb --args sets the inferior + argv but does NOT start it; the Brain's run()/starti()
    # does. timeout -k 10 tree-kills gdb (and the inferior in its process group) so a hung
    # target or an infinite continue cannot outlive the call and clog the VM.
    # An absolute `prog` is passed as is; a relative one resolves in the workdir and gets
    # "./" so gdb (and the shell) never search PATH for it. "./" was prefixed to every path
    # before, so "/usr/bin/sleep" became ".//usr/bin/sleep": gdb said "No executable file
    # specified" and the call still came back ok=True (found 2026-11-21).
    prog_s = str(prog)
    prog_arg = prog_s if prog_s.startswith("/") else "./" + prog_s
    gdb_cmd = (f"timeout -k 10 {timeout} gdb -q -batch -x {shlex.quote(run_name)} "
               f"--args {shlex.quote(prog_arg)}" + (f" {argstr}" if argstr else ""))
    if gui:
        inner = (f"cd {_wd()} 2>/dev/null; "
                 "xvfb-run -a -s " + shlex.quote("-screen 0 " + str(screen))
                 + " bash -c " + shlex.quote(gdb_cmd) + "; echo __GDB_RC=$?")
    else:
        inner = f"cd {_wd()} 2>/dev/null; " + gdb_cmd + "; echo __GDB_RC=$?"
    cmd = f"bash -c {shlex.quote(inner)}"

    try:
        remote.write_remote(f"{_wd()}/{run_name}",
                            _GDB_PRELUDE + _GDB_RUN_TAIL.replace("__GDB_USER__", repr(user_name)),
                            host=host)
        remote.write_remote(f"{_wd()}/{user_name}", script, host=host)
        out, errtxt = remote.ssh_exec(cmd, host=host, read_timeout=timeout + 30)
    finally:
        remote.remove_quiet([run_name, user_name], host=host)
    # The `echo __GDB_RC=$?` marker is on STDOUT, as its last line: take the LAST marker
    # line of stdout, before stderr is appended. Searching the end of stdout+stderr missed
    # it whenever gdb wrote to stderr (pwndbg's terminfo warning on Kali), so a run killed
    # by its timeout (rc 124) came back ok=True (measured 2026-11-20). Not anchored at the
    # start of a line: a prompt without a newline (`Password: ` + a blocking read) puts the
    # marker on the same line, `Password: __GDB_RC=124` (review R2-F1, 2026-11-21). Only the
    # marker itself (and the line end the echo wrote) is stripped.
    out = out or ""
    rc = None
    mrcs = list(re.finditer(r"__GDB_RC=(\d+)[ \t\r]*\n?", out))
    if mrcs:
        mrc = mrcs[-1]
        rc = int(mrc.group(1))
        out = out[:mrc.start()] + out[mrc.end():]
    combined = (out.strip() + (("\n" + errtxt) if (errtxt or "").strip() else "")).strip()
    timed_out = rc == 124
    if re.search(r"gdb:\s*(command )?not found|No such file.*gdb", combined) and rc:
        return VMResult(False, f"linux_gdb: gdb did not run on {host} - measured output: "
                                 + combined[:400])
    user_exc = "__GDB_USER_EXC__" in combined
    combined = combined.replace("__GDB_USER_EXC__", "").strip()

    lines = combined.splitlines()
    capped = ""
    if len(lines) > int(max_lines):
        capped = f"\n[... {len(lines) - int(max_lines)} more lines - raise max_lines or log() a summary]"
        combined = "\n".join(lines[:int(max_lines)])
    combined = (combined + capped) if combined else "(gdb produced no output)"

    detail, saved = _spill(host, combined, "gdb_" + re.sub(r"[^A-Za-z0-9]", "_", prog_s)[:20])
    flag = _find_match(combined)
    n = len(combined)

    note = ""
    if timed_out:
        note = (f" - KILLED after {timeout}s (raise `timeout`, or don't run() an app "
                "that never exits: set your hooks and let them fire, or kill() in a hook)")
    elif user_exc:
        note = " - your script raised a Python exception (traceback above; line = your script)"
    ok = not timed_out
    known = [f"linux_gdb on {prog} -> {n} chars"
             + (f" (full -> {saved}, page with read_file)" if saved else "")
             + (" [TIMEOUT]" if timed_out else "")]
    return VMResult(ok, f"linux_gdb on {prog}: {n} chars{note}" + ("; MATCH" if flag else ""),
                      detail=f"[linux_gdb @ {prog}]\n" + detail,
                      known=known if ok else [], match=flag, info=detail)


# --- Linux: capability triage (capa, YARA / YARA-X, Volatility 3) ----------------------
# Thin "run a tool, return its raw output" wrappers, like linux_gdb. labhand only RUNS
# capa / yara / yr (YARA-X) / vol (Volatility 3) on the host and hands back their text
# untouched: no parsing, no capability map, no ATT&CK / MBC reading, no verdict - that is
# the Brain's job. A tool that is not installed on the host comes back as the raw shell
# error (ok=False); labhand never installs anything. Every caller-supplied path / option is shlex-quoted, and an
# argument that starts with "-" is prefixed with "./" so it cannot be read as an option.

def _q_path(p):
    p = str(p)
    if p.startswith("-"):
        p = "./" + p
    return shlex.quote(p)


def _q_extra(extra):
    """`extra` (a string or a list) -> shlex-quoted words. Options are passed through as
    options (that is what `extra` is for), but each word is quoted, so no shell syntax in
    them (`;`, `$(..)`, backticks, redirections) ever reaches the shell."""
    if not extra:
        return ""
    words = [str(a) for a in extra] if isinstance(extra, (list, tuple)) else shlex.split(str(extra))
    return " ".join(shlex.quote(w) for w in words)


def _int_timeout(timeout, default):
    try:
        t = int(timeout)
    except (TypeError, ValueError):
        return default
    return t if t > 0 else default


def _missing_tool_note(res, tool, hint):
    """A missing tool (exit 127 / 'command not found') keeps its raw output; only the
    summary says so, with the install hint. Nothing is installed here."""
    if not res.ok and re.search(rf"\b{re.escape(tool)}: (command )?not found", res.detail or ""):
        res.summary = (f"{tool} is not installed on the host (labhand does not install it; "
                       f"e.g. `{hint}`) - " + res.summary)
    return res


def linux_capa(path, host=None, timeout=120, extra=""):
    """Run `capa` on a linux host against `path` (relative to the host workdir) and return
    the RAW VMResult: capa's stdout/stderr unchanged. No interpretation."""
    h, err = hosts.check(host, "linux_capa", "linux")
    if err:
        return err
    if not str(path or "").strip():
        return VMResult(False, "linux_capa: `path` is empty - name the sample in the host workdir")
    timeout = _int_timeout(timeout, 120)
    try:
        opts = _q_extra(extra)
    except ValueError as e:          # e.g. an unbalanced quote in `extra`
        return VMResult(False, f"linux_capa: could not split `extra` into words ({e})")
    cmd = "capa " + (opts + " " if opts else "") + _q_path(path)
    res = _run_and_wrap(cmd, h.name, timeout, f"capa {str(path)[:60]}")
    return _missing_tool_note(res, "capa", "pipx install flare-capa")


_YARA_ENGINES = {                    # engine -> (command prefix, tool name, install hint)
    "yara": ("yara", "yara", "apt install yara"),
    "yarax": ("yr scan", "yr", "cargo install yara-x-cli"),
}


def linux_yara(rules, target, host=None, timeout=60, extra="", engine="yara"):
    """Run `yara <rules> <target>` on a linux host (paths relative to the host workdir) and
    return the RAW VMResult: yara's stdout/stderr unchanged. No interpretation.
    engine="yarax" runs the YARA-X CLI instead: `yr scan <extra> <rules> <target>`."""
    h, err = hosts.check(host, "linux_yara", "linux")
    if err:
        return err
    if not isinstance(engine, str) or engine not in _YARA_ENGINES:
        return VMResult(False, f"linux_yara: unknown engine {engine!r}; allowed: "
                                 + ", ".join(_YARA_ENGINES))
    prefix, tool, hint = _YARA_ENGINES[engine]
    if not str(rules or "").strip() or not str(target or "").strip():
        return VMResult(False, "linux_yara: `rules` and `target` are both required "
                                 "(a rules file and a file/dir in the host workdir)")
    timeout = _int_timeout(timeout, 60)
    try:
        opts = _q_extra(extra)
    except ValueError as e:          # e.g. an unbalanced quote in `extra`
        return VMResult(False, f"linux_yara: could not split `extra` into words ({e})")
    cmd = prefix + " " + (opts + " " if opts else "") + _q_path(rules) + " " + _q_path(target)
    res = _run_and_wrap(cmd, h.name, timeout, f"{prefix} {str(rules)[:30]} {str(target)[:30]}")
    return _missing_tool_note(res, tool, hint)


def linux_vol(image, plugin, host=None, timeout=300, extra="", vol_opts=""):
    """Run Volatility 3 (`vol <vol_opts> -f <image> <plugin> <extra>`) on a linux host
    (`image` relative to the host workdir) and return the RAW VMResult: vol's stdout/stderr
    unchanged. No interpretation. `vol_opts` are vol's GLOBAL options (e.g. `--offline`,
    `-r json`, `-s <symdir>`) and go before `-f`; `extra` goes after the plugin (plugin
    options). A plugin option such as `--dump` / `-o <dir>` can write files into the workdir."""
    h, err = hosts.check(host, "linux_vol", "linux")
    if err:
        return err
    if not str(image or "").strip() or not str(plugin or "").strip():
        return VMResult(False, "linux_vol: `image` and `plugin` are both required "
                                 "(a memory image in the host workdir and a plugin, "
                                 "e.g. linux.pslist.PsList)")
    if str(plugin).startswith("-"):  # a plugin name, not an option: options go in `extra`
        return VMResult(False, f"linux_vol: `plugin` must be a plugin name, not an option "
                                 f"({str(plugin)[:40]!r}); pass options in `extra`")
    timeout = _int_timeout(timeout, 300)
    try:
        opts = _q_extra(extra)
    except ValueError as e:          # e.g. an unbalanced quote in `extra`
        return VMResult(False, f"linux_vol: could not split `extra` into words ({e})")
    try:
        gopts = _q_extra(vol_opts)
    except ValueError as e:          # e.g. an unbalanced quote in `vol_opts`
        return VMResult(False, f"linux_vol: could not split `vol_opts` into words ({e})")
    cmd = ("vol " + (gopts + " " if gopts else "") + "-f " + _q_path(image) + " "
           + shlex.quote(str(plugin)) + (" " + opts if opts else ""))
    res = _run_and_wrap(cmd, h.name, timeout, f"vol {str(plugin)[:30]} {str(image)[:30]}")
    return _missing_tool_note(res, "vol", "pipx install volatility3")


# --- Linux: Ghidra headless (analyzeHeadless) ----------------------------------------
# The text route into Ghidra: import one binary into a throw-away project, analyse it
# (unless analyze=False) and run the caller's post-script; the raw Ghidra log + script
# output comes back. No interpretation. Everything a call creates lives in ONE per-call
# directory `<workdir>/_lh_ghidra_<uuid>/` (proj/ = the project, scripts/ = the script) that
# the remote shell removes on every path: the cleanup runs OUTSIDE the inner `timeout`
# that kills analyzeHeadless + its java (GNU timeout puts them in their own process group
# and signals the whole group), so a killed run is still cleaned up. The project is opened
# -readOnly and created with -deleteProject, so nothing is saved and the sample is untouched.

# Where analyzeHeadless is looked for after `command -v analyzeHeadless`.
GHIDRA_HEADLESS_CANDIDATES = (
    "/usr/share/ghidra/support/analyzeHeadless",     # Kali / Debian package `ghidra`
    "/opt/ghidra/support/analyzeHeadless",
)
GHIDRA_OVERRIDE_KEY = "GHIDRA_HEADLESS"              # inventory key <NAME>_GHIDRA_HEADLESS
GHIDRA_SCRIPT_MAX = 64 * 1024                        # bytes; the script travels in the command line
_GHIDRA_SCRIPT_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*\.[A-Za-z0-9]+$")
# analyzeHeadless takes post-script arguments until the next word that is one of ITS options
# (AnalyzeHeadless.Arg.matches: equalsIgnoreCase, plus any "-loader-..." word), so such a word
# cannot be passed as a script argument: it would be read as a Ghidra option. Refused instead.
_GHIDRA_OPTIONS = frozenset(o.lower() for o in (
    "-import", "-process", "-prescript", "-postscript", "-scriptPath", "-propertiesPath",
    "-scriptlog", "-log", "-overwrite", "-recursive", "-readOnly", "-deleteproject",
    "-noanalysis", "-processor", "-cspec", "-analysisTimeoutPerFile", "-keystore", "-connect",
    "-p", "-commit", "-okToDelete", "-max-cpu", "-librarySearchPaths", "-loader"))
_GH_PATH_RE = re.compile(r"^__LH_GHIDRA_PATH__=(.*)$", re.M)
_GH_VER_RE = re.compile(r"^__LH_GHIDRA_VERSION__=(.*)$", re.M)
_GH_RC_RE = re.compile(r"^__LH_GHIDRA_RC__=(\d+) elapsed=(\d+)$", re.M)
_GH_PYG_RE = re.compile(r"^__LH_GHIDRA_PYGHIDRA__=(.*)$", re.M)
# A .py post-script runs under PyGhidra. Ghidra 12 has no built-in Jython, and plain
# analyzeHeadless does not start PyGhidra (measured on Kali, Ghidra 12.1.3, 2026-11-20:
# JythonStubException / "Ghidra was not started with PyGhidra"). Ghidra's own launcher
# (support/pyghidraRun -> pyghidra_launcher.py) may create a venv and offers pip installs and
# upgrades, so it is NOT used: labhand runs the command that launcher ends with, after
# checking that python3 can import pyghidra (else raw ok=False; labhand never installs).
# `-P` (Python >= 3.11; Kali: 3.14.7, measured 2026-11-20): the cwd is the samples workdir,
# which holds untrusted material, and `-c` / `-m` would put it FIRST on sys.path, so a planted
# `pyghidra/` (or `logging.py`, `jpype/`, ...) there would be imported and run. With -P nothing
# is imported from the cwd; an older python3 rejects -P and the import check fails (ok=False).
# Not -I: that also drops the user site, where Kali's pyghidra lives (~/.local/lib/python3.14).
_PYGHIDRA_LAUNCH = ('python3 -P -m pyghidra.ghidra_launch --install-dir "$__ghi" '
                    "ghidra.app.util.headless.AnalyzeHeadless")
# One line out either way: the version + interpreter, or the import error as `Type: message`
# (the traceback of a `-c` one-liner only adds `File "<string>", line 1`).
_PYGHIDRA_CHECK = ("import sys\n"
                   "try:\n"
                   "    import pyghidra\n"
                   "except BaseException as e:\n"
                   "    print(type(e).__name__ + ': ' + str(e)); sys.exit(1)\n"
                   "print(getattr(pyghidra, '__version__', '?'), sys.executable)")
_PYGHIDRA_CHECK_T = 60               # s; the import check runs before (and besides) `timeout`
_GHIDRA_KILL_GRACE = 5               # s; `timeout -k` grace after SIGTERM
# Ghidra compiles a .java post-script into an OSGi bundle OUTSIDE the per-call dir:
# <user settings>/osgi/compiled-bundles/<Integer.toHexString(scriptDir.getAbsolutePath()
# .hashCode())>/ (measured on Kali, Ghidra 12.1.3, 2026-11-20: script dir
# /home/vahpem/samples/_lh_ghidra_35ef4d5a415e458b99148e79020d889b/scripts -> f851c098). The
# per-call script dir makes that name unique per call, so the EXIT trap computes it with
# this bash port of Java's String.hashCode (exact for ASCII paths; the uuid part always is)
# and removes the bundle if it was written during this call (newer than the call's stamp).
_GHIDRA_JHASH_FN = ("__lh_jh() { local s=$1 h=0 i c; for ((i=0; i<${#s}; i++)); do "
                    "printf -v c '%d' \"'${s:i:1}\"; h=$(( (h*31 + c) & 0xffffffff )); done; "
                    "printf '%x' \"$h\"; }")
_GHIDRA_BUNDLE_ROOTS = ('"${XDG_CONFIG_HOME:-$HOME/.config}"/ghidra/*/osgi/compiled-bundles',
                        '"$HOME"/.ghidra/*/osgi/compiled-bundles')


def _ghidra_uses_pyghidra(script, script_name):
    return bool(script) and script_name.lower().endswith(".py")


def _ghidra_backstop(timeout, pyg):
    """The outer (_wrap_cmd) timeout. It only fires if the shell itself hangs, so it must be
    longer than everything the block bounds itself: the PyGhidra import check (+ grace) and
    the inner `timeout` (+ grace), plus slack for the locate / write / cleanup steps. Were it
    shorter, it would kill the wrapper while the inner GNU timeout (its own process group)
    keeps java running, and the EXIT trap would remove the dir under a running Ghidra."""
    pre = (_PYGHIDRA_CHECK_T + _GHIDRA_KILL_GRACE) if pyg else 0
    return pre + int(timeout) + _GHIDRA_KILL_GRACE + 30


def _ghidra_block(path, script, script_name, script_args, analyze, timeout, opts,
                  override, call_dir, key="<NAME>_" + GHIDRA_OVERRIDE_KEY):
    """The bash block linux_ghidra runs (in the host workdir, inside _wrap_cmd's backstop).
    Every caller word is shlex-quoted; the script text travels base64-encoded."""
    cands = " ".join(shlex.quote(c) for c in GHIDRA_HEADLESS_CANDIDATES)
    pyg = _ghidra_uses_pyghidra(script, script_name)
    lines = [
        "(",
        f"__lh_d={call_dir}",
        # removes the per-call dir (and the compiled bundle of its script dir) on EVERY exit
        # of this subshell (success, Ghidra error, script error, inner timeout); the inner
        # `timeout` never signals this shell
        "__lh_sd=",
        _GHIDRA_JHASH_FN,
        "__lh_cleanup() {",
        '  if [ -n "$__lh_sd" ] && [ -e "$__lh_d/stamp" ]; then',
        '    __lh_h=$(__lh_jh "$__lh_sd")',
        '    [ -n "$__lh_h" ] &&',      # never an empty name: that would be compiled-bundles/ itself
        "    for __lh_b in " + " ".join(r + '/"$__lh_h"' for r in _GHIDRA_BUNDLE_ROOTS) + "; do",
        '      [ -d "$__lh_b" ] && [ -n "$(find "$__lh_b" -maxdepth 0 -newer "$__lh_d/stamp" '
        '2>/dev/null)" ] && rm -rf -- "$__lh_b"',
        "    done",
        "  fi",
        '  rm -rf -- "$__lh_d"',
        "}",
        "trap __lh_cleanup EXIT",
        f"__gh={shlex.quote(override)}",
        'if [ -n "$__gh" ]; then',
        '  [ -f "$__gh" ] && [ -x "$__gh" ] || { echo "linux_ghidra: '
        f'{key}=$__gh is not an executable file"; exit 127; }}',
        "else",
        "  __gh=$(command -v analyzeHeadless 2>/dev/null)",
        '  if [ -z "$__gh" ]; then',
        f'    for __c in {cands}; do [ -x "$__c" ] && {{ __gh=$__c; break; }}; done',
        "  fi",
        '  [ -n "$__gh" ] || { echo "analyzeHeadless: not found (not on PATH, not at '
        + " ".join(GHIDRA_HEADLESS_CANDIDATES).replace('"', "")
        + f'; set {key} to its absolute path)"; exit 127; }}',
        "fi",
        '__ghr=$(readlink -f -- "$__gh" 2>/dev/null || echo "$__gh")',
        "__ghv=$(sed -n 's/^application\\.version=//p' "
        '"${__ghr%/*}/../Ghidra/application.properties" 2>/dev/null | head -1)',
        'echo "__LH_GHIDRA_PATH__=$__gh"',
        'echo "__LH_GHIDRA_VERSION__=$__ghv"',
    ]
    if pyg:
        lines += [
            # the Ghidra install dir = the parent of support/ that holds analyzeHeadless
            '__ghi=$(cd -- "${__ghr%/*}/.." 2>/dev/null && pwd -P)',
            f"__pyv=$(timeout -k {_GHIDRA_KILL_GRACE} {_PYGHIDRA_CHECK_T} python3 -P -c "
            f"{shlex.quote(_PYGHIDRA_CHECK)} </dev/null 2>&1)",
            'if [ $? -ne 0 ] || [ -z "$__ghi" ]; then',
            "  echo \"linux_ghidra: a .py post-script runs under PyGhidra, but \\`python3 -P -c "
            "'import pyghidra'\\` failed on this host (Ghidra install dir: '$__ghi'). labhand "
            "never installs it: write the post-script in Java (.java), or install PyGhidra "
            "on the host yourself.\"",
            '  printf \'%s\\n\' "$__pyv" | tail -n 5',
            "  exit 127",
            "fi",
            'echo "__LH_GHIDRA_PYGHIDRA__=$__pyv"',
        ]
    lines.append('mkdir -p -- "$__lh_d/proj" "$__lh_d/scripts" || exit 1')
    argv = ['"$PWD/$__lh_d/proj"', "p", "-import", _q_path(path), "-deleteProject", "-readOnly"]
    if not analyze:
        argv.append("-noanalysis")
    if opts:
        argv.append(opts)
    if script:
        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        lines.append(f'printf %s {b64} | base64 -d > "$__lh_d/scripts/{script_name}" || exit 1')
        lines.append('__lh_sd="$PWD/$__lh_d/scripts"; : > "$__lh_d/stamp" || exit 1')
        argv += ["-scriptPath", '"$PWD/$__lh_d/scripts"', "-postScript", script_name]
        argv += [shlex.quote(a) for a in script_args]
    lines += [
        "__t0=$SECONDS",
        f"timeout -k {_GHIDRA_KILL_GRACE} {int(timeout)} " + (_PYGHIDRA_LAUNCH if pyg else '"$__gh"') + " "
        + " ".join(argv) + " </dev/null 2>&1",
        "__g=$?",
        'echo "__LH_GHIDRA_RC__=$__g elapsed=$((SECONDS-__t0))"',
        "exit $__g",
        ")",
    ]
    return "\n".join(lines)


def linux_ghidra(path, script="", script_name="labhand_ghidra.py", script_args=(),
                 analyze=True, host=None, timeout=300, extra=""):
    """Run Ghidra's analyzeHeadless on a linux host: import `path` (relative to the host
    workdir) into a fresh per-call project, analyse it unless analyze=False (-noanalysis),
    and when `script` is non-empty run it as the post-script `script_name` with
    `script_args`. Returns the RAW combined output (Ghidra log + script output); no
    interpretation. The project is read-only and deleted; the per-call dirs are removed on
    every path; `timeout` kills analyzeHeadless and its java. Ghidra is located through
    <NAME>_GHIDRA_HEADLESS (inventory), else `command -v analyzeHeadless`, else
    GHIDRA_HEADLESS_CANDIDATES; not found -> ok=False with the raw reason. labhand never
    installs Ghidra.

    Script language: `.java` (and anything else but `.py`) runs under analyzeHeadless; a
    `.py` post-script runs under PyGhidra (`python3 -P -m pyghidra.ghidra_launch ...
    AnalyzeHeadless`, same arguments) after checking that python3 can import pyghidra -
    not importable -> ok=False with the raw error, nothing installed. Measured on Kali,
    Ghidra 12.1.3, 2026-11-20: plain analyzeHeadless cannot run a .py at all (no Jython ->
    JythonStubException; `# @runtime PyGhidra` -> "Ghidra was not started with PyGhidra",
    exit still 0)."""
    h, err = hosts.check(host, "linux_ghidra", "linux")
    if err:
        return err
    host = h.name
    if not str(path or "").strip():
        return VMResult(False, "linux_ghidra: `path` is empty - name the binary in the host workdir")
    script = "" if script is None else str(script)
    script_name = str(script_name or "")
    if script and (not _GHIDRA_SCRIPT_NAME.match(script_name) or len(script_name) > 128):
        return VMResult(False, f"linux_ghidra: bad `script_name` {script_name[:60]!r}: letters, "
                                 "digits and '._-' only, not starting with '.' or '-', with an "
                                 "extension (.py, .java, ...) that picks Ghidra's script language")
    if isinstance(script_args, (list, tuple)):
        args = [str(a) for a in script_args]
    else:
        try:
            args = shlex.split(str(script_args or ""))
        except ValueError as e:
            return VMResult(False, f"linux_ghidra: could not split `script_args` into words ({e})")
    clash = [a for a in args if a.lower() in _GHIDRA_OPTIONS or a.startswith("-loader-")]
    if clash:
        return VMResult(False, f"linux_ghidra: script argument(s) {clash[:3]} are analyzeHeadless "
                                 "options, so Ghidra would read them as options, not as script "
                                 "arguments; encode them differently")
    try:
        script_bytes = script.encode("utf-8")
    except UnicodeEncodeError as e:  # e.g. a lone surrogate from a JSON "\ud800"
        return VMResult(False, f"linux_ghidra: `script` is not valid UTF-8 text ({e})")
    if script and len(script_bytes) > GHIDRA_SCRIPT_MAX:
        return VMResult(False, f"linux_ghidra: `script` is {len(script_bytes)} bytes; "
                                 f"the limit is {GHIDRA_SCRIPT_MAX} (it travels in the command line)")
    if not script and args:
        return VMResult(False, "linux_ghidra: `script_args` given but `script` is empty")
    blocked = _security_block(script) if script else None
    if blocked:
        return VMResult(False, f"linux_ghidra: refused - {blocked}")
    timeout = _int_timeout(timeout, 300)
    try:
        opts = _q_extra(extra)
    except ValueError as e:          # e.g. an unbalanced quote in `extra`
        return VMResult(False, f"linux_ghidra: could not split `extra` into words ({e})")
    override = hosts.setting(host, GHIDRA_OVERRIDE_KEY)
    if override and not override.startswith("/"):
        return VMResult(False, f"linux_ghidra: {hosts.prefix(host)}_{GHIDRA_OVERRIDE_KEY}="
                                 f"{override!r} must be an absolute path to analyzeHeadless")

    call_dir = "_lh_ghidra_" + uuid.uuid4().hex
    block = _ghidra_block(path, script, script_name, args, bool(analyze), timeout, opts,
                          override, call_dir, key=f"{hosts.prefix(host)}_{GHIDRA_OVERRIDE_KEY}")
    backstop = _ghidra_backstop(timeout, _ghidra_uses_pyghidra(script, script_name))
    label = f"ghidra {str(path)[:60]}"
    try:
        out, errtxt = remote.ssh_exec(_wrap_cmd(block, host, backstop), host=host,
                                      read_timeout=backstop + 25)
    except Exception as e:  # noqa: BLE001 - a connection failure is a result, not a crash
        return VMResult(False, f"linux_ghidra: could not run on {host}: {type(e).__name__}: {e}")
    combined = ((out or "") + (("\n" + errtxt) if (errtxt or "").strip() else "")).strip()
    combined, rc_outer = _peel_rc(combined)
    # Only remote.ssh_exec's own read-timeout note (appended to the stderr it returns) and a
    # missing __RC marker mean a dead channel; the same words in Ghidra / script output do not.
    channel_dead = rc_outer is None and "(TIMEOUT after" in (errtxt or "")
    combined, newfiles = _peel_newfiles(combined)

    gh_path = gh_ver = gh_pyg = None
    m = _GH_PYG_RE.search(combined)           # printed BEFORE Ghidra runs: first occurrence
    if m:
        gh_pyg = m.group(1).strip()
        combined = combined[:m.start()] + combined[m.end() + 1:]
    m = _GH_PATH_RE.search(combined)          # printed BEFORE Ghidra runs: first occurrence
    if m:
        gh_path = m.group(1).strip()
        combined = combined[:m.start()] + combined[m.end() + 1:]
    m = _GH_VER_RE.search(combined)
    if m:
        gh_ver = m.group(1).strip()
        combined = combined[:m.start()] + combined[m.end() + 1:]
    rc = elapsed = None
    ms = list(_GH_RC_RE.finditer(combined))   # printed AFTER Ghidra: last occurrence
    if ms:
        m = ms[-1]
        rc, elapsed = int(m.group(1)), int(m.group(2))
        combined = combined[:m.start()] + combined[m.end():]
    combined = combined.strip()
    if rc is None:
        rc = rc_outer
    # 124 = GNU timeout fired; 137 = it had to SIGKILL after the grace (only counted as the
    # timeout when the run actually lasted that long, so an OOM kill is not relabelled).
    timed_out = (channel_dead or rc_outer == 124 or rc == 124
                 or (rc == 137 and elapsed is not None and elapsed >= timeout))
    ok = (rc == 0) and not timed_out

    text = combined or "(analyzeHeadless produced no output)"
    try:
        detail, saved = _spill(host, text, "ghidra")
    except Exception as e:  # noqa: BLE001 - the run happened; do not lose its result
        detail, saved = text[:4000] + f"\n[... output not spilled to the host: {e}]", None
    if newfiles:                     # e.g. a post-script that exported into the workdir
        detail += (f"\nFILES WRITTEN OR CHANGED in {_wd()}/ by this call: "
                   + "; ".join(f"{fn} ({fsz} bytes)" for fn, fsz in newfiles[:8]))
    flag = _find_match(text)
    known = []
    if gh_path:
        known.append(f"analyzeHeadless: {gh_path}")
    if gh_ver:
        known.append(f"Ghidra version: {gh_ver}")
    if gh_pyg:
        known.append(f"PyGhidra (version, python): {gh_pyg}")
    if rc is not None:
        known.append(f"analyzeHeadless exit code: {rc}")
    if elapsed is not None:
        known.append(f"analyzeHeadless elapsed: {elapsed}s")
    if saved:
        known.append(f"linux_ghidra full output -> {saved} (page with read_file)")
    why = (f"TIMEOUT (killed after {timeout}s - raise `timeout`, or analyze=False for a quick "
           "look), " if timed_out else
           "shell aborted before finishing, " if rc is None else
           f"exit {rc}, " if rc != 0 else "")
    return VMResult(ok, f"ran on {host}: {label} -> {why}{len(text)} chars"
                        + ("; MATCH" if flag else ""),
                    detail=f"[linux_ghidra @ {path}]\n" + detail, known=known,
                    match=flag, info=detail)
