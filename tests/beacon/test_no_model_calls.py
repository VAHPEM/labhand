"""beacon's real defaults make no model calls (Ve, 2026-11-15): with a key in the environment and a transport
that would answer, a stall goes to the human (the orchestrator) instead of the lighthouse, a branch keeps the
worker's order, and the budgets are 0."""
import pytest

from beacon import decider
from beacon.ledger import DEFAULT_CONFIG

pytestmark = pytest.mark.real_defaults
BUDGETS = ("max_lighthouse_calls", "max_decider_calls", "max_classifier_calls", "max_followup_calls")


def test_every_model_budget_defaults_to_zero():
    assert {k: DEFAULT_CONFIG[k] for k in BUDGETS} == {k: 0 for k in BUDGETS}


def test_a_new_run_has_no_model_budget(make_run):
    cfg = make_run().config()
    assert all(int(cfg[k]) == 0 for k in BUDGETS)


def test_the_decider_makes_no_call_by_default(make_run):
    called = []

    def transport(*a, **k):
        called.append(1)
        raise AssertionError("a model call was made")
    L = make_run()
    s = L.add_node("P1", "a hypothesis to test")
    r = L.ask("approach_choice", "static or dynamic first?", node_id=s,
              options=["read it statically", "trace it with Frida"])
    res = decider.decide(L, s, "static or dynamic first?", r["created"], transport=transport, key="sk-test-not-used")
    assert not called and res["status"] == "budget"
