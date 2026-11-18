# -*- coding: utf-8 -*-
"""scripts/install_cua_driver.py with fakes for network, codesign/spctl/ditto/lsregister and
cua-driver: nothing here downloads, installs or runs the real Cua Driver."""
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import plistlib
import shutil
import sys
import tarfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("install_cua_driver",
                                               ROOT / "scripts" / "install_cua_driver.py")
inst = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = inst  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(inst)


def make_tarball(version=inst.VERSION, bundle_id=inst.BUNDLE_ID, extra=None):
    """A tar.gz shaped like the release: TOP_DIR/CuaDriver.app/Contents/{Info.plist,MacOS/cua-driver}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        def add(name, data, mode=0o644):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode = len(data), mode
            tf.addfile(ti, io.BytesIO(data))
        app = f"{inst.TOP_DIR}/{inst.APP_NAME}/Contents"
        add(f"{app}/Info.plist", plistlib.dumps(
            {"CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version}))
        add(f"{app}/MacOS/cua-driver", b"#!/bin/false\n", 0o755)
        for ti, data in (extra or []):
            tf.addfile(ti, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def write_app(app: pathlib.Path, version=inst.VERSION, marker=b"old"):
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleIdentifier": inst.BUNDLE_ID, "CFBundleShortVersionString": version}))
    (app / "Contents/MacOS/cua-driver").write_bytes(marker)


class FakeSystem(inst.System):
    def __init__(self, paths, tarball=None, platform=("Darwin", "arm64")):
        self.paths = paths
        self.tarball = tarball if tarball is not None else make_tarball()
        self.plat = platform
        self.calls = []          # every argv, in order
        self.downloads = []
        self.bad_sig = set()     # path-name substrings whose codesign fails
        self.bad_spctl = set()
        self.fail = set()        # tool names that fail: ditto, lsregister, telemetry, download
        self.version_out = f"cua-driver {inst.VERSION}"

    def platform(self):
        return self.plat

    def download(self, url, dest):
        self.downloads.append(url)
        if "download" in self.fail:
            raise OSError("network unreachable")
        dest.write_bytes(self.tarball)
        return hashlib.sha256(self.tarball).hexdigest()

    def run(self, argv, env=None, timeout=120):
        self.calls.append(list(argv))
        tool = os.path.basename(argv[0])
        target = argv[-1]
        if tool == "codesign":
            if any(s in target for s in self.bad_sig):
                return 3, "test-requirement: code failed to satisfy specified code requirement(s)"
            if "-dv" in argv:
                return 0, f"Executable=x\nIdentifier={inst.BUNDLE_ID}\nTeamIdentifier={inst.TEAM_ID}\n"
            return 0, ""
        if tool == "spctl":
            if any(s in target for s in self.bad_spctl):
                return 3, f"{target}: rejected"
            return 0, (f"{target}: accepted\nsource=Notarized Developer ID\n"
                       f"origin=Developer ID Application: Cua AI, Inc. ({inst.TEAM_ID})")
        if tool == "ditto":
            if "ditto" in self.fail:
                pathlib.Path(argv[2]).mkdir(parents=True)  # a partial copy
                return 1, "ditto: No space left on device"
            shutil.copytree(argv[1], argv[2], symlinks=True)
            return 0, ""
        if tool == "lsregister":
            return (1, "lsregister failed") if "lsregister" in self.fail else (0, "")
        if tool == "cua-driver":
            assert env and env.get("CUA_DRIVER_RS_TELEMETRY_ENABLED") == "0"
            sub = argv[1:]
            if sub == ["telemetry", "disable"]:
                if "telemetry" in self.fail:
                    return 1, "boom"
                self.paths.config.parent.mkdir(parents=True, exist_ok=True)
                self.paths.config.write_text('{"telemetry_enabled": false}')
                return 0, "Telemetry disabled."
            if sub == ["--version"]:
                return 0, self.version_out + "\n"
            if sub == ["stop"]:
                return 0, "stopped"
            if sub[:2] == ["permissions", "status"]:
                return 0, json.dumps({"accessibility": True, "screen_recording": True,
                                      "daemon_running": True, "source": {"attribution": "driver-daemon"}})
            return 2, "unexpected"
        raise AssertionError(f"unexpected command {argv}")

    def cua_calls(self):
        return [c[1:] for c in self.calls if os.path.basename(c[0]) == "cua-driver"]

    def tools(self):
        return [os.path.basename(c[0]) for c in self.calls]


@pytest.fixture
def env(tmp_path):
    paths = inst.Paths(app=tmp_path / "Applications" / inst.APP_NAME,
                       bin_link=tmp_path / "home/.local/bin/cua-driver",
                       config=tmp_path / "home/.cua-driver/config.json")
    (tmp_path / "Applications").mkdir()
    fake = FakeSystem(paths)
    sha = hashlib.sha256(fake.tarball).hexdigest()
    logs = []

    def do_install():
        return inst.install(fake, paths, log=logs.append, sha256=sha)
    return paths, fake, do_install, logs


def test_install_cua_pins_the_release():
    assert inst.URL == ("https://github.com/trycua/cua/releases/download/cua-driver-rs-v0.53.2/"
                        "cua-driver-rs-0.53.2-darwin-arm64.tar.gz")
    assert inst.SHA256 == "2d8954c3a5000c2247bccbdb714adb7df00af3aab3ac1403f22a92b335b9d20a"
    assert inst.BUNDLE_ID == "com.trycua.driver" and inst.TEAM_ID == "YCK386LBJ7"
    assert inst.Paths().app == pathlib.Path("/Applications/CuaDriver.app")
    assert inst.Paths().bin_link == pathlib.Path.home() / ".local/bin/cua-driver"
    assert 'identifier "com.trycua.driver"' in inst.REQUIREMENT and '"YCK386LBJ7"' in inst.REQUIREMENT


def test_install_cua_fresh_install_steps_in_order(env):
    paths, fake, do_install, logs = env
    assert do_install() is True
    assert fake.downloads == [inst.URL]
    cua = fake.cua_calls()
    assert cua[0] == ["telemetry", "disable"], "telemetry must be the first cua-driver command"
    assert ["stop"] not in cua  # nothing was installed before
    assert cua[-1] == ["--version"]
    tools = fake.tools()
    assert tools.index("ditto") < tools.index("lsregister")
    assert tools.index("cua-driver") < tools.index("ditto")
    # the installed app was re-verified (codesign + spctl on the destination)
    assert ["/usr/bin/codesign", "--verify", "--deep", "--strict", f"-R={inst.REQUIREMENT}",
            str(paths.app)] in fake.calls
    assert ["/usr/sbin/spctl", "-a", "-vv", "-t", "exec", str(paths.app)] in fake.calls
    assert [inst.LSREGISTER, "-f", str(paths.app)] in fake.calls
    assert inst.read_bundle(paths.app) == (inst.BUNDLE_ID, inst.VERSION)
    assert os.readlink(paths.bin_link) == str(paths.app_binary)
    assert inst.telemetry_disabled(paths.config) is True
    assert not list(paths.app.parent.glob("*.install-backup.*"))


def test_install_cua_is_idempotent(env):
    paths, fake, do_install, logs = env
    do_install()
    fake.calls.clear(); fake.downloads.clear(); logs.clear()
    assert do_install() is False
    assert fake.downloads == []
    assert "ditto" not in fake.tools() and "lsregister" not in fake.tools()
    assert fake.cua_calls() == []
    assert logs[-1] == "no change"


def test_install_cua_valid_app_only_repairs_link_and_telemetry(env):
    paths, fake, do_install, logs = env
    write_app(paths.app)
    assert do_install() is True
    assert fake.downloads == [] and "ditto" not in fake.tools()
    assert fake.cua_calls() == [["telemetry", "disable"]]
    assert os.readlink(paths.bin_link) == str(paths.app_binary)
    assert (paths.app_binary).read_bytes() == b"old"


def test_install_cua_sha_mismatch_installs_nothing(env):
    paths, fake, _, _ = env
    with pytest.raises(inst.InstallError, match="SHA256 mismatch"):
        inst.install(fake, paths, log=lambda m: None)  # the real pinned digest != fake tarball
    assert not paths.app.exists() and not paths.bin_link.is_symlink()
    assert fake.cua_calls() == [] and "ditto" not in fake.tools()


def test_install_cua_download_error_is_reported(env):
    paths, fake, do_install, _ = env
    fake.fail.add("download")
    with pytest.raises(inst.InstallError, match="download failed"):
        do_install()
    assert not paths.app.exists()


def test_install_cua_bad_staged_signature_keeps_existing_app(env):
    paths, fake, do_install, _ = env
    write_app(paths.app, version="0.50.0")
    fake.bad_sig.add("stage")
    with pytest.raises(inst.InstallError, match="staged"):
        do_install()
    assert inst.read_bundle(paths.app) == (inst.BUNDLE_ID, "0.50.0")
    assert "ditto" not in fake.tools() and fake.cua_calls() == []


def test_install_cua_spctl_rejection_installs_nothing(env):
    paths, fake, do_install, _ = env
    fake.bad_spctl.add("stage")
    with pytest.raises(inst.InstallError):
        do_install()
    assert not paths.app.exists()


def test_install_cua_wrong_bundle_id_in_tarball_refused(tmp_path, env):
    paths, fake, _, _ = env
    fake.tarball = make_tarball(bundle_id="com.evil.driver")
    sha = hashlib.sha256(fake.tarball).hexdigest()
    with pytest.raises(inst.InstallError, match="staged"):
        inst.install(fake, paths, log=lambda m: None, sha256=sha)
    assert not paths.app.exists()


def test_install_cua_replaces_other_version_and_stops_daemon(env):
    paths, fake, do_install, _ = env
    write_app(paths.app, version="0.50.0")
    assert do_install() is True
    assert inst.read_bundle(paths.app) == (inst.BUNDLE_ID, inst.VERSION)
    cua = fake.cua_calls()
    assert cua[0] == ["telemetry", "disable"] and ["stop"] in cua
    assert not list(paths.app.parent.glob("*.install-backup.*"))


@pytest.mark.parametrize("failing", ["lsregister", "ditto"])
def test_install_cua_failure_after_backup_rolls_back(env, failing):
    paths, fake, do_install, logs = env
    write_app(paths.app, version="0.50.0", marker=b"previous")
    fake.fail.add(failing)
    with pytest.raises(inst.InstallError):
        do_install()
    assert inst.read_bundle(paths.app) == (inst.BUNDLE_ID, "0.50.0")
    assert paths.app_binary.read_bytes() == b"previous"
    assert not list(paths.app.parent.glob("*.install-backup.*"))
    assert any("rolled back" in m for m in logs)


def test_install_cua_failure_without_previous_app_leaves_nothing(env):
    paths, fake, do_install, _ = env
    fake.fail.add("ditto")
    with pytest.raises(inst.InstallError, match="ditto"):
        do_install()
    assert not paths.app.exists() and not paths.bin_link.is_symlink()


def test_install_cua_telemetry_failure_stops_before_install(env):
    paths, fake, do_install, _ = env
    fake.fail.add("telemetry")
    with pytest.raises(inst.InstallError, match="telemetry"):
        do_install()
    assert "ditto" not in fake.tools() and not paths.app.exists()


def test_install_cua_version_mismatch_after_install_fails(env):
    paths, fake, do_install, _ = env
    fake.version_out = "cua-driver 0.99.0"
    with pytest.raises(inst.InstallError, match="--version"):
        do_install()


@pytest.mark.parametrize("args", [("update",), ("update", "--apply"), ("check-update",),
                                  ("channel", "set", "nightly", "--apply"), (),
                                  ("call", "check_for_update"), ("call", "update")])
def test_install_cua_never_runs_update(env, args):
    paths, fake, _, _ = env
    with pytest.raises(inst.InstallError, match="refused"):
        inst.run_cua(fake, paths.app_binary, *args)
    assert fake.calls == []


def test_install_cua_source_never_calls_update():
    src = (ROOT / "scripts" / "install_cua_driver.py").read_text()
    assert '"update"' not in src.replace('FORBIDDEN_ARGS = {"update", "check-update", "check_for_update", "--apply"}', "")


def test_install_cua_refuses_other_platforms(env):
    paths, fake, do_install, _ = env
    fake.plat = ("Darwin", "x86_64")
    with pytest.raises(inst.InstallError, match="darwin-arm64"):
        do_install()
    assert fake.downloads == []


@pytest.mark.parametrize("name,kind,link", [
    ("../evil", tarfile.REGTYPE, ""),
    ("/etc/evil", tarfile.REGTYPE, ""),
    ("other-top/x", tarfile.REGTYPE, ""),
    (f"{inst.TOP_DIR}/l", tarfile.SYMTYPE, "../../etc/passwd"),
    (f"{inst.TOP_DIR}/l", tarfile.SYMTYPE, "/etc/passwd"),
    (f"{inst.TOP_DIR}/h", tarfile.LNKTYPE, "/etc/passwd"),
    (f"{inst.TOP_DIR}/d", tarfile.CHRTYPE, ""),
])
def test_install_cua_rejects_unsafe_tar_members(env, name, kind, link):
    paths, fake, _, _ = env
    ti = tarfile.TarInfo(name)
    ti.type, ti.linkname = kind, link
    fake.tarball = make_tarball(extra=[(ti, b"" if kind == tarfile.REGTYPE else None)])
    sha = hashlib.sha256(fake.tarball).hexdigest()
    with pytest.raises(inst.InstallError, match="unsafe|escapes|special"):
        inst.install(fake, paths, log=lambda m: None, sha256=sha)
    assert not paths.app.exists() and fake.cua_calls() == []


def test_install_cua_safe_inner_symlink_is_kept(tmp_path):
    ti = tarfile.TarInfo(f"{inst.TOP_DIR}/{inst.APP_NAME}/Contents/MacOS/alias")
    ti.type, ti.linkname = tarfile.SYMTYPE, "cua-driver"
    tb = tmp_path / "t.tar.gz"
    tb.write_bytes(make_tarball(extra=[(ti, None)]))
    out = tmp_path / "out"; out.mkdir()
    inst.safe_extract(tb, out)
    assert os.readlink(out / inst.TOP_DIR / inst.APP_NAME / "Contents/MacOS/alias") == "cua-driver"


def test_install_cua_link_regular_file_is_not_clobbered(env):
    paths, fake, do_install, _ = env
    write_app(paths.app)
    paths.bin_link.parent.mkdir(parents=True)
    paths.bin_link.write_text("mine")
    with pytest.raises(inst.InstallError, match="not a symlink"):
        do_install()
    assert paths.bin_link.read_text() == "mine"


def test_install_cua_wrong_link_target_is_fixed(env):
    paths, fake, do_install, _ = env
    write_app(paths.app)
    paths.bin_link.parent.mkdir(parents=True)
    os.symlink("/somewhere/else", paths.bin_link)
    paths.config.parent.mkdir(parents=True)
    paths.config.write_text('{"telemetry_enabled": false}')
    assert do_install() is True
    assert os.readlink(paths.bin_link) == str(paths.app_binary)
    assert fake.cua_calls() == []


@pytest.mark.parametrize("text,want", [(None, False), ('{"telemetry_enabled": true}', False),
                                       ('{"telemetry_enabled": false}', True), ("{bad", None)])
def test_install_cua_telemetry_config_reading(tmp_path, text, want):
    cfg = tmp_path / "config.json"
    if text is not None:
        cfg.write_text(text)
    assert inst.telemetry_disabled(cfg) is want


def test_install_cua_check_is_read_only_and_reports(env, capsys):
    paths, fake, _, _ = env
    assert inst.main(["--check"], sys_=fake, paths=paths) == 1
    assert "not installed" in capsys.readouterr().out
    assert fake.calls == []                       # nothing to inspect, nothing run
    write_app(paths.app)
    paths.bin_link.parent.mkdir(parents=True)
    os.symlink(paths.app_binary, paths.bin_link)
    paths.config.parent.mkdir(parents=True)
    paths.config.write_text('{"telemetry_enabled": false}')
    before = sorted(p.relative_to(paths.app.parent.parent) for p in paths.app.parent.parent.rglob("*"))
    assert inst.main(["--check", "--json"], sys_=fake, paths=paths) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["permissions"]["accessibility"] is True
    assert set(fake.tools()) <= {"codesign", "spctl", "cua-driver"}
    assert fake.cua_calls() == [["--version"], ["permissions", "status", "--json"]]
    assert fake.downloads == []
    after = sorted(p.relative_to(paths.app.parent.parent) for p in paths.app.parent.parent.rglob("*"))
    assert before == after


def test_install_cua_check_flags_telemetry_on(env, capsys):
    paths, fake, _, _ = env
    write_app(paths.app)
    paths.bin_link.parent.mkdir(parents=True)
    os.symlink(paths.app_binary, paths.bin_link)
    assert inst.main(["--check"], sys_=fake, paths=paths) == 1
    out = capsys.readouterr().out
    assert "NOT disabled" in out and ["telemetry", "disable"] not in fake.cua_calls()
