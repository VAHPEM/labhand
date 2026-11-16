# -*- coding: utf-8 -*-
"""A backup whose bytes are committed in git HEAD is redundant: teardown removes it ("committed in HEAD").

`git`: real temporary repositories: tracked + edited during the run, untracked, not a repo, HEAD differs,
symlink, .old backups, nested project, staged-but-uncommitted content.
`gitsafe`: never loses content: a backup that differs from both the final file and HEAD is kept and named;
--dry-run changes nothing; git failing or hanging never fails teardown; the CLI says why."""
import os
import shutil
import subprocess
import sys

import pytest

from beacon import cli, project
from beacon.project import MARK_BEGIN, setup_project, teardown_project

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="no git")

ORIG = b"# My project\n\nKeep this text.\n"
EDIT = b"# My project\n\nKeep this text.\n\nA documentation change made during the run.\n"


def _setup(proj):
    return setup_project(str(proj), sys.executable, sys.executable)


def _git(repo, *args):
    g = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    return subprocess.run(g + list(args), check=True, capture_output=True).stdout


def _repo(tmp_path, files=None, name="proj"):
    """A git repo with `files` (rel -> bytes) committed; CLAUDE.md = ORIG by default."""
    proj = tmp_path / name
    proj.mkdir()
    for rel, data in (files or {"CLAUDE.md": ORIG}).items():
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        (proj / rel).write_bytes(data)
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    return proj


def _edit_claude_md(proj, extra=b"\nA documentation change made during the run.\n"):
    """The worker edits CLAUDE.md outside beacon's block during the run."""
    p = proj / "CLAUDE.md"
    with open(p, "ab") as fh:
        fh.write(extra)


def _baks(proj):
    return sorted(os.path.relpath(str(p), proj) for p in proj.glob("**/*.beacon-bak*"))


def _tree(proj):
    out = {}
    for root, dirs, files in os.walk(proj):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, proj)
            out[rel] = ("link", os.readlink(p)) if os.path.islink(p) else open(p, "rb").read()
    return out


HEAD_MSG = "CLAUDE.md.beacon-bak: removed the backup (committed in HEAD: the same bytes as CLAUDE.md in the " \
           "repository's HEAD commit)"


# ---------------------------------------------------------------- git (P1)
def test_beacon_bak_git_tracked_and_edited_backup_removed(tmp_path):
    """The measured case: a tracked CLAUDE.md edited during the run leaves no backup; the edit stays."""
    proj = _repo(tmp_path)
    _setup(proj)
    assert (proj / "CLAUDE.md.beacon-bak").read_bytes() == ORIG
    _edit_claude_md(proj)
    changes = teardown_project(str(proj))
    assert HEAD_MSG in changes
    assert _baks(proj) == []
    assert (proj / "CLAUDE.md").read_bytes() == EDIT               # the run's edit is kept
    assert MARK_BEGIN.encode() not in (proj / "CLAUDE.md").read_bytes()


def test_beacon_bak_git_untracked_file_keeps_backup(tmp_path):
    proj = _repo(tmp_path, {"README": b"x\n"})
    (proj / "CLAUDE.md").write_bytes(ORIG)                         # exists, never committed
    _setup(proj)
    _edit_claude_md(proj)
    changes = teardown_project(str(proj))
    assert _baks(proj) == ["CLAUDE.md.beacon-bak"]
    assert (proj / "CLAUDE.md.beacon-bak").read_bytes() == ORIG
    assert any(c.startswith("CLAUDE.md.beacon-bak: kept the backup: CLAUDE.md differs from it") for c in changes)
    assert not any("committed in HEAD" in c for c in changes)


def test_beacon_bak_git_staged_but_not_committed_keeps_backup(tmp_path):
    """Only HEAD counts, not the index: a file added but never committed keeps its backup."""
    proj = _repo(tmp_path, {"README": b"x\n"})
    (proj / "CLAUDE.md").write_bytes(ORIG)
    _git(proj, "add", "CLAUDE.md")
    _setup(proj)
    _edit_claude_md(proj)
    teardown_project(str(proj))
    assert _baks(proj) == ["CLAUDE.md.beacon-bak"]


def test_beacon_bak_git_not_a_repo_keeps_backup(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_bytes(ORIG)
    _setup(proj)
    _edit_claude_md(proj)
    changes = teardown_project(str(proj))
    assert _baks(proj) == ["CLAUDE.md.beacon-bak"]
    assert not any("committed in HEAD" in c for c in changes)


def test_beacon_bak_git_head_differs_keeps_backup(tmp_path):
    """The file was changed (not committed) before the run: the backup holds that change, HEAD does not."""
    proj = _repo(tmp_path)
    pre = ORIG + b"uncommitted line before the run\n"
    (proj / "CLAUDE.md").write_bytes(pre)
    _setup(proj)
    _edit_claude_md(proj)
    changes = teardown_project(str(proj))
    assert _baks(proj) == ["CLAUDE.md.beacon-bak"]
    assert (proj / "CLAUDE.md.beacon-bak").read_bytes() == pre
    assert any(c.startswith("CLAUDE.md.beacon-bak: kept the backup") for c in changes)


def test_beacon_bak_git_head_differs_by_line_endings_keeps_backup(tmp_path):
    """Exact bytes: CRLF in the backup vs LF in HEAD is a difference."""
    proj = _repo(tmp_path)
    (proj / "CLAUDE.md").write_bytes(ORIG.replace(b"\n", b"\r\n"))
    _setup(proj)
    _edit_claude_md(proj, b"\r\nedit\r\n")
    teardown_project(str(proj))
    assert _baks(proj) == ["CLAUDE.md.beacon-bak"]


def test_beacon_bak_git_symlink_target_tracked(tmp_path):
    """CLAUDE.md -> shared/CLAUDE.md, both tracked: the backup is compared with the target's HEAD version."""
    proj = _repo(tmp_path, {"shared/CLAUDE.md": ORIG})
    os.symlink(os.path.join("shared", "CLAUDE.md"), proj / "CLAUDE.md")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "link")
    _setup(proj)
    assert os.path.islink(proj / "CLAUDE.md")
    _edit_claude_md(proj)
    changes = teardown_project(str(proj))
    assert ("CLAUDE.md.beacon-bak: removed the backup (committed in HEAD: the same bytes as shared/CLAUDE.md in "
            "the repository's HEAD commit)") in changes
    assert _baks(proj) == []
    assert os.path.islink(proj / "CLAUDE.md") and (proj / "shared" / "CLAUDE.md").read_bytes() == EDIT


def test_beacon_bak_git_symlink_target_untracked_keeps_backup(tmp_path):
    """The link is tracked, its target is not: git holds only the link text, so the backup is kept."""
    proj = _repo(tmp_path, {"README": b"x\n"})
    shared = tmp_path / "elsewhere"
    shared.mkdir()
    (shared / "CLAUDE.md").write_bytes(ORIG)
    os.symlink(str(shared / "CLAUDE.md"), proj / "CLAUDE.md")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "link")
    _setup(proj)
    _edit_claude_md(proj)
    teardown_project(str(proj))
    assert _baks(proj) == ["CLAUDE.md.beacon-bak"]


def test_beacon_bak_git_old_backup_equal_to_head_removed(tmp_path):
    proj = _repo(tmp_path)
    (proj / "CLAUDE.md.beacon-bak").write_bytes(ORIG)               # an older run's, equal to HEAD
    (proj / "CLAUDE.md").write_bytes(ORIG + b"changed since\n")     # so setup moves it aside
    _setup(proj)
    assert (proj / "CLAUDE.md.beacon-bak.old").read_bytes() == ORIG
    changes = teardown_project(str(proj))
    assert ("CLAUDE.md.beacon-bak.old: removed the older backup (committed in HEAD: the same bytes as CLAUDE.md "
            "in the repository's HEAD commit)") in changes
    assert _baks(proj) == []                                        # the fresh backup equals the final file


def test_beacon_bak_git_old_backup_differing_from_head_kept(tmp_path):
    proj = _repo(tmp_path)
    (proj / "CLAUDE.md.beacon-bak").write_bytes(b"# from an older run\n")
    _setup(proj)
    changes = teardown_project(str(proj))
    assert _baks(proj) == ["CLAUDE.md.beacon-bak.old"]
    assert any(c.startswith("CLAUDE.md.beacon-bak.old: kept older backup: differs from CLAUDE.md") for c in changes)


def test_beacon_bak_git_nested_project_in_repo(tmp_path):
    """The project is a subdirectory of the repository: the path git tracks is relative to the top level."""
    repo = _repo(tmp_path, {"sub dir/p[1]/CLAUDE.md": ORIG}, name="repo")
    proj = repo / "sub dir" / "p[1]"
    _setup(proj)
    _edit_claude_md(proj)
    changes = teardown_project(str(proj))
    assert ("CLAUDE.md.beacon-bak: removed the backup (committed in HEAD: the same bytes as sub dir/p[1]/CLAUDE.md "
            "in the repository's HEAD commit)") in changes
    assert _baks(proj) == []


def test_beacon_bak_git_json_file_committed(tmp_path):
    """Same rule for a JSON wired file: a tracked .mcp.json the run edited."""
    mcp = b'{\n    "mcpServers": {\n        "labhand": {"command": "labhand-mcp"}\n    }\n}\n'
    proj = _repo(tmp_path, {".mcp.json": mcp})
    _setup(proj)
    import json
    cfg = json.loads((proj / ".mcp.json").read_text())
    cfg["mcpServers"]["other"] = {"command": "x"}                   # edited during the run
    (proj / ".mcp.json").write_text(json.dumps(cfg, indent=2))
    changes = teardown_project(str(proj))
    assert ".mcp.json.beacon-bak: removed the backup (committed in HEAD: the same bytes as .mcp.json in the " \
           "repository's HEAD commit)" in changes
    assert _baks(proj) == []
    assert "other" in json.loads((proj / ".mcp.json").read_text())["mcpServers"]


def test_beacon_bak_git_file_removed_during_run_but_in_head(tmp_path):
    """The run deleted the file; the backup's bytes are in HEAD, so it is not the only copy."""
    proj = _repo(tmp_path)
    _setup(proj)
    os.remove(proj / "CLAUDE.md")
    changes = teardown_project(str(proj))
    assert HEAD_MSG in changes
    assert _baks(proj) == []
    assert _git(proj, "show", "HEAD:CLAUDE.md") == ORIG


def test_beacon_bak_git_inherited_git_dir_is_ignored(tmp_path, monkeypatch):
    """A GIT_DIR inherited from a hook points at another repo whose HEAD has the backup's bytes: ignored."""
    other = _repo(tmp_path, name="other")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_bytes(ORIG)
    _setup(proj)
    _edit_claude_md(proj)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(proj))
    teardown_project(str(proj))
    assert _baks(proj) == ["CLAUDE.md.beacon-bak"]


# ---------------------------------------------------------------- gitsafe (P2)
def test_beacon_bak_gitsafe_differs_from_file_and_head_kept_and_named(tmp_path, monkeypatch, capsys):
    proj = _repo(tmp_path)
    pre = ORIG + b"local, uncommitted\n"
    (proj / "CLAUDE.md").write_bytes(pre)
    _setup(proj)
    _edit_claude_md(proj)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    assert cli.main(["teardown-project", str(proj)]) == 0
    out = capsys.readouterr().out
    assert "CLAUDE.md.beacon-bak: kept the backup: CLAUDE.md differs from it" in out
    assert "Kept CLAUDE.md.beacon-bak:" in out and "nor git HEAD" in out
    assert (proj / "CLAUDE.md.beacon-bak").read_bytes() == pre


def test_beacon_bak_gitsafe_dry_run_changes_nothing(tmp_path, monkeypatch, capsys):
    proj = _repo(tmp_path)
    (proj / "CLAUDE.md.beacon-bak").write_bytes(ORIG)
    (proj / "CLAUDE.md").write_bytes(ORIG + b"changed since\n")
    _setup(proj)
    _edit_claude_md(proj)
    before = _tree(proj)
    head_before = _git(proj, "rev-parse", "HEAD")
    status_before = _git(proj, "status", "--porcelain")
    dry = teardown_project(str(proj), dry_run=True)
    assert _tree(proj) == before
    assert _git(proj, "rev-parse", "HEAD") == head_before and _git(proj, "status", "--porcelain") == status_before
    assert ("CLAUDE.md.beacon-bak.old: removed the older backup (committed in HEAD: the same bytes as CLAUDE.md "
            "in the repository's HEAD commit)") in dry
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    assert cli.main(["teardown-project", str(proj), "--dry-run"]) == 0
    assert "committed in HEAD" in capsys.readouterr().out
    assert _tree(proj) == before
    real = teardown_project(str(proj))
    assert real == dry                                              # the dry run reported the real verdicts


@pytest.mark.parametrize("how", ["missing", "timeout", "fails", "garbage"])
def test_beacon_bak_gitsafe_git_misbehaving_keeps_backup(tmp_path, monkeypatch, how):
    """git missing, hanging, failing or printing nonsense: teardown still finishes and keeps the backup."""
    proj = _repo(tmp_path)
    _setup(proj)
    _edit_claude_md(proj)
    real_run = subprocess.run

    def fake(cmd, *a, **kw):
        if cmd and cmd[0] == "git":
            if how == "missing":
                raise FileNotFoundError("git")
            if how == "timeout":
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            if how == "fails":
                return subprocess.CompletedProcess(cmd, 128, b"", b"fatal")
            return subprocess.CompletedProcess(cmd, 0, b"\xff\x00garbage", b"")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(project.subprocess, "run", fake)
    changes = teardown_project(str(proj))
    assert _baks(proj) == ["CLAUDE.md.beacon-bak"]
    assert (proj / "CLAUDE.md.beacon-bak").read_bytes() == ORIG
    assert not any("committed in HEAD" in c for c in changes)


def test_beacon_bak_gitsafe_git_has_a_timeout(tmp_path, monkeypatch):
    seen = []
    real_run = subprocess.run

    def spy(cmd, *a, **kw):
        if cmd and cmd[0] == "git":
            seen.append(kw.get("timeout"))
        return real_run(cmd, *a, **kw)

    proj = _repo(tmp_path)
    _setup(proj)
    _edit_claude_md(proj)
    monkeypatch.setattr(project.subprocess, "run", spy)
    teardown_project(str(proj))
    assert seen and all(t and t <= 30 for t in seen)


def test_beacon_bak_gitsafe_empty_repo_without_head_keeps_backup(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_bytes(ORIG)
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")                                          # staged, no commit: no HEAD
    _setup(proj)
    _edit_claude_md(proj)
    teardown_project(str(proj))
    assert _baks(proj) == ["CLAUDE.md.beacon-bak"]


def test_beacon_bak_gitsafe_renamed_in_head_keeps_backup(tmp_path):
    """HEAD tracks the content under another name only: the wired path is not tracked, so the backup stays."""
    proj = _repo(tmp_path, {"OLD.md": ORIG})
    (proj / "CLAUDE.md").write_bytes(ORIG)                           # same bytes, untracked path
    _setup(proj)
    _edit_claude_md(proj)
    teardown_project(str(proj))
    assert _baks(proj) == ["CLAUDE.md.beacon-bak"]


def test_beacon_bak_gitsafe_unchanged_run_still_byte_for_byte(tmp_path):
    """No edit during the run in a repo: the old rule removes the backup (back to it byte for byte), git clean."""
    proj = _repo(tmp_path)
    _setup(proj)
    changes = teardown_project(str(proj))
    assert "CLAUDE.md.beacon-bak: removed the backup (CLAUDE.md is back to it byte for byte)" in changes
    assert _baks(proj) == [] and _git(proj, "status", "--porcelain") == b""


def test_beacon_bak_gitsafe_cli_setup_message_mentions_head(tmp_path, capsys):
    proj = _repo(tmp_path)
    assert cli.main(["setup-project", str(proj)]) == 0
    assert "committed in git HEAD" in capsys.readouterr().out
