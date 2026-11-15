"""What the human / orchestrator decided must reach the worker through ledger_view: a reopen note, a proposal
waiting for approval, a rejected proposal with its reason (measured 2026-11-15: the orchestrator reopened a node
with a note, the view did not show it, and the worker re-proposed the unchanged work)."""
from beacon import tools


def _done_approach(L, parent="P1", title="try it"):
    a = L.add_node(parent, title, kind="approach")
    L.start(a)
    e, _ = L.attach_evidence(a, "it ran", content=f"output of {title}")
    L.set_status(a, "done", evidence_ids=[e])
    return a


def test_reopen_note_is_shown_on_the_reopened_node_until_it_closes_again(make_run):
    L = make_run()
    a = _done_approach(L)
    L.reopen(a, "the test covers only the happy path: add the rejected case", actor="orchestrator")
    view = tools.ledger_view(run=L.dir)
    line = next(ln for ln in view.splitlines() if "REOPENED" in ln)
    assert "the orchestrator" in line and "add the rejected case" in line
    # it sits under the node, right after the node's own line
    lines = view.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.strip().startswith(f"{a} ["))
    assert "REOPENED" in "\n".join(lines[i + 1:i + 3])
    # closed again: the note no longer shows as an open instruction
    L.start(a)
    e, _ = L.attach_evidence(a, "rejected case added", content="2 passed")
    L.set_status(a, "done", evidence_ids=[e])
    assert "REOPENED" not in tools.ledger_view(run=L.dir)


def test_reopen_note_latest_wins_and_names_the_human(make_run):
    L = make_run()
    a = _done_approach(L)
    L.reopen(a, "first note, now stale", actor="orchestrator")
    L.set_status(a, "dead_end", reason="cannot work this way", assumption="the API has no hook")
    L.reopen(a, "second note: use the other API", actor="human")
    view = tools.ledger_view(run=L.dir)
    assert "first note" not in view
    line = next(ln for ln in view.splitlines() if "REOPENED" in ln)
    assert "by the human" in line and "second note: use the other API" in line


def test_reopen_note_on_a_problem_and_a_noteless_reopen_shows_nothing(make_run):
    L = make_run()
    a = _done_approach(L, "P2")
    L.reopen(a, actor="human")                       # no note: nothing to show
    assert "REOPENED" not in tools.ledger_view(run=L.dir)
    L.reopen("P1", "the answer must be the bare value", actor="orchestrator")
    view = tools.ledger_view(run=L.dir, node_id="P1")
    assert "REOPENED by the orchestrator (acting for the human), note: the answer must be the bare value" in view


def test_proposed_problem_view_says_it_waits_for_approval(make_run):
    L = make_run(problem_approval="human")
    out = tools.propose_problem("Second parser crashes too", "found while fuzzing P1", run=L.dir)
    pid = out.split()[1]
    view = tools.ledger_view(run=L.dir)
    lines = view.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith(f"{pid} [proposed]"))
    assert "WAITING FOR APPROVAL" in lines[i + 1] or "WAITING FOR APPROVAL" in lines[i + 2]
    assert "do not work on it" in view
    L.accept_problem(pid, actor="orchestrator")
    assert "WAITING FOR APPROVAL" not in tools.ledger_view(run=L.dir)


def test_proposed_problem_view_shows_a_rejection_and_its_reason(make_run):
    L = make_run(problem_approval="human")
    pid = L.propose_problem("Rewrite the parser", "it is ugly", actor="worker")["id"]
    L.accept_problem(pid, accept=False, note="a sub-step of P1, not a problem of its own", actor="orchestrator")
    view = tools.ledger_view(run=L.dir)
    line = next(ln for ln in view.splitlines() if "REJECTED" in ln)
    assert "by the orchestrator" in line and "a sub-step of P1, not a problem of its own" in line
    assert "propose it again" in line and "WAITING FOR APPROVAL" not in view


def test_worker_md_says_when_to_propose_a_problem_and_that_human_approval_waits():
    import os
    import re
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "beacon", "WORKER.md"), encoding="utf-8") as fh:
        text = re.sub(r"\s+", " ", fh.read())
    for phrase in ("propose_problem(title, rationale, evidence_ids)", "problem_approval=human",
                   "keep working on the other problems", "never a problem", "a bug you found",
                   "requirement the spec implies", "target nobody tests", "do not propose it again under another name",
                   "REOPENED by"):
        assert phrase in text, phrase


def test_reopen_note_is_replaced_by_a_later_verification_rejection(make_run):
    """F1: reopen "note A" -> propose_done -> the orchestrator rejects with "note B" -> only note B shows."""
    L = make_run()
    a = _done_approach(L, "P1", title="first try")
    L.reopen("P1", "note A: the answer must be the bare value", actor="orchestrator")
    e, _ = L.attach_evidence(a, "bare value now", content="value 42")
    assert L.propose_done("P1", "42", [e])["status"] == "pending_verification"
    L.verify("P1", False, "note B: 42 is the decoy, look at the second table", actor="orchestrator")
    view = tools.ledger_view(run=L.dir)
    assert "note A" not in view
    line = next(ln for ln in view.splitlines() if "CANDIDATE REJECTED" in ln)
    assert "by the orchestrator (acting for the human)" in line and "note B: 42 is the decoy" in line
    assert "REOPENED" not in view
    # accepted: the problem is closed and the rejection is gone
    e2, _ = L.attach_evidence(a, "second table", content="value 7")
    L.propose_done("P1", "7", [e2])
    L.verify("P1", True, "right", actor="orchestrator")
    assert "CANDIDATE REJECTED" not in tools.ledger_view(run=L.dir)


def test_reopen_note_survives_a_verify_command_rejection(make_run):
    """The verify command's own verdict is no human decision: the reopen note it answered stays shown."""
    L = make_run()
    a = _done_approach(L, "P1", title="first try")
    L.reopen("P1", "note A: use the second table", actor="orchestrator")
    e, _ = L.attach_evidence(a, "tried", content="value 1")
    L.propose_done("P1", "1", [e])
    L.verify("P1", False, "exit 1: wrong", actor="verifier")
    view = tools.ledger_view(run=L.dir)
    assert "REOPENED by the orchestrator (acting for the human), note: note A: use the second table" in view
    assert "CANDIDATE REJECTED" not in view


def test_reopen_note_is_dropped_by_a_later_noteless_reopen(make_run):
    L = make_run()
    a = _done_approach(L)
    L.reopen(a, "note A: cover the error path", actor="orchestrator")
    L.start(a)
    e, _ = L.attach_evidence(a, "error path covered", content="3 passed")
    L.set_status(a, "done", evidence_ids=[e])
    L.reopen(a, actor="human")
    view = tools.ledger_view(run=L.dir)
    assert "note A" not in view and "REOPENED" not in view


def test_reopen_note_long_and_multiline_is_a_bounded_preview(make_run):
    from beacon.ledger import HUMAN_NOTE_SHOW
    L = make_run()
    a = _done_approach(L)
    note = "first line\n\n    second line\n\n" + "x" * 3000 + " THE-END"
    L.reopen(a, note, actor="orchestrator")
    view = tools.ledger_view(run=L.dir)
    line = next(ln for ln in view.splitlines() if "REOPENED" in ln)
    assert "first line | second line | xxx" in line and "|  |" not in line
    assert "THE-END" not in line and len(line) < HUMAN_NOTE_SHOW + 200
    assert f'problem_note("{a}")' in line and "more chars" in line
    full = tools.problem_note(a, run=L.dir)
    assert "THE-END" in full and "second line" in full and "reopen note by orchestrator" in full


def test_proposed_problem_view_gate_followup_is_not_called_yours(make_run):
    """F2: a follow-up problem the gate made waits for approval too, but the worker never proposed it."""
    L = make_run(problem_approval="human")
    pid = L.followup_problem("P1", wait=True, actor="gate")
    view = tools.ledger_view(run=L.dir)
    lines = view.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith(f"{pid} [proposed]"))
    near = "\n".join(lines[i + 1:i + 4])
    assert "WAITING FOR APPROVAL" in near and "do not work on it" in near
    assert "you proposed" not in view


def test_proposed_problem_view_long_rejection_reason_is_cut_and_readable_in_full(make_run):
    L = make_run(problem_approval="human")
    pid = L.propose_problem("Rewrite the parser", "it is ugly", actor="worker")["id"]
    reason = "not a problem of its own:\n\n" + "y" * 2000 + " END-OF-REASON"
    L.accept_problem(pid, accept=False, note=reason, actor="orchestrator")
    line = next(ln for ln in tools.ledger_view(run=L.dir).splitlines() if "REJECTED" in ln)
    assert "END-OF-REASON" not in line and f'problem_note("{pid}")' in line
    assert "END-OF-REASON" in tools.problem_note(pid, run=L.dir)
