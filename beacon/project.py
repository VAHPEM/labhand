# -*- coding: utf-8 -*-
"""beacon.project — wire a worker project to beacon (`beacon setup-project DIR`).

Writes, inside DIR only (apart from the target of a symlinked file, below), merging with whatever is already there:
  .claude/settings.local.json   the five hooks + an allow rule for the beacon MCP tools
                                (settings.json instead with --shared; the hook commands
                                carry absolute paths of this machine, hence "local")
  .mcp.json                     the beacon MCP server entry (and, with --allow-mac, the labhand-mac
                                server, enabled in enabledMcpjsonServers and allowed as mcp__labhand-mac)
  CLAUDE.md                     the worker rules (beacon/WORKER.md) between markers,
                                replaced in place on a re-run
  .claude/beacon-setup.json     setup's record of what it added because it was absent (written
                                first; a re-run adds to it), so teardown removes only that

A wired file that is a symlink (CLAUDE.md -> a shared file) is written through: the link stays a link and
its target, wherever it lives, gets the change. A rewritten file keeps its permission bits.

An existing file is copied to <file>.beacon-bak, and that backup is this run's pre-run state: a backup
already there that equals the file (bytes and mode) is reused; one that differs from the unwired file is
from an older run and is moved aside to a free <file>.beacon-bak.old[.N] (never deleted, never
overwritten), then a fresh one is taken; a file that is still wired (an interrupted run) keeps its backup,
the real pre-run state. Nothing is removed from an existing file: other hooks, servers, rules and text stay.

`beacon teardown-project DIR` is the inverse: it removes exactly what setup wrote (the rules block,
the hooks / allow / deny entries and the beacon MCP server setup's record lists: a rule, hook or server the
user had before setup stays, one setup rewrote gets its old value back; .claude/beacon when it is beacon's
wrapper by content: any other file there is the project's and is never removed; the record, last) and
keeps everything else; a file
setup created that is left with nothing in it is removed. When what is left equals setup's .beacon-bak, the backup
is put back byte for byte (line endings, trailing blank lines, JSON formatting, mode), so a run that
changed nothing leaves no diff; a file setup created is removed; an edit the user made during the run
outside beacon's parts is kept (a file it rewrites with no backup is backed up first).

Backups never outlive their use and never take the only copy of anything with them: after teardown a
backup is removed only when the final file equals it (bytes and permission bits; for a symlink, the
target's content, the link untouched), or when its bytes equal the file's version in the repository's HEAD
commit (the content is safe in git; `git cat-file` on the tracked path, for a symlink the target's path, a
regular file in HEAD only, the index never counts; "committed in HEAD" in the output). Outside a git work
tree, for an untracked file, or on any git error or timeout the HEAD check simply does not apply. Any other
backup is kept and named in teardown's output with the reason and what it holds (the pre-run file, or
teardown's own copy of a wired file). The older backups setup moved aside (.beacon-bak.old[.N]) are
settled the same way: removed when the final file or HEAD has their bytes, otherwise kept and named. A .claude/beacon backup that holds a beacon wrapper (an older version
backed it up) is not a copy of anything the project owns: setup and teardown remove it, and it never
stands in for the project's file. --dry-run reports the same verdicts.
"""
import json
import os
import re
import shutil
import stat
import subprocess

from .hooks import HUMAN_COMMANDS
from .shellrun import posix_path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_EVENTS = (("Stop", "stop", 300), ("SessionStart", "session-start", 30),
               ("PreToolUse", "pre-tool-use", 10), ("PostToolUse", "post-tool-use", 10),
               ("StopFailure", "stop-failure", 30), ("UserPromptSubmit", "user-prompt-submit", 30))
# Deny rules block in every permission mode, bypassPermissions included, and they do not
# depend on the hooks running: a second line of defence for the supervisor's own files.
# (Edit rules cover every built-in file-editing tool.)
PROTECT_DENY = ("Edit(**/ledger.db*)", "Bash(sqlite3 *)", "Edit(**/.claude/settings.json)",
                "Edit(**/.claude/settings.local.json)", "Edit(**/.mcp.json)", "Edit(**/.claude/beacon)",
                "Edit(**/.claude/beacon-setup.json)",
                # agent definitions: one can carry `permissionMode: bypassPermissions` for the subagent it starts
                "Edit(**/.claude/agents/**)", "Edit(~/.claude/agents/**)") + tuple(
    f"Bash(.claude/beacon {c} *)" for c in HUMAN_COMMANDS)      # the guard's single list (hooks.py)
WRAPPER = """#!/bin/sh
# beacon, for `! .claude/beacon ...` in the Claude Code chat (and any terminal in this project).
# Written by `beacon setup-project`. The commands that act as the human (answer, verify, ...) need a terminal.
PYTHONPATH="{repo}${{PYTHONPATH:+{sep}$PYTHONPATH}}" exec "{python}" -m beacon "$@"
"""
MARK_BEGIN = "<!-- beacon:worker-rules:begin -->"
MARK_END = "<!-- beacon:worker-rules:end -->"
BAK = ".beacon-bak"
# setup's record of what it added (because it was absent); teardown removes exactly that
RECORD = os.path.join(".claude", "beacon-setup.json")
RECORD_KEY = "beacon_setup"
WIRED = ("CLAUDE.md", ".mcp.json", os.path.join(".claude", "settings.local.json"),
         os.path.join(".claude", "settings.json"), os.path.join(".claude", "beacon"))


def hook_command(python, arg, repo=REPO):
    # Claude Code runs hook commands with sh (POSIX) or Git Bash (Windows): forward slashes, or bash eats the
    # backslashes of a Windows path and the hook silently never runs.
    return f'PYTHONPATH="{posix_path(repo)}" "{posix_path(python)}" -m beacon.hooks {arg}'


def beacon_events(settings):
    """Events for which a settings dict already runs a beacon hook."""
    out = set()
    for event, arg, _ in HOOK_EVENTS:
        for g in ((settings or {}).get("hooks", {}) or {}).get(event, []) or []:
            for h in (g.get("hooks", []) if isinstance(g, dict) else []):
                if isinstance(h, dict) and f"beacon.hooks {arg}" in str(h.get("command", "")):
                    out.add(event)
    return out


def merge_settings(settings, python, repo=REPO, allow=("mcp__beacon",), skip_events=(), deny=PROTECT_DENY):
    """Return (new_settings, changes). Replaces an existing beacon hook command for
    the same event (paths may have moved) instead of adding a second one. Events in
    skip_events already run beacon from the other settings file and are left alone
    (Claude Code merges both files: a second copy would run every hook twice)."""
    s = json.loads(json.dumps(settings or {}))
    changes = []
    hooks = s.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings 'hooks' is not an object")
    for event, arg, timeout in HOOK_EVENTS:
        if event in skip_events:
            changes.append(f"left {event} alone: the other settings file already runs beacon for it")
            continue
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"settings hooks.{event} is not a list")
        cmd = hook_command(python, arg, repo)
        found = False
        for g in groups:
            for h in g.get("hooks", []) if isinstance(g, dict) else []:
                if isinstance(h, dict) and f"beacon.hooks {arg}" in str(h.get("command", "")):
                    found = True
                    if h.get("command") != cmd or h.get("timeout") != timeout:
                        h.update({"type": "command", "command": cmd, "timeout": timeout})
                        changes.append(f"updated {event} hook")
        if not found:
            groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]})
            changes.append(f"added {event} hook")
    perms = s.setdefault("permissions", {})
    allow_list = perms.setdefault("allow", []) if isinstance(perms, dict) else None
    if not isinstance(allow_list, list):
        raise ValueError("settings permissions.allow is not a list")
    for rule in allow:
        if rule not in allow_list:
            allow_list.append(rule)
            changes.append(f"allowed {rule}")
    deny_list = perms.setdefault("deny", [])
    if not isinstance(deny_list, list):
        raise ValueError("settings permissions.deny is not a list")
    for rule in deny:
        if rule not in deny_list:
            deny_list.append(rule)
            changes.append(f"denied {rule}")
    return s, changes


def merge_mcp(cfg, mcp_python, repo=REPO):
    c = json.loads(json.dumps(cfg or {}))
    servers = c.setdefault("mcpServers", {})
    # The decider calls the Claude API from the MCP server. An MCP server inherits Claude Code's
    # environment and `${VAR:-}` expands, but ANTHROPIC_API_KEY is removed whenever Claude Code scrubs
    # subprocess credentials (CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1, CI, cloud sessions; measured on 2.1.322:
    # blank in a cloud sandbox, passed through on plain Linux). So the decider's key is BEACON_API_KEY (or
    # a file named by BEACON_API_KEY_FILE); they are named here explicitly for clients that do not pass the
    # environment on.
    want = {"command": mcp_python, "args": ["-m", "beacon.mcp_server"],
            "env": {"PYTHONPATH": repo, "BEACON_RUN": "${BEACON_RUN}", "BEACON_API_KEY": "${BEACON_API_KEY:-}",
                    "BEACON_API_KEY_FILE": "${BEACON_API_KEY_FILE:-}",
                    "BEACON_API_URL": "${BEACON_API_URL:-https://api.anthropic.com/v1/messages}"}}
    if servers.get("beacon") == want:
        return c, []
    change = "updated beacon MCP server" if "beacon" in servers else "added beacon MCP server"
    servers["beacon"] = want
    return c, [change]


# The Mac-control server (`--allow-mac`): never at user scope (it would be in every session), only in the
# .mcp.json of a project setup was asked to give it to. It is an existing engine registered under the name
# labhand-mac: $LABHAND_MAC_MCP_COMMAND, else Cua Driver's MCP mode (`cua-driver mcp`, which proxies to the
# CuaDriver.app daemon that holds the macOS permissions).
MAC_SERVER = "labhand-mac"
MAC_ALLOW = "mcp__labhand-mac"
LABHAND_ALLOW = "mcp__labhand"
MAC_COMMAND_ENV = "LABHAND_MAC_MCP_COMMAND"
MAC_DEFAULT_COMMAND = ("~/.local/bin/cua-driver", "mcp")
# servers setup adds beside `beacon` (each recorded under the .mcp.json entry's "servers")
EXTRA_SERVERS = (MAC_SERVER,)


class MacCommandError(ValueError):
    """$LABHAND_MAC_MCP_COMMAND is malformed (setup refuses it before writing anything)."""


def mac_command():
    """The labhand-mac server's argv: $LABHAND_MAC_MCP_COMMAND (a JSON array of strings: the program, then its
    args) when set, else `~/.local/bin/cua-driver mcp`. A program given as a path (with `~` or a separator) is
    made absolute (Claude Code spawns it without a shell, from the project's directory); a bare name stays,
    for a PATH lookup. Raises MacCommandError for a malformed value."""
    raw = os.environ.get(MAC_COMMAND_ENV, "").strip()
    if not raw:
        argv = list(MAC_DEFAULT_COMMAND)
    else:
        try:
            argv = json.loads(raw)
        except ValueError as e:
            raise MacCommandError(f"${MAC_COMMAND_ENV} is not valid JSON ({e}); expected a JSON array, the program "
                                  f'then its args, e.g. ["/path/to/engine", "mcp"]') from None
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv) and argv[0].strip()):
            raise MacCommandError(f"${MAC_COMMAND_ENV} must be a non-empty JSON array of strings, the program "
                                  f'(non-empty) then its args, e.g. ["/path/to/engine", "mcp"]; got {raw!r}')
    prog = argv[0]
    if prog.startswith("~") or os.sep in prog or (os.altsep and os.altsep in prog):
        prog = os.path.abspath(os.path.expanduser(prog))
    return [prog] + argv[1:]


def mac_server_entry(argv):
    # no env at all, PYTHONPATH least of all: an inherited beacon home on PYTHONPATH once made the labhand
    # server import the wrong copy of labhand (and see no hosts)
    return {"command": argv[0], "args": list(argv[1:])}


def merge_mac(cfg, settings, argv):
    """Add the labhand-mac server to an .mcp.json object and enable it (enabledMcpjsonServers) in a settings
    object, so Claude Code starts it without asking. -> (new_cfg, mcp changes, new_settings, settings changes)."""
    c = json.loads(json.dumps(cfg or {}))
    servers = c.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(".mcp.json 'mcpServers' is not an object")
    want, ch_mcp = mac_server_entry(argv), []
    if servers.get(MAC_SERVER) != want:
        ch_mcp.append(f"{'updated' if MAC_SERVER in servers else 'added'} {MAC_SERVER} MCP server")
        servers[MAC_SERVER] = want
    s = json.loads(json.dumps(settings or {}))
    enabled = s.setdefault("enabledMcpjsonServers", [])
    if not isinstance(enabled, list):
        raise ValueError("settings enabledMcpjsonServers is not a list")
    ch_s = []
    if MAC_SERVER not in enabled:
        enabled.append(MAC_SERVER)
        ch_s.append(f"enabled {MAC_SERVER} in enabledMcpjsonServers")
    return c, ch_mcp, s, ch_s


def merge_claude_md(text, rules):
    block = f"{MARK_BEGIN}\n{rules.strip()}\n{MARK_END}"
    text = text or ""
    if MARK_BEGIN in text and MARK_END in text and text.index(MARK_BEGIN) < text.index(MARK_END):
        head, rest = text.split(MARK_BEGIN, 1)
        _, tail = rest.split(MARK_END, 1)
        new = head + block + tail
        return new, ([] if new == text else ["updated worker rules in CLAUDE.md"])
    return text + _claude_md_sep(text) + block + "\n", ["added worker rules to CLAUDE.md"]


def _claude_md_sep(text):
    """What setup puts between the existing text and the rules block (a blank line, no more)."""
    return "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")


def _read_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        return {}
    obj = json.loads(text)        # a malformed file is an error for the human, not something to overwrite
    if not isinstance(obj, dict):
        raise ValueError(f"{path} is not a JSON object")
    return obj


def _target(path):
    """The file a write to `path` should land in: through a symlink (CLAUDE.md -> a shared file stays a
    link), unless it dangles into a directory that does not exist (then the link itself is replaced)."""
    if os.path.islink(path):
        real = os.path.realpath(path)
        if os.path.isdir(os.path.dirname(real)):
            return real
    return path


def _file_state(path):
    """(bytes, permission bits) of the file at `path`, read through a symlink; None when it is missing or
    unreadable (a directory, a dangling link)."""
    try:
        with open(path, "rb") as fh:
            return fh.read(), stat.S_IMODE(os.fstat(fh.fileno()).st_mode)
    except OSError:
        return None


def _free_name(base):
    """`base`, or `base.1`, `base.2`, ...: the first name nothing (not even a dangling link) has."""
    name, n = base, 0
    while os.path.lexists(name):
        n += 1
        name = f"{base}.{n}"
    return name


def _is_wired(path, name=None, record=None):
    """Does the file still hold what setup writes? True / False, None when it cannot be read or parsed.
    name: the wired file's name when `path` is a copy of it (a backup). record: setup's record
    (`_read_record`): a JSON file then holds what setup wrote only when it holds what the record says setup
    added; without one (an older setup) any beacon part counts."""
    name = name or os.path.basename(path)
    try:
        if name == "beacon":
            with open(path, "rb") as fh:
                return bool(BEACON_WRAPPER.search(fh.read()))
        if name == "CLAUDE.md":
            with open(path, encoding="utf-8") as fh:
                return bool(strip_claude_md(fh.read())[1])
        if name == ".mcp.json":
            return bool(strip_mcp(_read_json(path), _entry(record, name))[1])
        return bool(strip_settings(_read_json(path), _entry(record, name))[1])
    except (OSError, ValueError):
        return None


def _setup_backup(path, dry_run, wired=None):
    """Make sure the backup this run uses is this run's pre-run state. -> messages.

    No backup: one is taken of an existing file that is not wired; a file still wired with no backup was
    created by an interrupted run (its pre-run state is "no file"), so none is taken. The file still wired
    and a backup there (an interrupted run): the backup is the real pre-run state and stays. A backup equal
    to the file (bytes and mode): reused. Otherwise the backup is from an older run: it is moved aside to a
    free `<file>.beacon-bak.old[.N]` (never deleted, never overwriting anything) and a fresh one is taken;
    when the file does not exist at all, setup creates it and there is no fresh backup to take. A
    `.claude/beacon` backup holding a beacon wrapper is beacon's own file, not a copy of the project's: it
    is removed first, and the project's file (if any) is backed up instead.
    wired: whether the file still holds what an earlier setup added (settings and .mcp.json: decided from
    setup's record); None: decided from the content (`_is_wired`)."""
    bak = path + BAK
    exists = os.path.exists(path)
    if wired is None or not exists:
        wired = _is_wired(path) if exists else False
    out = []
    if os.path.basename(path) == "beacon" and _is_wrapper_backup(path):
        # beacon's own command (an older version backed it up): no copy of anything the project owns, and it
        # must not stand in for the project's file
        if not dry_run:
            os.remove(bak)
        out.append(f"removed {os.path.basename(bak)}: it held beacon's own command, not a file of the project's")
    if dry_run and out or not os.path.lexists(bak):
        if exists and wired is not True and not dry_run:      # unknown: back it up, as setup always did
            shutil.copy2(path, bak)
        return out
    if wired is not False or (exists and _file_state(path) == _file_state(bak)):
        return out                                            # unknown: keep the backup as it is
    old = _free_name(bak + ".old")
    if not dry_run:
        os.rename(bak, old)
        if exists:
            shutil.copy2(path, bak)
    why = "it differed from the file" if exists else "the file did not exist"
    fresh = f" and backed up the file to {os.path.basename(bak)}" if exists else ""
    return [f"moved an older backup aside to {os.path.basename(old)} ({why}, so it is from an earlier run)"
            + fresh]


def _is_wrapper_backup(path):
    """Is `path`.beacon-bak a regular file holding a beacon wrapper (beacon's own command)?"""
    if os.path.islink(path + BAK) or not os.path.isfile(path + BAK):
        return False
    return _is_wired(path + BAK, "beacon") is True


def _write(path, content, dry_run, backup=True):
    """Write `content` to `path` (through a symlink, keeping the mode). backup: copy an existing file with
    no backup to <file>.beacon-bak first (teardown); setup settles its backup in `_setup_backup` instead."""
    if dry_run:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if backup and os.path.exists(path) and not os.path.lexists(path + BAK):
        shutil.copy2(path, path + BAK)
    real = _target(path)
    tmp = real + ".beacon-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    if os.path.exists(real):
        os.chmod(tmp, stat.S_IMODE(os.stat(real).st_mode))     # a rewritten file keeps its mode, not the umask's
    os.replace(tmp, real)


def setup_project(project, python, mcp_python, shared=False, allow_labhand=False, dry_run=False,
                  quiet_shell=False, allow_mac=False):
    """allow_mac: also add the labhand-mac server (Mac control) to the project's .mcp.json, enable it and allow
    its tools. Its command ($LABHAND_MAC_MCP_COMMAND, else `cua-driver mcp`) is only written, never run here; a
    malformed $LABHAND_MAC_MCP_COMMAND raises MacCommandError before anything is read or written."""
    mac_argv = mac_command() if allow_mac else None      # a malformed override is refused before any write
    project = os.path.abspath(os.path.expanduser(project))
    if not os.path.isdir(project):
        raise FileNotFoundError(f"no such project directory: {project}")
    changes = []
    allow = ("mcp__beacon", "mcp__labhand") if allow_labhand else ("mcp__beacon",)
    if allow_mac:
        allow += (MAC_ALLOW,)

    sp = os.path.join(project, ".claude", "settings.json" if shared else "settings.local.json")
    other = os.path.join(project, ".claude", "settings.local.json" if shared else "settings.json")
    mp = os.path.join(project, ".mcp.json")
    s_before, o_before, m_before = _read_json(sp), _read_json(other), _read_json(mp)
    old_record = _read_record(project)
    new, ch = merge_settings(s_before, python, allow=allow, skip_events=beacon_events(o_before))
    if quiet_shell and new.get("respondToBashCommands") is not False:
        new["respondToBashCommands"] = False      # `! cmd` output lands in the chat without a model reply
        ch.append("set respondToBashCommands=false (shell-mode output gets no automatic reply)")
    new_mcp, ch_mcp = merge_mcp(m_before, mcp_python)
    if allow_mac:
        new_mcp, ch_m, new, ch_s = merge_mac(new_mcp, new, mac_argv)
        ch_mcp += ch_m
        ch += ch_s
    o_new, ch_o = o_before, []
    base = old_record
    if not allow_mac and old_record and any(_mac_in_entry(n, e) for n, e in old_record["files"].items()):
        # The grant follows the flag: a re-run without --allow-mac takes back exactly the labhand-mac parts an
        # earlier setup recorded adding (a labhand-mac entry or approval the user had before stays theirs).
        for path in (sp, other, mp):
            name = os.path.basename(path)
            entry = old_record["files"].get(name)
            if not _mac_in_entry(name, entry):
                continue
            if path == mp:
                sub = _extra_entries(entry)[MAC_SERVER]
                if _server_holds(m_before, MAC_SERVER, sub):       # else stale: the file as it is is the user's
                    new_mcp, c = strip_mcp(new_mcp, {"servers": {MAC_SERVER: sub}})
                    ch_mcp += [f"{x} (no --allow-mac)" for x in c]
            elif path == sp:
                new, c = _strip_settings_recorded(new, _mac_settings_rec(entry, new))
                ch += [f"{x} (no --allow-mac)" for x in c]
            else:
                o_new, c = _strip_settings_recorded(o_new, _mac_settings_rec(entry, o_new))
                ch_o += [f"{x} (no --allow-mac)" for x in c]
        base = {RECORD_KEY: 1, "files": {n: _without_mac(n, e) for n, e in old_record["files"].items()}}
    # name -> the labhand-mac parts this run takes back (record form), kept recorded until the files are written
    mac_src = dict(old_record["files"]) if base is not old_record else {}
    legacy_mac = {}        # no record: name -> the labhand-mac parts an earlier setup added (record form)
    if old_record is None:
        # No record (deleted by hand, a lost write): beacon's own labhand-mac wiring is recognised as teardown
        # recognises it (absent from setup's .beacon-bak, or a file setup created that holds nothing else);
        # without --allow-mac it is taken back. A labhand-mac part the user had before setup stays theirs.
        for path, cur in ((sp, s_before), (other, o_before), (mp, m_before)):
            plain, beacon_parts = (strip_mcp if path == mp else strip_settings)(cur)
            entry = _mac_unrecorded_entry(path, plain) if beacon_parts else None
            if entry is None:
                continue
            legacy_mac[os.path.basename(path)] = entry
            if allow_mac:
                continue
            mac_src[os.path.basename(path)] = entry
            if path == mp:
                new_mcp, c = _strip_mac_entry(path, new_mcp, entry)
                ch_mcp += [f"{x} (no --allow-mac)" for x in c]
            elif path == sp:
                new, c = _strip_mac_entry(path, new, entry)
                ch += [f"{x} (no --allow-mac)" for x in c]
            else:
                o_new, c = _strip_mac_entry(path, o_new, entry)
                ch_o += [f"{x} (no --allow-mac)" for x in c]

    # The record of what setup adds because it is absent, written before any wired file: an interrupted
    # setup leaves a record that covers what it wrote. A re-run (an interrupted run, a moved checkout) adds
    # to it; what an earlier setup recorded stays recorded, and what was there before stays the user's.
    files = dict(base["files"]) if base else {}
    wired = {}
    for path, before, after, legacy, make, merge in (
            (sp, s_before, new, _legacy_settings_entry, _settings_record, _merge_settings_entry),
            (other, o_before, o_new, _legacy_settings_entry, _settings_record, _merge_settings_entry),
            (mp, m_before, new_mcp, _legacy_mcp_entry, _mcp_record, _merge_mcp_entry)):
        name = os.path.basename(path)
        if base:
            inherited = base["files"].get(name)
        else:
            # beacon's own legacy parts are judged on the file without setup's labhand-mac parts (they would
            # read as "something else in the file"); those parts are setup's too
            um = legacy_mac.get(name)
            judged = _strip_mac_entry(path, before, um)[0] if um else before
            if (um and name != ".mcp.json" and judged.get("enabledMcpjsonServers") == []
                    and _json_backup(path) is None):
                # a file setup created (no backup) whose labhand-mac enable the user took out: the list left
                # empty is setup's residue, not something else in the file (it stays, as with a record)
                judged = {k: v for k, v in judged.items() if k != "enabledMcpjsonServers"}
            inherited = legacy(path, judged)
            if um:
                inherited = _with_mac(name, inherited, um)
        # each server's parts are judged alone: losing a labhand-mac part (the server disabled, its rule
        # removed, an interrupted revocation) must not make setup forget beacon's own parts
        inherited = (_prune_mcp_entry if name == ".mcp.json" else _prune_settings_entry)(before, inherited)
        strip = strip_mcp if name == ".mcp.json" else strip_settings
        live = _holds_entry(name, before, inherited)
        wired[name] = live is True and bool(strip(before, inherited)[1])
        # an entry whose file does not hold what it lists is stale (an interrupted setup that never got to
        # the file, an interrupted teardown that already stripped it): the file as it is now is the pre-run
        # state, and a rule the user added since (even one equal to beacon's) is theirs
        files[name] = merge(None if live is False else inherited, make(before, after))
        if mac_src:
            # what revoking took out of the file is no change of setup's to record (a server it took away
            # would otherwise read as "replaced")
            files[name] = _without_mac(name, files[name])
    record = {RECORD_KEY: 1, "files": files}
    rp = os.path.join(project, RECORD)
    # Revoking: the record keeps listing the labhand-mac parts until the files no longer hold them, so a run
    # stopped in between leaves them recorded (the next run takes them back) instead of looking like yours.
    first = record if not mac_src else {RECORD_KEY: 1, "files": {
        n: _with_mac(n, files.get(n), mac_src.get(n)) for n in list(files) + [k for k in mac_src if k not in files]}}
    if record != old_record:
        changes.append(f"{RECORD}: {'recorded' if old_record is None else 'updated the record of'} what setup "
                       f"added (teardown removes only that; what you had before stays)")
    if first != old_record:
        _write(rp, json.dumps(first, indent=2) + "\n", dry_run, backup=False)

    ch_real = [c for c in ch if not c.startswith("left ")]
    notes = [c for c in ch if c.startswith("left ")]
    changes += [f"{os.path.relpath(sp, project)}: {c}" for c in notes]
    ch = ch_real
    if ch:
        changes += [f"{os.path.relpath(sp, project)}: {c}"
                    for c in ch + _setup_backup(sp, dry_run, wired[os.path.basename(sp)])]
        _write(sp, json.dumps(new, indent=2) + "\n", dry_run, backup=False)

    if ch_o:
        changes += [f"{os.path.relpath(other, project)}: {c}"
                    for c in ch_o + _setup_backup(other, dry_run, wired[os.path.basename(other)])]
        _write(other, json.dumps(o_new, indent=2) + "\n", dry_run, backup=False)

    if ch_mcp:
        changes += [f".mcp.json: {c}" for c in ch_mcp + _setup_backup(mp, dry_run, wired[".mcp.json"])]
        _write(mp, json.dumps(new_mcp, indent=2) + "\n", dry_run, backup=False)
    if record != first:
        _write(rp, json.dumps(record, indent=2) + "\n", dry_run, backup=False)     # revoked: now forget them

    wp = os.path.join(project, ".claude", "beacon")
    # the wrapper runs under sh / Git Bash; the separator is the one Python on this OS splits PYTHONPATH on
    wrapper = WRAPPER.format(repo=posix_path(REPO), python=posix_path(python), sep=os.pathsep)
    try:
        with open(wp, encoding="utf-8") as fh:
            same = fh.read() == wrapper
    except OSError:
        same = False
    if not same:
        changes.append(".claude/beacon: wrote the in-chat command (`! .claude/beacon log`)")
        changes += [f".claude/beacon: {c}" for c in _setup_backup(wp, dry_run)]
        if not dry_run:
            _write(wp, wrapper, dry_run, backup=False)
            os.chmod(wp, 0o755)

    cp = os.path.join(project, "CLAUDE.md")
    with open(os.path.join(REPO, "beacon", "WORKER.md"), encoding="utf-8") as fh:
        rules = fh.read()
    old = ""
    if os.path.exists(cp):
        with open(cp, encoding="utf-8") as fh:
            old = fh.read()
    new_text, ch = merge_claude_md(old, rules)
    if ch:
        changes += [f"CLAUDE.md: {c}" for c in ch + _setup_backup(cp, dry_run)]
        _write(cp, new_text, dry_run, backup=False)
    return changes


# ---------------------------------------------------------------- setup's record
def _read_record(project):
    """Setup's record (`RECORD`), or None when there is none (no setup, or one older than the record).
    A record that is not one is an error for the human, never guessed around."""
    path = os.path.join(project, RECORD)
    if not os.path.lexists(path):
        return None
    obj = _read_json(path)
    if obj.get(RECORD_KEY) != 1 or not isinstance(obj.get("files"), dict) \
            or not all(isinstance(v, dict) for v in obj["files"].values()):
        raise ValueError(f"{path} is not beacon setup's record; remove it by hand if it is stale")
    return obj


def _entry(record, name):
    """The record's entry for the wired file `name` (basename): {} when the record has none, None when there
    is no record at all (an older setup: teardown then falls back to removing every beacon part)."""
    if record is None:
        return None
    return record["files"].get(os.path.basename(name)) or {}


def _list(v):
    return v if isinstance(v, list) else []


def _rules(settings, key):
    perms = (settings or {}).get("permissions")
    return _list(perms.get(key)) if isinstance(perms, dict) else []


def _beacon_hooks(settings, event):
    """The beacon hook dicts of `event` in a settings dict, in order."""
    hooks = (settings or {}).get("hooks")
    out = []
    for g in _list(hooks.get(event)) if isinstance(hooks, dict) else []:
        for h in _list(g.get("hooks")) if isinstance(g, dict) else []:
            if isinstance(h, dict) and BEACON_HOOK.search(str(h.get("command", ""))):
                out.append(h)
    return out


def _settings_record(before, after):
    """What `after` holds of beacon's that `before` lacks: hooks of events that had none (added) or whose
    beacon hook setup rewrote (replaced: the old ones, put back by teardown), beacon's allow / deny rules,
    the keys setup set; plus what teardown must leave: whether `beacon` was enabled in
    enabledMcpjsonServers, and the mcp__beacon__* approvals there already were."""
    before, after = before or {}, after or {}
    added, replaced = [], {}
    for event, _, _ in HOOK_EVENTS:
        b, a = _beacon_hooks(before, event), _beacon_hooks(after, event)
        if a and not b:
            added.append(event)
        elif b and a != b:
            replaced[event] = json.loads(json.dumps(b))
    allow_b, deny_b = _rules(before, "allow"), _rules(before, "deny")
    keys = {}
    for k in ("respondToBashCommands",):
        if k in after and (k not in before or before[k] != after[k]):
            keys[k] = {"set": after[k], "had": k in before, "value": before.get(k)}
    e = {"hooks": added, "hooks_replaced": replaced,
         "allow": [r for r in _rules(after, "allow") if r not in allow_b
                   and (_ours_allow(r) or r in ("mcp__labhand", MAC_ALLOW))],
         "deny": [r for r in _rules(after, "deny") if r not in deny_b and _ours_deny(r)],
         "keys": keys, "enabled_had": "beacon" in _list(before.get("enabledMcpjsonServers")),
         "beacon_tools_had": [r for r in allow_b if str(r).startswith("mcp__beacon__")]}
    # the other servers setup enabled (labhand-mac: --allow-mac), and their per-tool approvals there already
    # were (teardown leaves those); only when there are any, so a plain setup's record is as it always was
    had = _list(before.get("enabledMcpjsonServers"))
    enabled = [n for n in EXTRA_SERVERS if n in _list(after.get("enabledMcpjsonServers")) and n not in had]
    if enabled:
        e["enabled_added"] = enabled
        # an empty list the user had stays a (now empty again) list at teardown
        e["enabled_key_had"] = isinstance(before.get("enabledMcpjsonServers"), list)
    tools_had = {n: [r for r in allow_b if str(r).startswith(f"mcp__{n}__")] for n in EXTRA_SERVERS
                 if f"mcp__{n}" in e["allow"] or n in enabled}
    if tools_had:
        e["tools_had"] = tools_had
    return e


def _holds_entry(name, cur, entry):
    """Does the file `name` (its object `cur`) still hold everything the record entry says setup added?
    True / False; None when the entry lists nothing to check (no entry, or only what was there before)."""
    if not entry:
        return None
    if name == ".mcp.json":
        held = [_server_holds(cur, "beacon", entry)] + [
            _server_holds(cur, n, r) for n, r in _extra_entries(entry).items()]
        held = [h for h in held if h is not None]
        return all(held) if held else None
    hooks, allow, deny = _list(entry.get("hooks")), _list(entry.get("allow")), _list(entry.get("deny"))
    replaced = entry.get("hooks_replaced") or {}
    enabled = _list(entry.get("enabled_added"))
    if not (hooks or allow or deny or replaced or enabled):
        return None
    return (all(_beacon_hooks(cur, e) for e in hooks)
            and all(_beacon_hooks(cur, e) and _beacon_hooks(cur, e) != old for e, old in replaced.items())
            and all(r in _rules(cur, "allow") for r in allow) and all(r in _rules(cur, "deny") for r in deny)
            and all(n in _list((cur or {}).get("enabledMcpjsonServers")) for n in enabled))


def _merge_settings_entry(old, new):
    """An earlier setup's entry (kept as it is: it knows the pre-run state) plus what this setup adds."""
    if not old:
        return new
    e = json.loads(json.dumps(old))
    for k in ("hooks", "allow", "deny", "beacon_tools_had"):
        e.setdefault(k, [])
    e.setdefault("hooks_replaced", {})
    e.setdefault("keys", {})
    e.setdefault("enabled_had", new["enabled_had"])
    known = set(e["hooks"]) | set(e["hooks_replaced"])
    e["hooks"] += [x for x in new["hooks"] if x not in known]
    e["hooks_replaced"].update({k: v for k, v in new["hooks_replaced"].items() if k not in known})
    for k in ("allow", "deny"):
        e[k] += [r for r in new[k] if r not in e[k]]
    e["keys"].update({k: v for k, v in new["keys"].items() if k not in e["keys"]})
    enabled = _list(e.get("enabled_added")) + [n for n in _list(new.get("enabled_added"))
                                               if n not in _list(e.get("enabled_added"))]
    if enabled:
        e["enabled_added"] = enabled
        e.setdefault("enabled_key_had", new.get("enabled_key_had", False))
    tools_had = dict(new.get("tools_had") or {})
    tools_had.update(old.get("tools_had") or {})
    if tools_had:
        e["tools_had"] = tools_had
    return e


def _server_of(cfg, name):
    servers = (cfg or {}).get("mcpServers")
    return servers.get(name) if isinstance(servers, dict) else None


def _server_record(before, after, name):
    """What setup did to the server `name`: {"server": "added"}, {"server": "replaced", "old": ...}, or {}."""
    b, a = _server_of(before, name), _server_of(after, name)
    if a is not None and b is None:
        return {"server": "added"}
    if b is not None and a != b:
        return {"server": "replaced", "old": b}
    return {}


def _mcp_record(before, after):
    """The .mcp.json entry: the `beacon` server's at the top level (as every record since the first), the
    other servers setup added (EXTRA_SERVERS) under "servers", only when there are any."""
    e = _server_record(before, after, "beacon")
    servers = {n: r for n in EXTRA_SERVERS for r in [_server_record(before, after, n)] if r}
    if servers:
        e["servers"] = servers
    return e


def _extra_entries(entry):
    """name -> record entry of the servers besides `beacon` an .mcp.json entry lists."""
    servers = (entry or {}).get("servers")
    return {n: r for n, r in servers.items() if isinstance(r, dict) and r.get("server")} \
        if isinstance(servers, dict) else {}


def _server_holds(cur, name, sub):
    """Does .mcp.json (`cur`) still hold what a server's record entry says setup wrote? None: nothing to check."""
    s = _server_of(cur, name)
    if sub.get("server") == "added":
        return s is not None
    if sub.get("server") == "replaced":
        return s is not None and s != sub.get("old")
    return None


def _prune_mcp_entry(cur, entry):
    """An .mcp.json entry without the servers the file no longer holds as setup wrote them (stale: an
    interrupted setup or teardown). Each server is judged alone, so a server the user removed during the run
    does not make setup forget the others it added."""
    if not entry:
        return entry
    e = {k: v for k, v in entry.items() if k != "servers"}
    if _server_holds(cur, "beacon", e) is False:
        e = {}
    servers = {n: r for n, r in _extra_entries(entry).items() if _server_holds(cur, n, r) is not False}
    if servers:
        e["servers"] = servers
    return e


def _prune_settings_entry(cur, entry):
    """A settings record entry without the labhand-mac parts the file (`cur`) no longer holds: its
    enabledMcpjsonServers entry, its allow rule, and (once neither is left) its per-tool approvals record.
    Staleness (`_holds_entry`) is then decided on what is left, so a lost labhand-mac part never drops
    beacon's own parts from the record. Beacon's parts are untouched; an entry without labhand-mac parts is
    returned as it is."""
    if entry and LABHAND_ALLOW in _list(entry.get("allow")) and LABHAND_ALLOW not in _rules(cur, "allow"):
        # the --allow-labhand rule is optional like labhand-mac's parts: losing it (removed by hand, an
        # interrupted run that recorded it before writing the file) must not make the rest look stale
        entry = dict(entry, allow=[r for r in entry["allow"] if r != LABHAND_ALLOW])
    if not entry or not _mac_in_entry("settings", entry):
        return entry
    e = json.loads(json.dumps(entry))
    if MAC_ALLOW in _list(e.get("allow")) and MAC_ALLOW not in _rules(cur, "allow"):
        e["allow"] = [r for r in e["allow"] if r != MAC_ALLOW]
    if MAC_SERVER in _list(e.get("enabled_added")) and MAC_SERVER not in _list((cur or {}).get("enabledMcpjsonServers")):
        e["enabled_added"] = [n for n in e["enabled_added"] if n != MAC_SERVER]
        if not e["enabled_added"]:
            # enabled_key_had stays: whether the list existed before the first setup is still known, so a
            # list setup created and the user emptied goes at teardown
            del e["enabled_added"]
    if MAC_ALLOW not in _list(e.get("allow")) and MAC_SERVER not in _list(e.get("enabled_added")):
        if isinstance(e.get("tools_had"), dict):
            e["tools_had"].pop(MAC_SERVER, None)
            if not e["tools_had"]:
                del e["tools_had"]
    return e


def _merge_mcp_entry(old, new):
    """An earlier setup's entry wins for each server it lists (it knows the pre-run state); this setup's
    entry fills in the servers it does not."""
    base = old if old and old.get("server") else new
    e = {k: v for k, v in (base or {}).items() if k != "servers"}
    servers = dict(_extra_entries(new))
    servers.update(_extra_entries(old))
    if servers:
        e["servers"] = servers
    return e


def _mac_in_entry(name, entry):
    """Does a record entry list a labhand-mac part setup added (server, enable, allow rule)?"""
    if not entry:
        return False
    if name == ".mcp.json":
        return MAC_SERVER in _extra_entries(entry)
    return (MAC_ALLOW in _list(entry.get("allow")) or MAC_SERVER in _list(entry.get("enabled_added"))
            or MAC_SERVER in (entry.get("tools_had") if isinstance(entry.get("tools_had"), dict) else {}))


def _mac_settings_rec(entry, cur):
    """A settings record entry holding only the labhand-mac parts of `entry`, for _strip_settings_recorded:
    beacon's parts (its hooks, rules, enable, the mcp__beacon__* approvals in `cur`) are all left."""
    tools_had = entry.get("tools_had") if isinstance(entry.get("tools_had"), dict) else {}
    rec = {"hooks": [], "hooks_replaced": {}, "deny": [], "keys": {}, "enabled_had": True,
           "allow": [MAC_ALLOW] if MAC_ALLOW in _list(entry.get("allow")) else [],
           "beacon_tools_had": [r for r in _rules(cur, "allow") if str(r).startswith("mcp__beacon__")]}
    if MAC_SERVER in _list(entry.get("enabled_added")):
        rec["enabled_added"] = [MAC_SERVER]
        rec["enabled_key_had"] = bool(entry.get("enabled_key_had"))
    if MAC_SERVER in tools_had:
        rec["tools_had"] = {MAC_SERVER: _list(tools_had[MAC_SERVER])}
    return rec


def _without_mac(name, entry):
    """A record entry without its labhand-mac parts (what stays setup's once the grant is revoked)."""
    if not entry:
        return entry
    e = json.loads(json.dumps(entry))
    if name == ".mcp.json":
        if isinstance(e.get("servers"), dict):
            e["servers"].pop(MAC_SERVER, None)
            if not e["servers"]:
                del e["servers"]
        return e
    if isinstance(e.get("allow"), list):
        e["allow"] = [r for r in e["allow"] if r != MAC_ALLOW]
    if isinstance(e.get("enabled_added"), list):
        e["enabled_added"] = [n for n in e["enabled_added"] if n != MAC_SERVER]
        if not e["enabled_added"]:
            del e["enabled_added"]
            e.pop("enabled_key_had", None)
    if isinstance(e.get("tools_had"), dict):
        e["tools_had"].pop(MAC_SERVER, None)
        if not e["tools_had"]:
            del e["tools_had"]
    return e


def _with_mac(name, entry, old):
    """A record entry plus the labhand-mac parts the earlier entry `old` lists (undoes `_without_mac`)."""
    e = json.loads(json.dumps(entry or {}))
    if not _mac_in_entry(name, old):
        return e
    if name == ".mcp.json":
        servers = e["servers"] if isinstance(e.get("servers"), dict) else {}
        servers[MAC_SERVER] = _extra_entries(old)[MAC_SERVER]
        e["servers"] = servers
        return e
    if MAC_ALLOW in _list(old.get("allow")):
        e["allow"] = _list(e.get("allow")) + [MAC_ALLOW]
    if MAC_SERVER in _list(old.get("enabled_added")):
        e["enabled_added"] = _list(e.get("enabled_added")) + [MAC_SERVER]
        e["enabled_key_had"] = bool(old.get("enabled_key_had"))
    if isinstance(old.get("tools_had"), dict) and MAC_SERVER in old["tools_had"]:
        tools_had = e["tools_had"] if isinstance(e.get("tools_had"), dict) else {}
        tools_had[MAC_SERVER] = old["tools_had"][MAC_SERVER]
        e["tools_had"] = tools_had
    return e


def _legacy_settings_entry(path, cur):
    """No record (the project was wired by a setup older than it, and not torn down): what of `cur` is
    beacon's, judged from that setup's .beacon-bak (its pre-run state); with no backup, only a file that
    holds nothing but beacon's parts (and the mcp__labhand rule `--allow-labhand` adds), among them the
    `mcp__beacon` allow rule every setup since the first has written (that setup created it; a user's own file
    with, say, only a beacon hook is not taken for one).
    Otherwise None: nothing in it is beacon's."""
    if not strip_settings(cur)[1]:
        return None
    bak = _json_backup(path)
    if bak is not None:
        before = bak
    elif (_only_labhand_rule(strip_settings(cur)[0]) and "mcp__beacon" in _rules(cur, "allow")
          and any(_beacon_hooks(cur, e) for e, _, _ in HOOK_EVENTS)):
        before = {}
    else:
        return None
    e = _settings_record(before, cur)
    return e if e["hooks"] or e["hooks_replaced"] or e["allow"] or e["deny"] else None


def _only_labhand_rule(rest):
    """Is `rest` (a settings object with beacon's parts stripped) empty, or does it hold only the mcp__labhand
    allow rule `--allow-labhand` writes (strip_settings without a record keeps that rule)?"""
    return not rest or rest == {"permissions": {"allow": ["mcp__labhand"]}}


def _legacy_mcp_entry(path, cur):
    if not strip_mcp(cur)[1]:
        return None
    bak = _json_backup(path)
    if bak is not None:
        return _mcp_record(bak, cur) or None
    rest = strip_mcp(cur)[0]
    return {"server": "added"} if not rest or rest == {"mcpServers": {}} else None


# ---------------------------------------------------------------- teardown
BEACON_HOOK = re.compile(r"-m\s+beacon\.hooks\b")
BEACON_WRAPPER = re.compile(rb'-m\s+beacon\s+"\$@"')       # a wrapper setup wrote (any version)


def _ours_allow(rule):
    return rule == "mcp__beacon" or str(rule).startswith("mcp__beacon__")


def _ours_deny(rule):
    # every rule setup has ever written: the current set, and any `.claude/beacon <cmd>` rule of an older version
    return rule in PROTECT_DENY or str(rule).startswith("Bash(.claude/beacon ")


def strip_settings(settings, rec=None):
    """Return (new_settings, changes): beacon's hooks, allow and deny rules removed, the rest kept.

    rec: setup's record entry for this file (`_entry`): only what setup added is removed (a hook of an event
    it added, the allow / deny rules it added, a key it set), a beacon hook it rewrote gets its old value
    back, and `beacon` leaves enabledMcpjsonServers (and mcp__beacon__* approvals the allow list) only when
    they were not there before setup. None (no record: an older setup): every beacon part is removed."""
    if rec is not None:
        return _strip_settings_recorded(settings, rec)
    s = json.loads(json.dumps(settings or {}))
    changes = []
    hooks = s.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks):
            groups = hooks[event]
            if not isinstance(groups, list):
                continue
            kept_groups = []
            for g in groups:
                inner = g.get("hooks") if isinstance(g, dict) else None
                if not isinstance(inner, list):
                    kept_groups.append(g)
                    continue
                kept = [h for h in inner if not (isinstance(h, dict) and BEACON_HOOK.search(str(h.get("command", ""))))]
                if len(kept) != len(inner):
                    changes.append(f"removed {event} hook")
                if kept:
                    kept_groups.append(dict(g, hooks=kept))
            if kept_groups:
                hooks[event] = kept_groups
            else:
                del hooks[event]
        if not hooks:
            del s["hooks"]
    perms = s.get("permissions")
    if isinstance(perms, dict):
        for key, ours in (("allow", _ours_allow), ("deny", _ours_deny)):
            rules = perms.get(key)
            if not isinstance(rules, list):
                continue
            gone = [r for r in rules if ours(r)]
            if gone:
                perms[key] = [r for r in rules if not ours(r)]
                changes += [f"removed {'allowed' if key == 'allow' else 'denied'} {r}" for r in gone]
            if not perms[key]:
                del perms[key]
        if not perms:
            del s["permissions"]
    if changes and isinstance(perms, dict) and "mcp__labhand" in (perms.get("allow") or []):
        # setup's --allow-labhand adds it, but a project may have had it before: setup does not record which
        changes.append("kept allowed mcp__labhand (remove it by hand if it was only for beacon)")
    servers = s.get("enabledMcpjsonServers")
    if isinstance(servers, list) and "beacon" in servers:
        s["enabledMcpjsonServers"] = [x for x in servers if x != "beacon"]
        changes.append("removed beacon from enabledMcpjsonServers")
        if not s["enabledMcpjsonServers"]:
            del s["enabledMcpjsonServers"]
    return s, changes


def _strip_settings_recorded(settings, rec):
    s = json.loads(json.dumps(settings or {}))
    changes = []
    added, replaced = set(_list(rec.get("hooks"))), rec.get("hooks_replaced") or {}
    hooks = s.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks):
            groups = hooks[event]
            if not isinstance(groups, list):
                continue
            olds = list(_list(replaced.get(event)))
            kept_groups = []
            for g in groups:
                inner = g.get("hooks") if isinstance(g, dict) else None
                if not isinstance(inner, list):
                    kept_groups.append(g)
                    continue
                kept = []
                for h in inner:
                    if not (isinstance(h, dict) and BEACON_HOOK.search(str(h.get("command", "")))):
                        kept.append(h)
                    elif event in replaced:
                        old = olds.pop(0) if olds else h
                        if old != h:
                            changes.append(f"restored your {event} hook as it was before setup")
                        kept.append(old)
                    elif event in added:
                        changes.append(f"removed {event} hook")
                    else:
                        kept.append(h)                       # yours: it was there before setup
                if kept:
                    kept_groups.append(dict(g, hooks=kept))
            if kept_groups:
                hooks[event] = kept_groups
            else:
                del hooks[event]
        if not hooks:
            del s["hooks"]
    perms = s.get("permissions")
    if isinstance(perms, dict):
        had_tools = set(_list(rec.get("beacon_tools_had")))
        other_tools = rec.get("tools_had") if isinstance(rec.get("tools_had"), dict) else {}

        def other_tool(r):
            # a per-tool approval of a server setup added (mcp__labhand-mac__*) that was not there before
            return any(str(r).startswith(f"mcp__{n}__") and r not in _list(had) for n, had in other_tools.items())
        for key, ours in (("allow", lambda r: r in _list(rec.get("allow")) or (
                              str(r).startswith("mcp__beacon__") and r not in had_tools) or other_tool(r)),
                          ("deny", lambda r: r in _list(rec.get("deny")))):
            rules = perms.get(key)
            if not isinstance(rules, list):
                continue
            gone = [r for r in rules if ours(r)]
            if gone:
                perms[key] = [r for r in rules if not ours(r)]
                changes += [f"removed {'allowed' if key == 'allow' else 'denied'} {r}" for r in gone]
            if not perms[key]:
                del perms[key]
        if not perms:
            del s["permissions"]
    for k, info in (rec.get("keys") or {}).items():
        if isinstance(info, dict) and k in s and s[k] == info.get("set"):
            if info.get("had"):
                if info.get("value") != s[k]:
                    s[k] = info.get("value")
                    changes.append(f"set {k} back to {json.dumps(info.get('value'))}")
            else:
                del s[k]
                changes.append(f"removed {k}")
    servers = s.get("enabledMcpjsonServers")
    gone = [n for n in (["beacon"] if not rec.get("enabled_had") else []) + _list(rec.get("enabled_added"))
            if isinstance(servers, list) and n in servers]
    for n in gone:
        s["enabledMcpjsonServers"] = [x for x in s["enabledMcpjsonServers"] if x != n]
        changes.append(f"removed {n} from enabledMcpjsonServers")
    if gone and not s["enabledMcpjsonServers"] and not rec.get("enabled_key_had"):
        del s["enabledMcpjsonServers"]
    return s, changes


def _kept_notes(kind, obj):
    """After a recorded teardown: the beacon parts left because they were there before setup."""
    _, legacy = (strip_mcp if kind == ".mcp.json" else strip_settings)(obj)
    out = []
    for c in legacy:
        if c.startswith("removed beacon from "):
            out.append(f"kept beacon in {c[len('removed beacon from '):]} (it was there before setup)")
        elif c.startswith("removed "):
            out.append(f"kept {c[len('removed '):]} (it was there before setup)")
    return out


def strip_mcp(cfg, rec=None):
    """Return (new_cfg, changes): the beacon MCP server removed. rec: setup's record entry: removed only
    when setup added it, put back as it was when setup replaced it, else left (it was the project's)."""
    c = json.loads(json.dumps(cfg or {}))
    servers = c.get("mcpServers")
    if not isinstance(servers, dict):
        return c, []
    changes = []
    # beacon by the entry's top level (or every beacon server without a record); the other servers setup
    # added only by the entry's "servers" (no setup without a record ever added one)
    todo = [("beacon", rec)] + list(_extra_entries(rec).items())
    for name, sub in todo:
        if name not in servers:
            continue
        if sub is not None and sub.get("server") == "replaced":
            if servers[name] != sub.get("old"):
                servers[name] = sub.get("old")
                changes.append(f"restored your {name} MCP server entry as it was before setup")
            continue
        if sub is not None and sub.get("server") != "added":
            continue
        del servers[name]
        changes.append(f"removed {name} MCP server")
    if changes and not servers:
        del c["mcpServers"]
    return c, changes


def strip_claude_md(text, original=None):
    """Remove the worker-rules block (markers included) and exactly the separator setup put around it.

    Setup appends `_claude_md_sep(original) + block + "\n"`. A head ending in exactly two newlines is
    ambiguous (the original may have ended in none, one or two), so `original` (setup's .beacon-bak) breaks
    the tie; a separator is only taken when setup would have chosen it for what remains, so a stale backup
    cannot remove more than setup added. Without a backup the tie goes to one newline."""
    text = text or ""
    if not (MARK_BEGIN in text and MARK_END in text and text.index(MARK_BEGIN) < text.index(MARK_END)):
        return text, []
    head, rest = text.split(MARK_BEGIN, 1)
    _, tail = rest.split(MARK_END, 1)
    if tail.startswith("\n"):
        tail = tail[1:]                                   # the newline setup wrote after the end marker
    prefer = [_claude_md_sep(original)] if original is not None else []
    for sep in prefer + ["\n", "", "\n\n"]:
        if head.endswith(sep) and _claude_md_sep(head[:len(head) - len(sep)]) == sep:
            head = head[:len(head) - len(sep)]
            break
    if head and tail and not head.endswith(("\n", "\r")) and not tail.startswith(("\n", "\r")):
        head += "\n"            # text the user added after the block starts on its own line, never glued on
    return head + tail, ["removed worker rules from CLAUDE.md"]


def _backup_text(path):
    """setup's backup of `path` read the way setup read the original, or None (none / not UTF-8 text)."""
    try:
        with open(path + BAK, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _json_backup(path):
    """The backup of a JSON file parsed like setup parsed the original, or None."""
    if not os.path.isfile(path + BAK):
        return None
    try:
        return _read_json(path + BAK)
    except (OSError, ValueError):                 # UnicodeDecodeError and JSONDecodeError are ValueErrors
        return None


class _Teardown:
    """Teardown's file actions. With dry_run nothing is touched and `final` records what each file would
    end up as, so the backup verdict of a dry run is the one the real run would reach. `made` holds the
    files teardown backs up itself (no backup from setup: setup created the file), with the state the
    backup has (or would have)."""

    def __init__(self, dry_run, record=None):
        self.dry_run = dry_run
        self.record = record
        self.final = {}
        self.made = {}

    def write(self, path, content):
        """Rewrite the file without beacon's parts; a file with no backup is backed up first (`_write`)."""
        if os.path.exists(path) and not os.path.lexists(path + BAK):
            self.made[path] = _file_state(path)
        if self.dry_run:
            cur = _file_state(path)
            self.final[path] = (content.encode("utf-8"), cur[1] if cur else 0o644)
        else:
            _write(path, content, False)

    def restore(self, path):
        """Put setup's backup back byte for byte (mode and mtime too); the link of a symlink stays a link.
        The backup itself is settled afterwards, by `_settle_backups`."""
        if self.dry_run:
            self.final[path] = _file_state(path + BAK)
            return
        real = _target(path)
        tmp = real + ".beacon-tmp"
        shutil.copy2(path + BAK, tmp)
        os.replace(tmp, real)

    def remove(self, path):
        """Remove a file that held nothing but beacon's parts (and whitespace): nothing in it needs a
        backup; a backup setup took of it stays and is settled afterwards."""
        if self.dry_run:
            self.final[path] = None
        else:
            os.remove(_target(path))        # the file setup wrote through a symlink, not the link

    def state(self, path):
        if self.dry_run and path in self.final:
            return self.final[path]
        return _file_state(path)


def _strip_json(path, obj, record=None):
    """strip_mcp / strip_settings for the file at `path` (by setup's record when there is one), plus the
    empty `mcpServers` strip_mcp drops only when it removed beacon from it. -> (stripped object, changes)."""
    if os.path.basename(path) != ".mcp.json":
        s, ch = strip_settings(obj, _entry(record, path))
        return _strip_mac_unrecorded(path, s, ch) if record is None else (s, ch)
    c, ch = strip_mcp(obj, _entry(record, path))
    if record is None:
        c, ch = _strip_mac_unrecorded(path, c, ch)
    if c.get("mcpServers") == {}:
        del c["mcpServers"]
    return c, ch


def _only_mac_settings(s):
    """Does a settings object (beacon's parts already stripped) hold nothing but (a subset of) what
    --allow-mac adds (plus the mcp__labhand rule an unrecorded teardown keeps anyway), with at least one
    labhand-mac part present? A file that lost one labhand-mac part (server disabled, rule removed) still
    counts; a file with anything else in it does not."""
    if not isinstance(s, dict) or not set(s) <= {"permissions", "enabledMcpjsonServers"}:
        return False
    perms = s.get("permissions", {})
    if not isinstance(perms, dict) or not set(perms) <= {"allow"}:
        return False
    allow, enabled = perms.get("allow", []), s.get("enabledMcpjsonServers", [])
    if not isinstance(allow, list) or not isinstance(enabled, list):
        return False
    if not all(r in (MAC_ALLOW, LABHAND_ALLOW) or (isinstance(r, str) and r.startswith(MAC_ALLOW + "__"))
               for r in allow) or not all(n == MAC_SERVER for n in enabled):
        return False
    return MAC_ALLOW in allow or MAC_SERVER in enabled


def _looks_like_mac_entry(server):
    # the shape setup writes for labhand-mac: a command and its args, nothing else (no env)
    return (isinstance(server, dict) and set(server) == {"command", "args"} and isinstance(server["command"], str)
            and isinstance(server["args"], list))


def _mac_unrecorded_entry(path, cur):
    """No record: the labhand-mac parts of `cur` (beacon's own parts already stripped from it) that setup
    added, judged like beacon's own legacy parts: absent from setup's .beacon-bak, or (no backup: setup
    created the file) the file holds nothing else. -> a record entry holding only those parts, or None (a
    labhand-mac part the user had before setup stays theirs)."""
    name = os.path.basename(path)
    bak = _json_backup(path)
    if name == ".mcp.json":
        server = _server_of(cur, MAC_SERVER)
        if server is None:
            return None
        if bak is not None:
            sub = _server_record(bak, cur, MAC_SERVER)
        else:
            only = set(cur) == {"mcpServers"} and list(cur["mcpServers"]) == [MAC_SERVER]
            sub = {"server": "added"} if only and _looks_like_mac_entry(server) else {}
        return {"servers": {MAC_SERVER: sub}} if sub else None
    if bak is not None:
        before = bak
    elif _only_mac_settings(cur):
        before = {}
    else:
        return None
    e = _settings_record(before, cur)
    return _with_mac(name, {}, e) if _mac_in_entry(name, e) else None


def _strip_mac_entry(path, cur, entry):
    """Take the labhand-mac parts a record entry lists out of the object `cur`. -> (object, changes)."""
    if os.path.basename(path) == ".mcp.json":
        return strip_mcp(cur, {"servers": {MAC_SERVER: _extra_entries(entry)[MAC_SERVER]}})
    return _strip_settings_recorded(cur, _mac_settings_rec(entry, cur))


def _strip_mac_unrecorded(path, cur, ch):
    """Teardown without a record (deleted by hand): the labhand-mac parts setup added (`_mac_unrecorded_entry`),
    only in a file beacon was wired in (`ch`: beacon's parts were removed from it). -> (object, changes)."""
    if not ch:
        return cur, ch
    entry = _mac_unrecorded_entry(path, cur)
    if entry is None:
        return cur, ch
    c, more = _strip_mac_entry(path, cur, entry)
    return c, ch + more


def _same_as_backup(path, bak, new, record=None):
    """Is the stripped object `new` what setup found (`bak`)? Exactly, or once the empty containers that
    stripping drops are dropped from both: setup fills `{"mcpServers": {}}` or `"hooks": {}` and stripping
    empties and removes them, which is no change of the user's. A backup that holds beacon's parts (a
    stripped compare would put them back) is never a match this way."""
    if json.dumps(bak) == json.dumps(new):
        return True
    norm_bak, wired = _strip_json(path, bak, record)
    return not wired and json.dumps(norm_bak) == json.dumps(_strip_json(path, new, record)[0])


def _put_json(path, new, ch, td):
    """Write the stripped JSON: the original's exact bytes when they hold the same object, serialised
    identically (key order, true vs 1, 1.0 vs 1 count: `==` on dicts ignores them, so a user's change of
    them during the run would be lost) up to the empty containers stripping drops, else the object itself;
    a file left with nothing in it is removed only when setup created it (no backup that matches)."""
    bak = _json_backup(path)
    if bak is not None and _same_as_backup(path, bak, new, td.record):
        td.restore(path)
        ch.append("restored the file as it was before setup")
    elif new:
        td.write(path, json.dumps(new, indent=2) + "\n")
    else:
        td.remove(path)
        ch.append("removed the file (nothing else was in it)")


def _old_backups(bak):
    """The `<file>.beacon-bak.old[.N]` names setup moved older backups aside to, in order."""
    d, base = os.path.split(bak)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    pat = re.compile(re.escape(base) + r"\.old(?:\.(\d+))?\Z")
    found = [(int(m.group(1) or 0), n) for n in names for m in [pat.match(n)] if m]
    return [os.path.join(d, n) for _, n in sorted(found)]


GIT_TIMEOUT = 10


def _git(args, cwd):
    """Run git in `cwd` with every GIT_* variable of this environment dropped (a GIT_DIR or GIT_INDEX_FILE
    inherited from a hook would point it at another repository). -> stdout bytes, or None on any failure:
    git missing, a timeout, a non-zero exit."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"                 # read-only: never take the index lock
    try:
        r = subprocess.run(["git", "--literal-pathspecs", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=GIT_TIMEOUT)
    except Exception:                               # OSError, TimeoutExpired, ...: never fail teardown on git
        return None
    return r.stdout if r.returncode == 0 else None


def _head_blob(path):
    """The bytes of the file at `path` in its repository's HEAD commit, with the path git tracks it under:
    (bytes, path in the repository), or None when there is none to compare with (not in a git work tree,
    git missing or failing, no HEAD yet, the path not tracked in HEAD, or tracked as anything but a regular
    file). A symlink is followed to its target, the file a backup copies, and the target's tracked version
    is read in the repository that holds the target. HEAD only: the index (staged changes) never counts."""
    try:
        real = os.path.realpath(path)
        cwd = os.path.dirname(real)
        if not os.path.isdir(cwd):
            return None
        top = _git(["rev-parse", "--show-toplevel"], cwd)
        if not top or not top.endswith(b"\n"):
            return None
        top = os.path.realpath(os.fsdecode(top[:-1]))
        rel = os.path.relpath(real, top)
        if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
            return None
        rel = rel.replace(os.sep, "/")
        out = _git(["ls-tree", "-z", "--full-tree", "HEAD", "--", rel], top)
        if not out:
            return None
        entries = [e for e in out.split(b"\0") if e]
        if len(entries) != 1:
            return None
        meta, _, name = entries[0].partition(b"\t")
        parts = meta.split(b" ")
        if (len(parts) != 3 or parts[0] not in (b"100644", b"100755") or parts[1] != b"blob"
                or os.fsdecode(name) != rel):
            return None
        sha = parts[2].decode("ascii")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            return None
        blob = _git(["cat-file", "blob", sha], top)
        return None if blob is None else (blob, rel)
    except Exception:
        return None


def _settle_backups(project, td):
    """After teardown: a backup the file is back to (bytes and permission bits, through a symlink) is
    removed; any other backup is kept and named, with what it holds. The older backups setup moved aside
    (`.beacon-bak.old[.N]`) are settled the same way: one whose bytes the final file has is removed, every
    other one is kept and named. A backup (or .old) whose bytes equal the file's version in HEAD is removed
    too ("committed in HEAD", `_head_blob`): git holds that content. -> messages.

    A backup is deleted only when the same bytes exist in the file it backs up or in the HEAD commit, or
    when it holds nothing but beacon's own command (`.claude/beacon` of an older version: no copy of anything the project owns).
    A backup that is not a regular file is left alone and named."""
    out = []
    for rel in WIRED:
        path = os.path.join(project, rel)
        bak = path + BAK
        name = rel + BAK
        final = td.state(path)
        head = []                                   # HEAD's version, looked up once and only when needed

        def in_head(data, head=head, path=path):
            if not head:
                head.append(_head_blob(path))
            return head[0] is not None and head[0][0] == data

        def committed(head=head):
            return f"committed in HEAD: the same bytes as {head[0][1]} in the repository's HEAD commit"
        if path in td.made:
            have = td.made[path] if td.dry_run else _file_state(bak)
        elif os.path.lexists(bak) and (os.path.islink(bak) or not os.path.isfile(bak)):
            have = False
        elif os.path.isfile(bak):
            have = _file_state(bak)
        else:
            have = ()
        teardowns = path in td.made or (have and _is_wired(bak, os.path.basename(path), td.record) is True)
        if have == ():
            pass
        elif have is False:
            out.append(f"{name}: kept the backup: it is not a regular file, so it was left alone")
        elif have is None:
            out.append(f"{name}: kept the backup: it could not be read")
        elif os.path.basename(path) == "beacon" and teardowns:
            _drop(bak, td, out, f"{name}: removed the backup (it held beacon's own command, not a file of the "
                                f"project's)")
        elif final == have:
            _drop(bak, td, out, f"{name}: removed the backup ({rel} is back to it byte for byte)",
                  f"{name}: kept the backup: {rel} is back to it byte for byte, but removing it failed")
        elif in_head(have[0]):
            _drop(bak, td, out, f"{name}: removed the backup ({committed()})",
                  f"{name}: kept the backup: it is {committed()}, but removing it failed")
        elif teardowns:
            what = f"teardown's copy of {rel} taken before it removed beacon's parts" + (
                "" if path in td.made else " (by a teardown that did not finish)")
            gone = "" if final is not None else f"; {rel} was removed, so it holds the only copy of its content"
            out.append(f"{name}: kept the backup: {what}; it holds changes made during the run{gone}")
        elif final is None:
            gone = "was removed" if td.dry_run or not os.path.lexists(path) else "cannot be read"
            out.append(f"{name}: kept the backup: {rel} {gone}, so the backup holds the only copy of its content")
        elif final[0] == have[0]:
            out.append(f"{name}: kept the backup: same content as {rel}, but the permission bits differ "
                       f"({final[1]:o} in the file, {have[1]:o} in the backup)")
        else:
            out.append(f"{name}: kept the backup: {rel} differs from it (edited during the run, or it could not "
                       f"be restored), so it holds the file as it was before the run")
        for old in _old_backups(bak):
            oname = os.path.relpath(old, project)
            if os.path.islink(old) or not os.path.isfile(old):
                out.append(f"{oname}: kept older backup: not a regular file, so it was left alone")
                continue
            ostate = _file_state(old)
            if ostate is None:
                out.append(f"{oname}: kept older backup: it could not be read")
            elif os.path.basename(path) == "beacon" and _is_wired(old, "beacon") is True:
                _drop(old, td, out, f"{oname}: removed the older backup (it held beacon's own command, not a "
                                    f"file of the project's)")
            elif final is not None and final[0] == ostate[0]:
                _drop(old, td, out, f"{oname}: removed the older backup ({rel} has the same bytes)",
                      f"{oname}: kept older backup: {rel} has the same bytes, but removing it failed")
            elif in_head(ostate[0]):
                _drop(old, td, out, f"{oname}: removed the older backup ({committed()})",
                      f"{oname}: kept older backup: it is {committed()}, but removing it failed")
            else:
                why = f"differs from {rel}" if final is not None else f"{rel} does not exist"
                out.append(f"{oname}: kept older backup: {why} (setup moved it aside: it is from an earlier run)")
    return out


def _drop(path, td, out, done, failed=None):
    """Remove a settled backup (not with dry_run) and append `done`; on failure append `failed` instead."""
    try:
        if not td.dry_run:
            os.remove(path)
        out.append(done)
    except OSError as e:
        out.append(f"{failed or done.split(': ', 1)[0] + ': kept the backup: removing it failed'} ({e.strerror})")


def teardown_project(project, dry_run=False):
    """Undo setup_project in DIR. -> the list of changes (what would change, with dry_run)."""
    project = os.path.abspath(os.path.expanduser(project))
    if not os.path.isdir(project):
        raise FileNotFoundError(f"no such project directory: {project}")
    changes = []
    record = _read_record(project)
    td = _Teardown(dry_run, record)
    for rel in (os.path.join(".claude", "settings.json"), os.path.join(".claude", "settings.local.json"),
                ".mcp.json"):
        path = os.path.join(project, rel)
        if not os.path.exists(path):
            continue
        new, ch = _strip_json(path, _read_json(path), record)
        if ch:
            _put_json(path, new, ch, td)
        if record is not None:
            ch += _kept_notes(os.path.basename(path), new)
        changes += [f"{rel}: {c}" for c in ch]

    wp = os.path.join(project, ".claude", "beacon")
    # only beacon's own command is touched; any other file there is the project's and stays as it is
    if os.path.isfile(wp) and _is_wired(wp) is True:
        if os.path.isfile(wp + BAK) and not _is_wrapper_backup(wp) and _file_state(wp + BAK) is not None:
            td.restore(wp)                      # setup overwrote a file of the project's own: put it back
            changes.append(".claude/beacon: restored the project's own file setup had replaced")
        else:
            td.remove(wp)
            changes.append(".claude/beacon: removed the in-chat command")

    cp = os.path.join(project, "CLAUDE.md")
    if os.path.exists(cp):
        with open(cp, encoding="utf-8") as fh:
            old = fh.read()
        bak = _backup_text(cp)
        new_text, ch = strip_claude_md(old, bak)
        if ch:
            if bak is not None and bak == new_text:
                td.restore(cp)                  # exact bytes: line endings, a missing final newline, an empty file
            elif new_text.strip():
                td.write(cp, new_text)
            else:
                td.remove(cp)
                ch.append("removed the file (nothing else was in it)")
            changes += [f"CLAUDE.md: {c}" for c in ch]
    # last, so a failure in any step above leaves every backup where it was
    changes += _settle_backups(project, td)
    if record is not None:
        # the record goes last of all: a teardown that stops before this point can be run again
        rp = os.path.join(project, RECORD)
        try:
            if not dry_run:
                os.remove(rp)
            changes.append(f"{RECORD}: removed setup's record of what it added")
        except OSError as e:
            changes.append(f"{RECORD}: kept setup's record: removing it failed ({e.strerror})")
    return changes
