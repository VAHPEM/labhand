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
