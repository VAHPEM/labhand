# beacon — keep Claude Code working, track what is left, light the way when it stalls

`labhand` is the Muscle: it runs things on the two VMs and never interprets.
`beacon` is a Brain-side harness around Claude Code (the worker) that fixes three
failure modes seen in practice:

1. the worker gives up shortly before it would have finished;
2. it notices promising new directions mid-task and loses them;
3. it runs into a dead end and reports back instead of re-examining its assumptions.

Standard library only (`sqlite3`, `urllib`): the hooks run under a plain `python3`
without a virtualenv. Only the worker-facing MCP server needs the `mcp` package.

## Pieces

| Module | Role |
|---|---|
| `ledger.py` | SQLite ledger: problems (from the checklist) → solutions (hypotheses) → approaches, evidence (de-duplicated by content hash), questions for the human, and a hash-chained event log with a state digest (tamper-evident). Enforces the rules: no deletion; `done` needs own evidence; `dead_end` needs reason **and** assumption; a problem is done only through verification. |
| `gate.py` | The Stop decision. Reads the ledger, not the worker's prose. Escalation: nudge → lighthouse → stop with a report. Budgets (soft, hard, one near-done extension), stall/idle counters, waiting jobs, refusals routed to the human. |
| `lighthouse.py` | Fresh-context reviewer via the Messages API (no tools, no transcript). Answers: which dead ends rest on a doubtful assumption, which approach classes are missing, which done claims are weak. Only proposes; vague or duplicate suggestions are dropped. |
| `hooks.py` | Claude Code hook entry points: `stop`, `session-start`, `pre-tool-use` (guards the ledger, settings, internal task tools, the human's CLI commands, scheduled prompts, nested `claude`, beacon's own MCP tools in a subagent, optional no-web), `post-tool-use` (activity hash), `stop-failure` (log + notify), `user-prompt-submit` (chat commands). Each shows a one-line `beacon ▸ …` message in the chat. Fail-open. |
| `chat.py` | `beacon: …` commands typed in the Claude Code chat (`beacon: help` lists them); `#N <text>` answers question N. |
| `board.py` | The visual graph: `<run>/graph.html`, self-contained (no CDN), statuses as colours + symbols, details per node, what waits on you, recent stops; rewritten by the hooks after each change once opened, and reloads itself while "live" is ticked. |
| `logview.py` | `beacon log` (each stop: what Claude Code said + the exact reply beacon sent) and `beacon transcript` (Claude Code's own session log, readable), both with `--follow`. |
| `tools.py`, `mcp_server.py` | The worker's write path: `ledger_view`, `problem_note`, `add_node`, `start_node`, `attach_evidence`, `set_status`, `propose_done`, `claim_near_done`, `ask`, `mark_waiting`. |
| `cli.py` | The human's side: `init`, `status`, `report`, `graph`, `check`, `answer`, `verify`, `reopen`, `config`, `hooks-config`, `lighthouse`, `log`, `transcript`, `claude`, `watch`, `doctor`. `answer`/`verify`/`reopen`/`config KEY=VAL`/`lighthouse` need a terminal (stdin a TTY). |
| `report.py` | `REPORT.md` (near-done first, then what needs you) and a mermaid/HTML graph. |
| `decider.py` | At a branch (`ask(kind="approach_choice")`), a separate model (Opus 5.5) ranks the options from the problem's ledger, evidence, the human's answers, the worker's brief and the session's observations (tool calls + outputs; `decider_transcript=full` adds the worker's prose). Its ranking orders the nodes; its reasons go into the ledger. Falls back to the worker's order without a key / over budget / on refusal or timeout. |
| `classify.py` | A question the worker left as prose at a stop is read by Opus (`classifier_model`) with the problem's ledger and the session's observations: a choice is made on the spot, a request to run something is approved by your standing rule, only information you alone have comes to you; when the worker looks stuck it adds a step-back note. No key / over budget = keyword heuristic. |
| `project.py` | `beacon setup-project DIR`: merges the hooks, an allow rule for the beacon MCP tools, the MCP server entry and the worker rules into a project (backups as `*.beacon-bak`, removed again by teardown once the file is back to them or their content is committed in `HEAD`, the others kept and named; never installs the hooks twice across `settings.json` / `settings.local.json`); `beacon teardown-project DIR` removes exactly that again. |
| `vmstate.py` | `vm_check` tool: fingerprints a VM through labhand (processes, listening ports, samples/ digest without labhand's `_*` files), keeps a baseline in the ledger and attaches each check as evidence, so a dead end shows whether the VM was clean. |
| `evalkit.py` | `beacon eval`: runs a suite under `baseline` / `gate` / `gate_lh` / `gate_lh_alt` (lighthouse on another Claude model), isolated copies, web denied, success decided by the task's verify command; `results.jsonl` (resumable) + `summary.md`. |
| `demo.py` | `beacon demo`: a toy practice run (three puzzles, `check.py`, `TESTING.md`) and `--auto`, a live smoke test of the whole cycle with the real Claude Code. |
| `doctor.py` | `beacon doctor [--project DIR] [--run RUN]`: python, claude CLI version, hooks installed exactly once, MCP python imports `mcp`, worker rules, API key presence, labhand importable. OK / WARN / FAIL, exit 1 on FAIL. |
| `watchdog.py` | `beacon watch`: runs the worker headless (`claude -p … --output-format stream-json`), waits out rate limits (until the announced reset, else backoff) and resumes, stops on auth/billing errors and on any final gate decision, starts a fresh session when the gate rotates (context rot). |

## Quick start

```bash
cd ~/labhand
cat > /tmp/fo13.md <<'EOF'
# Flare-On 13 practice
- [ ] Challenge 5: recover the flag
  verify: python3 check5.py
  note: Rust PE, packed
- [ ] Challenge 6: triage the crash in the parser
EOF
# verify: exit 0 = accepted; the candidate arrives in $BEACON_CANDIDATE (never on the command line)
# note: the worker sees a note whole in ledger_view up to 4000 characters; a longer one is shown cut with
# "(cut, N more chars)" and read in full with the problem_note tool (`beacon init` warns about it)
python3 -m beacon init ~/beacon-runs/fo13 --checklist /tmp/fo13.md
python3 -m beacon setup-project ~/ctf/fo13 --mcp-python ~/venvs/labhand/bin/python   # hooks, MCP, rules
python3 -m beacon claude --run ~/beacon-runs/fo13   # interactive (= BEACON_RUN=… claude); `beacon watch` unattended
```

### Verify commands

`propose_done` runs the problem's `verify:` command at once. On a rejection the worker gets the
exit code and the last 40 lines of the command's output (stdout and stderr together, in the order
they were written). It does **not** see the command itself by default, since a CTF check may hold
the answer. For an engineering run, where the command is just a test, show it too:
`beacon init RUN --checklist … --set verify_visibility=command` (or later,
`beacon config verify_visibility=command`). The command then appears in `ledger_view` and on
each rejection.

Make each step print why it fails. Otherwise the worker sees only an exit code and cannot tell
which check said no:

```bash
  verify: ! grep -rn "import dualvm" . || { echo "P1: dualvm import left"; exit 1; }; python3 -m pytest tests -q || { echo "P1: tests red"; exit 1; }
```

If a verify command turns out wrong, see [Getting past a wrong verify command](#getting-past-a-wrong-verify-command).

### Getting past a wrong verify command

A problem with a `verify:` command never waits in `pending_verification`: `propose_done` runs the
command at once, so a wrong command rejects correct work for ever and plain `beacon verify P1` has
nothing to accept. Two ways out, both only for you:

- `beacon verify P1 --force --note "the check greps the wrong directory"` (terminal), or
  `beacon: verify P1 force the check greps the wrong directory` (chat): P1 is done, from
  in_progress, blocked or pending_verification. The reason is required.
- `beacon set-verify P1 'python3 -m pytest tests -q'` (terminal): replaces the command; it runs on
  the worker's next `propose_done`.

Both are recorded as the human (actor `human` in the hash chain). The terminal forms need a TTY and
the PreToolUse guard refuses them from the worker's Bash; no MCP tool reaches them.

### Removing beacon from a project

`beacon teardown-project DIR` is the inverse of `setup-project`. It removes:

- the worker-rules block between its markers in `CLAUDE.md`;
- the hooks, allow rules (`mcp__beacon`, `mcp__labhand`) and deny rules `setup-project` added to
  `.claude/settings.json` / `.claude/settings.local.json`, the `respondToBashCommands` it set, and the
  `mcp__beacon__*` approvals and `enabledMcpjsonServers` entry the run added;
- the beacon server in `.mcp.json`, when setup added it;
- `.claude/beacon`, when it is beacon's own command (by content); any other file there is the
  project's and stays untouched (one setup replaced is put back from its backup);
- `.claude/beacon-setup.json`, setup's record (last, so an interrupted teardown can be run again).

**What you had stays.** Setup records in `.claude/beacon-setup.json` what it added because it was
absent: per settings file the hook events, allow / deny rules and keys, whether `beacon` was already in
`enabledMcpjsonServers` and which `mcp__beacon__*` approvals already existed; for `.mcp.json` whether it
added the beacon server or replaced yours. It writes the record before any wired file, so an
interrupted setup leaves a record that covers whatever it got to; a re-run (an interrupted run, a moved
checkout) keeps what the earlier record says and adds only what is new. An entry whose file no longer
holds everything it lists is stale (an interrupted setup that never reached the file, an interrupted
teardown that already stripped it) and is replaced by what this setup finds: a rule you added since
stays yours. The cost of that rule: if one of beacon's parts was removed by hand during a run and setup
is run again, the beacon parts still there count as yours and teardown leaves them (rules left
behind, never a rule of yours removed). Teardown removes exactly what
the record lists: a deny rule such as `Edit(~/.claude/agents/**)` or `Bash(sqlite3 *)`, an allow rule, a
beacon hook or a beacon MCP server you had before setup stays, and a beacon hook or server entry setup
rewrote (paths may have moved) gets your old value back; teardown names each part it kept ("kept denied
… (it was there before setup)"). A project wired by a setup older than the record (no record, the wired
file still there) is judged from that setup's `.beacon-bak` (its pre-run state) on the next setup; with
no record at teardown either, every beacon part is removed as before. A record that is not one is an
error to fix by hand, never guessed around. The record is protected like the settings files (deny rule
`Edit(**/.claude/beacon-setup.json)`, and the guard refuses Write/Edit/Bash on it).

Everything else stays. A file setup created that is left with nothing in it is removed; a file that
held only an empty object or empty text before setup is put back. A file that is back to its
pre-setup state is restored byte for byte from `setup-project`'s `<file>.beacon-bak`, and that backup
is then removed: after a run that changed nothing, no `*.beacon-bak` is left. A backup is removed only
when the final file equals it (bytes and permission bits; for a symlink, the target's content). Any
other backup (the file was edited during the run, or could not be restored) is kept, and teardown names
it and says why. A file `setup-project` created and the run edited is backed up before teardown rewrites
it, and that backup is kept the same way. Every kept backup's message says what it holds: the file as
it was before the run, or teardown's own copy taken before it removed beacon's parts. The older backups
setup moved aside (`<file>.beacon-bak.old[.N]`) are settled too: one whose bytes the final file has is
removed, every other one is kept and named; teardown's last line lists exactly the backups left behind.

A backup (or `.old`) whose bytes equal the file's version in the repository's `HEAD` commit is
redundant as well and is removed with the reason "committed in HEAD": a run that legitimately edits a
tracked `CLAUDE.md` leaves no backup behind, since the pre-run content is in git. Teardown reads it with
`git ls-tree` / `git cat-file` (10 s timeout, inherited `GIT_*` variables dropped) for the path git
tracks (for a symlink, its target's path), only as a regular file in `HEAD`; the index (staged changes)
never counts. Not a git work tree, git missing, an untracked file, no commit yet or any git error: the
check does not apply and the rules above decide. A backup that differs from both the final file and
`HEAD` is always kept and named, and `--dry-run` reports the same verdicts without touching anything.

`setup-project` makes sure the backup is this run's pre-run state: a `<file>.beacon-bak` already there
that differs from the (unwired) file is from an older run; it is moved aside to a free
`<file>.beacon-bak.old` (`.old.1`, ...; never deleted, never overwritten) and a fresh backup is taken.
A backup equal to the file is reused; a file still wired (an interrupted run) keeps its backup.
A `.claude/beacon.beacon-bak` that holds a beacon wrapper (left by an older version) is not a copy of
anything the project owns: setup and teardown remove it, and it never stands in for the project's file.
An `mcp__labhand` allow rule `setup-project --allow-labhand` added is removed; one the project had
before stays (without a record: kept and reported, since it may have been the project's). `--dry-run`
shows what would change. The real run needs a terminal and is refused from the worker's Bash.

### Running beacon on a repo that contains beacon

When the worker's project is this repo (or any checkout with a `beacon/` package), the worker edits
the very code that supervises it. Worse, Python imports that code: Claude Code runs the hooks with the
project as the working directory, and `python -m beacon.hooks` puts the working directory first on
`sys.path`. So the hooks would load the project's `beacon/` instead of the copy you installed. Run
the supervisor from a frozen copy, with `python -P` (Python ≥ 3.11), which leaves the working
directory off `sys.path`. This is how the labhand run was supervised:

```bash
rsync -a --exclude .git --exclude __pycache__ ~/labhand/ ~/beacon-frozen/     # a snapshot, not a checkout
printf '#!/bin/sh\nexec ~/venvs/lab/bin/python -P "$@"\n' > ~/beacon-frozen/py   # a python with `mcp`
chmod +x ~/beacon-frozen/py
PYTHONPATH=~/beacon-frozen ~/beacon-frozen/py -m beacon setup-project ~/labhand \
    --python ~/beacon-frozen/py --mcp-python ~/beacon-frozen/py
PYTHONPATH=~/beacon-frozen ~/beacon-frozen/py -m beacon init ~/beacon-runs/labhand --checklist ck.md
cd ~/labhand && .claude/beacon claude --run ~/beacon-runs/labhand
```

Run `setup-project` from the frozen copy, so the hook commands, the MCP server and `.claude/beacon`
all point at `~/beacon-frozen` through `py`. Refresh the copy only between runs. Afterwards,
`PYTHONPATH=~/beacon-frozen ~/beacon-frozen/py -m beacon teardown-project ~/labhand` unwires the
repo.

## Watching it — in the Claude Code chat or in a terminal

**In the chat (interactive `beacon claude`).** Every hook returns a one-line `systemMessage`,
which Claude Code shows in the conversation:
- `beacon ▸ run "…" supervises this session: 2/3 problem(s) open …` at session start;
- `beacon ▸ kept the session going (stop 4, forced 3/40); next: P1.A2 …; lighthouse queued 2 idea(s)`
  each time the gate refuses a stop; the full text sent to Claude also appears in the chat as the
  Stop hook's block message (Claude Code labels a block as a hook error);
- `beacon ▸ let the session stop: awaiting_human …` followed by what waits on you (candidates,
  open questions) and how to answer;
- `beacon guard ▸ blocked Bash: …` when the guard refuses a tool call.

Type to beacon in the same chat box (the UserPromptSubmit hook reads it before Claude does).
`beacon: help` prints this list:

| you type | effect |
|---|---|
| **Look** — answered by beacon alone: the prompt stops there, no model turn, no tokens | |
| `beacon: status` | the whole tree with statuses, the gate's counters, PAUSED if paused |
| `beacon: show P1.A2` | one node: why it exists, its result and assumption, evidence, children |
| `beacon: graph` | writes `<run>/graph.html` and opens it in the browser (live from then on) |
| `beacon: log [N]` | the last N stops: what Claude said, what beacon replied |
| `beacon: questions` / `beacon: report` / `beacon: help` | open questions / the report / the list |
| `beacon: decisions` | the decider's latest branch decisions: order, why, first step, when to switch |
| **Decide** — recorded as yours (actor `human` in the hash chain), then passed on to Claude with a note, so it carries on | |
| `beacon: answer 3 use the arm64 image`, or `#3 use the arm64 image` | answer question 3 (a plain reply also counts when Claude stopped to wait for you and exactly one question is open; beacon says so). With several open, only `#N <answer>` closes one: a plain reply reaches Claude but closes nothing, and beacon lists which are still open |
| `beacon: verify P1` / `beacon: verify P1 reject wrong format` | accept / reject a candidate; an unclear word is refused, never read as "accept" |
| `beacon: verify P1 force <why>` | declare P1 done past a wrong verify command (see below); the reason is required |
| `beacon: add P1 <idea>` / `beacon: add P1.S1 approach <idea>` | your own hypothesis / approach, high priority, marked "● you" |
| `beacon: reopen P1.A2 [note]` | reopen a closed node |
| `beacon: abandon P2 <why>` | drop a problem or node |
| `beacon: pick P1.A3` | choose which approach goes next, over Claude's order ("★ your pick" on the graph) |
| `beacon: accept P4` / `beacon: reject P4 <why>` | a problem Claude proposed joins the run / is turned down |
| `beacon: lighthouse [note]` | a lighthouse review at Claude's next stop, with your note; outside the lighthouse budget (it is your call) |
| **Steer** | |
| `beacon: pause` | beacon lets every stop through and pushes nothing (not sent to Claude) |
| `beacon: resume` | beacon pushes again; the note tells Claude the next node |

**The visual graph** (`beacon: graph` in the chat, `python3 -m beacon graph --run RUN` in a
terminal): one card per problem with a progress bar, the tree of hypotheses and approaches,
each node's status as colour + symbol + word (○ todo · ◐ in progress · ◎ awaiting your
verification · ✓ done · ✕ dead end · ‖ blocked · ↷ superseded · – abandoned), ✦ for the
lighthouse's ideas, ● for yours, "▶ next" on the node the gate points Claude at. Click a node
for its details; the side panel lists what waits on you and the recent stops (what Claude said,
what beacon replied). Filter chips per status, "hide closed", a text filter. The file is rewritten
by the hooks after every stop, beacon tool call and chat command once it exists, and the page
reloads every 5 s while "live" is ticked (its state is kept in the URL). `--format mermaid`
still prints a mermaid flowchart for a markdown file.

`setup-project` also writes `.claude/beacon`, so shell mode works in the chat too:
`! .claude/beacon log -n 5`, `! .claude/beacon transcript -n 40`, `! .claude/beacon status`
(shell-mode output enters the conversation and Claude replies to it unless you set up the
project with `--quiet-shell`, i.e. `respondToBashCommands: false`). Prefer `beacon: …`.

**In a terminal (any mode, also `beacon watch`):**
```bash
python3 -m beacon log -f --run ~/beacon-runs/fo13         # each stop: what Claude said / what beacon replied, live
python3 -m beacon transcript -f --run ~/beacon-runs/fo13  # Claude Code's own log: prompts, text, every tool call + result
python3 -m beacon transcript --list --run ~/beacon-runs/fo13   # sessions of the run (interactive or watch)
```
`--full` disables shortening. The files behind them, all in the run directory:
`outputs.jsonl` (Claude's last message at each stop), `decisions.jsonl` (verdict, the exact
reply, lighthouse result, report path), `sessions.jsonl` (each session's Claude Code
transcript path), `sessions/` (raw stream-json of `beacon watch` sessions, prompt included),
`chat.jsonl` (your `beacon:` commands), `denials.jsonl`, `failures.jsonl`, `REPORT.md`.

After the first real runs, `beacon calibrate-refusals` lists what the refusal heuristic
flagged and the near misses, to check it against real Claude Code output. When a run stops:
read `REPORT.md`, answer and verify (chat or terminal), relaunch with the same run — the
SessionStart hook re-injects the ledger.

Claude API key for the lighthouse, the classifier and the decider: put **`BEACON_API_KEY`** (or
`BEACON_API_KEY_FILE`) in the environment you launch Claude Code from. `ANTHROPIC_API_KEY` is not
enough: Claude Code removes Anthropic credentials from the environment of hooks and MCP servers
whenever it scrubs subprocess credentials (`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`, GitHub Actions,
Claude's cloud sessions; measured on 2.1.322: blank in a cloud sandbox, passed through on a plain
Linux machine, `test_e2e_claude.py`), a subscription login has none, and with it set Claude Code
itself bills the API instead of your subscription. `BEACON_API_KEY` works in every case. Without a key, a stall goes straight to you, a branch keeps the worker's
order. Models and budgets: `python3 -m beacon config lighthouse_model=claude-sonnet-5-5`,
`decider_model=…`, `max_decider_calls=…`.

## Testing it on your machine

Three levels, cheapest first:

```bash
# 1. unit tests: no Claude Code, no network (~5 s). The MCP tests skip without the `mcp` package.
python3 -m pytest tests/beacon -q

# 2. the real Claude Code CLI against a scripted, local Messages API (no model, no cost):
#    beacon's MCP tools, the headless runner, --resume, deny rules
BEACON_E2E_CLAUDE="$(which claude)" python3 -m pytest tests/beacon/test_e2e_claude.py -q -rs

# 3. the demo kit: three toy puzzles, a real run with the real model
python3 -m beacon demo ~/beacon-demo                  # builds it, prints the manual steps (TESTING.md)
~/beacon-demo/project/.claude/beacon demo ~/beacon-demo --auto    # live smoke test, headless
```

The demo kit (`demo.py`): P1 is a flag hidden by an XOR keystream in a Python file (checked by
`check.py`), P2 is a file encrypted with a passphrase that is not in the project (Claude must ask
you; `TESTING.md` holds it), P3 is a short write-up with no automatic check (you verify it).
`TESTING.md` walks through every feature in an interactive session — the chat lines, `beacon:`
commands, the graph, pause/resume, the guard, the lighthouse — with what you should see.

`--auto` runs Claude Code headless (`acceptEdits`, the kit's allow rules, `--max-budget-usd 1`
per session, Sonnet 5.5 unless `--model`), tells it to solve only P1 and stop, answers its
question as you, resumes, verifies P3 as you, and prints PASS / WARN / FAIL per step: hooks
installed and runnable as installed, the Stop gate deciding, a forced continuation, the MCP
tools used, the question asked and the session stopping for it, P2 solved with the answer, the
hash chain intact, the graph and logs. With an API key it also makes one lighthouse call, on a
copy of the ledger. Level 2 cannot show whether the hooks run on your machine (its isolated
HOME has no login, so Claude Code turns them off and the test says so); level 3 can.

## Measuring it: `beacon eval`

```bash
cat > suite.json <<'EOF2'
{"tasks": [{"id": "fo13-5", "title": "Recover the flag from challenge 5",
            "files": "~/ctf/fo13/ch5", "verify": "python3 check5.py", "notes": "Rust PE"}]}
EOF2
python3 -m beacon eval suite.json --out ~/beacon-eval/fo13 --reps 3 \
    --configs baseline,gate,gate_lh,gate_lh_alt --alt-lighthouse-model claude-sonnet-5-5 \
    --max-cost-per-run 15 --dry-run          # drop --dry-run to run it
```

Configurations are interleaved per task (seeded) so rate limits and load do not fall on one
of them. Prefer challenges from after the model's training data (Flare-On 13 rather than
older years) and keep web tools denied (the harness does), or the numbers measure recall
of write-ups.

## Permissions: running without prompts

Store the mode once per run and every launch uses it:

```bash
python3 -m beacon config --run ~/beacon-runs/fo13 permission_mode=bypassPermissions
python3 -m beacon claude --run ~/beacon-runs/fo13      # = BEACON_RUN=… claude --permission-mode bypassPermissions
python3 -m beacon watch  --run ~/beacon-runs/fo13 --cwd ~/ctf/fo13    # uses it too (an explicit flag wins)
```

Facts that shaped this (Claude Code docs, and measured on 2.1.322):
- `bypassPermissions` set in a project's `.claude/settings*.json` is ignored (the session starts
  in Manual mode), so it has to come from the launch command — hence `beacon claude`.
- Claude Code documents the mode for isolated containers and VMs and says it gives no
  protection against prompt injection. On the Mac host that means any Bash command runs
  without a question; the lab VMs are the safer place for anything that touches a sample.
- **Deny rules still block in bypassPermissions.** `setup-project` adds rules protecting the
  supervisor (`Edit(**/ledger.db*)`, `Bash(sqlite3 *)`, `Edit(**/.claude/settings*.json)`,
  `Edit(**/.mcp.json)`). Measured with the real CLI in `acceptEdits` and `bypassPermissions`:
  those were denied, ordinary writes and Bash commands ran (`test_e2e_claude.py`). They hold
  even in a session where the hooks do not run.
- beacon's own permission questions (the worker's `ask(need_permission)`, or "shall I run it on the
  VM?" left at a stop) are a separate layer from Claude Code's permission prompts. By your standing
  rule (`permission_policy=approve`, the default) they are approved at once and recorded as
  `auto_approved` in the ledger, running a binary included (your decision, 2026-11-13). To have a
  request to execute a **real malware sample** come to you instead, set
  `ask_before_running_real_samples=true` (off by default). The approval covers your own computer as
  well as the VMs, so say in the task where samples may run. `permission_policy=ask` sends every such
  request to you.

## Unattended runs: the watchdog

```bash
python3 -m beacon watch --run ~/beacon-runs/fo13 --cwd ~/ctf/fo13 --dry-run   # shows the exact command
python3 -m beacon watch --run ~/beacon-runs/fo13 --cwd ~/ctf/fo13          # runs until the gate says stop
```

- **No time or cost limit by default.** The run ends when the gate lets it (no open work, budget of forced
  continuations, waiting on you, ...), on a fatal error, or after `--max-sessions` (20) sessions. A
  rate-limit wait lasts until the reset Claude Code announces, up to 6 h (see "Usage limits" below). Each limit is still there if
  you want one: `--max-hours`, `--max-cost-usd`, `--session-budget-usd`, `--session-timeout-min`,
  `--max-wait-hours`. Ctrl-C stops the run cleanly (the session tree is killed, the report written).
  (`beacon eval` keeps per-run caps: equal budgets are part of comparing configurations.)

- Every session is logged under `<run>/sessions/` (raw stream-json + stderr); `watch.jsonl`
  has one line per session and wait. The run ends with `REPORT.md` and `notify_cmd`.
- **Permissions.** A headless run has nobody to answer prompts: with the default (Manual)
  mode, any tool not pre-approved by the project's `permissions.allow` rules is denied.
  Pre-approve what the worker legitimately needs (the labhand and beacon MCP tools, the
  Bash commands it uses) in the project's settings, or pass `--permission-mode`. The
  watchdog never adds a permissive mode on its own.
- **Auth.** The CLI uses your Claude Code login unless `ANTHROPIC_API_KEY` is set (the key
  wins). For long unattended automation an API key is the clean, policy-stable path; the
  watchdog prints a note when none is set.
- **Rotation.** Under the watchdog the gate ends a session after `rotate_every` (15) forced
  continuations; the next session starts fresh with the ledger injected by SessionStart.
- **Never `--bare`**: bare mode skips the project's hooks and `.mcp.json`, i.e. the supervisor.
  If two sessions end without any gate decision, the watchdog stops with `no_gate`.
- **Hooks need the organization's settings (measured, Claude Code 2.1.322).** At startup Claude
  Code reads your organization's settings from `api.anthropic.com/api/claude_code/settings`
  (a fixed host, not `ANTHROPIC_BASE_URL`). If it cannot, it runs the session with the user's
  hooks OFF ("Hooks not running: …") and denies every tool call a PreToolUse hook would have
  checked. The supervisor is then absent; the watchdog recognises Claude Code's notice and
  stops with `hooks_disabled` instead of looping. Normal network + a real login: not an issue.

### Usage limits: a wait, not a crash

When the subscription's limit is used up, Claude Code ends every session at once with a message like
`You've hit your 5-hour limit · resets 12pm (Asia/Saigon)` (`subtype: success`, `is_error: true`, cost $0;
measured 2026-11-16). The watchdog treats that as a pause:

- **Recognised** from the stream's own fields (the result's `api_error_status: 429` /
  `api_error_type: rate_limit_error`, the assistant message's `error: "rate_limit"`) and from the wording
  (`hit your … limit`, `usage limit`, `rate limit`, `limit reached`, `429`).
- **The reset** comes from the stream's `rate_limit_event.resetsAt` (exact) or else from the text:
  `resets 12pm (Asia/Saigon)`, `resets 3:30am (Europe/Paris)`, `resets Nov 18, 9am (Europe/Paris)`,
  `reset at 15:30`, `resets in 2h 15m`. A time in parentheses is read in that IANA time zone (otherwise
  this Mac's), DST included; without a date it is the next such time there. A time that passed less than
  10 minutes ago counts as just passed (clock skew), not as tomorrow.
- **The wait** lasts until the reset + 60 s, then the same session is resumed. Without a usable reset
  time: 5, 10, 20, 40 min, then hourly. `watch.jsonl` gets
  `{"event": "wait", "kind": "rate_limit", "seconds": …, "until": <epoch seconds>, "reset": "resets 12pm (Asia/Saigon)"}`
  (`reset` is `""` when nothing was announced). The wait sleeps in 5-minute steps against the clock, so a
  Mac that slept meanwhile does not stretch it, and it ends early if the run has no open problem left.
- **Not a failure.** A rate-limited session counts toward neither `too_many_failures` (`max_transient`),
  the crash backoff, nor `--max-sessions`.
- **Too far away.** A reset more than 6 h away (a weekly limit), or a limit without a reset time that
  has lasted 6 h, ends the run with `usage_limit`: `REPORT.md` ("Stopped because: watchdog:usage_limit")
  and a `watch_end` event in the ledger give the reset time and text. Start `beacon watch` again after it.
  `--max-wait-hours` still ends the run earlier (`wait_too_long`) if you set it.
- Ctrl-C or a SIGTERM during the wait ends the run as `interrupted`, with the report written.

## Design notes and limits (read these)

- **Session isolation.** Only sessions launched with `BEACON_RUN` are gated; hooks
  inherit Claude Code's environment. Other sessions in the same project pass through.
- **Fail-open.** A hook that crashes logs to `<run>/hook-errors.log` and allows the stop.
  A broken supervisor must not become an infinite loop.
- **The human's channel.** Answers, verification and reopening are the human's. Through
  Bash the worker meets a guard (the CLI's human commands, nested `claude`, scheduled prompts
  are refused), deny rules for `.claude/beacon answer|verify|…`, and the CLI's TTY check;
  the chat channel only sees prompts Claude Code submits for you (`beacon watch` skips it).
  These stop accidental or casual self-approval. They do not stop a determined process
  running as your user (it could fake a terminal or write the database with its own code):
  every human action is an event in the hash chain (`chat_command` / `chat_answer` mark the
  chat ones), so the record shows it.
- **An orchestrator acting for the human.** ccbridge (a Claude session in the Claude app driving beacon
  runs for you) answers questions, verifies candidates and reopens or picks nodes through the ledger API as
  actor `orchestrator` (`HUMAN_ACTORS` in `ledger.py`): it may do what the human does, and the hash chain
  records it under its own name, so `orchestrator` events are never mistaken for yours. The CLI's human
  commands still need a terminal and still act as `human`.
- **Tamper-evident, not tamper-proof.** The worker runs as your OS user. The PreToolUse
  guard blocks the obvious routes (Write/Edit into the run dir or the beacon package,
  Bash commands naming the ledger, `.claude/settings*`, `.mcp.json`); the state digest
  catches the rest. Only the main session talks to beacon: a subagent inherits the `mcp__beacon`
  tools, and the guard denies every one of them when the hook input carries an `agent_id` (set only
  for a subagent's call; a session started with an agent option has `agent_type` alone and is the
  main session). Agent definitions are off-limits too: one can carry `permissionMode:
  bypassPermissions` for the subagent it starts, so `setup-project` adds the deny rules
  `Edit(**/.claude/agents/**)` and `Edit(~/.claude/agents/**)` (teardown removes them unless you had
  them before setup), and the guard refuses writes there by every route it can recognise. A path counts
  only when a whole `.claude` component is followed by a whole `agents` component, after `~`, `abspath`
  (`./`, `//`, `..`) and resolving the symlinks of its existing prefix, case-insensitively
  (`.claude/agents-notes.md` or `~/.claude/agentsX` do not count), and only writes are refused: reading
  an agent file is allowed. Write/Edit/NotebookEdit: the file path. Another MCP server's tool: any tool
  not named like a read (`read_*`, `get_*`, `list_*`, `search_*`, ...) with a path value there, or a
  command value that writes there. Bash is parsed (shlex), not grepped: redirections, `tee`,
  `cp`/`mv`/`ln`/`install`/`rsync` targets (a recursive copy or a move onto a `.claude` directory too),
  `rm`/`touch`/`mkdir`/`chmod`/... operands, `dd of=`, `sed -i`/`perl -i`, editors, `curl -o`,
  `wget`, `tar -C`, `unzip -d`, `git checkout|restore|rm|...`, `find -delete|-exec`, and
  `python|perl|node|... -c/-e` code (or a heredoc fed to one) that names an agent path and writes;
  followed through `cd` within the command (and the hook's `cwd`), variables the command assigns or the
  environment holds, `for` loop variables, globs and brace lists, quoting, `$(...)`, backticks,
  `bash -c`, `eval`, `sudo`/`env`/`xargs` prefixes and heredocs fed to a shell. Shell parsing cannot be
  complete, and these are not recognised: a script file run by an interpreter, a computed variable
  holding several components, a program not listed writing into the current directory, hard links, an
  MCP server rooted inside `.claude` given a path relative to it. An unknown variable stands for one
  unknown component (`$X/agents/x` is refused), and a command that does not parse is refused when it
  names `.claude` next to `agents`. The deny rules still cover the built-in Edit/Write tools whatever
  the guard misses, and Claude Code's auto-mode classifier is another layer. On a failed integrity check the gate writes nothing (a new event
  would re-seal the tampered state) and stops with a loud report.
- **No model calls by default (since 2026-11-15).** `max_lighthouse_calls`, `max_decider_calls`,
  `max_classifier_calls` and `max_followup_calls` default to 0: a stall stops the run for the human (or the
  orchestrator acting for them, ccbridge), a branch keeps the worker's order, a prose question goes through
  the keyword heuristic, leftovers are pushed back to the worker. To use the models below, raise the budgets
  for a run (`beacon init RUN ... --set max_lighthouse_calls=20`, or `beacon config`) and provide
  `BEACON_API_KEY`. ccbridge also strips every API key from the runs it starts.
- **Which model, when.** Opus 5.5 everywhere a model judges: (a) `classifier_model`, a question left
  as prose at a stop (at most `max_classifier_calls`, 100, per run; "shall I continue?" is answered without
  a call); (b) the lighthouse, when the run stalls (`max_lighthouse_calls`, 20); (c) the decider, at every
  branch (`max_decider_calls`, 60); (d) `followup_model`, a final message that lists unfinished work
  (`max_followup_calls`, 100, below; lines that only point you at your own move -- `beacon: accept P5`,
  question #8 -- and quoted text do not trigger it).
  The gate, the guard, the ledger, verification, chat commands and the graph use no model.
- **Requests and decisions.** "May I run X?", "shall I run this on the VM?": approved by standing
  rule, the worker goes on (a real malware sample too, unless you turned that check on, above). "A or B?": Opus chooses — at a stop
  the classifier makes the choice and the nudge names it; through `ask(approach_choice)` the decider
  ranks the recorded options. Only information you alone have (a file, a password, a scope decision)
  waits for you.
- **Opus 5.5 API settings (from the model's docs).** It always thinks (adaptive thinking cannot be turned
  off) and its thinking counts against `max_tokens`, so the limits are generous (`*_max_tokens`: 8000 for
  the classifier, 16000 for the decider and the lighthouse; you pay for what is used) and `*_effort` sets how
  hard it thinks (classifier `medium`, decider and lighthouse `high`). Replies are constrained to a JSON
  schema with structured outputs (`output_config.format`); if an API rejects `output_config`, the call is
  repeated once without it. A reply cut at `max_tokens` is reported as such, not as "unparseable". The
  prompts ask for short reasons only, never for the model's reasoning written out (that can be refused).
  Cost: thinking is billed as output ($20 per million tokens), so a decider or lighthouse call is roughly
  $0.10–0.40 and a classifier call $0.03–0.15, depending on context size and thinking.
- **Waiting is not stalling.** A job registered with `mark_waiting`, or one of Claude Code's own background
  tasks (the Stop hook's `background_tasks`), freezes the stall and idle counters. Interactively, a stop
  while a background task runs is let through (`background_wait`): Claude Code wakes the session when the
  task ends. Under `beacon watch` (headless) the worker is kept going with a note to work elsewhere
  meanwhile. Plan mode (`permission_mode: plan`) is left alone (`plan_mode`). No `notify_cmd` for these
  or for a pause.
- **Questions that lost their reason.** When a problem is solved, a node is closed or a proposed problem is
  rejected, the open questions about it are marked `obsolete` and stop showing as "waiting on you".
- **Claude Code changes the watchdog accounts for.** `claude -p` now exits 1 with an error where it used to
  exit 0 (startup failures, a full disk or quota): a full disk and a bad flag/config end the run
  (`fatal_disk`, `fatal_config`) instead of being retried; an expired login (`claude auth login`,
  `setup-token`) ends it as `fatal_auth`; and when a result carries `credential_expires_at` within 3 days the
  watchdog warns once (log, `watch.jsonl`, `notify_cmd`).
- **Claude Code's own `/goal`.** `/goal` is a session-scoped prompt Stop hook judged by a small model; it can
  run next to beacon (either hook blocking keeps the session going) but adds nothing beacon's ledger-based
  gate does not, and both share Claude Code's 8-continuation cap. Use one or the other.
- **A list of what is left is work, not an ending.** When the worker ends a turn with "still missing: X
  is not tested on Windows, Y could also be checked, Z needs a key", every item becomes a node and the
  worker is kept on it. A cheap text check spots such a message; then one Opus call (`followup_model`,
  effort `medium`) reads it with the ledger and sorts each item: **work** the worker can do with what it
  has (`worker_capabilities`, default: its own machine, the project, the web, the Kali and Windows VMs) →
  a node (↻): under the problem it belongs to if that problem is still open, else under a follow-up
  problem "Follow-ups of P1: ..." that closes with `propose_done` once every item is done or a dead end
  (`followup_autoverify`, no human check); **needs the human** (a key, money, your own GUI session, a
  scope or policy call, a change outside the project such as a system-wide install or anything needing
  root) → the same node, blocked on a question to you; **optional** (an idea beyond the run) →
  "Optional follow-ups of P1", proposed, waiting for `beacon: accept`; **supervisor** (a problem the
  worker reports about beacon itself, or a fix it suggests there) → one line in the report, "What Claude
  reported about beacon itself", recorded once, never a node or a question: the worker reports beacon's
  problems and does not fix them, and they do not keep a finished run waiting. Items a node already
  covers, a problem already waiting for your verification and a question already asked are skipped; a
  message is read once; at most `followup_max_items` (8) per stop and
  `followup_max_total` (40) per run, so a worker that finds new gaps forever is stopped. Without a model
  the worker is told (up to 3 times a run) to do or record each item itself. `followup_policy`: `work`
  (default), `propose` (every item waits for you), `off`.
- **One clock per stop.** Claude Code kills the Stop hook at 300 s and a killed hook saves nothing, so
  every model call one stop makes (follow-ups, classifier, lighthouse, their retries) shares
  `stop_budget_seconds` (270): a call is cut to the time left, a retry it has no time for is skipped, and a
  lighthouse review that would not fit waits for the next stop instead of being burnt as a dry round.
- **Windows.** Claude Code runs every hook command through Git Bash there, so beacon does the same for what
  it runs on your behalf (`verify_cmd`, `notify_cmd`, the demo's hook check): POSIX shell syntax, found as
  Claude Code finds bash (`CLAUDE_CODE_GIT_BASH_PATH`, `C:\Program Files\Git`, next to `git`), else `cmd.exe`.
  Write paths in those commands with forward slashes (`C:/Users/...`): bash eats backslashes. The hook
  commands `setup-project` installs are written that way, hook input is read as UTF-8 and the output is
  plain-ASCII JSON, so a Vietnamese chat command or a non-ASCII path is not garbled by the Windows code
  page. Measured: the unit suite on Windows 11 ARM64 with x64 Python 3.14 (emulated). Not measured on
  Windows: a live Claude Code session with beacon's hooks.
- **Two sessions on one run.** Every ledger write is a transaction, and Stop decisions are serialised by
  a lock file (`<run>/gate.lock`), so two sessions stopping at once no longer lose each other's counts.
  They still share one budget and one next-node choice; one session per run stays the clearer setup.
- **Getting unstuck.** Claude Code does not look back by itself, so beacon makes it, in three steps.
  (1) Free, from one stop before the lighthouse threshold without a conclusion (3 stops), or 2 stops
  without new evidence: the nudge opens with the step-back reminder -- re-read the problem statement
  and the given files, re-read the evidence and list every step taken for what was skipped or assumed,
  test the weakest assumption under the dead ends, widen the view (other components, other approach
  classes), check the VM state, beware of decoys. (2) At `k_stall` (4) stops without a conclusion, or
  `k_idle` (2) without new evidence, or near-done claims with no new evidence: the lighthouse (Opus)
  reviews the problem, the ledger, the evidence and the worker's actual steps from the session (each
  tool call and its output, in order, without the worker's reasoning: `lighthouse_transcript`), so it
  sees what was repeated, skipped or never checked even when the worker recorded nothing. It returns new
  approaches (queued as nodes), dead ends whose assumption looks wrong, weak "done" claims, and a
  concrete `review` (what to re-read, which step to re-check, where to look wider), which opens the next
  nudge, ahead of everything else, so the length cap never cuts it. (3) Two reviews in a row with
  nothing new (`lighthouse_dry_max`), or the review budget spent: the run stops and the report goes to
  you. The classifier also adds a `note` when a question shows the worker going in circles.
- **What the worker can add, without asking you.** Solutions and approaches (`add_node`), problems of its
  own (`propose_problem(title, rationale, evidence_ids)`: a second crash, another payload stage), and the
  unfinished work it lists becomes nodes too (follow-ups, above). Code, scripts and notes in the project, as
  usual (only beacon's own files and Claude Code's settings are guarded); what they produce becomes evidence
  (`ref` = the file). With `problem_approval=auto` (the default) a new problem is open at once and the gate
  pushes on it like the checklist's. The run still ends: the hard budget of forced continuations
  (`hard_forced`, 40) and the follow-up cap (`followup_max_total`, 40) bound a worker that keeps finding new
  work. `problem_approval=human` makes new problems wait for `beacon: accept` (the checklist stays your
  scope). Your own decisions still win either way: leftovers of a problem you abandoned, and every item under
  `followup_policy=propose`, wait for you.
- **Branches are decided by the decider, not by you and not by the worker alone.** At a fork the
  worker calls `ask(kind="approach_choice", options=[...], context=...)`; nobody is asked and nothing
  waits. The options become approach nodes and, before the tool returns, a separate model
  (`decider_model`, Opus 5.5) ranks them. It reads the problem's ledger (dead ends with their
  assumptions), the evidence excerpts, your answers, the worker's brief, and the tail of the
  session from Claude Code's transcript — by default the observations only (tool calls and their
  outputs), not the worker's prose, so it judges from the facts rather than inheriting the worker's
  preference (`decider_transcript=full` adds the prose, `off` drops the session). It may add one
  option the worker missed. Its rule is yours: effectiveness first, then cost. The worker is told
  which option to start, the first step and the result that means switching; a failed option
  becomes a dead end and the next is taken. An "A or B?" left at a stop is decided by the classifier
  (Opus) on the spot. To overrule: `beacon: pick P1.A3` (a human pick outranks the decider).
  Cost and limits: one call per branch, roughly $0.10–0.40 with the default context budget and thinking,
  at most `max_decider_calls` (60) per run; it runs inside the beacon MCP server, so the API key
  must be in the environment you start Claude Code from and `.mcp.json` must pass it through
  (`setup-project` writes that; `doctor` checks it). `decider_timeout` (100 s) stays under Claude
  Code's 2-minute mark, after which a tool call moves to the background; the Claude Code desktop
  app is reported to cut MCP calls at about 60 s, so prefer the CLI there. Without a key, over
  budget, on a refusal or a timeout, the worker's own order stands and the reply says why.
  The session excerpt goes to the Claude API — the provider Claude Code already sends the whole
  session to; `decider_transcript=off` keeps it to the ledger and the brief.
- **Refusals are not stalls.** A message that looks like a refusal stops the run for a
  human decision; a lighthouse refusal (`stop_reason: refusal`) is a dry round, never
  retried. The refusal heuristic is deliberately conservative (short messages only) and
  must be calibrated against what Claude Code actually emits on your machine.
- **API errors.** When a turn ends on a rate limit / overload, Claude Code fires
  StopFailure, whose output is ignored — the Stop gate cannot continue the session.
  `stop-failure` only logs and notifies; `beacon watch` is what resumes the run.
- **Activity vs evidence.** The PostToolUse hook stores a hash of each tool output in
  `activity.jsonl` (never the output). New activity only keeps the idle counter at zero;
  progress still requires a concluded node.
- **Flags.** `labhand.set_matcher` only spots a *candidate*. Done means the checklist's
  `verify` command (or you) accepted it: a worker can print any string.
- **Tested on:** Linux x86-64 (Python 3.13, mcp 2.3.0) and Linux aarch64 (Python 3.10.12,
  mcp 2.3.0) — the unit tests in `tests/beacon/`, hooks driven as subprocesses with real hook
  JSON, the watchdog's runner against a fake `claude`; and **end to end with the real Claude
  Code 2.1.322 CLI** against a scripted Messages API (`tests/beacon/test_e2e_claude.py`, opt-in
  with `BEACON_E2E_CLAUDE=/path/to/claude`): the beacon MCP tools as Claude Code calls them,
  the runner on real stream-json, `--resume`, verification. The hook half of that test is
  adaptive: in the sandbox Claude Code turned the hooks off (organization settings unreadable
  through a mock), and the test then checks that the watchdog reports `hooks_disabled`.
  **Not tested:** macOS, the hooks inside a live Claude Code session (against the mock,
  Claude Code could not read its organization's settings, so it kept the hooks off; whether
  the hook half runs elsewhere depends on that read — the test adapts either way, and a normal
  interactive or `beacon watch` run with your own login is the real check), mcp 1.x, real API
  calls (lighthouse, classifier) and real VMs (`vm_check`).
- **Stop-hook cap.** Claude Code overrides a Stop block after 8 consecutive continuations
  with no tool call in between (`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`). The gate escalates to the
  lighthouse and then lets the stop through well before that on idle turns; if Claude Code
  overrides anyway, `decisions.jsonl` shows a block the session did not obey, and
  `beacon watch` starts a fresh session.
- **Chat display (not measured here).** The `beacon ▸` lines and the `beacon:` commands rely on
  hook `systemMessage` / UserPromptSubmit `decision: block` as Claude Code documents them; in the
  sandbox the hooks were off (see above), so check them in your first interactive run.
- **Not measured yet.** Thresholds (`k_stall=4`, `k_idle=2`, budgets) are starting
  points; calibrate them on real runs.
