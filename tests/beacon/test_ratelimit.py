"""A usage limit is a wait until its reset, never a crash (W21, measured 2026-11-16).

The fixture is a real session `beacon watch` started while the 5-hour limit was exhausted (home directory
renamed): Claude Code answered at once with "You've hit your 5-hour limit · resets 12pm (Asia/Saigon)",
subtype success, is_error true, cost 0. The watch classified it "crash" and backed off 15 s .. 600 s.
"""
import datetime
import json
import os
import signal
import stat
import sys

import pytest

from beacon import watchdog
from beacon.watchdog import (FRESH_PROMPT, RESUME_PROMPT, CliRunner, SessionResult, classify_end,
                             read_session_log, reset_info, watch)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "usage_limit_session.jsonl")
MESSAGE = "You've hit your 5-hour limit · resets 12pm (Asia/Saigon)"
SESSION_TS = 1794800791.0                  # 2026-11-16 03:46:31 UTC = 10:46:31 in Asia/Saigon
RESETS_AT = 1794805200                     # the stream's resetsAt = 2026-11-16 12:00 +07:00


def utc(*a):
    return datetime.datetime(*a, tzinfo=datetime.timezone.utc).timestamp()


# ------------------------------------------------------------------ classification
def test_beacon_ratelimit_classify_real_session_file():
    res = read_session_log(FIXTURE, now=SESSION_TS)
    assert res.error_kind == "rate_limit" and res.is_error
    assert res.reset_at == RESETS_AT
    assert res.reset_text == "resets 12pm (Asia/Saigon)"
    assert res.session_id == "9b88cbe8-bd22-4873-8404-41baa2ca01fe" and res.cost_usd == 0.0
    assert res.text == MESSAGE


def _fixture_lines():
    with open(FIXTURE, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def _write(tmp_path, events, name="s.jsonl"):
    p = tmp_path / name
    p.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return str(p)


def test_beacon_ratelimit_classify_each_signal_alone(tmp_path):
    """The stream's resetsAt, the result's api_error fields, the assistant's error field and the wording each
    classify the session on their own."""
    evs = _fixture_lines()
    no_event = [e for e in evs if e.get("type") != "rate_limit_event"]
    res = read_session_log(_write(tmp_path, no_event), now=SESSION_TS)
    # resetsAt still comes from the assistant message's api_error_params
    assert res.error_kind == "rate_limit" and res.reset_at == RESETS_AT

    def bare_result(text):
        return {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "session_id": "x",
                "total_cost_usd": 0, "result": text}

    # only the wording: the reset comes from the text, in its time zone
    res = read_session_log(_write(tmp_path, [bare_result(MESSAGE)]), now=SESSION_TS)
    assert res.error_kind == "rate_limit" and res.reset_at == RESETS_AT
    # only the result's own fields
    r = dict(bare_result("Something went wrong"), api_error_status=429, api_error_type="rate_limit_error")
    assert read_session_log(_write(tmp_path, [r]), now=SESSION_TS).error_kind == "rate_limit"
    # only the assistant's error field
    msg = {"type": "assistant", "error": "rate_limit", "is_api_error_message": True, "message": {"content": []}}
    res = read_session_log(_write(tmp_path, [msg, bare_result("Something went wrong")]), now=SESSION_TS)
    assert res.error_kind == "rate_limit" and res.reset_at == 0.0


def test_beacon_ratelimit_classify_wording_and_existing_patterns():
    def kind(text, code=0):
        return classify_end({"is_error": True, "subtype": "success", "result": text}, [], code)
    assert kind(MESSAGE) == "rate_limit"
    assert kind("You've hit your weekly limit · resets Nov 18, 9am (Europe/Paris)") == "rate_limit"
    assert kind("Claude usage limit reached. Your limit will reset at 3am", 1) == "rate_limit"
    assert kind("API Error: 429 rate_limit_error", 1) == "rate_limit"
    assert kind("Rate limit exceeded", 1) == "rate_limit"
    # the others are unchanged
    assert kind("Invalid API key · Please run /login", 1) == "auth"
    assert kind("Credit balance is too low", 1) == "billing"
    assert kind("Repeated 529 Overloaded errors", 1) == "overloaded"
    assert classify_end({"is_error": False, "result": MESSAGE}, [], 0) == ""     # a clean end stays clean
    assert classify_end({"is_error": True, "result": "x", "api_error_status": 529}, [], 1) == "overloaded"
    assert classify_end({"is_error": True, "result": "x", "api_error_status": 401}, [], 1) == "auth"
    assert classify_end({"is_error": True, "result": "x"}, [], 1, stream_errors=["rate_limit"]) == "rate_limit"


def test_beacon_ratelimit_classify_reset_in_its_time_zone():
    now = SESSION_TS
    assert reset_info(MESSAGE, now) == (utc(2026, 11, 16, 5, 0), "resets 12pm (Asia/Saigon)")
    # after noon in Saigon the next 12pm is tomorrow, wherever this Mac is
    assert reset_info(MESSAGE, utc(2026, 11, 16, 6, 0))[0] == utc(2026, 11, 17, 5, 0)
    assert reset_info("resets 3:30am (Europe/Paris)", now) == (utc(2026, 11, 17, 2, 30),
                                                               "resets 3:30am (Europe/Paris)")
    assert reset_info("resets Nov 18, 9am (Europe/Paris)", now) == (utc(2026, 11, 18, 8, 0),
                                                                    "resets Nov 18, 9am (Europe/Paris)")
    assert reset_info("resets Nov 18 at 9:15pm (America/New_York)", now)[0] == utc(2026, 11, 19, 2, 15)
    assert reset_info("Your limit will reset at 15:30 (UTC)", now)[0] == utc(2026, 11, 16, 15, 30)
    # a date that has passed this year is next year's (Dec 31 -> Jan 2)
    assert reset_info("resets Jan 2, 9am (UTC)", utc(2026, 12, 31, 20, 0))[0] == utc(2027, 1, 2, 9, 0)
    assert reset_info("resets Feb 30, 9am (UTC)", now) == (0.0, "")
    # the forms the old code handled still parse
    assert reset_info("resets in 2h 15m", now) == (now + 2 * 3600 + 15 * 60, "resets in 2h 15m")
    assert reset_info("no reset information here", now) == (0.0, "")
    assert reset_info("resets 25:00", now) == (0.0, "")
    assert reset_info("resets 13pm (UTC)", now) == (0.0, "")


def test_beacon_ratelimit_classify_reset_dst_and_skew():
    # Europe/Paris springs forward on 2027-03-28 (02:00 -> 03:00): 9am that day is 07:00 UTC, the day before 08:00
    assert reset_info("resets 9am (Europe/Paris)", utc(2027, 3, 27, 12, 0))[0] == utc(2027, 3, 28, 7, 0)
    assert reset_info("resets 9am (Europe/Paris)", utc(2027, 3, 27, 6, 0))[0] == utc(2027, 3, 27, 8, 0)
    # and falls back on 2026-10-25: 9am is 08:00 UTC again
    assert reset_info("resets 9am (Europe/Paris)", utc(2026, 10, 24, 12, 0))[0] == utc(2026, 10, 25, 8, 0)
    # 2:30am does not exist in Paris on 2027-03-28; it still resolves to a time near the gap, not an error
    t = reset_info("resets 2:30am (Europe/Paris)", utc(2027, 3, 27, 12, 0))[0]
    assert abs(t - utc(2027, 3, 28, 1, 0)) <= 3600
    # clock skew: three minutes after noon Saigon, "resets 12pm" is noon that just passed, not tomorrow
    assert reset_info(MESSAGE, utc(2026, 11, 16, 5, 3))[0] == utc(2026, 11, 16, 5, 0)
    # an unknown zone name falls back to this Mac's zone (and still parses)
    assert reset_info("resets 3am (Mars/Olympus)", SESSION_TS)[0] > SESSION_TS


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_beacon_ratelimit_classify_through_the_runner(make_run, tmp_path):
    """The real runner, fed the real session's stream by a stand-in CLI (never a real claude)."""
    L = make_run()
    script = tmp_path / "replay.py"
    script.write_text("import sys\nsys.stdout.write(open(sys.argv[1], encoding='utf-8').read())\n")
    wrapper = tmp_path / "claude"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} {FIXTURE}\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    res = CliRunner(cwd=str(tmp_path), run=L.dir, claude=str(wrapper)).run(FRESH_PROMPT)
    assert res.error_kind == "rate_limit" and res.reset_at == RESETS_AT
    assert res.reset_text == "resets 12pm (Asia/Saigon)"


# ------------------------------------------------------------------ waiting
class FakeRunner:
    def __init__(self, L, steps):
        self.L, self.steps, self.calls, self.stopped = L, list(steps), [], False

    def run(self, prompt, resume=None):
        self.calls.append((prompt, resume))
        return self.steps.pop(0)(self.L)

    def stop(self):
        self.stopped = True


class Time:
    """An injected clock that sleeping advances (jump: extra seconds per sleep, e.g. a Mac asleep)."""

    def __init__(self, t, jump=0.0):
        self.t, self.jump, self.sleeps = t, jump, []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s + self.jump


def limited(reset_at=0.0, reset_text="", sid="s1"):
    def step(L):
        return SessionResult(session_id=sid, is_error=True, error_kind="rate_limit", reset_at=reset_at,
                             reset_text=reset_text, text="You've hit your 5-hour limit")
    return step


def failing(kind, sid="s1"):
    return lambda L: SessionResult(session_id=sid, is_error=True, error_kind=kind)


def allow(why):
    def step(L):
        L.save_gate(L.gate_state(), {"action": "allow", "why": why})
        return SessionResult(session_id="s9")
    return step


def go(L, steps, t, **kw):
    r = FakeRunner(L, steps)
    out = watch(L.dir, r, clock=t.clock, sleep=t.sleep, log=lambda *_: None, **kw)
    return out, r


def events(L, name="watch.jsonl"):
    with open(os.path.join(L.dir, name), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def waits(L):
    return [e for e in events(L) if e["event"] == "wait"]


def test_beacon_ratelimit_wait_until_the_reset_and_log_it(make_run):
    L = make_run()
    t = Time(1_000_000.0)
    out, r = go(L, [limited(1_000_000.0 + 1800, "resets 12pm (Asia/Saigon)"), allow("awaiting_human")], t)
    assert out["why"] == "gate_awaiting_human"
    assert sum(t.sleeps) == 1860.0 and max(t.sleeps) <= watchdog.WAIT_CHUNK_S
    assert r.calls == [(FRESH_PROMPT, None), (RESUME_PROMPT, "s1")]
    (w,) = waits(L)
    assert w["kind"] == "rate_limit" and w["until"] == 1_001_860 and w["reset"] == "resets 12pm (Asia/Saigon)"
    assert isinstance(w["until"], int) and w["seconds"] == 1860.0


def test_beacon_ratelimit_wait_the_real_session(make_run):
    L = make_run()
    t = Time(SESSION_TS)
    out, _ = go(L, [lambda L: read_session_log(FIXTURE, now=SESSION_TS), allow("awaiting_human")], t)
    assert out["why"] == "gate_awaiting_human"
    (w,) = waits(L)
    assert (w["kind"], w["until"], w["reset"]) == ("rate_limit", RESETS_AT + 60, "resets 12pm (Asia/Saigon)")
    assert t.t >= RESETS_AT + 60


def test_beacon_ratelimit_wait_is_not_a_failure(make_run):
    L = make_run()
    t = Time(1_000_000.0)
    # 13 limits in a row (the run that died with too_many_failures had 13): max_transient=2, max_sessions=3
    steps = [(lambda k: (lambda L: limited(t.t + 600, "resets soon")(L)))(k) for k in range(13)]
    out, r = go(L, steps + [allow("awaiting_human")], t, max_transient=2, max_sessions=3)
    assert out["why"] == "gate_awaiting_human" and len(r.calls) == 14
    assert not any(e.get("why") == "too_many_failures" for e in events(L))


def test_beacon_ratelimit_wait_does_not_touch_the_crash_backoff(make_run):
    L = make_run()
    t = Time(1_000_000.0)
    out, _ = go(L, [failing("crash"), limited(1_000_000.0 + 15 + 300), failing("crash"), allow("awaiting_human")], t)
    kinds = [(w["kind"], w["seconds"]) for w in waits(L)]
    assert kinds[0] == ("crash", 15.0) and kinds[1][0] == "rate_limit" and kinds[2] == ("crash", 30.0)
    assert "until" not in waits(L)[0]                    # the other waits keep their shape


def test_beacon_ratelimit_wait_over_6h_ends_the_run(make_run):
    L = make_run()
    now = 1_000_000.0
    t = Time(now)
    out, r = go(L, [limited(now + 7 * 3600, "resets Nov 18, 9am (Europe/Paris)")], t)
    assert out["why"] == "usage_limit" and not t.sleeps and len(r.calls) == 1
    assert "resets Nov 18, 9am (Europe/Paris)" in out["detail"] and "7.0 h" in out["detail"]
    report = open(os.path.join(L.dir, "REPORT.md"), encoding="utf-8").read()
    assert "watchdog:usage_limit" in report and "resets Nov 18, 9am (Europe/Paris)" in report
    ends = [json.loads(e["payload"]) for e in L.__class__.open(L.dir).events(etype="watch_end")]
    assert ends and ends[-1]["why"] == "usage_limit" and "Europe/Paris" in ends[-1]["detail"]
    # exactly 6 h is still waited for
    L2 = make_run()
    t2 = Time(now)
    out, _ = go(L2, [limited(now + 6 * 3600), allow("awaiting_human")], t2)
    assert out["why"] == "gate_awaiting_human" and sum(t2.sleeps) == 6 * 3600 + 60


def test_beacon_ratelimit_wait_without_a_reset_backs_off_capped(make_run):
    L = make_run()
    t = Time(1_000_000.0)
    out, r = go(L, [limited() for _ in range(20)], t)
    secs = [w["seconds"] for w in waits(L)]
    assert secs[:5] == [300.0, 600.0, 1200.0, 2400.0, 3600.0] and max(secs) == 3600.0
    assert all(w["kind"] == "rate_limit" and w["reset"] == "" and w["until"] >= w["ts"] for w in waits(L))
    # a limit with no announced reset that lasts past 6 h ends the run too
    assert out["why"] == "usage_limit" and "no reset time" in out["detail"]
    assert t.t - 1_000_000.0 <= 6 * 3600


def test_beacon_ratelimit_wait_a_reset_in_the_past(make_run):
    """Clock skew: the announced reset has already passed here. A short backoff, never a negative sleep."""
    L = make_run()
    t = Time(1_000_000.0)
    out, _ = go(L, [limited(1_000_000.0 - 30, "resets 12pm"), allow("awaiting_human")], t)
    assert out["why"] == "gate_awaiting_human" and sum(t.sleeps) == 300.0 and min(t.sleeps) > 0


def test_beacon_ratelimit_wait_follows_the_clock_not_the_sleeps(make_run):
    """A Mac asleep during the wait: once the clock passes `until` the watch goes on at once."""
    L = make_run()
    t = Time(1_000_000.0, jump=2 * 3600)
    out, _ = go(L, [limited(1_000_000.0 + 3 * 3600), allow("awaiting_human")], t)
    assert out["why"] == "gate_awaiting_human" and len(t.sleeps) == 2


def test_beacon_ratelimit_wait_stopped_while_waiting(make_run):
    L = make_run()
    t = Time(1_000_000.0)

    def interrupted(s):
        raise KeyboardInterrupt

    r = FakeRunner(L, [limited(1_000_000.0 + 3600)])
    out = watch(L.dir, r, clock=t.clock, sleep=interrupted, log=lambda *_: None)
    assert out["why"] == "interrupted" and r.stopped
    assert os.path.exists(os.path.join(L.dir, "REPORT.md"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_beacon_ratelimit_wait_sigterm_while_waiting_ends_cleanly(make_run):
    L = make_run()
    t = Time(1_000_000.0)
    before = signal.getsignal(signal.SIGTERM)

    def killed(s):
        os.kill(os.getpid(), signal.SIGTERM)

    r = FakeRunner(L, [limited(1_000_000.0 + 3600)])
    out = watch(L.dir, r, clock=t.clock, sleep=killed, log=lambda *_: None)
    assert out["why"] == "interrupted" and r.stopped
    assert signal.getsignal(signal.SIGTERM) is before


def test_beacon_ratelimit_wait_wakes_when_the_run_is_done(make_run):
    """Problems closed by the orchestrator during a long wait: the watch ends instead of sleeping on."""
    L = make_run([{"title": "Only problem"}])
    t = Time(1_000_000.0)

    def close_all(s):
        t.sleep(s)
        if len(t.sleeps) == 2:
            pid = L.problems()[0]["id"]
            L.set_status(pid, "blocked", reason="the worker hit the usage limit")
            L.force_verify(pid, "closed while the watch waited", actor="orchestrator")

    r = FakeRunner(L, [limited(1_000_000.0 + 3 * 3600)])
    out = watch(L.dir, r, clock=t.clock, sleep=close_all, log=lambda *_: None)
    assert out["why"] == "no_open_problems" and len(t.sleeps) == 2
