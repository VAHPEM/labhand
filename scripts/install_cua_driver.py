#!/usr/bin/env python3
"""Pinned installer for Cua Driver (trycua/cua, MIT) on this Mac: the engine behind the
``labhand-mac`` MCP server. Standard library only.

    python scripts/install_cua_driver.py            # install / repair the pinned version
    python scripts/install_cua_driver.py --check    # read-only report (exit 0 = all good)
    python scripts/install_cua_driver.py --check --json

What the install does, in order (each step stops the run on failure, nothing is half-installed):

1. Already installed at the pinned version with a valid signature -> the app is left alone;
   only a missing / wrong ``~/.local/bin/cua-driver`` link or telemetry still on is fixed.
2. Download the pinned ``darwin-arm64`` tarball from the GitHub release (https only) into a
   fresh temp dir and check its SHA256 against the pinned digest.
3. Extract (member paths validated, nothing outside the staging dir), then check the staged
   ``CuaDriver.app``: Info.plist bundle id + version, ``codesign --verify --deep --strict``
   against a designated requirement (Developer ID, identifier ``com.trycua.driver``, team
   ``YCK386LBJ7``), and ``spctl`` (Gatekeeper: notarized Developer ID).
4. ``cua-driver telemetry disable`` with the staged binary: the first cua-driver command run.
   Every cua-driver call made by this script also runs with ``CUA_DRIVER_RS_TELEMETRY_ENABLED=0``.
5. Stop a running daemon (best effort), move any existing app aside, ``ditto`` the staged app to
   ``/Applications/CuaDriver.app``, re-check signature + spctl + version there, ``lsregister -f``.
   Any failure rolls the previous app back.
6. Symlink ``~/.local/bin/cua-driver`` -> the app's binary; ``cua-driver --version`` must print
   the pinned version.

It never runs ``cua-driver update`` / ``check-update`` / ``--apply`` (refused in code) and never
changes TCC: grant Accessibility + Screen Recording once with ``cua-driver permissions grant``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

VERSION = "0.53.2"
TAG = f"cua-driver-rs-v{VERSION}"
ASSET = f"cua-driver-rs-{VERSION}-darwin-arm64.tar.gz"
URL = f"https://github.com/trycua/cua/releases/download/{TAG}/{ASSET}"
SHA256 = "2d8954c3a5000c2247bccbdb714adb7df00af3aab3ac1403f22a92b335b9d20a"
BUNDLE_ID = "com.trycua.driver"
TEAM_ID = "YCK386LBJ7"
APP_NAME = "CuaDriver.app"
TOP_DIR = f"cua-driver-rs-{VERSION}-darwin-arm64"
# Developer ID Application leaf (…6.1.13) issued by the Developer ID CA (…6.2.6), Apple-anchored,
# with the pinned identifier and team: checked by codesign, not by parsing text.
REQUIREMENT = (
    f'anchor apple generic and identifier "{BUNDLE_ID}"'
    " and certificate 1[field.1.2.840.113635.100.6.2.6] exists"
    " and certificate leaf[field.1.2.840.113635.100.6.1.13] exists"
    f' and certificate leaf[subject.OU] = "{TEAM_ID}"'
)
LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/"
    "LaunchServices.framework/Versions/A/Support/lsregister"
)
# Refused anywhere in a cua-driver argv: CLI subcommands, the MCP tool via `call`, the apply flag.
FORBIDDEN_ARGS = {"update", "check-update", "check_for_update", "--apply"}
MAX_DOWNLOAD = 512 * 1024 * 1024


class InstallError(RuntimeError):
    pass


@dataclass
class Paths:
    app: Path = Path("/Applications") / APP_NAME
    bin_link: Path = field(default_factory=lambda: Path.home() / ".local/bin/cua-driver")
    config: Path = field(default_factory=lambda: Path.home() / ".cua-driver/config.json")

    @property
    def app_binary(self) -> Path:
        return self.app / "Contents/MacOS/cua-driver"


class System:
    """Everything that touches the outside world; tests replace it with a fake."""

    def run(self, argv: list[str], env: dict | None = None, timeout: int = 120) -> tuple[int, str]:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                               env={**os.environ, **(env or {})})
        except (OSError, subprocess.TimeoutExpired) as e:
            return 127, f"{type(e).__name__}: {e}"
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    def download(self, url: str, dest: Path) -> str:
        """Stream ``url`` to ``dest``; return the sha256 hex of what was written."""
        if not url.startswith("https://"):
            raise InstallError(f"refusing non-https URL {url}")
        h = hashlib.sha256()
        total = 0
        req = urllib.request.Request(url, headers={"User-Agent": "labhand-install-cua-driver"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
            if not resp.geturl().startswith("https://"):
                raise InstallError(f"redirected to non-https URL {resp.geturl()}")
            while chunk := resp.read(1 << 20):
                total += len(chunk)
                if total > MAX_DOWNLOAD:
                    raise InstallError(f"download larger than {MAX_DOWNLOAD} bytes")
                h.update(chunk)
                out.write(chunk)
        return h.hexdigest()

    def platform(self) -> tuple[str, str]:
        return platform.system(), platform.machine()


# ---------------------------------------------------------------- checks (read-only)

def cua_env() -> dict:
    return {"CUA_DRIVER_RS_TELEMETRY_ENABLED": "0", "CUA_TELEMETRY_ENABLED": "0"}


def run_cua(sys_: System, binary: Path, *args: str, timeout: int = 120) -> tuple[int, str]:
    """Run a cua-driver subcommand; ``update`` and friends are refused here, whatever the caller."""
    if not args or FORBIDDEN_ARGS & set(args):
        raise InstallError(f"refused: cua-driver {' '.join(args)} (updates go through this installer only)")
    return sys_.run([str(binary), *args], env=cua_env(), timeout=timeout)


def read_bundle(app: Path) -> tuple[str | None, str | None]:
    try:
        with open(app / "Contents/Info.plist", "rb") as f:
            info = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None, None
    return info.get("CFBundleIdentifier"), info.get("CFBundleShortVersionString")


def check_signature(sys_: System, app: Path) -> tuple[bool, str]:
    rc, out = sys_.run(["/usr/bin/codesign", "--verify", "--deep", "--strict",
                        f"-R={REQUIREMENT}", str(app)])
    if rc != 0:
        return False, f"codesign rc={rc}: {out.strip()[:300]}"
    rc, out = sys_.run(["/usr/bin/codesign", "-dv", "--verbose=2", str(app)])
    fields = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    ident, team = fields.get("Identifier"), fields.get("TeamIdentifier")
    if rc != 0 or ident != BUNDLE_ID or team != TEAM_ID:
        return False, f"codesign -dv rc={rc} Identifier={ident} TeamIdentifier={team}"
    return True, f"Identifier={ident} TeamIdentifier={team} (requirement satisfied)"


def check_spctl(sys_: System, app: Path) -> tuple[bool, str]:
    rc, out = sys_.run(["/usr/sbin/spctl", "-a", "-vv", "-t", "exec", str(app)])
    ok = rc == 0 and "accepted" in out and TEAM_ID in out
    return ok, " ".join(out.split())[:300]


def check_app(sys_: System, app: Path) -> tuple[bool, list[str]]:
    """Bundle id + pinned version + signature + spctl. Returns (ok, notes)."""
    if not (app / "Contents/Info.plist").is_file():
        return False, [f"{app}: not installed"]
    notes = []
    bid, ver = read_bundle(app)
    ok = bid == BUNDLE_ID and ver == VERSION
    notes.append(f"bundle {bid} version {ver} (want {BUNDLE_ID} {VERSION})")
    if not ok:
        return False, notes
    sig_ok, sig = check_signature(sys_, app)
    notes.append(f"signature: {sig}")
    if not sig_ok:
        return False, notes
    sp_ok, sp = check_spctl(sys_, app)
    notes.append(f"spctl: {sp}")
    return sp_ok, notes


def telemetry_disabled(config: Path) -> bool | None:
    """Persisted telemetry setting: True = disabled, False = enabled/default, None = unreadable."""
    try:
        data = json.loads(config.read_text())
    except FileNotFoundError:
        return False  # default is ON
    except (OSError, ValueError):
        return None
    return isinstance(data, dict) and data.get("telemetry_enabled") is False


def link_ok(paths: Paths) -> bool:
    return paths.bin_link.is_symlink() and os.readlink(paths.bin_link) == str(paths.app_binary)


def check(sys_: System, paths: Paths) -> dict:
    app_ok, notes = check_app(sys_, paths.app)
    report: dict = {"pinned": VERSION, "app": str(paths.app), "app_ok": app_ok, "app_notes": notes,
                    "link": str(paths.bin_link), "link_ok": link_ok(paths),
                    "telemetry_disabled": telemetry_disabled(paths.config)}
    report["version_output"] = None
    report["permissions"] = None
    if app_ok:
        rc, out = run_cua(sys_, paths.app_binary, "--version", timeout=30)
        report["version_output"] = out.strip()
        report["version_ok"] = rc == 0 and out.split()[-1:] == [VERSION]
        rc, out = run_cua(sys_, paths.app_binary, "permissions", "status", "--json", timeout=60)
        try:
            perm = json.loads(out[out.index("{"):]) if rc == 0 else None
        except ValueError:
            perm = None
        report["permissions"] = (
            {k: perm.get(k) for k in ("accessibility", "screen_recording", "daemon_running")}
            | {"attribution": (perm.get("source") or {}).get("attribution")}
            if isinstance(perm, dict) else f"rc={rc}: {out.strip()[:200]}")
    else:
        report["version_ok"] = False
    report["ok"] = bool(app_ok and report["version_ok"] and report["link_ok"]
                        and report["telemetry_disabled"] is True)
    return report


def format_report(r: dict) -> str:
    def mark(b):
        return "ok  " if b is True else "FAIL"
    lines = [f"Cua Driver pinned {r['pinned']} ({TAG})",
             f"[{mark(r['app_ok'])}] app {r['app']}"]
    lines += [f"         {n}" for n in r["app_notes"]]
    lines.append(f"[{mark(r['version_ok'])}] version: {r['version_output']}")
    lines.append(f"[{mark(r['link_ok'])}] link {r['link']}")
    td = r["telemetry_disabled"]
    lines.append(f"[{mark(td)}] telemetry: {'disabled (persisted)' if td else 'NOT disabled' if td is False else 'config unreadable'}")
    p = r["permissions"]
    if isinstance(p, dict):
        good = p.get("accessibility") is True and p.get("screen_recording") is True
        lines.append(f"[{'ok  ' if good else 'WARN'}] permissions: accessibility={p.get('accessibility')} "
                     f"screen_recording={p.get('screen_recording')} daemon_running={p.get('daemon_running')} "
                     f"attribution={p.get('attribution')}"
                     + ("" if good else "  (grant once: cua-driver permissions grant)"))
    else:
        lines.append(f"[WARN] permissions: {p}")
    lines.append("OK" if r["ok"] else "NOT OK: run `python scripts/install_cua_driver.py` to install / repair")
    return "\n".join(lines)


# ---------------------------------------------------------------- install

def safe_extract(tarball: Path, dest: Path) -> None:
    """Extract only members that stay inside ``dest`` under TOP_DIR; links may not escape."""
    with tarfile.open(tarball, "r:gz") as tf:
        for m in tf.getmembers():
            p = PurePosixPath(m.name)
            if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] != TOP_DIR:
                raise InstallError(f"unsafe tar member {m.name!r}")
            if m.isdev() or m.isfifo():
                raise InstallError(f"unexpected special file {m.name!r}")
            if m.islnk() or m.issym():
                target = PurePosixPath(m.linkname)
                resolved = target if m.islnk() else p.parent / target
                if target.is_absolute() or _escapes(resolved):
                    raise InstallError(f"tar link escapes: {m.name!r} -> {m.linkname!r}")
    # bsdtar keeps the bundle exactly as packed (modes, symlinks) once the names are known safe.
    p = subprocess.run(["/usr/bin/tar", "-xzf", str(tarball), "-C", str(dest)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise InstallError(f"tar -x failed rc={p.returncode}: {p.stderr.strip()[:300]}")


def _escapes(p: PurePosixPath) -> bool:
    depth = 0
    for part in p.parts:
        depth += -1 if part == ".." else 0 if part == "." else 1
        if depth < 0:
            return True
    return False


def ensure_link(paths: Paths, log) -> bool:
    """Point the CLI link at the app binary. Returns True if it changed anything."""
    if link_ok(paths):
        return False
    link = paths.bin_link
    if link.exists() and not link.is_symlink():
        raise InstallError(f"{link} exists and is not a symlink; move it away and rerun")
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.tmp{os.getpid()}")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(paths.app_binary, tmp)
    os.replace(tmp, link)
    log(f"linked {link} -> {paths.app_binary}")
    return True


def ensure_telemetry_off(sys_: System, binary: Path, paths: Paths, log) -> bool:
    if telemetry_disabled(paths.config) is True:
        return False
    rc, out = run_cua(sys_, binary, "telemetry", "disable", timeout=60)
    if rc != 0 or telemetry_disabled(paths.config) is not True:
        raise InstallError(f"cua-driver telemetry disable failed (rc={rc}): {out.strip()[:300]}")
    log("telemetry disabled (persisted)")
    return True


def install(sys_: System, paths: Paths, log=print, url: str = URL, sha256: str = SHA256) -> bool:
    """Install or repair the pinned version. Returns True if anything changed."""
    system, machine = sys_.platform()
    if (system, machine) != ("Darwin", "arm64"):
        raise InstallError(f"this installer pins the darwin-arm64 build; this is {system} {machine}")

    app_ok, notes = check_app(sys_, paths.app)
    if app_ok:
        log(f"{paths.app}: Cua Driver {VERSION} already installed, signature valid; app left unchanged")
        changed = ensure_telemetry_off(sys_, paths.app_binary, paths, log)
        changed |= ensure_link(paths, log)
        if not changed:
            log("no change")
        return changed
    for n in notes:
        log(f"current: {n}")

    with tempfile.TemporaryDirectory(prefix="cua-driver-install-") as td:
        tmp = Path(td)
        tarball = tmp / ASSET
        log(f"downloading {url}")
        try:
            digest = sys_.download(url, tarball)
        except InstallError:
            raise
        except Exception as e:  # network / HTTP errors
            raise InstallError(f"download failed: {type(e).__name__}: {e}") from e
        if digest != sha256:
            raise InstallError(f"SHA256 mismatch: got {digest}, pinned {sha256}; nothing installed")
        log(f"sha256 ok {digest}")

        stage = tmp / "stage"
        stage.mkdir()
        safe_extract(tarball, stage)
        staged = stage / TOP_DIR / APP_NAME
        st_ok, st_notes = check_app(sys_, staged)
        for n in st_notes:
            log(f"staged: {n}")
        if not st_ok:
            raise InstallError("staged CuaDriver.app failed verification; nothing installed")
        staged_bin = staged / "Contents/MacOS/cua-driver"

        # First cua-driver command of the run: switch telemetry off before anything else.
        ensure_telemetry_off(sys_, staged_bin, paths, log)
        if paths.app.exists():
            run_cua(sys_, staged_bin, "stop", timeout=30)  # best effort; the daemon may not run

        backup = paths.app.with_name(f"{paths.app.name}.install-backup.{os.getpid()}")
        if paths.app.exists():
            os.rename(paths.app, backup)
        try:
            rc, out = sys_.run(["/usr/bin/ditto", str(staged), str(paths.app)])
            if rc != 0:
                raise InstallError(f"ditto failed rc={rc}: {out.strip()[:300]}")
            ok, inst_notes = check_app(sys_, paths.app)
            for n in inst_notes:
                log(f"installed: {n}")
            if not ok:
                raise InstallError(f"{paths.app} failed verification after ditto")
            rc, out = sys_.run([LSREGISTER, "-f", str(paths.app)])
            if rc != 0:
                raise InstallError(f"lsregister -f failed rc={rc}: {out.strip()[:300]}")
        except BaseException:
            shutil.rmtree(paths.app, ignore_errors=True)
            if backup.exists():
                os.rename(backup, paths.app)
                sys_.run([LSREGISTER, "-f", str(paths.app)])
                log(f"rolled back to the previous {paths.app}")
            raise
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        log(f"installed {paths.app} (ditto, lsregister -f)")

    ensure_link(paths, log)
    rc, out = run_cua(sys_, paths.app_binary, "--version", timeout=30)
    if rc != 0 or out.split()[-1:] != [VERSION]:
        raise InstallError(f"cua-driver --version after install: rc={rc} {out.strip()[:200]}")
    log(f"{out.strip()}; next (once, by a person): cua-driver permissions grant")
    return True


def main(argv: list[str] | None = None, sys_: System | None = None, paths: Paths | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="read-only report; exit 0 when all is well")
    ap.add_argument("--json", action="store_true", help="with --check: print the report as JSON")
    a = ap.parse_args(argv)
    sys_ = sys_ or System()
    paths = paths or Paths()
    if a.check:
        r = check(sys_, paths)
        print(json.dumps(r, indent=2) if a.json else format_report(r))
        return 0 if r["ok"] else 1
    try:
        install(sys_, paths)
    except InstallError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
