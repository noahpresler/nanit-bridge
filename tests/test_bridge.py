import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bridge  # noqa: E402
import nanit  # noqa: E402

CHILD = "5a4d0b0c-0000-4000-8000-000000000011"


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeHttp:
    """Plays back canned responses and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": json.loads(data) if data else None})
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


def client(http, sleeps=None):
    return bridge.Client("https://example.test/api/v1", "tok", http=http, sleep=(sleeps.append if sleeps is not None else lambda s: None))


T0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def ts(h, m=0):
    """Epoch seconds h hours (and m minutes) before T0."""
    return (T0 - bridge.timedelta(hours=h, minutes=m)).timestamp()


def ev(uid, key, at):
    return {"kind": "event", "uid": uid, "key": key, "time": at}


def baby(feed=(), calendar=(), room=None, uid="b1", at=T0):
    return {"type": "baby", "uid": uid, "feed": list(feed), "calendar": list(calendar),
            "room": room or {"celsius": None, "humidity": None}, "at": bridge.iso(at)}


class MapTests(unittest.TestCase):
    kids = {"b1": CHILD}

    def test_sample_gives_last_night_finished_and_a_sleep_going_on(self):
        n = bridge.SampleNanit(T0)
        items = [baby(n.get("/babies/x/events", {})["events"], n.get("/babies/x/calendar", {})["calendar"])]
        entries, _ = bridge.map(items, {}, self.kids)
        self.assertEqual(sorted(e["kind"] for e in entries), ["diaper", "sleep", "sleep"])
        night, now = sorted((e for e in entries if e["kind"] == "sleep"), key=lambda e: e["started_at"])
        self.assertEqual((night["started_at"], night["ended_at"]), ("2026-11-17T22:00:00Z", "2026-11-18T08:00:00Z"))
        self.assertNotIn("ended_at", now)
        self.assertTrue(all(e["source_event_id"].startswith("nanit:b1:") and e["child_id"] == CHILD for e in entries))

    def test_short_wake_does_not_split_a_sleep(self):
        feed = [ev("a", "FELL_ASLEEP", ts(10)), ev("b", "WOKE_UP", ts(9)), ev("c", "FELL_ASLEEP", ts(8, 57)), ev("d", "WOKE_UP", ts(6))]
        self.assertEqual(len(bridge.sleeps_from_feed(feed)), 1)
        feed[2] = ev("c", "FELL_ASLEEP", ts(8, 30))
        self.assertEqual(len(bridge.sleeps_from_feed(feed)), 2)

    def test_laid_down_asleep_and_lifted_out_asleep(self):
        # Nanit's own order: PUT_TO_SLEEP starts a sleep with no FELL_ASLEEP; VISIT changes nothing.
        feed = [ev("a", "PUT_TO_SLEEP", ts(10)), ev("v", "VISIT", ts(9)), ev("b", "WOKE_UP", ts(8)),
                ev("c", "PUT_IN_BED", ts(5)), ev("d", "FELL_ASLEEP", ts(5)), ev("e", "REMOVED_ASLEEP", ts(3))]
        at = lambda t: datetime.fromtimestamp(t, timezone.utc)  # noqa: E731
        self.assertEqual([(s["start"], s["end"]) for s in bridge.sleeps_from_feed(feed)],
                         [(at(ts(10)), at(ts(8))), (at(ts(5)), at(ts(3)))])

    def test_calendar_pieces_split_by_a_brief_wake_are_one_sleep(self):
        cal = [{"uid": "c1", "type": "auto_sleep", "time": ts(10), "duration": 50 * 60},
               {"uid": "c2", "type": "auto_sleep", "time": ts(9, 8), "duration": 68 * 60},
               {"uid": "c3", "type": "auto_sleep", "time": ts(6), "duration": 30 * 60}]
        sleeps = bridge.sleeps_from_calendar(cal)
        self.assertEqual([(s["key"], round((s["end"] - s["start"]).total_seconds() / 60)) for s in sleeps],
                         [("c1", 120), ("c3", 30)])

    def test_motion_and_deleted_events_are_ignored(self):
        feed = [ev("a", "MOTION", ts(3)), dict(ev("b", "FELL_ASLEEP", ts(2)), deleted_at="2026-11-18T11:00:00Z")]
        self.assertEqual(bridge.sleeps_from_feed(feed), [])

    def test_live_sleep_keeps_its_id_when_nanit_finishes_it(self):
        state = {}
        first, _ = bridge.map([baby([ev("f7", "FELL_ASLEEP", ts(1))])], state, self.kids)
        self.assertEqual(len(first), 1)
        self.assertNotIn("ended_at", first[0])
        self.assertTrue(bridge.is_asleep(state))
        # An hour later the wake has happened and Nanit's calendar has the finished sleep, a minute off.
        later = [baby([ev("f7", "FELL_ASLEEP", ts(1)), ev("f8", "WOKE_UP", ts(0, 10))],
                      [{"uid": "c9", "type": "auto_sleep", "time": ts(1, 1), "duration": 3060, "net_duration": 6120}])]
        second, _ = bridge.map(later, state, self.kids)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["source_event_id"], first[0]["source_event_id"])
        self.assertEqual(second[0]["started_at"], "2026-11-18T10:59:00Z")
        self.assertEqual(second[0]["ended_at"], "2026-11-18T11:50:00Z")  # duration, not the doubled net_duration
        self.assertFalse(bridge.is_asleep(state))

    def test_wake_after_fell_asleep_scrolled_out_of_the_feed(self):
        state = {}
        bridge.map([baby([ev("f1", "FELL_ASLEEP", ts(9))])], state, self.kids)
        noisy = [ev(f"m{i}", "MOTION", ts(0, i)) for i in range(50)] + [ev("w", "WOKE_UP", ts(0, 5))]
        entries, _ = bridge.map([baby(noisy)], state, self.kids)
        self.assertEqual([e.get("ended_at") for e in entries], ["2026-11-18T11:55:00Z"])

    def test_only_connected_babies_and_each_to_its_child(self):
        items = [baby([ev("a", "FELL_ASLEEP", ts(1))], uid="b1"), baby([ev("z", "FELL_ASLEEP", ts(1))], uid="b2")]
        entries, _ = bridge.map(items, {}, {"b2": "child-2"})
        self.assertEqual([(e["child_id"], e["source_event_id"]) for e in entries], [("child-2", "nanit:b2:z")])

    def test_nanit_logged_naps_are_left_out_when_switched_off(self):
        cal = [{"uid": "n1", "type": "sleep", "time": ts(5), "duration": 1800}]
        self.assertEqual(len(bridge.sleeps_from_calendar(cal)), 1)
        with mock.patch.object(bridge, "INCLUDE_NANIT_LOGS", False):
            self.assertEqual(bridge.sleeps_from_calendar(cal), [])
            entries, _ = bridge.map([baby(calendar=[{"uid": "d", "type": "diaper_change", "change_type": "pee", "time": ts(1)}])],
                                    {}, self.kids)
            self.assertEqual(entries, [])

    def test_what_was_logged_in_the_nanit_app(self):
        cal = [
            {"uid": "d1", "type": "diaper_change", "change_type": "dry", "time": ts(9), "user": {"name": "Pat"}, "comment": "x"},
            {"uid": "d2", "type": "diaper_change", "change_type": "pee_and_poo", "time": ts(8)},
            {"uid": "b1", "type": "bottle_feed", "amount": 4, "unit": "oz", "content": "formula", "time": ts(7)},
            {"uid": "n1", "type": "nursing", "left_duration": 600, "right_duration": 300, "time": ts(6)},
            {"uid": "p1", "type": "pumping", "amount": 120, "duration": 900, "time": ts(5)},
            {"uid": "s1", "type": "solids", "time": ts(4)},
            {"uid": "d3", "type": "diaper_change", "change_type": "dry", "time": ts(3), "deleted_at": "2026-11-18T10:00:00Z"},
        ]
        entries, unread = bridge.logs_from_calendar("b1", cal, CHILD)
        self.assertEqual([(e["kind"], e["details"]) for e in entries], [
            ("diaper", {"diaper": "dry"}), ("diaper", {"diaper": "mixed"}),
            ("bottle", {"volume_ml": 118.3, "contents": "formula"}),
            ("nursing", {"left_seconds": 600.0, "right_seconds": 300.0}),
            ("pump", {"total_ml": 120.0, "duration_seconds": 900.0})])
        self.assertEqual(entries[0]["source_event_id"], "nanit:b1:log:d1")
        self.assertNotIn("user", entries[0]["raw"])  # who logged it stays in Nanit
        self.assertNotIn("comment", entries[0]["raw"])
        self.assertEqual(unread, {"solids (Miara can't take these from a bridge yet)": 1})
        _, _, problems = bridge.check(entries, [], set())
        self.assertEqual(problems, [])

    def test_a_log_it_cannot_read_is_reported_not_guessed(self):
        entries, unread = bridge.logs_from_calendar("b1", [
            {"uid": "b2", "type": "bottle_feed", "time": ts(1)},  # no amount
            {"uid": "x", "type": "diaper_change", "change_type": "something_new", "time": ts(1)}], CHILD)
        self.assertEqual((entries, unread), ([], {"bottle_feed": 1, "diaper_change": 1}))

    def test_room_readings_rounded_to_the_minute(self):
        at = T0.replace(second=42)
        _, readings = bridge.map([baby(room={"celsius": 21.437, "humidity": 45.6}, at=at)], {}, self.kids)
        self.assertEqual(readings, [
            {"metric": "room_temp_c", "child_id": CHILD, "started_at": "2026-11-18T12:00:00Z", "value": 21.4},
            {"metric": "humidity", "child_id": CHILD, "started_at": "2026-11-18T12:00:00Z", "value": 46}])

    def test_children_spec(self):
        self.assertEqual(bridge.parse_children("u1=c1, u2=c2"), {"u1": "c1", "u2": "c2"})
        self.assertEqual(bridge.parse_children("c1"), {"*": "c1"})

    def test_event_id_is_valid_for_odd_and_long_ids(self):
        for vid in ["a b/c", "x" * 300, "ünï-1"]:
            self.assertRegex(bridge.event_id(vid), r"^[A-Za-z0-9._:-]{1,128}$")
        self.assertNotEqual(bridge.event_id("x" * 300), bridge.event_id("x" * 301))

    def test_times_are_utc_z_to_the_second(self):
        self.assertEqual(bridge.iso("2026-11-13T09:00:00-05:00"), "2026-11-13T14:00:00Z")


class WatchTests(unittest.TestCase):
    def test_interval_follows_the_baby(self):
        self.assertEqual(bridge.next_interval({"open": {"b1": {}}}, T0), bridge.WATCH_ASLEEP)
        recent = {"sleeps": {"b1": {"s": ["2026-11-18T09:00:00Z", "2026-11-18T11:00:00Z"]}}}
        self.assertEqual(bridge.next_interval(recent, T0), bridge.WATCH_ACTIVE)
        self.assertEqual(bridge.next_interval({}, T0), bridge.WATCH_QUIET)

    def test_wait_returns_early_when_miara_asks(self):
        http = FakeHttp(Resp(200, {"woken": False}), Resp(200, {"woken": True}))
        out = bridge.asyncio.run(bridge.wait_for_next(client(http), 60, {}))
        self.assertEqual(out, "woken")
        self.assertEqual(len(http.calls), 2)
        self.assertIn("/wait?timeout=25", http.calls[0]["url"])

    def test_wait_falls_back_to_sleeping_on_an_older_miara(self):
        http = FakeHttp(Resp(404, {"error": {"code": "not_found", "message": "No such endpoint."}}))
        state = {}
        out = bridge.asyncio.run(bridge.wait_for_next(client(http), 1.5, state))
        self.assertEqual(out, "timeout")
        self.assertTrue(state["wait_unsupported"])


class CheckTests(unittest.TestCase):
    def test_refuses_undeclared_metric_free_text_and_bad_kind(self):
        entries = [{"source_event_id": "a", "kind": "solids", "started_at": "2026-11-13T09:00:00Z"}]
        readings = [
            {"metric": "x.sample.mystery", "started_at": "2026-11-13T09:00:00Z", "value": 1},
            {"metric": "sleep_stage", "started_at": "2026-11-13T09:00:00Z", "value": "She slept badly, poor thing"},
            {"metric": "heart_rate", "started_at": "2026-11-13T09:00:00Z", "value": 120},
        ]
        e, r, bad = bridge.check(entries, readings, {"x.sample.score"})
        self.assertEqual(e, [])
        self.assertEqual([x["metric"] for x in r], ["heart_rate"])
        self.assertEqual(len(bad), 3)


class KeyTests(unittest.TestCase):
    def test_same_batch_same_key_and_key_is_allowed(self):
        body = {"entries": [{"source_event_id": "a"}]}
        k1 = bridge.idempotency_key("/events:batch", body)
        self.assertEqual(k1, bridge.idempotency_key("/events:batch", {"entries": [{"source_event_id": "a"}]}))
        self.assertRegex(k1, r"^[\x21-\x7e]{1,255}$")

    def test_key_ignores_dict_order_but_not_content_or_path(self):
        a = bridge.idempotency_key("/events:batch", {"x": 1, "y": 2})
        self.assertEqual(a, bridge.idempotency_key("/events:batch", {"y": 2, "x": 1}))
        self.assertNotEqual(a, bridge.idempotency_key("/events:batch", {"x": 1, "y": 3}))
        self.assertNotEqual(a, bridge.idempotency_key("/observations:batch", {"x": 1, "y": 2}))


class BatchTests(unittest.TestCase):
    def test_at_most_500_rows(self):
        rows = [{"i": i} for i in range(1201)]
        out = bridge.batches(rows)
        self.assertEqual([len(b) for b in out], [500, 500, 201])
        self.assertEqual([r for b in out for r in b], rows)

    def test_splits_by_size(self):
        rows = [{"raw": "x" * 1000} for _ in range(10)]
        out = bridge.batches(rows, max_bytes=3500)
        self.assertGreater(len(out), 1)
        self.assertTrue(all(len(b) <= 3 for b in out))

    def test_empty(self):
        self.assertEqual(bridge.batches([]), [])

    def test_send_posts_one_request_per_batch_with_child_and_key(self):
        entries = [{"source_event_id": f"e{i}", "kind": "diaper", "started_at": "2026-11-13T09:00:00Z", "details": {"diaper": "wet"}} for i in range(501)]
        results = lambda n: Resp(200, {"results": [{"source_event_id": "x", "status": "saved"}] * n})
        http = FakeHttp(results(500), results(1))
        s = bridge.send(client(http), CHILD, entries, [])
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(len(http.calls[0]["body"]["entries"]), 500)
        self.assertTrue(all(e["child_id"] == CHILD for e in http.calls[0]["body"]["entries"]))
        self.assertNotEqual(http.calls[0]["headers"]["Idempotency-Key"], http.calls[1]["headers"]["Idempotency-Key"])
        self.assertEqual(s["entries"]["saved"], 501)


class RetryTests(unittest.TestCase):
    def test_429_waits_retry_after_and_resends_same_key(self):
        sleeps = []
        http = FakeHttp(Resp(429, {"error": {"code": "rate_limit", "message": "slow"}}, {"Retry-After": "7"}), Resp(200, {"ok": 1}))
        out = client(http, sleeps).request("POST", "/events:batch", {"entries": []})
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(sleeps, [7.0])
        self.assertEqual(http.calls[0]["headers"]["Idempotency-Key"], http.calls[1]["headers"]["Idempotency-Key"])

    def test_5xx_backs_off_then_succeeds(self):
        sleeps = []
        http = FakeHttp(Resp(503), Resp(500), Resp(200, {"ok": 1}))
        client(http, sleeps).request("POST", "/metrics", {"metric": "x.a.b"})
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(len(http.calls), 3)
        self.assertEqual(len({c["headers"]["Idempotency-Key"] for c in http.calls}), 1)

    def test_gives_up_after_five_5xx(self):
        http = FakeHttp(Resp(502))
        with self.assertRaises(bridge.BridgeError):
            client(http).request("POST", "/metrics", {})
        self.assertEqual(len(http.calls), bridge.MAX_TRIES)

    def test_network_error_is_retried(self):
        http = FakeHttp(ConnectionError("down"), Resp(200, {"ok": 1}))
        self.assertEqual(client(http).request("GET", "/children"), {"ok": 1})

    def test_daily_limit_stops_without_waiting(self):
        sleeps = []
        http = FakeHttp(Resp(429, {"error": {"code": "daily_limit", "message": "Come back tomorrow."}}, {"Retry-After": "40000"}))
        with self.assertRaises(bridge.DailyLimit):
            client(http, sleeps).request("POST", "/events:batch", {"entries": []})
        self.assertEqual(sleeps, [])
        self.assertEqual(len(http.calls), 1)

    def test_4xx_is_not_retried_and_says_why(self):
        http = FakeHttp(Resp(403, {"error": {"code": "scope", "message": "No write:events."}}))
        with self.assertRaisesRegex(bridge.BridgeError, "No write:events"):
            client(http).request("POST", "/events:batch", {"entries": []})
        self.assertEqual(len(http.calls), 1)

    def test_get_has_no_idempotency_key(self):
        http = FakeHttp(Resp(200, {}))
        client(http).request("GET", "/children")
        self.assertNotIn("Idempotency-Key", http.calls[0]["headers"])


class SendTests(unittest.TestCase):
    def setUp(self):
        self._declared = bridge.DECLARED_METRICS
        bridge.DECLARED_METRICS = [{"metric": "x.sample.score", "label": "Sleep score", "unit": "points", "min": 0, "max": 100}]

    def tearDown(self):
        bridge.DECLARED_METRICS = self._declared

    def test_declares_x_metric_once_before_readings(self):
        http = FakeHttp(Resp(200, {"metric": "x.sample.score", "saved": True}), Resp(200, {"saved": 1, "received": 1}))
        state = {}
        readings = [{"metric": "x.sample.score", "started_at": "2026-11-13T09:00:00Z", "value": 80}]
        s = bridge.send(client(http), CHILD, [], readings, state)
        self.assertEqual([c["url"].rsplit("/", 1)[-1] for c in http.calls], ["metrics", "observations:batch"])
        self.assertEqual(s["declared"], ["x.sample.score"])
        http2 = FakeHttp(Resp(200, {"saved": 0, "received": 1}))
        bridge.send(client(http2), CHILD, [], [dict(readings[0], value=81)], state)
        self.assertEqual(len(http2.calls), 1)  # already declared

    def test_summary_lists_reasons(self):
        http = FakeHttp(Resp(200, {"results": [
            {"source_event_id": "a", "status": "saved"},
            {"source_event_id": "b", "status": "suggested"},
            {"source_event_id": "c", "status": "rejected", "error": "volume_ml is too big"},
        ]}))
        entries = [{"source_event_id": s, "kind": "sleep", "started_at": "2026-11-13T09:00:00Z"} for s in "abc"]
        s = bridge.send(client(http), CHILD, entries, [])
        lines = []
        bridge.print_summary(s, [("z", "no started_at")], out=lines.append)
        text = "\n".join(lines)
        self.assertIn("1 saved", text)
        self.assertIn("1 suggested", text)
        self.assertIn("rejected: c: volume_ml is too big", text)
        self.assertIn("overlaps a sleep someone logged", text)
        self.assertIn("rejected before sending: z", text)


class ReplayTests(unittest.TestCase):
    def test_replay_is_noted(self):
        http = FakeHttp(Resp(200, {"results": [{"source_event_id": "a", "status": "saved"}]}, {"Idempotent-Replayed": "true"}))
        c = client(http)
        s = bridge.send(c, CHILD, [{"source_event_id": "a", "kind": "diaper", "started_at": "2026-11-13T09:00:00Z"}], [])
        self.assertEqual(s["replayed"], 1)
        lines = []
        bridge.print_summary(s, [], out=lines.append)
        self.assertIn("saved nothing twice", "\n".join(lines))


class UnsentTests(unittest.TestCase):
    def test_unchanged_rows_are_not_sent_again(self):
        e = [{"source_event_id": "nanit:b1:a", "kind": "sleep", "started_at": "2026-11-18T09:00:00Z"}]
        r = [{"metric": "humidity", "started_at": "2026-11-18T09:00:00Z", "value": 40}]
        state = {}
        http = FakeHttp(Resp(200, {"results": [{"source_event_id": "nanit:b1:a", "status": "saved"}]}), Resp(200, {"saved": 1, "received": 1}))
        bridge.send(client(http), CHILD, e, r, state)
        http2 = FakeHttp(Resp(500))
        s = bridge.send(client(http2), CHILD, e, r, state)
        self.assertEqual((http2.calls, s["sent"]), ([], 0))
        # The sleep ends: only it goes again.
        http3 = FakeHttp(Resp(200, {"results": [{"source_event_id": "nanit:b1:a", "status": "updated"}]}))
        bridge.send(client(http3), CHILD, [dict(e[0], ended_at="2026-11-18T10:00:00Z")], r, state)
        self.assertEqual(len(http3.calls), 1)

    def test_rejected_rows_are_tried_again(self):
        e = [{"source_event_id": "a", "kind": "sleep", "started_at": "2026-11-18T09:00:00Z"}]
        state = {}
        bridge.send(client(FakeHttp(Resp(200, {"results": [{"source_event_id": "a", "status": "rejected", "error": "x"}]}))), CHILD, e, [], state)
        http = FakeHttp(Resp(200, {"results": [{"source_event_id": "a", "status": "saved"}]}))
        bridge.send(client(http), CHILD, e, [], state)
        self.assertEqual(len(http.calls), 1)


class MainTests(unittest.TestCase):
    def run_main(self, args, http, tmp, env=None):
        keys = ("MIARA_BASE", "MIARA_TOKEN", "MIARA_CHILD_ID", "MIARA_CHILDREN")
        old = {k: os.environ.get(k) for k in keys}
        for k in keys:
            os.environ.pop(k, None)
        os.environ.update({"MIARA_BASE": "https://example.test/api/v1", **(env or {})})
        try:
            return bridge.main(args + ["--state", str(Path(tmp) / "s.json")], http=http, sleep=lambda s: None, now=T0)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def ok_http(self):
        return FakeHttp(Resp(200, {"children": [{"id": CHILD}]}),
                        Resp(200, {"results": [{"source_event_id": "nanit:samplebaby:c1", "status": "saved"},
                                               {"source_event_id": "nanit:samplebaby:f7", "status": "saved"},
                                               {"source_event_id": "nanit:samplebaby:log:c2", "status": "saved"}]}),
                        Resp(200, {"saved": 2, "received": 2}))

    def test_sandbox_sends_made_up_data_then_nothing_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            http = self.ok_http()
            self.assertEqual(self.run_main(["--sandbox"], http, tmp), 0)
            sent = http.calls[1]["body"]["entries"]
            self.assertEqual(sorted(e["kind"] for e in sent), ["diaper", "sleep", "sleep"])
            self.assertTrue(all(e["child_id"] == CHILD for e in sent))
            state = json.loads((Path(tmp) / "s.json").read_text())
            self.assertEqual(state["last_sync"], "2026-11-18T12:00:00Z")
            http2 = FakeHttp(Resp(200, {"children": [{"id": CHILD}]}), Resp(500))
            self.assertEqual(self.run_main(["--sandbox"], http2, tmp), 0)
            self.assertEqual([c["url"].rsplit("/", 1)[-1] for c in http2.calls], ["children"])  # nothing new to send

    def test_dry_run_sends_nothing_and_writes_no_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            http = FakeHttp(Resp(500))
            code = bridge.main(["--dry-run", "--state", str(Path(tmp) / "s.json")], http=http, now=T0,
                               nanit_rest=bridge.SampleNanit(T0))
            self.assertEqual(code, 0)
            self.assertEqual(http.calls, [])
            self.assertFalse((Path(tmp) / "s.json").exists())

    def test_a_real_family_needs_a_child_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MIARA_TOKEN"] = "miara_pat_real"
            try:
                http = FakeHttp(Resp(500))
                code = bridge.main(["--state", str(Path(tmp) / "s.json")], http=http, now=T0, nanit_rest=bridge.SampleNanit(T0))
            finally:
                os.environ.pop("MIARA_TOKEN")
            self.assertEqual(code, 1)
            self.assertEqual(http.calls, [])

    def test_daily_limit_stops_and_says_nothing_is_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            http = FakeHttp(Resp(200, {"children": [{"id": CHILD}]}),
                            Resp(429, {"error": {"code": "daily_limit", "message": "Done for today."}}))
            self.assertEqual(self.run_main(["--sandbox"], http, tmp), 3)
            self.assertFalse((Path(tmp) / "s.json").exists())


if __name__ == "__main__":
    unittest.main()


class LinkTests(unittest.TestCase):
    def test_matches_by_first_name_or_the_only_pair(self):
        B = nanit.Baby
        kids = [{"id": "k1", "name": "Juniper (sample)"}, {"id": "k2", "name": "Basil (sample)"},
                {"id": "k3", "name": "Old", "history_only": True}]
        self.assertEqual(bridge.match_children([B("u1", "basil", "c")], kids), {"u1": "k2"})
        self.assertEqual(bridge.match_children([B("u1", "Nursery", "c")], kids), {})
        self.assertEqual(bridge.match_children([B("u1", "Nursery", "c")], kids[:1]), {"u1": "k1"})

    def test_write_env_value_replaces_the_line_and_keeps_the_rest(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("MIARA_TOKEN=t\nMIARA_CHILDREN=old\n")
            bridge.write_env_value("MIARA_CHILDREN", "u1=k1", p)
            self.assertEqual(p.read_text(), "MIARA_TOKEN=t\nMIARA_CHILDREN=u1=k1\n")
            saved = dict(os.environ)
            try:
                os.environ["MIARA_TOKEN"] = "already set"
                bridge.load_env(p)
                self.assertEqual((os.environ["MIARA_TOKEN"], os.environ["MIARA_CHILDREN"]), ("already set", "u1=k1"))
            finally:
                os.environ.clear()
                os.environ.update(saved)
