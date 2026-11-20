# -*- coding: utf-8 -*-
"""labhand.linux.linux_ghidra (Ghidra analyzeHeadless): VM-free.

Two layers:
* the command labhand sends is captured (remote.ssh_exec mocked) and inspected;
* the SAME command is executed by a local bash against a fake `analyzeHeadless` (a shell
  script that prints the argv it got) and a fake `timeout`, so quoting, the per-call dirs,
  the cleanup on every exit path and the not-found / override paths are checked by actually
  running the generated shell code, not by reading it.
"""
import os
import pathlib
import re
import shutil
import subprocess

import pytest

from labhand import hosts, linux, remote

NASTY = "-evil sample; rm -rf ~ $(id) `whoami` 'q\" >x.txt"
FAKE_GHIDRA = r"""#!/bin/bash
# fake analyzeHeadless: print the argv one per line, show the project dir and the script
i=0; for a in "$@"; do echo "ARG$i=[$a]"; i=$((i+1)); done
[ -d "$1" ] && echo "PROJDIR_EXISTS"
prev=""; for a in "$@"; do
  if [ "$prev" = "-scriptPath" ]; then sp="$a"; fi
  if [ "$prev" = "-postScript" ]; then echo "SCRIPT_BODY<<"; cat "$sp/$a"; echo ">>"; fi
  prev="$a"
done
pwd > "${FAKE_LOG:-/dev/null}"; echo "$1" >> "${FAKE_LOG:-/dev/null}"
if [ -n "$FAKE_BUNDLE" ] && [ -n "$sp" ]; then   # what Ghidra does for a .java script (R2-F2)
  h=$("$FAKE_PY" -c 'import sys
h = 0
for ch in sys.argv[1]: h = (31 * h + ord(ch)) & 0xffffffff
print("%x" % h)' "$sp")
  b="${XDG_CONFIG_HOME:-$HOME/.config}/ghidra/ghidra_11.4.2_DEV/osgi/compiled-bundles/$h"
  mkdir -p "$b/META-INF" && : > "$b/GeneratedActivator.class" && echo "BUNDLE=$b"
fi
[ -n "$FAKE_SLEEP" ] && sleep "$FAKE_SLEEP"
exit "${FAKE_RC:-0}"
"""
# fake python3: answers the pyghidra import check (fails when FAKE_NO_PYGHIDRA is set) and
# turns `-P -m pyghidra.ghidra_launch --install-dir D ...AnalyzeHeadless ARGS` into the fake
# D/support/analyzeHeadless ARGS, so the PyGhidra route runs through the same fake Ghidra.
FAKE_PYTHON3 = r"""#!/bin/bash
# labhand must always pass -P (never import from the samples cwd): refuse otherwise
[ "$1" = -P ] || { echo "python3 called without -P: $*"; exit 98; }
shift
if [ "$1" = -c ]; then
  if [ -n "$FAKE_NO_PYGHIDRA" ]; then
    echo "ModuleNotFoundError: No module named 'pyghidra'" >&2; exit 1
  fi
  echo "3.2.0 /fake/bin/python3"; exit 0
fi
if [ "$1" = -m ] && [ "$2" = pyghidra.ghidra_launch ] && [ "$3" = --install-dir ] \
   && [ "$5" = ghidra.app.util.headless.AnalyzeHeadless ]; then
  echo "PYGHIDRA_LAUNCH install=[$4]"; d=$4; shift 5; exec "$d/support/analyzeHeadless" "$@"
fi
echo "unexpected python3 call: $*"; exit 99
"""
# GNU timeout is not on macOS: drop "-k N DURATION" and run the rest (the real kill is
# exercised live; here the timeout exit codes are simulated through FAKE_RC).
FAKE_TIMEOUT = """#!/bin/bash
[ "$1" = -k ] && shift 2
shift
exec "$@"
"""


@pytest.fixture
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a connection was attempted")
    monkeypatch.setattr(remote, "_client", boom)
    monkeypatch.setattr(remote, "_POOL", {})
    monkeypatch.setattr(linux, "_spill", lambda host, text, stem: (text, None))
    monkeypatch.delenv("KALI_GHIDRA_HEADLESS", raising=False)


class _Calls(list):
    reply = None


@pytest.fixture
def captured(monkeypatch, no_network):
    """Capture the command; answer with a canned raw output."""
    calls = _Calls()
    reply = {"out": "__LH_GHIDRA_PATH__=/usr/bin/analyzeHeadless\n__LH_GHIDRA_VERSION__=11.4\n"
                    "INFO  hello (GhidraScript)\n__LH_GHIDRA_RC__=0 elapsed=7\n__RC=0"}

    def fake(cmd, host=None, read_timeout=None):
        calls.append({"cmd": cmd, "host": host, "read_timeout": read_timeout})
        return reply["out"], ""
    monkeypatch.setattr(remote, "ssh_exec", fake)
    calls.reply = reply
    return calls


@pytest.fixture
def local(monkeypatch, no_network, tmp_path):
    """Run the generated command with a local bash in tmp_path (the 'home'), with the fake
    analyzeHeadless reachable only through the override key or PATH as each test chooses."""
    if not shutil.which("bash"):
        pytest.skip("no bash")
    home = tmp_path / "home"
    home.mkdir()
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "timeout").write_text(FAKE_TIMEOUT)
    (shim / "timeout").chmod(0o755)
    (shim / "python3").write_text(FAKE_PYTHON3)
    (shim / "python3").chmod(0o755)
    fake = tmp_path / "ghidra" / "support" / "analyzeHeadless"
    fake.parent.mkdir(parents=True)
    fake.write_text(FAKE_GHIDRA)
    fake.chmod(0o755)
    props = tmp_path / "ghidra" / "Ghidra" / "application.properties"
    props.parent.mkdir()
    props.write_text("application.name=Ghidra\napplication.version=11.4.2\n")
    st = {"env": {"PATH": f"{shim}:/usr/bin:/bin", "HOME": str(home),
                  "FAKE_LOG": str(tmp_path / "fake.log")},
          "home": home, "fake": fake, "shim": shim, "cmds": [], "log": tmp_path / "fake.log"}
    monkeypatch.setattr(linux, "GHIDRA_HEADLESS_CANDIDATES", ("/nonexistent/analyzeHeadless",))

    def run(cmd, host=None, read_timeout=None):
        st["cmds"].append(cmd)
        p = subprocess.run(["bash", "-c", cmd], cwd=home, env=st["env"],
                           capture_output=True, text=True, timeout=30)
        return p.stdout, p.stderr
    monkeypatch.setattr(remote, "ssh_exec", run)
    return st


def _leftovers(home):
    wd = home / "samples"
    return sorted(p.name for p in wd.iterdir() if p.name.startswith("_lh_ghidra_")) if wd.exists() else []


def _args(detail):
    return re.findall(r"^ARG\d+=\[(.*?)\]$", detail, re.M | re.S)


# ---------------------------------------------------------------- command construction
def test_ghidra_command_flags_dirs_and_postscript(captured):
    r = linux.linux_ghidra("bin/a.out", script="print(1)\n", script_args=["x", "two words"],
                           timeout=200)
    assert r.ok and "INFO  hello (GhidraScript)" in r.detail
    c = captured[0]
    # backstop around everything: PyGhidra check (60 + 5) + timeout (200 + 5) + 30 s slack
    assert c["host"] == "kali" and c["read_timeout"] == 65 + 200 + 5 + 30 + 25
    cmd = c["cmd"]
    assert cmd.startswith("timeout -k 5 300 bash -c ")
    inner = [line for line in shlex_inner(cmd) if "-import" in line][0]
    # the default script_name is .py -> PyGhidra route (see the pyghidra tests below)
    assert inner.startswith('timeout -k 5 200 python3 -P -m pyghidra.ghidra_launch --install-dir '
                            '"$__ghi" ghidra.app.util.headless.AnalyzeHeadless '
                            '"$PWD/$__lh_d/proj" p -import bin/a.out '
                            "-deleteProject -readOnly -scriptPath")
    assert '-postScript labhand_ghidra.py x \'two words\' </dev/null 2>&1' in inner
    assert "-noanalysis" not in inner
    assert re.search(r"__lh_d=_lh_ghidra_[0-9a-f]{32}\n", shlex_inner(cmd, raw=True))
    raw = shlex_inner(cmd, raw=True)
    assert "trap __lh_cleanup EXIT" in raw and '  rm -rf -- "$__lh_d"\n}' in raw


def shlex_inner(cmd, raw=False):
    """The bash text inside `timeout -k 5 N bash -c '<inner>'; echo __RC=$?`."""
    import shlex
    words = shlex.split(cmd.split("; echo __RC=$?")[0])
    inner = words[words.index("-c") + 1]
    return inner if raw else inner.splitlines()


def test_ghidra_no_analysis_no_script_and_extra(captured):
    linux.linux_ghidra("a", analyze=False, extra="-processor x86:LE:64:default -max-cpu 2")
    inner = [line for line in shlex_inner(captured[0]["cmd"]) if "-import" in line][0]
    assert "-deleteProject -readOnly -noanalysis -processor x86:LE:64:default -max-cpu 2 " in inner
    assert "-postScript" not in inner and "-scriptPath" not in inner
    assert "base64" not in shlex_inner(captured[0]["cmd"], raw=True)


def test_ghidra_default_timeout_and_bad_timeout(captured):
    linux.linux_ghidra("a")
    linux.linux_ghidra("a", timeout="soon")
    linux.linux_ghidra("a", timeout=-3)
    # backstop = timeout + kill grace + 30 s slack (no .py: no PyGhidra pre-phase)
    assert [c["cmd"].split()[3] for c in captured] == ["335", "335", "335"]


def test_ghidra_per_call_dirs_are_unique(captured):
    for _ in range(5):
        linux.linux_ghidra("a", script="pass")
    dirs = [re.search(r"__lh_d=(_lh_ghidra_[0-9a-f]+)", shlex_inner(c["cmd"], raw=True)).group(1)
            for c in captured]
    assert len(set(dirs)) == 5


def test_ghidra_known_holds_measured_facts_only(captured):
    r = linux.linux_ghidra("a")
    assert r.known == ["analyzeHeadless: /usr/bin/analyzeHeadless", "Ghidra version: 11.4",
                       "analyzeHeadless exit code: 0", "analyzeHeadless elapsed: 7s"]
    assert "__LH_GHIDRA" not in r.detail and "__RC" not in r.detail


def test_ghidra_connection_failure_is_ok_false_not_raised(monkeypatch, no_network):
    def down(cmd, host=None, read_timeout=None):
        raise OSError(64, "Host is down")
    monkeypatch.setattr(remote, "ssh_exec", down)
    r = linux.linux_ghidra("a")
    assert r.ok is False and "Host is down" in r.summary


# ---------------------------------------------------------------- refused before running
@pytest.mark.parametrize("kw", [
    dict(path=""), dict(path="  "),
    dict(path="a", script="x", script_name="../evil.py"),
    dict(path="a", script="x", script_name="a b.py"),
    dict(path="a", script="x", script_name="x.py;id"),
    dict(path="a", script="x", script_name="-import.py"),
    dict(path="a", script="x", script_name=".hidden"),
    dict(path="a", script="x", script_name="noext"),
    dict(path="a", script="x", script_name="$(id).py"),
    dict(path="a", script="x", script_args=["-scriptPath", "/tmp"]),
    dict(path="a", script="x", script_args=["ok", "-IMPORT"]),
    dict(path="a", script="x", script_args=["-loader-baseAddr"]),
    dict(path="a", script_args=["x"]),
    dict(path="a", script="x" * (64 * 1024 + 1)),
    dict(path="a", extra="-processor 'unbalanced"),
    dict(path="a", script="x", script_args="'unbalanced"),
    dict(path="a", host="windows"),
    dict(path="a", host="kaly"),
], ids=lambda kw: repr(kw)[:50])
def test_ghidra_bad_input_refused_before_connecting(kw, captured):
    r = linux.linux_ghidra(**kw)
    assert r.ok is False
    assert captured == []


def test_ghidra_override_must_be_absolute(captured, monkeypatch):
    monkeypatch.setenv("KALI_GHIDRA_HEADLESS", "ghidra/analyzeHeadless")
    r = linux.linux_ghidra("a")
    assert r.ok is False and "KALI_GHIDRA_HEADLESS" in r.summary and captured == []


# ---------------------------------------------------------------- executed locally
def test_ghidra_local_success_args_reach_ghidra_verbatim(local, monkeypatch):
    monkeypatch.setenv("KALI_GHIDRA_HEADLESS", str(local["fake"]))
    script = "print('hi')\n# $(id) `whoami` 'q\" \\n\n"
    r = linux.linux_ghidra(NASTY, script=script, script_name="My_s-1.py",
                           script_args=[NASTY, "", "a b", "-x"], timeout=60)
    assert r.ok, r.summary
    args = _args(r.detail)
    assert args[0].startswith(str(local["home"] / "samples" / "_lh_ghidra_"))
    assert args[0].endswith("/proj") and args[1] == "p"
    assert args[2:6] == ["-import", "./" + NASTY, "-deleteProject", "-readOnly"]
    assert args[6] == "-scriptPath" and args[6 + 1].endswith("/scripts")
    assert args[8:] == ["-postScript", "My_s-1.py", NASTY, "", "a b", "-x"]
    assert "PROJDIR_EXISTS" in r.detail
    assert "SCRIPT_BODY<<\n" + script in r.detail + "\n"        # script text byte-exact
    assert f"analyzeHeadless: {local['fake']}" in r.known
    assert "Ghidra version: 11.4.2" in r.known
    assert not (local["home"] / "pwned").exists() and not (local["home"] / "samples" / "x.txt").exists()
    assert _leftovers(local["home"]) == []                      # cleaned up after success


def test_ghidra_local_found_on_path(local):
    local["env"]["PATH"] = f"{local['fake'].parent}:{local['env']['PATH']}"
    r = linux.linux_ghidra("a", analyze=False)
    assert r.ok
    assert "-noanalysis" in _args(r.detail)
    assert f"analyzeHeadless: {local['fake']}" in r.known


def test_ghidra_local_found_at_candidate_path(local, monkeypatch):
    monkeypatch.setattr(linux, "GHIDRA_HEADLESS_CANDIDATES",
                        ("/nonexistent/analyzeHeadless", str(local["fake"])))
    r = linux.linux_ghidra("a")
    assert r.ok and f"analyzeHeadless: {local['fake']}" in r.known


def test_ghidra_local_override_wins_over_path(local, monkeypatch, tmp_path):
    other = tmp_path / "other" / "analyzeHeadless"
    other.parent.mkdir()
    other.write_text("#!/bin/bash\necho WRONG_GHIDRA\n")
    other.chmod(0o755)
    local["env"]["PATH"] = f"{other.parent}:{local['env']['PATH']}"
    monkeypatch.setenv("KALI_GHIDRA_HEADLESS", str(local["fake"]))
    r = linux.linux_ghidra("a")
    assert r.ok and "WRONG_GHIDRA" not in r.detail and "ARG0=" in r.detail


def test_ghidra_local_not_installed_is_raw_ok_false(local):
    r = linux.linux_ghidra("a", script="print(1)")
    assert r.ok is False
    assert "analyzeHeadless: not found" in r.detail and "set KALI_GHIDRA_HEADLESS" in r.detail
    assert "exit 127" in r.summary
    assert len(local["cmds"]) == 1                               # one run, no install attempt
    assert not re.search(r"\b(apt|apt-get|pip|pipx|dpkg)\b", local["cmds"][0])
    assert _leftovers(local["home"]) == []


def test_ghidra_local_override_not_executable(local, monkeypatch, tmp_path):
    monkeypatch.setenv("KALI_GHIDRA_HEADLESS", str(tmp_path / "missing" / "analyzeHeadless"))
    r = linux.linux_ghidra("a")
    assert r.ok is False and "KALI_GHIDRA_HEADLESS=" in r.detail and "is not an executable file" in r.detail
    assert _leftovers(local["home"]) == []


@pytest.mark.parametrize("rc, sleep, timeout, timed_out", [
    (1, "", 60, False),        # Ghidra error (bad file, script error reported by Ghidra)
    (124, "", 60, True),       # GNU timeout fired
    (137, "1.2", 1, True),     # timeout had to SIGKILL after the grace
    (137, "", 60, False),      # a SIGKILL that is not the timeout (OOM): not relabelled
])
def test_ghidra_local_cleanup_on_every_failure_path(local, monkeypatch, rc, sleep, timeout,
                                                     timed_out):
    monkeypatch.setenv("KALI_GHIDRA_HEADLESS", str(local["fake"]))
    local["env"]["FAKE_RC"] = str(rc)
    if sleep:
        local["env"]["FAKE_SLEEP"] = sleep
    r = linux.linux_ghidra("a", script="print(1)", timeout=timeout)
    assert r.ok is False
    assert ("TIMEOUT" in r.summary) is timed_out
    assert f"analyzeHeadless exit code: {rc}" in r.known
    assert _leftovers(local["home"]) == []                      # gone on every path


def test_ghidra_local_cleanup_when_script_write_fails(local, monkeypatch):
    monkeypatch.setenv("KALI_GHIDRA_HEADLESS", str(local["fake"]))
    local["env"]["PATH"] = f"{local['shim']}"                    # no base64, no sed: write fails
    (local["shim"] / "mkdir").symlink_to(shutil.which("mkdir"))
    (local["shim"] / "rm").symlink_to(shutil.which("rm"))
    r = linux.linux_ghidra("a", script="print(1)")
    assert r.ok is False
    assert _leftovers(local["home"]) == []


def test_ghidra_local_concurrent_calls_use_their_own_dirs(local, monkeypatch):
    monkeypatch.setenv("KALI_GHIDRA_HEADLESS", str(local["fake"]))
    seen = []
    for _ in range(2):
        r = linux.linux_ghidra("a", script="print(1)")
        seen.append(_args(r.detail)[0])
    assert seen[0] != seen[1] and _leftovers(local["home"]) == []


def test_ghidra_sample_untouched_by_construction(captured):
    """-readOnly is always passed; nothing in the command writes to `path`."""
    for analyze in (True, False):
        linux.linux_ghidra("s.bin", analyze=analyze, script="pass")
    for c in captured:
        line = [x for x in shlex_inner(c["cmd"]) if "-import" in x][0]
        assert "-readOnly" in line and "-deleteProject" in line


def test_ghidra_hosts_setting_reads_per_host_key():
    env = {"KALI_GHIDRA_HEADLESS": " /opt/g/support/analyzeHeadless ",
           "WIN_2_GHIDRA_HEADLESS": "/x"}
    assert hosts.setting("kali", "GHIDRA_HEADLESS", env) == "/opt/g/support/analyzeHeadless"
    assert hosts.setting("win-2", "GHIDRA_HEADLESS", env) == "/x"
    assert hosts.setting("box", "GHIDRA_HEADLESS", env) == ""


_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_ghidra_documented():
    claude = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "linux_ghidra(" in claude and "_GHIDRA_HEADLESS" in claude
    assert "GHIDRA_HEADLESS" in (_ROOT / ".env.example").read_text(encoding="utf-8")
    skill = (_ROOT / "skills" / "binary-analysis" / "SKILL.md").read_text(encoding="utf-8")
    assert "linux_ghidra" in skill
    assert os.path.exists(_ROOT / ".claude" / "skills" / "binary-analysis" / "SKILL.md")


def test_ghidra_mcp_tool_hints_and_forwarding(monkeypatch):
    pytest.importorskip("mcp")
    import asyncio
    from labhand import mcp_server
    from labhand._result import VMResult
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    ann = tools["linux_ghidra"].annotations

    def attr(o, camel):
        return getattr(o, camel) if hasattr(o, camel) else \
            getattr(o, re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower())
    # runs an arbitrary caller script: like linux_gdb, not read-only
    hints = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
    assert tuple(attr(ann, h) for h in hints) == (False, True, False, True)
    assert tuple(attr(ann, h) for h in hints) == \
        tuple(attr(tools["linux_gdb"].annotations, h) for h in hints)
    seen = []
    monkeypatch.setattr(linux, "linux_ghidra",
                        lambda *a, **k: seen.append((a, k)) or VMResult(True, "g", detail="raw"))
    out = mcp_server.linux_ghidra("a.out", script="print(1)", script_args=["x", "y"],
                                  analyze=False, timeout=9, extra="-max-cpu 1", host="kali")
    assert "raw" in str(out)
    assert seen == [(("a.out",), dict(script="print(1)", script_name="labhand_ghidra.py",
                                      script_args=("x", "y"), analyze=False, host="kali",
                                      timeout=9, extra="-max-cpu 1"))]


def test_ghidra_local_files_written_by_the_script_are_reported(local, monkeypatch):
    fake = local["fake"]
    fake.write_text(FAKE_GHIDRA.replace('exit "${FAKE_RC:-0}"', 'echo hi > export.txt\nexit 0'))
    monkeypatch.setenv("KALI_GHIDRA_HEADLESS", str(fake))
    if not shutil.which("find") or subprocess.run(
            ["find", ".", "-maxdepth", "0", "-printf", ""], capture_output=True).returncode:
        pytest.skip("needs GNU find -printf (the workdir snapshot)")
    r = linux.linux_ghidra("a")
    assert "export.txt (3 bytes)" in r.detail


def test_ghidra_spill_failure_keeps_the_result(captured, monkeypatch):
    def broken(host, text, stem):
        raise OSError("sftp gone")
    monkeypatch.setattr(linux, "_spill", broken)
    r = linux.linux_ghidra("a")
    assert r.ok and "INFO  hello" in r.detail and "sftp gone" in r.detail


def test_ghidra_newfile_markers_are_reported(captured):
    captured.reply["out"] = captured.reply["out"].replace(
        "__RC=0", "__NEWFILE__ export.txt\t3\n__RC=0")
    r = linux.linux_ghidra("a")
    assert "export.txt (3 bytes)" in r.detail and "__NEWFILE__" not in r.detail


# ---------------------------------------------------------------- .py -> PyGhidra
def _launch_line(cmd):
    return [x for x in shlex_inner(cmd) if "-import" in x][0]


@pytest.mark.parametrize("script, name, pyg", [
    ("pass", "labhand_ghidra.py", True), ("pass", "S.PY", True),
    ("x", "Foo.java", False), ("", "labhand_ghidra.py", False), ("x", "a.pyc", False),
])
def test_ghidra_pyghidra_route_only_for_py_scripts(captured, script, name, pyg):
    linux.linux_ghidra("a", script=script, script_name=name)
    raw, line = shlex_inner(captured[0]["cmd"], raw=True), _launch_line(captured[0]["cmd"])
    assert line.startswith("timeout -k 5 300 python3 -P -m pyghidra.ghidra_launch --install-dir "
                           '"$__ghi" ghidra.app.util.headless.AnalyzeHeadless "$PWD/') is pyg
    assert line.startswith('timeout -k 5 300 "$__gh" "$PWD/') is (not pyg)
    assert ("    import pyghidra\n" in raw) is pyg
    assert "pyghidraRun" not in raw and "pip" not in raw.replace("pipe", "")


def test_ghidra_pyghidra_known_line(captured):
    captured.reply["out"] = ("__LH_GHIDRA_PATH__=/usr/bin/analyzeHeadless\n__LH_GHIDRA_VERSION__=12.1.3\n"
                             "__LH_GHIDRA_PYGHIDRA__=3.2.0 /usr/bin/python3\nINFO  x\n"
                             "__LH_GHIDRA_RC__=0 elapsed=6\n__RC=0")
    r = linux.linux_ghidra("a", script="pass")
    assert "PyGhidra (version, python): 3.2.0 /usr/bin/python3" in r.known
    assert "__LH_GHIDRA" not in r.detail


def test_ghidra_local_pyghidra_launch_args_verbatim(local, monkeypatch):
    monkeypatch.setenv("KALI_GHIDRA_HEADLESS", str(local["fake"]))
    script = "print('hi')\n# $(id) `whoami` 'q\" \\n\n"
    r = linux.linux_ghidra(NASTY, script=script, script_name="lh.py",
                           script_args=[NASTY, "", "a b", "-x"], timeout=60)
    assert r.ok, r.summary
    ghidra_dir = local["fake"].parent.parent.resolve()
    assert f"PYGHIDRA_LAUNCH install=[{ghidra_dir}]" in r.detail
    args = _args(r.detail)
    assert args[1:6] == ["p", "-import", "./" + NASTY, "-deleteProject", "-readOnly"]
    assert args[8:] == ["-postScript", "lh.py", NASTY, "", "a b", "-x"]
    assert "SCRIPT_BODY<<\n" + script in r.detail + "\n"
    assert "PyGhidra (version, python): 3.2.0 /fake/bin/python3" in r.known
    assert not (local["home"] / "samples" / "x.txt").exists()
    assert _leftovers(local["home"]) == []


def test_ghidra_local_java_runs_analyzeheadless_directly_not_pyghidra(local, monkeypatch):
    monkeypatch.setenv("KALI_GHIDRA_HEADLESS", str(local["fake"]))
    local["env"]["FAKE_NO_PYGHIDRA"] = "1"            # irrelevant for .java
    r = linux.linux_ghidra("a", script="class X {}", script_name="X.java")
    assert r.ok and "PYGHIDRA_LAUNCH" not in r.detail and "-postScript" in _args(r.detail)
    assert not any(k.startswith("PyGhidra") for k in r.known)


def test_ghidra_local_pyghidra_missing_is_raw_ok_false_never_installs(local, monkeypatch):
    monkeypatch.setenv("KALI_GHIDRA_HEADLESS", str(local["fake"]))
    local["env"]["FAKE_NO_PYGHIDRA"] = "1"
    r = linux.linux_ghidra("a", script="print(1)", script_name="s.py")
    assert r.ok is False and "exit 127" in r.summary
    assert "No module named 'pyghidra'" in r.detail and "write the post-script in Java" in r.detail
    assert "ARG0=" not in r.detail                    # Ghidra was never launched
    assert len(local["cmds"]) == 1
    assert not re.search(r"\b(apt|apt-get|pip|pipx|dpkg|venv|pyghidraRun)\b", local["cmds"][0])
    assert _leftovers(local["home"]) == []


@pytest.mark.parametrize("rc, timed_out", [(0, False), (1, False), (124, True)])
def test_ghidra_local_pyghidra_cleanup_and_timeout(local, monkeypatch, rc, timed_out):
    monkeypatch.setenv("KALI_GHIDRA_HEADLESS", str(local["fake"]))
    local["env"]["FAKE_RC"] = str(rc)
    r = linux.linux_ghidra("a", script="print(1)", script_name="s.py", timeout=60)
    assert "PYGHIDRA_LAUNCH" in r.detail
    assert r.ok is (rc == 0) and ("TIMEOUT" in r.summary) is timed_out
    assert f"analyzeHeadless exit code: {rc}" in r.known
    assert _leftovers(local["home"]) == []


# ---------------------------------------------------------------- round 2 (independent review)
def _java_hash_hex(text):
    h = 0
    for ch in text:
        h = (31 * h + ord(ch)) & 0xffffffff
    return "%x" % h


def test_ghidra_r2_f1_py_route_never_imports_from_the_samples_cwd(local, monkeypatch):
    """A planted samples/pyghidra/ (untrusted material lives in samples/) must not be
    imported: both python3 calls run with -P. Uses a REAL python3 (>= 3.11)."""
    import sys
    if sys.version_info < (3, 11):
        pytest.skip("needs python >= 3.11 for -P")
    monkeypatch.setenv("KALI_GHIDRA_HEADLESS", str(local["fake"]))
    (local["shim"] / "python3").unlink()
    (local["shim"] / "python3").symlink_to(sys.executable)
    pkg = local["home"] / "samples" / "pyghidra"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "import pathlib; pathlib.Path('HIJACKED_INIT').write_text('x')\n__version__ = 'PLANTED'\n")
    (pkg / "ghidra_launch.py").write_text(
        "import pathlib; pathlib.Path('HIJACKED').write_text('sample code ran')\n")
    r = linux.linux_ghidra("a.out", script="print(1)\n")       # default .py -> PyGhidra route
    wd = local["home"] / "samples"
    assert not (wd / "HIJACKED").exists() and not (wd / "HIJACKED_INIT").exists()
    assert not any("PLANTED" in k for k in r.known)
    assert r.ok is False and "ModuleNotFoundError: No module named 'pyghidra'" in r.detail
    assert "Traceback" not in r.detail                          # one line, not a traceback
    assert _leftovers(local["home"]) == []


def test_ghidra_r2_f1_both_python3_calls_use_safe_path(captured):
    linux.linux_ghidra("a", script="pass", script_name="s.py")
    raw = shlex_inner(captured[0]["cmd"], raw=True)
    calls = re.findall(r"\btimeout -k \d+ \d+ python3 (\S+) -[cm] ", raw)
    assert calls == ["-P", "-P"], calls                        # import check + launch


def test_ghidra_r2_f2_bundle_hash_matches_ghidra(tmp_path):
    """The bash port of Java's String.hashCode gives Ghidra's bundle dir name; anchor: the
    value measured live on Kali (Ghidra 12.1.3, 2026-11-20)."""
    measured = "/home/vahpem/samples/_lh_ghidra_35ef4d5a415e458b99148e79020d889b/scripts"
    assert _java_hash_hex(measured) == "f851c098"
    for text in (measured, "/a", "", "/home/u/s amples/_lh_ghidra_0/scripts", "x" * 300):
        out = subprocess.run(["bash", "-c", linux._GHIDRA_JHASH_FN + '\n__lh_jh "$1"', "-", text],
                             capture_output=True, text=True, check=True).stdout
        assert out == _java_hash_hex(text), text


@pytest.mark.parametrize("rc, xdg", [(0, False), (1, False), (124, False), (0, True)])
def test_ghidra_r2_f2_java_call_removes_its_compiled_bundle(local, monkeypatch, rc, xdg):
    import os
    import sys
    monkeypatch.setenv("KALI_GHIDRA_HEADLESS", str(local["fake"]))
    local["env"].update(FAKE_BUNDLE="1", FAKE_PY=sys.executable, FAKE_RC=str(rc))
    cfg = local["home"] / ".config"
    if xdg:
        cfg = local["home"] / "xdg"
        local["env"]["XDG_CONFIG_HOME"] = str(cfg)
    cb = cfg / "ghidra" / "ghidra_11.4.2_DEV" / "osgi" / "compiled-bundles"
    other = cb / "deadbeef"                              # someone else's, older: must stay
    other.mkdir(parents=True)
    os.utime(other, (1, 1))
    r = linux.linux_ghidra("a", script="class X {}", script_name="X.java", timeout=60)
    m = re.search(r"^BUNDLE=(.*)$", r.detail, re.M)
    assert m, r.detail                                    # the fake did write a bundle
    assert not pathlib.Path(m.group(1)).exists()          # ... and the call removed it
    assert sorted(p.name for p in cb.iterdir()) == ["deadbeef"]
    assert r.ok is (rc == 0) and _leftovers(local["home"]) == []


def test_ghidra_r2_f3_timeout_words_in_output_are_not_a_timeout(captured):
    captured.reply["out"] = ("__LH_GHIDRA_PATH__=/usr/bin/analyzeHeadless\n__LH_GHIDRA_VERSION__=12.1.3\n"
                             "INFO  Strings.java> str@0x4010: (TIMEOUT after 5 s) retrying\n"
                             "__LH_GHIDRA_RC__=0 elapsed=1\n__RC=0")
    r = linux.linux_ghidra("a", script="class S {}", script_name="S.java")
    assert r.ok is True and "TIMEOUT" not in r.summary
    assert "(TIMEOUT after 5 s) retrying" in r.detail       # raw output kept verbatim


def test_ghidra_r2_f3_dead_channel_still_reported_as_timeout(monkeypatch, no_network):
    def dead(cmd, host=None, read_timeout=None):
        return ("__LH_GHIDRA_PATH__=/x\nINFO  ANALYZING",
                f"\n(TIMEOUT after {read_timeout}s - the command was still running)")
    monkeypatch.setattr(remote, "ssh_exec", dead)
    r = linux.linux_ghidra("a", script="class S {}", script_name="S.java", timeout=60)
    assert r.ok is False and "TIMEOUT" in r.summary


def test_ghidra_r2_f4_lone_surrogate_in_script_is_ok_false(captured):
    import json
    script = json.loads('"print(\'\\ud800\')"')            # what a JSON-RPC argument can carry
    for name in ("s.java", "s.py"):
        r = linux.linux_ghidra("a.out", script=script, script_name=name)
        assert r.ok is False and "UTF-8" in r.summary
    assert captured == []                                  # refused before connecting


def _timeouts(text):
    """[(grace, duration)] of every `timeout [-k K] T` in `text`."""
    return [(int(k or 0), int(t)) for k, t in re.findall(r"\btimeout (?:-k (\d+) )?(\d+) ", text)]


@pytest.mark.parametrize("name", ["s.py", "S.java", None])
@pytest.mark.parametrize("t", [1, 60, 300, 3600])
def test_ghidra_r2_backstop_covers_every_bounded_phase(captured, name, t):
    """The outer backstop must outlast every phase the block bounds itself (PyGhidra import
    check + the inner analyzeHeadless timeout, each with its kill grace) with slack, so it never
    kills the wrapper (and runs the EXIT trap) while Ghidra still runs; ssh reads longer still."""
    kw = {"script": "pass", "script_name": name} if name else {}
    linux.linux_ghidra("a", timeout=t, **kw)
    c = captured[0]
    outer = _timeouts(c["cmd"].split(" bash -c ")[0] + " ")
    assert len(outer) == 1
    backstop = outer[0][1]
    inner = _timeouts(shlex_inner(c["cmd"], raw=True))
    assert len(inner) == (2 if name == "s.py" else 1)
    assert backstop >= sum(k + d for k, d in inner) + 20, (backstop, inner)
    assert c["read_timeout"] > backstop
