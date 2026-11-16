# -*- coding: utf-8 -*-
"""setup-project then teardown-project leaves every file it touched byte for byte as it was.

Found in a ccbridge run: CLAUDE.md ended in a blank line ("\\n\\n") and came back with one newline, so a
no-op task left a diff in a user file. `teardown_bytes` covers CLAUDE.md, `teardown_roundtrip` the
other files (.mcp.json, .claude/settings*.json, .claude/beacon) and a user edit made during the run."""
import json
import os
import stat
import sys

import pytest

from beacon.project import MARK_BEGIN, MARK_END, merge_claude_md, setup_project, strip_claude_md, teardown_project

WIRED = ("CLAUDE.md", ".mcp.json", ".claude/settings.local.json", ".claude/settings.json", ".claude/beacon")


def _setup(proj, **kw):
    assert setup_project(str(proj), sys.executable, sys.executable, **kw)


def _state(proj):
    """Bytes and permission bits of every non-backup file under proj."""
    out = {}
    for root, _, files in os.walk(proj):
        for f in files:
            p = os.path.join(root, f)
            if not p.endswith(".beacon-bak"):
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, proj)] = (fh.read(), stat.S_IMODE(os.stat(p).st_mode))
    return out


def _proj(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    return proj


# ---------------------------------------------------------------- CLAUDE.md (P1)
CLAUDE_ORIGINALS = {
    "no_newline": b"# My project\n\nKeep this text.",
    "one_newline": b"# My project\n\nKeep this text.\n",
    "blank_line": b"# My project\n\nKeep this text.\n\n",          # the case seen in the ccbridge run
    "two_blank_lines": b"# My project\n\nKeep this text.\n\n\n",
    "only_newlines": b"\n\n",
    "empty_file": b"",
    "crlf": b"# My project\r\n\r\nKeep this text.\r\n",
    "crlf_no_newline": b"# My project\r\nKeep this text.",
    "non_ascii": "# Projekt äöü — 日本\n".encode(),
}


@pytest.mark.parametrize("case", sorted(CLAUDE_ORIGINALS))
def test_teardown_bytes_claude_md_identical_after_roundtrip(tmp_path, case):
    proj = _proj(tmp_path)
    (proj / "CLAUDE.md").write_bytes(CLAUDE_ORIGINALS[case])
    _setup(proj)
    assert MARK_BEGIN.encode() in (proj / "CLAUDE.md").read_bytes()
    changes = teardown_project(str(proj))
    assert any(c.startswith("CLAUDE.md: removed worker rules") for c in changes)
    assert (proj / "CLAUDE.md").read_bytes() == CLAUDE_ORIGINALS[case]
    assert teardown_project(str(proj)) == []


@pytest.mark.parametrize("case", sorted(CLAUDE_ORIGINALS))
def test_teardown_bytes_claude_md_identical_after_setup_twice(tmp_path, case):
    proj = _proj(tmp_path)
    (proj / "CLAUDE.md").write_bytes(CLAUDE_ORIGINALS[case])
    _setup(proj)
    setup_project(str(proj), sys.executable, sys.executable)          # a re-run replaces the block in place
    teardown_project(str(proj))
    assert (proj / "CLAUDE.md").read_bytes() == CLAUDE_ORIGINALS[case]


def test_teardown_bytes_claude_md_mode_kept(tmp_path):
    proj = _proj(tmp_path)
    (proj / "CLAUDE.md").write_bytes(b"x\n\n")
    os.chmod(proj / "CLAUDE.md", 0o600)
    _setup(proj)
    teardown_project(str(proj))
    assert stat.S_IMODE(os.stat(proj / "CLAUDE.md").st_mode) == 0o600


def test_teardown_bytes_claude_md_created_by_setup_is_removed(tmp_path):
    proj = _proj(tmp_path)
    _setup(proj)
    assert (proj / "CLAUDE.md").exists()
    changes = teardown_project(str(proj))
    assert not (proj / "CLAUDE.md").exists()
    assert "CLAUDE.md: removed the file (nothing else was in it)" in changes


@pytest.mark.parametrize("original", ["x", "x\n", "x\n\n", "x\n\n\n", "x\n\n\n\n", ""])
def test_teardown_bytes_strip_inverts_merge(original):
    rules = "RULES\nmore\n"
    merged, _ = merge_claude_md(original, rules)
    assert strip_claude_md(merged, original)[0] == original
    # a stale backup (from an earlier run, another ending) never takes more than setup added
    for stale in ("y", "y\n", "y\n\n", ""):
        out = strip_claude_md(merged, stale)[0]
        assert out.startswith(original.rstrip("\n")) and merge_claude_md(out, rules)[0] == merged


def test_teardown_bytes_strip_without_backup_is_exact_where_the_file_tells():
    block = f"{MARK_BEGIN}\nrules\n{MARK_END}"
    # three or more newlines before the block: setup added none, so all of them were the user's
    assert strip_claude_md(f"x\n\n\n{block}\n")[0] == "x\n\n\n"
    assert strip_claude_md(f"x\n\n{block}\n")[0] == "x\n"                 # ambiguous: one newline, as before
    assert strip_claude_md(f"x\n\n{block}\n", "x\n\n")[0] == "x\n\n"      # the backup decides
    assert strip_claude_md(f"x\n\n{block}\n", "x")[0] == "x"


def test_teardown_bytes_without_backup_three_newlines(tmp_path):
    proj = _proj(tmp_path)
    (proj / "CLAUDE.md").write_bytes(b"x\n\n\n")
    _setup(proj)
    os.remove(proj / "CLAUDE.md.beacon-bak")
    teardown_project(str(proj))
    assert (proj / "CLAUDE.md").read_bytes() == b"x\n\n\n"


# ---------------------------------------------------------------- the other files (P2)
def _full_project(proj):
    (proj / ".claude").mkdir(parents=True)
    (proj / "CLAUDE.md").write_bytes(b"# My project\n\nKeep this text.\n\n")
    # formatting json.dumps(indent=2) would not reproduce: 4-space indent, odd key order, no final newline
    (proj / ".mcp.json").write_bytes(b'{\n    "mcpServers": {"labhand": {"command": "labhand-mcp", "args": []}}\n}')
    (proj / ".claude" / "settings.local.json").write_bytes(json.dumps({
        "permissions": {"deny": ["Bash(rm -rf *)"], "allow": ["Bash(ls *)"]},
        "model": "opus",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-own-stop-hook"}]}]}},
        separators=(",", ":")).encode() + b"\r\n")
    os.chmod(proj / ".mcp.json", 0o640)


def test_teardown_roundtrip_every_file_byte_identical(tmp_path):
    proj = _proj(tmp_path)
    _full_project(proj)
    before = _state(proj)
    _setup(proj)
    assert _state(proj) != before
    teardown_project(str(proj))
    assert _state(proj) == before
    assert teardown_project(str(proj)) == []


def test_teardown_roundtrip_shared_settings_json(tmp_path):
    proj = _proj(tmp_path)
    (proj / ".claude").mkdir()
    (proj / ".claude" / "settings.json").write_bytes(b'{ "model" : "opus" }\n\n')
    before = _state(proj)
    _setup(proj, shared=True)
    teardown_project(str(proj))
    assert _state(proj) == before


@pytest.mark.parametrize("content", [b"{}\n", b"", b"  \n"])
def test_teardown_roundtrip_file_that_held_nothing_is_restored_not_removed(tmp_path, content):
    proj = _proj(tmp_path)
    (proj / ".claude").mkdir()
    (proj / ".mcp.json").write_bytes(content)
    (proj / ".claude" / "settings.local.json").write_bytes(content)
    before = _state(proj)
    _setup(proj)
    teardown_project(str(proj))
    assert _state(proj) == before


def test_teardown_roundtrip_project_own_claude_beacon_is_restored(tmp_path):
    proj = _proj(tmp_path)
    (proj / ".claude").mkdir()
    (proj / ".claude" / "beacon").write_bytes(b"#!/bin/sh\necho my own lighthouse tool\n")
    os.chmod(proj / ".claude" / "beacon", 0o700)
    before = _state(proj)
    _setup(proj)
    assert b"-m beacon" in (proj / ".claude" / "beacon").read_bytes()
    changes = teardown_project(str(proj))
    assert ".claude/beacon: restored the project's own file setup had replaced" in changes
    assert _state(proj) == before


def test_teardown_roundtrip_fresh_project_files_removed(tmp_path):
    proj = _proj(tmp_path)
    _setup(proj)
    assert set(_state(proj)) == {"CLAUDE.md", ".mcp.json", ".claude/settings.local.json", ".claude/beacon"}
    teardown_project(str(proj))
    assert _state(proj) == {}


def test_teardown_roundtrip_second_cycle_ignores_the_first_cycles_backups(tmp_path):
    """Files setup created hold only beacon's parts: teardown removes them and leaves no backup behind, so
    a later setup has nothing to resurrect."""
    proj = _proj(tmp_path)
    _setup(proj)
    teardown_project(str(proj))
    assert not list(proj.glob("**/*.beacon-bak*"))
    _setup(proj)
    teardown_project(str(proj))
    assert _state(proj) == {}


@pytest.mark.parametrize("original,edit,expected", [
    # user prepends a line during the run
    (b"a\n", lambda t: "x\n" + t, b"x\na\n"),
    (b"a", lambda t: "x\n" + t, b"x\na"),
    (b"a\n\n", lambda t: "x\n" + t, b"x\na\n\n"),
    # user appends a line after the block
    (b"a\n", lambda t: t + "more\n", b"a\nmore\n"),
    # user edits a line in the middle of their text
    (b"one\ntwo\n\n", lambda t: t.replace("two", "TWO"), b"one\nTWO\n\n"),
])
def test_teardown_roundtrip_user_edit_outside_block_is_kept(tmp_path, original, edit, expected):
    proj = _proj(tmp_path)
    _full_project(proj)
    (proj / "CLAUDE.md").write_bytes(original)
    before = _state(proj)
    _setup(proj)
    cp = proj / "CLAUDE.md"
    cp.write_text(edit(cp.read_text(encoding="utf-8")), encoding="utf-8")
    teardown_project(str(proj))
    after = _state(proj)
    assert after["CLAUDE.md"][0] == expected
    assert MARK_BEGIN.encode() not in expected
    for rel in before:
        if rel != "CLAUDE.md":
            assert after[rel] == before[rel], rel
    assert set(after) == set(before)


def test_teardown_roundtrip_user_edit_to_json_keeps_the_edit(tmp_path):
    """A JSON file changed outside beacon's entries cannot come back as the old bytes: the edit wins."""
    proj = _proj(tmp_path)
    _full_project(proj)
    _setup(proj)
    mp = proj / ".mcp.json"
    cfg = json.loads(mp.read_text())
    cfg["mcpServers"]["mine"] = {"command": "x"}
    mp.write_text(json.dumps(cfg))
    teardown_project(str(proj))
    assert json.loads(mp.read_text()) == {"mcpServers": {"labhand": {"command": "labhand-mcp", "args": []},
                                                        "mine": {"command": "x"}}}


def test_teardown_roundtrip_dry_run_changes_nothing(tmp_path):
    proj = _proj(tmp_path)
    _full_project(proj)
    (proj / ".claude" / "beacon").write_bytes(b"mine\n")
    _setup(proj)
    wired = _state(proj)
    assert teardown_project(str(proj), dry_run=True)
    assert _state(proj) == wired


# ---------------------------------------------------------------- review round 2
@pytest.mark.parametrize("original,expected", [
    (b"last line", b"last line\nuser line\n"),               # no final newline: the head keeps a line break
    (b"# Notes\nlast line", b"# Notes\nlast line\nuser line\n"),
    (b"last line\n", b"last line\nuser line\n"),
    (b"last line\n\n", b"last line\n\nuser line\n"),
    (b"", b"user line\n"),
])
def test_teardown_bytes_user_line_after_block_is_not_glued(tmp_path, original, expected):
    proj = _proj(tmp_path)
    cp = proj / "CLAUDE.md"
    cp.write_bytes(original)
    _setup(proj)
    cp.write_bytes(cp.read_bytes() + b"user line\n")
    teardown_project(str(proj))
    assert cp.read_bytes() == expected


def test_teardown_bytes_strip_never_glues_head_to_tail():
    for original in ("x", "a\r\nx"):
        merged, _ = merge_claude_md(original, "rules")
        out, _ = strip_claude_md(merged + "tail\n", original)
        assert out == original + "\ntail\n"


SETTINGS_LOCAL = os.path.join(".claude", "settings.local.json")


def _settings_edit_keeps(tmp_path, original, edit):
    """Setup, let the user rewrite the local settings with `edit(cfg) -> bytes`, teardown; -> the file's bytes."""
    proj = _proj(tmp_path)
    sp = proj / SETTINGS_LOCAL
    sp.parent.mkdir()
    sp.write_bytes(original)
    _setup(proj)
    sp.write_bytes(edit(json.loads(sp.read_bytes())))
    teardown_project(str(proj))
    return sp.read_bytes()


def test_teardown_roundtrip_json_key_reorder_by_user_is_kept(tmp_path):
    orig = b'{\n  "permissions": {"allow": ["Bash(ls)"]},\n  "model": "opus"\n}\n'
    out = _settings_edit_keeps(tmp_path, orig, lambda c: json.dumps({k: c[k] for k in reversed(list(c))}).encode())
    assert out != orig
    assert list(json.loads(out)) == ["model", "permissions"]


@pytest.mark.parametrize("value", [True, 1.0])
def test_teardown_roundtrip_json_value_type_change_by_user_is_kept(tmp_path, value):
    orig = b'{"cleanupPeriodDays": 1}\n'

    def edit(c):
        c["cleanupPeriodDays"] = value
        return json.dumps(c).encode()
    out = _settings_edit_keeps(tmp_path, orig, edit)
    assert out != orig
    assert json.dumps(json.loads(out)["cleanupPeriodDays"]) == json.dumps(value)


def test_teardown_roundtrip_unchanged_json_still_gets_the_original_bytes(tmp_path):
    orig = b'{ "model":"opus",  "cleanupPeriodDays": 1.0 }'
    assert _settings_edit_keeps(tmp_path, orig, lambda c: json.dumps(c).encode()) == orig


SYMLINKED = ("CLAUDE.md", ".mcp.json", SETTINGS_LOCAL)


def _symlinked_project(proj):
    """Every wired file is a relative symlink into shared/, which holds the real content (mode 0600)."""
    (proj / ".claude").mkdir()
    (proj / "shared").mkdir()
    content = {"CLAUDE.md": b"shared rules\n", ".mcp.json": b'{"mcpServers": {"x": {"command": "x"}}}',
               SETTINGS_LOCAL: b'{"model": "opus"}\n'}
    for rel in SYMLINKED:
        target = proj / "shared" / rel.replace(os.sep, "_")
        target.write_bytes(content[rel])
        os.chmod(target, 0o600)
        os.symlink(os.path.relpath(target, (proj / rel).parent), proj / rel)


def _links(proj):
    return {rel: os.readlink(proj / rel) for rel in SYMLINKED if os.path.islink(proj / rel)}


def test_teardown_roundtrip_symlinked_files_stay_links(tmp_path):
    proj = _proj(tmp_path)
    _symlinked_project(proj)
    before, links = _state(proj), _links(proj)
    assert len(links) == 3
    _setup(proj)
    assert _links(proj) == links                                  # setup wrote through the links
    assert MARK_BEGIN.encode() in (proj / "shared" / "CLAUDE.md").read_bytes()
    teardown_project(str(proj))
    assert _links(proj) == links
    assert _state(proj) == before


def test_teardown_roundtrip_symlinked_files_with_user_edit(tmp_path):
    """The rewrite path (no restore from the backup) also keeps the link and the target's mode."""
    proj = _proj(tmp_path)
    _symlinked_project(proj)
    links = _links(proj)
    _setup(proj)
    cp = proj / "CLAUDE.md"
    cp.write_bytes(cp.read_bytes().replace(b"shared rules", b"shared rules, edited"))
    mp = proj / ".mcp.json"
    cfg = json.loads(mp.read_bytes())
    cfg["mcpServers"]["mine"] = {"command": "y"}
    mp.write_bytes(json.dumps(cfg).encode())
    teardown_project(str(proj))
    assert _links(proj) == links
    assert (proj / "shared" / "CLAUDE.md").read_bytes() == b"shared rules, edited\n"
    assert set(json.loads(mp.read_bytes())["mcpServers"]) == {"x", "mine"}
    for name in ("CLAUDE.md", ".mcp.json"):
        assert stat.S_IMODE(os.stat(proj / "shared" / name).st_mode) == 0o600, name


def test_teardown_roundtrip_dangling_symlink_target_created_then_removed(tmp_path):
    proj = _proj(tmp_path)
    (proj / "shared").mkdir()
    os.symlink("shared/rules.md", proj / "CLAUDE.md")
    _setup(proj)
    assert os.path.islink(proj / "CLAUDE.md")
    assert MARK_BEGIN.encode() in (proj / "shared" / "rules.md").read_bytes()
    teardown_project(str(proj))
    assert os.readlink(proj / "CLAUDE.md") == "shared/rules.md"
    assert not (proj / "shared" / "rules.md").exists()


@pytest.mark.parametrize("rel,original,edit", [
    ("CLAUDE.md", b"secret\n", lambda b: b.replace(b"secret", b"secret2")),
    (".mcp.json", b'{"mcpServers": {}}\n', lambda b: b.replace(b'"mcpServers"', b'"mine": 1, "mcpServers"', 1)),
    (SETTINGS_LOCAL, b'{"model": "opus"}\n', lambda b: b.replace(b'"opus"', b'"sonnet"')),
])
def test_teardown_roundtrip_rewritten_file_keeps_mode(tmp_path, rel, original, edit):
    proj = _proj(tmp_path)
    path = proj / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original)
    os.chmod(path, 0o600)
    _setup(proj)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600           # setup's rewrite kept it
    path.write_bytes(edit(path.read_bytes()))
    teardown_project(str(proj))
    assert path.read_bytes() != original                          # the user's edit, not the backup
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
