# Mac host — Cua Driver behind the `labhand-mac` MCP server

The lab Mac itself (macOS 15.5, arm64) can be driven by a worker: observe windows through
the accessibility tree, click, type, scroll, drag, use menus and the clipboard, in the
background (the person's cursor and frontmost app stay put). labhand does not implement
this: it uses **Cua Driver** (github.com/trycua/cua, MIT), pinned to release
`cua-driver-rs-v0.53.2`, installed and checked by `scripts/install_cua_driver.py`.
labhand's own Python package has no Mac code; `labhand-mac` is a separate MCP server.

Worker skill: `skills/mac-host/SKILL.md` (how to drive it cheaply, the measured quirks).
Measurements behind this page: `~/.ccbridge/reviews/mac-gate/GATE.md` (Cua vs Peekaboo
gate, 2026-11-18) and `~/.ccbridge/reviews/mac2-d1-budget/` (tool-budget runs below).

## 1. How a worker gets it

- ccbridge starts the worker with `--allow-mac`; the task also holds the exclusive
  resource `mac-desktop` (one controller per desktop: sessions do not isolate keyboard
  focus, the clipboard or single-instance apps).
- beacon writes a project MCP server named **`labhand-mac`** whose command is the JSON
  array in `LABHAND_MAC_MCP_COMMAND`, or `~/.local/bin/cua-driver mcp` when unset.
- Claude Code then shows the tools as **`mcp__labhand-mac__<tool>`**, e.g.
  `mcp__labhand-mac__get_window_state`, `mcp__labhand-mac__click`,
  `mcp__labhand-mac__type_text`.
- Workers without `--allow-mac` do not get the server. That is a tool-routing choice,
  **not an OS boundary** (see section 3).

## 2. Install / check

```bash
python scripts/install_cua_driver.py --check          # read-only report, exit 0 = all good
python scripts/install_cua_driver.py --check --json
python scripts/install_cua_driver.py                  # install or repair the pinned version
```

The installer (standard library only):

| step | what |
|---|---|
| idempotence | app at `/Applications/CuaDriver.app` with bundle id `com.trycua.driver`, version `0.53.2`, valid signature and spctl → app left alone; only a wrong `~/.local/bin/cua-driver` link or telemetry still on is fixed; otherwise prints `no change` |
| download | `https://github.com/trycua/cua/releases/download/cua-driver-rs-v0.53.2/cua-driver-rs-0.53.2-darwin-arm64.tar.gz` (https only, size-capped) into a fresh temp dir |
| checksum | SHA256 must be `2d8954c3a5000c2247bccbdb714adb7df00af3aab3ac1403f22a92b335b9d20a` (= release SHA256SUMS = GitHub asset digest) |
| extract | every tar member must stay under `cua-driver-rs-0.53.2-darwin-arm64/`; no absolute paths, `..`, escaping links or device files |
| signature | `codesign --verify --deep --strict -R=<requirement>`: Apple-anchored Developer ID Application leaf, identifier `com.trycua.driver`, team `YCK386LBJ7`; `codesign -dv` Identifier / TeamIdentifier; `spctl -a -t exec` accepted (Notarized Developer ID) — on the staged copy and again on the installed copy |
| telemetry | `cua-driver telemetry disable` with the staged binary is the **first** cua-driver command run; every call the script makes also sets `CUA_DRIVER_RS_TELEMETRY_ENABLED=0`; result persisted in `~/.cua-driver/config.json` (`"telemetry_enabled": false`) |
| install | best-effort `cua-driver stop` of a running daemon, move the old app aside, `ditto` to `/Applications/CuaDriver.app`, re-verify, `lsregister -f`; any failure restores the previous app |
| link | `~/.local/bin/cua-driver` → `/Applications/CuaDriver.app/Contents/MacOS/cua-driver` (a regular file there is never overwritten); `cua-driver --version` must print `0.53.2` |
| never | `cua-driver update`, `check-update`, `call check_for_update`, `--apply` are refused in code; TCC is never touched |

`--check` reports version, signature, spctl, link, persisted telemetry and
`cua-driver permissions status --json` (accessibility / screen recording / daemon /
attribution). Exit 1 means install or repair is needed; missing permissions are a
`WARN` (a person grants them once, section 3). Updating = change the pinned version,
SHA256 and tests in the script, review the new release, rerun the installer.

Tests: `tests/test_install_cua_driver.py` (fakes for network, codesign, spctl, ditto,
lsregister and cua-driver; nothing is installed).

## 3. Permissions and who can drive the Mac

- **CuaDriver.app's daemon** holds Accessibility + Screen Recording (granted once with
  `cua-driver permissions grant`, a guided flow a person clicks through). `cua-driver mcp`
  (no `--direct`) proxies to that daemon, so actions run under the app's own TCC identity
  (`attribution=driver-daemon, bundle_id=com.trycua.driver`).
- **Residual, by Ve's choice:** `/usr/local/bin/node` (Desktop Commander, the responsible
  process of every ccbridge worker) also holds Accessibility + Screen Recording +
  PostEvent. So **any process started under Desktop Commander can drive the Mac**, with or
  without Cua. `--allow-mac` decides which workers get the `labhand-mac` tools; it is not
  an OS-level boundary. The grants stay as they are.
- Telemetry is off (persisted). There is no auto-update (no LaunchAgent; `update` only on
  demand, and labhand never runs it). Updates go only through the pinned installer.
- Cua's own `standard` permission mode refuses input onto macOS security / consent
  prompts (`security_prompt_protected`). Workers must still never touch System Settings,
  password managers or Keychain unless the task says so.

## 4. MCP tool budget (`tools/list`)

Measured 2026-11-18, one persistent MCP stdio session per row, `initialize` +
`tools/list` only (no tool calls); tokens = tiktoken `cl100k_base`. Raw files and
scripts: `~/.ccbridge/reviews/mac2-d1-budget/` (`probe_list.py`, `tok.py`,
`raw/*_tools_list.json`, `raw/tokens.json`).

| what | tools | chars (JSON) | cl100k |
|---|---|---|---|
| `cua-driver mcp` (proxy to the app daemon), full | 61 | 185,518 | 41,242 |
| `cua-driver mcp --direct` + `CUA_DRIVER_POLICY_FILE` (core policy) | 14 | 65,956 | 14,811 |
| the same 14 tools cut from the full list (byte-identical per tool) | 14 | 65,899 | 14,796 |
| tool **names** only (`mcp__labhand-mac__*`, 61 lines) — what a deferred listing costs | 61 | 1,983 | 695 |
| CLI `cua-driver list-tools` (one line per tool) | 61 | 5,880 | 1,223 |
| CLI `cua-driver describe <tool>`, summed over the 14 core tools | 14 | 51,088 | 12,264 |

Core set (14): `list_apps`, `list_windows`, `get_window_state`, `click`, `type_text`,
`set_value`, `press_key`, `hotkey`, `scroll`, `drag`, `launch_app`, `invoke_menu`,
`clipboard_read`, `clipboard_write`. Per tool (cl100k): `get_window_state` 2,712,
`drag` 1,497, `hotkey` 1,463, `click` 1,450, `type_text` 1,155, `scroll` 1,095,
`press_key` 1,076, `set_value` 968, `invoke_menu` 860, `launch_app` 741,
`list_windows` 632, `list_apps` 580, `clipboard_write` 300, `clipboard_read` 264.
The biggest non-core tools: `run_steps` 1,745, `start_session` 1,741,
`start_recording` 1,443, `set_agent_cursor_motion` 1,318, `set_config` 1,225.

### Can `cua-driver mcp` expose only a subset?

- `cua-driver mcp --help` has no tool filter. Cua's filters are **daemon startup**
  settings ("agent authorization (serve only)"; `docs/content/docs/cua-driver/guides/permissions.mdx`
  at the tag): a user policy (`CUA_DRIVER_POLICY_FILE`, YAML or Rego, deny by default),
  a managed policy, the permission mode and a capability manifest. A running daemon
  cannot be reconfigured.
- On macOS the plain `cua-driver mcp` proxies to the one shared CuaDriver.app daemon, so
  per-client environment is ignored: with `CUA_DRIVER_POLICY_FILE` or a manifest in its
  env it still lists 61 tools (measured).
- A **user policy does filter `tools/list`** where the runtime reads it: `mcp --direct`
  with `CUA_DRIVER_POLICY_FILE` listed exactly the 14 allowed tools (measured).
- A **capability manifest** (`version: 3`, `allow.tools`) did **not** shrink
  `tools/list` (61 with `--direct` + `CUA_DRIVER_CAPABILITY_MANIFEST_FILE`); it only
  refuses calls.
- Results: there is no switch to drop the repeated tree from `structuredContent`
  (`get_window_state` returns the Markdown tree in the text block and again in
  `structuredContent`, so the result JSON is about twice the text). Make results small
  instead: `include_screenshot:false`, `query:"…"` (matching rows + ancestors),
  `max_elements` / `max_depth`, and `since:"latest"` (diff read: only `+`/`~`/`-` rows)
  on every re-read; for pictures `include_accessibility_tree:false`,
  `screenshot_format:"jpeg"`, `max_dimension`, or `screenshot_out_file` (a path instead
  of base64). `tree_format:"both"` and `full_output:true` double the size.

### `LABHAND_MAC_MCP_COMMAND` — the argv to use

**Recommended (default): leave it unset**, or set it to

```bash
export LABHAND_MAC_MCP_COMMAND='["/Users/vahpem/.local/bin/cua-driver", "mcp"]'
```

This is the path GATE.md measured (app TCC identity, background behaviour, latencies).
Its 41k-token schema is only paid if the client loads every schema: Claude Code's tool
search defers MCP tool schemas (this setup lists MCP tools as deferred), so a worker pays
the ~695 tokens of names and loads only what it needs, e.g.
`ToolSearch select:mcp__labhand-mac__get_window_state,mcp__labhand-mac__click,mcp__labhand-mac__type_text`
(all 14 core tools = 14,796 tokens; usually fewer are needed).

Measured alternatives that cut the list server-side to the core 14 (14,811 tokens),
each with a cost; use one only for a client that loads all schemas up front:

1. **`--direct` + user policy** (one argv, measured to list 14 tools):

   ```bash
   export LABHAND_MAC_MCP_COMMAND='["/usr/bin/env", "CUA_DRIVER_POLICY_FILE=/Users/vahpem/.cua-driver/labhand-core-policy.yaml", "/Users/vahpem/.local/bin/cua-driver", "mcp", "--direct"]'
   ```

   Caveat: `--direct` runs the runtime inside the worker process, so TCC attribution is
   the worker chain (node), not CuaDriver.app, and the binary's own diagnostics say an
   unbundled runtime's no-raise focus steps fail (`SLPSPostEventRecordTo` 1002). Its
   background behaviour was **not** measured (driving apps was out of scope).
2. **A private policy daemon inside CuaDriver.app** (keeps the app identity; not
   measured): start it once, e.g.
   `open -n -g --env CUA_DRIVER_POLICY_FILE=/Users/vahpem/.cua-driver/labhand-core-policy.yaml /Applications/CuaDriver.app --args serve --socket /Users/vahpem/Library/Caches/cua-driver/labhand-core.sock`,
   then `LABHAND_MAC_MCP_COMMAND='["/Users/vahpem/.local/bin/cua-driver", "mcp", "--socket", "/Users/vahpem/Library/Caches/cua-driver/labhand-core.sock"]'`.
   Needs its own lifecycle (start after login, `cua-driver stop --socket …`).

The core policy file both alternatives use (`~/.cua-driver/labhand-core-policy.yaml`;
deny by default once set; a missing or invalid file stops the runtime from starting):

```yaml
allow:
  tools:
    - list_apps
    - list_windows
    - get_window_state
    - click
    - type_text
    - set_value
    - press_key
    - hotkey
    - scroll
    - drag
    - launch_app
    - invoke_menu
    - clipboard_read
    - clipboard_write
```

### CLI path cost (`cua-driver call <tool>`)

A worker with a shell but no MCP server can use the CLI: no schema cost up front;
`cua-driver list-tools` is 1,223 tokens and `cua-driver describe <tool>` 152-2,208 per
tool (12,264 for all 14 core tools), read only for tools it does not know. Per call
it costs a process: median 60.5 ms observe vs 43.6 ms over MCP, pixel click 725.9 vs
695.5 ms, type 20 chars 430.2 vs 410.6 ms (GATE.md 3c). A one-shot call gets a
disposable session, so element tokens and screenshots do not carry over; pass the same
`"session":"<label>"` on every call of a multi-step flow (a pixel click without an
earlier screenshot in the same session fails `screenshot_context_missing`).

## 5. CLI equivalents

All CLI calls go to the same daemon. `--json`/JSON arguments as in the MCP schema.

| MCP tool (`mcp__labhand-mac__…`) | CLI |
|---|---|
| `list_apps` | `cua-driver call list_apps '{}'` |
| `list_windows` | `cua-driver call list_windows '{"pid":1234}'` |
| `get_window_state` | `cua-driver call get_window_state '{"pid":1234,"window_id":56,"include_screenshot":false,"session":"t1"}'` |
| `type_text` | `cua-driver call type_text '{"pid":1234,"element_token":"<snapshot_id>:7","text":"hi","session":"t1"}'` |
| `click` | `cua-driver call click '{"pid":1234,"element_token":"<snapshot_id>:3","session":"t1"}'` |
| schema of one tool | `cua-driver describe click` |
| all tool names | `cua-driver list-tools` |
| health | `cua-driver status`, `cua-driver doctor`, `cua-driver permissions status --json` |

## 6. What the gate measured (summary of GATE.md)

- Cua Driver 0.53.2 vs Peekaboo 4.9.9; Cua chosen. `tools/list` 61 tools / 41,242
  cl100k (Peekaboo 26 / 11,830).
- Tree-only observation: text 1.1k-2.3k cl100k per window (TextEdit 1,140, Finder 2,275,
  System Settings 2,339); the result JSON is about 2x because the tree is repeated in
  `structuredContent`. Screenshots come back at Retina 2x.
- MCP medians: observe 43.6 ms, type 20 chars 410.6 ms, pixel click 695.5 ms, read
  46.0 ms; element click 474 ms where it works.
- Background: across 87 MCP and 54 CLI calls the cursor and frontmost app never changed
  (exception found in exploration: `launch_app` **with a document URL** brought TextEdit
  to the front).
- Correct Unicode: `Xin chào labhand-mac 123` typed by element and read back equal.

## 6b. Live end-to-end (2026-11-19)

First run through the real bridge (`~/.ccbridge/reviews/mac-e2e/E2E.md`, raw
`latency.txt`, `probes.jsonl`): a ccbridge `--allow-mac` worker drove TextEdit only through
`mcp__labhand-mac__*` (Cua Driver 0.53.2, default argv `cua-driver mcp`, `session:"e2e"`).

- **Definitions are deferred.** All 61 tools were present by name only; none was loaded up
  front. One `ToolSearch select:` loaded 8 schemas (`list_windows`, `get_window_state`,
  `type_text`, `set_value`, `click`, `launch_app`, `invoke_menu`, `run_steps`); 6 were
  called (`invoke_menu` and `run_steps` were not needed).
- **Flow:** find TextEdit → `launch_app` (no URLs) → observe → `set_value ""` → `type_text`
  `Xin chào từ labhand-mac 42` by element → read back → close button by element → sheet
  → `Delete` by element → window gone. Read-back exact (26 code points, NFC).
- **14 calls, ~6.5 s of Mac tool time** (tool_use → tool_result, from the transcript; the
  whole flow took ~90 s of clock, mostly model turns and probes). Per call:

  | call | latency |
  |---|---|
  | `list_windows` (×3) | 126, 113, 111 ms |
  | `launch_app` | 728 ms |
  | `get_window_state` (×6: placeholder id, document, elements budget 11, `query`, `max_depth:2` + `query`, sheet query) | 124, 212, 119, 284, 175, 320 ms |
  | `set_value` (clear, `confirmed`) | 720 ms |
  | `type_text` 26 chars by element (`confirmed`) | 571 ms |
  | `click` close button / `Delete` by element (`unverifiable`, both worked) | 1517, 1337 ms |

- **Background:** `launch_app` without URLs did not activate TextEdit
  (`self_activation_suppressed:true`); the frontmost app (Chrome) never changed in 11
  probes. The document window was on Space 1 while the user was on Space 22; every element
  action still worked. Over the input actions (`set_value`, `type_text`, both `click`s)
  the cursor stayed exactly the same; it moved only across reads and while no input tool
  ran (a person was using the Mac), so nothing shows the driver moving it.
- **What went wrong (now in the skill):** `launch_app` returned placeholder window ids
  (`ax_window_unresolved` → re-list with `list_windows {pid}`); `tree_format:"elements"`
  with `max_elements:11` missed the text area because the menu bar is walked first (a
  `query:"AXTextArea|labhand"` read returned the value); the sheet query
  `Delete|Save|Cancel|keep` also matched the menu bar (383 rows); `/usr/bin/python3` has no
  PyObjC (the probe used ctypes).
- **Negative check** (`~/.ccbridge/reviews/mac-e2e/CONTROL.md`): a task **without**
  `--allow-mac` found no `labhand-mac` tool (`ToolSearch` for `labhand-mac`,
  `+labhand-mac`, `labhand_mac`: no match) and no `labhand-mac` entry in the scratch
  repo's `.mcp.json` or `.claude/settings.local.json`.
- **Consequence for the argv:** because Claude Code loads the definitions on demand, a
  worker never pays the 41k-token `tools/list`. The default proxy argv (`cua-driver mcp`,
  `LABHAND_MAC_MCP_COMMAND` unset) is kept; the policy-file routes in §4 stay documented
  as unneeded alternatives for a client that loads all schemas up front.

## 6c. Mac trials (2026-11-19)

Five harder live trials after the E2E, each by a ccbridge `--allow-mac` worker driving the Mac only
through `mcp__labhand-mac__*` (Cua Driver 0.53.2, macOS 15 arm64); the shell only read state.
Raw detail (every call, failure and workaround): `~/.ccbridge/reviews/mac-trials/s1..s5/REPORT.md`
(not in this repo). The rules a worker needs are in the `mac-host` skill; this section keeps the
numbers and the reasons.

| trial | what it tested | result | `labhand-mac` calls | screenshots | front app changed | cursor moved | wall time |
|---|---|---|---|---|---|---|---|
| S1 | TextEdit: typing, selecting, Bold via menus only; save panel with Go to Folder; Finder rename | done, with workarounds (bold font and file verified by AX and checker) | 64 (7 errors/refusals) | 2 | yes: Chrome → TextEdit (`bring_to_front` for Format menu), left so | no | ~7.7 min |
| S2 | Calculator by 12 element presses (12345×6789), Cmd+C to clipboard, Cmd+V into TextEdit, two-window layout | done (background Cmd+C failed, foreground worked) | 24 | 0 | no lasting change (brief flash during foreground hotkeys not measured); a shell `ls` of a sandbox container raised a consent prompt afterwards | no | ~2.8 min |
| S3 | Chess: three moves by background drag, background click-click, foreground drag; Game Log read-back; save as PGN | done; all moves landed | 45 (3 errors) | 3 | yes during the run (`bring_to_front` for Moves menu); Terminal again after quitting | yes, only by the foreground `drag`, left at the drop point | ~5 min 13 s |
| S4 | Activity Monitor: tabs, sort by Memory, top 5 rows, toolbar search, View switch, restore | done (View > All Processes needed to find a system process) | 31 | 0 | no | moved between the two samples, but no pixel or foreground action ran: not attributed (not measured) | ~4.5 min |
| S5 | Preview: open panel + Go to Folder, Tools > Rotate Left, Adjust Size sheet (popup, checkboxes, width), export as JPEG, revert the source | done, with workarounds (scratch document to get a window; `kill_app` at the end) | 78 | 4 | not measured (the person was using the Mac; foreground flashes not probed) | not measured per action (the person's own mouse use masks it) | ~8 min |

### Why the workarounds work

- **Disabled menus.** macOS greys out some items while their app is inactive (TextEdit Format >
  Font, Chess Moves). `invoke_menu` walks the real menu by AX and finds the item disabled.
  `delivery_mode:"foreground"` only changes how input events are posted; it does not activate the
  app, so the item stays disabled (S1). `bring_to_front` activates the app, after which the item
  and the following menus work, and the app stays frontmost. Menus that macOS leaves enabled
  (Finder File/Go, Activity Monitor View, Preview Tools/File) work in the background, also on a
  window that is not key (S1, S4, S5); the old "needs the key/main window" line is corrected.
  `window_not_key` came only from a `launch_app` placeholder id, and the error names the real
  focused window (S2).
- **Background hotkeys.** A background `hotkey` was posted as `route:"synthetic_events"`,
  `effect:"unverifiable"`; Calculator ignored Cmd+C (clipboard empty, S2), and the save panel
  ignored Cmd+Shift+G (S1, S3). Foreground delivery posts through `route:"global_input"` (HID
  tap) to the briefly fronted window and the prior app is restored; in S2 the cursor and front app
  read unchanged afterwards.
- **Panel process structure.** The open/save panel is hosted by
  `com.apple.appkit.xpc.openAndSavePanelService`, a separate process. Its own window is listed
  `is_on_screen:false` (S1) or resolves to an empty tree (`ax_window_unresolved`, S5). The panel's
  AX is bridged into the host app: the host's `list_windows` shows the sheet as its own window
  ("Save", "Open" id=open-panel, `AXSheet (export)` id=save-panel), `get_window_state` on it
  returns the full tree, and element tokens from it work for `set_value` and `click`. S1 read the
  same `AXFocusedUIElement` from the host pid and the service pid. Because the controls are
  remote, keystrokes do not land in the Go to Folder `PathTextField` (S1: `type_text` delivered 0
  of 34 chars, background and foreground); `set_value` does. Return needs foreground (S1, S3, S5);
  AXConfirm and a background Return did nothing. In S1 Return also failed until AX focus moved
  from the `GoToWindow` sheet to the field (a `type_text` on the field moved it, though it
  delivered nothing). In S5 keys reached the open panel only after a background pixel click on
  an empty area of its file list. Finder's own Go to sheet (not the panel) had the field focused
  and took a background Return (S1).
- **Pixel click vs drag.** A background pixel `click` over an AX element is hit-tested and sent as
  `AXPress` (S3: "PX hit-test pressed the background element via AX"), so it is no mouse test.
  A background `drag` is posted as synthetic events to the window and landed on Chess's board
  with another app frontmost; a foreground `drag` moves the real pointer and leaves it at the drop
  point (screen 716,590 → 611,516 in S3, front app restored).
- **Chess coordinates.** The board is a 3D perspective view; AX square frames had usable x but
  flipped y (rank 1 smallest). S3 took rank rows and file edges from one PNG screenshot (1366×1059
  px for an 1111×861 pt window, scale ≈1.23) and checked each move by AX (square labels like
  "white pawn, e4"). Chess exposes 64 square `AXButton`s, so state is readable by AX.
- **Windowless apps.** `launch_app` without URLs gave Preview no window at all; `invoke_menu`,
  `hotkey` and `bring_to_front` all need one, so the menu bar was unreachable until a scratch
  document was opened by URL (S5). A running windowless TextEdit instead opened an Untitled
  document but returned only placeholder ids (S2). An app left windowless refuses `invoke_menu`
  and pid-targeted `hotkey`; Chess, being frontmost, quit with `hotkey cmd+q scope:"desktop"`
  (S3); for Preview a pid `hotkey cmd+q` was refused ("No eligible top-level windows") and only `kill_app` (SIGKILL) was left (S5). Calculator quits with its last window
  (S2).
- **Preview revert.** Preview autosaved the rotate and resize into the source within seconds
  (S5); File > Revert To > "Last Opened — Today, HH:MM" (full dynamic title) plus `Revert` in the
  alert sheet restored the original size and mtime.
- **Shell ground truth.** `ls ~/Library/Containers/com.apple.TextEdit/...` from the worker's
  shell hung >120 s and left `UserNotificationCenter` frontmost, most likely a privacy consent
  prompt on the person's screen (S2). Read-only AX attribute reads through pyobjc in
  `~/venvs/lab/bin/python` (`AXSelectedTextRange`, `AXAttributedStringForRange`,
  `AXFocusedUIElement`) raised nothing and verified selection and the bold run (S1).
- **Locale.** The Mac shows decimal commas and dot thousands separators (`16,00 GB`, `572,9 MB`,
  `1.006`); a US-format parser misreads them (S4). Calculator's AX value starts with U+200E; the
  copied clipboard text does not (S2).

### Measured AX read sizes (S4, Activity Monitor, characters estimated from response length)

| read shape | returned | approx. chars |
|---|---|---|
| `max_depth:5, max_elements:150`, markdown, no query | 5 complete rows, bottom stats panel, toolbar, menu bar | ~3.5k |
| `max_depth:5, max_elements:200`, markdown, search filtered to 1 row | that row with all 10 cells | ~3k |
| `max_depth:1, query:"GB\|MB\|Memory"`, elements | bottom stats panel only | ~2.5k |
| `max_depth:3-4`, role query, elements | tab segments + search field with tokens | ~2.5-3k |
| `max_depth:8, query:" MB"`, elements | 138 rows + 138 memory cells | ~70k |
| `max_depth:4/5, max_elements:70-80`, markdown | toolbar, menu bar, at most 1 row without cells | ~2.5k |

The window walk reported ~500-2,700 nodes. A `query` read always walked a 2,000-node budget
whatever `max_elements` said, and the table rows came first, so a role query for the toolbar
returned 0 matches; `max_depth:3, max_elements:8000, timeout_ms:10000` walked all 563 nodes and
found it. With `max_elements` 40 or 120 the rows came back without cells, with 150 or 200 with
cells. Menu-word queries cost the most elsewhere: `Cancel`/`Log` returned ~180 menu-bar rows
(~6k tokens, S3), `Cancel` on a Preview window 390 rows (S5), and `max_depth:4` did not stop
`Delete|Don|Cancel|Save` from returning 180 menu rows (S2).

## 7. Troubleshooting

| symptom | cause / fix |
|---|---|
| `--check`: app FAIL / version FAIL | not installed, other version, or signature broken: run `python scripts/install_cua_driver.py` |
| `--check`: permissions WARN (accessibility / screen_recording false) | a person runs `cua-driver permissions grant` once; never change TCC from a worker |
| `--check`: telemetry NOT disabled | rerun the installer (it runs `cua-driver telemetry disable`) |
| link FAIL, "exists and is not a symlink" | something else owns `~/.local/bin/cua-driver`; move it away, rerun |
| `stale_element_token` | the snapshot was replaced or idled out (~5 min TTL): `get_window_state` again |
| `click` by element: `AXPress returned -25206` (TextEdit text area) | the element only advertises AXShowMenu: type into it by element without clicking, or pixel-click from a fresh screenshot of the same session |
| `screenshot_context_missing` | pixel click without a screenshot in the same session: observe with a screenshot first, same `session` |
| `ax_unresolved` / empty tree right after File > New | the window is still appearing: wait ~1.5 s, observe again |
| `hotkey` (e.g. cmd+w) does nothing | the window is on another Space / not key: press its close button by element instead |
| `invoke_menu`: `path segment N is disabled while the app is in the background` | `bring_to_front` the window first (the app stays frontmost); `delivery_mode:"foreground"` does not help (§6c) |
| `invoke_menu`: `window_not_key … focused=Some(N)` | you passed a placeholder id; use window `N` from the message |
| an app came to the front unexpectedly | `launch_app` with a document URL activates the app; launch without URLs, then open the document another way |
| `security_prompt_protected` | input would land on a macOS security / consent prompt; that is the person's call |
| daemon questions | `cua-driver status` (socket, pid, permission mode, policies), `cua-driver doctor` |
