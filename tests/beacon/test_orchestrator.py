"""The orchestrator actor: a delegate of the human (ccbridge, driving runs from a Claude app session).
It may do what the human does in the ledger, and the hash chain records it under its own name."""
import pytest

from beacon.ledger import HUMAN_ACTORS, LedgerError


def test_orchestrator_answers_verifies_reopens_picks(make_run):
    L = make_run()
    a = L.add_node("P1", "read the parser", kind="approach")
    qid = L.ask("need_info", "Which build of the sample should I use?", node_id=a)["qid"]
    res = L.answer(qid, "The arm64 build in samples/.", actor="orchestrator")
    assert res["reopened"] == [a] and L.node(a)["status"] == "todo"
    e, _ = L.attach_evidence(a, "an output", content="out-1")
    assert L.propose_done("P1", "flag{x}", [e])["status"] == "pending_verification"
    L.verify("P1", False, "wrong format", actor="orchestrator")
    assert L.node("P1")["status"] == "in_progress"
    b = L.add_node("P1", "another approach", kind="approach")
    L.pick(b, actor="orchestrator")
    L.set_status(b, "abandoned", reason="superseded by the plan", actor="orchestrator")
    L.reopen(b, "worth a second look", actor="orchestrator")
    assert L.node(b)["status"] == "todo"
    actors = {ev["actor"] for ev in L.events()}
    assert "orchestrator" in actors and "human" not in actors
    assert L.verify_integrity()[0]


def test_orchestrator_node_source_is_shown(make_run):
    L = make_run()
    n = L.add_node("P1", "lead from the orchestrator", actor="orchestrator")
    assert L.node(n)["source"] == "orchestrator"
    assert "(from orchestrator)" in L.render_tree(evidence=False)


def test_worker_still_cannot_act_as_human(make_run):
    L = make_run()
    a = L.add_node("P1", "a real approach", kind="approach")
    qid = L.ask("need_info", "What is the passphrase?", node_id=a)["qid"]
    for fn in (lambda: L.answer(qid, "guess", actor="worker"),
               lambda: L.pick(a, actor="worker"),
               lambda: L.reopen(a, actor="lighthouse")):
        with pytest.raises(LedgerError):
            fn()
    assert HUMAN_ACTORS == ("human", "orchestrator")
