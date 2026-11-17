"""B2: logview.sessions() raised KeyError('paths') when Claude Code's transcript was moved/deleted but a
`beacon watch` stream file had the same session id. The decider and the lighthouse crashed on it, and on the
human-requested review the request flag was consumed before the crash (the review was lost, the Stop hook
failed open)."""
import functools
import json
import os

from beacon import decider, logview
from beacon.gate import on_stop
from beacon.lighthouse import run_lighthouse


def _write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _stream(sid, marker):
    return [{"type": "system", "subtype": "init", "session_id": sid},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": f"ls {marker}"}}]}}]


def _moved_transcript(run_dir, tmp_path, sid="abc123", resumed=False):
    """sessions.jsonl names a transcript that no longer exists; the stream file(s) carry the same id."""
    _write(os.path.join(run_dir, "sessions.jsonl"),
           [{"ts": 5, "session": sid, "transcript_path": str(tmp_path / "moved-away" / "cc.jsonl")}])
    _write(os.path.join(run_dir, "sessions", "20261117-1-0.jsonl"), _stream(sid, "stream-marker-one"))
    if resumed:
        _write(os.path.join(run_dir, "sessions", "20261117-1-1.jsonl"), _stream(sid, "stream-marker-two"))


def _transport(calls, reply=None):
    def tr(body, key, timeout):
        calls.append(body)
        return {"stop_reason": "end_turn", "usage": {},
                "content": [{"type": "text", "text": json.dumps(reply or {"suggestions": [], "review": "fine"})}]}
    return tr


def test_b2_sessions_missing_transcript_no_keyerror(tmp_path):
    d = tmp_path / "run"
    _moved_transcript(str(d), tmp_path)
    got = logview.sessions(str(d))
    assert len(got) == 1
    s = got[0]
    assert s["session"] == "abc123" and s["kind"] == "stream"
    assert s["paths"] == [str(d / "sessions" / "20261117-1-0.jsonl")] and s["path"] == s["paths"][0]
    assert "stream-marker-one" in logview.render_transcript(s["paths"])
    assert logview.pick_session(str(d), "abc")["kind"] == "stream"


def test_b2_sessions_missing_transcript_with_resumed_streams(tmp_path):
    d = tmp_path / "run"
    _moved_transcript(str(d), tmp_path, resumed=True)
    (s,) = logview.sessions(str(d))
    assert len(s["paths"]) == 2
    text = logview.render_transcript(s["paths"])
    assert text.index("stream-marker-one") < text.index("stream-marker-two")


def test_b2_sessions_missing_every_entry_has_paths(tmp_path):
    """A mixed run: a live transcript, a moved one whose session also has a stream file (the KeyError case on
    the old code), and a stream-only session. Every entry must carry `paths`, path == paths[0]."""
    d = tmp_path / "run"
    tp = tmp_path / "cc.jsonl"
    _write(str(tp), [{"type": "user", "message": {"content": "hello"}}])
    _write(str(d / "sessions.jsonl"), [{"ts": 1, "session": "present", "transcript_path": str(tp)},
                                       {"ts": 2, "session": "gone", "transcript_path": str(tmp_path / "nope.jsonl")}])
    _write(str(d / "sessions" / "20261117-1-0.jsonl"), _stream("gone", "gone-marker"))
    _write(str(d / "sessions" / "20261117-2-0.jsonl"), _stream("streamonly", "only-marker"))
    got = logview.sessions(str(d))
    assert {s["session"] for s in got} >= {"present", "gone", "streamonly"}
    for s in got:
        assert s["paths"] and s["paths"][0] == s["path"]


def test_b2_sessions_missing_decider_session_text(make_run, tmp_path):
    L = make_run()
    _moved_transcript(L.dir, tmp_path)
    assert "stream-marker-one" in decider._session_text(L, "observations", 10000)


def test_b2_sessions_missing_human_review_runs_and_takes_the_flag(make_run, tmp_path):
    L = make_run(k_stall=99, k_idle=99)
    _moved_transcript(L.dir, tmp_path)
    calls = []
    lh = functools.partial(run_lighthouse, transport=_transport(calls), key="k")
    L.set_human_flag("lighthouse_request", {"note": "look at P1 again"})
    d = on_stop(L, {"session_id": "abc123", "last_assistant_message": "working"}, lighthouse=lh)
    assert len(calls) == 1 and "stream-marker-one" in json.dumps(calls[0])
    assert d.lighthouse["requested_by"] == "human" and d.lighthouse["status"] in ("ok", "empty")
    assert "lighthouse_request" not in L.human_flags()


def test_b2_sessions_missing_failure_keeps_the_human_flag(make_run, tmp_path, monkeypatch):
    """Any failure while building the review's prompt: no crash of the stop path, no model call, and the human's
    request stays for the next stop (where it runs once the log reads again)."""
    L = make_run(k_stall=99, k_idle=99)
    _moved_transcript(L.dir, tmp_path)

    def broken(run_dir):
        raise KeyError("paths")
    monkeypatch.setattr(logview, "sessions", broken)
    calls = []
    lh = functools.partial(run_lighthouse, transport=_transport(calls), key="k")
    L.set_human_flag("lighthouse_request", {"note": "look at P1 again"})
    d = on_stop(L, {"session_id": "abc123", "last_assistant_message": "working"}, lighthouse=lh)
    assert calls == []
    assert d.lighthouse["status"] == "error" and "could not build the prompt" in d.lighthouse["detail"]
    assert L.human_flags().get("lighthouse_request") == {"note": "look at P1 again"}
    assert d.action == "block"                       # the gate still decided normally (open work)

    monkeypatch.undo()
    d = on_stop(L, {"session_id": "abc123", "last_assistant_message": "working"}, lighthouse=lh)
    assert len(calls) == 1 and d.lighthouse["requested_by"] == "human"
    assert "lighthouse_request" not in L.human_flags()


def test_b2_sessions_missing_lighthouse_that_raises_does_not_crash_the_stop(make_run):
    L = make_run(k_stall=99, k_idle=99)

    def lh(L_, task, focus, cfg, on_prompt=None):
        raise RuntimeError("boom before the prompt was built")
    L.set_human_flag("lighthouse_request", {"note": ""})
    d = on_stop(L, {"session_id": "s", "last_assistant_message": "working"}, lighthouse=lh)
    assert d.lighthouse["status"] == "error" and "boom" in d.lighthouse["detail"]
    assert "lighthouse_request" in L.human_flags()


def test_b2_sessions_missing_decider_degrades(make_run, monkeypatch):
    L = make_run()
    a = L.add_node("P1", "option a", kind="approach")
    b = L.add_node("P1", "option b", kind="approach")

    def broken(run_dir):
        raise KeyError("paths")
    monkeypatch.setattr(logview, "sessions", broken)
    calls = []
    r = decider.decide(L, "P1", "which first?", [a, b], transport=_transport(calls), key="k")
    assert r["status"] == "error" and "could not build the prompt" in r["detail"]
    assert r["order"] == [a, b] and calls == []
