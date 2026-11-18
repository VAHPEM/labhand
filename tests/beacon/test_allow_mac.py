# -*- coding: utf-8 -*-
"""`setup-project --allow-mac`: the labhand-mac MCP server (control of this Mac) is wired into one project only
when asked, and teardown removes exactly what setup added for it (a user's prior labhand-mac entry stays)."""
import json
import os
import sys

import pytest

from beacon import cli
from beacon import project as _project
from beacon.project import MAC_SERVER, teardown_project

SL = os.path.join(".claude", "settings.local.json")
SJ = os.path.join(".claude", "settings.json")
MCP = ".mcp.json"
MAC_ARGS = ["mcp"]


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.delenv("LABHAND_MAC_MCP_COMMAND", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _proj(tmp_path):
    p = tmp_path / "proj"
    (p / ".claude").mkdir(parents=True)
    return p


def _setup(proj, **kw):
    return _project.setup_project(str(proj), sys.executable, sys.executable, **kw)


def _put(proj, rel, obj):
    (proj / rel).write_text(json.dumps(obj, indent=2) + "\n")


def _get(proj, rel):
    p = proj / rel
    return json.loads(p.read_text()) if p.exists() else None


def _snapshot(proj):
    """Every file under the project, path -> bytes (the record included)."""
    out = {}
    for root, _, files in os.walk(proj):
        for f in files:
            path = os.path.join(root, f)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, proj)] = fh.read()
    return out


def _default_command(tmp_path):
    return str(tmp_path / "home" / ".local" / "bin" / "cua-driver")


def test_allow_mac_setup_adds_server_enable_allow(tmp_path):
    proj = _proj(tmp_path)
    changes = _setup(proj, allow_mac=True)
    server = _get(proj, MCP)["mcpServers"][MAC_SERVER]
    assert server == {"command": _default_command(tmp_path), "args": MAC_ARGS}
    assert os.path.isabs(server["command"])
    s = _get(proj, SL)
    assert MAC_SERVER in s["enabledMcpjsonServers"]
    assert "mcp__labhand-mac" in s["permissions"]["allow"]
    assert "beacon" not in s["enabledMcpjsonServers"]           # beacon's own wiring is unchanged
    assert any("labhand-mac" in c for c in changes)
    # the beacon server is still there as before
    assert "beacon" in _get(proj, MCP)["mcpServers"]


def test_allow_mac_setup_cli_flag(tmp_path, capsys):
    proj = _proj(tmp_path)
    assert cli.main(["setup-project", str(proj), "--allow-mac"]) == 0
    assert MAC_SERVER in _get(proj, MCP)["mcpServers"]
    assert "mcp__labhand-mac" in _get(proj, SL)["permissions"]["allow"]
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["setup-project", "--help"])
    assert "--allow-mac" in capsys.readouterr().out


def test_allow_mac_shared_writes_settings_json(tmp_path):
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True, shared=True)
    s = _get(proj, SJ)
    assert MAC_SERVER in s["enabledMcpjsonServers"] and "mcp__labhand-mac" in s["permissions"]["allow"]
    assert _get(proj, SL) is None
    teardown_project(str(proj))
    assert _get(proj, SJ) is None and _get(proj, MCP) is None


def test_allow_mac_absent_without_flag(tmp_path):
    proj = _proj(tmp_path)
    _setup(proj)
    _setup(proj, allow_labhand=True)
    for rel, data in _snapshot(proj).items():
        assert b"labhand-mac" not in data, rel
    assert MAC_SERVER not in _get(proj, MCP)["mcpServers"]
    assert "enabledMcpjsonServers" not in _get(proj, SL)


def test_allow_mac_absent_without_flag_cli(tmp_path):
    proj = _proj(tmp_path)
    assert cli.main(["setup-project", str(proj), "--allow-labhand"]) == 0
    for rel, data in _snapshot(proj).items():
        assert b"labhand-mac" not in data, rel


def test_allow_mac_teardown_removes_only_added(tmp_path):
    # a fresh project: everything setup wrote goes, nothing is left
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    s = _get(proj, SL)
    s["permissions"]["allow"].append("mcp__labhand-mac__mac_click")      # an approval made during the run
    _put(proj, SL, s)
    teardown_project(str(proj))
    assert _get(proj, MCP) is None and _get(proj, SL) is None


def test_allow_mac_teardown_keeps_user_prior_entry(tmp_path):
    proj = _proj(tmp_path)
    mine = {"command": "/usr/local/bin/my-engine", "args": ["serve"], "env": {"X": "1"}}
    other = {"command": "node", "args": ["srv.js"]}
    _put(proj, MCP, {"mcpServers": {MAC_SERVER: mine, "other": other}})
    _put(proj, SL, {"enabledMcpjsonServers": ["other", MAC_SERVER],
                    "permissions": {"allow": ["mcp__labhand-mac__mac_screenshot", "Bash(ls)"]}})
    _setup(proj, allow_mac=True)
    assert _get(proj, MCP)["mcpServers"][MAC_SERVER]["command"] == _default_command(tmp_path)
    s = _get(proj, SL)
    assert s["enabledMcpjsonServers"].count(MAC_SERVER) == 1
    s["permissions"]["allow"].append("mcp__labhand-mac__mac_click")      # added during the run: goes
    s["permissions"]["allow"].append("Bash(pwd)")                        # added during the run, not ours: stays
    _put(proj, SL, s)
    teardown_project(str(proj))
    assert _get(proj, MCP) == {"mcpServers": {MAC_SERVER: mine, "other": other}}
    s = _get(proj, SL)
    assert s["enabledMcpjsonServers"] == ["other", MAC_SERVER]
    assert s["permissions"]["allow"] == ["mcp__labhand-mac__mac_screenshot", "Bash(ls)", "Bash(pwd)"]
    assert "hooks" not in s


def test_allow_mac_teardown_keeps_user_identical_entry_and_rule(tmp_path):
    # the user already had exactly setup's labhand-mac wiring: setup adds nothing for it, teardown keeps it
    proj = _proj(tmp_path)
    entry = {"command": _default_command(tmp_path), "args": MAC_ARGS}
    _put(proj, MCP, {"mcpServers": {MAC_SERVER: entry}})
    _put(proj, SL, {"enabledMcpjsonServers": [MAC_SERVER], "permissions": {"allow": ["mcp__labhand-mac"]}})
    before = _snapshot(proj)
    _setup(proj, allow_mac=True)
    teardown_project(str(proj))
    after = _snapshot(proj)
    assert after == before


def test_allow_mac_teardown_twice_and_interrupted(tmp_path):
    proj = _proj(tmp_path)
    _put(proj, SL, {"model": "opus"})
    _setup(proj, allow_mac=True)
    # an interrupted teardown: the settings were already stripped, the record is still there
    from beacon.project import _read_record, strip_settings, _entry
    rec = _read_record(str(proj))
    stripped, ch = strip_settings(_get(proj, SL), _entry(rec, SL))
    assert "removed labhand-mac from enabledMcpjsonServers" in ch
    _put(proj, SL, stripped)
    teardown_project(str(proj))
    assert _get(proj, SL) == {"model": "opus"} and _get(proj, MCP) is None
    assert teardown_project(str(proj)) == []                            # nothing left to remove


def test_allow_mac_teardown_keeps_user_empty_enabled_list(tmp_path):
    # the user's settings had an empty enabledMcpjsonServers: removing labhand-mac empties it again, and the
    # key stays (the file goes back to its pre-setup bytes, no backup left)
    proj = _proj(tmp_path)
    (proj / SL).write_text('{"enabledMcpjsonServers": [], "model": "opus"}')
    before = _snapshot(proj)
    _setup(proj, allow_mac=True)
    s = _get(proj, SL)
    s["permissions"]["allow"].append("Bash(pwd)")             # edited during the run: no byte-for-byte restore
    _put(proj, SL, s)
    teardown_project(str(proj))
    assert _get(proj, SL) == {"enabledMcpjsonServers": [], "model": "opus", "permissions": {"allow": ["Bash(pwd)"]}}
    proj2 = _proj(tmp_path / "b")
    (proj2 / SL).write_text('{"enabledMcpjsonServers": [], "model": "opus"}')
    before = _snapshot(proj2)
    _setup(proj2, allow_mac=True)
    teardown_project(str(proj2))
    assert _snapshot(proj2) == before


def test_allow_mac_server_removed_by_hand_keeps_beacon_record(tmp_path):
    # the user removes labhand-mac during the run and setup runs again: the beacon server setup added is
    # still setup's (each server's record is judged alone)
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    cfg = _get(proj, MCP)
    del cfg["mcpServers"][MAC_SERVER]
    _put(proj, MCP, cfg)
    _setup(proj)
    teardown_project(str(proj))
    assert _get(proj, MCP) is None


def test_allow_mac_interrupted_setup_record_only(tmp_path):
    # setup wrote its record, then stopped before the wired files: a re-run and a teardown leave the
    # user's own labhand-mac entry alone
    proj = _proj(tmp_path)
    mine = {"command": "/x/engine", "args": MAC_ARGS}
    _put(proj, MCP, {"mcpServers": {MAC_SERVER: mine}})
    _setup(proj, allow_mac=True)
    # undo the wired files by hand, as if setup had stopped after writing the record
    _put(proj, MCP, {"mcpServers": {MAC_SERVER: mine}})
    os.remove(proj / SL)
    os.remove(proj / (MCP + ".beacon-bak"))
    _setup(proj, allow_mac=True)
    teardown_project(str(proj))
    assert _get(proj, MCP) == {"mcpServers": {MAC_SERVER: mine}}
    assert _get(proj, SL) is None


@pytest.mark.parametrize("labhand", [False, True])
@pytest.mark.parametrize("mac", [False, True])
def test_allow_mac_both_flags(tmp_path, labhand, mac):
    proj = _proj(tmp_path)
    _setup(proj, allow_labhand=labhand, allow_mac=mac)
    allow = _get(proj, SL)["permissions"]["allow"]
    assert ("mcp__labhand" in allow) == labhand
    assert ("mcp__labhand-mac" in allow) == mac
    assert (MAC_SERVER in _get(proj, MCP)["mcpServers"]) == mac
    assert (MAC_SERVER in _get(proj, SL).get("enabledMcpjsonServers", [])) == mac
    teardown_project(str(proj))
    assert _get(proj, SL) is None and _get(proj, MCP) is None


def test_allow_mac_both_flags_cli(tmp_path):
    proj = _proj(tmp_path)
    assert cli.main(["setup-project", str(proj), "--allow-labhand", "--allow-mac"]) == 0
    allow = _get(proj, SL)["permissions"]["allow"]
    assert "mcp__labhand" in allow and "mcp__labhand-mac" in allow
    teardown_project(str(proj))        # the CLI form needs a terminal (it acts as the human)
    assert _get(proj, SL) is None and _get(proj, MCP) is None


def test_allow_mac_env_override(tmp_path, monkeypatch):
    proj = _proj(tmp_path)
    monkeypatch.setenv("LABHAND_MAC_MCP_COMMAND", '["/opt/engine/bin/eng", "serve", "--stdio"]')
    _setup(proj, allow_mac=True)
    assert _get(proj, MCP)["mcpServers"][MAC_SERVER] == {"command": "/opt/engine/bin/eng",
                                                         "args": ["serve", "--stdio"]}
    monkeypatch.setenv("LABHAND_MAC_MCP_COMMAND", "  ")              # blank counts as unset
    _setup(proj, allow_mac=True)
    assert _get(proj, MCP)["mcpServers"][MAC_SERVER] == {"command": _default_command(tmp_path), "args": MAC_ARGS}
    teardown_project(str(proj))
    assert _get(proj, MCP) is None


def test_allow_mac_no_pythonpath(tmp_path, monkeypatch):
    proj = _proj(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "beacon_home"))
    _setup(proj, allow_mac=True)
    server = _get(proj, MCP)["mcpServers"][MAC_SERVER]
    assert "PYTHONPATH" not in (server.get("env") or {})
    assert "PYTHONPATH" not in json.dumps(server)
    assert server["args"] == MAC_ARGS


def test_allow_mac_rerun_idempotent(tmp_path):
    proj = _proj(tmp_path)
    _put(proj, SL, {"model": "opus", "enabledMcpjsonServers": ["other"]})
    _setup(proj, allow_mac=True, allow_labhand=True)
    first = _snapshot(proj)
    assert _setup(proj, allow_mac=True, allow_labhand=True) == []
    assert _snapshot(proj) == first
    teardown_project(str(proj))
    assert _get(proj, SL) == {"model": "opus", "enabledMcpjsonServers": ["other"]}
    assert _get(proj, MCP) is None


def test_allow_mac_rerun_adds_mac_later(tmp_path):
    # first without the grant, then with it: the second setup's record adds labhand-mac, teardown removes all
    proj = _proj(tmp_path)
    _put(proj, SL, {"model": "opus"})
    _setup(proj)
    _setup(proj, allow_mac=True)
    assert MAC_SERVER in _get(proj, MCP)["mcpServers"]
    teardown_project(str(proj))
    assert _get(proj, SL) == {"model": "opus"} and _get(proj, MCP) is None


def test_allow_mac_r2_engine_setup_never_runs_the_engine(tmp_path, monkeypatch):
    # setup only writes the command; it never starts the engine (nor any program)
    import subprocess

    def refuse(*a, **kw):
        raise AssertionError(f"setup ran a program: {a!r}")
    monkeypatch.setattr(subprocess, "Popen", refuse)
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    assert MAC_SERVER in _get(proj, MCP)["mcpServers"]


def _exe(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return path


def test_allow_mac_r2_engine_doctor_check(tmp_path, monkeypatch):
    from beacon import doctor
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)                                       # default: ~/.local/bin/cua-driver, absent
    rep = doctor.Report()
    doctor.check_labhand_mac(rep, str(proj))
    assert not rep.failed and len(rep.lines) == 1 and "does not exist" in rep.lines[0]
    noexec = tmp_path / "bin" / "noexec"
    noexec.parent.mkdir(parents=True)
    noexec.write_text("x")
    noexec.chmod(0o644)
    monkeypatch.setenv("LABHAND_MAC_MCP_COMMAND", json.dumps([str(noexec), "mcp"]))
    _setup(proj, allow_mac=True)
    rep = doctor.Report()
    doctor.check_labhand_mac(rep, str(proj))
    assert not rep.failed and "not executable" in rep.lines[0]
    eng = _exe(tmp_path / "bin" / "eng", 'echo "eng 1.2.3"')
    monkeypatch.setenv("LABHAND_MAC_MCP_COMMAND", json.dumps([str(eng), "mcp"]))
    _setup(proj, allow_mac=True)
    rep = doctor.Report()
    doctor.check_labhand_mac(rep, str(proj))
    assert not rep.failed and len(rep.lines) == 1 and "eng 1.2.3" in rep.lines[0] and "executable" in rep.lines[0]
    rep = doctor.Report()
    doctor.check_labhand_mac(rep, str(_proj(tmp_path / "other")))     # no labhand-mac: nothing reported
    assert rep.lines == []


def test_allow_mac_r2_engine_doctor_version_timeout(tmp_path, monkeypatch):
    # an engine that does not answer --version: doctor gives up after 5 s and only reports it
    from beacon import doctor
    seen = {}

    def fake_run(argv, env=None, timeout=20):
        seen["argv"], seen["timeout"] = argv, timeout
        return -1, "timed out after 5 seconds"
    monkeypatch.setattr(doctor, "_run", fake_run)
    eng = _exe(tmp_path / "bin" / "eng", "sleep 60")
    monkeypatch.setenv("LABHAND_MAC_MCP_COMMAND", json.dumps([str(eng), "mcp"]))
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    rep = doctor.Report()
    doctor.check_labhand_mac(rep, str(proj))
    assert seen == {"argv": [str(eng), "--version"], "timeout": 5}
    assert not rep.failed and "did not answer" in rep.lines[0]


def test_allow_mac_r2_engine_doctor_version_real_timeout(tmp_path):
    # the real _run gives up on a program that hangs (5 s limit, measured here with a shorter one)
    from beacon import doctor
    eng = _exe(tmp_path / "bin" / "eng", "sleep 30")
    code, out = doctor._run([str(eng), "--version"], timeout=1)
    assert code == -1 and "timed out" in out


def test_allow_mac_r2_engine_default_command(tmp_path):
    from beacon.project import mac_command
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    server = _get(proj, MCP)["mcpServers"][MAC_SERVER]
    assert server == {"command": _default_command(tmp_path), "args": ["mcp"]}
    assert os.path.isabs(server["command"]) and "~" not in server["command"]
    assert mac_command() == [_default_command(tmp_path), "mcp"]
    assert "env" not in server


@pytest.mark.parametrize("given, want", [
    ("~/eng/bin/x", "{home}/eng/bin/x"),             # ~ expanded
    ("venv/bin/x", "{cwd}/venv/bin/x"),              # relative path: against setup's cwd, not the project's
    ("./x", "{cwd}/x"),
    ("/abs/../abs/x", "/abs/x"),
    ("cua-driver", "cua-driver"),                    # a bare name stays, for PATH lookup
])
def test_allow_mac_r2_engine_program_made_absolute(tmp_path, monkeypatch, given, want):
    # R2 F4: a program given as a path reaches the server entry absolute (Claude Code spawns it without a
    # shell, from the project's directory)
    run_dir = tmp_path / "rundir"
    run_dir.mkdir()
    monkeypatch.chdir(run_dir)
    monkeypatch.setenv("LABHAND_MAC_MCP_COMMAND", json.dumps([given, "mcp", "~/arg"]))
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    server = _get(proj, MCP)["mcpServers"][MAC_SERVER]
    home, cwd = str(tmp_path / "home"), os.getcwd()
    assert server["command"] == want.format(home=home, cwd=cwd)
    assert server["args"] == ["mcp", "~/arg"]                       # args are passed as given


@pytest.mark.parametrize("value", [
    "not json", '{"a": 1}', "[]", '[""]', '["  "]', "[1, 2]", '["/x", 3]', '"/x/eng"', "null",
])
def test_allow_mac_r2_engine_malformed_refused_before_writing(tmp_path, monkeypatch, value):
    from beacon.project import MacCommandError
    proj = _proj(tmp_path)
    _put(proj, SL, {"model": "opus"})
    before = _snapshot(proj)
    monkeypatch.setenv("LABHAND_MAC_MCP_COMMAND", value)
    with pytest.raises(MacCommandError, match="LABHAND_MAC_MCP_COMMAND"):
        _setup(proj, allow_mac=True)
    assert _snapshot(proj) == before
    _setup(proj)                                                     # without the grant the override is unused
    assert MAC_SERVER not in _get(proj, MCP)["mcpServers"]


def test_allow_mac_r2_engine_malformed_cli_error(tmp_path, monkeypatch, capsys):
    proj = _proj(tmp_path)
    monkeypatch.setenv("LABHAND_MAC_MCP_COMMAND", "[")
    rc = cli.main(["setup-project", str(proj), "--allow-mac"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "LABHAND_MAC_MCP_COMMAND" in err and "nothing was written" in err
    assert _snapshot(proj) == {}


def test_allow_mac_r2_engine_no_python_module(tmp_path, monkeypatch):
    # the python module command and its env var are gone: LABHAND_MAC_PYTHON has no effect
    monkeypatch.setenv("LABHAND_MAC_PYTHON", "/opt/py/bin/python3")
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    server = _get(proj, MCP)["mcpServers"][MAC_SERVER]
    assert server == {"command": _default_command(tmp_path), "args": ["mcp"]}
    assert "mac_mcp_server" not in json.dumps(server)


def test_allow_mac_r2_engine_switch_from_old_entry(tmp_path):
    # a project wired by the earlier python-module version: a re-run rewrites the entry, teardown removes it
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    cfg = _get(proj, MCP)
    cfg["mcpServers"][MAC_SERVER] = {"command": "/x/python", "args": ["-m", "labhand.mac_mcp_server"]}
    _put(proj, MCP, cfg)
    _setup(proj, allow_mac=True)
    assert _get(proj, MCP)["mcpServers"][MAC_SERVER]["args"] == ["mcp"]
    teardown_project(str(proj))
    assert _get(proj, MCP) is None and _get(proj, SL) is None


# ---------------------------------------------------------------- revocation: the grant follows the flag
def _no_mac_anywhere(proj):
    for rel, data in _snapshot(proj).items():
        assert b"labhand-mac" not in data, rel


def test_allow_mac_revoke_rerun_without_flag(tmp_path):
    # a task restarted without the grant: setup takes back the server, its enable, its allow rule and the
    # per-tool approvals made during the run, and its record forgets them; beacon's own wiring stays
    proj = _proj(tmp_path)
    _put(proj, SL, {"model": "opus"})
    _setup(proj, allow_mac=True, allow_labhand=True)
    s = _get(proj, SL)
    s["permissions"]["allow"] += ["mcp__labhand-mac__mac_click", "mcp__beacon__ledger_view"]
    _put(proj, SL, s)
    changes = _setup(proj, allow_labhand=True)
    assert any("no --allow-mac" in c for c in changes)
    _no_mac_anywhere(proj)
    assert "beacon" in _get(proj, MCP)["mcpServers"]
    allow = _get(proj, SL)["permissions"]["allow"]
    assert "mcp__beacon" in allow and "mcp__labhand" in allow and "mcp__beacon__ledger_view" in allow
    assert _get(proj, SL)["hooks"]
    assert _setup(proj, allow_labhand=True) == []                      # revoking again: nothing to do
    teardown_project(str(proj))
    assert _get(proj, SL) == {"model": "opus"} and _get(proj, MCP) is None


def test_allow_mac_revoke_keeps_user_prior_entry(tmp_path):
    proj = _proj(tmp_path)
    mine = {"command": "/usr/local/bin/my-engine", "args": MAC_ARGS}
    _put(proj, MCP, {"mcpServers": {MAC_SERVER: mine}})
    _put(proj, SL, {"enabledMcpjsonServers": [MAC_SERVER],
                    "permissions": {"allow": ["mcp__labhand-mac__mac_screenshot"]}})
    _setup(proj, allow_mac=True)
    assert _get(proj, MCP)["mcpServers"][MAC_SERVER] != mine          # setup replaced it with its own
    s = _get(proj, SL)
    s["permissions"]["allow"].append("mcp__labhand-mac__mac_click")
    _put(proj, SL, s)
    _setup(proj)
    assert _get(proj, MCP)["mcpServers"][MAC_SERVER] == mine           # the user's entry is back
    s = _get(proj, SL)
    assert s["enabledMcpjsonServers"] == [MAC_SERVER]                  # the user's enable stays
    assert "mcp__labhand-mac" not in s["permissions"]["allow"]
    assert "mcp__labhand-mac__mac_screenshot" in s["permissions"]["allow"]
    assert "mcp__labhand-mac__mac_click" not in s["permissions"]["allow"]
    teardown_project(str(proj))
    assert _get(proj, MCP) == {"mcpServers": {MAC_SERVER: mine}}
    assert _get(proj, SL) == {"enabledMcpjsonServers": [MAC_SERVER],
                              "permissions": {"allow": ["mcp__labhand-mac__mac_screenshot"]}}


def test_allow_mac_revoke_user_identical_wiring_stays(tmp_path):
    # the user had exactly setup's labhand-mac wiring: it was never setup's, so revoking leaves it
    proj = _proj(tmp_path)
    entry = {"command": _default_command(tmp_path), "args": MAC_ARGS}
    _put(proj, MCP, {"mcpServers": {MAC_SERVER: entry}})
    _put(proj, SL, {"enabledMcpjsonServers": [MAC_SERVER], "permissions": {"allow": ["mcp__labhand-mac"]}})
    before = _snapshot(proj)
    _setup(proj, allow_mac=True)
    _setup(proj)
    assert _get(proj, MCP)["mcpServers"][MAC_SERVER] == entry
    assert "mcp__labhand-mac" in _get(proj, SL)["permissions"]["allow"]
    teardown_project(str(proj))
    assert _snapshot(proj) == before


def test_allow_mac_revoke_then_grant_again(tmp_path):
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    _setup(proj)
    _no_mac_anywhere(proj)
    _setup(proj, allow_mac=True)
    assert MAC_SERVER in _get(proj, MCP)["mcpServers"]
    assert MAC_SERVER in _get(proj, SL)["enabledMcpjsonServers"]
    teardown_project(str(proj))
    assert _get(proj, MCP) is None and _get(proj, SL) is None


def test_allow_mac_revoke_empty_enabled_list_kept(tmp_path):
    # the user's empty enabledMcpjsonServers stays an (empty) list after revoking
    proj = _proj(tmp_path)
    (proj / SL).write_text('{"enabledMcpjsonServers": [], "model": "opus"}')
    _setup(proj, allow_mac=True)
    _setup(proj)
    assert _get(proj, SL)["enabledMcpjsonServers"] == []
    teardown_project(str(proj))
    assert _get(proj, SL) == {"enabledMcpjsonServers": [], "model": "opus"}


def test_allow_mac_revoke_other_settings_file(tmp_path):
    # granted with --shared (settings.json), restarted without the grant and without --shared: the grant
    # in settings.json is taken back too
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True, shared=True)
    _setup(proj)
    _no_mac_anywhere(proj)
    teardown_project(str(proj))
    assert _get(proj, MCP) is None and _get(proj, SL) is None and _get(proj, SJ) is None


def test_allow_mac_revoke_server_removed_by_hand(tmp_path):
    # the user removed setup's labhand-mac server during the run: revoking still takes back the settings parts
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    cfg = _get(proj, MCP)
    del cfg["mcpServers"][MAC_SERVER]
    _put(proj, MCP, cfg)
    _setup(proj)
    _no_mac_anywhere(proj)
    teardown_project(str(proj))
    assert _get(proj, MCP) is None and _get(proj, SL) is None


# ---------------------------------------------------------------- teardown with setup's record deleted by hand
def _drop_record(proj):
    from beacon.project import RECORD
    os.remove(proj / RECORD)


def test_allow_mac_teardown_no_record_fresh_project(tmp_path):
    # setup created the files (no backup): with the record gone, what is left after beacon's parts is only
    # setup's labhand-mac wiring, and it goes too (else the next session there would get Mac control)
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    _drop_record(proj)
    teardown_project(str(proj))
    assert _get(proj, MCP) is None and _get(proj, SL) is None


def test_allow_mac_teardown_no_record_with_backup(tmp_path):
    proj = _proj(tmp_path)
    _put(proj, SL, {"model": "opus"})
    _put(proj, MCP, {"mcpServers": {"other": {"command": "x"}}})
    before = _snapshot(proj)
    _setup(proj, allow_mac=True)
    _drop_record(proj)
    teardown_project(str(proj))
    assert _snapshot(proj) == before


def test_allow_mac_teardown_no_record_keeps_user_prior_entry(tmp_path):
    proj = _proj(tmp_path)
    mine = {"command": "/x/engine", "args": MAC_ARGS}
    _put(proj, MCP, {"mcpServers": {MAC_SERVER: mine}})
    _put(proj, SL, {"enabledMcpjsonServers": [MAC_SERVER], "permissions": {"allow": ["mcp__labhand-mac"]}})
    before = _snapshot(proj)
    _setup(proj, allow_mac=True)
    _drop_record(proj)
    teardown_project(str(proj))
    assert _snapshot(proj) == before


def test_allow_mac_teardown_no_record_user_file_without_backup_kept(tmp_path):
    # no record and no backup, and the file holds more than setup's labhand-mac parts: nothing proves they
    # are setup's, so they stay (as beacon's own legacy rule does)
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    s = _get(proj, SL)
    s["model"] = "opus"
    _put(proj, SL, s)
    _drop_record(proj)
    teardown_project(str(proj))
    s = _get(proj, SL)
    assert s["model"] == "opus" and "mcp__labhand-mac" in s["permissions"]["allow"]


@pytest.mark.parametrize("stop_at", ["settings.local.json", MCP])
def test_allow_mac_revoke_interrupted(tmp_path, monkeypatch, stop_at):
    # a revoking run stops before (or while) writing a wired file: the record still lists the labhand-mac
    # parts, so the next run without the grant takes them back and teardown leaves nothing
    import beacon.project as project
    proj = _proj(tmp_path)
    _put(proj, SL, {"model": "opus"})
    _setup(proj, allow_mac=True)
    real = project._write

    def stop(path, *a, **kw):
        if os.path.basename(path) == stop_at:
            raise OSError("interrupted")
        return real(path, *a, **kw)
    monkeypatch.setattr(project, "_write", stop)
    with pytest.raises(OSError):
        _setup(proj)
    monkeypatch.setattr(project, "_write", real)
    _setup(proj)
    _no_mac_anywhere(proj)
    teardown_project(str(proj))
    assert _get(proj, SL) == {"model": "opus"} and _get(proj, MCP) is None


def test_allow_mac_revoke_dry_run_writes_nothing(tmp_path):
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    before = _snapshot(proj)
    changes = _setup(proj, dry_run=True)
    assert any("no --allow-mac" in c for c in changes)
    assert _snapshot(proj) == before


# ---------------------------------------------------------------- R2 F1: a lost labhand-mac settings part
def _beacon_hooks_left(proj):
    return "beacon.hooks" in json.dumps((_get(proj, SL) or {}).get("hooks", {}))


def _r2_f1_check(proj):
    """After the re-run with the grant: the record still holds beacon's hooks, and teardown restores the
    user's file exactly (beacon's hooks gone too)."""
    from beacon.project import RECORD
    assert _get(proj, RECORD)["files"]["settings.local.json"]["hooks"]
    teardown_project(str(proj))
    assert not _beacon_hooks_left(proj)
    assert _get(proj, SL) == {"model": "opus"} and _get(proj, MCP) is None


def test_allow_mac_r2_f1_a_disabled_server_keeps_beacon_record(tmp_path):
    # labhand-mac taken out of enabledMcpjsonServers (by hand or /mcp disable), then ccbridge re-runs setup
    proj = _proj(tmp_path)
    _put(proj, SL, {"model": "opus"})
    _setup(proj, allow_mac=True)
    s = _get(proj, SL)
    s["enabledMcpjsonServers"] = [n for n in s["enabledMcpjsonServers"] if n != MAC_SERVER]
    _put(proj, SL, s)
    _setup(proj, allow_mac=True)
    assert MAC_SERVER in _get(proj, SL)["enabledMcpjsonServers"]       # enabled again, and recorded as setup's
    _r2_f1_check(proj)


def test_allow_mac_r2_f1_b_removed_allow_rule_keeps_beacon_record(tmp_path):
    proj = _proj(tmp_path)
    _put(proj, SL, {"model": "opus"})
    _setup(proj, allow_mac=True)
    s = _get(proj, SL)
    s["permissions"]["allow"] = [r for r in s["permissions"]["allow"] if r != "mcp__labhand-mac"]
    _put(proj, SL, s)
    _setup(proj, allow_mac=True)
    assert "mcp__labhand-mac" in _get(proj, SL)["permissions"]["allow"]
    _r2_f1_check(proj)


def test_allow_mac_r2_f1_c_interrupted_revocation_then_grant(tmp_path, monkeypatch):
    # a revoking run stopped after the settings file and before .mcp.json; the next run has the grant again
    import beacon.project as project
    proj = _proj(tmp_path)
    _put(proj, SL, {"model": "opus"})
    _setup(proj, allow_mac=True)
    real = project._write

    def stop(path, *a, **kw):
        if os.path.basename(path) == MCP:
            raise KeyboardInterrupt("interrupted")
        return real(path, *a, **kw)
    monkeypatch.setattr(project, "_write", stop)
    with pytest.raises(KeyboardInterrupt):
        _setup(proj)
    monkeypatch.setattr(project, "_write", real)
    assert MAC_SERVER not in _get(proj, SL).get("enabledMcpjsonServers", [])
    _setup(proj, allow_mac=True)
    _r2_f1_check(proj)


def test_allow_mac_r2_f1_user_approval_kept_after_part_lost(tmp_path):
    # the user's own per-tool approval from before setup stays theirs when a labhand-mac part is lost and
    # the grant is given again
    proj = _proj(tmp_path)
    _put(proj, SL, {"model": "opus", "permissions": {"allow": ["mcp__labhand-mac__mac_screenshot"]}})
    _setup(proj, allow_mac=True)
    s = _get(proj, SL)
    s["enabledMcpjsonServers"] = []
    _put(proj, SL, s)
    _setup(proj, allow_mac=True)
    teardown_project(str(proj))
    assert _get(proj, SL) == {"model": "opus", "permissions": {"allow": ["mcp__labhand-mac__mac_screenshot"]}}


# ---------------------------------------------------------------- R2 F2: record lost, re-run without the grant
def test_allow_mac_r2_f2_record_lost_fresh_project_revoked(tmp_path):
    # setup created the files (no backup); the record is gone; a setup without the grant takes labhand-mac back
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    _drop_record(proj)
    changes = _setup(proj)
    assert any("no --allow-mac" in c for c in changes)
    _no_mac_anywhere(proj)
    assert "beacon" in _get(proj, MCP)["mcpServers"] and _get(proj, SL)["hooks"]
    assert _setup(proj) == []
    teardown_project(str(proj))
    assert _get(proj, SL) is None and _get(proj, MCP) is None


def test_allow_mac_r2_f2_record_lost_with_backup_revoked(tmp_path):
    proj = _proj(tmp_path)
    _put(proj, SL, {"model": "opus"})
    _put(proj, MCP, {"mcpServers": {"other": {"command": "x"}}})
    before = _snapshot(proj)
    _setup(proj, allow_mac=True)
    s = _get(proj, SL)
    s["permissions"]["allow"].append("mcp__labhand-mac__mac_click")          # approved during the run
    _put(proj, SL, s)
    _drop_record(proj)
    _setup(proj)
    _no_mac_anywhere(proj)
    assert _get(proj, MCP)["mcpServers"]["other"] == {"command": "x"}
    teardown_project(str(proj))
    assert _snapshot(proj) == before


def test_allow_mac_r2_f2_record_lost_user_prior_entry_stays(tmp_path):
    # the user's own labhand-mac (a different entry, its enable, its rule) from before setup stays
    proj = _proj(tmp_path)
    mine = {"command": "/x/engine", "args": ["serve"], "env": {"A": "1"}}
    _put(proj, MCP, {"mcpServers": {MAC_SERVER: mine}})
    _put(proj, SL, {"enabledMcpjsonServers": [MAC_SERVER], "permissions": {"allow": ["mcp__labhand-mac"]}})
    before = _snapshot(proj)
    _setup(proj, allow_mac=True)
    _drop_record(proj)
    _setup(proj)
    assert _get(proj, MCP)["mcpServers"][MAC_SERVER] == mine
    s = _get(proj, SL)
    assert s["enabledMcpjsonServers"] == [MAC_SERVER] and "mcp__labhand-mac" in s["permissions"]["allow"]
    teardown_project(str(proj))
    assert _snapshot(proj) == before


def test_allow_mac_r2_f2_record_lost_user_file_without_backup_kept(tmp_path):
    # no record, no backup, and the file holds more than setup's parts: nothing proves labhand-mac is
    # setup's, so it stays (teardown's judgement)
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    s = _get(proj, SL)
    s["model"] = "opus"
    _put(proj, SL, s)
    _drop_record(proj)
    _setup(proj)
    assert "mcp__labhand-mac" in _get(proj, SL)["permissions"]["allow"]
    assert MAC_SERVER not in _get(proj, MCP)["mcpServers"]       # .mcp.json holds nothing else: taken back


@pytest.mark.parametrize("stop_at", ["settings.local.json", MCP])
def test_allow_mac_r2_f2_record_lost_revoke_interrupted(tmp_path, monkeypatch, stop_at):
    # the record-less revoke stops before a wired file is written: the record it wrote first lists the
    # labhand-mac parts, so the next run takes them back and teardown leaves nothing
    import beacon.project as project
    proj = _proj(tmp_path)
    _put(proj, SL, {"model": "opus"})
    _setup(proj, allow_mac=True)
    _drop_record(proj)
    real = project._write

    def stop(path, *a, **kw):
        if os.path.basename(path) == stop_at:
            raise OSError("interrupted")
        return real(path, *a, **kw)
    monkeypatch.setattr(project, "_write", stop)
    with pytest.raises(OSError):
        _setup(proj)
    monkeypatch.setattr(project, "_write", real)
    _setup(proj)
    _no_mac_anywhere(proj)
    teardown_project(str(proj))
    assert _get(proj, SL) == {"model": "opus"} and _get(proj, MCP) is None


def test_allow_mac_r2_f2_record_lost_with_flag_keeps_grant(tmp_path):
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    _drop_record(proj)
    _setup(proj, allow_mac=True)
    assert MAC_SERVER in _get(proj, MCP)["mcpServers"]
    assert MAC_SERVER in _get(proj, SL)["enabledMcpjsonServers"]
    # the new record covers beacon's and labhand-mac's parts both, so teardown leaves nothing
    teardown_project(str(proj))
    assert _get(proj, MCP) is None and _get(proj, SL) is None


# ---------------------------------------------------------------- R3 G1: a lost mcp__labhand rule

def _user_start(proj):
    """A project with the user's own settings and MCP content, which must come back after teardown."""
    sl = {"permissions": {"allow": ["Read(./x)"]}, "model": "m"}
    mcp = {"mcpServers": {"u": {"command": "u"}}}
    _put(proj, SL, sl)
    _put(proj, MCP, mcp)
    return sl, mcp


def _settings_rec(proj):
    return _get(proj, _project.RECORD)["files"]["settings.local.json"]


@pytest.mark.parametrize("allow_mac", [False, True])
def test_allow_mac_r3_g1_removed_labhand_rule_keeps_record(tmp_path, allow_mac):
    # the user removes the --allow-labhand rule by hand; the next setup with the same flags must still
    # know beacon's parts (and the labhand-mac grant) as setup's, so teardown leaves the user's files
    proj = _proj(tmp_path)
    sl, mcp = _user_start(proj)
    _setup(proj, allow_labhand=True, allow_mac=allow_mac)
    s = _get(proj, SL)
    s["permissions"]["allow"].remove("mcp__labhand")
    _put(proj, SL, s)
    _setup(proj, allow_labhand=True, allow_mac=allow_mac)
    assert _settings_rec(proj)["hooks"]                    # beacon's hooks still recorded as setup's
    teardown_project(str(proj))
    assert _get(proj, SL) == sl and _get(proj, MCP) == mcp


@pytest.mark.parametrize("allow_mac", [False, True])
def test_allow_mac_r3_g1_interrupted_labhand_run_keeps_record(tmp_path, allow_mac, monkeypatch):
    # a setup adding --allow-labhand stops after its record write, before the settings write: the record
    # lists mcp__labhand the file never got; the restart without the flag must keep the rest of the record
    proj = _proj(tmp_path)
    sl, mcp = _user_start(proj)
    _setup(proj, allow_mac=allow_mac)
    real, n = _project._write, [0]

    def stop(path, *a, **k):
        if n[0] == 1:
            raise KeyboardInterrupt
        n[0] += 1
        return real(path, *a, **k)
    monkeypatch.setattr(_project, "_write", stop)
    with pytest.raises(KeyboardInterrupt):
        _setup(proj, allow_labhand=True, allow_mac=allow_mac)
    monkeypatch.setattr(_project, "_write", real)
    # the interruption hit where meant: recorded, not written
    assert "mcp__labhand" not in _get(proj, SL)["permissions"]["allow"]
    assert "mcp__labhand" in _settings_rec(proj)["allow"]
    _setup(proj, allow_mac=allow_mac)
    teardown_project(str(proj))
    assert _get(proj, SL) == sl and _get(proj, MCP) == mcp


def test_allow_mac_r3_g1_user_labhand_rule_added_later_stays(tmp_path):
    # the pruning only forgets a recorded mcp__labhand the file lacks; one the user adds back after that
    # setup run is theirs and survives teardown (the existing "added since stays yours" rule)
    proj = _proj(tmp_path)
    _user_start(proj)
    _setup(proj, allow_labhand=True, allow_mac=True)
    s = _get(proj, SL)
    s["permissions"]["allow"].remove("mcp__labhand")
    _put(proj, SL, s)
    _setup(proj, allow_mac=True)
    s = _get(proj, SL)
    s["permissions"]["allow"].append("mcp__labhand")
    _put(proj, SL, s)
    teardown_project(str(proj))
    assert _get(proj, SL) == {"permissions": {"allow": ["Read(./x)", "mcp__labhand"]}, "model": "m"}


# ---------------------------------------------------------------- R3 G2: record-less, one mac part lost

def _after_mac_part_lost(edit):
    # what teardown leaves of a file setup created, with the record intact, after the same edit: the list
    # the user emptied stays (the record-less path must not leave more, nor less)
    return {"enabledMcpjsonServers": []} if edit == "disable" else None


def _lose_mac_part(proj, edit):
    s = _get(proj, SL)
    if edit == "disable":
        s["enabledMcpjsonServers"].remove(MAC_SERVER)
    else:
        s["permissions"]["allow"].remove("mcp__labhand-mac")
    _put(proj, SL, s)
    _drop_record(proj)


@pytest.mark.parametrize("edit", ["disable", "rmallow"])
def test_allow_mac_r3_g2_teardown_removes_remaining_mac_part(tmp_path, edit):
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    _lose_mac_part(proj, edit)
    teardown_project(str(proj))
    assert _get(proj, SL) == _after_mac_part_lost(edit) and _get(proj, MCP) is None


@pytest.mark.parametrize("edit", ["disable", "rmallow"])
def test_allow_mac_r3_g2_setup_without_flag_removes_remaining_mac_part(tmp_path, edit):
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    _lose_mac_part(proj, edit)
    _setup(proj)
    s = _get(proj, SL)
    assert "mcp__labhand-mac" not in s.get("permissions", {}).get("allow", [])
    assert MAC_SERVER not in s.get("enabledMcpjsonServers", [])
    assert MAC_SERVER not in _get(proj, MCP)["mcpServers"]
    teardown_project(str(proj))
    # beacon's own parts are recognised too (the emptied list is no "other content"), so they go
    assert _get(proj, SL) == _after_mac_part_lost(edit) and _get(proj, MCP) is None


@pytest.mark.parametrize("edit", ["disable", "rmallow"])
def test_allow_mac_r3_g2_other_content_keeps_mac_part(tmp_path, edit):
    # a file with anything else in it is not judged without a backup: the remaining labhand-mac part stays
    proj = _proj(tmp_path)
    _setup(proj, allow_mac=True)
    s = _get(proj, SL)
    s["permissions"]["allow"].append("Read(./u)")
    _put(proj, SL, s)
    _lose_mac_part(proj, edit)
    teardown_project(str(proj))
    s = _get(proj, SL)
    assert "Read(./u)" in s["permissions"]["allow"]
    assert "mcp__labhand-mac" in s["permissions"]["allow"] or MAC_SERVER in s.get("enabledMcpjsonServers", [])
