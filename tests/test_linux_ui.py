# -*- coding: utf-8 -*-
"""labhand.linux_ui (read and drive a Linux GUI through AT-SPI): VM-free.

Four layers:
* the pure helpers the driver is built from (tree walk + pruning, renderer, target parsing,
  element search, action and type-method choice) on fake accessors;
* the session's shell code (supervisor, inner script, start / stop / unknown-session paths)
  EXECUTED by a local bash against fake xvfb-run / dbus-run-session / timeout / setsid /
  flock / python3, so the private bus, the display, the env switches, the throw-away dirs,
  the lifetime bound and the cleanup are checked by running the generated code;
* the per-call AT-SPI driver EXECUTED by the local python against a fake `gi.repository.Atspi`
  and a fake xdotool (tree -> ids -> click by id / name, the type fallback);
* the Python tools with remote.ssh_exec / SFTP mocked (naming, unknown session, missing
  dependencies, stop / list parsing, max lifetime).
"""
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import textwrap

import pytest

from labhand import linux_ui as lu
from labhand import remote


# ============================================================ fakes for the pure helpers
class FakeAcc:
    """Accessor over nested dicts: {role, name, states, actions, text, desc, children}."""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.info_calls = 0

    def states(self, n):
        if n.get("name") in self.fail:
            raise RuntimeError("dead element")
        return n.get("states", [])

    def info(self, n):
        self.info_calls += 1
        if n.get("name") in self.fail:
            raise RuntimeError("dead element")
        return {"role": n.get("role", ""), "name": n.get("name", ""),
                "states": n.get("states", []), "n": len(n.get("children", [])),
                "actions": n.get("actions", []), "nact": len(n.get("actions", [])),
                "text": n.get("text"), "tlen": len(n.get("text") or ""), "desc": n.get("desc", "")}

    def child(self, n, i):
        kids = n.get("children", [])
        return kids[i] if i < len(kids) else None


SH = ["showing", "visible", "enabled", "sensitive"]


def el(role, name="", states=SH, children=(), **kw):
    d = {"role": role, "name": name, "states": list(states), "children": list(children)}
    d.update(kw)
    return d


def app_tree():
    menu_items = [el("menu item", "New      ", ["visible", "enabled", "sensitive"],
                     actions=["click"]),
                  el("menu item", "About", ["visible", "enabled", "sensitive"],
                     actions=["click"])]
    return el("application", "mousepad", [], [
        el("frame", "Untitled 1 - Mousepad", SH + ["active"], [
            el("menu bar", "", SH, [
                el("menu", "File", SH, menu_items, actions=["click"]),
                el("menu", "Edit", SH + ["selected"], [
                    el("menu item", "Undo", SH, actions=["click"])], actions=["click"])]),
            el("panel", "hidden panel", ["visible"], [el("button", "Ghost", SH)]),
            el("button", "Save", SH, actions=["click", "press", "activate", "a4", "a5", "a6"]),
            el("button", "Reload", ["visible", "showing"], actions=[]),
            el("text", "", SH + ["editable", "multi-line", "focused"], text="hello"),
            el("button", "", SH, desc="<html>CodeBrowser", actions=["click"]),
        ])])


# ============================================================ walk + pruning
def test_linux_ui_collect_prunes_non_showing_subtrees():
    nodes, notes = lu._ui_collect([app_tree()], FakeAcc())
    names = [n["name"] for n in nodes]
    assert "hidden panel" not in names and "Ghost" not in names
    assert "Save" in names and "Untitled 1 - Mousepad" in names
    assert notes == []


def test_linux_ui_collect_skips_closed_menus_but_walks_open_ones():
    nodes, _ = lu._ui_collect([app_tree()], FakeAcc())
    file_menu = next(n for n in nodes if n["name"] == "File")
    assert file_menu["cut"] == "closed menu: 2 items not walked"
    assert "About" not in [n["name"] for n in nodes]
    assert "Undo" in [n["name"] for n in nodes]          # Edit is open (selected)


def test_linux_ui_collect_showing_only_false_walks_everything():
    nodes, _ = lu._ui_collect([app_tree()], FakeAcc(), showing_only=False)
    names = [n["name"] for n in nodes]
    for n in ("Ghost", "hidden panel", "About", "New      "):
        assert n in names
    assert not any(n.get("cut") for n in nodes)


def test_linux_ui_collect_caps_depth_nodes_and_time():
    nodes, _ = lu._ui_collect([app_tree()], FakeAcc(), depth=1)
    frame = next(n for n in nodes if n["role"] == "frame")
    assert "below depth 1 not walked" in frame["cut"]
    assert max(n["d"] for n in nodes) == 1
    nodes, notes = lu._ui_collect([app_tree()], FakeAcc(), max_nodes=3)
    assert len(nodes) == 3 and "max_nodes" in notes[0] and "partial" in notes[0]
    ticks = iter(range(100))
    nodes, notes = lu._ui_collect([app_tree()], FakeAcc(), deadline=2, clock=lambda: next(ticks))
    assert len(nodes) < 5 and "time budget" in notes[0]


def test_linux_ui_collect_marks_an_app_without_showing_children():
    app = el("application", "java", [], [el("frame", "f", ["visible"])])
    nodes, _ = lu._ui_collect([app], FakeAcc())
    assert len(nodes) == 1 and "showing_only=False" in nodes[0]["cut"]


def test_linux_ui_collect_reports_an_unreadable_element_and_goes_on():
    nodes, _ = lu._ui_collect([app_tree()], FakeAcc(fail={"Save"}))
    bad = [n for n in nodes if n.get("err")]
    assert len(bad) == 1 and "dead element" in bad[0]["err"]
    assert "Reload" in [n["name"] for n in nodes]        # the walk went on
    assert "<unreadable element" in lu._ui_line(bad[0], 3)


# ============================================================ renderer
def test_linux_ui_line_format():
    nodes, _ = lu._ui_collect([app_tree()], FakeAcc())
    by = {n["name"]: n for n in nodes}
    save = lu._ui_line(by["Save"], 7)
    assert save == "    #7 [button] 'Save' actions=click|press|activate|a4|+2"
    assert "(disabled)" in lu._ui_line(by["Reload"], 1)
    text = lu._ui_line(next(n for n in nodes if n["role"] == "text"), 2)
    assert "(focused,editable,multi-line)" in text and "text='hello'" in text
    icon = lu._ui_line(next(n for n in nodes if n.get("desc")), 3)
    assert "desc='<html>CodeBrowser'" in icon
    assert "'New'" in lu._ui_line(el("menu item", "  New      ", SH), 1)   # name stripped
    long = lu._ui_line(dict(el("label", "x" * 500, SH), d=0), 1)
    assert len(long) < 140 and long.endswith("...'")


def test_linux_ui_render_limit_cuts_and_says_so():
    nodes, _ = lu._ui_collect([app_tree()], FakeAcc())
    text, ids, kept = lu._ui_render(nodes, limit=3)
    lines = text.splitlines()
    assert len(lines) == 4 and lines[0].startswith("#1 [application]")
    assert re.match(r"^\[\.\.\. %d more lines not shown \(limit=3\)" % (len(nodes) - 3), lines[3])
    assert set(ids) == {"1", "2", "3"} and kept == len(nodes)
    assert ids["2"] == {"path": [0, 0], "role": "frame", "name": "Untitled 1 - Mousepad",
                        "showing": True, "pn": 1}                # pn: the parent's child count


def test_linux_ui_render_match_keeps_subtrees_and_their_ancestors():
    nodes, _ = lu._ui_collect([app_tree()], FakeAcc())
    text, ids, kept = lu._ui_render(nodes, match="EDIT")   # case-insensitive, name or role
    names = [v["name"] for v in ids.values()]
    assert names == ["mousepad", "Untitled 1 - Mousepad", "", "Edit", "Undo"]
    assert "Save" not in text and kept == 5
    text, ids, _ = lu._ui_render(nodes, match="menu bar")  # by role: the whole subtree
    assert [v["name"] for v in ids.values()][-3:] == ["File", "Edit", "Undo"]
    text, ids, kept = lu._ui_render(nodes, match="no such thing")
    assert text == "" and ids == {} and kept == 0


# ============================================================ targets, search, actions, typing
def test_linux_ui_parse_target():
    assert lu._ui_parse_target("#12") == ("id", 12, None)
    assert lu._ui_parse_target(" 12 ") == ("id", 12, None)
    assert lu._ui_parse_target(12) == ("id", 12, None)
    assert lu._ui_parse_target("menu item:About") == ("match", "menu item", "About")
    assert lu._ui_parse_target("text:") == ("match", "text", "")
    assert lu._ui_parse_target("button:7") == ("match", "button", "7")
    assert lu._ui_parse_target("Ghidra: NO ACTIVE PROJECT") == (
        "match", None, "Ghidra: NO ACTIVE PROJECT")
    assert lu._ui_parse_target("weird role:x", roles=("weird role",)) == (
        "match", "weird role", "x")
    assert lu._ui_parse_target("")[0] == "error"
    assert lu._ui_parse_target(None)[0] == "error"
    assert lu._ui_parse_target(True)[0] == "error"


def test_linux_ui_find_prefers_exact_then_description_then_prefix_and_showing():
    nodes, _ = lu._ui_collect([app_tree()], FakeAcc(), showing_only=False)
    hits, how = lu._ui_find(nodes, "new")                 # 'New      ' after strip
    assert [h["name"] for h in hits] == ["New      "] and how == "exact name"
    hits, how = lu._ui_find(nodes, "<html>codebrowser")
    assert how == "exact description" and hits[0]["role"] == "button"
    hits, how = lu._ui_find(nodes, "Untitled")
    assert how == "name prefix" and hits[0]["role"] == "frame"
    hits, how = lu._ui_find(nodes, "", "text")
    assert how == "role" and len(hits) == 1
    hits, how = lu._ui_find(nodes, "mousepad")             # the app itself is not a target
    assert hits == [] and how is None
    two = el("application", "a", [], [el("button", "OK", ["visible"]), el("button", "OK", SH)])
    nodes, _ = lu._ui_collect([two], FakeAcc(), showing_only=False)
    hits, _ = lu._ui_find(nodes, "ok")
    assert len(hits) == 1 and "showing" in hits[0]["states"]   # showing beats hidden
    three = el("application", "a", [], [el("button", "OK", SH), el("button", "OK", SH)])
    nodes, _ = lu._ui_collect([three], FakeAcc())
    assert len(lu._ui_find(nodes, "OK")[0]) == 2               # ambiguous: both returned
    assert lu._ui_find(nodes, "OK", "menu item") == ([], None)


def test_linux_ui_choose_action():
    assert lu._ui_choose_action(["showContextMenu", "press", "click"]) == (2, "click", None)
    assert lu._ui_choose_action(["jump", "Toggle"]) == (1, "Toggle", None)
    assert lu._ui_choose_action(["a", "Press"], "press") == (1, "Press", None)
    i, n, why = lu._ui_choose_action(["jump"])
    assert i is None and "jump" in why and "action=" in why
    i, n, why = lu._ui_choose_action(["click"], "expand")
    assert i is None and "no action 'expand'" in why


def test_linux_ui_type_method_choice():
    assert lu._ui_type_plan(True, True) == ["set_text", "focus_xdotool"]
    assert lu._ui_type_plan(False, True) == ["focus_xdotool"]
    assert lu._ui_type_plan(True, False) == ["set_text"]
    assert lu._ui_type_plan(False, False) == []
    assert lu._ui_type_verified("a\nb", "a\nb")
    assert not lu._ui_type_verified("abc", "initial")
    assert not lu._ui_type_verified("abc", None)


# ============================================================ session naming
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*-[0-9a-f]{8}$")


def test_linux_ui_session_names_are_unique_and_safe():
    a = lu._new_session_name(None, "/usr/bin/mousepad --disable-server")
    b = lu._new_session_name(None, "/usr/bin/mousepad --disable-server")
    assert a != b and a.startswith("mousepad-") and NAME_RE.match(a)
    for name, cmd, stem in ((None, "env FOO=1 qterminal", "env"),
                            ("my app;rm -rf $(id)", "x", "my_app_rm_-rf___id_"),
                            ("../../etc", "x", "etc"), ("", "'unbalanced", "unbalanced"),
                            ("...", "x", "ui"), ("a" * 80, "x", "a" * 24)):
        s = lu._new_session_name(name, cmd)
        assert NAME_RE.match(s) and ".." not in s, s
        assert s.rsplit("-", 1)[0] == stem, (s, stem)
        assert lu._session_arg(s, "t")[0] == s


@pytest.mark.parametrize("bad", ["", None, "../x", "a/b", "$(id)", "a b", ".hidden",
                                 "x" * 65, "a..b", "-rf"])
def test_linux_ui_invalid_session_names_never_reach_the_host(bad, monkeypatch):
    monkeypatch.setattr(remote, "ssh_exec", lambda *a, **k: pytest.fail("must not connect"))
    monkeypatch.setattr(remote, "write_remote", lambda *a, **k: pytest.fail("must not write"))
    for r in (lu.linux_ui_tree(bad), lu.linux_ui_click(bad, "OK"), lu.linux_ui_type(bad, "x"),
              lu.linux_ui_screenshot(bad)):
        assert r.ok is False and "unknown session" in r.summary
    if bad not in ("", None):                     # "" / None = list mode of linux_ui_stop
        r = lu.linux_ui_stop(bad)
        assert r.ok is False and "unknown session" in r.summary


# ============================================================ executing the shell code
FAKE_TIMEOUT = """#!/bin/bash
echo "timeout $*" >> "$FAKE_LOG"
[ "$1" = -k ] && shift 2
shift
exec "$@"
"""
FAKE_XVFB_RUN = """#!/bin/bash
echo "xvfb-run $*" >> "$FAKE_LOG"
while [ $# -gt 0 ]; do case "$1" in -a) shift;; -s|-n|-f) shift 2;; *) break;; esac; done
x=$(mktemp -d "${TMPDIR:-/tmp}/xvfb-run.XXXXXX")
DISPLAY=:77 XAUTHORITY="$x/Xauthority" "$@"
rc=$?
rm -rf "$x"
exit $rc
"""
FAKE_DBUS_RUN_SESSION = """#!/bin/bash
echo "dbus-run-session $*" >> "$FAKE_LOG"
cfg=""
while [ $# -gt 0 ]; do case "$1" in --config-file=*) cfg=${1#*=}; shift;; --) shift; break;; *) shift;; esac; done
[ -n "$cfg" ] && { echo "BUSCONF<<"; cat "$cfg"; echo ">>"; ls "$(dirname "$cfg")/services"; } >> "$FAKE_LOG"
DBUS_SESSION_BUS_ADDRESS=unix:path=/private/test-bus "$@"
"""
# `sleep infinity` (inner.sh holding the session) returns at once, so the session ends and
# the supervisor's own cleanup runs; any other sleep is real
FAKE_SLEEP = """#!/bin/bash
[ "$1" = infinity ] && { [ -n "$FAKE_HOLD" ] && while [ ! -e "$FAKE_HOLD" ]; do /bin/sleep 0.05; done; exit 0; }
exec /bin/sleep "$@"
"""
# GNU rm has --one-file-system, BSD rm (macOS) does not: drop it here, the live runs use GNU
FAKE_RM = """#!/bin/bash
a=(); for x in "$@"; do [ "$x" = --one-file-system ] || a+=("$x"); done
exec /bin/rm "${a[@]}"
"""
FAKE_PY_OK = """#!/bin/bash
[ "$1" = -P ] || { echo "python3 without -P: $*"; exit 98; }
if [ "$2" = -c ]; then exit 0; fi
echo "DRIVER $2 $3"; echo "__LH_UI_RC=0"
"""


def _bin(tmp, **scripts):
    d = tmp / "bin"
    d.mkdir(exist_ok=True)
    for name, body in scripts.items():
        p = d / name.replace("_", "-")
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return d


def _fake_env(tmp, extra_path=True, **kw):
    env = {"PATH": str(tmp / "bin") + (":/usr/bin:/bin" if extra_path else ""),
           "HOME": str(tmp / "realhome"), "FAKE_LOG": str(tmp / "fake.log"),
           # what the SSH shell brings along: the REAL desktop's bus / display / a11y bus
           "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus", "DISPLAY": ":0",
           "XAUTHORITY": "/home/u/.Xauthority", "AT_SPI_BUS_ADDRESS": "unix:path=/real/a11y",
           "NO_AT_BRIDGE": "1", "XDG_RUNTIME_DIR": "/run/user/1000",
           "JAVA_TOOL_OPTIONS": "-Dreal=1"}
    env.update(kw)
    return env


@pytest.fixture
def session_files(tmp_path, monkeypatch):
    """A session dir as linux_ui_start lays it out, plus fake tools and service files."""
    svc = tmp_path / "dbus-services"
    svc.mkdir()
    for s in ("org.a11y.Bus", "ca.desrt.dconf", "org.gtk.vfs.Daemon",
              "org.freedesktop.portal.Documents"):
        (svc / f"{s}.service").write_text(f"[D-BUS Service]\nName={s}\n")
    monkeypatch.setattr(lu, "BUS_SERVICE_DIRS", (str(svc),))
    _bin(tmp_path, timeout=FAKE_TIMEOUT, xvfb_run=FAKE_XVFB_RUN,
         dbus_run_session=FAKE_DBUS_RUN_SESSION, sleep=FAKE_SLEEP, rm=FAKE_RM)
    wd = tmp_path / "samples"
    wd.mkdir()
    (tmp_path / "realhome").mkdir()
    d = wd / "_lh_ui_mp-0123abcd"
    for sub in ("home/.config", "home/.cache", "home/.local/share", "home/.local/state", "tmp",
                "run"):
        (d / sub).mkdir(parents=True)
    (d / "super.sh").write_text(lu._super_sh())
    (d / "inner.sh").write_text(lu._INNER_SH)
    out = tmp_path / "out"
    out.mkdir()
    (d / "cmd.sh").write_text(f"env > {out}/app_env.txt; pwd > {out}/app_pwd.txt; "
                              f"echo marker-{{}} > \"$HOME/.config/app-wrote-this\"\n")
    jar = tmp_path / "java-atk-wrapper.jar"
    jar.write_text("jar")
    return {"tmp": tmp_path, "wd": wd, "d": d, "out": out, "jar": jar}


def _env_file(p):
    return dict(line.split("=", 1) for line in p.read_text().splitlines() if "=" in line)


def test_linux_ui_session_runs_on_a_private_bus_and_display_with_throwaway_dirs(session_files):
    f = session_files
    d, tmp = f["d"], f["tmp"]
    r = subprocess.run(["bash", str(d / "super.sh"), str(d), "mp-0123abcd", "1800",
                        "1280x800x24", str(f["wd"]), str(f["jar"])],
                       env=_fake_env(tmp), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    env = _env_file(f["out"] / "app_env.txt")
    # private bus + Xvfb display, never the SSH shell's (= the real desktop's)
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/private/test-bus"
    assert env["DISPLAY"] == ":77" and env["XAUTHORITY"].startswith(f"{d}/tmp/xvfb-run.")
    assert "AT_SPI_BUS_ADDRESS" not in env and "NO_AT_BRIDGE" not in env
    # throw-away HOME / XDG dirs / runtime dir / TMPDIR, all inside the session dir
    assert env["HOME"] == f"{d}/home"
    assert env["XDG_CONFIG_HOME"] == f"{d}/home/.config"
    assert env["XDG_CACHE_HOME"] == f"{d}/home/.cache"
    assert env["XDG_DATA_HOME"] == f"{d}/home/.local/share"
    assert env["XDG_STATE_HOME"] == f"{d}/home/.local/state"
    assert env["XDG_RUNTIME_DIR"] == f"{d}/run" and env["TMPDIR"] == f"{d}/tmp"
    # the accessibility switches + the session marker
    for k, v in lu.A11Y_ENV:
        assert env[k] == v
    assert env["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] == "1" and env["GNOME_ACCESSIBILITY"] == "1"
    assert env["GIO_USE_VFS"] == "local" and env["GTK_USE_PORTAL"] == "0"
    assert env["JAVA_TOOL_OPTIONS"] == (
        f"-Xbootclasspath/a:{f['jar']} -Djavax.accessibility.assistive_technologies="
        "org.GNOME.Accessibility.AtkWrapper -XX:-UsePerfData")
    assert env["LABHAND_UI_SESSION"] == "mp-0123abcd"
    assert (f["out"] / "app_pwd.txt").read_text().strip() == str(f["wd"])   # app cwd = workdir
    log = (tmp / "fake.log").read_text()
    # lifetime bound, Xvfb with the screen, private bus with the allow-list config
    assert (f"timeout -k 10 1800 xvfb-run -a -s -screen 0 1280x800x24 dbus-run-session "
            f"--config-file={d}/bus.conf -- bash {d}/inner.sh {d} {f['wd']}") in log
    conf = log.split("BUSCONF<<", 1)[1].split(">>", 1)[0]
    assert f"<servicedir>{d}/services</servicedir>" in conf
    assert "standard_session_servicedirs" not in conf and "<type>session</type>" in conf
    listed = log.split(">>", 1)[1].split()
    assert sorted(listed) == ["ca.desrt.dconf.service", "org.a11y.Bus.service"]  # no gvfs/portal
    # the session ended (sleep infinity is fake): the supervisor removed the whole dir,
    # including what the app wrote to its HOME; the real HOME got nothing
    assert "__LH_SESSION_END rc=0" in r.stdout
    assert not d.exists()
    assert list((tmp / "realhome").iterdir()) == []


def test_linux_ui_session_without_the_jar_sets_no_java_options(session_files):
    f = session_files
    subprocess.run(["bash", str(f["d"] / "super.sh"), str(f["d"]), "mp-0123abcd", "60",
                    "800x600x16", str(f["wd"]), ""], env=_fake_env(f["tmp"]),
                   capture_output=True, text=True, timeout=60, check=True)
    env = _env_file(f["out"] / "app_env.txt")
    assert "JAVA_TOOL_OPTIONS" not in env                 # the SSH shell's value is dropped too
    assert "timeout -k 10 60 xvfb-run -a -s -screen 0 800x600x16" in (
        f["tmp"] / "fake.log").read_text()


def test_linux_ui_session_that_never_came_up_keeps_its_log_for_start(session_files):
    f = session_files
    (f["tmp"] / "bin" / "xvfb-run").write_text("#!/bin/bash\necho 'Xvfb failed' >&2; exit 1\n")
    r = subprocess.run(["bash", str(f["d"] / "super.sh"), str(f["d"]), "mp-0123abcd", "60",
                        "800x600x16", str(f["wd"]), ""], env=_fake_env(f["tmp"]),
                       capture_output=True, text=True, timeout=60)
    assert "__LH_SESSION_END rc=1" in r.stdout
    assert f["d"].exists() and not (f["d"] / "env.sh").exists()   # start reads it, then stops


def _run_body(body, wd, env, timeout=60):
    script = f"cd {wd} || exit 9\nWD=$(pwd)\n" + lu._BASH_FUNCS + body
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True,
                          timeout=timeout)


def test_linux_ui_start_body_runs_the_session_and_reports_it(session_files, tmp_path):
    f = session_files
    shutil.rmtree(f["d"])                                 # start creates it itself
    _bin(tmp_path, setsid='#!/bin/bash\nexec "$@"\n', flock="#!/bin/bash\nexit 0\n",
         python3=FAKE_PY_OK)
    hold = tmp_path / "release"
    env = _fake_env(tmp_path, FAKE_HOLD=str(hold))
    cmd = "echo started > \"$HOME/../../started.txt\"; env > " + str(f["out"]) + "/env2.txt"
    body = lu._start_body("mp-0123abcd", cmd, "1024x768x24", 120, 0, "_lh_uidrv_x.py",
                          {"session": "mp-0123abcd"}, jar=str(f["jar"]))
    r = _run_body(body, f["wd"], env)
    out = r.stdout
    d = f["d"]
    assert r.returncode == 0, out + r.stderr
    assert f"__LH_DIR={d}" in out and "__LH_DISPLAY=:77" in out
    assert re.search(r"^__LH_SUPER=\d+$", out, re.M) and re.search(r"^__LH_APP=\d+$", out, re.M)
    assert f"DRIVER _lh_uidrv_x.py {d}" in out             # the first look at the bus
    assert (d / "cmd.sh").read_text() == cmd + "\n"        # verbatim, no shell parsing
    assert json.loads((d / "meta.json").read_text()) == {"session": "mp-0123abcd"}
    assert oct((d / "run").stat().st_mode & 0o777) == "0o700"
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    envsh = (d / "env.sh").read_text()
    assert "DISPLAY=:77" in envsh and "DBUS_SESSION_BUS_ADDRESS=unix:path=/private/test-bus" in envsh
    assert "timeout -k 10 120 xvfb-run -a -s -screen 0 1024x768x24" in (
        tmp_path / "fake.log").read_text()
    # a second start with the same name refuses (atomic mkdir), and leaves the first alone
    r2 = _run_body(body, f["wd"], env)
    assert "__LH_EXISTS" in r2.stdout and r2.returncode == 4 and d.exists()
    hold.write_text("x")                                   # let the session end
    for _ in range(100):
        if not d.exists():
            break
        subprocess.run(["/bin/sleep", "0.05"])
    assert not d.exists()                                  # the supervisor's cleanup ran


def test_linux_ui_start_body_names_every_missing_dependency(tmp_path, monkeypatch):
    monkeypatch.setattr(lu, "BUS_SERVICE_DIRS", (str(tmp_path / "none"),))
    _bin(tmp_path, python3="#!/bin/bash\nexit 1\n", setsid="#!/bin/bash\n",
         timeout="#!/bin/bash\n", flock="#!/bin/bash\n")
    for tool in ("bash", "mkdir", "base64", "cat", "rm", "sed", "grep", "tr"):
        p = shutil.which(tool)
        if p:
            os.symlink(p, tmp_path / "bin" / tool)
    wd = tmp_path / "samples"
    wd.mkdir()
    body = lu._start_body("mp-1", "mousepad", "1280x800x24", 60, 0, "_lh_d.py", {})
    r = _run_body(body, wd, _fake_env(tmp_path, extra_path=False))
    assert r.returncode == 3
    missing = re.search(r"^__LH_MISSING=(.*)$", r.stdout, re.M).group(1).split()
    assert {"xvfb-run", "dbus-run-session", "python3-gi+AT-SPI-typelib(gir1.2-atspi-2.0)",
            "at-spi2-core(org.a11y.Bus-D-Bus-service)"} <= set(missing)
    assert "setsid" not in missing and "flock" not in missing
    assert list(wd.iterdir()) == []                         # nothing created


def test_linux_ui_drive_and_stop_bodies_on_an_unknown_session(tmp_path):
    wd = tmp_path / "samples"
    (wd / "_lh_ui_other-1").mkdir(parents=True)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    r = _run_body(lu._drive_body("ghost-1", "_lh_d.py"), wd, env)
    assert r.returncode == 5 and "__LH_UNKNOWN" in r.stdout and "__LH_LIVE=other-1" in r.stdout
    r = _run_body(lu._stop_body("ghost-1"), wd, dict(env, __LH_PROC=str(tmp_path / "noproc")))
    assert r.returncode == 5 and "__LH_UNKNOWN" in r.stdout
    (wd / "_lh_ui_starting-1").mkdir()                       # dir but no env.sh yet
    r = _run_body(lu._drive_body("starting-1", "_lh_d.py"), wd, env)
    assert r.returncode == 5 and "__LH_STARTING" in r.stdout


def _fake_proc(tmp, procs):
    root = tmp / "proc"
    for pid, envs in procs.items():
        (root / str(pid)).mkdir(parents=True)
        (root / str(pid) / "environ").write_bytes(b"".join(e.encode() + b"\0" for e in envs))
    return root


def test_linux_ui_marker_matches_exactly_one_session(tmp_path):
    proc = _fake_proc(tmp_path, {101: ["A=1", "LABHAND_UI_SESSION=mp-1"],
                                 102: ["LABHAND_UI_SESSION=mp-10"],
                                 103: ["X=LABHAND_UI_SESSION=mp-1"],
                                 104: ["LABHAND_UI_SESSION=mp-1", "B=2"], 105: ["HOME=/h"]})
    r = _run_body('__lh_marked "mp-1"', tmp_path, {"PATH": "/usr/bin:/bin",
                                                   "__LH_PROC": str(proc)})
    assert sorted(r.stdout.split()) == ["101", "104"]


def test_linux_ui_stop_body_removes_the_session_dir(session_files):
    f = session_files
    (f["d"] / "display").write_text(":77\n")
    (f["d"] / "super.pid").write_text("999999\n")
    env = _fake_env(f["tmp"], __LH_PROC=str(_fake_proc(f["tmp"], {})))
    r = _run_body(lu._stop_body("mp-0123abcd"), f["wd"], env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "__LH_STOPPED=" in r.stdout and re.search(r"^__LH_LEFT= *$", r.stdout, re.M)
    assert "__LH_DIR_LEFT" not in r.stdout and not f["d"].exists()


def test_linux_ui_list_body_lists_sessions_and_orphans(session_files):
    f = session_files
    (f["d"] / "started").write_text("1000\n")
    (f["d"] / "meta.json").write_text('{"command": "mousepad", "max_minutes": 30}\n')
    proc = _fake_proc(f["tmp"], {201: ["LABHAND_UI_SESSION=mp-0123abcd"],
                                 202: ["LABHAND_UI_SESSION=gone-1"]})
    script = lu._LIST_BODY.replace("/proc/[0-9]*/environ", f"{proc}/[0-9]*/environ") \
        .replace("s#^/proc/", f"s#^{proc}/")
    r = _run_body(script, f["wd"], dict(_fake_env(f["tmp"]), __LH_PROC=str(proc)))
    assert "__LH_SESS=mp-0123abcd procs=1 started=1000" in r.stdout
    assert '__LH_META={"command": "mousepad", "max_minutes": 30}' in r.stdout
    assert "__LH_ORPHAN=gone-1 pid=202" in r.stdout


# ============================================================ the driver, against a fake Atspi
FAKE_ATSPI = r'''
import json, os
_T = json.load(open(os.environ["FAKE_ATSPI_TREE"]))
_LOG = os.environ["FAKE_ATSPI_LOG"]
def _log(s):
    with open(_LOG, "a") as f:
        f.write(s + "\n")
class StateType:
    def __init__(self, v): self.value_nick = v
class _SS:
    def __init__(self, st): self.st = st
    def get_states(self): return list(self.st)
_ROLES = ["invalid", "button", "text", "menu item", "frame", "entry"]
class Role:
    def __init__(self, v):
        if not 0 <= v < len(_ROLES): raise ValueError(v)
        self.v = v
def role_get_name(r): return _ROLES[r.v]
class CoordType: SCREEN = 0
class _Rect:
    x, y, width, height = 10, 20, 100, 30
class Accessible:
    def __init__(self, d):
        self.d = d
        self.kids = [Accessible(c) for c in d.get("children", [])]
    def get_role_name(self): return self.d.get("role", "")
    def get_name(self): return self.d.get("name", "")
    def get_description(self): return self.d.get("desc", "")
    def get_state_set(self): return _SS(self.d.get("states", []))
    def get_child_count(self): return len(self.kids)
    def get_child_at_index(self, i): return self.kids[i]
    def get_action_iface(self): return self if "actions" in self.d else None
    def get_text_iface(self): return self if "text" in self.d else None
    def get_editable_text_iface(self): return _Edit(self) if self.d.get("editable") else None
    def get_component_iface(self): return self
class _Edit:
    def __init__(self, a): self.a = a
    def set_text_contents(self, t):
        _log("set_text_contents " + t)
        if self.a.d["editable"] == "works":
            self.a.d["text"] = t
        return True                     # Firefox: True although nothing changed
def _value(a):
    typed = os.environ.get("FAKE_XDO_TYPED")
    if a.d.get("keyboard") and typed and os.path.exists(typed):
        return open(typed).read()
    return a.d["text"]
class Text:
    @staticmethod
    def get_character_count(a): return len(_value(a))
    @staticmethod
    def get_text(a, s, e): return _value(a)[s:e]
class Action:
    @staticmethod
    def get_n_actions(a): return len(a.d["actions"])
    @staticmethod
    def get_action_name(a, i): return a.d["actions"][i]
    @staticmethod
    def do_action(a, i):
        _log("do_action %s %s" % (a.get_name(), a.d["actions"][i]))
        return True
class Component:
    @staticmethod
    def grab_focus(a):
        _log("grab_focus " + a.get_name()); return True
    @staticmethod
    def get_extents(a, ct): return _Rect()
def init(): return 0
def set_timeout(a, b): pass
_DESK = Accessible({"role": "desktop frame", "children": _T})
def get_desktop(i): return _DESK
'''
FAKE_XDOTOOL = """#!/bin/bash
echo "xdotool $*" >> "$FAKE_ATSPI_LOG"
if [ "$1" = type ]; then cat > "$FAKE_XDO_TYPED"; fi
exit 0
"""

DRIVER_TREE = [{"role": "application", "name": "app", "states": [], "children": [
    {"role": "frame", "name": "Main", "states": SH, "children": [
        {"role": "button", "name": "OK", "states": SH, "actions": ["press", "click"]},
        {"role": "button", "name": "OK", "states": SH, "actions": ["click"]},
        {"role": "button", "name": "Apply", "states": SH, "actions": ["jump"]},
        {"role": "text", "name": "gtk entry", "states": SH + ["editable"], "text": "old",
         "editable": "works"},
        {"role": "entry", "name": "firefox entry", "states": SH + ["editable"], "text": "initial",
         "editable": "ignores", "keyboard": True},
        {"role": "entry", "name": "chromium entry", "states": SH + ["editable"], "text": "",
         "keyboard": True},
        {"role": "text", "name": "no xdo entry", "states": SH, "text": ""},
    ]}]}]


@pytest.fixture
def driver(tmp_path):
    gi = tmp_path / "fakegi" / "gi"
    (gi / "repository").mkdir(parents=True)
    (gi / "__init__.py").write_text("def require_version(name, version):\n    pass\n")
    (gi / "repository" / "__init__.py").write_text("")
    (gi / "repository" / "Atspi.py").write_text(FAKE_ATSPI)
    tree = tmp_path / "tree.json"
    tree.write_text(json.dumps(DRIVER_TREE))
    sess = tmp_path / "session"
    sess.mkdir()
    log = tmp_path / "atspi.log"
    xbin = tmp_path / "xbin"
    xbin.mkdir()
    (xbin / "xdotool").write_text(FAKE_XDOTOOL)
    (xbin / "xdotool").chmod(0o755)

    def run(args, xdotool=True, gi_path=True, tree_data=None):
        if tree_data is not None:
            tree.write_text(json.dumps(tree_data))
        log.write_text("")
        src = tmp_path / "drv.py"
        src.write_text(lu._driver_source(dict({"after": 0}, **args)))
        env = {"PATH": (str(xbin) + ":" if xdotool else "") + "/usr/bin:/bin",
               "FAKE_ATSPI_TREE": str(tree), "FAKE_ATSPI_LOG": str(log),
               "FAKE_XDO_TYPED": str(tmp_path / "typed.txt")}
        if gi_path:
            env["PYTHONPATH"] = str(tmp_path / "fakegi")
        if (tmp_path / "typed.txt").exists():
            (tmp_path / "typed.txt").unlink()
        p = subprocess.run([sys.executable, str(src), str(sess)], env=env, capture_output=True,
                           text=True, timeout=60)
        rc = re.findall(r"^__LH_UI_RC=(\d+)$", p.stdout, re.M)
        return p.stdout + p.stderr, (int(rc[-1]) if rc else None), log.read_text()

    run.sess = sess
    return run


def test_linux_ui_driver_tree_writes_ids_and_click_by_id_verifies_them(driver):
    out, rc, _ = driver({"op": "tree"})
    assert rc == 0, out
    assert "#3 [button] 'OK' actions=press|click" in out
    ids = json.loads((driver.sess / "ids.json").read_text())["ids"]
    assert ids["3"] == {"path": [0, 0, 0], "role": "button", "name": "OK", "showing": True,
                        "pn": 7, "desc": "", "ext": [10, 20, 100, 30]}
    out, rc, log = driver({"op": "click", "target": "#3"})
    assert rc == 0, out
    assert "do_action OK click" in log                      # default: click before press
    assert "do_action('click') -> True" in out and "after: [button] 'OK'" in out
    assert "windows now:" in out and "app 'app': [frame] 'Main' (showing)" in out
    out, rc, log = driver({"op": "click", "target": "#3", "action": "press"})
    assert rc == 0 and "do_action OK press" in log
    # the element at that path is now something else -> refused, nothing clicked
    changed = json.loads(json.dumps(DRIVER_TREE))
    changed[0]["children"][0]["children"][0]["name"] = "Cancel"
    out, rc, log = driver({"op": "click", "target": "3"}, tree_data=changed)
    assert rc == 2 and "changed since the last tree" in out and "do_action" not in log
    out, rc, _ = driver({"op": "click", "target": "#99"})
    assert rc == 2 and "no element #99" in out


def test_linux_ui_driver_click_by_name_ambiguous_then_by_candidate_id(driver):
    out, rc, log = driver({"op": "click", "target": "OK"})
    assert rc == 2 and "ambiguous: 2 elements match 'OK'" in out and "do_action" not in log
    cand = re.findall(r"^#(\d+) \[button\] 'OK'", out, re.M)
    assert len(cand) == 2
    out, rc, log = driver({"op": "click", "target": "#" + cand[1]})
    assert rc == 0 and "do_action OK click" in log
    out, rc, log = driver({"op": "click", "target": "button:Apply"})
    assert rc == 2 and "no click/press/activate/toggle action" in out and "jump" in out
    out, rc, log = driver({"op": "click", "target": "button:Apply", "action": "jump"})
    assert rc == 0 and "do_action Apply jump" in log
    out, rc, _ = driver({"op": "click", "target": "Nothing Here"})
    assert rc == 2 and "no element matches 'Nothing Here'" in out


def test_linux_ui_driver_type_uses_editable_text_when_it_works(driver):
    out, rc, log = driver({"op": "type", "target": "gtk entry", "text": "new 'txt' $(id)"})
    assert rc == 0, out
    assert "set_text_contents new 'txt' $(id)" in log and "xdotool" not in log
    assert "read-back equals the text: yes" in out


def test_linux_ui_driver_type_falls_back_to_xdotool_when_set_text_is_ignored(driver):
    out, rc, log = driver({"op": "type", "target": "firefox entry", "text": "-typed via xdo"})
    assert rc == 0, out
    lines = log.splitlines()
    assert lines[0] == "set_text_contents -typed via xdo"   # tried first, ignored
    assert "grab_focus firefox entry" in lines
    assert "xdotool mousemove 60 35" in lines                # pointer to the element's centre
    assert "xdotool key ctrl+a" in lines
    assert "xdotool type --delay 12 --file -" in lines       # text on stdin, never argv
    assert "read back after set_text: 'initial'" in out
    assert "read-back equals the text: yes" in out


def test_linux_ui_driver_type_without_editable_text_goes_straight_to_xdotool(driver):
    out, rc, log = driver({"op": "type", "target": "chromium entry", "text": "abc",
                           "enter": True})
    assert rc == 0, out
    assert "set_text_contents" not in log and "xdotool key Return" in log
    out, rc, log = driver({"op": "type", "target": "no xdo entry", "text": "abc"},
                          xdotool=False)
    assert rc == 3 and "__LH_MISSING=xdotool" in out
    out, rc, log = driver({"op": "type", "target": "gtk entry", "text": "x", "enter": True},
                          xdotool=False)
    assert rc == 3 and "__LH_MISSING=xdotool" in out and "set_text" not in log


def test_linux_ui_driver_names_missing_python_gi(driver):
    out, rc, _ = driver({"op": "tree"}, gi_path=False)
    if rc != 3:                                  # the local python has a real gi: skip
        pytest.skip("this python can import gi")
    assert "__LH_MISSING=python3-gi with the AT-SPI typelib" in out


# ============================================================ the Python tools, SSH mocked
@pytest.fixture
def fake_host(monkeypatch):
    calls = {"exec": [], "written": [], "removed": []}
    replies = []

    def ssh_exec(cmd, host=None, read_timeout=None):
        calls["exec"].append(cmd)
        return (replies.pop(0) if replies else "__LH_RC=0\n"), ""

    monkeypatch.setattr(remote, "ssh_exec", ssh_exec)
    monkeypatch.setattr(remote, "write_remote",
                        lambda path, content, host=None: calls["written"].append((path, content)))
    monkeypatch.setattr(remote, "remove_quiet",
                        lambda names, host=None: calls["removed"].extend(names))
    calls["replies"] = replies
    return calls


STOP_CALL = '__lh_stop_dir "$N" "$D"'   # the call, not the shared function definition
START_OK = """__LH_DIR=/home/u/samples/_lh_ui_mousepad-1
__LH_DISPLAY=:99
__LH_SUPER=500
__LH_APP=510
__LH_PIDS=501 502 510
--- app output (tail) ---
apps on the session's AT-SPI bus: 'mousepad' (1 windows)
__LH_UI_RC=0
__LH_RC=0
"""


def test_linux_ui_start_returns_session_display_pids_and_state_dir(fake_host):
    fake_host["replies"].append(START_OK)
    r = lu.linux_ui_start("mousepad --disable-server", name="mousepad")
    assert r.ok, r.summary
    s = re.search(r"session (mousepad-[0-9a-f]{8}) started", r.summary).group(1)
    k = r.known[0]
    assert f"linux_ui session {s} on kali: display :99, supervisor pid 500, app pid 510" in k
    assert "pids 501 502 510" in k and "state in /home/u/samples/_lh_ui_mousepad-1" in k
    assert "'mousepad' (1 windows)" in r.detail
    cmd = fake_host["exec"][0]
    assert cmd.startswith("timeout -k 5 60 bash -c ")         # tree-killing timeout
    assert f"N={s};" in cmd and "MAXS=1800;" in cmd and "SCREEN=1280x800x24;" in cmd
    drv = fake_host["written"][0][0].rsplit("/", 1)[1]
    assert re.match(r"^_lh_uidrv_\d{10,}_\d+_[0-9a-f]{8}\.py$", drv)   # per-call scratch name
    assert fake_host["removed"] == [drv]                     # removed after the call


@pytest.mark.parametrize("minutes,seconds", [(30, 1800), (0.5, 60), (2.5, 150),
                                             (99999, 86400), ("x", 1800)])
def test_linux_ui_start_bounds_the_session_lifetime(fake_host, minutes, seconds):
    fake_host["replies"].append(START_OK)
    lu.linux_ui_start("mousepad", max_minutes=minutes)
    assert f"MAXS={seconds};" in fake_host["exec"][0]


def test_linux_ui_start_refuses_bad_input_before_connecting(fake_host):
    assert not lu.linux_ui_start("").ok
    assert not lu.linux_ui_start("x", screen="1280x800; rm -rf ~").ok
    assert "blocked" in lu.linux_ui_start("mkfs.ext4 /dev/sda").summary
    assert fake_host["exec"] == [] and fake_host["written"] == []


def test_linux_ui_start_missing_dependencies_are_named(fake_host):
    fake_host["replies"].append("__LH_MISSING=xvfb-run python3-gi+AT-SPI-typelib(gir1.2-atspi-2.0)"
                                "\n__LH_RC=3\n")
    r = lu.linux_ui_start("mousepad")
    assert not r.ok and "missing on kali: xvfb-run python3-gi" in r.summary
    assert "does not install" in r.summary
    assert len(fake_host["exec"]) == 1                       # no teardown needed: nothing made


def test_linux_ui_start_timeout_tears_the_session_down(fake_host):
    fake_host["replies"].append("partial\n__LH_RC=124\n")
    r = lu.linux_ui_start("mousepad", timeout=20)
    assert not r.ok and "TIMEOUT after 20s" in r.summary and "torn down" in r.summary
    s = re.search(r"session (\S+) was torn down", r.summary).group(1)
    assert len(fake_host["exec"]) == 2 and f"N={s};" in fake_host["exec"][1]
    assert STOP_CALL in fake_host["exec"][1]


def test_linux_ui_start_connection_failure_tears_down_and_removes_the_driver(fake_host,
                                                                            monkeypatch):
    seen = []

    def boom(cmd, host=None, read_timeout=None):
        seen.append(cmd)
        if len(seen) == 1:
            raise EOFError("channel died")
        return "__LH_RC=0\n", ""

    monkeypatch.setattr(remote, "ssh_exec", boom)
    r = lu.linux_ui_start("mousepad")
    assert not r.ok and "EOFError" in r.summary
    assert len(seen) == 2 and STOP_CALL in seen[1]
    assert len(fake_host["removed"]) == 1


def test_linux_ui_tree_unknown_session_lists_the_live_ones(fake_host):
    fake_host["replies"].append("__LH_UNKNOWN\n__LH_LIVE=mousepad-1\n__LH_LIVE=qt-2\n__LH_RC=5\n")
    r = lu.linux_ui_tree("ghost-12345678")
    assert not r.ok and "unknown session 'ghost-12345678' on kali" in r.summary
    assert "mousepad-1, qt-2" in r.summary
    assert len(fake_host["removed"]) == 1                    # driver removed on this path too


def test_linux_ui_tree_passes_its_options_to_the_driver(fake_host):
    fake_host["replies"].append("app pid 1 running; 1 app(s)\n#1 [application] 'x'\n"
                                "__LH_UI_RC=0\n__LH_RC=0\n")
    r = lu.linux_ui_tree("mp-1", match="Save", limit=9999, depth=0, showing_only=False,
                         timeout=40)
    assert r.ok and r.detail.startswith("app pid 1 running")
    src = fake_host["written"][0][1]
    args = json.loads(__import__("base64").b64decode(
        re.search(r"^_ARGS_B64 = '([^']+)'", src, re.M).group(1)))
    assert args == {"op": "tree", "match": "Save", "limit": 5000, "depth": 1,
                    "showing_only": False, "budget": 32}
    compile(src, "driver", "exec")


def test_linux_ui_tree_timeout_and_driver_failure_are_not_ok(fake_host):
    fake_host["replies"].append("half a tree\n__LH_RC=124\n")
    r = lu.linux_ui_tree("mp-1", timeout=10)
    assert not r.ok and "TIMEOUT after 10s" in r.summary
    fake_host["replies"].append("Traceback ...\n__LH_RC=1\n")
    assert not lu.linux_ui_tree("mp-1").ok


def test_linux_ui_tree_missing_gi_is_named(fake_host):
    fake_host["replies"].append("__LH_MISSING=python3-gi with the AT-SPI typelib "
                                "(gir1.2-atspi-2.0) - ModuleNotFoundError\n__LH_UI_RC=3\n"
                                "__LH_RC=3\n")
    r = lu.linux_ui_tree("mp-1")
    assert not r.ok and "missing on kali: python3-gi" in r.summary


def test_linux_ui_type_and_click_results(fake_host):
    fake_host["replies"].append("target: [text] ''\nread-back equals the text: NO\n"
                                "__LH_UI_RC=2\n__LH_RC=0\n")
    r = lu.linux_ui_type("mp-1", "hello", target="text:")
    assert not r.ok and "read-back equals the text: NO" in r.summary
    assert not lu.linux_ui_type("mp-1", "x" * (lu.TEXT_MAX + 1)).ok
    fake_host["replies"].append("target: x\ndo_action('click') -> True\nafter: [button] 'OK'\n"
                                "__LH_UI_RC=0\n__LH_RC=0\n")
    r = lu.linux_ui_click("mp-1", "#3")
    assert r.ok and "do_action('click') -> True" in r.known[0]


def test_linux_ui_stop_reports_what_it_stopped(fake_host):
    fake_host["replies"].append("__LH_STOPPED=500 501 510 \n__LH_LEFT=\n__LH_RC=0\n")
    r = lu.linux_ui_stop("mp-1")
    assert r.ok and "nothing left" in r.summary and "500 501 510" in r.detail
    fake_host["replies"].append("__LH_STOPPED=500 \n__LH_LEFT=777 \n__LH_DIR_LEFT\n__LH_RC=0\n")
    r = lu.linux_ui_stop("mp-1")
    assert not r.ok and "still running: 777" in r.detail and "STILL THERE" in r.detail
    fake_host["replies"].append("__LH_UNKNOWN\n__LH_RC=5\n")
    r = lu.linux_ui_stop("mp-1")
    assert not r.ok and "unknown session" in r.summary and "live sessions: none" in r.summary
    fake_host["replies"].append("__LH_ORPHANS=900 901\n__LH_STOPPED=900 901 \n__LH_LEFT=\n"
                                "__LH_RC=0\n")
    r = lu.linux_ui_stop("gone-1")
    assert r.ok and "orphan processes 900 901" in r.detail
    assert all(STOP_CALL in c for c in fake_host["exec"])


def test_linux_ui_stop_without_session_lists(fake_host):
    fake_host["replies"].append(
        "__LH_SESS=mp-1 procs=12 started=1000 now=1600\n"
        '__LH_META={"command": "mousepad --disable-server", "screen": "1280x800x24", '
        '"max_minutes": 30}\n__LH_ORPHAN=gone-1 pid=202\n__LH_RC=0\n')
    r = lu.linux_ui_stop()
    assert r.ok and r.summary == "linux_ui sessions on kali: 1"
    assert "mp-1: 12 processes, ends in 20 min; command: 'mousepad --disable-server'" in r.detail
    assert "ORPHAN process (session dir gone): gone-1 pid=202" in r.detail
    assert "_lh_ui_*" in fake_host["exec"][0] and STOP_CALL not in fake_host["exec"][0]


def test_linux_ui_screenshot_reads_and_removes_its_scratch_file(fake_host, monkeypatch):
    import base64
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAj"
                           "CB0C8AAAAASUVORK5CYII=")
    got = []
    monkeypatch.setattr(remote, "read_bytes", lambda p, host=None: got.append(p) or png)
    fake_host["replies"].append("__LH_SHOT_OK\n__LH_RC=0\n")
    r = lu.linux_ui_screenshot("mp-1")
    assert r.ok and r.image and r.image["media_type"].startswith("image/")
    shot = got[0].rsplit("/", 1)[1]
    assert re.match(r"^_lh_uishot_\d{10,}_\d+_[0-9a-f]{8}\.png$", shot)
    assert fake_host["removed"] == [shot] and f"SHOT={shot}" in fake_host["exec"][0]
    fake_host["replies"].append("__LH_MISSING=import (ImageMagick)\n__LH_RC=3\n")
    r = lu.linux_ui_screenshot("mp-1")
    assert not r.ok and "import (ImageMagick)" in r.summary


def test_linux_ui_tools_refuse_a_windows_host():
    for r in (lu.linux_ui_start("x", host="windows"), lu.linux_ui_tree("a-1", host="windows"),
              lu.linux_ui_stop(host="windows"), lu.linux_ui_stop("a-1", host="nope")):
        assert r.ok is False


def test_linux_ui_status_flags_are_whole_lines_only(fake_host):
    # an element NAMED like a status marker must not turn the result into "unknown session"
    fake_host["replies"].append("app pid 1 running\n#1 [label] 'x __LH_UNKNOWN y'\n"
                                "#2 [label] '__LH_UNKNOWN'\n__LH_UI_RC=0\n__LH_RC=0\n")
    r = lu.linux_ui_tree("mp-1")
    assert r.ok and "__LH_UNKNOWN" in r.detail
    assert lu._flag("a\n__LH_UNKNOWN\nb", "UNKNOWN") and not lu._flag("x__LH_UNKNOWN", "UNKNOWN")


def test_linux_ui_driver_refuses_a_session_without_bus_or_display(tmp_path):
    wd = tmp_path / "samples"
    d = wd / "_lh_ui_mp-1"
    d.mkdir(parents=True)
    (d / "env.sh").write_text("export DISPLAY=:99 XAUTHORITY='' DBUS_SESSION_BUS_ADDRESS='' "
                              "XDG_RUNTIME_DIR=/x\n")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}   # the real one
    r = _run_body(lu._drive_body("mp-1", "_lh_d.py"), wd, env)
    assert "__LH_NOBUS" in r.stdout and r.returncode == 7
    body = lu._session_dir_expr("mp-1") + lu._UNKNOWN_CHECK + "SHOT=x.png\n" + lu._SHOT_BODY_TAIL
    (d / "env.sh").write_text("export DISPLAY='' DBUS_SESSION_BUS_ADDRESS=unix:path=/p\n")
    _bin(tmp_path, **{"import": "#!/bin/bash\necho SHOULD-NOT-RUN\n"})
    r = _run_body(body, wd, dict(env, PATH=f"{tmp_path}/bin:/usr/bin:/bin", DISPLAY=":0"))
    assert "__LH_NOBUS" in r.stdout and "SHOULD-NOT-RUN" not in r.stdout


def test_linux_ui_stop_finds_the_supervisor_by_its_argv(tmp_path):
    proc = tmp_path / "proc"
    for pid, argv in {301: ["bash", "/w/_lh_ui_mp-1/super.sh", "/w/_lh_ui_mp-1", "mp-1"],
                      302: ["bash", "/w/_lh_ui_mp-10/super.sh"],
                      303: ["vim", "/w/_lh_ui_mp-1/super.sh.bak"]}.items():
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in argv))
    r = _run_body('__lh_super "/w/_lh_ui_mp-1"', tmp_path,
                  {"PATH": "/usr/bin:/bin", "__LH_PROC": str(proc)})
    assert r.stdout.split() == ["301"]


# ============================================================ round 2 (independent review)
def _drv_files(tmp, atspi=None):
    """A fake gi with the fake Atspi (or `atspi`), a session dir, a log; -> runner."""
    gi = tmp / "fakegi" / "gi"
    (gi / "repository").mkdir(parents=True, exist_ok=True)
    (gi / "__init__.py").write_text("def require_version(name, version):\n    pass\n")
    (gi / "repository" / "__init__.py").write_text("")
    (gi / "repository" / "Atspi.py").write_text(atspi or FAKE_ATSPI)
    sess = tmp / "session"
    (sess / "tmp").mkdir(parents=True, exist_ok=True)
    log, tj = tmp / "atspi.log", tmp / "tree.json"

    def start(args, tree):
        tj.write_text(json.dumps(tree))
        log.write_text("")
        src = tmp / ("drv_%s.py" % os.urandom(4).hex())
        src.write_text(lu._driver_source(dict({"after": 0}, **args)))
        return subprocess.Popen([sys.executable, str(src), str(sess)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(tmp / "fakegi"),
                                     "FAKE_ATSPI_TREE": str(tj), "FAKE_ATSPI_LOG": str(log)})

    def run(args, tree):
        p = start(args, tree)
        o, e = p.communicate(timeout=60)
        rc = re.findall(r"^__LH_UI_RC=(\d+)$", o, re.M)
        return o + e, (int(rc[-1]) if rc else None), log.read_text()

    run.start, run.sess = start, sess
    return run


SPOOF = "org.x.Error: no\n__LH_UNKNOWN\n__LH_MISSING=evil\n__LH_UI_RC=0"


def test_linux_ui_r2_marker_safe_neutralises_every_marker_line():
    t = "a\n__LH_UNKNOWN\n  __LH_X\n__LH_MISSING=x\nb __LH_EXISTS"
    s = lu._ui_safe(t)
    assert not re.search(r"^__LH_", s, re.M)
    assert "\n_LH_UNKNOWN\n" in s and "  __LH_X" in s and s.endswith("b __LH_EXISTS")
    assert not lu._flag(s, "UNKNOWN") and lu._marks(s, "MISSING") == []


def test_linux_ui_r2_marker_app_output_cannot_fake_start_markers(session_files, tmp_path,
                                                                  fake_host):
    """The live spoof of the review: an app that prints labhand's markers on its own lines."""
    f = session_files
    shutil.rmtree(f["d"])
    _bin(tmp_path, setsid='#!/bin/bash\nexec "$@"\n', flock="#!/bin/bash\nexit 0\n",
         python3=FAKE_PY_OK)
    hold = tmp_path / "release"
    env = _fake_env(tmp_path, FAKE_HOLD=str(hold))
    cmd = ("printf '__LH_MISSING=spoof\\n__LH_EXISTS\\n__LH_NOSTART\\n__LH_NODIR\\n__LH_UNKNOWN"
           "\\n__LH_PIDS=1 2 3\\n__LH_DISPLAY=:666\\n__LH_RC=3\\n'; exec /bin/sleep 2")
    try:
        r = _run_body(lu._start_body("mp-0123abcd", cmd, "800x600x24", 120, 1, "_lh_d.py",
                                     {"session": "mp-0123abcd"}), f["wd"], env)
    finally:
        hold.write_text("x")
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    assert "_LH_MISSING=spoof" in out                       # shown, neutralised
    for key in ("EXISTS", "NOSTART", "NODIR", "UNKNOWN"):
        assert not lu._flag(out, key), key
    assert lu._marks(out, "MISSING") == [] and lu._marks(out, "DISPLAY") == [":77"]
    assert len(lu._marks(out, "PIDS")) == 1
    fake_host["replies"].append(out + "__LH_RC=0\n")
    res = lu.linux_ui_start(cmd, name="mp")
    assert res.ok, res.summary
    assert "display :77" in res.known[0] and len(fake_host["exec"]) == 1   # no teardown


def test_linux_ui_r2_marker_session_log_cannot_fake_nostart_markers(session_files, tmp_path):
    """A session that does not come up: its logs are printed, neutralised."""
    f = session_files
    shutil.rmtree(f["d"])
    _bin(tmp_path, setsid='#!/bin/bash\nexec "$@"\n', flock="#!/bin/bash\nexit 0\n",
         python3=FAKE_PY_OK,
         xvfb_run="#!/bin/bash\necho '__LH_MISSING=from-xvfb'; echo '__LH_EXISTS'; exit 1\n")
    r = _run_body(lu._start_body("mp-0123abcd", "echo hi", "800x600x24", 120, 0, "_lh_d.py",
                                 {}), f["wd"], _fake_env(tmp_path))
    assert r.returncode == 6 and lu._flag(r.stdout, "NOSTART")
    assert "_LH_MISSING=from-xvfb" in r.stdout and lu._marks(r.stdout, "MISSING") == []
    assert not lu._flag(r.stdout, "EXISTS")


@pytest.mark.parametrize("reply", [
    "__LH_NODIR\n__LH_RC=6\n",
    "__LH_NOSTART\n--- session log ---\nXvfb failed\n__LH_STOPPED=\n__LH_LEFT=\n__LH_RC=6\n",
    START_OK.replace("__LH_UI_RC=0", "__LH_MISSING=python3-gi (driver)\n__LH_UI_RC=3"),
    START_OK.replace("apps on", "__LH_NOBUS\napps on"),
    "something broke\n__LH_RC=1\n",
])
def test_linux_ui_r2_marker_failed_start_always_tears_its_session_down(fake_host, reply):
    fake_host["replies"].append(reply)
    r = lu.linux_ui_start("mousepad", name="mp")
    assert not r.ok
    s = re.search(r"(mp-[0-9a-f]{8})", r.summary).group(1)   # the name is never hidden
    assert len(fake_host["exec"]) == 2 and STOP_CALL in fake_host["exec"][1]
    assert f"N={s};" in fake_host["exec"][1] and "torn down" in r.summary


def test_linux_ui_r2_marker_before_the_dir_exists_nothing_is_torn_down(fake_host):
    fake_host["replies"].append("__LH_EXISTS\n__LH_RC=4\n")
    assert "already exists" in lu.linux_ui_start("mousepad").summary
    fake_host["replies"].append("__LH_MISSING=xvfb-run\n__LH_RC=3\n")
    assert "missing on kali: xvfb-run" in lu.linux_ui_start("mousepad").summary
    assert len(fake_host["exec"]) == 2                       # no stop call after either


def test_linux_ui_r2_marker_driver_error_text_and_stderr_are_neutralised(tmp_path):
    atspi = FAKE_ATSPI + textwrap.dedent('''
        import sys as _sys
        def init():
            _sys.stderr.write("py warning\\n__LH_UNKNOWN\\n"); _sys.stderr.flush()
            os.write(2, b"(drv:1): Atspi-WARNING: x\\n__LH_MISSING=from-c-stderr\\n")
            return 0
        def _boom(a, i):
            raise RuntimeError(%r)
        Action.do_action = staticmethod(_boom)
        ''' % SPOOF)
    run = _drv_files(tmp_path, atspi)
    tree = [{"role": "application", "name": "app", "states": [], "children": [
        {"role": "frame", "name": "W", "states": SH, "children": [
            {"role": "button", "name": "Go", "states": SH, "actions": ["click"]}]}]}]
    out, rc, _ = run({"op": "tree"}, tree)
    assert rc == 0, out
    assert "_LH_UNKNOWN" in out and "_LH_MISSING=from-c-stderr" in out   # shown, neutralised
    assert not lu._flag(out, "UNKNOWN") and lu._marks(out, "MISSING") == []
    out, rc, _ = run({"op": "click", "target": "Go"}, tree)
    assert rc == 4 and "do_action('click') raised RuntimeError" in out
    assert re.findall(r"^__LH_UI_RC=(\d+)$", out, re.M) == ["4"]   # only labhand's own
    assert not lu._flag(out, "UNKNOWN") and lu._marks(out, "MISSING") == []
    assert list((run.sess / "tmp").iterdir()) == []          # the stderr file is removed


def _rows(rows, focused=1):
    return [{"role": "application", "name": "app", "states": [], "children": [
        {"role": "frame", "name": "Files", "states": SH, "children": [
            {"role": "button", "name": "Delete", "desc": f"row {r}", "actions": ["click"],
             "states": SH + (["focused"] if r == focused else [])} for r in rows]}]}]


def test_linux_ui_r2_id_same_compares_more_than_role_and_name():
    base = {"role": "button", "name": "Delete", "showing": True, "pn": 3, "desc": "row 1",
            "ext": [0, 10, 50, 20]}
    assert lu._ui_same(base, dict(base)) is None
    assert "parent had 3 children, now 2" in lu._ui_same(base, dict(base, pn=2))
    assert "description" in lu._ui_same(base, dict(base, desc="row 2"))
    assert "moved" in lu._ui_same(base, dict(base, ext=[0, 40, 50, 20]))
    assert "was [button]" in lu._ui_same(base, dict(base, name="Remove"))
    # extents only count when the element is showing then and now (a closed menu's items)
    assert lu._ui_same(dict(base, showing=False), dict(base, ext=[1, 2, 3, 4])) is None
    # an old entry without the extra fields still works on role + name
    assert lu._ui_same({"role": "button", "name": "Delete"}, dict(base)) is None


def test_linux_ui_r2_id_refuses_the_next_row_with_the_same_role_and_name(tmp_path):
    run = _drv_files(tmp_path)
    out, rc, _ = run({"op": "tree"}, _rows([1, 2]))
    ids = json.loads((run.sess / "ids.json").read_text())["ids"]
    row1 = [k for k, v in ids.items() if v["name"] == "Delete"][0]
    assert ids[row1]["desc"] == "row 1" and ids[row1]["pn"] == 2
    # row 1 removed: row 2's Delete slides into the path -> refused, nothing pressed
    out, rc, log = run({"op": "click", "target": "#" + row1}, _rows([2]))
    assert rc == 2 and "changed since the last tree" in out and "do_action" not in log
    # rows swapped (same count): the description tells them apart
    out, rc, log = run({"op": "click", "target": "#" + row1}, _rows([2, 1]))
    assert rc == 2 and "description was 'row 1'" in out and "do_action" not in log
    # unchanged tree: the id still works
    out, rc, log = run({"op": "click", "target": "#" + row1}, _rows([1, 2]))
    assert rc == 0 and "do_action Delete click" in log


def test_linux_ui_r2_id_ambiguous_candidates_carry_the_same_identity(tmp_path):
    run = _drv_files(tmp_path)
    out, rc, _ = run({"op": "click", "target": "Delete"}, _rows([1, 2]))
    assert rc == 2 and "ambiguous: 2 elements" in out
    cand = re.findall(r"^#(\d+) \[button\] 'Delete'", out, re.M)
    ids = json.loads((run.sess / "ids.json").read_text())["ids"]
    assert [ids[c]["desc"] for c in cand] == ["row 1", "row 2"]
    assert all(ids[c]["pn"] == 2 for c in cand)
    out, rc, log = run({"op": "click", "target": "#" + cand[0]}, _rows([2]))
    assert rc == 2 and "do_action" not in log


def test_linux_ui_r2_ids_lock_tree_and_resolve_wait_for_the_session_lock(tmp_path):
    import fcntl
    run = _drv_files(tmp_path)
    ids = run.sess / "ids.json"
    lockf = os.open(run.sess / "ids.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        p = run.start({"op": "tree"}, _rows([1, 2]))
        subprocess.run(["/bin/sleep", "1.5"])
        assert p.poll() is None and not ids.exists()        # waits for the lock
        fcntl.flock(lockf, fcntl.LOCK_UN)
        o, _ = p.communicate(timeout=30)
        assert "__LH_UI_RC=0" in o and ids.exists()
        before = json.loads(ids.read_text())
        # an ambiguous name-resolve merges its candidates under the same lock
        fcntl.flock(lockf, fcntl.LOCK_EX)
        p = run.start({"op": "click", "target": "Delete"}, _rows([1, 2]))
        subprocess.run(["/bin/sleep", "1.5"])
        assert p.poll() is None and json.loads(ids.read_text()) == before
        fcntl.flock(lockf, fcntl.LOCK_UN)
        o, _ = p.communicate(timeout=30)
        assert "ambiguous" in o
        after = json.loads(ids.read_text())
        assert all(after["ids"][k] == v for k, v in before["ids"].items())   # nothing lost
        assert after["next"] == before["next"] + 2
    finally:
        os.close(lockf)


def test_linux_ui_r2_ids_lock_gives_up_at_the_deadline(tmp_path):
    import fcntl
    run = _drv_files(tmp_path)
    lockf = os.open(run.sess / "ids.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        out, rc, _ = run({"op": "tree", "budget": 3}, _rows([1]))
        assert rc == 4 and "stayed locked" in out
    finally:
        os.close(lockf)


def test_linux_ui_r2_stopkill_stop_kills_every_marked_process(session_files):
    f = session_files
    (f["d"] / "display").write_text(":77\n")
    pids = {}
    for who in ("mp-0123abcd", "other-1"):                   # not children: no zombies
        p = subprocess.run(["bash", "-c", "/bin/sleep 60 >/dev/null 2>&1 & echo $!"],
                           capture_output=True, text=True)
        pids[who] = int(p.stdout.strip())
    try:
        proc = _fake_proc(f["tmp"], {pids["mp-0123abcd"]: ["A=1", "LABHAND_UI_SESSION=mp-0123abcd"],
                                     pids["other-1"]: ["LABHAND_UI_SESSION=other-1"]})
        # like the real /proc, a dead process drops out of the listing
        alive = ('eval "$(declare -f __lh_marked | sed \'1s/__lh_marked/__lh_marked_all/\')"\n'
                 '__lh_marked() { for p in $(__lh_marked_all "$1"); do '
                 'kill -0 "$p" 2>/dev/null && echo "$p"; done; }\n')
        env = _fake_env(f["tmp"], __LH_PROC=str(proc))
        r = _run_body(alive + lu._stop_body("mp-0123abcd"), f["wd"], env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert str(pids["mp-0123abcd"]) in lu._marks(r.stdout, "STOPPED")[0].split()
        assert lu._marks(r.stdout, "LEFT")[0].strip() == ""
        with pytest.raises(ProcessLookupError):
            os.kill(pids["mp-0123abcd"], 0)                  # the session's process is dead
        os.kill(pids["other-1"], 0)                          # another session's lives on
        assert not f["d"].exists()
    finally:
        for p in pids.values():
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass


def test_linux_ui_r2_drvname_tree_click_type_use_a_per_call_driver(fake_host):
    for call in (lambda: lu.linux_ui_tree("mp-1"), lambda: lu.linux_ui_click("mp-1", "#3"),
                 lambda: lu.linux_ui_type("mp-1", "x", target="#4"),
                 lambda: lu.linux_ui_tree("mp-1")):
        fake_host["replies"].append("ok\n__LH_UI_RC=0\n__LH_RC=0\n")
        call()
    names = [p.rsplit("/", 1)[1] for p, _ in fake_host["written"]]
    assert len(names) == 4 and len(set(names)) == 4          # a new name every call
    for n, cmd in zip(names, fake_host["exec"]):
        assert re.match(r"^_lh_uidrv_\d{10,}_\d+_[0-9a-f]{8}\.py$", n), n
        assert f"python3 -P {n} " in cmd                     # the body runs that file
    assert fake_host["removed"] == names                     # and removes it
