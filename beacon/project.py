# -*- coding: utf-8 -*-
"""beacon.project — wire a worker project to beacon (`beacon setup-project DIR`).

Writes, inside DIR only (apart from the target of a symlinked file, below), merging with whatever is already there:
  .claude/settings.local.json   the five hooks + an allow rule for the beacon MCP tools
                                (settings.json instead with --shared; the hook commands
                                carry absolute paths of this machine, hence "local")
  .mcp.json                     the beacon MCP server entry
  CLAUDE.md                     the worker rules (beacon/WORKER.md) between markers,
                                replaced in place on a re-run

A wired file that is a symlink (CLAUDE.md -> a shared file) is written through: the link stays a link and
its target, wherever it lives, gets the change. A rewritten file keeps its permission bits.

Existing files are copied to <file>.beacon-bak once (an earlier backup is kept).
Nothing is removed from an existing file: other hooks, servers, rules and text stay.

`beacon teardown-project DIR` is the inverse: it removes exactly what setup wrote (the rules block,
beacon's hooks / allow / deny entries, the beacon MCP server, .claude/beacon) and keeps everything
else; a file left with nothing in it is removed. Backups as above. When what is left equals setup's
.beacon-bak, the backup is put back byte for byte (line endings, trailing blank lines, JSON formatting,
mode), so a run that changed nothing leaves no diff; a file setup created is removed; an edit the user
made during the run outside beacon's parts is kept.
"""
import json
import os
import re
import shutil
import stat

from .shellrun import posix_path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_EVENTS = (("Stop", "stop", 300), ("SessionStart", "session-start", 30),
               ("PreToolUse", "pre-tool-use", 10), ("PostToolUse", "post-tool-use", 10),
               ("StopFailure", "stop-failure", 30), ("UserPromptSubmit", "user-prompt-submit", 30))
# Deny rules block in every permission mode, bypassPermissions included, and they do not
# depend on the hooks running: a second line of defence for the supervisor's own files.
# (Edit rules cover every built-in file-editing tool.)
PROTECT_DENY = ("Edit(**/ledger.db*)", "Bash(sqlite3 *)", "Edit(**/.claude/settings.json)",
                "Edit(**/.claude/settings.local.json)", "Edit(**/.mcp.json)", "Edit(**/.claude/beacon)") + tuple(
    f"Bash(.claude/beacon {c} *)" for c in ("answer", "verify", "set-verify", "reopen", "config", "lighthouse",
                                            "init", "setup-project", "teardown-project"))
WRAPPER = """#!/bin/sh
# beacon, for `! .claude/beacon ...` in the Claude Code chat (and any terminal in this project).
# Written by `beacon setup-project`. The commands that act as the human (answer, verify, ...) need a terminal.
PYTHONPATH="{repo}${{PYTHONPATH:+{sep}$PYTHONPATH}}" exec "{python}" -m beacon "$@"
"""
MARK_BEGIN = "<!-- beacon:worker-rules:begin -->"
MARK_END = "<!-- beacon:worker-rules:end -->"


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


def _write(path, content, dry_run):
    if dry_run:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and not os.path.exists(path + ".beacon-bak"):
        shutil.copy2(path, path + ".beacon-bak")
    real = _target(path)
    tmp = real + ".beacon-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    if os.path.exists(real):
        os.chmod(tmp, stat.S_IMODE(os.stat(real).st_mode))     # a rewritten file keeps its mode, not the umask's
    os.replace(tmp, real)


def setup_project(project, python, mcp_python, shared=False, allow_labhand=False, dry_run=False,
                  quiet_shell=False):
    project = os.path.abspath(os.path.expanduser(project))
    if not os.path.isdir(project):
        raise FileNotFoundError(f"no such project directory: {project}")
    changes = []
    allow = ("mcp__beacon", "mcp__labhand") if allow_labhand else ("mcp__beacon",)

    sp = os.path.join(project, ".claude", "settings.json" if shared else "settings.local.json")
    other = os.path.join(project, ".claude", "settings.local.json" if shared else "settings.json")
    new, ch = merge_settings(_read_json(sp), python, allow=allow, skip_events=beacon_events(_read_json(other)))
    if quiet_shell and new.get("respondToBashCommands") is not False:
        new["respondToBashCommands"] = False      # `! cmd` output lands in the chat without a model reply
        ch.append("set respondToBashCommands=false (shell-mode output gets no automatic reply)")
    ch_real = [c for c in ch if not c.startswith("left ")]
    notes = [c for c in ch if c.startswith("left ")]
    changes += [f"{os.path.relpath(sp, project)}: {c}" for c in notes]
    ch = ch_real
    if ch:
        _write(sp, json.dumps(new, indent=2) + "\n", dry_run)
        changes += [f"{os.path.relpath(sp, project)}: {c}" for c in ch]

    mp = os.path.join(project, ".mcp.json")
    new, ch = merge_mcp(_read_json(mp), mcp_python)
    if ch:
        _write(mp, json.dumps(new, indent=2) + "\n", dry_run)
        changes += [f".mcp.json: {c}" for c in ch]

    wp = os.path.join(project, ".claude", "beacon")
    # the wrapper runs under sh / Git Bash; the separator is the one Python on this OS splits PYTHONPATH on
    wrapper = WRAPPER.format(repo=posix_path(REPO), python=posix_path(python), sep=os.pathsep)
    try:
        with open(wp, encoding="utf-8") as fh:
            same = fh.read() == wrapper
    except OSError:
        same = False
    if not same:
        if not dry_run:
            _write(wp, wrapper, dry_run)
            os.chmod(wp, 0o755)
        changes.append(".claude/beacon: wrote the in-chat command (`! .claude/beacon log`)")

    cp = os.path.join(project, "CLAUDE.md")
    with open(os.path.join(REPO, "beacon", "WORKER.md"), encoding="utf-8") as fh:
        rules = fh.read()
    old = ""
    if os.path.exists(cp):
        with open(cp, encoding="utf-8") as fh:
            old = fh.read()
    new_text, ch = merge_claude_md(old, rules)
    if ch:
        _write(cp, new_text, dry_run)
        changes += [f"CLAUDE.md: {c}" for c in ch]
    return changes


# ---------------------------------------------------------------- teardown
BEACON_HOOK = re.compile(r"-m\s+beacon\.hooks\b")
BEACON_WRAPPER = re.compile(rb'-m\s+beacon\s+"\$@"')       # a wrapper setup wrote (any version)


def _ours_allow(rule):
    return rule == "mcp__beacon" or str(rule).startswith("mcp__beacon__")


def _ours_deny(rule):
    # every rule setup has ever written: the current set, and any `.claude/beacon <cmd>` rule of an older version
    return rule in PROTECT_DENY or str(rule).startswith("Bash(.claude/beacon ")


def strip_settings(settings):
    """Return (new_settings, changes): beacon's hooks, allow and deny rules removed, the rest kept."""
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


def strip_mcp(cfg):
    c = json.loads(json.dumps(cfg or {}))
    servers = c.get("mcpServers")
    if not isinstance(servers, dict) or "beacon" not in servers:
        return c, []
    del servers["beacon"]
    if not servers:
        del c["mcpServers"]
    return c, ["removed beacon MCP server"]


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


def _backup(path, dry_run):
    if not dry_run and os.path.exists(path) and not os.path.exists(path + ".beacon-bak"):
        shutil.copy2(path, path + ".beacon-bak")


def _backup_text(path):
    """setup's backup of `path` read the way setup read the original, or None (none / not UTF-8 text)."""
    try:
        with open(path + ".beacon-bak", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _restore(path, dry_run):
    """Put setup's backup back byte for byte (mode and mtime too); the backup itself stays."""
    if not dry_run:
        real = _target(path)                    # through a symlink: the link stays a link
        tmp = real + ".beacon-tmp"
        shutil.copy2(path + ".beacon-bak", tmp)
        os.replace(tmp, real)


def _json_backup(path):
    """The backup of a JSON file parsed like setup parsed the original, or None."""
    if not os.path.isfile(path + ".beacon-bak"):
        return None
    try:
        return _read_json(path + ".beacon-bak")
    except (OSError, ValueError):                 # UnicodeDecodeError and JSONDecodeError are ValueErrors
        return None


def _put_json(path, new, ch, dry_run):
    """Write the stripped JSON: the original's exact bytes when they hold the same object, serialised
    identically (key order, true vs 1, 1.0 vs 1 count: `==` on dicts ignores them, so a user's change of
    them during the run would be lost), else the object itself; a file left with nothing in it (and none
    before setup) is removed."""
    bak = _json_backup(path)
    if bak is not None and json.dumps(bak) == json.dumps(new):
        _restore(path, dry_run)
        ch.append("restored the file as it was before setup")
    elif new:
        _write(path, json.dumps(new, indent=2) + "\n", dry_run)
    else:
        _remove(path, dry_run)
        ch.append("removed the file (nothing else was in it)")


def _remove(path, dry_run):
    if dry_run:
        return
    _backup(path, dry_run)
    os.remove(_target(path))        # the file setup wrote through a symlink, not the link


def teardown_project(project, dry_run=False):
    """Undo setup_project in DIR. -> the list of changes (what would change, with dry_run)."""
    project = os.path.abspath(os.path.expanduser(project))
    if not os.path.isdir(project):
        raise FileNotFoundError(f"no such project directory: {project}")
    changes = []
    for name in ("settings.json", "settings.local.json"):
        sp = os.path.join(project, ".claude", name)
        if not os.path.exists(sp):
            continue
        new, ch = strip_settings(_read_json(sp))
        if not ch:
            continue
        rel = os.path.relpath(sp, project)
        _put_json(sp, new, ch, dry_run)
        changes += [f"{rel}: {c}" for c in ch]

    mp = os.path.join(project, ".mcp.json")
    if os.path.exists(mp):
        new, ch = strip_mcp(_read_json(mp))
        if ch:
            _put_json(mp, new, ch, dry_run)
            changes += [f".mcp.json: {c}" for c in ch]

    wp = os.path.join(project, ".claude", "beacon")
    if os.path.isfile(wp):
        try:
            with open(wp + ".beacon-bak", "rb") as fh:
                bak = fh.read()
        except OSError:
            bak = None
        if bak is not None and not BEACON_WRAPPER.search(bak):
            _restore(wp, dry_run)               # setup overwrote a file of the project's own: put it back
            changes.append(".claude/beacon: restored the project's own file setup had replaced")
        else:
            _remove(wp, dry_run)
            changes.append(".claude/beacon: removed the in-chat command")

    cp = os.path.join(project, "CLAUDE.md")
    if os.path.exists(cp):
        with open(cp, encoding="utf-8") as fh:
            old = fh.read()
        bak = _backup_text(cp)
        new_text, ch = strip_claude_md(old, bak)
        if ch:
            if bak is not None and bak == new_text:
                _restore(cp, dry_run)           # exact bytes: line endings, a missing final newline, an empty file
            elif new_text.strip():
                _write(cp, new_text, dry_run)
            else:
                _remove(cp, dry_run)
                ch.append("removed the file (nothing else was in it)")
            changes += [f"CLAUDE.md: {c}" for c in ch]
    return changes
