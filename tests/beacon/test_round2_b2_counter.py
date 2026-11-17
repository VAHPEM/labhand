"""Review P5 (round 2 of B2): when the lighthouse prompt could not be built, the human's request was kept (right)
but every such stop still did lighthouse_calls += 1. A persistent failure pushed the counter past
max_lighthouse_calls and so disabled the automatic stall/idle lighthouse, with zero model calls made."""
import functools
import json

from beacon import logview
from beacon.gate import on_stop
from beacon.lighthouse import run_lighthouse


def _transport(calls):
    def tr(body, key, timeout):
        calls.append(body)
        return {"stop_reason": "end_turn", "usage": {},
                "content": [{"type": "text", "text": json.dumps({"suggestions": [], "review": ""})}]}
    return tr


def _broken(run_dir):
    raise OSError("transcript unreadable")


def _stop(L, lh):
    return on_stop(L, {"session_id": "s", "last_assistant_message": "working"}, lighthouse=lh)


def test_round2_b2_counter_failed_human_review_spends_no_budget(make_run, monkeypatch):
    L = make_run(k_stall=99, k_idle=99, max_lighthouse_calls=3)
    monkeypatch.setattr(logview, "sessions", _broken)
    calls = []
    lh = functools.partial(run_lighthouse, transport=_transport(calls), key="k")
    L.set_human_flag("lighthouse_request", {"note": ""})
    for _ in range(5):
        d = _stop(L, lh)
        assert d.lighthouse["status"] == "error" and d.lighthouse.get("prompt_failed")
    assert calls == []
    assert L.gate_state()["lighthouse_calls"] == 0
    assert "lighthouse_request" in L.human_flags()                  # round-1 behaviour kept: the request waits

    # once the log reads again the review runs and counts exactly once
    monkeypatch.undo()
    d = _stop(L, lh)
    assert len(calls) == 1 and d.lighthouse["requested_by"] == "human"
    assert L.gate_state()["lighthouse_calls"] == 1
    assert "lighthouse_request" not in L.human_flags()


def test_round2_b2_counter_automatic_lighthouse_still_runs_after_failures(make_run, monkeypatch):
    """The review's demo shape (max 3, five failing stops), then the run goes idle: the automatic lighthouse
    must still be available (not `lighthouse_exhausted`) and make its call."""
    L = make_run(k_stall=99, k_idle=2, max_lighthouse_calls=3, lighthouse_dry_max=99)
    monkeypatch.setattr(logview, "sessions", _broken)
    calls = []
    lh = functools.partial(run_lighthouse, transport=_transport(calls), key="k")
    L.set_human_flag("lighthouse_request", {"note": ""})
    for _ in range(5):
        _stop(L, lh)
    assert calls == [] and L.gate_state()["lighthouse_calls"] < 3
    monkeypatch.undo()
    L.take_human_flag("lighthouse_request")                          # the human withdrew it; the run idles on
    whys = []
    for _ in range(3):
        d = _stop(L, lh)
        whys.append(d.why)
    assert "lighthouse_exhausted" not in whys
    assert calls, "the automatic lighthouse never ran"
    assert L.gate_state()["lighthouse_calls"] == len(calls)


def test_round2_b2_counter_failed_automatic_prompt_spends_no_budget(make_run, monkeypatch):
    L = make_run(k_stall=99, k_idle=1, max_lighthouse_calls=3, lighthouse_dry_max=99)
    monkeypatch.setattr(logview, "sessions", _broken)
    calls = []
    lh = functools.partial(run_lighthouse, transport=_transport(calls), key="k")
    for _ in range(5):
        d = _stop(L, lh)
        assert d.why != "lighthouse_exhausted"
    assert calls == []
    assert L.gate_state()["lighthouse_calls"] == 0


def test_round2_b2_counter_review_that_went_ahead_still_counts(make_run, monkeypatch):
    """The budget still counts real reviews, including one whose model call failed after the prompt was built."""
    L = make_run(k_stall=99, k_idle=99)

    def failing(body, key, timeout):
        raise OSError("network down")
    lh = functools.partial(run_lighthouse, transport=failing, key="k")
    L.set_human_flag("lighthouse_request", {"note": ""})
    _stop(L, lh)
    assert L.gate_state()["lighthouse_calls"] == 1
    assert "lighthouse_request" not in L.human_flags()
