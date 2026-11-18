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
| `invoke_menu` fails | it needs the target window to be the key/main window |
| an app came to the front unexpectedly | `launch_app` with a document URL activates the app; launch without URLs, then open the document another way |
| `security_prompt_protected` | input would land on a macOS security / consent prompt; that is the person's call |
| daemon questions | `cua-driver status` (socket, pid, permission mode, policies), `cua-driver doctor` |
