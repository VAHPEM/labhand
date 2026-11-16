# -*- coding: utf-8 -*-
"""The .beacon-bak lifecycle: no stale backup is left behind, and the only copy of a file is never lost.

`cleanup`: teardown removes a backup only when the final file equals it (bytes and permission bits; for a
symlink the target's content) and keeps and names it otherwise; --dry-run reports the same.
`edge`: at setup a backup from an older run is moved aside (never deleted, never overwriting) and a fresh
one taken; re-runs stay idempotent; the CLI says so."""
import json
import os
import stat
import sys

import pytest

from beacon import cli
from beacon.project import MARK_BEGIN, setup_project, teardown_project

WIRED = ("CLAUDE.md", ".mcp.json", ".claude/settings.local.json", ".claude/settings.json", ".claude/beacon")
MCP = ".mcp.json"
SETTINGS = ".claude/settings.local.json"


def _setup(proj, python=sys.executable, **kw):
    return setup_project(str(proj), python, python, **kw)


def _files(proj):
    """Bytes and permission bits of every file under proj (backups included), links reported as links."""
    out = {}
    for root, _, files in os.walk(proj):
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, proj)
            if os.path.islink(p):
                out[rel] = ("link", os.readlink(p))
            else:
                with open(p, "rb") as fh:
                    out[rel] = (fh.read(), stat.S_IMODE(os.stat(p).st_mode))
    return out


def _baks(proj):
    return sorted(os.path.relpath(str(p), proj) for p in proj.glob("**/*.beacon-bak*"))


def _no_baks(state):
    return {k: v for k, v in state.items() if ".beacon-bak" not in k}


def _full_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / "CLAUDE.md").write_bytes(b"# My project\n\nKeep this text.\n\n")
    (proj / MCP).write_text(json.dumps({"mcpServers": {"labhand": {"command": "labhand-mcp"}}}, indent=4))
    (proj / SETTINGS).write_text(json.dumps({"model": "opus", "permissions": {"allow": ["Bash(ls *)"]}}))
    os.chmod(proj / "CLAUDE.md", 0o640)
    os.chmod(proj / SETTINGS, 0o600)
    return proj


# ---------------------------------------------------------------- cleanup (P1)
def test_beacon_bak_cleanup_roundtrip_leaves_no_backup(tmp_path):
    proj = _full_project(tmp_path)
    before = _files(proj)
    _setup(proj)
    assert _baks(proj) == sorted([".claude/settings.local.json.beacon-bak", ".mcp.json.beacon-bak",
                                  "CLAUDE.md.beacon-bak"])
    changes = teardown_project(str(proj))
    assert _files(proj) == before                         # byte for byte, modes too, and no backup left
    for name in ("CLAUDE.md", MCP, SETTINGS):
        assert f"{name}.beacon-bak: removed the backup ({name} is back to it byte for byte)" in changes
    assert teardown_project(str(proj)) == []              # teardown twice: nothing left to do


def test_beacon_bak_cleanup_fresh_project_leaves_nothing(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _setup(proj)
    changes = teardown_project(str(proj))
    assert _files(proj) == {}
    assert not any("beacon-bak" in c for c in changes)    # a file setup created has no backup to settle


def test_beacon_bak_cleanup_user_edit_keeps_and_names_the_backup(tmp_path):
    proj = _full_project(tmp_path)
    original = (proj / "CLAUDE.md").read_bytes()
    _setup(proj)
    (proj / "CLAUDE.md").write_text("Added during the run.\n" + (proj / "CLAUDE.md").read_text())
    changes = teardown_project(str(proj))
    assert (proj / "CLAUDE.md").read_bytes() == b"Added during the run.\n" + original
    assert (proj / "CLAUDE.md.beacon-bak").read_bytes() == original           # the pre-run state survives
    assert any(c.startswith("CLAUDE.md.beacon-bak: kept the backup: CLAUDE.md differs from it") for c in changes)
    assert _baks(proj) == ["CLAUDE.md.beacon-bak"]                             # the untouched files: removed


def test_beacon_bak_cleanup_json_user_edit_keeps_the_backup(tmp_path):
    proj = _full_project(tmp_path)
    original = (proj / SETTINGS).read_bytes()
    _setup(proj)
    s = json.loads((proj / SETTINGS).read_text())
    s["permissions"]["allow"].append("Read")
    (proj / SETTINGS).write_text(json.dumps(s, indent=2))
    changes = teardown_project(str(proj))
    assert (proj / (SETTINGS + ".beacon-bak")).read_bytes() == original
    assert any(c.startswith(SETTINGS + ".beacon-bak: kept the backup") for c in changes)
    assert json.loads((proj / SETTINGS).read_text())["permissions"]["allow"] == ["Bash(ls *)", "Read"]


def test_beacon_bak_cleanup_mode_difference_keeps_the_backup(tmp_path):
    """Same bytes, other permission bits: not the same file, the backup stays (a leftover from an older run)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_bytes(b"text\n")
    (proj / "CLAUDE.md.beacon-bak").write_bytes(b"text\n")
    os.chmod(proj / "CLAUDE.md", 0o644)
    os.chmod(proj / "CLAUDE.md.beacon-bak", 0o600)
    changes = teardown_project(str(proj))
    assert (proj / "CLAUDE.md.beacon-bak").exists()
    assert changes == ["CLAUDE.md.beacon-bak: kept the backup: same content as CLAUDE.md, but the permission "
                       "bits differ (644 in the file, 600 in the backup)"]
    os.chmod(proj / "CLAUDE.md.beacon-bak", 0o644)        # now an exact duplicate: removed
    assert teardown_project(str(proj)) == ["CLAUDE.md.beacon-bak: removed the backup (CLAUDE.md is back to it "
                                           "byte for byte)"]
    assert not (proj / "CLAUDE.md.beacon-bak").exists()


def test_beacon_bak_cleanup_removed_file_keeps_the_only_copy(tmp_path):
    """Teardown removes a file left empty while its backup differs (the user took their own server out
    during the run): the backup is then the only copy."""
    proj = tmp_path / "proj"
    proj.mkdir()
    orig = '{"mcpServers": {"mine": {"command": "x"}}}'
    (proj / MCP).write_text(orig)
    _setup(proj)
    cfg = json.loads((proj / MCP).read_text())
    del cfg["mcpServers"]["mine"]
    (proj / MCP).write_text(json.dumps(cfg))
    changes = teardown_project(str(proj))
    assert not (proj / MCP).exists()
    assert (proj / (MCP + ".beacon-bak")).read_text() == orig
    assert (MCP + ".beacon-bak: kept the backup: " + MCP + " was removed, so the backup holds the only copy of "
            "its content") in changes


def test_beacon_bak_cleanup_setup_created_file_edited_keeps_teardowns_copy(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _setup(proj)
    wired = (proj / SETTINGS).read_bytes()
    s = json.loads(wired)
    s["model"] = "opus"
    (proj / SETTINGS).write_text(json.dumps(s, indent=2) + "\n")
    wired = (proj / SETTINGS).read_bytes()
    changes = teardown_project(str(proj))
    assert json.loads((proj / SETTINGS).read_text()) == {"model": "opus"}
    assert (proj / (SETTINGS + ".beacon-bak")).read_bytes() == wired
    assert any(c.startswith(SETTINGS + ".beacon-bak: kept the backup: teardown's copy") for c in changes)


@pytest.mark.parametrize("edit", [False, True])
def test_beacon_bak_cleanup_symlink_target_compared_link_untouched(tmp_path, edit):
    proj = tmp_path / "proj"
    shared = tmp_path / "shared"
    proj.mkdir()
    shared.mkdir()
    (shared / "rules.md").write_bytes(b"shared rules\n")
    os.chmod(shared / "rules.md", 0o600)
    os.symlink(shared / "rules.md", proj / "CLAUDE.md")
    _setup(proj)
    if edit:
        (proj / "CLAUDE.md").write_text("mine\n" + (proj / "CLAUDE.md").read_text())
    changes = teardown_project(str(proj))
    assert os.path.islink(proj / "CLAUDE.md") and os.readlink(proj / "CLAUDE.md") == str(shared / "rules.md")
    assert stat.S_IMODE(os.stat(shared / "rules.md").st_mode) == 0o600
    if edit:
        assert (shared / "rules.md").read_bytes() == b"mine\nshared rules\n"
        assert (proj / "CLAUDE.md.beacon-bak").read_bytes() == b"shared rules\n"
        assert any(c.startswith("CLAUDE.md.beacon-bak: kept the backup") for c in changes)
    else:
        assert (shared / "rules.md").read_bytes() == b"shared rules\n"
        assert not (proj / "CLAUDE.md.beacon-bak").exists()
        assert "CLAUDE.md.beacon-bak: removed the backup (CLAUDE.md is back to it byte for byte)" in changes
    assert sorted(os.listdir(shared)) == ["rules.md"]


@pytest.mark.parametrize("scenario", ["clean", "edited", "fresh", "setup_created_edited", "removed"])
def test_beacon_bak_cleanup_dry_run_reports_what_the_real_run_does(tmp_path, scenario):
    if scenario == "fresh" or scenario == "setup_created_edited":
        proj = tmp_path / "proj"
        proj.mkdir()
    elif scenario == "removed":
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / MCP).write_text('{"mcpServers": {"mine": {"command": "x"}}}')
    else:
        proj = _full_project(tmp_path)
    _setup(proj)
    if scenario == "removed":                             # the user takes their server out: file left empty
        cfg = json.loads((proj / MCP).read_text())
        del cfg["mcpServers"]["mine"]
        (proj / MCP).write_text(json.dumps(cfg))
    if scenario == "edited":
        (proj / "CLAUDE.md").write_text((proj / "CLAUDE.md").read_text() + "more\n")
    if scenario == "setup_created_edited":
        s = json.loads((proj / SETTINGS).read_text())
        s["model"] = "opus"
        (proj / SETTINGS).write_text(json.dumps(s))
    state = _files(proj)
    preview = teardown_project(str(proj), dry_run=True)
    assert _files(proj) == state                          # a dry run touches nothing
    assert preview == teardown_project(str(proj))         # and says exactly what the real run then did
    assert any("beacon-bak" in c for c in preview) == (scenario != "fresh")


def test_beacon_bak_cleanup_failure_mid_teardown_keeps_every_backup(tmp_path):
    proj = _full_project(tmp_path)
    before = _files(proj)
    _setup(proj)
    wired = (proj / "CLAUDE.md").read_bytes()
    (proj / "CLAUDE.md").write_bytes(wired + b"\xff\xfe not utf-8\n")        # teardown fails on CLAUDE.md
    with pytest.raises(UnicodeDecodeError):
        teardown_project(str(proj))
    assert len(_baks(proj)) == 3                          # the JSON files were restored, their backups stay
    (proj / "CLAUDE.md").write_bytes(wired)
    teardown_project(str(proj))                           # the re-run finishes the job
    assert _files(proj) == before


def test_beacon_bak_cleanup_cli_names_kept_backups(tmp_path, monkeypatch, capsys):
    proj = _full_project(tmp_path)
    _setup(proj)
    (proj / "CLAUDE.md").write_text((proj / "CLAUDE.md").read_text() + "more\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    assert cli.main(["teardown-project", str(proj), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Would change:" in out and "Would keep CLAUDE.md.beacon-bak:" in out
    assert "removed the backup (" + MCP + " is back to it byte for byte)" in out
    assert cli.main(["teardown-project", str(proj)]) == 0
    out = capsys.readouterr().out
    assert "Kept CLAUDE.md.beacon-bak:" in out and "kept the backup: CLAUDE.md differs from it" in out


# ---------------------------------------------------------------- edge (P2)
def test_beacon_bak_edge_stale_backup_moved_aside_and_fresh_one_taken(tmp_path):
    proj = _full_project(tmp_path)
    (proj / "CLAUDE.md.beacon-bak").write_bytes(b"from an older run\n")
    before = _no_baks(_files(proj))
    changes = _setup(proj)
    assert ("CLAUDE.md: moved an older backup aside to CLAUDE.md.beacon-bak.old (it differed from the file, so it "
            "is from an earlier run) and backed up the file to CLAUDE.md.beacon-bak") in changes
    assert (proj / "CLAUDE.md.beacon-bak.old").read_bytes() == b"from an older run\n"
    assert (proj / "CLAUDE.md.beacon-bak").read_bytes() == before["CLAUDE.md"][0]
    teardown_project(str(proj))
    assert _no_baks(_files(proj)) == before               # restored from this run's state, not the stale one
    assert _baks(proj) == ["CLAUDE.md.beacon-bak.old"]    # never deleted


def test_beacon_bak_edge_never_overwrites_an_old_backup(tmp_path):
    proj = _full_project(tmp_path)
    (proj / "CLAUDE.md.beacon-bak").write_bytes(b"third\n")
    (proj / "CLAUDE.md.beacon-bak.old").write_bytes(b"first\n")
    (proj / "CLAUDE.md.beacon-bak.old.1").write_bytes(b"second\n")
    changes = _setup(proj)
    assert any("aside to CLAUDE.md.beacon-bak.old.2 " in c for c in changes)
    assert [(proj / f"CLAUDE.md.beacon-bak.old{s}").read_bytes() for s in ("", ".1", ".2")] == \
        [b"first\n", b"second\n", b"third\n"]


def test_beacon_bak_edge_equal_backup_is_reused(tmp_path):
    proj = _full_project(tmp_path)
    orig = proj / "CLAUDE.md"
    (proj / "CLAUDE.md.beacon-bak").write_bytes(orig.read_bytes())
    os.chmod(proj / "CLAUDE.md.beacon-bak", stat.S_IMODE(os.stat(orig).st_mode))
    changes = _setup(proj)
    assert not any("older backup" in c for c in changes)
    assert not (proj / "CLAUDE.md.beacon-bak.old").exists()


def test_beacon_bak_edge_same_bytes_other_mode_is_stale(tmp_path):
    proj = _full_project(tmp_path)
    (proj / "CLAUDE.md.beacon-bak").write_bytes((proj / "CLAUDE.md").read_bytes())
    os.chmod(proj / "CLAUDE.md.beacon-bak", 0o666)
    changes = _setup(proj)
    assert any(c.startswith("CLAUDE.md: moved an older backup aside") for c in changes)
    assert stat.S_IMODE(os.stat(proj / "CLAUDE.md.beacon-bak").st_mode) == 0o640


def test_beacon_bak_edge_stale_backup_of_a_missing_file(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / (MCP + ".beacon-bak")).write_text('{"mcpServers": {"old": {}}}')
    changes = _setup(proj)
    assert (MCP + ": moved an older backup aside to " + MCP + ".beacon-bak.old (the file did not exist, so it is "
            "from an earlier run)") in changes
    assert not (proj / (MCP + ".beacon-bak")).exists()
    teardown_project(str(proj))
    assert not (proj / MCP).exists()                      # setup created it: removed
    assert _baks(proj) == [MCP + ".beacon-bak.old"]


def test_beacon_bak_edge_dry_run_setup_moves_nothing(tmp_path):
    proj = _full_project(tmp_path)
    (proj / "CLAUDE.md.beacon-bak").write_bytes(b"old\n")
    state = _files(proj)
    changes = _setup(proj, dry_run=True)
    assert _files(proj) == state
    assert any("moved an older backup aside to CLAUDE.md.beacon-bak.old" in c for c in changes)


def test_beacon_bak_edge_setup_twice_and_teardown_twice(tmp_path):
    proj = _full_project(tmp_path)
    before = _files(proj)
    _setup(proj)
    wired = _files(proj)
    assert _setup(proj) == []
    assert _files(proj) == wired
    teardown_project(str(proj))
    assert teardown_project(str(proj)) == []
    assert _files(proj) == before


def test_beacon_bak_edge_interrupted_run_keeps_the_real_pre_run_backup(tmp_path):
    """Setup again on a still-wired project (an interrupted run, here with other paths): the backup is the
    real pre-run state; it is neither moved aside nor replaced by the wired file."""
    proj = _full_project(tmp_path)
    before = _files(proj)
    _setup(proj)
    baks = {k: v for k, v in _files(proj).items() if k.endswith(".beacon-bak")}
    changes = _setup(proj, python="/other/python3")
    assert changes and not any("older backup" in c for c in changes)
    assert {k: v for k, v in _files(proj).items() if ".beacon-bak" in k} == baks
    assert MARK_BEGIN.encode() in (proj / "CLAUDE.md").read_bytes()
    teardown_project(str(proj))
    assert _files(proj) == before


def test_beacon_bak_edge_old_teardown_leftovers_then_new_cycle(tmp_path):
    """Backups an older beacon left (setup's pre-run copy kept, teardown's copy of a removed file) do not
    stand in for the next run, and the next cycle ends clean apart from what was moved aside."""
    proj = _full_project(tmp_path)
    before = _files(proj)
    (proj / ".claude" / "beacon.beacon-bak").write_text('#!/bin/sh\nexec python -m beacon "$@"\n')
    (proj / "CLAUDE.md.beacon-bak").write_bytes(b"# My project\n\nOld text.\n")
    _setup(proj)
    teardown_project(str(proj))
    assert _no_baks(_files(proj)) == before
    assert _baks(proj) == ["CLAUDE.md.beacon-bak.old"]        # a wrapper backup is beacon's own file: removed


def test_beacon_bak_edge_cli_setup_message(tmp_path, capsys):
    proj = _full_project(tmp_path)
    (proj / "CLAUDE.md.beacon-bak").write_bytes(b"old\n")
    assert cli.main(["setup-project", str(proj)]) == 0
    out = capsys.readouterr().out
    assert "moved an older backup aside to CLAUDE.md.beacon-bak.old" in out
    assert "this run's pre-run state" in out and "keeps and names the others" in out


def test_beacon_bak_ownfile_foreign_command_file_is_left_alone(tmp_path):
    """.claude/beacon replaced during the run by something that is not beacon's command: it is the
    project's, teardown leaves it in place and keeps no backup of it."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _setup(proj)
    (proj / ".claude" / "beacon").write_bytes(b"#!/bin/sh\necho mine\n")
    changes = teardown_project(str(proj))
    assert (proj / ".claude" / "beacon").read_bytes() == b"#!/bin/sh\necho mine\n"
    assert not any(c.startswith(".claude/beacon") for c in changes)
    assert _baks(proj) == []


# ---------------------------------------------------------------- emptyjson
# A file that held only an empty object (or empty text) before setup is the user's file: teardown puts it
# back byte for byte; "removed the file (nothing else was in it)" is only for a file setup created.
EMPTY_CASES = [
    (MCP, '{"mcpServers": {}}'),
    (MCP, "{}"),
    (MCP, '{\n  "mcpServers": {}\n}\n'),
    (MCP, ""),
    (SETTINGS, "{}"),
    (SETTINGS, '{"hooks": {}}'),
    (SETTINGS, '{"permissions": {"allow": [], "deny": []}}'),
    (SETTINGS, '{"permissions": {}, "enabledMcpjsonServers": []}'),
    (SETTINGS, '{"hooks": {"Stop": []}}'),
    (SETTINGS, '{"model": "opus", "hooks": {}}'),
    (".claude/settings.json", '{"hooks": {}, "permissions": {}}'),
    ("CLAUDE.md", ""),
    ("CLAUDE.md", "\n\n"),
    ("CLAUDE.md", "   "),
]


def _empty_project(tmp_path, rel, content):
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    if rel != "CLAUDE.md":
        (proj / "CLAUDE.md").write_text("# p\n")
    (proj / rel).write_text(content)
    os.chmod(proj / rel, 0o640)
    return proj


@pytest.mark.parametrize("rel,content", EMPTY_CASES)
def test_beacon_bak_emptyjson_file_is_restored_not_removed(tmp_path, rel, content):
    proj = _empty_project(tmp_path, rel, content)
    before = _files(proj)
    _setup(proj, shared=rel == ".claude/settings.json")
    out = teardown_project(str(proj))
    assert not any(c.startswith(rel + ":") and "removed the file" in c for c in out), out
    assert _files(proj) == before, out          # back byte for byte (mode too), no backup left
    assert _baks(proj) == []


@pytest.mark.parametrize("rel,content", EMPTY_CASES)
def test_beacon_bak_emptyjson_two_cycles_no_old_backup(tmp_path, rel, content):
    proj = _empty_project(tmp_path, rel, content)
    before = _files(proj)
    for _ in range(2):
        setup_out = _setup(proj, shared=rel == ".claude/settings.json")
        assert not any("moved an older backup" in c for c in setup_out), setup_out
        teardown_project(str(proj))
        assert _files(proj) == before
        assert _baks(proj) == []


def test_beacon_bak_emptyjson_dry_run_matches_real_run(tmp_path):
    proj = _empty_project(tmp_path, MCP, '{"mcpServers": {}}')
    _setup(proj)
    dry = teardown_project(str(proj), dry_run=True)
    real = teardown_project(str(proj))
    assert dry == real
    assert MCP + ": restored the file as it was before setup" in real
    assert (proj / MCP).read_text() == '{"mcpServers": {}}'


def test_beacon_bak_emptyjson_git_shows_no_change(tmp_path):
    """The orchestrator's reproduction: a committed .mcp.json = {"mcpServers": {}} and settings {} survive
    two setup + teardown cycles with `git status` clean."""
    import shutil
    import subprocess
    if not shutil.which("git"):
        pytest.skip("no git")
    proj = _empty_project(tmp_path, MCP, '{"mcpServers": {}}')
    (proj / SETTINGS).write_text("{}")
    g = ["git", "-C", str(proj), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(g + ["init", "-q"], check=True)
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-qm", "init"], check=True)
    for _ in range(2):
        _setup(proj)
        teardown_project(str(proj))
        st = subprocess.run(g + ["status", "--porcelain"], check=True, capture_output=True, text=True).stdout
        assert st == "", st


def test_beacon_bak_emptyjson_wired_backup_is_not_put_back(tmp_path):
    """A backup that still holds beacon's server is not 'equal once empty containers are dropped':
    restoring it would wire beacon again. It is kept, as the only copy."""
    proj = _empty_project(tmp_path, MCP, '{"mcpServers": {}}')
    _setup(proj)
    wired = (proj / MCP).read_text()
    (proj / (MCP + ".beacon-bak")).write_text(wired)
    out = teardown_project(str(proj))
    assert not (proj / MCP).exists(), out
    assert (proj / (MCP + ".beacon-bak")).read_text() == wired


# ---------------------------------------------------------------- ownfile (R15 F4, F1)
# Teardown removes .claude/beacon only when it IS beacon's wrapper; a backup that is itself a wrapper is not
# a copy of anything the project owns and never stands in for the project's file.
WRAP = b'#!/bin/sh\nPYTHONPATH="/old" exec "/old/python" -m beacon "$@"\n'
OWN = b"#!/bin/sh\necho project tool\n"


def test_beacon_bak_ownfile_second_teardown_keeps_the_projects_file(tmp_path):
    """F4 inverted: setup, teardown, a second teardown (a no-op), setup, teardown: the project's own
    .claude/beacon is there each time and never becomes a .old."""
    proj = tmp_path / "p"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "beacon").write_bytes(OWN)
    _setup(proj)
    teardown_project(str(proj))
    assert (proj / ".claude" / "beacon").read_bytes() == OWN
    assert _baks(proj) == []
    assert teardown_project(str(proj)) == []                  # the second teardown is a no-op
    assert (proj / ".claude" / "beacon").read_bytes() == OWN
    out = _setup(proj)
    assert not any("moved an older backup" in c for c in out), out
    teardown_project(str(proj))
    assert (proj / ".claude" / "beacon").read_bytes() == OWN
    assert _baks(proj) == []


def test_beacon_bak_ownfile_three_cycles_byte_for_byte(tmp_path):
    proj = tmp_path / "p"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "beacon").write_bytes(OWN)
    os.chmod(proj / ".claude" / "beacon", 0o700)
    (proj / "CLAUDE.md").write_text("# p\n")
    before = _files(proj)
    for _ in range(3):
        _setup(proj)
        assert b"-m beacon" in (proj / ".claude" / "beacon").read_bytes()
        teardown_project(str(proj))
        teardown_project(str(proj))
        assert _files(proj) == before                         # no .old, no backup, the file byte for byte


def test_beacon_bak_ownfile_wrapper_backup_never_stands_in(tmp_path):
    """F1 inverted: .claude/beacon.beacon-bak holds a wrapper (an older beacon / an interrupted run) and
    the run puts the project's own script at .claude/beacon: teardown keeps the script, drops the wrapper."""
    proj = tmp_path / "p"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "beacon").write_bytes(WRAP)
    (proj / ".claude" / "beacon.beacon-bak").write_bytes(WRAP)
    out = _setup(proj)
    assert any("held beacon's own command" in c for c in out), out
    mine = b"#!/bin/sh\necho the project's own command\n"
    (proj / ".claude" / "beacon").write_bytes(mine)
    preview = teardown_project(str(proj), dry_run=True)
    out = teardown_project(str(proj))
    assert preview == out
    assert (proj / ".claude" / "beacon").read_bytes() == mine
    assert not any("only copy" in c for c in out), out
    assert _baks(proj) == []


def test_beacon_bak_ownfile_wrapper_backup_left_at_teardown(tmp_path):
    """A wrapper backup still there at teardown (setup of an older beacon): the wrapper file is removed, the
    wrapper backup too, and nothing claims it holds the only copy of anything."""
    proj = tmp_path / "p"
    proj.mkdir()
    _setup(proj)
    (proj / ".claude" / "beacon.beacon-bak").write_bytes(WRAP)
    out = teardown_project(str(proj))
    assert not (proj / ".claude" / "beacon").exists() and _baks(proj) == []
    assert ".claude/beacon.beacon-bak: removed the backup (it held beacon's own command, not a file of the " \
           "project's)" in out
    assert not any("only copy" in c for c in out)


def test_beacon_bak_ownfile_setup_backs_up_the_projects_file_over_a_wrapper_backup(tmp_path):
    proj = tmp_path / "p"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "beacon").write_bytes(OWN)
    (proj / ".claude" / "beacon.beacon-bak").write_bytes(WRAP)
    assert _setup(proj, dry_run=True)
    assert (proj / ".claude" / "beacon.beacon-bak").read_bytes() == WRAP     # a dry run touches nothing
    _setup(proj)
    assert (proj / ".claude" / "beacon.beacon-bak").read_bytes() == OWN
    teardown_project(str(proj))
    assert (proj / ".claude" / "beacon").read_bytes() == OWN and _baks(proj) == []


# ---------------------------------------------------------------- oldfiles (R15 F2)
def _old_project(tmp_path, stale=b"# from an older run\n"):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "CLAUDE.md").write_bytes(b"# mine\n")
    (proj / "CLAUDE.md.beacon-bak").write_bytes(stale)
    _setup(proj)
    assert (proj / "CLAUDE.md.beacon-bak.old").read_bytes() == stale
    return proj


def test_beacon_bak_oldfiles_differing_old_is_kept_and_named(tmp_path, monkeypatch, capsys):
    """F2 inverted: the CLI names the .old it leaves and never says nothing is left."""
    proj = _old_project(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    assert cli.main(["teardown-project", str(proj)]) == 0
    out = capsys.readouterr().out
    assert "No .beacon-bak" not in out
    assert "CLAUDE.md.beacon-bak.old: kept older backup: differs from CLAUDE.md" in out
    assert "Kept CLAUDE.md.beacon-bak.old:" in out
    assert (proj / "CLAUDE.md.beacon-bak.old").exists() and not (proj / "CLAUDE.md.beacon-bak").exists()
    assert cli.main(["teardown-project", str(proj)]) == 0      # a second teardown still names it
    out = capsys.readouterr().out
    assert "No beacon wiring found" in out and "Kept CLAUDE.md.beacon-bak.old:" in out


def test_beacon_bak_oldfiles_redundant_old_is_removed(tmp_path, monkeypatch, capsys):
    """An .old with the bytes the final file has (setup moved it aside for its mode) is redundant: removed."""
    proj2 = tmp_path / "q"
    proj2.mkdir()
    (proj2 / "CLAUDE.md").write_bytes(b"# mine\n")
    (proj2 / "CLAUDE.md.beacon-bak").write_bytes(b"# mine\n")
    os.chmod(proj2 / "CLAUDE.md.beacon-bak", 0o600)
    os.chmod(proj2 / "CLAUDE.md", 0o644)
    _setup(proj2)
    assert (proj2 / "CLAUDE.md.beacon-bak.old").read_bytes() == b"# mine\n"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    assert cli.main(["teardown-project", str(proj2), "--dry-run"]) == 0
    assert "CLAUDE.md.beacon-bak.old: removed the older backup (CLAUDE.md has the same bytes)" in \
        capsys.readouterr().out
    assert (proj2 / "CLAUDE.md.beacon-bak.old").exists()
    assert cli.main(["teardown-project", str(proj2)]) == 0
    out = capsys.readouterr().out
    assert "removed the older backup" in out and "No .beacon-bak or .beacon-bak.old is left behind." in out
    assert _baks(proj2) == []


def test_beacon_bak_oldfiles_numbered_olds_each_settled(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "CLAUDE.md").write_bytes(b"# mine\n")
    (proj / "CLAUDE.md.beacon-bak.old").write_bytes(b"# mine\n")
    (proj / "CLAUDE.md.beacon-bak.old.1").write_bytes(b"# other\n")
    (proj / "CLAUDE.md.beacon-bak.old.x").write_bytes(b"not ours\n")
    _setup(proj)
    out = teardown_project(str(proj))
    assert _baks(proj) == ["CLAUDE.md.beacon-bak.old.1", "CLAUDE.md.beacon-bak.old.x"]
    assert "CLAUDE.md.beacon-bak.old.1: kept older backup: differs from CLAUDE.md (setup moved it aside: it is " \
           "from an earlier run)" in out
    assert not any("old.x" in c for c in out)


def test_beacon_bak_oldfiles_missing_file_keeps_old(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / (MCP + ".beacon-bak")).write_text("{}")             # an older run's: the file does not exist
    out = _setup(proj)
    assert any("moved an older backup aside to " + MCP + ".beacon-bak.old" in c for c in out)
    out = teardown_project(str(proj))
    assert not (proj / MCP).exists()
    assert (MCP + ".beacon-bak.old: kept older backup: " + MCP + " does not exist (setup moved it aside: it is "
            "from an earlier run)") in out


# ---------------------------------------------------------------- msgs (R15 F3)
def test_beacon_bak_msgs_rerun_after_failed_teardown_names_teardowns_copy(tmp_path):
    """F3 inverted: the re-run after a failed teardown names teardown's copy as what it is."""
    proj = tmp_path / "p"
    proj.mkdir()
    _setup(proj)
    sp = proj / SETTINGS
    d = json.loads(sp.read_text())
    d["model"] = "opus"
    sp.write_text(json.dumps(d))
    cm = proj / "CLAUDE.md"
    good = cm.read_bytes()
    cm.write_bytes(good + b"\xff\n")
    with pytest.raises(UnicodeDecodeError):
        teardown_project(str(proj))
    bak = proj / (SETTINGS + ".beacon-bak")
    assert b"beacon.hooks" in bak.read_bytes()
    cm.write_bytes(good)
    out = teardown_project(str(proj))
    msg = [c for c in out if c.startswith(SETTINGS + ".beacon-bak:")]
    assert len(msg) == 1 and "holds the file as it was before the run" not in msg[0], msg
    assert "teardown's copy of " + SETTINGS + " taken before it removed beacon's parts (by a teardown that did " \
           "not finish)" in msg[0]
    assert json.loads(sp.read_text()) == {"model": "opus"} and bak.exists()


def test_beacon_bak_msgs_pre_run_copy_still_says_so(tmp_path):
    proj = _full_project(tmp_path)
    _setup(proj)
    (proj / "CLAUDE.md").write_text((proj / "CLAUDE.md").read_text() + "more\n")
    out = teardown_project(str(proj))
    assert "CLAUDE.md.beacon-bak: kept the backup: CLAUDE.md differs from it (edited during the run, or it " \
           "could not be restored), so it holds the file as it was before the run" in out


def test_beacon_bak_msgs_this_runs_copy_of_a_setup_created_file(tmp_path):
    """The copy teardown takes of a file setup created is named as teardown's copy, never as the pre-run file."""
    proj = tmp_path / "p"
    proj.mkdir()
    _setup(proj)
    mp = proj / MCP
    d = json.loads(mp.read_text())
    d["mcpServers"]["mine"] = {"command": "x"}
    mp.write_text(json.dumps(d))
    out = teardown_project(str(proj))
    msg = [c for c in out if c.startswith(MCP + ".beacon-bak:")]
    assert msg and ("teardown's copy of " + MCP + " taken before it removed beacon's parts; it holds changes made "
                    "during the run") in msg[0]
    assert "before the run" not in msg[0]
