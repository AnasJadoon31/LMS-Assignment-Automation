import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lms_worker.kv import FileKV
from lms_worker.lms.bahria_pages import LMS_TZ
from lms_worker.solver.claude import StreamReader
from lms_worker.solver.usage import UsageGuard

FIXTURE = Path(__file__).parent / "fixtures" / "claude_stream_ok.jsonl"
NOW = datetime(2026, 10, 6, 10, 0, tzinfo=LMS_TZ)


def limit_line(status="allowed", five=0.1, week=0.33, overage=False, kind="five_hour"):
    reset5, reset7 = NOW + timedelta(hours=2), NOW + timedelta(days=3)
    return json.dumps({"type": "rate_limit_event", "session_id": "s1", "rate_limit_info": {
        "status": status, "resetsAt": int(reset5.timestamp()), "rateLimitType": kind, "isUsingOverage": overage,
        "unifiedWindows": {"five_hour": {"utilization": five, "resetsAt": int(reset5.timestamp())},
                           "seven_day": {"utilization": week, "resetsAt": int(reset7.timestamp())}}}})


def test_a_real_stream_gives_the_answer_and_the_usage():
    r = StreamReader()
    seen = [x for x in (r.feed(line) for line in FIXTURE.read_text().splitlines()) if x]
    o = r.outcome
    assert o.finished and not o.is_error and not o.usage_limited
    assert o.structured == {"answer": True, "reason": "2 + 2 equals 4 by basic arithmetic."}
    assert o.session_id and o.turns == 2 and o.cost_usd > 0 and o.tokens > 0
    assert len(seen) == 1 and seen[0].status == "allowed" and seen[0].windows["seven_day"][0] == 0.33
    assert seen[0].resets_at == datetime.fromtimestamp(1791272400, tz=timezone.utc)


def test_a_rejected_request_or_limit_text_means_the_plan_limit():
    r = StreamReader()
    r.feed(limit_line(status="rejected", five=1.0))
    r.feed(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "You've hit your limit · resets 3pm"}))
    assert r.outcome.usage_limited
    text_only = StreamReader()
    text_only.feed(json.dumps({"type": "result", "is_error": True, "result": "Claude AI usage limit reached|1791272400"}))
    assert text_only.outcome.usage_limited
    capped = StreamReader()
    capped.feed(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 200}))
    assert capped.outcome.turn_capped and not capped.outcome.usage_limited
    assert StreamReader().feed("not json") is None


def test_pauses_for_the_limit_extra_usage_and_headroom(tmp_path):
    guard = UsageGuard(FileKV(tmp_path), pause_at=0.8, daily_cap=6)
    r = StreamReader()
    assert guard.record(r.feed(limit_line(five=0.4)), NOW) is None and guard.paused(NOW) is None
    pause = guard.record(StreamReader().feed(limit_line(five=0.85)), NOW)
    assert pause.reason == "headroom" and pause.until > NOW + timedelta(hours=2)
    assert "resumes" in pause.describe()
    assert guard.paused(NOW + timedelta(hours=3)) is None  # the window has reset
    pause = guard.record(StreamReader().feed(limit_line(status="rejected", five=1.0)), NOW)
    assert pause.reason == "limit"
    pause = guard.record(StreamReader().feed(limit_line(overage=True)), NOW + timedelta(hours=3))
    assert pause.reason == "extra usage"


def test_the_weekly_window_can_pause_for_days(tmp_path):
    guard = UsageGuard(FileKV(tmp_path))
    pause = guard.record(StreamReader().feed(limit_line(five=0.2, week=0.92)), NOW)
    assert pause.reason == "headroom" and pause.until - NOW > timedelta(days=2)


def test_daily_cap_counts_runs_per_pakistan_day(tmp_path):
    guard = UsageGuard(FileKV(tmp_path), daily_cap=2)
    guard.count_run(NOW)
    guard.count_run(NOW)
    pause = guard.paused(NOW)
    assert pause.reason == "daily cap" and pause.until.astimezone(LMS_TZ).hour == 0
    assert guard.paused(NOW + timedelta(days=1)) is None


def test_status_line(tmp_path):
    guard = UsageGuard(FileKV(tmp_path))
    assert guard.status_line(NOW).startswith("Claude usage: not reported yet")
    guard.record(StreamReader().feed(limit_line(five=0.62, week=0.33)), NOW)
    guard.count_run(NOW)
    line = guard.status_line(NOW)
    assert "5-hour 62%" in line and "weekly 33%" in line and "Solver runs today: 1 of 6" in line


def test_lifting_the_limits_for_a_day_takes_away_the_cap_and_the_early_stop_only(tmp_path):
    kv = FileKV(tmp_path)
    guard = UsageGuard(kv, pause_at=0.8, daily_cap=1)
    assert guard.record(StreamReader().feed(limit_line(five=0.85)), NOW).reason == "headroom"
    guard.count_run(NOW)
    assert guard.paused(NOW).reason == "headroom" and guard.paused(NOW + timedelta(hours=3)).reason == "daily cap"
    kv.set("solver_cap_lifted", NOW.astimezone(LMS_TZ).date().isoformat())
    assert guard.paused(NOW) is None  # neither the cap nor the pause already recorded holds today
    assert guard.record(StreamReader().feed(limit_line(five=0.95)), NOW) is None and guard.paused(NOW) is None
    assert "limits lifted for today" in guard.status_line(NOW)
    assert guard.record(StreamReader().feed(limit_line(status="rejected", five=1.0)), NOW).reason == "limit"  # the plan's own limit does
    assert guard.paused(NOW).reason == "limit"
    assert not guard.lifted(NOW + timedelta(days=1))  # back by itself tomorrow
