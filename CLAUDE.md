# CLAUDE.md — labhand

> Read this first. It is the authoritative, stable entry point for any AI working
> with this repo: what the package is, how to drive the lab machines, and the full list
> of tools available (run commands, read output, screenshots, GUI automation, Frida,
> gdb, background jobs). Prefer reading this one file over re-reading all of `labhand/`.

## What this is

**The Muscle.** A deterministic, `$0` Python library (no model calls) that lets an AI
**operate** the machines of a lab over SSH — today a **Kali Linux** VM and a **Windows** VM,
more hosts later (see the host inventory below).
Every function runs something on a VM and returns the **raw** output as a plain
`VMResult`. It **never interprets** results and has no concept of "flag", "challenge"
or "task". The deciding Brain lives elsewhere — any agent that imports this package.

## Configure (once)

`labhand` reads, by default, a `.env` and `.ssh_keys/` **next to the package**
(repo root). Override with env vars `LABHAND_ENV` / `LABHAND_SSH_KEY`.

- **Hosts are a named inventory** (`labhand/hosts.py`, example in `.env.example`):
  `LABHAND_HOSTS=kali,windows` and per host `<NAME>_HOST`, `<NAME>_USER`, `<NAME>_OS`
  (`linux` | `windows`), optional `<NAME>_PYTHON`, `<NAME>_UTM_VM`, `<NAME>_GHIDRA_HEADLESS`
  (absolute path of `analyzeHeadless` on a linux host; overrides the lookup of `linux_ghidra`). Without
  `LABHAND_HOSTS` the old keys still work unchanged: `kali` from `KALI_HOST` /
  `KALI_USER` (+ `KALI_PYTHON`), `windows` from `WIN_HOST` / `WIN_USER` (+ `WIN_PYTHON_EXE`).
- **Working directory** on every host: `LABHAND_WORKDIR` (default `samples`, relative to
  the SSH user's home, letters/digits/`._-/` only). Every tool starts there, relative
  paths resolve there, spills / jobs / uploads land there. "samples/" in this file means it.
- `.env`, `.env.*`, `.ssh_keys/`, and any private key are **git-ignored — never commit
  them.** This repo is public.

## Use it (for an AI)

```python
import labhand
from labhand import shell, windows, linux, jobs

r = shell.run_cmd("uname -a")          # -> VMResult
print(r.ok, r.summary, r.detail)

windows.win_screenshot()               # r.image = {"media_type","data"(base64)}
windows.win_windows(match="Error")     # read the Windows desktop as text (cheap)
```

### `VMResult` (what every tool returns)

| field | meaning |
|---|---|
| `ok` | did the call succeed |
| `summary` | one short line (headline) |
| `detail` | the raw output (large output spills to a VM file; page it with `read_file`) |
| `known` | measured facts worth keeping |
| `match` | what the injected matcher found in the raw output (see below); else `None`. `flag` is its deprecated alias (`r.flag`, `VMResult(..., flag=)` still work) |
| `info` | the text actually shown, for novelty accounting |
| `image` | `{media_type, data}` for screenshot/picture tools, else `None` |
| `waiting` | the step is legitimately waiting on a running job |

### `set_matcher` — inject what a "win string" looks like

The library does not know what a flag is. A Brain that cares teaches it once:

```python
import re, labhand
labhand.set_matcher(lambda t: (re.search(r"WIN\{[^}]+\}", t) or [None])[0])
```

Afterwards tools fill `VMResult.match` when the pattern appears in raw output.

## Tools — everything you can do on the VMs

All are `$0`, deterministic, and return raw output. `host` is a name from the
inventory; omitted, it is the first host (`win_*`: the first windows host, `linux_*`: the
first linux host). Every tool branches on the host's **OS**, never its name, and an
unknown name comes back as `VMResult(ok=False)` listing the known hosts, before any
connection. Every `win_*` / `linux_*` call below also takes `host=None`.

### Run commands & read output — `labhand.shell`
| call | does |
|---|---|
| `run_cmd(command, host=None, timeout=20)` | run one shell line in `samples/` (linux=bash, windows=cmd.exe); raw output. Real tree-killing timeout. |
| `run_script(script, host=None, kind=None, timeout=20, python=None)` | write & run a multi-line script (`kind`: bash/python3 on linux; powershell/cmd on windows). |
| `read_file(path, offset=0, length=4000, host=None, pinned_docs=())` | page a file on a host; if it is not on `host`, tries every other inventory host in order and says where it was found. |
| `upload_file(local_path, name=None, host=None)` | copy a local file into the host's `samples/`. |
| `download_file(path, dest=None, host=None, overwrite=False, max_bytes=None, timeout=600)` | copy a file from a host to this machine: streamed over SFTP in bounded 8 MiB windows to a temp file, renamed when complete; default `$LABHAND_DOWNLOAD_DIR/<host>/` or `~/labhand-downloads/<host>/`, never inside a git checkout; keeps an existing file unless `overwrite`; size cap (32 GiB default) and disk-space check; `known` has size + sha256. The file is mode 0600 and never executed. |

### Windows desktop — read / see / run — `labhand.windows`
| call | does |
|---|---|
| `win_windows(match=None, timeout=40)` | list open windows (process, title, UI-Automation class/control type) as **text** — the cheap way to read a GUI; pass `match` to dump one window's inner text. |
| `win_screenshot(timeout=40)` | capture the interactive desktop as an **image** (`r.image`). |
| `win_gui_run(program, args="", wait=6, kill=True, screenshot=True, timeout=60)` | run a program on the real desktop, wait, report windows + screenshot. |

### Windows GUI automation (act, not just look) — `labhand.windows`
| call | does |
|---|---|
| `win_ui_tree(match=None, limit=250, timeout=45)` | dump the control tree (UI Automation + Win32) of a window. |
| `win_ui_click(hwnd=None, target=None, window=None, x=None, y=None, after=1, timeout=45)` | click a control by handle / name / coordinates. |
| `win_ui_type(text, hwnd=None, target=None, window=None, enter=False, sendkeys=False, after=1, timeout=45)` | type into a control / focused window. |
| `win_key(keys, window=None, after=1, timeout=45)` | send keystrokes (SendKeys syntax). |
| `win_ui_seq(clicks, window=None, pause=250, after=2, limit=250, timeout=120)` | click a sequence of named controls in one pass. |

### Windows instrumentation — `labhand.windows`
| call | does |
|---|---|
| `win_frida(script, program=None, pid=None, args="", wait=8, kill=True, desktop=False, max_lines=800, timeout=90, drive=None, launch=None, bat_text="", bat_names=())` | run a **Frida** JavaScript hook: spawn by `program`, attach by `pid`, or `launch` a `.bat` GUI app; returns every `send()`/`log()` line. `drive=` presses UI buttons while hooked. |

### Linux — headless GUI & debugger — `labhand.linux`
| call | does |
|---|---|
| `linux_gui_run(command, wait=5, screen="1280x800x24", timeout=60)` | run a GUI program on Kali under **Xvfb** and return a screenshot. Its `xvfb-run -a` (and `linux_gdb`'s) takes the workdir display lock shared with `linux_ui_start`. |
| `linux_gdb(prog, script, args="", gui=True, screen="1280x800x24", max_lines=800, timeout=120)` | run a **gdb-python** script against an ELF (`prog`: relative = in `samples/`, or an absolute path; ASLR off, PIE base fixed at `0x555555554000`); returns whatever it `log()`s. Helpers injected: `rd/wr/u64/u32/f64/reg/cstr/at/hook/starti/cont/run`. |
| `linux_ghidra(path, script="", script_name="labhand_ghidra.py", script_args=(), analyze=True, host=None, timeout=300, extra="")` | run **Ghidra headless** (`analyzeHeadless`) on a linux host: import `path` (host workdir) into a fresh per-call project (`-deleteProject`, `-readOnly`: nothing saved, sample untouched), analyse unless `analyze=False` (`-noanalysis`), run `script` as post-script `script_name` (the extension picks the language: `.py` runs under **PyGhidra** via `python3 -P -m pyghidra.ghidra_launch` after an import check (`-P`: nothing is imported from the samples cwd; Python >= 3.11) - not importable → raw `ok=False`, never installed; anything else, e.g. `.java`, under analyzeHeadless itself. Ghidra 12 has no Jython, measured 2026-11-20) with `script_args`; **raw** Ghidra log + script output, `known` = analyzeHeadless path, Ghidra version, PyGhidra version + python (`.py`), exit code, elapsed. Per-call dirs `samples/_lh_ghidra_<uuid>/` and the OSGi bundle Ghidra compiles for that call's script dir (`~/.config/ghidra/*/osgi/compiled-bundles/<hash>`) removed on every path; tree-killing timeout. Located by `<NAME>_GHIDRA_HEADLESS` (inventory, absolute path), else `command -v analyzeHeadless`, else `/usr/share/ghidra/support/analyzeHeadless`, `/opt/ghidra/support/analyzeHeadless`. Not found → raw `ok=False`; labhand never installs. Prefer it over driving Ghidra's GUI by `linux_gui_run` screenshots. |
| `linux_capa(path, host=None, timeout=120, extra="")` | run **capa** on a linux host against `path` (in the host workdir); returns capa's **raw** output. labhand only runs it — the analyst/Brain reads the MITRE ATT&CK / MBC mapping. Not installed → raw `ok=False`; labhand never installs. |
| `linux_yara(rules, target, host=None, timeout=60, extra="")` | run **yara** (`rules` against `target`) on a linux host; **raw** output, no interpretation. Not installed → raw `ok=False`. |

### Linux GUI as text (AT-SPI) — `labhand.linux_ui`
Read and drive a GUI app on a linux host as **text** through its accessibility tree (AT-SPI),
step by step, instead of `linux_gui_run` pictures. A **session** = one app under its own
Xvfb display + a **private** D-Bus session bus (never the real `:0` or the desktop's bus:
without a private bus AT-SPI reaches the logged-in desktop's apps, measured), kept alive
between calls. Measured basis: `~/.ccbridge/reviews/kali-atspi/REPORT.md`; live run:
`~/.ccbridge/reviews/labhand-linux-ui/LIVE.md`.
| call | does |
|---|---|
| `linux_ui_start(command, name=None, screen="1280x800x24", wait=5, host=None, timeout=60, max_minutes=30)` | start `command` (runs verbatim from a file, cwd = workdir) as a labhand-owned background session: `setsid` supervisor → `timeout -k 10 <max>` → `xvfb-run -a` → `dbus-run-session --config-file` (allow-list: only `org.a11y.Bus` and `ca.desrt.dconf` can be activated, so no gvfs / portals / FUSE mounts). Throw-away `HOME`, `XDG_{CONFIG,CACHE,DATA,STATE}_HOME`, `XDG_RUNTIME_DIR`, `TMPDIR` in `samples/_lh_ui_<session>/`. Returns the session name (`<stem>-<8 hex>`); `known` = display, pids, state dir. The session dies by itself after `max_minutes` (dir removed too). |
| `linux_ui_tree(session, match=None, limit=250, depth=30, showing_only=True, host=None, timeout=45)` | the tree as text, one line per element: `#id [role] 'name' (key states) actions=a\|b text='...'`. Only `showing` subtrees by default (a full Ghidra CodeBrowser tree was 1.78 M chars, 20-31 k showing), closed menus not walked (marked `[closed menu: N items]`), ≤4 actions per line, `match` keeps subtrees whose name/role contains it (+ ancestors), `limit` caps lines and says how many were cut. The `#id`s stay valid until the next tree of that session; each stores role, name, description, the parent's child count and (when showing) the screen extents, and an act tool refuses the id when any of them changed (a removed row would otherwise let the next row's identical `Delete` slide into its place). `ids.json` is written under a per-session lock, so a tree and a name-resolve at the same time lose nothing. |
| `linux_ui_click(session, target, action=None, host=None, timeout=45)` | `target` = `#id` from the last tree (refused if that element changed meanwhile), or a name / `role:name` (`menu item:About`; exact name, then description, then prefix; showing wins; ambiguous → the candidates with new ids). Runs the AT-SPI action (default the first of click/press/activate/toggle), then **re-reads** the element and lists the app's windows: `do_action` returns True even when nothing happens. Items of a closed menu can be clicked directly. |
| `linux_ui_type(session, text, target=None, enter=False, host=None, timeout=45)` | replace `target`'s text: `EditableText.set_text_contents` when it has it; when it lacks it (Chromium) or ignores it (Firefox: True, value unchanged) → `grab_focus` + pointer to the element + `xdotool` ctrl+a + type (text on stdin). Then the text is **read back**; ok = read-back equals `text`. No `target`: xdotool types at the keyboard focus. |
| `linux_ui_screenshot(session, host=None, timeout=30)` | a picture of the session's display (`r.image`), for layout questions text cannot answer. |
| `linux_ui_stop(session=None, host=None, timeout=30)` | end a session: every process carrying its env marker `LABHAND_UI_SESSION=<session>` (app, Xvfb, bus, a11y bus, children) + the supervisor get TERM, then KILL; a stale X lock of its display and its dir go (FUSE mounts unmounted first, `rm --one-file-system`). No `session`: list the live sessions (and orphan processes). |
Switches set in every session (all per process; an app without its switch is simply absent
from the tree): GTK3/GTK4 none; Qt5/Qt6 `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1`; Firefox
`GNOME_ACCESSIBILITY=1`; Java Swing `JAVA_TOOL_OPTIONS=-Xbootclasspath/a:/usr/share/java/java-atk-wrapper.jar
-Djavax.accessibility.assistive_technologies=org.GNOME.Accessibility.AtkWrapper -XX:-UsePerfData`
(no hsperfdata file in /tmp); **Chromium also needs `--force-renderer-accessibility` on its
own command line** (pass it in `command`). Also `GIO_USE_VFS=local`, `GTK_USE_PORTAL=0`.
Blind spots: custom-painted content is not in the tree (qterminal's terminal screen, canvases);
web text fields need the xdotool fallback; icon-only buttons have no name (a `desc=` is shown);
names carry padding / accelerators (`'New      '`, `'New Project... Ctrl+N'`: match by prefix);
Ghidra's CodeBrowser breaks java-atk-wrapper in most launches: use `linux_ghidra`. Always
verify an action by reading again. Needs xvfb-run, dbus-run-session, python3-gi + the Atspi
typelib, at-spi2-core (xdotool for the type fallback / Enter, ImageMagick `import` for the
screenshot); missing → raw `ok=False` naming it, labhand never installs. A start serialises
on a flock of the workdir directory until its display is up; `linux_gui_run` and `linux_gdb(gui=True)`
take the same lock around their `xvfb-run -a` (released inside it once Xvfb is up), so no two
labhand starts pick the same display (a non-labhand `xvfb-run -a` at the same instant can still
collide: the start then fails cleanly). Output labhand did not write (the app's log, the session log, element text, D-Bus error text, the driver's stderr) is neutralised on the host, so no line of it can start with labhand's `__LH_` status markers; a start that fails after its session dir exists always tears the session down and names it. **A session is a private environment, not a sandbox:** labhand never points it at the real `:0`, the real session bus or the real home, but the app runs as the same uid, so a program that targets them by explicit path (`DISPLAY=:0`, `/run/user/<uid>/bus`, `/home/<user>`) still reaches them, and it can touch labhand's own session files; isolate untrusted samples with a disposable VM (`vm_revert`) and the lab network boundary. Known limit: a process that clears `LABHAND_UI_SESSION`
from its environment **and** calls `setsid` escapes `linux_ui_stop` and the `max_minutes` expiry
(the same class as every labhand tree kill).

### UTM power & disposable sessions — `labhand.utm` (runs `utmctl` on the Mac, not over SSH)
Needs `<NAME>_UTM_VM` in the inventory. Refuses to stop a host with a RUNNING job.
| call | does |
|---|---|
| `vm_status(host)` | `started` / `stopped` / ... of the host's UTM VM. |
| `vm_start(host, disposable=False, timeout=300)` | start; `disposable=True` = every disk write of the session is discarded at stop. Waits for SSH. |
| `vm_stop(host, how="request", timeout=180)` | `request`: clean shutdown (guest asked over SSH + ACPI), fails rather than forcing; `force` (after `sync`), `kill`. |
| `vm_revert(host)` | stop + start a fresh disposable session on the saved disk. |
| `vm_snapshot(action, host, name)` | `list/create/restore/delete` over `utmctl snapshot` once UTM >= 5.0.6 (+ macOS 26 for QEMU); refused with that reason before; restore/delete only labhand's own snapshots. |
A clean stop needs the guest: Windows `shutdown /s`; Kali ignores the ACPI button
(xfce) and shuts down through its passwordless `sudo -n /usr/bin/systemctl poweroff`
rule (`/etc/sudoers.d/labhand-power`, exact line and install steps in
`docs/LAB-ISOLATION.md`). `vm_revert` powers off only a disposable session labhand started in
the guest's current boot; a successful stop forgets the session, so any session labhand
did not start counts as unknown and gets the clean request. Windows after a restart sits at the logon screen until someone logs on,
and the `win_*` GUI tools need that logon.

### Background jobs — `labhand.jobs`
| call | does |
|---|---|
| `job_start(name, command=None, script=None, host=None, python=None, truncated=False)` | start work in the background, return immediately with a pid. |
| `job_poll(name=None, tail=40, grep=None, stop=False, host=None)` | check / tail / `grep` / `stop` a job (omit `name` to list all). |

### Mac host — Cua Driver behind the `labhand-mac` MCP server
The lab Mac itself (observe windows, click, type, scroll, drag, menus, clipboard; in the
background) is driven by **Cua Driver** (trycua/cua, MIT) pinned to `cua-driver-rs-v0.53.2`,
not by labhand's Python code. Full guide: `docs/MAC-HOST.md`; worker skill: `mac-host`.
- **Getting it:** ccbridge `--allow-mac` + the exclusive resource `mac-desktop`; beacon adds a
  project MCP server `labhand-mac` = `LABHAND_MAC_MCP_COMMAND` (JSON argv) or
  `~/.local/bin/cua-driver mcp`. Tools appear as `mcp__labhand-mac__<tool>`
  (`get_window_state`, `click`, `type_text`, `set_value`, `press_key`, `hotkey`, `scroll`,
  `drag`, `launch_app`, `invoke_menu`, `list_apps`, `list_windows`, `clipboard_read/_write`, …).
- **Token tips:** the full `tools/list` is 61 tools / 41,242 cl100k; load only the schemas you
  need (`ToolSearch select:mcp__labhand-mac__get_window_state,…`; the 14 core tools = 14,796).
  `cua-driver mcp` cannot filter its list (a user policy can, but only in `--direct` or a
  private daemon: docs/MAC-HOST.md §4). Observe with `include_screenshot:false`, narrow with
  `query` / `max_elements`, re-read with `since:"latest"`; the tree is repeated in
  `structuredContent`, so a result is ~2x its text.
- **Deferred definitions (live E2E 2026-11-19):** Claude Code shows the 61 tools by name only;
  load just the step's tools in one `ToolSearch select:` call (never the whole list), so the
  default argv needs no policy filter (docs/MAC-HOST.md §6b).
- **Permissions:** CuaDriver.app's daemon holds Accessibility + Screen Recording and
  `cua-driver mcp` proxies to it. Residual (Ve's choice): `/usr/local/bin/node` (Desktop
  Commander, responsible process of every ccbridge worker) also holds Accessibility + Screen
  Recording + PostEvent, so any process under Desktop Commander can drive the Mac;
  `--allow-mac` picks who gets the tools, it is **not** an OS boundary.
- **Install / update:** only `python scripts/install_cua_driver.py` (pinned SHA256, signature
  `com.trycua.driver` / team `YCK386LBJ7`, spctl, idempotent); `--check` is a read-only report.
  Telemetry is off (persisted); never run `cua-driver update`.
- **Gate (measured 2026-11-18, `~/.ccbridge/reviews/mac-gate/GATE.md`):** tree-only observation
  1.1k-2.3k tokens of text per window; MCP medians observe 44 ms, type 20 chars 411 ms, pixel
  click 696 ms; element click on TextEdit's text area fails (AXPress -25206) while `type_text`
  by element works; cursor and frontmost app never changed.

## MCP server — expose the Muscle to any MCP client

```bash
pip install -e ".[mcp]"        # mcp >= 1.7; SDK 1.x (FastMCP) and 2.x (MCPServer) both work
LABHAND_ENV=... LABHAND_SSH_KEY=... labhand-mcp      # stdio; = python -m labhand.mcp_server
```
Each tool above is published as an MCP tool; screenshots come back as image content.
Every tool declares all four MCP hints (readOnly / destructive / idempotent /
openWorld): `read_file`, `win_windows`, `win_screenshot`, `win_ui_tree`, `vm_status`, `linux_capa`, `linux_yara`, `linux_vol`, `linux_ui_tree`, `linux_ui_screenshot` are read-only;
everything else is destructive. Keep this when adding a tool (`tests/` enforces it).
The server registers as `labhand`. See `labhand/mcp_server.py`.

## Design invariants (do not break)

- **No interpretation.** Tools return raw output; the Brain concludes.
- **Real, tree-killing timeouts** on every host — a killed call never leaves a process
  running on the VM.
- **Windows background jobs are created through WMI** (`Win32_Process.Create`) and run
  by a per-job launcher: a `Start-Process` child of the SSH session dies ~1 s after the
  call returns, and a one-line `& echo %errorlevel%` is expanded before the command
  runs (it logged `exit 3` as 0). `job_poll(stop=True)` kills the whole tree. Measured
  2026-11-10; `tests/test_jobs.py`.
- **Windows GUI work runs in the interactive session (session 1)** via a scheduled
  task; an SSH login lands in session 0 which has no visible desktop.
- **Large output spills to a per-call file** on the VM; page it back with `read_file`.
- **Every scratch file is per call and removed after it.** A script / command file /
  launcher / config / screenshot a tool writes to the workdir and then runs or reads is
  named `_lh_<stem>_<time_ns>_<pid>_<rand8>[.ext]` (`remote.scratch_name`, same scheme as
  the spill names), its reader gets that name (argv, or formatted into the driver), and
  `remote.remove_quiet` deletes it in a `finally`; the session-1 scheduled task is
  `<task>_<token>`. Several labhand processes share one host: a fixed name lets one
  call run another's script (found 2026-11-20). `tests/test_scratch_unique.py`.
- **One pooled SSH connection per host**, reused; a network blip is retried here, not
  surfaced as a failure.
- **The lab is free-action; network isolation is the boundary.** Security/system
  configuration on the VMs (Defender, firewall, registry, services, ACLs, accounts,
  `cipher`, a root shell) may be changed freely. The `labhand.safety` filter refuses
  exactly three disk-wrecking commands: recursive delete of `/`, `mkfs` / `diskpart`,
  `format X:`. What keeps a sample in the lab is the UTM network setup, documented in
  `docs/LAB-ISOLATION.md` and applied by the human.

## Map of the package

`labhand/`: `hosts.py` (the host inventory), `remote.py` (SSH/SFTP pool), `shell.py` (run/read), `windows.py`,
`linux.py`, `linux_ui.py` (GUI as text through AT-SPI), `jobs.py`, `images.py`, `safety.py`, `_result.py` (`VMResult`,
`set_matcher`), `config.py`, `mcp_server.py`.

`skills/`: the agent skills, one `skills/<name>/SKILL.md` each — the **single source**,
hand-edited markdown (`binary-analysis`: the analysis discipline R1-R10 and technique
notes; `vuln-research`: memory-safety triage; `mac-host`: driving the lab Mac through
`labhand-mac` token-efficiently). `python skills/sync.py` copies them to
`.claude/skills/` (generated but committed, so a fresh clone has the project skills;
`tests/test_skills.py` fails if it drifts from `skills/`; `--dest DIR` for other skill
dirs, `--check` reports drift). Never edit the copies.

`docs/`: `LAB-ISOLATION.md` (the lab's network boundary), `MAC-HOST.md` (the Mac host:
Cua Driver install / check, permissions, MCP tool budget, `LABHAND_MAC_MCP_COMMAND`).

`scripts/`: `install_cua_driver.py` (pinned Cua Driver installer / `--check`; stdlib only).

`tests/`: VM-free unit tests (`pytest`). License: Apache-2.0.

## Research workspace

For the feasibility-evidence study, read `research/CLAUDE.md` and `research/STATUS.md` first. API costs belong to the separate research harness; the deterministic `labhand` library still makes no model calls.
