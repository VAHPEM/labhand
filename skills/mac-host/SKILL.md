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
- **Load only the schemas you use** (the full list is 61 tools / ~41k tokens; the 14 core
  tools ~15k): `ToolSearch select:mcp__labhand-mac__list_windows,mcp__labhand-mac__get_window_state,mcp__labhand-mac__type_text`
  and add `click`, `set_value`, `press_key`, `hotkey`, `scroll`, `drag`, `launch_app`,
  `invoke_menu`, `list_apps`, `clipboard_read`, `clipboard_write` when needed.
- **Never touch** System Settings, password managers or Keychain unless the task says
  so. A person is using this Mac: work in the background, keep their cursor and front app.
- Pass one short `session` label (e.g. `"session":"t1"`) on every call that takes it.

## Observe cheaply

1. Find the window: `list_windows` with `app` (name or bundle id) or `pid`, never
   unfiltered. Off-Space windows are listed too (marked off-screen).
2. `get_window_state {pid, window_id, include_screenshot:false}` → a compact Markdown
   tree (1-2.5k tokens per window). Rows `[N]` are addressed as `element_token`
   `"<snapshot_id>:N"`.
3. Narrow before reading everything: `query:"Save|Cancel"` (matching rows + ancestors),
   `max_elements`, `max_depth`.
4. **Re-read with `since:"latest"`** after acting: only changed rows (`+`/`~`/`-`) come
   back. Use the NEW `snapshot_id` from that response in tokens.
5. A screenshot only when the tree cannot answer (canvas, video, custom-drawn UI, or you
   need pixels for a click). Cheaper picture: `include_accessibility_tree:false`,
   `max_dimension`, `screenshot_format:"jpeg"` (but a pixel click needs a PNG read).
6. Do not ask for `tree_format:"both"` or `full_output:true` (double size). The tree is
   also repeated in `structuredContent`; do not echo results back into your notes.

**Element tokens are snapshot-bound.** A new read of the same window replaces the old
snapshot; the session idles out after ~5 min. `stale_element_token` → observe again,
then act with the fresh token. Never reuse a token across a long wait.

## Act, then verify

Every action returns an `effect`: `confirmed`, `partial`, `unverifiable`,
`suspected_noop` or `refused`. **Unless it is `confirmed`, observe again**
(`since:"latest"`) and check the state yourself before the next step.

Action ladder, one rung at a time, checking state between rungs:

1. **By element** (`element_token`): no cursor, no focus change, works on background,
   minimized and off-Space windows. Prefer `set_value` / `type_text` by element over
   clicking into a field.
2. **By pixel** (`x`,`y` = pixels of a screenshot taken in the **same session**, less than
   ~60 s old, window not moved/resized): for elements missing from the tree or when the
   element action fails. No screenshot in the session → `screenshot_context_missing`.
3. **`delivery_mode:"foreground"`** only when 1 and 2 fail and the task allows a visible
   takeover: it fronts the window briefly and can move focus and the cursor.

## Measured quirks (GATE.md, 2026-11-18)

- **TextEdit text area:** `click` by element fails (`AXPress … -25206`, it only offers
  AXShowMenu). Type into it **by element without clicking**: `set_value ""` to clear,
  then `type_text {element_token, text}`. Unicode (`Xin chào`) types and reads back exactly.
- **`launch_app` with a document URL brings the app to the front**, although it reports
  activation suppressed. Launch without URLs when the person must keep their front app.
- **`hotkey` does not reach a window on another Space** (cmd+w did nothing). Close a
  window with its close button **by element**.
- **`invoke_menu` needs the target window to be the key/main window.**
- **After File > New wait ~1.5 s** before a screenshot / read; earlier reads hit
  `ax_unresolved` or an empty tree. `ax_app_launching` / `ax_window_unresolved` → wait and
  re-read.
- `query:"First Text View"` stopped matching once the area held text; read the value
  from the tree rows instead.
- `security_prompt_protected`: the input would land on a macOS security / consent
  prompt. Stop; that is the person's call.

## Clean up

- **Close what you open, nothing else.** Close button by element; on "Do you want to keep
  this new document?" press `Delete` by element for scratch documents you created; never
  discard the person's work. Quit only apps you launched, through their UI.
- Do not stop the Cua daemon, change its config, or run `cua-driver update`; updates go
  through `scripts/install_cua_driver.py` only.
- Report what you opened and closed, and anything left open with the reason.

## When something is off

- Tools missing or `permission` errors: `cua-driver permissions status --json` /
  `python scripts/install_cua_driver.py --check` (read-only). Granting permissions is for
  a person (`cua-driver permissions grant`); never change TCC yourself.
- `degraded_reason: screen_locked`: the Mac is locked; stop and report, do not type.
- Without the MCP server but with a shell: `cua-driver call <tool> '<json>'` reaches the
  same daemon (~60 ms per process); use one `session` label on every call.
