---
name: mac-host
description: >-
  How to drive the lab Mac itself (open apps, read windows, click, type, menus,
  clipboard) through the labhand-mac MCP server (Cua Driver) with few tokens and
  without disturbing the person using it: observe tree-only, act by element, verify
  each effect, the measured quirks. Load it before any mcp__labhand-mac__ call.
---

# Driving the lab Mac (`labhand-mac` = Cua Driver 0.53.2)

You get the tools only when ccbridge started you with `--allow-mac`; they are named
`mcp__labhand-mac__<tool>`. Background guide and numbers: `docs/MAC-HOST.md`.

## Before you start

- **Hold `mac-desktop`.** One controller per desktop: sessions do not isolate keyboard
  focus, the clipboard or single-instance apps.
- **The 61 tool definitions are deferred** (Claude Code shows only the names; calling one
  before loading fails). Load what the step needs in **one** call, never the whole list
  (61 tools ~41k tokens): the set the live E2E used was
  `ToolSearch select:mcp__labhand-mac__list_windows,mcp__labhand-mac__get_window_state,mcp__labhand-mac__type_text,mcp__labhand-mac__set_value,mcp__labhand-mac__click,mcp__labhand-mac__launch_app,mcp__labhand-mac__invoke_menu`
  (+ `mcp__labhand-mac__run_steps` when batching helps). Add `press_key`, `hotkey`,
  `scroll`, `drag`, `bring_to_front`, `set_window_frame`, `list_apps`, `clipboard_read`/`_write` only when a step needs them.
- **Never touch** System Settings, password managers or Keychain unless told. A person is using this Mac: work in the background, keep their cursor and front app.
- Pass one short `session` label (e.g. `"session":"t1"`) on every call that takes it.

## Observe cheaply

1. Find the window: `list_windows` with `app` (name or bundle id) or `pid`, never
   unfiltered. Off-Space windows are listed too (marked off-screen).
2. `get_window_state {pid, window_id, include_screenshot:false}` → a compact Markdown
   tree (1-2.5k tokens per window). Rows `[N]` are addressed as `element_token`
   `"<snapshot_id>:N"`.
3. Narrow before reading everything: `query` (matching rows + ancestors), `max_depth`,
   `max_elements`. With `tree_format:"elements"` the **menu bar is walked first**, so a
   small `max_elements` (11 in the E2E) misses the window content: query by role or text
   (`query:"AXTextArea|<text>"`) or limit `max_depth`; menu-item words: see Queries below.
4. **Re-read with `since:"latest"`** after acting: only changed rows (`+`/`~`/`-`) come
   back. Use the NEW `snapshot_id` from that response in tokens.
5. A screenshot only when the tree cannot answer (canvas, video, custom-drawn UI, or you
   need pixels for a click). Cheaper picture: `include_accessibility_tree:false`,
   `max_dimension`, `screenshot_format:"jpeg"` (but a pixel click needs a PNG read).
6. No `tree_format:"both"` or `full_output:true` (double size); do not echo results into notes.

**Element tokens are snapshot-bound.** A new read of the same window replaces the old
snapshot; the session idles out after ~5 min. `stale_element_token` → observe again,
then act with the fresh token. Never reuse a token across a long wait.

## Act, then verify

Every action returns an `effect`: `confirmed`, `partial`, `unverifiable`,
`suspected_noop` or `refused`. **Unless it is `confirmed`, observe again**
(`since:"latest"`) and check the state yourself before the next step. Element `click`
often returns `unverifiable` even when it worked: observe once,
**never repeat the click blindly**.

Action ladder, one rung at a time, checking state between rungs:

1. **By element** (`element_token`): no cursor, no focus change, works on background,
   minimized and off-Space windows. Prefer `set_value` / `type_text` by element over
   clicking into a field.
2. **By pixel** (`x`,`y` = pixels of a screenshot taken in the **same session**, less than
   ~60 s old, window not moved/resized): for elements missing from the tree or when the
   element action fails. No screenshot in the session → `screenshot_context_missing`.
3. **`delivery_mode:"foreground"`** only when 1 and 2 fail and the task allows a visible
   takeover: it fronts the window briefly; a foreground `drag` moves the cursor (see Mouse).

## Measured quirks (GATE.md 2026-11-18, live E2E and five trials 2026-11-19)

**Background vs foreground**
- Menu items macOS disables while the app is in the background (TextEdit Format > Font > Bold,
  Chess Moves > Game Log) fail with `path segment N is disabled while the app is in
  the background`. `delivery_mode:"foreground"` does **not** activate the app for `invoke_menu`;
  `bring_to_front {pid, window_id}` first, then `invoke_menu`. That app stays frontmost: report it.
- Many menus work in the background: Finder File and Go, Activity Monitor View, Preview Tools/File.
- `invoke_menu` refused with `window_not_key … focused=Some(N)`: you passed a placeholder id; the
  error names the real key window, use `N`.
- `invoke_menu` paths must match titles exactly, dynamic ones too (Preview `File > Revert To >
  Last Opened — Today, 18:21`); the error lists the real items, copy from it.
- A background `hotkey` can go out as `route:"synthetic_events"`, `effect:"unverifiable"` and do
  nothing (Calculator cmd+c left the clipboard empty; cmd+shift+g in a save panel opened nothing).
  Repeat with `delivery_mode:"foreground"`: it fronts the target briefly, restores the prior app.

**Open/save panels and sheets**
- The panel runs in `com.apple.appkit.xpc.openAndSavePanelService`, but its own window is
  off-screen or `ax_window_unresolved`. Drive it through the **host app's pid**: `list_windows
  {pid:<app>}` lists it ("Save", "Open" id=open-panel, `AXSheet (export)` id=save-panel), and
  `get_window_state` on that window gives the full tree.
- Act by AX identifier: `saveAsNameTextField` (name), `ContentTypesPopup` or the unlabelled format
  popup, `where popup`, `OKButton`, `CancelButton`, `DontSaveButton`. `set_value` the name, and set a
  popup in one call by item title (`"Moves (PGN)"`, `"JPEG"`, `"pixels"`), fully background.
- Go to Folder: foreground `hotkey cmd+shift+g` opens a separate window (`AXSheet` id=GoToWindow).
  An open panel needs one background pixel click on an empty list area first, or keys never reach it.
- Go to Folder: keystrokes never land in `PathTextField`; `set_value` the path (folder or file),
  then `press_key return` with `delivery_mode:"foreground"` (AXConfirm, background Return: no-ops).
  Still open? AX focus sits on the sheet: a `type_text` on the field moves it there (even at 0
  chars), then `set_value` + foreground Return again. Check "Where:" after.
- Dialog sheets (Preview Adjust Size): `set_value` a field, then `click action:"confirm"` so the app
  recomputes; checkbox state is `value` 1/0 in `tree_format:"elements"` (markdown omits it).
- Alert buttons: `run_steps` `{click:{role:"button",name:"Revert"}, expect:[{role:"sheet",
  gone:true}]}` found them when a quoted `query` matched nothing.
- A screenshot of a sheet window returns the whole parent window image: do not pixel-click from it.

**Queries and large AX trees**
- Words that also name menu items or actions (`Cancel`, `Log`, `Delete`, `Save`) return the whole
  menu bar (180-390 rows, ~6k tokens), `max_depth` does not stop it: query by AX identifier
  (`saveAsNameTextField|OKButton`, `DontSaveButton|CancelButton`) or role.
- A `query` walks a ~2,000-node budget whatever `max_elements` says; on a big table the rows eat it
  and toolbar controls return 0 matches. Raise the budget: `max_depth:3, max_elements:8000,
  timeout_ms:10000` with the query.
- Top rows of a table: a plain read without query (`max_depth:5, max_elements:150`) gave 5 full
  rows for ~3.5k chars; fewer elements give rows without cells. A query matching a column's text
  (`" MB"`) returns every row (~70k chars): avoid.
- Tabs and segments may show without children in markdown: read `tree_format:"elements"`
  (`AXRadioButton`, subrole `AXSegment`, `selected`).
- Close button unlabelled: `tree_format:"elements"`, `query:"AXButton"`, `max_depth:1` shows
  `subrole: AXCloseButton`; none (Chess) → `invoke_menu ["Window","Close"]`.
- Empty an `AXSearchField` with its own cancel `AXButton` by element. `run_steps` `expect` cannot
  assert an empty value (`value:""` is rejected).
- AX values can carry invisible marks (Calculator: leading U+200E, no thousands separator).
  This Mac's locale shows **decimal commas** (`16,00 GB`, `572,9 MB`) and dot thousands separators.

**Mouse**
- Background `drag` lands (Chess moves) with no cursor or focus change. Foreground `drag` moves
  the real cursor along the path and **leaves it at the drop point**; the front app is restored.
- A background pixel `click` over an AX element is sent as `AXPress` ("PX hit-test pressed the
  background element via AX"), not a mouse event; use `drag` when you need a real gesture.
- AX frames can be flipped/projected (Chess 3D board): take coordinates from a PNG screenshot.

**Windows and apps**
- `set_window_frame` moves/resizes by AX in top-left coordinates, the same as `list_windows`; y=38 is just under the menu bar.
- `launch_app` without URLs: a running windowless TextEdit opens an Untitled document but returns
  placeholder ids (re-list; no File > New). Preview gets **no window at all**, so no
  menu, `hotkey` or `bring_to_front` works: launch it with a scratch document URL.
- `launch_app` with a document URL brings the app to the front (reports suppressed); without, not.
- Placeholder ids (tiny, `is_on_screen:false`, `ax_window_unresolved`): re-list `list_windows {pid}`.
- Finder's inline rename editor is its own window (`ax_window_unresolved`); only `type_text {pid,
  window_id:<editor>, delivery_mode:"foreground"}` reached it.
- `hotkey` does not reach a window on another Space (cmd+w did nothing): close by element.
- TextEdit text area: `click` by element fails (`AXPress … -25206`); `set_value ""`, then
  `type_text {element_token, text}`. Unicode types and reads back exactly.
- After File > New wait ~1.5 s; `ax_app_launching` → wait and re-read.
- Preview autosaves edits into the source within seconds; Revert To > Last Opened… restores it.
- `security_prompt_protected`: input would land on a consent prompt. Stop; the person's call.

**Shell ground truth**
- Never read another app's data from the shell (`~/Library/Containers/…`): macOS raises a privacy
  consent prompt on the person's screen and the command hangs. Check your own files, `pbpaste`, `pgrep`.
- Read-only AX attribute checks are fine: `AXSelectedTextRange`, `AXAttributedStringForRange` (bold
  runs), `AXFocusedUIElement` through pyobjc in `~/venvs/lab/bin/python`; never write AX from the shell.

## Clean up

- **Close what you open, nothing else.** Close button by element; on "Do you want to keep
  this new document?" press `Delete` by element for scratch documents you created; never
  discard the person's work. Quit only apps you launched, through their UI, **before closing their
  last window** (`invoke_menu ["<App>","Quit <App>"]`): a windowless app refuses `invoke_menu` and
  pid `hotkey`; if it is frontmost, `hotkey ["cmd","q"]` with `scope:"desktop"` quits it, else
  only `kill_app` (SIGKILL) is left. Calculator quits with its last window.
- Do not stop the Cua daemon, change its config, or run `cua-driver update`; updates go
  through `scripts/install_cua_driver.py` only.
- Report what you opened and closed, and anything left open with the reason.

## When something is off

- Tools missing or `permission` errors: `cua-driver permissions status --json` /
  `python scripts/install_cua_driver.py --check` (read-only). Granting permissions is for
  a person (`cua-driver permissions grant`); never change TCC yourself.
- `degraded_reason: screen_locked`: the Mac is locked; stop and report, do not type.
- Probing cursor / frontmost app yourself: `/usr/bin/python3` has no PyObjC (`No module
  named 'AppKit'`); call CoreGraphics / AppKit through ctypes, or use `~/venvs/lab/bin/python`.
- Without the MCP server but with a shell: `cua-driver call <tool> '<json>'` reaches the
  same daemon (~60 ms per process); use one `session` label on every call.
