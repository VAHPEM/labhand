# -*- coding: utf-8 -*-
"""The linux wrapper's samples/ change report keeps at most 15 new files. labhand's own per-call
scratch files (`_lh_<stem>_<time_ns>_<pid>_<rand8>[.ext]`, other concurrent calls' scripts and
launchers) must be dropped BEFORE that cut, or they take the 15 slots and hide a real new file of
this command (review R2-C2, 2026-11-21). VM-free: the wrapper's own `comm | ... | sed` segment
runs in a local bash over fake before/after snapshots (the full wrapper needs GNU find)."""
import re
import shlex
import shutil
import subprocess

import pytest

from labhand import shell
from labhand.output import SCRATCH_RE, scratch_name


def _report_segment(cmd):
    """The lines of the wrapper's inner bash script from `comm -13` through the sed."""
    inner = shlex.split(cmd.split("; echo __RC=$?")[0])[-1]
    lines = inner.splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.startswith("comm -13"))
    j = next(n for n in range(i, len(lines)) if "__NEWFILE__" in lines[n])
    return "\n".join(lines[i:j + 1])


@pytest.mark.skipif(not (shutil.which("bash") and shutil.which("comm")), reason="needs bash + comm")
def test_newfiles_filter_scratch_names_do_not_hide_a_real_file(tmp_path):
    seg = _report_segment(shell._wrap_cmd("true", "kali", 20))
    pre = tmp_path / "pre"
    post = tmp_path / "post"
    pre.write_text("old.bin\t10\n")
    scratch = [scratch_name(s, e) for s in ("script", "gdb_run", "win_cmd") for e in (".sh", ".py", "")
               for _ in range(3)]
    assert len(scratch) > 15 and all(SCRATCH_RE.match(n) for n in scratch)
    rows = sorted([f"{n}\t5" for n in scratch] + ["old.bin\t10", "zz_real_output.txt\t42",
                                                   "_lh_not_scratch.txt\t7"])
    post.write_text("\n".join(rows) + "\n")
    seg = seg.replace("/tmp/_pre.$$", shlex.quote(str(pre))).replace("/tmp/_post.$$", shlex.quote(str(post)))
    p = subprocess.run(["bash", "-c", "set -o pipefail\n" + seg], capture_output=True, text=True, timeout=30)
    _, found = shell._peel_newfiles(p.stdout)
    names = [n for n, _ in found]
    assert "zz_real_output.txt" in names, p.stdout
    assert "_lh_not_scratch.txt" in names               # only real scratch names are dropped
    assert not any(SCRATCH_RE.match(n) for n in names)


def test_newfiles_filter_ere_matches_scratch_re():
    ere = re.compile(shell._SCRATCH_LINE_ERE.replace("[[:space:]]", r"\s"))
    for stem, ext in (("script", ".sh"), ("win_cmd", ".txt"), ("gdb_user", ".py"), ("gui_task", "")):
        n = scratch_name(stem, ext)
        assert ere.match(f"{n}\t12") and SCRATCH_RE.match(n)
    for n in ("_lh_x.txt", "real.txt", "_lh_script_123_4_deadbeef.sh"):
        assert not ere.match(f"{n}\t12") and not SCRATCH_RE.match(n)
