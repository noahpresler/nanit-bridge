import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bridge  # noqa: E402

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


class MapTests(unittest.TestCase):
    def test_sample_maps_to_entries_and_readings(self):
        entries, readings = bridge.map(bridge.fetch(None))
        self.assertEqual({e["kind"] for e in entries}, {"sleep", "bottle", "nursing", "diaper", "pump"})
        self.assertEqual({r["metric"] for r in readings}, {"room_temp_c", "sleep_stage", "x.sample.score"})
        self.assertTrue(all(e["source_event_id"].startswith("sample:") for e in entries))

    def test_source_event_ids_come_from_vendor_ids_and_are_stable(self):
        a, _ = bridge.map(bridge.fetch(None))
        b, _ = bridge.map(bridge.fetch(None))
        self.assertEqual([e["source_event_id"] for e in a], [e["source_event_id"] for e in b])
        self.assertEqual(len({e["source_event_id"] for e in a}), len(a))

    def test_event_id_is_valid_for_odd_and_long_ids(self):
        import re
        for vid in ["a b/c", "x" * 300, "ünï-1"]:
            out = bridge.event_id(vid)
            self.assertRegex(out, r"^[A-Za-z0-9._:-]{1,128}$")
        self.assertNotEqual(bridge.event_id("x" * 300), bridge.event_id("x" * 301))

    def test_times_are_utc_z_to_the_second(self):
        self.assertEqual(bridge.iso("2026-11-13T09:00:00-05:00"), "2026-11-13T14:00:00Z")
        self.assertEqual(bridge.iso("2026-11-13T09:00:00"), "2026-11-13T09:00:00Z")

    def test_fetch_since_filters(self):
        later = datetime(2100, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(bridge.fetch(later), [])


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
    def test_declares_x_metric_once_before_readings(self):
        http = FakeHttp(Resp(200, {"metric": "x.sample.score", "saved": True}), Resp(200, {"saved": 1, "received": 1}))
        state = {}
        readings = [{"metric": "x.sample.score", "started_at": "2026-11-13T09:00:00Z", "value": 80}]
        s = bridge.send(client(http), CHILD, [], readings, state)
        self.assertEqual([c["url"].rsplit("/", 1)[-1] for c in http.calls], ["metrics", "observations:batch"])
        self.assertEqual(s["declared"], ["x.sample.score"])
        http2 = FakeHttp(Resp(200, {"saved": 0, "received": 1}))
        bridge.send(client(http2), CHILD, [], readings, state)
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


class MainTests(unittest.TestCase):
    def run_main(self, args, http, tmp):
        env = {"MIARA_BASE": "https://example.test/api/v1"}
        old = {k: os.environ.get(k) for k in ("MIARA_BASE", "MIARA_TOKEN", "MIARA_CHILD_ID")}
        os.environ.pop("MIARA_TOKEN", None)
        os.environ.pop("MIARA_CHILD_ID", None)
        os.environ.update(env)
        try:
            return bridge.main(args + ["--state", str(Path(tmp) / "s.json")], http=http, sleep=lambda s: None,
                               now=datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc))
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def ok_http(self):
        def many(n):
            return Resp(200, {"results": [{"source_event_id": "x", "status": "saved"}] * n})
        return FakeHttp(Resp(200, {"children": [{"id": CHILD}]}), many(5), Resp(200, {"metric": "x.sample.score", "saved": True}),
                        Resp(200, {"saved": 11, "received": 11}))

    def test_second_run_sends_nothing_and_state_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            http = self.ok_http()
            self.assertEqual(self.run_main(["--sandbox"], http, tmp), 0)
            state = json.loads((Path(tmp) / "s.json").read_text())
            self.assertEqual(state["last_sync"], "2026-11-18T12:00:00Z")
            http2 = FakeHttp(Resp(500))
            self.assertEqual(self.run_main(["--sandbox"], http2, tmp), 0)
            self.assertEqual(http2.calls, [])  # nothing new, no requests at all

    def test_dry_run_sends_nothing_and_writes_no_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            http = FakeHttp(Resp(500))
            self.assertEqual(self.run_main(["--sandbox", "--dry-run"], http, tmp), 0)
            self.assertEqual(http.calls, [])
            self.assertFalse((Path(tmp) / "s.json").exists())

    def test_sample_refuses_a_real_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MIARA_TOKEN"] = "miara_pat_real"
            try:
                http = FakeHttp(Resp(500))
                code = bridge.main(["--child", CHILD, "--state", str(Path(tmp) / "s.json")], http=http)
            finally:
                os.environ.pop("MIARA_TOKEN")
            self.assertEqual(code, 1)
            self.assertEqual(http.calls, [])

    def test_daily_limit_keeps_last_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            http = FakeHttp(Resp(200, {"children": [{"id": CHILD}]}),
                            Resp(429, {"error": {"code": "daily_limit", "message": "Done for today."}}))
            self.assertEqual(self.run_main(["--sandbox"], http, tmp), 3)
            self.assertFalse((Path(tmp) / "s.json").exists())


if __name__ == "__main__":
    unittest.main()
