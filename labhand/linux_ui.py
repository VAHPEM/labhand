# -*- coding: utf-8 -*-
"""labhand.linux_ui — read and drive a Linux GUI as TEXT through AT-SPI (no screenshots).

The Linux twin of the `win_ui_*` family. A session is one GUI program started on a linux
host under its own Xvfb display and its own PRIVATE D-Bus session bus, kept alive between
calls so a Brain can read the accessibility tree, act, and read again, step by step:

    s = linux_ui_start("mousepad").known ...        # -> session name, e.g. mousepad-9f3a1c0e
    linux_ui_tree(s)                                # one line per element: #id [role] 'name' ...
    linux_ui_type(s, "hello", target="text:")       # EditableText, else focus + xdotool type
    linux_ui_click(s, "About")                      # AT-SPI action, then the element re-read
    linux_ui_stop(s)                                # app, Xvfb, bus, every child, the session dir

Measured basis: ~/.ccbridge/reviews/kali-atspi/REPORT.md (Kali ARM64, 2026-11-21).

Session layout (all under one per-session dir `<workdir>/_lh_ui_<session>/`, removed by
linux_ui_stop, or by the session itself when `max_minutes` runs out):
    super.sh   the supervisor: `timeout -k 10 <max> xvfb-run -a ... dbus-run-session -- bash
               inner.sh`, started under `setsid nohup`, so it outlives the SSH call; after the
               timeout it kills what is left of the session and removes the dir.
    inner.sh   runs inside Xvfb + the private bus: starts the app (cmd.sh), records DISPLAY /
               XAUTHORITY / DBUS_SESSION_BUS_ADDRESS in env.sh, then holds the session open.
    home/ run/ tmp/   throw-away HOME, XDG_{CONFIG,CACHE,DATA,STATE}_HOME, XDG_RUNTIME_DIR and
               TMPDIR: dconf, autosave, recently-used, at-spi sockets and xvfb-run's Xauthority
               dir never land in the user's real home or /run/user.
    ids.json   the element ids of the last tree (id -> child path + role + name + description,
               parent's child count, extents), so an act tool can resolve `#12` and refuse
               when that element changed meanwhile; written under a flock on ids.lock.
Every process of the session carries `LABHAND_UI_SESSION=<session>` in its environment;
linux_ui_stop and the expiry kill exactly the processes that carry it (TERM, then KILL).

Never the real display or bus: the SSH shell's DBUS_SESSION_BUS_ADDRESS is the logged-in
desktop's bus (measured: without a private bus a dump lists all 20 apps of the real XFCE
desktop on :0), so it, DISPLAY, XAUTHORITY and AT_SPI_BUS_ADDRESS are unset before
xvfb-run / dbus-run-session start.

Raw output, no interpretation. labhand never installs anything: a missing piece comes back
as ok=False naming it.
"""
import base64
import inspect
import json
import re
import secrets
import shlex
import time

from . import remote, hosts
from .hosts import workdir as _wd
from ._result import VMResult, _spill, _find_match
from .safety import _security_block
from .images import _prepare_image

# --- constants ------------------------------------------------------------------------
SESSION_PREFIX = "_lh_ui_"             # session dir = <workdir>/_lh_ui_<session>/
SESSION_ENV = "LABHAND_UI_SESSION"     # marker every process of a session carries
ATK_WRAPPER_JAR = "/usr/share/java/java-atk-wrapper.jar"
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SCREEN_RE = re.compile(r"^\d{2,5}x\d{2,5}x(8|16|24|32)$")
MAX_MINUTES_CAP = 24 * 60
TEXT_MAX = 50000                       # chars linux_ui_type accepts in one call

# The per-toolkit accessibility switches (REPORT.md "Other toolkits", all measured per
# process): GTK3/GTK4 need none; Qt5/Qt6 need QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
# (QT_ACCESSIBILITY=1 alone did nothing); Firefox needs GNOME_ACCESSIBILITY=1; Chromium
# needs GNOME_ACCESSIBILITY=1 AND `--force-renderer-accessibility` on ITS command line
# (either alone: no web content), which only the caller can add; Java needs java-atk-wrapper
# on the boot class path plus the assistive_technologies property (JAVA_TOOL_OPTIONS reaches
# every JVM, also one started by a launcher script that owns -cp).
A11Y_ENV = (("GNOME_ACCESSIBILITY", "1"), ("ACCESSIBILITY_ENABLED", "1"),
            ("QT_ACCESSIBILITY", "1"), ("QT_LINUX_ACCESSIBILITY_ALWAYS_ON", "1"),
            ("GDK_BACKEND", "x11"), ("QT_QPA_PLATFORM", "xcb"),
            # no gvfs / portals: measured 2026-11-21, with the session's own XDG_RUNTIME_DIR
            # the private bus activated gvfsd-fuse and xdg-document-portal, which mounted FUSE
            # file systems in <session>/run/{gvfs,doc}; mounts are global, so the REAL desktop's
            # xfdesktop showed them and wrote them into ~/.config/xfce4/desktop/
            ("GIO_USE_VFS", "local"), ("GTK_USE_PORTAL", "0"))
# The only D-Bus services the private bus may activate (an allow-list bus config replaces
# dbus-run-session's standard service dirs): the AT-SPI bus launcher, and dconf so GSettings
# writes land in the throw-away XDG_CONFIG_HOME instead of failing.
BUS_SERVICES = ("org.a11y.Bus", "ca.desrt.dconf")
BUS_SERVICE_DIRS = ("/usr/share/dbus-1/services", "/usr/local/share/dbus-1/services")
# never inherited from the SSH shell: the real desktop's bus / display / a11y bus
UNSET_ENV = ("DBUS_SESSION_BUS_ADDRESS", "DBUS_STARTER_ADDRESS", "DBUS_STARTER_BUS_TYPE",
             "AT_SPI_BUS_ADDRESS", "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY",
             "SESSION_MANAGER", "XDG_SESSION_ID", "XDG_SEAT", "XDG_VTNR",
             "GNOME_KEYRING_CONTROL", "PULSE_SERVER", "NO_AT_BRIDGE", "JAVA_TOOL_OPTIONS")


# --- pure helpers (also shipped to the host inside the per-call AT-SPI driver) --------------
# Everything from here to _DRIVER_MAIN is plain Python with no labhand import: the driver
# source is built from these functions with inspect.getsource, so the code the unit tests
# run is the code that runs on the host.

_UI_KEY_STATES = ("active", "focused", "selected", "checked", "pressed", "expanded",
                  "indeterminate", "editable", "read-only", "multi-line", "modal")
_UI_DEFAULT_ACTIONS = ("click", "press", "activate", "toggle")
_UI_ACTIONS_SHOWN = 4
_UI_NAME_MAX = 100
_UI_TEXT_MAX = 80
# role names of at-spi 2.5x (Atspi.role_get_name); the driver adds whatever the host's
# Atspi knows, so this list only has to cover the common ones
_UI_ROLES = ("alert", "application", "button", "push button", "toggle button", "check box",
             "radio button", "combo box", "dialog", "document web", "document frame",
             "entry", "password text", "text", "frame", "window", "label", "link",
             "list", "list item", "list box", "menu", "menu bar", "menu item",
             "check menu item", "radio menu item", "popup menu", "page tab", "page tab list",
             "panel", "filler", "scroll bar", "scroll pane", "separator", "slider",
             "spin button", "status bar", "table", "table cell", "tree", "tree item",
             "tree table", "tool bar", "tool tip", "heading", "paragraph", "image", "icon",
             "section", "static", "terminal", "canvas", "split pane", "viewport",
             "notification", "progress bar", "editbar", "form", "grouping")


def _ui_clip(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:max(n - 3, 1)] + "..."


def _ui_states(states, depth=1):
    """The states worth a token: the key ones in a fixed order, plus `disabled` (neither
    enabled nor sensitive) and `hidden` (not showing; only seen with showing_only=False)."""
    st = set(states or ())
    out = [s for s in _UI_KEY_STATES if s in st]
    if depth > 0 and states and "?" not in st:
        if "enabled" not in st and "sensitive" not in st:
            out.append("disabled")
        if "showing" not in st:
            out.append("hidden")
    return out


def _ui_line(rec, nid=None):
    """One element as one line: `#id [role] 'name' (states) actions=a|b text='...'`."""
    ind = "  " * int(rec.get("d", 0))
    head = ("#%d " % nid) if nid is not None else ""
    if rec.get("err"):
        return "%s%s<unreadable element: %s>" % (ind, head, rec["err"])
    name = _ui_clip((rec.get("name") or "").strip(), _UI_NAME_MAX)
    s = "%s%s[%s] %r" % (ind, head, rec.get("role") or "?", name)
    desc = (rec.get("desc") or "").strip()
    if not name and desc:
        s += " desc=%r" % _ui_clip(desc, _UI_TEXT_MAX)
    st = _ui_states(rec.get("states"), rec.get("d", 1))
    if st:
        s += " (" + ",".join(st) + ")"
    acts = list(rec.get("actions") or ())
    total = max(int(rec.get("nact") or 0), len(acts))
    if acts:
        shown = acts[:_UI_ACTIONS_SHOWN]
        s += " actions=" + "|".join(shown)
        if total > len(shown):
            s += "|+%d" % (total - len(shown))
    text = rec.get("text")
    if text is not None:
        editable = "editable" in (rec.get("states") or ())
        if editable or (text.strip() and text.strip() != (rec.get("name") or "").strip()):
            s += " text=%r" % _ui_clip(text, _UI_TEXT_MAX)
            tlen = int(rec.get("tlen") or len(text))
            if tlen > _UI_TEXT_MAX:
                s += " (%d chars)" % tlen
    if rec.get("cut"):
        s += "  [%s]" % rec["cut"]
    return s


def _ui_collect(roots, acc, showing_only=True, depth=30, max_nodes=6000, deadline=None,
                clock=None):
    """Walk the trees under `roots` (the apps on the session's AT-SPI desktop) in pre-order.

    `acc` reads one element: acc.states(obj) -> [state nicks], acc.info(obj) -> dict(role,
    name, states, n (child count), actions, nact, text, tlen, desc), acc.child(obj, i).
    Pruning (REPORT.md: a full Ghidra CodeBrowser tree was 1.78 M chars, 20-31 k showing):
    showing_only skips every element below an app that is not `showing` (and its subtree);
    a menu that is not open (not expanded/selected and its first item not showing) is not
    walked; `depth` and `max_nodes` cap the walk, `deadline` (clock() seconds) ends it early.
    Each cut is marked on its element. Returns (records, notes); a record is the info dict
    plus d (depth, apps = 0), path (child indices from the desktop) and obj."""
    clock = clock or time.time
    nodes, notes, stop = [], [], []

    def visit(obj, path, d):
        if stop:
            return
        if deadline is not None and clock() > deadline:
            stop.append("the time budget")
            return
        if len(nodes) >= max_nodes:
            stop.append("%d elements (max_nodes)" % max_nodes)
            return
        try:
            if showing_only and d > 0 and "showing" not in (acc.states(obj) or ()):
                return
            info = acc.info(obj)
        except Exception as e:  # noqa: BLE001 - a dead / hung element is reported, not fatal
            nodes.append({"d": d, "path": path, "role": "?", "name": "", "states": [],
                          "actions": [], "obj": obj,
                          "err": "%s: %s" % (type(e).__name__, str(e)[:80])})
            return
        rec = dict(info, d=d, path=path, obj=obj)
        nodes.append(rec)
        n = int(info.get("n") or 0)
        if n <= 0:
            return
        if showing_only and d > 0 and info.get("role") == "menu" and not (
                {"expanded", "selected"} & set(info.get("states") or ())):
            first = acc.child(obj, 0)
            try:
                first_showing = first is not None and "showing" in (acc.states(first) or ())
            except Exception:  # noqa: BLE001
                first_showing = False
            if not first_showing:
                rec["cut"] = "closed menu: %d items not walked" % n
                return
        if d >= depth:
            rec["cut"] = "%d children below depth %d not walked" % (n, depth)
            return
        before = len(nodes)
        for k in range(n):
            child = acc.child(obj, k)
            if child is not None:
                visit(child, path + (k,), d + 1)
        if d == 0 and showing_only and len(nodes) == before and not stop:
            rec["cut"] = ("none of its %d children is showing (showing_only=False walks them)"
                          % n)

    for i, root in enumerate(roots):
        if root is not None:
            visit(root, (i,), 0)
    if stop:
        notes.append("walk stopped at %s: the tree below is partial" % stop[0])
    return nodes, notes


def _ui_render(nodes, match=None, limit=250):
    """Records -> (text, ids, kept). ids: {"<n>": {path, role, name}} for every printed line.
    `match` keeps the subtrees whose root's name or role contains it (case-insensitive),
    plus their ancestors for context; `limit` caps the lines and says how many were cut."""
    limit = max(1, int(limit))
    if match:
        m = str(match).casefold()
        bypath = {tuple(r["path"]): i for i, r in enumerate(nodes)}
        keep = set()
        for i, r in enumerate(nodes):
            if m in (r.get("name") or "").casefold() or m in (r.get("role") or "").casefold():
                keep.add(i)
                p = tuple(r["path"])
                for k in range(1, len(p)):
                    if p[:k] in bypath:
                        keep.add(bypath[p[:k]])
                j = i + 1
                while j < len(nodes) and nodes[j]["d"] > r["d"]:
                    keep.add(j)
                    j += 1
        order = sorted(keep)
    else:
        order = list(range(len(nodes)))
    lines, ids = [], {}
    byp = {tuple(r["path"]): r for r in nodes}
    for i in order:
        if len(lines) >= limit:
            break
        r = nodes[i]
        n = len(lines) + 1
        ids[str(n)] = {"path": list(r["path"]), "role": r.get("role") or "",
                       "name": (r.get("name") or "").strip(),
                       "showing": "showing" in (r.get("states") or ())}
        parent = byp.get(tuple(r["path"])[:-1]) if r.get("d", 0) > 0 else None
        if parent is not None and not parent.get("err"):
            ids[str(n)]["pn"] = int(parent.get("n") or 0)
        lines.append(_ui_line(r, n))
    cut = len(order) - len(lines)
    if cut > 0:
        lines.append("[... %d more lines not shown (limit=%d): narrow with match= or raise "
                     "limit]" % (cut, limit))
    return "\n".join(lines), ids, len(order)


def _ui_where(rec, bypath):
    """`app 'x' > frame 'y'`: the app and window an element lives in."""
    parts = []
    p = tuple(rec["path"])
    for k in (1, 2):
        if len(p) > k and p[:k] in bypath:
            a = bypath[p[:k]]
            parts.append("[%s] %r" % (a.get("role"), _ui_clip((a.get("name") or "").strip(), 40)))
    return " > ".join(parts)


def _ui_parse_target(target, roles=()):
    """-> ("id", n, None) for `#12` / `12` / 12; ("match", role or None, name) for a name or
    `role:name` (role = an AT-SPI role name, e.g. `menu item:About`, `text:` = any text
    element); ("error", None, why) for an empty target. A numeric NAME needs `role:name`."""
    if isinstance(target, bool):
        return ("error", None, "target must be an element id or a name, not a boolean")
    if isinstance(target, int):
        return ("id", target, None)
    t = str(target if target is not None else "").strip()
    if not t:
        return ("error", None, "empty target: pass an element id from linux_ui_tree "
                               "(#12) or a name / role:name")
    m = re.match(r"^#?(\d{1,7})$", t)
    if m:
        return ("id", int(m.group(1)), None)
    if ":" in t:
        left, right = t.split(":", 1)
        known = {str(r).casefold() for r in tuple(roles) + _UI_ROLES}
        if left.strip() and left.strip().casefold() in known:
            return ("match", left.strip(), right.strip())
    return ("match", None, t)


def _ui_find(nodes, name, role=None):
    """Elements (not apps) matching `name` (stripped, case-insensitive): exact name first,
    then exact description (icon-only buttons), then name prefix (names carry padding and
    accelerators: 'New      ', 'New Project... Ctrl+N'). `role` filters by role; a role with
    no name matches every element of that role. Showing elements win over hidden ones.
    -> (matches, how)."""
    nm = (name or "").strip().casefold()
    rl = (role or "").strip().casefold() or None
    pool = [r for r in nodes if r.get("d", 0) > 0 and not r.get("err")
            and (rl is None or (r.get("role") or "").casefold() == rl)]
    hits, how = [], None
    if not nm:
        if rl:
            hits, how = pool, "role"
    else:
        for how, test in (
                ("exact name", lambda r: (r.get("name") or "").strip().casefold() == nm),
                ("exact description", lambda r: not (r.get("name") or "").strip()
                 and (r.get("desc") or "").strip().casefold() == nm),
                ("name prefix", lambda r: (r.get("name") or "").strip().casefold()
                 .startswith(nm))):
            hits = [r for r in pool if test(r)]
            if hits:
                break
        else:
            how = None
    showing = [r for r in hits if "showing" in (r.get("states") or ())]
    return (showing or hits), (how if hits else None)


def _ui_choose_action(actions, requested=None):
    """-> (index, name, None) or (None, None, why). Default: the first of
    click / press / activate / toggle the element has (in that order)."""
    names = [str(a) for a in (actions or ())]
    low = [a.casefold() for a in names]
    if requested:
        r = str(requested).strip().casefold()
        if r in low:
            i = low.index(r)
            return i, names[i], None
        return None, None, "no action %r on this element; it has: %s" % (
            requested, ", ".join(names) or "none")
    for want in _UI_DEFAULT_ACTIONS:
        if want in low:
            i = low.index(want)
            return i, names[i], None
    return None, None, ("no %s action on this element; it has: %s (pass action= to pick one)"
                        % ("/".join(_UI_DEFAULT_ACTIONS), ", ".join(names) or "none"))


def _ui_type_plan(has_editable, has_xdotool):
    """The order linux_ui_type tries: EditableText.set_text_contents when the element has
    it; then (or else) grab_focus + xdotool select-all + type. Measured: web text fields
    have no EditableText (Chromium) or ignore it (Firefox returns True, value unchanged),
    so the fallback also runs when the read-back after set_text is not the text."""
    plan = []
    if has_editable:
        plan.append("set_text")
    if has_xdotool:
        plan.append("focus_xdotool")
    return plan


def _ui_type_verified(text, readback):
    return readback is not None and readback == text


def _ui_safe(s):
    """Text labhand did not write (app output, element text, error messages, stderr), made
    unable to pose as a labhand status line: every line that starts with `__LH_` loses one
    underscore (`_LH_...`). The parsers only read whole lines starting with `__LH_`."""
    return re.sub(r"(?m)^__LH_", "_LH_", "" if s is None else str(s))


def _ui_same(stored, now):
    """None when the element now at an id's path is the one the tree printed, else why not.
    Role and name, plus what the tree stored beyond them: the description, the parent's
    child count (a removed / inserted sibling shifts child-index paths: the next row's
    'Delete' would slide into the old path) and, when the element was showing then and is
    showing now, its screen extents."""
    if now.get("role") != stored.get("role") or now.get("name") != stored.get("name"):
        return "was [%s] %r, now [%s] %r" % (stored.get("role"), stored.get("name"),
                                            now.get("role"), now.get("name"))
    if "pn" in stored and now.get("pn") != stored["pn"]:
        return "its parent had %s children, now %s" % (stored["pn"], now.get("pn"))
    if "desc" in stored and "desc" in now and now["desc"] != stored["desc"]:
        return "description was %r, now %r" % (stored["desc"], now["desc"])
    if (stored.get("showing") and now.get("showing") and stored.get("ext") and now.get("ext")
            and list(now["ext"]) != list(stored["ext"])):
        return "it moved: extents were %s, now %s" % (stored["ext"], now["ext"])
    return None


# --- the driver's main part (runs on the host, inside the session's env) ------------------
_DRIVER_MAIN = r'''
import base64, fcntl, json, os, shutil, subprocess, sys, warnings
warnings.simplefilter("ignore")
A = json.loads(base64.b64decode(_ARGS_B64).decode("utf-8"))
D = sys.argv[1]
OP = A["op"]
DEADLINE = time.time() + max(3.0, float(A.get("budget", 30)))

# stderr (Python's and the C libraries': GLib / libatspi warnings can quote an app's D-Bus
# error text) goes to a per-call file and comes back through out(), neutralised, at the end
ERRF = None
try:
    _ed = os.path.join(D, "tmp") if os.path.isdir(os.path.join(D, "tmp")) else D
    ERRF = os.path.join(_ed, "drv-%d-%s.err" % (os.getpid(), os.urandom(4).hex()))
    _efd = os.open(ERRF, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    sys.stderr.flush()
    os.dup2(_efd, 2)
    os.close(_efd)
except Exception:
    ERRF = None


def out(s=""):  # everything but labhand's own status lines: can never start with __LH_
    sys.stdout.write(_ui_safe(s) + "\n")


def mark(s):  # labhand's own status line, one line
    sys.stdout.write("__LH_" + str(s).replace("\r", " ").replace("\n", " ") + "\n")


def done(rc, msg=None, status=None):
    if ERRF:  # before msg: the tools take a result's last line as its summary
        try:
            sys.stderr.flush()
            with open(ERRF, "rb") as f:
                err = f.read()[-3000:].decode("utf-8", "replace").strip()
            os.unlink(ERRF)
            if err:
                out("driver stderr:\n" + err)
        except Exception:
            pass
    if msg:
        out(msg)
    if status:
        mark(status)
    mark("UI_RC=%d" % rc)
    sys.stdout.flush()
    os._exit(rc)


try:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
except Exception as e:
    done(3, status="MISSING=python3-gi with the AT-SPI typelib (gir1.2-atspi-2.0) - %s: %s"
         % (type(e).__name__, e))

try:
    Atspi.init()
except Exception:
    pass
try:  # per D-Bus call: a hung app costs seconds, not the whole call
    Atspi.set_timeout(int(A.get("call_ms", 4000)), int(A.get("start_ms", 15000)))
except Exception:
    pass


def nicks(a):
    try:
        return [Atspi.StateType(s).value_nick for s in a.get_state_set().get_states()]
    except Exception:
        return ["?"]


class Acc:
    def states(self, a):
        return nicks(a)

    def info(self, a):
        role = a.get_role_name() or ""
        name = a.get_name() or ""
        st = nicks(a)
        try:
            n = int(a.get_child_count())
        except Exception:
            n = 0
        acts, nact = [], 0
        try:
            ai = a.get_action_iface()
            if ai is not None:
                nact = int(Atspi.Action.get_n_actions(ai))
                acts = [Atspi.Action.get_action_name(ai, i) for i in range(min(nact, 6))]
        except Exception:
            pass
        text, tlen = None, 0
        try:
            ti = a.get_text_iface()
            if ti is not None:
                tlen = int(Atspi.Text.get_character_count(ti))
                text = Atspi.Text.get_text(ti, 0, min(tlen, 200))
        except Exception:
            pass
        desc = ""
        if not name.strip():
            try:
                desc = a.get_description() or ""
            except Exception:
                pass
        return {"role": role, "name": name, "states": st, "n": max(n, 0), "actions": acts,
                "nact": nact, "text": text, "tlen": tlen, "desc": desc}

    def child(self, a, i):
        try:
            return a.get_child_at_index(i)
        except Exception:
            return None


ACC = Acc()


def roots():
    desk = Atspi.get_desktop(0)
    return [ACC.child(desk, i) for i in range(desk.get_child_count())]


def role_names():
    names = set()
    for v in range(0, 160):
        try:
            nm = Atspi.role_get_name(Atspi.Role(v))
        except Exception:
            continue
        if nm:
            names.add(nm)
    return tuple(names)


def app_status():
    try:
        pid = int(open(os.path.join(D, "app.pid")).read().strip())
    except Exception:
        return "app: no pid recorded"
    try:
        rc = open(os.path.join(D, "app.rc")).read().strip()
    except Exception:
        rc = ""
    if rc:
        return "app pid %d exited (rc %s)" % (pid, rc)
    try:
        os.kill(pid, 0)
        return "app pid %d running" % pid
    except OSError:
        return "app pid %d not running" % pid


IDS = os.path.join(D, "ids.json")


def load_ids():
    try:
        with open(IDS) as f:
            m = json.load(f)
        if isinstance(m, dict) and isinstance(m.get("ids"), dict):
            return m
    except Exception:
        pass
    return {"ids": {}, "next": 1}


def save_ids(m):
    tmp = "%s.%d.%s.tmp" % (IDS, os.getpid(), os.urandom(4).hex())
    with open(tmp, "w") as f:
        json.dump(m, f)
    os.replace(tmp, IDS)


class IdsLock:
    """Exclusive flock on <session>/ids.lock around every write of ids.json (a tree's new
    map, a name-resolve's read-modify-write of candidates), so two calls of one session
    never lose each other's ids. Released when the process ends, whatever happens."""

    def __enter__(self):
        self.fd = os.open(os.path.join(D, "ids.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, PermissionError):
                if time.time() > DEADLINE - 1:
                    os.close(self.fd)
                    raise RuntimeError("the session's ids.json stayed locked by another "
                                       "linux_ui call of this session")
                time.sleep(0.05)

    def __exit__(self, *exc):
        os.close(self.fd)
        return False


def ident(obj):
    """What an id stores beyond role and name: description, screen extents."""
    r = {}
    try:
        r["desc"] = (obj.get_description() or "").strip()
    except Exception:
        pass
    try:
        ci = obj.get_component_iface()
        if ci is not None:
            e = Atspi.Component.get_extents(ci, Atspi.CoordType.SCREEN)
            r["ext"] = [int(e.x), int(e.y), int(e.width), int(e.height)]
    except Exception:
        pass
    return r


def id_entry(r, bypath):
    """A record (from _ui_collect) -> its ids.json entry, as _ui_render stores it + ident."""
    e = {"path": list(r["path"]), "role": r.get("role") or "",
         "name": (r.get("name") or "").strip(), "showing": "showing" in (r.get("states") or ())}
    p = bypath.get(tuple(r["path"])[:-1]) if r.get("d", 0) > 0 else None
    if p is not None and not p.get("err"):
        e["pn"] = int(p.get("n") or 0)
    e.update(ident(r["obj"]))
    return e


def resolve(target):
    """-> (obj, label, None) or (None, None, why)."""
    kind, a, b = _ui_parse_target(target, role_names())
    if kind == "error":
        return None, None, b
    if kind == "id":
        e = load_ids()["ids"].get(str(a))
        if not e:
            return None, None, ("no element #%d in this session's last tree: read "
                                "linux_ui_tree again" % a)
        obj, parent = None, None
        try:
            obj = Atspi.get_desktop(0)
            for k in e["path"]:
                parent = obj
                obj = obj.get_child_at_index(k)
                if obj is None:
                    break
        except Exception:
            obj = None
        if obj is None:
            return None, None, ("element #%d (%s %r) is gone: read linux_ui_tree again"
                                % (a, e["role"], e["name"]))
        try:
            now = {"role": obj.get_role_name() or "", "name": (obj.get_name() or "").strip(),
                   "showing": "showing" in nicks(obj)}
            if "pn" in e:
                now["pn"] = int(parent.get_child_count())
        except Exception as x:
            return None, None, "element #%d cannot be read (%s: %s)" % (a, type(x).__name__, x)
        now.update(ident(obj))
        why = _ui_same(e, now)
        if why:
            return None, None, ("element #%d changed since the last tree (%s): read "
                                "linux_ui_tree again" % (a, why))
        return obj, "#%d [%s] %r" % (a, now["role"], now["name"]), None
    nodes, notes = _ui_collect(roots(), ACC, showing_only=False, depth=40,
                               max_nodes=int(A.get("max_nodes", 8000)),
                               deadline=DEADLINE - 3)
    best, how = _ui_find(nodes, b, a)
    if not best:
        return None, None, "no element matches %r%s (%d elements searched%s)" % (
            b, (" with role %r" % a) if a else "", len(nodes),
            ("; " + "; ".join(notes)) if notes else "")
    if len(best) > 1:
        bypath = {tuple(r["path"]): r for r in nodes}
        entries = [id_entry(r, bypath) for r in best[:15]]
        lines = []
        with IdsLock():  # read-modify-write: a tree saving meanwhile is not overwritten
            m = load_ids()
            nxt = int(m.get("next", 1))
            for r, ent in zip(best[:15], entries):
                m["ids"][str(nxt)] = ent
                lines.append(_ui_line(dict(r, d=0), nxt) + "   in " + _ui_where(r, bypath))
                nxt += 1
            m["next"] = nxt
            save_ids(m)
        more = ("\n[... %d more]" % (len(best) - 15)) if len(best) > 15 else ""
        return None, None, ("ambiguous: %d elements match %r (%s); pass one id as target:\n%s%s"
                            % (len(best), target, how, "\n".join(lines), more))
    r = best[0]
    return r["obj"], "[%s] %r (matched by %s)" % (r["role"], (r["name"] or "").strip(), how), None


def describe(obj):
    try:
        info = ACC.info(obj)
    except Exception as e:
        return "gone or unreadable (%s: %s)" % (type(e).__name__, str(e)[:100])
    return _ui_line(dict(info, d=0)) + "\n    all states: " + ",".join(info["states"])


def windows_now():
    lines = []
    for i, app in enumerate(roots()):
        if app is None:
            continue
        try:
            ws = []
            for k in range(min(app.get_child_count(), 30)):
                w = app.get_child_at_index(k)
                if w is None:
                    continue
                st = [s for s in nicks(w) if s in ("active", "modal", "showing")]
                ws.append("[%s] %r%s" % (w.get_role_name(), _ui_clip((w.get_name() or "").strip(), 60),
                                         (" (" + ",".join(st) + ")") if st else ""))
            lines.append("app %r: %s" % (app.get_name() or "", "; ".join(ws) or "no windows"))
        except Exception as e:
            lines.append("app #%d: unreadable (%s)" % (i, e))
    return lines


def readback(obj):
    try:
        ti = obj.get_text_iface()
        if ti is None:
            return None
        n = int(Atspi.Text.get_character_count(ti))
        return Atspi.Text.get_text(ti, 0, min(n, 200000))
    except Exception:
        return None


XDO = shutil.which("xdotool")


def xdo(*args, **kw):
    try:
        p = subprocess.run([XDO] + list(args), input=kw.get("inp"), capture_output=True,
                           timeout=max(3.0, DEADLINE - time.time()))
    except Exception as e:
        return 99, "%s: %s" % (type(e).__name__, e)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace").strip()


def xdo_type(text):  # the text goes in on stdin: no argv, no option parsing of the text
    return xdo("type", "--delay", "12", "--file", "-", inp=text.encode("utf-8"))


def focus(obj):
    notes = []
    try:
        ci = obj.get_component_iface()
        if ci is None:
            return "no Component interface: focus not moved"
        notes.append("grab_focus -> %s" % Atspi.Component.grab_focus(ci))
        ext = Atspi.Component.get_extents(ci, Atspi.CoordType.SCREEN)
        if ext.width > 0 and ext.height > 0 and XDO:
            cx, cy = ext.x + ext.width // 2, ext.y + ext.height // 2
            rc, o = xdo("mousemove", str(cx), str(cy))
            notes.append("pointer to %d,%d (rc %d)" % (cx, cy, rc))
    except Exception as e:
        notes.append("focus: %s: %s" % (type(e).__name__, e))
    time.sleep(0.3)
    return "; ".join(notes)


def show(rb):
    if rb is None:
        return "(no Text interface: cannot read it back)"
    return "%r (%d chars)" % (_ui_clip(rb, 2000), len(rb))


def op_tree():
    t0 = time.time()
    showing = bool(A.get("showing_only", True))
    nodes, notes = _ui_collect(roots(), ACC, showing_only=showing, depth=int(A.get("depth", 30)),
                               max_nodes=int(A.get("max_nodes", 6000)), deadline=DEADLINE - 2)
    match = A.get("match")
    text, ids, kept = _ui_render(nodes, match, int(A.get("limit", 250)))
    bypath = {tuple(r["path"]): r for r in nodes}
    for e in ids.values():  # description + extents of the printed elements, for _ui_same
        if time.time() > DEADLINE - 2:  # a hung app: the rest compare on role/name/parent
            notes.append("identity (description, extents) not stored for every id: time budget")
            break
        r = bypath.get(tuple(e["path"]))
        if r is not None and not r.get("err"):
            e.update(ident(r["obj"]))
    with IdsLock():
        save_ids({"ids": ids, "next": len(ids) + 1})
    napps = sum(1 for r in nodes if r["d"] == 0)
    out("%s; %d app(s) on the session bus; %d elements walked (%s) in %.2f s; %d lines%s"
        % (app_status(), napps, len(nodes), "showing only" if showing else "all",
           time.time() - t0, len(ids), (", match %r keeps %d" % (match, kept)) if match else ""))
    for n in notes:
        out("note: " + n)
    if not nodes:
        out("(no application on the session's AT-SPI bus: it may still be starting, it may have "
            "exited, or its toolkit has no accessibility bridge / switch)")
    elif match and not ids:
        out("(nothing matches %r)" % match)
    if text:
        out(text)
    done(0)


def op_click():
    obj, label, err = resolve(A["target"])
    if err:
        done(2, err)
    out("target: " + label)
    names = []
    try:
        ai = obj.get_action_iface()
        if ai is not None:
            names = [Atspi.Action.get_action_name(ai, i)
                     for i in range(Atspi.Action.get_n_actions(ai))]
    except Exception:
        ai = None
    idx, aname, err = _ui_choose_action(names, A.get("action"))
    if err:
        done(2, err)
    out("before: " + describe(obj))
    try:
        r = Atspi.Action.do_action(ai, idx)
    except Exception as e:
        done(4, "do_action(%r) raised %s: %s" % (aname, type(e).__name__, e))
    out("do_action(%r) -> %s  (True only means the call went through: compare the states)"
        % (aname, r))
    time.sleep(float(A.get("after", 1.0)))
    out("after: " + describe(obj))
    out("windows now:")
    for line in windows_now():
        out("  " + line)
    done(0)


def op_type():
    text = A["text"]
    enter = bool(A.get("enter"))
    target = A.get("target")
    if enter and not XDO:
        done(3, status="MISSING=xdotool (enter=True sends Return with xdotool)")
    if target is None or target == "":
        if not XDO:
            done(3, status="MISSING=xdotool (linux_ui_type without a target types at the "
                           "keyboard focus with xdotool)")
        rc, o = xdo_type(text)
        out("xdotool type (at the current keyboard focus) -> rc %d %s" % (rc, o))
        if enter:
            rc2, o2 = xdo("key", "Return")
            out("xdotool key Return -> rc %d %s" % (rc2, o2))
            rc = rc or rc2
        time.sleep(0.5)
        nodes, _ = _ui_collect(roots(), ACC, showing_only=True, depth=40, max_nodes=4000,
                               deadline=DEADLINE - 2)
        foc = [r for r in nodes if "focused" in r["states"]]
        if foc:
            out("focused element: " + _ui_line(dict(foc[-1], d=0)))
            out("its text now: " + show(readback(foc[-1]["obj"])))
        else:
            out("no element reports the focused state")
        done(0 if rc == 0 else 4)
    obj, label, err = resolve(target)
    if err:
        done(2, err)
    out("target: " + label)
    try:
        ei = obj.get_editable_text_iface()
    except Exception:
        ei = None
    plan = _ui_type_plan(ei is not None, bool(XDO))
    if not plan:
        done(3, status="MISSING=xdotool (the element has no EditableText interface; the "
                       "focus + type fallback needs xdotool)")
    ok, rb = False, None
    for method in plan:
        if method == "set_text":
            try:
                out("EditableText.set_text_contents -> %s" % ei.set_text_contents(text))
            except Exception as e:
                out("EditableText.set_text_contents raised %s: %s" % (type(e).__name__, e))
                continue
        else:
            out("focus: " + focus(obj))
            rc, o = xdo("key", "ctrl+a")
            out("xdotool key ctrl+a (replace the content) -> rc %d %s" % (rc, o))
            rc, o = xdo_type(text)
            out("xdotool type -> rc %d %s" % (rc, o))
        time.sleep(0.5)
        rb = readback(obj)
        ok = _ui_type_verified(text, rb)
        out("read back after %s: %s" % (method, show(rb)))
        if ok or rb is None:
            break
    if enter:
        out("focus: " + focus(obj))
        rc, o = xdo("key", "Return")
        out("xdotool key Return -> rc %d %s" % (rc, o))
        time.sleep(0.5)
        out("text after Return: " + show(readback(obj)))
    if rb is None:
        done(0, "typed, NOT verified: the element has no Text interface to read back")
    done(0 if ok else 2, "read-back equals the text: %s" % ("yes" if ok else "NO"))


def op_apps():
    names = []
    for app in roots():
        if app is None:
            continue
        try:
            names.append("%r (%d windows)" % (app.get_name() or "", app.get_child_count()))
        except Exception as e:
            names.append("? (%s)" % e)
    out("apps on the session's AT-SPI bus: " + (", ".join(names) if names else
                                                 "none yet (still starting, or no a11y bridge)"))
    done(0)


try:
    {"tree": op_tree, "click": op_click, "type": op_type, "apps": op_apps}[OP]()
except SystemExit:
    raise
except Exception as e:
    done(4, "AT-SPI error: %s: %s" % (type(e).__name__, e))
'''

_PURE = (_ui_clip, _ui_states, _ui_line, _ui_collect, _ui_render, _ui_where,
         _ui_parse_target, _ui_find, _ui_choose_action, _ui_type_plan, _ui_type_verified,
         _ui_safe, _ui_same)
_PURE_CONSTS = ("_UI_KEY_STATES", "_UI_DEFAULT_ACTIONS", "_UI_ACTIONS_SHOWN", "_UI_NAME_MAX",
                "_UI_TEXT_MAX", "_UI_ROLES")


def _driver_source(args):
    """The per-call AT-SPI driver: the call's arguments (base64 JSON, so no quoting layer),
    the pure helpers above (inspect.getsource) and _DRIVER_MAIN."""
    g = globals()
    parts = ["# -*- coding: utf-8 -*-\n# labhand linux_ui driver: written for ONE call, "
             "removed after it\nimport re, time\n",
             "_ARGS_B64 = %r\n" % base64.b64encode(json.dumps(args).encode("utf-8")).decode()]
    parts += ["%s = %r\n" % (c, g[c]) for c in _PURE_CONSTS]
    parts += ["\n" + inspect.getsource(f) for f in _PURE]
    parts.append(_DRIVER_MAIN)
    return "".join(parts)


# --- the shell side -----------------------------------------------------------------------
# Shared bash functions. __lh_marked lists the pids whose environment carries the session's
# marker (one grep over /proc/*/environ: only the caller's own processes are readable);
# __lh_reap sends TERM, waits up to 6 s, then KILL; __lh_xlock removes the X lock / socket
# of OUR display only when its Xvfb is dead (a KILLed Xvfb leaves them behind).
_BASH_FUNCS = r'''
__lh_marked() {
  grep -lzxF "LABHAND_UI_SESSION=$1" "${__LH_PROC:-/proc}"/[0-9]*/environ 2>/dev/null \
    | sed -n 's#^.*/\([0-9][0-9]*\)/environ$#\1#p'
}
__lh_reap() {
  local ps i=0
  ps=$(__lh_marked "$1")
  [ -n "$ps" ] || return 0
  kill -TERM $ps 2>/dev/null
  while [ $i -lt 30 ]; do
    sleep 0.2
    ps=$(__lh_marked "$1")
    [ -n "$ps" ] || return 0
    i=$((i+1))
  done
  kill -KILL $ps 2>/dev/null
  sleep 0.5
}
__lh_safe() {
  # text labhand did not write (app / session logs): a line can never start with __LH_
  LC_ALL=C sed 's/^__LH_/_LH_/'
}
__lh_xlock() {
  local n f pid
  n=${1#:}; n=${n%%.*}
  case "$n" in ''|*[!0-9]*) return 0;; esac
  f=/tmp/.X$n-lock
  [ -O "$f" ] || return 0
  pid=$(tr -cd '0-9' < "$f" 2>/dev/null)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then return 0; fi
  rm -f -- "$f" "/tmp/.X11-unix/X$n"
}
__lh_rmdir() {
  local m
  # never delete THROUGH a mount (a FUSE portal can expose real files): unmount what is
  # still mounted below the session dir, and rm stays on the dir's own file system
  for m in $(awk -v d="$1/" 'index($2, d) == 1 {print $2}' /proc/self/mounts 2>/dev/null); do
    fusermount3 -u -z "$m" 2>/dev/null || fusermount -u -z "$m" 2>/dev/null || umount -l "$m" 2>/dev/null
  done
  rm -rf --one-file-system -- "$1"
}
__lh_names() {
  for __d in "$WD"/_lh_ui_*/; do
    [ -d "$__d" ] || continue
    __d=${__d%/}; echo "__LH_LIVE=${__d##*/_lh_ui_}"
  done
}
__lh_super() {
  # the supervisor: the process with an argv element exactly "$D/super.sh" (found even before
  # it wrote super.pid, and never a reused pid)
  grep -lzxF "$1/super.sh" "${__LH_PROC:-/proc}"/[0-9]*/cmdline 2>/dev/null \
    | sed -n 's#^.*/\([0-9][0-9]*\)/cmdline$#\1#p'
}
__lh_stop_dir() {
  local N=$1 D=$2 sp before
  sp=$(__lh_super "$D" | tr '\n' ' ')
  before=$(__lh_marked "$N" | tr '\n' ' ')
  if [ -n "$sp" ]; then
    kill -TERM $sp 2>/dev/null; before="$sp$before"
  fi
  __lh_reap "$N"
  __lh_xlock "$(cat "$D/display" 2>/dev/null)"
  __lh_rmdir "$D"
  echo "__LH_STOPPED=$before"
  echo "__LH_LEFT=$(__lh_marked "$N" | tr '\n' ' ')"
  [ -e "$D" ] && echo "__LH_DIR_LEFT"
  return 0
}
'''

# The supervisor. argv: D N MAXS SCREEN WD JAR (values travel as arguments, never pasted
# into the file). setsid makes it the leader of a new session, so it and everything below
# it survive the SSH call that started it; `timeout` bounds the session's lifetime.
_SUPER_SH = r'''#!/bin/bash
# labhand linux_ui session supervisor
D=$1; N=$2; MAXS=$3; SCREEN=$4; WD=$5; JAR=$6
echo $$ > "$D/super.pid"
date +%s > "$D/started"
__FUNCS__
export LABHAND_UI_SESSION="$N"
export HOME="$D/home" XDG_CONFIG_HOME="$D/home/.config" XDG_CACHE_HOME="$D/home/.cache" \
  XDG_DATA_HOME="$D/home/.local/share" XDG_STATE_HOME="$D/home/.local/state" \
  XDG_RUNTIME_DIR="$D/run" TMPDIR="$D/tmp"
unset __UNSET__
export __A11Y__
if [ -n "$JAR" ] && [ -f "$JAR" ]; then
  # -XX:-UsePerfData: no /tmp/hsperfdata_<user>/<pid> file (a killed JVM leaves it behind)
  export JAVA_TOOL_OPTIONS="-Xbootclasspath/a:$JAR -Djavax.accessibility.assistive_technologies=org.GNOME.Accessibility.AtkWrapper -XX:-UsePerfData"
fi
# the private bus may activate only the allow-listed services (no gvfs, portals, ...)
mkdir -p "$D/services"
for s in __SERVICES__; do
  for dir in __SERVICE_DIRS__; do
    if [ -f "$dir/$s.service" ]; then cp "$dir/$s.service" "$D/services/"; break; fi
  done
done
{
  echo '<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">'
  echo '<busconfig><type>session</type><keep_umask/><listen>unix:tmpdir=/tmp</listen><auth>EXTERNAL</auth>'
  echo "<servicedir>$D/services</servicedir>"
  echo '<policy context="default"><allow send_destination="*" eavesdrop="true"/><allow eavesdrop="true"/><allow own="*"/></policy></busconfig>'
} > "$D/bus.conf"
echo "__LH_SESSION_START $(date +%s) max=${MAXS}s"
timeout -k 10 "$MAXS" xvfb-run -a -s "-screen 0 $SCREEN" dbus-run-session --config-file="$D/bus.conf" -- bash "$D/inner.sh" "$D" "$WD"
echo "__LH_SESSION_END rc=$? $(date +%s)"
unset LABHAND_UI_SESSION
__lh_reap "$N"
# a session that never came up keeps its dir: linux_ui_start reads the log, then removes it
if [ -s "$D/env.sh" ]; then
  __lh_xlock "$(cat "$D/display" 2>/dev/null)"
  __lh_rmdir "$D"
fi
'''

# Runs inside xvfb-run (DISPLAY, XAUTHORITY) and dbus-run-session (the private bus).
_INNER_SH = r'''#!/bin/bash
# labhand linux_ui session: app + session env, then hold Xvfb and the bus open
D=$1; WD=$2
printf '%s\n' "$DISPLAY" > "$D/display"
cd "$WD" 2>/dev/null || cd "$D/home"
bash "$D/cmd.sh" > "$D/app.log" 2>&1 < /dev/null &
APP=$!
echo "$APP" > "$D/app.pid"
printf 'export DISPLAY=%q XAUTHORITY=%q DBUS_SESSION_BUS_ADDRESS=%q XDG_RUNTIME_DIR=%q\n' \
  "$DISPLAY" "${XAUTHORITY:-}" "${DBUS_SESSION_BUS_ADDRESS:-}" "$XDG_RUNTIME_DIR" > "$D/env.sh.tmp" \
  && mv -f "$D/env.sh.tmp" "$D/env.sh"
wait "$APP"
echo "$?" > "$D/app.rc"
# the app is gone; keep the display and the bus until linux_ui_stop or max_minutes
exec sleep infinity
'''


def _super_sh():
    return (_SUPER_SH.replace("__FUNCS__", _BASH_FUNCS.strip())
            .replace("__UNSET__", " ".join(UNSET_ENV))
            .replace("__A11Y__", " ".join(f"{k}={v}" for k, v in A11Y_ENV))
            .replace("__SERVICES__", " ".join(BUS_SERVICES))
            .replace("__SERVICE_DIRS__", " ".join(BUS_SERVICE_DIRS)))


def _b64(s):
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _session_dir_expr(session):
    return f'N={shlex.quote(session)}; D="$WD/{SESSION_PREFIX}$N"\n'


def _start_body(session, command, screen, max_seconds, wait, driver, meta,
                jar=ATK_WRAPPER_JAR):
    """The bash linux_ui_start runs (in the host workdir, under a tree-killing timeout):
    dependency check, the per-session dir (atomic mkdir), the session files (base64, so the
    command and the names never pass through a shell parser), the supervisor under setsid,
    a wait until the display and bus are up (under a lock on the workdir, so two starts never
    pick the same free display number), then the first look at the session's bus."""
    files = {"super.sh": _super_sh(), "inner.sh": _INNER_SH,
             "cmd.sh": command.rstrip("\n") + "\n", "meta.json": json.dumps(meta) + "\n"}
    put = "".join(f"printf %s {_b64(body)} | base64 -d > \"$D/{name}\"\n"
                  for name, body in files.items())
    return (_session_dir_expr(session)
            + f"SCREEN={shlex.quote(screen)}; MAXS={int(max_seconds)}; WAIT={int(wait)}; "
            + f"DRV={shlex.quote(driver)}; JAR={shlex.quote(jar)}\n"
            + r'''miss=""
for t in xvfb-run dbus-run-session python3 setsid timeout flock base64; do
  command -v "$t" >/dev/null 2>&1 || miss="$miss $t"
done
if command -v python3 >/dev/null 2>&1; then
  python3 -P -c 'import gi; gi.require_version("Atspi", "2.0"); from gi.repository import Atspi' >/dev/null 2>&1 \
    || miss="$miss python3-gi+AT-SPI-typelib(gir1.2-atspi-2.0)"
fi
a11y=""
for dir in __SERVICE_DIRS__; do [ -f "$dir/org.a11y.Bus.service" ] && a11y=1; done
[ -n "$a11y" ] || miss="$miss at-spi2-core(org.a11y.Bus-D-Bus-service)"
if [ -n "$miss" ]; then echo "__LH_MISSING=${miss# }"; exit 3; fi
command -v xdotool >/dev/null 2>&1 || echo "__LH_NOTE=xdotool is missing: linux_ui_type can only use EditableText (no focus + type fallback, no Enter)"
mkdir -m 700 "$D" 2>/dev/null || { echo "__LH_EXISTS"; exit 4; }
mkdir -p "$D/home/.config" "$D/home/.cache" "$D/home/.local/share" "$D/home/.local/state" "$D/tmp" \
  && mkdir -m 700 "$D/run" || { __lh_rmdir "$D"; echo "__LH_NODIR"; exit 6; }
'''
            .replace("__SERVICE_DIRS__", " ".join(BUS_SERVICE_DIRS))
            + put
            + r'''exec 9<"$WD"
flock -w 30 9 2>/dev/null || echo "__LH_NOTE=the display lock was not taken within 30 s"
setsid nohup bash "$D/super.sh" "$D" "$N" "$MAXS" "$SCREEN" "$WD" "$JAR" </dev/null >"$D/session.log" 2>&1 9<&- &
i=0
while [ ! -s "$D/env.sh" ] && [ $i -lt 300 ]; do
  sp=$(cat "$D/super.pid" 2>/dev/null)
  if [ -n "$sp" ] && ! kill -0 "$sp" 2>/dev/null; then break; fi
  sleep 0.1; i=$((i+1))
done
flock -u 9 2>/dev/null; exec 9<&-
if [ ! -s "$D/env.sh" ]; then
  echo "__LH_NOSTART"
  echo "--- session log ---"; tail -c 2500 "$D/session.log" 2>/dev/null | __lh_safe; echo
  echo "--- app output ---"; tail -c 1500 "$D/app.log" 2>/dev/null | __lh_safe; echo
  __lh_stop_dir "$N" "$D"
  exit 6
fi
sleep "$WAIT"
echo "__LH_DIR=$D"
echo "__LH_DISPLAY=$(cat "$D/display" 2>/dev/null)"
echo "__LH_SUPER=$(cat "$D/super.pid" 2>/dev/null)"
echo "__LH_APP=$(cat "$D/app.pid" 2>/dev/null)"
[ -s "$D/app.rc" ] && echo "__LH_APPRC=$(cat "$D/app.rc")"
echo "__LH_PIDS=$(__lh_marked "$N" | tr '\n' ' ')"
echo "--- app output (tail) ---"; tail -c 1500 "$D/app.log" 2>/dev/null | __lh_safe; echo
( . "$D/env.sh"; unset AT_SPI_BUS_ADDRESS; [ -n "$DBUS_SESSION_BUS_ADDRESS" ] && [ -n "$DISPLAY" ] || { echo "__LH_NOBUS"; exit 7; }; exec python3 -P "$DRV" "$D" ) 2>&1
exit 0
''')


_UNKNOWN_CHECK = r'''if [ ! -s "$D/env.sh" ]; then
  if [ -d "$D" ]; then echo "__LH_STARTING"; else echo "__LH_UNKNOWN"; fi
  __lh_names; exit 5
fi
'''


def _drive_body(session, driver):
    return (_session_dir_expr(session) + _UNKNOWN_CHECK
            + 'command -v python3 >/dev/null 2>&1 || { echo "__LH_MISSING=python3"; exit 3; }\n'
            + f'( . "$D/env.sh"; unset AT_SPI_BUS_ADDRESS; [ -n "$DBUS_SESSION_BUS_ADDRESS" ] && [ -n "$DISPLAY" ] || {{ echo "__LH_NOBUS"; exit 7; }}; exec python3 -P {shlex.quote(driver)} '
            + '"$D" ) 2>&1\n')


def _stop_body(session):
    return (_session_dir_expr(session) + r'''if [ ! -d "$D" ]; then
  ps=$(__lh_marked "$N")
  if [ -z "$ps" ]; then echo "__LH_UNKNOWN"; __lh_names; exit 5; fi
  echo "__LH_ORPHANS=$(echo $ps)"
fi
__lh_stop_dir "$N" "$D"
''')


_LIST_BODY = r'''now=$(date +%s)
for d in "$WD"/_lh_ui_*/; do
  [ -d "$d" ] || continue
  d=${d%/}; n=${d##*/_lh_ui_}
  echo "__LH_SESS=$n procs=$(__lh_marked "$n" | wc -l | tr -d ' ') started=$(cat "$d/started" 2>/dev/null) now=$now"
  echo "__LH_META=$(head -c 2000 "$d/meta.json" 2>/dev/null | tr -d '\n')"
done
grep -azoHE '^LABHAND_UI_SESSION=[A-Za-z0-9._-]+' /proc/[0-9]*/environ 2>/dev/null | tr '\0' '\n' \
  | sed -n 's#^/proc/\([0-9]*\)/environ:LABHAND_UI_SESSION=\(.*\)$#\2 \1#p' \
  | while read -r n p; do [ -d "$WD/_lh_ui_$n" ] || echo "__LH_ORPHAN=$n pid=$p"; done
exit 0
'''

_SHOT_BODY_TAIL = r'''command -v import >/dev/null 2>&1 || { echo "__LH_MISSING=import (ImageMagick)"; exit 3; }
( . "$D/env.sh"; [ -n "$DISPLAY" ] || { echo "__LH_NOBUS"; exit 7; }; exec import -window root "$SHOT" ) 2>&1 && echo "__LH_SHOT_OK"
'''


def _wrap(body, timeout):
    """`body` in the host workdir ($WD = its absolute path), with the shared functions, under
    a real tree-killing timeout; the exit code comes back as the last __LH_RC= line."""
    inner = (f"mkdir -p {_wd()} 2>/dev/null; cd {_wd()} || exit 9\nWD=$(pwd)\n"
             + _BASH_FUNCS + body)
    return f"timeout -k 5 {int(timeout)} bash -c {shlex.quote(inner)}; echo __LH_RC=$?"


def _exec(host, body, timeout):
    """-> (output, rc or None, timed_out). Raises on a connection failure."""
    out, err = remote.ssh_exec(_wrap(body, timeout), host=host, read_timeout=int(timeout) + 25)
    text = (out or "") + (("\n" + err) if (err or "").strip() else "")
    channel_dead = "(TIMEOUT after" in text
    rcs = list(re.finditer(r"__LH_RC=(\d+)[ \t\r]*\n?", text))
    rc = None
    if rcs:
        rc = int(rcs[-1].group(1))
        text = text[:rcs[-1].start()] + text[rcs[-1].end():]
    return text.strip(), rc, (channel_dead or rc in (124, 137))


_MARK_RE = re.compile(r"^__LH_[A-Z_]+(=.*)?$", re.M)


def _flag(text, key):
    """A bare status line `__LH_<KEY>` (whole line; text labhand did not write is
    neutralised on the host - __lh_safe / _ui_safe - so no line of it starts with __LH_)."""
    return re.search(rf"^__LH_{key}[ \t\r]*$", text or "", re.M) is not None


def _marks(text, key):
    return [m.group(1) for m in re.finditer(rf"^__LH_{key}=(.*)$", text or "", re.M)]


def _clean(text):
    """The output without labhand's own marker lines."""
    return re.sub(r"\n{3,}", "\n\n", _MARK_RE.sub("", text or "")).strip()


def _int(v, default, lo=None, hi=None):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    if lo is not None and n < lo:
        n = lo
    if hi is not None and n > hi:
        n = hi
    return n


def _session_arg(session, tool):
    s = str(session or "").strip()
    if not _SESSION_RE.match(s) or ".." in s:
        return None, VMResult(False, f"{tool}: unknown session {str(session)[:80]!r} (a session "
                                     "name is what linux_ui_start returned: letters, digits, "
                                     "'._-'); linux_ui_stop() lists the live ones")
    return s, None


def _new_session_name(name, command):
    """`<stem>-<8 hex>`: stem = `name` or the program's base name, letters/digits/`._-`."""
    stem = str(name or "").strip()
    if not stem:
        try:
            first = shlex.split(str(command))[0]
        except (ValueError, IndexError):
            first = (str(command).split() or ["ui"])[0]
        stem = first.rsplit("/", 1)[-1]
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem).lstrip("._-")[:24].rstrip(".") or "ui"
    stem = stem.replace("..", "_")
    return f"{stem}-{secrets.token_hex(4)}"


def _unknown(tool, session, host, text):
    live = _marks(text, "LIVE")
    if _flag(text, "STARTING"):
        return VMResult(False, f"{tool}: session {session!r} on {host} is still starting (no "
                               "display / bus recorded yet); try again in a moment")
    return VMResult(False, f"{tool}: unknown session {session!r} on {host}; live sessions: "
                           + (", ".join(live) if live else "none"))


def _missing(tool, host, text):
    what = "; ".join(_marks(text, "MISSING"))
    return VMResult(False, f"{tool}: missing on {host}: {what} (labhand does not install it)",
                    detail=_clean(text))


def _stop_quiet(host, session):
    """Best-effort session teardown after a failed / timed-out start; never raises."""
    try:
        _exec(host, _stop_body(session), 30)
    except Exception:  # noqa: BLE001
        pass


# --- the tools ------------------------------------------------------------------------------
def linux_ui_start(command, name=None, screen="1280x800x24", wait=5, host=None, timeout=60,
                   max_minutes=30):
    """Start `command` on a linux host under its own Xvfb display and private D-Bus session
    bus as a labhand-owned background session, with throw-away HOME/XDG dirs and the
    accessibility switches set. Returns the session name (r.known) for the other linux_ui_*
    tools. The session ends by itself after `max_minutes`."""
    tool = "linux_ui_start"
    h, err = hosts.check(host, tool, "linux")
    if err:
        return err
    host = h.name
    command = str(command or "")
    if not command.strip():
        return VMResult(False, f"{tool}: `command` is empty - name the GUI program, e.g. "
                               "'mousepad' (Chromium also needs --force-renderer-accessibility)")
    blocked = _security_block(command)
    if blocked:
        return VMResult(False, f"blocked: this command touches {blocked}")
    screen = str(screen or "1280x800x24").strip()
    if not _SCREEN_RE.match(screen):
        return VMResult(False, f"{tool}: screen must look like 1280x800x24 (WxHxDEPTH), "
                               f"got {screen[:40]!r}")
    timeout = _int(timeout, 60, lo=10, hi=3600)
    wait = _int(wait, 5, lo=0, hi=3600)
    try:
        max_seconds = int(round(float(max_minutes) * 60))
    except (TypeError, ValueError):
        max_seconds = 30 * 60
    max_seconds = max(60, min(max_seconds, MAX_MINUTES_CAP * 60))
    session = _new_session_name(name, command)
    meta = {"session": session, "command": command[:300], "screen": screen,
            "max_minutes": round(max_seconds / 60, 2), "host": host}
    driver = remote.scratch_name("uidrv", "py")
    try:
        remote.write_remote(f"{_wd()}/{driver}", _driver_source(
            {"op": "apps", "budget": 10}), host=host)
        text, rc, timed_out = _exec(host, _start_body(session, command, screen, max_seconds,
                                                      wait, driver, meta), timeout)
    except Exception as e:  # noqa: BLE001
        _stop_quiet(host, session)
        return VMResult(False, f"{tool}: {type(e).__name__}: {e} (session {session} torn down)")
    finally:
        remote.remove_quiet([driver], host=host)
    # App output and driver text are neutralised on the host (no line of theirs starts with
    # __LH_), so every marker here is labhand's. Any failure once the session dir may exist
    # tears the session down; only the two exits that happen before (rc 3: a dependency is
    # missing, rc 4: the dir belongs to another session) leave everything alone.
    if _marks(text, "MISSING") and rc == 3:
        return _missing(tool, host, text)
    if _flag(text, "EXISTS") and rc == 4:
        return VMResult(False, f"{tool}: session dir for {session} already exists on {host} "
                               "(name collision); call again")
    if timed_out:
        _stop_quiet(host, session)
        return VMResult(False, f"{tool}: TIMEOUT after {timeout}s on {host} (wait={wait}); "
                               f"session {session} was torn down", detail=_clean(text))
    if _marks(text, "MISSING"):
        _stop_quiet(host, session)
        r = _missing(tool, host, text)
        r.summary += f"; session {session} was torn down"
        return r
    if rc != 0 or _flag(text, "NOSTART") or _flag(text, "NODIR") or _flag(text, "NOBUS"):
        _stop_quiet(host, session)
        return VMResult(False, f"{tool}: session {session} did not come up on {host} (exit "
                               f"{rc}); its log is below and it was torn down",
                        detail=_clean(text))
    d = (_marks(text, "DIR") or ["?"])[0]
    disp = (_marks(text, "DISPLAY") or ["?"])[0]
    sup = (_marks(text, "SUPER") or ["?"])[0]
    app = (_marks(text, "APP") or ["?"])[0]
    pids = (_marks(text, "PIDS") or [""])[0].strip()
    apprc = _marks(text, "APPRC")
    notes = _marks(text, "NOTE")
    state = f"app pid {app} " + (f"EXITED (rc {apprc[-1]}; a launcher's children keep "
                                 "running in the session)" if apprc else "running")
    body = _clean(text)
    detail = (f"session {session} on {host}: display {disp} (Xvfb, private D-Bus), {state}\n"
              f"session pids: {pids}\nstate: {d} (removed by linux_ui_stop, or after "
              f"{meta['max_minutes']} min)\n" + "".join(f"note: {n}\n" for n in notes) + body)
    known = [f"linux_ui session {session} on {host}: display {disp}, supervisor pid {sup}, "
             f"app pid {app}, pids {pids}; state in {d}; ends after {meta['max_minutes']} min "
             f"or at linux_ui_stop(session='{session}')"]
    return VMResult(True, f"linux_ui session {session} started on {host} ({command[:40]}; "
                          f"{state}); read it with linux_ui_tree(session='{session}')",
                    detail=detail, known=known, info=detail)


def _drive(tool, session, args, host, timeout, default_timeout):
    h, err = hosts.check(host, tool, "linux")
    if err:
        return None, err, None
    s, err = _session_arg(session, tool)
    if err:
        return None, err, None
    timeout = _int(timeout, default_timeout, lo=5, hi=3600)
    args = dict(args, budget=max(3, timeout - 8))
    driver = remote.scratch_name("uidrv", "py")
    try:
        remote.write_remote(f"{_wd()}/{driver}", _driver_source(args), host=h.name)
        text, rc, timed_out = _exec(h.name, _drive_body(s, driver), timeout)
    except Exception as e:  # noqa: BLE001
        return None, VMResult(False, f"{tool}: {type(e).__name__}: {e}"), None
    finally:
        remote.remove_quiet([driver], host=h.name)
    if _flag(text, "UNKNOWN") or _flag(text, "STARTING"):
        return None, _unknown(tool, s, h.name, text), None
    if _marks(text, "MISSING"):
        return None, _missing(tool, h.name, text), None
    if _flag(text, "NOBUS"):
        return None, VMResult(False, f"{tool}: session {s} on {h.name} has no recorded display "
                                     "/ bus (refusing to fall back to the real desktop's)",
                              detail=_clean(text)), None
    drc = _marks(text, "UI_RC")
    drc = int(drc[-1]) if drc and drc[-1].isdigit() else None
    if timed_out:
        return None, VMResult(False, f"{tool}: TIMEOUT after {timeout}s on {h.name} (the walk "
                                     "or an AT-SPI call hung; the call was killed, the session "
                                     "lives on)", detail=_clean(text)), None
    return (h.name, s, drc, rc), None, _clean(text)


def linux_ui_tree(session, match=None, limit=250, depth=30, showing_only=True, host=None,
                  timeout=45):
    """The session's accessibility tree as text, one line per element: `#id [role] 'name'
    (key states) actions=... text='...'`. Only showing subtrees by default; closed menus are
    not walked; `match` keeps subtrees whose name/role contains it; `limit` caps the lines.
    The ids are valid for linux_ui_click / linux_ui_type until the next tree of the session."""
    tool = "linux_ui_tree"
    args = {"op": "tree", "match": (str(match) if match not in (None, "") else None),
            "limit": _int(limit, 250, lo=1, hi=5000), "depth": _int(depth, 30, lo=1, hi=200),
            "showing_only": bool(showing_only)}
    meta, err, text = _drive(tool, session, args, host, timeout, 45)
    if err:
        return err
    host, s, drc, rc = meta
    ok = drc == 0
    detail, saved = _spill(host, text, "ui_tree")
    first = text.splitlines()[0] if text else ""
    known = [f"linux_ui_tree({s}) -> {first[:200]}"
             + (f" (full -> {saved}, page with read_file)" if saved else "")] if ok else []
    return VMResult(ok, f"linux_ui_tree {s} on {host}: " + (first[:160] if ok else
                    f"failed (driver rc {drc}, exit {rc})"),
                    detail=detail, known=known, match=_find_match(text), info=detail)


def linux_ui_click(session, target, action=None, host=None, timeout=45):
    """Run an AT-SPI action on one element: `target` = an id from the last linux_ui_tree
    (`#12`), or a name / `role:name` (exact name first, then prefix; showing elements win;
    ambiguous -> the candidates with ids). `action` default: the first of
    click/press/activate/toggle. The element is read again afterwards (do_action returns True
    even when nothing happens), plus the app's windows."""
    tool = "linux_ui_click"
    args = {"op": "click", "target": target,
            "action": (str(action) if action not in (None, "") else None)}
    meta, err, text = _drive(tool, session, args, host, timeout, 45)
    if err:
        return err
    host, s, drc, rc = meta
    ok = drc == 0
    detail, saved = _spill(host, text, "ui_click")
    known = [f"linux_ui_click({s}, {str(target)[:60]!r}) -> " + " | ".join(
        ln.strip() for ln in text.splitlines() if ln.startswith(("do_action", "after:")))[:240]
             ] if ok else []
    return VMResult(ok, f"linux_ui_click {s} {str(target)[:60]!r} on {host}: "
                    + ("action done; the element's state after it is below" if ok else
                       (text.splitlines()[-1][:200] if text else f"failed (exit {rc})")),
                    detail=detail, known=known, match=_find_match(text), info=detail)


def linux_ui_type(session, text, target=None, enter=False, host=None, timeout=45):
    """Put `text` into an element: EditableText.set_text_contents when it has that interface,
    else (or when the read-back differs, as in Firefox) grab_focus + xdotool select-all +
    type; then the text is read back. target=None types at the keyboard focus with xdotool
    (no select-all). enter=True presses Return afterwards."""
    tool = "linux_ui_type"
    text_s = "" if text is None else str(text)
    if len(text_s) > TEXT_MAX:
        return VMResult(False, f"{tool}: text is {len(text_s)} chars; at most {TEXT_MAX} per call")
    args = {"op": "type", "text": text_s, "enter": bool(enter),
            "target": (target if target not in (None, "") else None)}
    meta, err, out = _drive(tool, session, args, host, timeout, 45)
    if err:
        return err
    host, s, drc, rc = meta
    ok = drc == 0
    detail, saved = _spill(host, out, "ui_type")
    last = out.splitlines()[-1] if out else ""
    known = [f"linux_ui_type({s}, target={str(target)[:60]!r}, {len(text_s)} chars) -> "
             + last[:200]] if ok else []
    return VMResult(ok, f"linux_ui_type {s} on {host}: " + (last[:200] or f"exit {rc}"),
                    detail=detail, known=known, match=_find_match(out), info=detail)


def linux_ui_screenshot(session, host=None, timeout=30):
    """A picture of the session's display (r.image), for what text cannot show (layout)."""
    tool = "linux_ui_screenshot"
    h, err = hosts.check(host, tool, "linux")
    if err:
        return err
    s, err = _session_arg(session, tool)
    if err:
        return err
    timeout = _int(timeout, 30, lo=5, hi=600)
    shot = remote.scratch_name("uishot", "png")
    body = (_session_dir_expr(s) + _UNKNOWN_CHECK + f"SHOT={shlex.quote(shot)}\n"
            + _SHOT_BODY_TAIL)
    try:
        text, rc, timed_out = _exec(h.name, body, timeout)
        if _flag(text, "UNKNOWN") or _flag(text, "STARTING"):
            return _unknown(tool, s, h.name, text)
        if _marks(text, "MISSING"):
            return _missing(tool, h.name, text)
        if timed_out or not _flag(text, "SHOT_OK"):
            return VMResult(False, f"{tool}: no screenshot from {s} on {h.name}"
                                   + (f" (TIMEOUT after {timeout}s)" if timed_out else "")
                                   + f": {_clean(text)[:400]}")
        raw = remote.read_bytes(f"{_wd()}/{shot}", host=h.name)
    except Exception as e:  # noqa: BLE001
        return VMResult(False, f"{tool}: {type(e).__name__}: {e}")
    finally:
        remote.remove_quiet([shot], host=h.name)
    media, b64, note = _prepare_image(raw, "png")
    if not media:
        return VMResult(False, f"{tool}: screenshot unusable ({note})")
    return VMResult(True, f"screenshot of linux_ui session {s} on {h.name}{note}; picture on "
                          "your next turn",
                    known=[f"linux_ui_screenshot({s}) taken"],
                    image={"media_type": media, "data": b64})


def linux_ui_stop(session=None, host=None, timeout=30):
    """End a session: its app, Xvfb, private bus and every other process carrying its marker
    (TERM, then KILL), then remove its dir. Without `session`: list the live sessions (and
    any process whose session dir is gone)."""
    tool = "linux_ui_stop"
    h, err = hosts.check(host, tool, "linux")
    if err:
        return err
    timeout = _int(timeout, 30, lo=5, hi=600)
    if session in (None, ""):
        try:
            text, rc, timed_out = _exec(h.name, _LIST_BODY, timeout)
        except Exception as e:  # noqa: BLE001
            return VMResult(False, f"{tool}: {type(e).__name__}: {e}")
        if timed_out or rc != 0:
            return VMResult(False, f"{tool}: listing failed on {h.name} (exit {rc})",
                            detail=_clean(text))
        return _format_list(h.name, text)
    s, err = _session_arg(session, tool)
    if err:
        return err
    try:
        text, rc, timed_out = _exec(h.name, _stop_body(s), timeout)
    except Exception as e:  # noqa: BLE001
        return VMResult(False, f"{tool}: {type(e).__name__}: {e}")
    if _flag(text, "UNKNOWN"):
        return _unknown(tool, s, h.name, text)
    if timed_out:
        return VMResult(False, f"{tool}: TIMEOUT after {timeout}s stopping {s} on {h.name}; "
                               "call again", detail=_clean(text))
    stopped = " ".join((_marks(text, "STOPPED") or [""])[-1].split())
    left = " ".join((_marks(text, "LEFT") or [""])[-1].split())
    orphans = _marks(text, "ORPHANS")
    dir_left = _flag(text, "DIR_LEFT")
    ok = not left and not dir_left
    detail = (f"session {s} on {h.name}: signalled pids: {stopped or 'none (already gone)'}\n"
              f"still running: {left or 'none'}\nsession dir: "
              f"{'STILL THERE' if dir_left else 'removed'}"
              + (f"\n(no session dir; killed orphan processes {orphans[-1]})" if orphans else ""))
    return VMResult(ok, f"linux_ui_stop {s} on {h.name}: "
                    + ("stopped, nothing left" if ok else "NOT clean: see detail"),
                    detail=detail, known=[f"linux_ui session {s} on {h.name} stopped"] if ok else [])


def _format_list(host, text):
    lines, n = [], 0
    metas = _marks(text, "META")
    for i, row in enumerate(_marks(text, "SESS")):
        n += 1
        f = dict(kv.split("=", 1) for kv in row.split()[1:] if "=" in kv)
        name = row.split()[0]
        try:
            meta = json.loads(metas[i]) if i < len(metas) and metas[i] else {}
        except ValueError:
            meta = {}
        left = ""
        try:
            left = (f", ends in {max(0, int(f['started']) + int(float(meta.get('max_minutes', 0)) * 60) - int(f['now'])) // 60} min"
                    if f.get("started") else ", not started")
        except (KeyError, ValueError):
            pass
        lines.append(f"{name}: {f.get('procs', '?')} processes{left}; command: "
                     f"{str(meta.get('command', '?'))[:80]!r}; screen {meta.get('screen', '?')}")
    for o in _marks(text, "ORPHAN"):
        lines.append(f"ORPHAN process (session dir gone): {o} - linux_ui_stop(session=<name>) "
                     "kills it")
    detail = "\n".join(lines) or "no linux_ui session on this host"
    return VMResult(True, f"linux_ui sessions on {host}: {n}", detail=detail,
                    known=[f"linux_ui sessions on {host}: {n}"], info=detail)
