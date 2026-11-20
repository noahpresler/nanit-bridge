"""Opt-in: talks to the real public sandbox. Run with  MIARA_LIVE=1 python -m unittest tests.test_live_sandbox

The sandbox is shared and rate limited (30 requests a minute for everyone), so this stays small.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bridge  # noqa: E402


@unittest.skipUnless(os.environ.get("MIARA_LIVE") == "1", "set MIARA_LIVE=1 to run against the sandbox")
class LiveSandbox(unittest.TestCase):
    def test_send_then_resend_is_unchanged(self):
        base = os.environ.get("MIARA_BASE", bridge.DEFAULT_BASE)
        c = bridge.Client(base, bridge.SANDBOX_TOKEN)
        child = bridge.pick_sandbox_child(c)
        n = bridge.SampleNanit()
        item = {"type": "baby", "uid": "samplebaby", "feed": n.get("/babies/x/events", {})["events"],
                "calendar": n.get("/babies/x/calendar", {})["calendar"], "room": {"celsius": 21.4, "humidity": 46},
                "at": bridge.iso(n.now)}
        entries, readings = bridge.map([item], {}, {"*": child})
        entries, readings, problems = bridge.check(entries, readings, {d["metric"] for d in bridge.DECLARED_METRICS})
        self.assertEqual(problems, [])
        state = {}
        first = bridge.send(c, child, entries, readings, state)
        self.assertEqual(first["entries"]["rejected"], 0)
        # The same batch would get the same Idempotency-Key and be answered from the first request, so
        # reverse it: same entries, a new key, and Miara has to compare them with what it already holds.
        again = bridge.send(c, child, entries[::-1], readings[::-1], {})
        # Entries that were saved or updated the first time come back unchanged. Suggested stay suggested.
        self.assertEqual(again["entries"]["saved"] + again["entries"]["updated"], 0)
        self.assertGreater(again["entries"]["unchanged"] + again["entries"]["suggested"] + again["entries"]["skipped"], 0)


if __name__ == "__main__":
    unittest.main()
