# -*- coding: utf-8 -*-
"""Spill file names must be unique per call: across processes (each restarts any
in-process state), across threads, and across two calls in the same nanosecond.

Measured 2026-11-20: every fresh Python process reported
`full output -> samples/_ghidra_001.txt`, so two labhand processes (two MCP servers,
two library scripts) wrote the same spill file and one overwrote the other.
VM-free: `_save_to_host` is replaced by a recorder, nothing connects anywhere.
"""
import importlib.util
import os
import re
import subprocess
import sys
import threading
import time

from labhand import output

BIG = "x" * (output.SLICE + 10)          # just over one page: always spills
# LABHAND_WORKDIR's rule (labhand/hosts.py _WD_OK) for one path component
_COMPONENT_OK = re.compile(r"^[A-Za-z0-9._-]+$")


def _fresh_instance(tag):
    """A second, independent copy of labhand/output.py: what a new Python process
    starts with (module state at its import-time values)."""
    spec = importlib.util.spec_from_file_location(f"labhand._spill_proc_{tag}",
                                                  output.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _recording(mod, names, lock=None):
    def save(host, name, data):
        if lock:
            with lock:
                names.append(name)
        else:
            names.append(name)
        return f"samples/{name}"
    mod._save_to_host = save
    return mod


def test_spill_unique_two_real_processes():
    """Two real Python processes, each spilling once, get two different names."""
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from labhand import output; "
            "output._save_to_host = lambda h, n, d: print('NAME=' + n) or 'samples/' + n; "
            "output._spill('kali', 'x' * (output.SLICE + 10), 'cmd_out')")
    root = os.path.dirname(os.path.dirname(os.path.abspath(output.__file__)))
    env = dict(os.environ)
    procs = [subprocess.Popen([sys.executable, "-c", code, root], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(2)]
    names = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        names += re.findall(r"NAME=(\S+)", out)
    assert len(names) == 2 and names[0] != names[1], names


def test_spill_unique_two_simulated_processes_same_instant(monkeypatch):
    """Restarted module state, same pid (pid reuse) and a frozen clock: only the
    random part tells them apart, and it must."""
    monkeypatch.setattr(time, "time_ns", lambda: 1_763_600_000_000_000_000)
    monkeypatch.setattr(os, "getpid", lambda: 4242)
    names = []
    for tag in ("a", "b"):
        _recording(_fresh_instance(tag), names)._spill("kali", BIG, "ghidra")
    assert len(names) == 2 and names[0] != names[1], names


def test_spill_unique_two_simulated_processes_different_pid(monkeypatch):
    monkeypatch.setattr(time, "time_ns", lambda: 1_763_600_000_000_000_000)
    names = []
    for pid in (1001, 1002):
        monkeypatch.setattr(os, "getpid", lambda pid=pid: pid)
        _recording(_fresh_instance(f"p{pid}"), names)._spill("kali", BIG, "ghidra")
    assert names[0] != names[1], names
    assert "1001" in names[0] and "1002" in names[1], names


def test_spill_names_same_process_same_nanosecond(monkeypatch):
    monkeypatch.setattr(time, "time_ns", lambda: 1_763_600_000_000_000_000)
    names = []
    mod = _recording(_fresh_instance("ns"), names)
    for _ in range(50):
        mod._spill("kali", BIG, "out")
    assert len(set(names)) == 50


def test_spill_unique_many_threads_in_two_processes(monkeypatch):
    """Two server processes (independent module state), 16 threads each, all
    spilling at the same moment with a frozen clock."""
    monkeypatch.setattr(time, "time_ns", lambda: 1_763_600_000_000_000_000)
    names, lock = [], threading.Lock()
    mods = [_recording(_fresh_instance(f"t{i}"), names, lock) for i in range(2)]
    start = threading.Barrier(32)

    def worker(mod):
        start.wait()
        mod._spill("kali", BIG, "cmd_out")

    threads = [threading.Thread(target=worker, args=(mods[i % 2],)) for i in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(names) == 32
    assert len(set(names)) == 32, sorted(names)


def test_spill_unique_name_stays_within_workdir_chars():
    """Whatever stem a caller passes, the name is one path component of
    LABHAND_WORKDIR's characters (letters, digits, `._-`), never `..`, bounded."""
    names = []
    mod = _recording(_fresh_instance("chars"), names)
    for stem in ("cmd_out", "job_a.b-c", "gdb x/../y", "ü\\évil;rm -rf", "", "z" * 300):
        mod._spill("kali", BIG, stem)
    assert len(names) == 6
    for n in names:
        assert _COMPONENT_OK.match(n), n
        assert ".." not in n and n.startswith("_") and n.endswith(".txt"), n
        assert len(n) <= 120, (len(n), n)
    assert names[0].startswith("_cmd_out_") and names[1].startswith("_job_a.b-c_")


def test_spill_read_file_hint_names_the_saved_file(monkeypatch):
    """The head+tail marker tells the Brain exactly the file that was written."""
    names = []
    mod = _recording(_fresh_instance("hint"), names)
    text, saved = mod._spill("kali", "y" * 40_000, "cmd_out")
    assert saved == f"samples/{names[0]}"
    assert f'read_file(path="{names[0]}", host="kali")' in text
