# -*- coding: utf-8 -*-
"""What the labhand run (2026-11-15) exposed in beacon, one section per fix."""
import pytest

from beacon import cli, tools
from beacon.ledger import NOTE_MAX, NOTE_SHOW, Ledger, LedgerError


# ---------------------------------------------------------------- 1. notes cut short
def test_a_note_up_to_the_limit_is_shown_whole(make_run):
    note = "step one: do this. " * 60                       # ~1140 chars: the old 600 cut it in half
    L = make_run([{"title": "Drop the shim", "rationale": note.strip()}])
    view = tools.ledger_view(run=L.dir)
    assert note.strip() in view and "(cut" not in view


def test_a_longer_note_says_it_was_cut_and_problem_note_returns_all(make_run):
    note = "line A\n" + "x" * (NOTE_SHOW + 250) + "\nTHE LAST INSTRUCTION"
    L = make_run([{"title": "Drop the shim", "rationale": note}])
    view = tools.ledger_view(run=L.dir)
    flat_len = len(note.replace("\n", " | "))
    assert f"(cut, {flat_len - NOTE_SHOW} more chars: problem_note(\"P1\")" in view
    assert "THE LAST INSTRUCTION" not in view
    full = tools.problem_note("P1", run=L.dir)
    assert full.endswith(note) and "line A\nx" in full           # whole, line breaks kept
    assert tools.problem_note("P1.S9", run=L.dir).startswith("REFUSED:")


def test_note_exactly_at_the_limit_is_not_cut(make_run):
    L = make_run([{"title": "Boundary", "rationale": "y" * NOTE_SHOW}])
    assert "(cut" not in L.render_tree()
    L2 = make_run([{"title": "Boundary", "rationale": "y" * (NOTE_SHOW + 1)}])
    assert "(cut, 1 more chars" in L2.render_tree()


def test_create_refuses_a_note_over_the_storage_limit_instead_of_cutting_it(tmp_path):
    with pytest.raises(LedgerError, match="note is"):
        Ledger.create(str(tmp_path / "r"), "t", [{"title": "Too long", "rationale": "z" * (NOTE_MAX + 1)}],
                      workdir=str(tmp_path))
    L = Ledger.create(str(tmp_path / "r2"), "t", [{"title": "Long ok", "rationale": "z" * NOTE_MAX}],
                      workdir=str(tmp_path))
    assert len(L.node("P1")["rationale"]) == NOTE_MAX               # stored whole (it was clipped at 4000)
    L.close()


def test_init_warns_when_a_note_would_be_cut(tmp_path, capsys):
    ck = tmp_path / "ck.md"
    ck.write_text("# t\n- [ ] short one\n  fine\n- [ ] long one\n  " + "w" * (NOTE_SHOW + 5) + "\n")
    assert cli.main(["init", str(tmp_path / "run"), "--checklist", str(ck)]) == 0
    err = capsys.readouterr().err
    assert "P2's note is" in err and 'problem_note("P2")' in err and "P1" not in err


def test_session_start_says_when_the_tree_was_cut(make_run):
    from beacon.hooks import _tree_for_start
    L = make_run([{"title": f"Problem number {i}", "rationale": "n" * 3000} for i in range(4)])
    out = _tree_for_start(L)
    assert "ledger_view shows the whole ledger" in out


def test_problem_note_is_listed_for_the_worker(make_run):
    assert tools.problem_note in tools.TOOLS


# ---------------------------------------------------------------- 2. a failed verify says why
STEPS = ('echo "step 1 ok"; echo "P1: dualvm import left" >&2; '
         'for i in $(seq 1 60); do echo "noise $i"; done; echo "step 2 failed" >&2; exit 3')


def _rejected(make_run, cmd, **cfg):
    L = make_run([{"title": "Drop the shim", "verify_cmd": cmd}], **cfg)
    tools.add_node("P1", "remove the alias package", run=L.dir)
    tools.attach_evidence("P1.S1", "grep output", content="nothing left", run=L.dir)
    return L, tools.propose_done("P1", "done", [1], run=L.dir)


def test_a_rejection_shows_the_exit_code_and_the_last_40_lines_in_order(make_run):
    L, out = _rejected(make_run, STEPS)
    assert "REJECTED" in out and "exit 3" in out
    assert "noise 60\nstep 2 failed" in out                          # stderr merged in the order written
    assert "noise 22" in out and "noise 21\n" not in out               # exactly the last 40 lines
    assert "earlier line(s) not shown" in out
    assert "seq 1 60" not in out and "The verify command" not in out   # hidden by default
    assert "verify:" not in tools.ledger_view(run=L.dir)


def test_verify_visibility_command_shows_the_command(make_run):
    L, out = _rejected(make_run, STEPS, verify_visibility="command")
    assert "The verify command: " + STEPS in out
    assert "verify: " + STEPS in tools.ledger_view(run=L.dir)


def test_verify_visibility_is_checked(make_run, tmp_path):
    with pytest.raises(LedgerError, match="verify_visibility"):
        make_run(verify_visibility="shown")
    L = make_run()
    with pytest.raises(LedgerError, match="verify_visibility"):
        L.set_config("verify_visibility", "all")
    L.set_config("verify_visibility", "command")
    assert L.verify_visible()


def test_init_set_verify_visibility(tmp_path):
    ck = tmp_path / "ck.md"
    ck.write_text("- [ ] Drop the shim\n  verify: true\n")
    assert cli.main(["init", str(tmp_path / "run"), "--checklist", str(ck), "--set",
                     "verify_visibility=command"]) == 0
    L = Ledger.open(str(tmp_path / "run"))
    assert L.config()["verify_visibility"] == "command"
    L.close()


def test_a_silent_failure_still_gets_the_form_hint(make_run):
    _, out = _rejected(make_run, "exit 1")
    assert "exit 1 (no output)" in out and "re-read the problem's note" in out


def test_output_tail_boundaries():
    from beacon.ledger import output_tail
    assert output_tail("") == "" and output_tail(None) == ""
    assert output_tail("a\nb\n") == "a\nb"
    forty = "\n".join(str(i) for i in range(40))
    assert output_tail(forty) == forty                                 # exactly 40: nothing left out
    assert output_tail(forty + "\n40").startswith("[… 1 earlier line(s) not shown]\n1\n")
    big = output_tail("x" * 10000)
    assert big.startswith("…") and len(big) <= 3501
    assert output_tail(b"bytes \xff ok") == "bytes � ok"
    wide = output_tail("\n".join("y" * 100 for _ in range(40)), chars=1000)
    assert len(wide.split("\n", 1)[1]) <= 1000 and "earlier line(s)" in wide


def test_a_timeout_reports_the_output_so_far(tmp_path):
    from beacon.ledger import run_verify
    ok, detail = run_verify('echo "reached step 2"; sleep 5', "c", str(tmp_path), timeout=1)
    assert not ok and detail.startswith("verify_cmd timed out after 1 s")
