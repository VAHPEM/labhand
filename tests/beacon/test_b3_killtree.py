"""B3: a shellrun timeout (ledger.run_verify, hooks._notify) kills the command's whole process tree. Before, a
bare subprocess.run(timeout=) killed only the shell: a grandchild (measured: `sleep 37.5`) kept running and hung
verify scripts piled up across a run."""
import os
import signal
import subprocess
import time

import pytest

from beacon import ledger, shellrun

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX process-group test (Windows uses taskkill /T)")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")         # a zombie waiting to be reaped has run its course


def _wait_dead(pids, within=5.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        if not any(_alive(p) for p in pids):
            return []
        time.sleep(0.05)
    return [p for p in pids if _alive(p)]


def _read_pids(d, names):
    pids = []
    for n in names:
        with open(os.path.join(d, n), encoding="utf-8") as fh:
            pids.append(int(fh.read().strip()))
    return pids


@pytest.fixture
def reaper():
    """Kill whatever a failing run left behind, so the suite itself never leaks processes."""
    pids = []
    yield pids
    for p in pids:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass


# shell -> background subshell (child) -> sleep (grandchild); the grandchild's output goes elsewhere, so the
# old code returned at the timeout and left it running.
TREE = ("echo $$ > sh.pid; "
        "( sleep 41.5 >/dev/null 2>&1 & echo $! > gc.pid; wait ) >/dev/null 2>&1 & echo $! > child.pid; "
        "while [ ! -s gc.pid ]; do sleep 0.05; done; echo started; wait")


def test_b3_killtree_timeout_leaves_no_descendant(tmp_path, reaper):
    d = str(tmp_path)
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as ei:
        shellrun.run(TREE, cwd=d, timeout=1.5, merge=True)
    assert time.monotonic() - t0 < 10
    pids = _read_pids(d, ("sh.pid", "child.pid", "gc.pid"))
    reaper.extend(pids)
    assert _wait_dead(pids) == [], f"survivors: {pids}"
    out = ei.value.output
    assert isinstance(out, str) and "started" in out      # the output so far, decoded, as before


def test_b3_killtree_grandchild_holding_the_pipe_does_not_hang(tmp_path, reaper):
    """The grandchild keeps stdout open: the old code killed the shell and then waited on the pipe until the
    grandchild ended by itself."""
    d = str(tmp_path)
    cmd = "sleep 8.5 & echo $! > gc.pid; echo $$ > sh.pid; wait"
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        shellrun.run(cmd, cwd=d, timeout=1)
    took = time.monotonic() - t0
    pids = _read_pids(d, ("sh.pid", "gc.pid"))
    reaper.extend(pids)
    assert took < 6, f"took {took:.1f} s"
    assert _wait_dead(pids) == []


def test_b3_killtree_run_verify_timeout(tmp_path, reaper):
    d = str(tmp_path)
    ok, detail = ledger.run_verify(TREE, "cand", d, timeout=1.5)
    assert not ok and "timed out after 1.5 s" in detail and "started" in detail
    pids = _read_pids(d, ("sh.pid", "child.pid", "gc.pid"))
    reaper.extend(pids)
    assert _wait_dead(pids) == []


def test_b3_killtree_contract_unchanged(tmp_path):
    r = shellrun.run('printf out; printf err >&2; exit 3', cwd=str(tmp_path), timeout=30)
    assert isinstance(r, subprocess.CompletedProcess)
    assert (r.returncode, r.stdout, r.stderr) == (3, "out", "err")
    r = shellrun.run('printf a; printf b >&2; printf c', timeout=30, merge=True)
    assert r.returncode == 0 and r.stdout == "abc" and r.stderr is None
    r = shellrun.run("cat", input="piped ▸ in", timeout=30)
    assert r.stdout == "piped ▸ in"
    r = shellrun.run("echo $BEACON_X", env=dict(os.environ, BEACON_X="from env"), timeout=30)
    assert r.stdout.strip() == "from env"
    r = shellrun.run("true")                           # no timeout at all
    assert r.returncode == 0 and r.stdout == ""
