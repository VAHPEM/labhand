# -*- coding: utf-8 -*-
"""beacon.hooks — the Claude Code hook entry points.

    python3 -m beacon.hooks stop            (Stop)
    python3 -m beacon.hooks session-start   (SessionStart)
    python3 -m beacon.hooks pre-tool-use    (PreToolUse)
    python3 -m beacon.hooks post-tool-use   (PostToolUse: activity hash only)
    python3 -m beacon.hooks stop-failure    (StopFailure, observability only)
    python3 -m beacon.hooks user-prompt-submit  (UserPromptSubmit: `beacon:` commands, answers in chat)

A session is supervised only if it was launched with BEACON_RUN=<run dir> in
its environment (hooks inherit Claude Code's environment). Every other session
in the same project passes straight through — this is the session isolation the
ralph-loop plugin lacked.

FAIL-OPEN: any exception inside a hook is logged to <run>/hook-errors.log and the
hook exits 0 with no output, i.e. the stop is allowed / the tool runs. A broken
supervisor must never turn into an infinite, money-burning loop.
"""
import fnmatch
import glob
import json
import os
import re
import shlex
import sys
import time
import traceback

from .board import refresh_if_open
from .ledger import Ledger, run_dir

WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
TASK_TOOLS = ("TodoWrite", "TaskCreate", "TaskUpdate")
# Tools that make the session submit a prompt to itself later. Such a prompt would reach the
# UserPromptSubmit hook as if the human had typed it (and `beacon:` there acts as the human).
SCHEDULE_TOOLS = ("CronCreate", "ScheduleWakeup")
# A second Claude Code started from Bash inherits BEACON_RUN: its prompts would also look human.
NESTED_CLAUDE = re.compile(r"(?:^|[;&|(`]\s*|\$\(\s*|\b(?:exec|nohup|xargs|npx|sudo)\s+|\benv\s+(?:\S+=\S*\s+)*"
                           r"|\btimeout\s+\S+\s+)(?:\S*/)?claude(?=\s|$|[;&|)`])")
OWN_TOOL_PREFIX = "mcp__beacon__"
# THE classification of every `beacon` CLI subcommand (one source of truth): the PreToolUse guard's regex
# and message below, setup-project's deny rules (project.PROTECT_DENY) and the CLI's own worker-context
# refusal (cli.main) are all built from these. tests/beacon check that every subcommand of cli.build_parser
# is in exactly one of HUMAN_COMMANDS / WORKER_COMMANDS.
# Human-only: the worker could otherwise verify its own candidate, answer its own questions, set notify_cmd
# (a shell command the hooks run), or start a nested supervised Claude Code (claude / watch / demo / eval),
# which inherits the run and possibly a bypassPermissions mode.
HUMAN_COMMANDS = ("answer", "verify", "set-verify", "reopen", "config", "lighthouse", "init",
                  "setup-project", "teardown-project", "claude", "watch", "demo", "eval")
# Read-only views the worker may run from Bash.
WORKER_COMMANDS = ("status", "report", "graph", "check", "hooks-config", "log", "transcript",
                   "calibrate-refusals", "doctor")
# The human commands that start a Claude Code session: also refused by the CLI itself when it runs inside a
# supervised session (inherited BEACON_RUN, stdin not a TTY), so even an invocation the regex misses is blocked.
NESTED_SESSION_COMMANDS = ("claude", "watch", "demo", "eval")


# ---- the Bash guard's view of a command line: shell words, not raw text (review P4) ----
# Matching raw text missed `.claude/beacon 'claude'`, `python3 -mbeacon watch`, `beacon -- watch`, a repeated
# `--run X`, `beacon${IFS}watch`; it also blocked ordinary commands that merely mention the words
# (`grep 'beacon watch' README.md`). So the command is split into shell words (quotes and backslashes removed)
# and simple commands (at ; & | ( ) ` and newlines), and only a beacon invocation in COMMAND POSITION counts.
_BEACON_MODULES = ("beacon", "beacon.cli", "beacon.__main__")
_PREFIX_WORDS = ("exec", "nohup", "sudo", "command", "builtin", "time", "nice", "caffeinate", "stdbuf", "doas",
                 "setsid", "chronic")
_SHELLS = ("sh", "bash", "zsh", "dash", "ksh", "fish")
_PY_OPTS_WITH_ARG = ("-X", "-W", "-Q")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _shell_words(cmd):
    """The command's simple commands, each a list of shell words. Unparseable quoting falls back to
    whitespace splitting with the quote characters dropped (a guard reads too much rather than too little)."""
    text = re.sub(r"\$\{IFS\}|\$IFS\b", " ", cmd).replace("\r\n", "\n").replace("\\\n", " ")
    text = text.replace("\r", "\n").replace("\n", " ; ")
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()`")
        lex.whitespace_split = True
        lex.commenters = ""
        toks = list(lex)
    except ValueError:
        toks = re.sub(r"[;&|()`]", lambda m: f" {m.group(0)} ", re.sub(r"[\"'\\]", "", text)).split()
    segs, cur = [], []
    for t in toks:
        if t and all(ch in ";&|()`" for ch in t):
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t[1:] if t.startswith("$") and t[1:2] == "(" else t)
    if cur:
        segs.append(cur)
    return segs


def _strip_prefixes(words):
    """Drop what runs another command without being it: VAR=x, env [-i] [-u N] [VAR=x], nohup, sudo, timeout N,
    xargs [opts], uv/poetry/pipenv run, ... Returns (words from the command word on, ran_through_xargs)."""
    i, xargs = 0, False
    while i < len(words):
        w, base = words[i], os.path.basename(words[i])
        if _ASSIGN.match(w):
            i += 1
        elif base == "env":
            i += 1
            while i < len(words) and (words[i].startswith("-") or _ASSIGN.match(words[i])):
                i += 2 if words[i] in ("-u", "-C", "-S", "--unset", "--chdir") else 1
        elif base in _PREFIX_WORDS:
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in ("-u", "-g", "-n", "-C", "-a", "-p", "-h") else 1
        elif base in ("timeout", "gtimeout"):
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in ("-s", "-k", "--signal", "--kill-after") else 1
            i += 1                                           # the duration
        elif base == "script":                               # script [-adkpqr] [-F p] [-t t] file command...
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in ("-F", "-t", "-T", "-c", "-E", "-I", "-O", "-B") else 1
            i += 1                                           # the typescript file
        elif base == "xargs":
            xargs, i = True, i + 1
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in ("-I", "-n", "-P", "-L", "-d", "-E", "-s", "-J", "-R") else 1
        elif base in ("uv", "poetry", "pipenv", "pdm", "hatch", "rye") and i + 1 < len(words) \
                and words[i + 1] == "run":
            i += 2
        else:
            break
    return words[i:], xargs


def _beacon_args(words):
    """If `words` (from the command word on) invoke the beacon CLI, return the words after the CLI itself,
    else None. Recognised: anything named `beacon` (.claude/beacon, a PATH entry), `python[3[.x]] [opts]
    -m beacon[.cli|.__main__]` (also `-mbeacon`, `-Im beacon`), `python .../beacon/cli.py|__main__.py`."""
    if not words:
        return None
    base = os.path.basename(words[0])
    if base == "beacon":
        return words[1:]
    if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?w?(?:\.exe)?|pypy3?", base):
        return None
    i = 1
    while i < len(words):
        w = words[i]
        if w == "--":
            i += 1
            break
        if w.startswith("--"):                               # --check-hash-based-pycs MODE, --help, ...
            i += 2 if w == "--check-hash-based-pycs" else 1
            continue
        if w.startswith("-") and len(w) > 1:                 # a cluster of short options: -I, -Im, -mbeacon, -Xdev
            for k, ch in enumerate(w[1:], 1):
                rest = w[k + 1:]
                if ch in "XW":                               # takes a value, attached or the next word
                    i += 1 if rest else 2
                    break
                if ch == "m":
                    mod = rest if rest else (words[i + 1] if i + 1 < len(words) else "")
                    after = i + 1 if rest else i + 2
                    return words[after:] if mod in _BEACON_MODULES else None
                if ch == "c":
                    return None
            else:
                i += 1
            continue
        break
    if i < len(words):
        norm = words[i].replace("\\", "/")
        if re.search(r"(?:^|/)beacon(?:/(?:cli|__main__)\.py)?/?$", norm):
            return words[i + 1:]
    return None


def _beacon_subcommand(rest):
    """The subcommand argparse would see: skips `--`, `--run X`, `--run=X` (any number) and other options."""
    i = 0
    while i < len(rest):
        w = rest[i]
        if w == "--run":
            i += 2
        elif w.startswith("-"):
            i += 1
        else:
            return w
    return None


def _python_c_code(words):
    """The code of `python [opts] -c CODE` (also -Ic CODE, -cCODE), else None."""
    if not words or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?w?(?:\.exe)?|pypy3?", os.path.basename(words[0])):
        return None
    i = 1
    while i < len(words):
        w = words[i]
        if w == "--" or not w.startswith("-") or len(w) < 2:
            return None
        if w.startswith("--"):
            i += 1
            continue
        step = 1
        for k, ch in enumerate(w[1:], 1):
            rest = w[k + 1:]
            if ch in "XW":
                step = 1 if rest else 2
                break
            if ch == "m":
                return None
            if ch == "c":
                return rest if rest else (words[i + 1] if i + 1 < len(words) else "")
        i += step
    return None


def human_cli_hit(cmd, _depth=0):
    """The human-only beacon subcommand `cmd` would run (from HUMAN_COMMANDS), else None."""
    if _depth > 3:
        return None
    human = set(HUMAN_COMMANDS)
    for seg in _shell_words(cmd):
        words, xargs = _strip_prefixes(seg)
        if not words:
            continue
        base = os.path.basename(words[0])
        if base == "eval" and len(words) > 1:                     # the shell builtin: its arguments are a command
            hit = human_cli_hit(" ".join(words[1:]), _depth + 1)
            if hit:
                return hit
            continue
        if base in _SHELLS:
            j, script = 1, None
            while j < len(words):
                w = words[j]
                if w.startswith("-") and not w.startswith("--") and "c" in w:     # sh -c STRING, bash -lc STRING
                    hit = human_cli_hit(words[j + 1], _depth + 1) if j + 1 < len(words) else None
                    if hit:
                        return hit
                    break
                if w.startswith(("-", "+")):
                    j += 2 if w in ("-o", "+o", "--rcfile", "--init-file") else 1
                    continue
                script = j                                       # sh .claude/beacon watch: the wrapper is a script
                break
            if script is None:
                continue
            words = words[script:]
        code = _python_c_code(words)
        if code is not None:                                     # python -c "...beacon.cli...main(['watch'])"
            if "beacon" in code and re.search(r"\bcli\b|__main__|runpy|\bmain\b", code):
                for c in HUMAN_COMMANDS:
                    if re.search(r"""['"]""" + re.escape(c) + r"""['"]""", code):
                        return c
            continue
        if words[0].startswith("$") and len(words) > 1 and words[1] in human:   # B=.claude/beacon; $B watch
            return words[1]
        rest = _beacon_args(words)
        if rest is None:
            continue
        sub = _beacon_subcommand(rest)
        if sub in human:
            return sub
        if xargs and sub not in WORKER_COMMANDS:                 # echo watch | xargs .claude/beacon [{}]
            return "(subcommand from xargs)"
    return None


class _HumanCli:
    """HUMAN_CLI.search(cmd): truthy when `cmd` runs a human-only beacon subcommand (kept as the guard's
    historic interface; built from HUMAN_COMMANDS like everything else)."""

    @staticmethod
    def search(cmd):
        return human_cli_hit(str(cmd))


HUMAN_CLI = _HumanCli()


def _claude_in_command_position(cmd):
    for seg in _shell_words(cmd):
        words, _ = _strip_prefixes(seg)
        if words and os.path.basename(words[0]) == "claude":
            return True
        if words and os.path.basename(words[0]) == "npx" and len(words) > 1 and \
                os.path.basename(words[1]) == "claude":
            return True
    return False

HUMAN_ONLY_MESSAGE = ("That beacon command is for the human (" + ", ".join(HUMAN_COMMANDS) + "). Use the beacon "
                      "tools; ask() if you need the human. A subagent is the Agent tool, not a nested session.")
WEB_TOOLS = ("WebSearch", "WebFetch")
# under .claude/; beacon-setup.json is setup's record of what it added (teardown removes exactly that)
PROTECTED_NAMES = ("settings.json", "settings.local.json", "beacon-setup.json")


def _append_jsonl(run, name, obj):
    try:
        with open(os.path.join(run_dir(run), name), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _notify(L, event, report_path=""):
    cmd = (L.config().get("notify_cmd") or "").strip()
    if not cmd:
        return
    env = dict(os.environ, BEACON_EVENT=event, BEACON_REPORT=report_path, BEACON_RUN_DIR=L.dir)
    try:
        from .shellrun import run
        run(cmd, env=env, timeout=20)                          # sh, or Git Bash on Windows (as Claude Code)
    except Exception:  # noqa: BLE001  -- a notification must never break the hook
        pass


# ---------------------------------------------------------------- Stop
def stop(run, inp):
    from .classify import classify
    from .followups import extract
    from .gate import on_stop, stop_lock
    from .lighthouse import run_lighthouse
    from .report import write_report

    L = Ledger.open(run)
    try:
        _append_jsonl(run, "outputs.jsonl", {
            "ts": time.time(), "session": inp.get("session_id"),
            "stop_hook_active": inp.get("stop_hook_active"), "transcript_path": inp.get("transcript_path"),
            "text": inp.get("last_assistant_message") or ""})
        _remember_session(run, inp)
        t0 = time.monotonic()
        with stop_lock(L.dir) as locked:
            if not locked:
                L.record("stop_lock_timeout", {"session": inp.get("session_id")})
            d = on_stop(L, inp, lighthouse=run_lighthouse, classifier=classify, followup=extract,
                        rotate=os.environ.get("BEACON_WATCHDOG") == "1", started=t0)
        record = {"ts": time.time(), "session": inp.get("session_id"), "action": d.action, "why": d.why,
                  "reason": d.reason, "report": d.report, "classified": d.classified or None,
                  "lighthouse": {k: d.lighthouse.get(k) for k in ("status", "added", "added_desc", "detail", "review")}
                  if d.lighthouse else None}
        g = L.gate_state()
        if d.action == "block":
            _append_jsonl(run, "decisions.jsonl", record)
            refresh_if_open(L)
            nxt = L.next_node()
            msg = (f"beacon ▸ kept the session going (stop {g['stops']}, forced {g['forced']}/"
                   f"{L.config()['hard_forced']})" + (f"; next: {nxt['id']} {nxt['title']}" if nxt else ""))
            if d.lighthouse.get("added"):
                msg += (f"; lighthouse{' (you asked)' if d.lighthouse.get('requested_by') else ''} queued "
                        f"{len(d.lighthouse['added'])} idea(s)")
            fu = (d.classified or {}).get("followups") or {}
            if fu.get("work"):
                msg += f"; Claude listed unfinished work -> {len(fu['work'])} node(s): {', '.join(fu['work'][:6])}"
            if d.why == "leftovers_unrecorded":
                msg += "; Claude listed unfinished work (no model to read it): told it to do or record each item"
            told = " ".join((d.reason or "").split())
            msg += "\n→ told Claude: " + (told[:420] + " …" if len(told) > 420 else told)
            return {"decision": "block", "reason": d.reason, "systemMessage": msg[:700]}
        integrity = L.verify_integrity()
        path = write_report(L, d, integrity)
        record["report_path"] = path
        _append_jsonl(run, "decisions.jsonl", record)
        refresh_if_open(L)
        # never run a command taken from a ledger that failed its check; no notice for a stop the
        # human chose (pause, plan mode) or a pause for background work
        if integrity[0] and d.why not in ("paused", "plan_mode", "background_wait"):
            _notify(L, "stop:" + d.why, path)
        msg = f"beacon ▸ let the session stop: {d.why}. Report: {path}"
        qs = L.questions("open")
        pend = [p for p in L.problems() if p["status"] == "pending_verification"]
        prop = [p for p in L.problems() if p["status"] == "proposed"]
        if qs or pend or prop:
            from .chat import _questions
            msg += "\nWaiting on you:"
            for p in pend[:5]:
                msg += (f"\n{p['id']} candidate: {(p.get('candidate') or '')[:200]}  -> `beacon: verify {p['id']}` "
                        f"or `beacon: verify {p['id']} reject <why>`")
            for p in prop[:5]:
                msg += (f"\n{p['id']} proposed by Claude: {p['title'][:160]}  -> `beacon: accept {p['id']}` "
                        f"or `beacon: reject {p['id']} <why>`")
            if qs:
                from .chat import how_to_answer
                msg += "\n" + _questions(L)[:1200] + "\n" + how_to_answer(qs, plain_ok=d.why == "awaiting_human")
            msg += "\n(`beacon: help` lists the chat commands.)"
        elif d.report:
            msg += "\n" + d.report[:400]
        return {"systemMessage": msg}
    finally:
        L.close()


def _remember_session(run, inp):
    """Record where Claude Code keeps this session's transcript (every hook input carries it)."""
    sid, path = inp.get("session_id"), inp.get("transcript_path")
    if not sid or not path:
        return
    try:
        with open(os.path.join(run_dir(run), "sessions.jsonl"), encoding="utf-8") as fh:
            if any(json.loads(x).get("session") == sid for x in fh if x.strip()):
                return
    except (OSError, ValueError):
        pass
    _append_jsonl(run, "sessions.jsonl", {"ts": time.time(), "session": sid, "transcript_path": path,
                                          "source": inp.get("source") or inp.get("hook_event_name")})


# ---------------------------------------------------------------- SessionStart
def session_start(run, inp):
    _remember_session(run, inp)
    L = Ledger.open(run)
    try:
        ok, why = L.verify_integrity()
        src = inp.get("source", "")
        if not ok:
            ctx = (f"A beacon run is configured for this session, but its ledger failed the integrity "
                   f"check ({why}). Tell the human and do not write to the ledger.")
            return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}
        L.record("session_start", {"session": inp.get("session_id"), "source": src})
        answered = [q for q in L.questions("answered") if not q["delivered"]]
        parts = [
            f"A beacon run \"{L.title()}\" supervises this session (session start reason: {src or 'startup'}).",
            "The ledger below is the source of truth for what is done and what is left. "
            "The Stop gate keeps the session going while workable nodes remain.",
            "Tools (beacon MCP server): ledger_view, problem_note, add_node, start_node, attach_evidence, set_status, "
            "propose_done, claim_near_done, ask, mark_waiting, vm_check, propose_problem.",
            "", _tree_for_start(L),
        ]
        for q in answered:
            parts.append(f"The human answered question #{q['id']} ({q['kind']}): {q['answer']}")
        if answered:
            L.mark_delivered([q["id"] for q in answered])
        nxt = L.next_node()
        if nxt is not None:
            parts.append(f"Next node by the ledger's ordering: {nxt['id']} \"{nxt['title']}\".")
        open_n, total = len(L.open_problems()), len(L.problems())
        msg = f"beacon ▸ run \"{L.title()}\" supervises this session: {open_n}/{total} problem(s) open."
        if os.path.exists(os.path.join(inp.get("cwd") or os.getcwd(), ".claude", "beacon")):
            msg += (" In this chat: `! .claude/beacon log` (what beacon replied), "
                    "`! .claude/beacon transcript -n 40`, `! .claude/beacon status`.")
        else:
            msg += f" Its log: `python3 -m beacon log -f --run {L.dir}` in a terminal."
        return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                       "additionalContext": "\n".join(parts)[:9000]},
                "systemMessage": msg}
    finally:
        L.close()


# ---------------------------------------------------------------- PreToolUse
def _variants(path):
    """A path as it may appear in a shell command: absolute, ~/..., $HOME/..."""
    out = {path}
    home = os.path.expanduser("~")
    if home and path.startswith(home + os.sep):
        rest = path[len(home):]
        out |= {"~" + rest, "$HOME" + rest, "${HOME}" + rest}
    return out


def protected_paths(run):
    rd = os.path.realpath(run_dir(run))
    pkg = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
    return [rd, pkg]


def _under(path, roots):
    rp = os.path.realpath(os.path.expanduser(path))
    return any(rp == r or rp.startswith(r + os.sep) for r in roots)


# An agent definition can carry `permissionMode: bypassPermissions`: one the worker writes (project
# .claude/agents or the user's ~/.claude/agents) would hand a subagent every permission. setup-project denies
# the edit (PROTECT_DENY: Edit/Write and every built-in file tool); this guard covers Bash and other MCP
# servers' file tools too.
#
# Precise, not a substring test: a path is an agent definition only when one of its components is `.claude`
# and the next is `agents` (whole components, case-insensitive like macOS file systems), after `~` expansion,
# `abspath` and resolving the symlinks of its existing prefix (`.claude/agents-notes.md`, `~/.claude/agentsX`
# are not). Only writes are refused: reading an agent file is harmless.
#
# Bash is parsed, not grepped, and a shell parse cannot be complete. Recognised (refused when a target is an
# agent definition): redirections (`>`, `>>`, `&>`, `N>`, `>|`, `<>`), `tee`, `cp`/`mv`/`ln`/`install`/
# `rsync`/`ditto`/`scp` destinations (`mv` and `ln` every operand; a recursive copy or a move onto a `.claude`
# directory too), `rm`/`rmdir`/`unlink`/`touch`/`mkdir`/`truncate`/`chmod`/... operands, `dd of=`, `sed -i` /
# `perl -i`, editors (vi, vim, nano, emacs, ed, ex, ...), `curl -o`, `wget -O/-P`, `tar -C`/`-f`, `unzip -d`,
# `git checkout|restore|mv|rm|...` operands, `find ... -delete|-exec`, and `python|perl|ruby|node|... -c/-e`
# code that names an agent path and writes. Followed through: `cd`/`pushd` within the command, `VAR=...`
# assignments and environment variables, globs and brace lists (matched against the component names, and
# expanded against the disk), quoting, `$(...)`, backticks, `bash -c`, `eval`, `sudo`/`env`/`xargs`/...
# prefixes, and heredocs fed to a shell or an interpreter. An unknown variable stands for one unknown
# component; a command that does not parse is refused when it names `.claude` next to `agents` at all.
# NOT recognised (documented gaps; the deny rules still cover the built-in Edit/Write tools, and Claude Code's
# auto-mode classifier is another layer): a script file run by an interpreter, a variable holding several
# components (`D=$(...); cat > "$D/x"` where `$D` is computed), writes into the current directory by a
# program not listed (`git checkout <branch>`, an archive extracted without `-C`), hard links, and an MCP
# server rooted inside `.claude` given a path relative to it.
AGENTS_DENIED = ("Agent definitions (.claude/agents in the project or your home) are off-limits during a beacon "
                 "run: one could grant a subagent permissions the run does not have. Reading them is allowed.")
_GLOB = re.compile(r"[*?\[]")
_UNKNOWN = "\x00"                       # stands for an unresolved variable: one unknown component
_UNKNOWN_CWD = "/" + _UNKNOWN           # the directory after a `cd` whose target is unknown


def _component_is(comp, name):
    c = comp.lower()
    if c == name:
        return True
    if _UNKNOWN in c:
        return True
    return bool(_GLOB.search(c)) and fnmatch.fnmatchcase(name, c)


def _has_agents_pair(path):
    comps = path.split("/")
    return any(_component_is(a, ".claude") and _component_is(b, "agents") and not (_UNKNOWN in a and _UNKNOWN in b)
               for a, b in zip(comps, comps[1:]))


def _path_forms(path, cwd):
    """The absolute forms of `path` to test: `abspath` (`./`, `//`, `..` folded) and `realpath` (symlinks of
    the existing prefix resolved), relative paths against `cwd`; a glob also by what it matches on disk."""
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    forms = {os.path.normpath(p)}
    try:
        forms.add(os.path.realpath(p))
        if _GLOB.search(p) and _UNKNOWN not in p:
            for g in glob.glob(p)[:200]:
                forms.add(os.path.realpath(g))
    except (OSError, ValueError):
        pass
    return forms


def is_agent_definition(path, cwd=None):
    """Is `path` inside (or is it) a `.claude/agents` directory: a whole `.claude` component followed by a
    whole `agents` component, as written (after `~`, `abspath`) or with the symlinks of its existing prefix
    resolved? Case-insensitive (macOS file systems usually are)."""
    if not isinstance(path, str) or not path or "\n" in path or len(path) > 4096:
        return False
    if path.startswith("file://"):
        path = path[len("file://"):]
    try:
        return any(_has_agents_pair(f) for f in _path_forms(path, cwd))
    except (TypeError, ValueError):
        return False


_is_agent_definition = is_agent_definition     # the name 171f4e5 used


# ---- Bash: what a command writes
_PUNCT = "();<>|&\n"
_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
_VAR = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*)|[({'\"]|\W|$)")
_BRACE = re.compile(r"\{([^{}]*,[^{}]*)\}")
_HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_][\w.-]*)\2")
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"}
_INTERP = re.compile(r"^(?:python[\d.]*|pypy[\d.]*|perl[\d.]*|ruby|node|nodejs|bun|deno|php|osascript|lua|"
                     r"rscript|tclsh|jshell|swift)$")
_HEREDOC_SHELL = re.compile(r"(?:^|[\s;|&(])(?:\S*/)?(?:(?:ba|z|da|k|c|tc)?sh|fish|eval|source)(?=[\s;|&)]|$)")
_HEREDOC_INTERP = re.compile(r"(?:^|[\s;|&(])(?:\S*/)?(?:python[\d.]*|pypy[\d.]*|perl[\d.]*|ruby|node|bun|deno|php|"
                             r"osascript|lua)(?=[\s;|&)]|$)")
_WRAPPERS = {"sudo", "doas", "env", "nohup", "time", "command", "builtin", "exec", "nice", "ionice", "timeout",
             "gtimeout", "stdbuf", "caffeinate", "chronic", "unbuffer", "then", "do", "else", "elif", "if",
             "while", "until", "!", "{", "}", "xargs", "arch", "script", "watch", "parallel"}
_WRAPPER_ARG_OPTS = {"sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"}, "doas": {"-u", "-C"},
                     "env": {"-u", "-C", "-S", "-P"}, "nice": {"-n"}, "ionice": {"-c", "-n", "-p"},
                     "timeout": {"-s", "-k", "--signal", "--kill-after"}, "gtimeout": {"-s", "-k"},
                     "xargs": {"-I", "-J", "-L", "-n", "-P", "-R", "-S", "-s", "-E", "-d", "-a"},
                     "arch": {"-arch"}, "watch": {"-n", "-d"}, "stdbuf": {"-i", "-o", "-e"}}
_ALL_OPERANDS = {"rm", "rmdir", "unlink", "touch", "mkdir", "truncate", "shred", "srm", "chmod", "chown", "chgrp",
                 "chflags", "xattr", "setfacl", "mkfifo", "mknod", "mv", "gmv", "ln", "gln", "tee", "gtee",
                 "sponge", "split", "csplit", "mktemp"}
_DEST = {"cp", "gcp", "install", "ginstall", "rsync", "ditto", "scp", "rcp"}
_EDITORS = {"vi", "vim", "nvim", "gvim", "mvim", "nano", "pico", "emacs", "emacsclient", "ed", "red", "ex", "micro",
            "joe", "jed", "kak", "hx", "helix", "mate", "subl", "code"}
_GIT_WRITE = {"checkout", "restore", "mv", "rm", "apply", "clone", "worktree", "stash", "reset", "switch", "am",
              "cherry-pick", "revert", "merge", "pull"}
_CWD_WRITERS = {"wget", "unzip", "7z", "7za", "cpio", "patch", "aa"}          # write into the cwd by default
_CODE_AGENTS = re.compile(r"\.claude\W{0,12}agents(?![\w.-])", re.I)
_CODE_WRITE = re.compile(r"\b(?:write\w*|writelines|dump|copy\w*|move|rename\w*|replace|symlink\w*|link\w*|"
                         r"mkdir\w*|makedirs|touch|unlink\w*|remove|rmtree|rm\w*|truncate|system|popen|Popen|run|"
                         r"call|check_call|check_output|exec\w*|spawn\w*|chmod\w*|append\w*|output\w*|"
                         r"put\w*|save\w*|create\w*|open\w*|mv|cp|install)\b|>")
_READ_OPEN_ONLY = re.compile(r"\bopen\s*\(([^()]*)\)")


def _expand_vars(word, env):
    """$NAME / ${NAME} from the command's own assignments, then this environment; anything else (`$(...)`,
    `$1`, an unknown name) becomes the unknown-component marker."""
    def sub(m):
        name = m.group(1) or m.group(2)
        if name is not None:
            val = env.get(name, os.environ.get(name))
            return _UNKNOWN if val is None else val
        return _UNKNOWN + m.group(0)[1:]
    return _VAR.sub(sub, word) if "$" in word else word


def _braces(word, limit=64):
    out, todo = [], [word]
    while todo and len(out) + len(todo) <= limit:
        w = todo.pop()
        m = _BRACE.search(w)
        if not m:
            out.append(w)
            continue
        todo += [w[:m.start()] + alt + w[m.end():] for alt in m.group(1).split(",")]
    return out + todo


def _split_heredocs(cmd):
    """-> (the command with heredoc bodies taken out, [(line holding the `<<`, body, unterminated)])."""
    lines, out, docs, i = cmd.split("\n"), [], [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in _HEREDOC.finditer(line):
            before = line[:m.start()]
            if before.count("'") % 2 or before.count('"') % 2:
                continue                                 # inside quotes: not a heredoc
            dash, delim, body = m.group(1), m.group(3), []
            while i < len(lines) and (lines[i].lstrip("\t") if dash else lines[i]) != delim:
                body.append(lines[i])
                i += 1
            docs.append((line, "\n".join(body), i >= len(lines)))
            i += 1                                       # the delimiter line
    return "\n".join(out), docs


def _tokens(cmd):
    lex = shlex.shlex(cmd.replace("\\\n", " ").replace("`", ";"), posix=True, punctuation_chars=_PUNCT)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _is_punct(tok):
    return bool(tok) and all(c in _PUNCT for c in tok)


def _code_writes(code, cwd):
    """Interpreter code (`python -c`, a heredoc fed to python, ...): names an agent path and writes?"""
    names_one = bool(_CODE_AGENTS.search(code))
    if not names_one:
        for m in re.finditer(r"[\w.~/$*?\[\]{}-]*agents[\w.~/$*?\[\]{}-]*", code, re.I):
            if any(is_agent_definition(w, cwd) for w in _braces(m.group(0))):
                names_one = True
                break
    if not names_one or not _CODE_WRITE.search(code):
        return False
    stripped = _READ_OPEN_ONLY.sub(lambda m: "" if not re.search(r"['\"][rbt]*[wax+][rbt+]*['\"]|mode", m.group(1))
                                   else m.group(0), code)
    return bool(_CODE_WRITE.search(stripped))


def bash_writes_agent_definition(cmd, cwd=None, _depth=0):
    """Does the shell command (as far as it can be parsed) write into a `.claude/agents` directory?"""
    if _depth > 4:
        return bool(_CODE_AGENTS.search(cmd))
    cmd, docs = _split_heredocs(cmd)
    for line, body, unterminated in docs:
        # a body fed to a shell is a script, one fed to an interpreter is code; an unterminated one (no
        # delimiter line: bash reads to the end) is checked as a script too; any other body is data
        if (unterminated or _HEREDOC_SHELL.search(line)) and bash_writes_agent_definition(body, cwd, _depth + 1):
            return True
        if _HEREDOC_INTERP.search(line) and _code_writes(body, cwd):
            return True
    try:
        toks = _tokens(cmd)
    except ValueError:                           # unbalanced quotes: no parse, so no precision either
        return bool(_CODE_AGENTS.search(cmd))
    st = {"cwd": cwd or os.getcwd(), "env": {}}
    words, redirs, i = [], [], 0
    while i < len(toks):
        t = toks[i]
        if _is_punct(t) and not t.endswith("("):
            if ">" in t or "<" in t:
                nxt = toks[i + 1] if i + 1 < len(toks) and not _is_punct(toks[i + 1]) else None
                if nxt is not None and ">" in t and not (t.endswith("&") and re.fullmatch(r"\d+|-", nxt)):
                    redirs.append(nxt)
                i += 2 if nxt is not None else 1
                continue
        if _is_punct(t):
            if _simple_writes(words, redirs, st, _depth):
                return True
            words, redirs = [], []
        else:
            words.append(t)
        i += 1
    return _simple_writes(words, redirs, st, _depth)


def _operands(args, arg_opts=()):
    """Positional arguments: options dropped (and the value of an option in `arg_opts`), `--` honoured."""
    out, i, plain = [], 0, False
    while i < len(args):
        a = args[i]
        if plain or not a.startswith("-") or a == "-":
            out.append(a)
        elif a == "--":
            plain = True
        elif a in arg_opts:
            i += 1
        i += 1
    return out


def _opt_values(args, short, long_=()):
    """Values of options like `-o FILE`, `-oFILE`, `--output FILE`, `--output=FILE`."""
    out = []
    for i, a in enumerate(args):
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a in short or a in long_:
            if nxt is not None:
                out.append(nxt)
        elif any(a.startswith(L + "=") for L in long_):
            out.append(a.split("=", 1)[1])
        elif any(a.startswith(s) and len(a) > len(s) and not a.startswith("--") for s in short):
            out.append(a[2:])
    return out


def _simple_writes(words, redirs, st, depth):
    env, cwd = st["env"], st["cwd"]

    def expand(w):
        return _braces(_expand_vars(w, env))

    def agent(w, here=None):
        return any(is_agent_definition(x, here or cwd) for x in expand(w))

    if any(agent(r) for r in redirs):
        return True
    words = list(words)
    while words and _ASSIGN.match(words[0]):                 # VAR=value [command]
        name, val = words.pop(0).split("=", 1)
        env[name] = _expand_vars(val, env)
    if len(words) >= 3 and words[0] in ("for", "select") and words[2] == "in":
        # `for f in .claude/agents/*; do rm "$f"; done`: the loop variable stands for its agent-path value
        vals = [x for w in words[3:] for x in expand(w)]
        env[words[1]] = next((v for v in vals if is_agent_definition(v, cwd)), vals[0] if vals else _UNKNOWN)
        return False
    # prefixes that run the rest as a command
    while words:
        w0 = os.path.basename(words[0]).lower()
        if w0 == "export" or w0 == "local" or w0 == "declare" or w0 == "readonly":
            for a in words[1:]:
                if _ASSIGN.match(a):
                    name, val = a.split("=", 1)
                    env[name] = _expand_vars(val, env)
            return False
        if w0 not in _WRAPPERS:
            break
        words.pop(0)
        opts = _WRAPPER_ARG_OPTS.get(w0, set())
        while words and (words[0].startswith("-") or (w0 == "env" and _ASSIGN.match(words[0]))):
            a = words.pop(0)
            if a in opts and words:
                words.pop(0)
        if w0 in ("timeout", "gtimeout") and words:
            words.pop(0)                                     # the duration
    if not words:
        return False
    cmd = os.path.basename(_expand_vars(words[0], env)).lower()
    args = words[1:]
    ops = _operands(args)
    if cmd in ("cd", "pushd", "chdir"):
        target = ops[0] if ops else "~"
        if target == "-":
            st["cwd"] = _UNKNOWN_CWD
        else:
            t = _expand_vars(target, env)
            t = os.path.expanduser(t)
            st["cwd"] = os.path.normpath(os.path.join(cwd, t)) if _UNKNOWN not in t else _UNKNOWN_CWD
        return False
    if cmd == "popd":
        st["cwd"] = _UNKNOWN_CWD
        return False
    if cmd in _SHELLS or cmd == "eval":
        code = " ".join(args) if cmd == "eval" else next(
            (args[k + 1] for k, a in enumerate(args[:-1]) if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", a)), None)
        return bool(code) and bash_writes_agent_definition(code, cwd, depth + 1)
    if _INTERP.match(cmd):
        code = _opt_values(args, ("-c", "-e", "-E", "-r"), ("--eval", "--command", "--print"))
        if cmd.startswith("perl") and any(re.match(r"^-[a-zA-Z]*i", a) for a in args) \
                and any(agent(o) for o in ops):
            return True
        return any(_code_writes(_expand_vars(c, env), cwd) for c in code)
    if is_agent_definition(cwd) and _writes_into_cwd(cmd, args):
        return True                                          # its implicit targets land in the agents directory
    if cmd in _ALL_OPERANDS:
        if any(agent(o) for o in ops):
            return True
        if cmd in ("mv", "gmv") and ops and _is_claude_dir(ops[-1], expand, cwd):
            return True                                      # a directory moved in as .claude brings agents/
        return False
    if cmd in _DEST:
        ops = _operands(args, {"-t", "-S", "-e", "-m", "-o", "-g", "-B", "--target-directory", "--suffix"})
        if cmd in ("install", "ginstall") and "-d" in args:
            return any(agent(o) for o in ops)
        dests = _opt_values(args, ("-t",), ("--target-directory",)) + (ops[-1:] if len(ops) > 1 else [])
        if any(agent(d) for d in dests):
            return True
        recursive = cmd in ("rsync", "ditto", "scp") or any(
            a in ("--recursive", "--archive") or re.match(r"^-[a-zA-Z]*[rRa]", a) for a in args)
        return recursive and any(_is_claude_dir(d, expand, cwd) for d in dests)
    if cmd == "dd":
        return any(a.startswith("of=") and agent(a[3:]) for a in args)
    if cmd in ("sed", "gsed"):
        return any(a.startswith(("-i", "--in-place")) or re.match(r"^-[a-zA-Z]*i", a) for a in args) \
            and any(agent(o) for o in ops)
    if cmd in _EDITORS:
        return any(agent(o) for o in ops)
    if cmd == "curl":
        return any(agent(v) for v in _opt_values(args, ("-o",), ("--output",)))
    if cmd == "wget":
        return any(agent(v) for v in _opt_values(args, ("-O", "-P"), ("--output-document", "--directory-prefix")))
    if cmd in ("tar", "bsdtar", "gtar"):
        vals = _opt_values(args, ("-C",), ("--directory", "--file"))
        vals += [args[k + 1] for k, a in enumerate(args[:-1]) if re.fullmatch(r"-?(?=[A-Za-z]*[cru])[A-Za-z]*f", a)]
        return any(agent(v) for v in vals)
    if cmd == "unzip":
        return any(agent(v) for v in _opt_values(args, ("-d",)))
    if cmd == "git":
        here = next(iter(_opt_values(args, ("-C",))), None)
        here = os.path.normpath(os.path.join(cwd, os.path.expanduser(_expand_vars(here, env)))) if here else cwd
        sub = next((o for o in _operands(args, {"-C", "-c"}) if o in _GIT_WRITE), None)
        return sub is not None and any(agent(o, here) for o in _operands(args, {"-C", "-c"}))
    if cmd in ("find", "gfind"):
        starts = []
        for a in args:
            if a.startswith(("-", "(", "!")):
                break
            starts.append(a)
        starts = starts or ["."]
        if "-delete" in args and any(agent(x) or _is_claude_dir(x, expand, cwd) for x in starts):
            return True
        for k, a in enumerate(args):
            if a in ("-fprint", "-fprintf", "-fls", "-fprint0") and k + 1 < len(args) and agent(args[k + 1]):
                return True
            if a in ("-exec", "-execdir", "-ok", "-okdir"):
                seg = []
                for b in args[k + 1:]:
                    if b in (";", "+"):
                        break
                    seg.append(b)
                for x in starts:                             # `{}`: any entry under the start path
                    sub = [b.replace("{}", x.rstrip("/") + "/*") for b in seg]
                    if _simple_writes(sub, [], {"cwd": cwd, "env": dict(env)}, depth):
                        return True
        return False
    return False


def _writes_into_cwd(cmd, args):
    """Does the command write into the current directory with no operand naming the target?"""
    if cmd in _CWD_WRITERS:
        return True
    if cmd == "curl":
        return any(a in ("--remote-name", "--remote-name-all") or re.match(r"^-[a-zA-Z]*[OJ]", a) for a in args)
    if cmd in ("tar", "bsdtar", "gtar"):
        return bool(args) and (args[0].lstrip("-").isalpha() and "x" in args[0]
                               or any(a in ("-x", "--extract", "--get") for a in args))
    if cmd in ("ditto", "pax"):
        return any(a in ("-x", "-r") for a in args)
    if cmd == "git":
        return any(o in _GIT_WRITE for o in _operands(args, {"-C", "-c"}))
    return False


def _is_claude_dir(word, expand, cwd):
    """Is the target a `.claude` directory itself (whose `agents/` a recursive copy or move would create)?"""
    for x in expand(word):
        p = os.path.expanduser(x)
        p = os.path.normpath(os.path.join(cwd, p))
        if _component_is(os.path.basename(p), ".claude"):
            return True
    return False


# ---- other MCP servers' tools
_MCP_READ = re.compile(r"^(?:read|get|list|search|find|view|show|stat|describe|query|directory_tree|tree|glob|grep|"
                       r"head|tail|cat|ls|count|exists|inspect|lookup|preview|fetch|start_search|stop_search|diff|"
                       r"compare|whoami|info|status|help|check)(?:_|$)")
_MCP_COMMAND_KEYS = {"command", "cmd", "shell", "commands"}         # shell command lines
_MCP_CODE_KEYS = {"script", "input", "code"}                         # a script or REPL input: shell or code


def _mcp_is_read(tool):
    name = tool.split("__")[-1]
    name = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).replace("-", "_").lower()
    return bool(_MCP_READ.match(name))


def _strings(obj, key=None, depth=0):
    if depth > 6:
        return
    if isinstance(obj, str):
        yield key, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, str(k).lower(), depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v, key, depth + 1)


def mcp_writes_agent_definition(tool, tool_input, cwd=None):
    """Another MCP server's tool: a write operation (any tool not named like a read: read_*, get_*, list_*,
    search_*, ...) with a path value inside `.claude/agents`, or a command value that writes there."""
    if _mcp_is_read(tool):
        return False
    for key, val in _strings(tool_input):
        if key in _MCP_COMMAND_KEYS or key in _MCP_CODE_KEYS:     # not a path: what does it write?
            if bash_writes_agent_definition(val, cwd) or (key in _MCP_CODE_KEYS and _code_writes(val, cwd)):
                return True
        elif is_agent_definition(val, cwd):
            return True
    return False


SUBAGENT_DENIED = ("Only the main session talks to beacon: a subagent does not call the beacon tools. Return what "
                   "you found (with the raw output) to the main session, which records it in the ledger.")


def guard(tool, tool_input, roots, cfg, agent_id=None, cwd=None):
    """Return a denial reason, or None to let the tool run.

    agent_id: the PreToolUse input's `agent_id`, present (non-empty) only for a subagent's call. A subagent
    inherits the beacon MCP tools, so "only the main session talks to beacon" is enforced here: every
    mcp__beacon__ call of a subagent is denied. The main session has no agent_id (a session started with
    an --agent option carries agent_type only), so it is never affected.
    cwd: the session's working directory (the hook input's `cwd`), for relative paths; default this process's."""
    ti = tool_input or {}
    if agent_id and tool.startswith(OWN_TOOL_PREFIX):
        return SUBAGENT_DENIED
    if tool in WEB_TOOLS and cfg.get("no_web"):
        return "Web tools are disabled for this beacon run (no_web)."
    if tool in TASK_TOOLS and cfg.get("block_internal_tasks", True):
        return ("During a beacon run the ledger is the task list: use add_node / start_node / set_status "
                "instead of the internal task tools.")
    if tool in SCHEDULE_TOOLS:
        return ("Scheduled prompts are disabled during a beacon run (the Stop gate keeps the session going; "
                "use mark_waiting for a job that needs time).")
    if tool in WRITE_TOOLS:
        for key in ("file_path", "notebook_path", "path"):
            p = ti.get(key)
            if not p:
                continue
            if _under(p, roots):
                return "That path belongs to the beacon supervisor; change the ledger only through the beacon tools."
            base = os.path.basename(p)
            if base == ".mcp.json" or (base in PROTECTED_NAMES + ("beacon",) and
                                        os.path.basename(os.path.dirname(os.path.abspath(p))) == ".claude"):
                return "Claude Code settings and MCP configuration are off-limits during a beacon run."
            if is_agent_definition(p, cwd):
                return AGENTS_DENIED
    if tool.startswith("mcp__") and not tool.startswith(OWN_TOOL_PREFIX):
        # another MCP server's tool (a filesystem or shell server) pointed at the supervisor's files
        if mcp_writes_agent_definition(tool, ti, cwd):
            return AGENTS_DENIED
        blob = json.dumps(ti, ensure_ascii=False)
        needles = {"ledger.db"} | {v for r in roots for v in _variants(r)}
        hit = next((n for n in needles if n and n in blob), None)
        if hit:
            return f"This tool call touches the beacon supervisor ({hit!r}). Use the beacon tools instead."
    if tool == "Bash":
        cmd = str(ti.get("command", ""))
        if HUMAN_CLI.search(cmd):
            return HUMAN_ONLY_MESSAGE
        if NESTED_CLAUDE.search(cmd) or _claude_in_command_position(cmd):
            return ("Starting another Claude Code session is disabled during a beacon run; use the Agent tool "
                    "for a subagent.")
        if bash_writes_agent_definition(cmd, cwd):
            return AGENTS_DENIED
        needles = {"BEACON_RUN", "ledger.db", ".claude/settings", ".mcp.json", "beacon-setup.json"}
        for r in roots:
            needles |= _variants(r)
        hit = next((n for n in needles if n and n in cmd), None)
        if hit:
            return (f"This command touches the beacon supervisor ({hit!r}). Use the beacon tools "
                    "(ledger_view, attach_evidence, set_status, ...) instead.")
    return None


def pre_tool_use(run, inp):
    cfg = {}
    try:  # the path rules do not depend on the ledger: a broken ledger must not disarm them
        L = Ledger.open(run)
        try:
            cfg = L.config()
        finally:
            L.close()
    except Exception:  # noqa: BLE001
        _log_error(run, "pre-tool-use (config unreadable; path rules still applied)")
    agent_id = inp.get("agent_id")
    reason = guard(str(inp.get("tool_name", "")), inp.get("tool_input") or {}, protected_paths(run), cfg,
                   agent_id=agent_id if isinstance(agent_id, str) and agent_id.strip() else None,
                   cwd=inp.get("cwd") if isinstance(inp.get("cwd"), str) and inp.get("cwd") else None)
    if not reason:
        return None
    _append_jsonl(run, "denials.jsonl", {"ts": time.time(), "tool": inp.get("tool_name"), "reason": reason,
                                         "input": str(inp.get("tool_input"))[:300]})
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason},
            "systemMessage": f"beacon guard ▸ blocked {inp.get('tool_name')}: {reason}"[:400]}


# ---------------------------------------------------------------- PostToolUse
def post_tool_use(run, inp):
    """Append a hash of the tool output to <run>/activity.jsonl (no ledger write: this
    runs after every tool call and must stay cheap). The gate reads it only to tell
    "working without recording" from "idle"."""
    from .gate import activity_line
    tool = str(inp.get("tool_name", ""))
    if tool.startswith(OWN_TOOL_PREFIX):
        # a beacon tool changed the ledger: keep the live graph current (only if it was opened once)
        if os.path.exists(os.path.join(run_dir(run), "graph.html")):
            L = Ledger.open(run)
            try:
                refresh_if_open(L)
            finally:
                L.close()
        return None
    line = activity_line(tool, inp.get("tool_response"))
    line["ts"] = time.time()
    _append_jsonl(run, "activity.jsonl", line)
    return None


# ---------------------------------------------------------------- UserPromptSubmit
def _browser():
    """`beacon: graph` opens the page in the browser of the machine the hooks run on."""
    if os.environ.get("BEACON_NO_BROWSER"):
        return None
    import webbrowser
    return webbrowser.open
def _waiting_for_human(run, session):
    """True if this session's last stop was let through because Claude waits on the human."""
    try:
        with open(os.path.join(run_dir(run), "decisions.jsonl"), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("session") == session:
            return d.get("action") == "allow" and d.get("why") == "awaiting_human"
    return False


def user_prompt_submit(run, inp):
    """`beacon: <command>` and replies to open questions, typed in the chat (see beacon.chat)."""
    if os.environ.get("BEACON_WATCHDOG") == "1":   # under `beacon watch` the prompts are the watchdog's
        return None
    from .chat import handle
    _remember_session(run, inp)
    L = Ledger.open(run)
    try:
        if not L.verify_integrity()[0]:
            return None
        r = handle(L, inp.get("prompt") or "", _waiting_for_human(run, inp.get("session_id")), opener=_browser())
        if r and r.get("changed"):
            refresh_if_open(L)
    finally:
        L.close()
    if r is None:
        return None
    _append_jsonl(run, "chat.jsonl", {"ts": time.time(), "session": inp.get("session_id"),
                                      "prompt": (inp.get("prompt") or "")[:500], "block": r["block"],
                                      "show": r["show"][:2000]})
    if r["block"]:
        return {"decision": "block", "reason": r["show"]}
    out = {"systemMessage": r["show"][:600]}
    if r.get("note"):
        out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit",
                                     "additionalContext": "[beacon] " + r["note"][:4000]}
    return out


# ---------------------------------------------------------------- StopFailure
def stop_failure(run, inp):
    _append_jsonl(run, "failures.jsonl", {
        "ts": time.time(), "session": inp.get("session_id"),
        "error_type": inp.get("error_type"), "error": inp.get("error"),
        "message": (inp.get("error_message") or "")[:500]})
    L = Ledger.open(run)
    try:
        if L.verify_integrity()[0]:
            _notify(L, "stop_failure:" + str(inp.get("error_type") or inp.get("error") or "unknown"))
    finally:
        L.close()
    return None  # output and exit code are ignored for StopFailure


UNSUPERVISED = ("beacon is set up in this project, but this session was started without BEACON_RUN, so beacon is "
                "NOT supervising it (no ledger tools, no stop checks). To supervise it: exit, then run "
                "`BEACON_RUN=<run dir> claude --continue`.")

HANDLERS = {"stop": stop, "session-start": session_start, "pre-tool-use": pre_tool_use,
            "post-tool-use": post_tool_use, "stop-failure": stop_failure,
            "user-prompt-submit": user_prompt_submit}


def _tree_for_start(L, limit=6000):
    tree = L.render_tree(max_evidence=2)
    if len(tree) <= limit:
        return tree
    return tree[:limit] + f"\n… (cut, {len(tree) - limit} more chars: ledger_view shows the whole ledger)"


def _log_error(run, event):
    try:
        with open(os.path.join(run_dir(run), "hook-errors.log"), "a", encoding="utf-8") as fh:
            fh.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {event}\n{traceback.format_exc()}\n")
    except OSError:
        pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    event = argv[0] if argv else ""
    try:
        # Claude Code sends UTF-8. Read bytes: on Windows the default text encoding is the ANSI code page
        # (cp1252, ...), which garbles a Vietnamese chat command or a non-ASCII path.
        data = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read()
        raw = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    except Exception:  # noqa: BLE001
        raw = ""
    run = os.environ.get("BEACON_RUN", "").strip()
    if (not run or run.startswith("${")) and event == "session-start":
        # The hooks are installed in this project but this session is unsupervised. Say so to the human once,
        # at the start: otherwise it shows up later as broken beacon tools (measured 2026-11-12, a restart
        # with `claude --continue` that dropped BEACON_RUN).
        sys.stdout.write(json.dumps({"systemMessage": UNSUPERVISED}, ensure_ascii=True))
        sys.stdout.flush()
        return 0
    if not run or event not in HANDLERS:
        return 0
    try:
        inp = json.loads(raw) if raw.strip() else {}
        if not isinstance(inp, dict):
            inp = {}
        out = HANDLERS[event](run, inp)
        if out:
            # Pure ASCII JSON (non-ASCII as \uXXXX): whatever the console or pipe encoding, nothing to garble
            # and nothing that cannot be encoded (a UnicodeEncodeError here would fail open and let the stop pass).
            sys.stdout.write(json.dumps(out, ensure_ascii=True))
            sys.stdout.flush()
    except Exception:  # noqa: BLE001  -- fail-open, see module docstring
        _log_error(run, event)
    return 0


if __name__ == "__main__":
    sys.exit(main())
