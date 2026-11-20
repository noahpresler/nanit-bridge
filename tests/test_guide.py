import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bridge  # noqa: E402
import guide  # noqa: E402
import nanit  # noqa: E402
from tests.test_bridge import CHILD, T0, FakeHttp, Resp  # noqa: E402

KIDS = [{"id": CHILD, "name": "Juniper (sample)"}, {"id": "k2", "name": "Basil (sample)"}]


class Script(guide.IO):
    """Answers questions in order and keeps what was said."""

    def __init__(self, *answers):
        self.answers, self.said = list(answers), []
        super().__init__(ask=self._next, secret=self._next, out=self.said.append)

    def _next(self, prompt):
        self.said.append(prompt)
        return self.answers.pop(0)

    def text(self):
        return "\n".join(self.said)


class GuideTests(unittest.TestCase):
    def setUp(self):
        self.old = {k: os.environ.get(k) for k in ("MIARA_TOKEN", "MIARA_CHILDREN", "MIARA_BASE")}
        for k in self.old:
            os.environ.pop(k, None)
        os.environ["MIARA_BASE"] = "https://example.test/api/v1"
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / ".env"

    def tearDown(self):
        for k, v in self.old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def test_nanit_sign_in_tries_again_after_a_wrong_password(self):
        tries = []

        def sign_in(email, password, code):
            tries.append(password)
            if password == "wrong":
                raise nanit.NanitError("Nanit didn't accept that email and password.")
            code()
            return [nanit.Baby("u1", "Juniper", "cam")]

        io = Script("me@example.com", "wrong", "me@example.com", "right", "123456")
        babies = guide.sign_in_to_nanit(io, signed_in=[], sign_in=sign_in)
        self.assertEqual([b.uid for b in babies], ["u1"])
        self.assertEqual(tries, ["wrong", "right"])
        self.assertIn("Try again.", io.text())

    def test_keeps_an_existing_nanit_sign_in(self):
        io = Script("")
        babies = guide.sign_in_to_nanit(io, signed_in=[nanit.Baby("u1", "Juniper", "cam")],
                                        sign_in=lambda *a: self.fail("asked to sign in again"))
        self.assertEqual(len(babies), 1)

    def test_miara_token_is_checked_then_saved(self):
        http = FakeHttp(Resp(200, {"children": KIDS}))
        io = Script("not a token", "miara_pat_abc")
        token, kids = guide.connect_miara(io, self.env, http=http)
        self.assertEqual(token, "miara_pat_abc")
        self.assertIn("It starts with miara_pat_", io.text())
        self.assertIn("MIARA_TOKEN=miara_pat_abc", self.env.read_text())
        self.assertEqual(oct(self.env.stat().st_mode & 0o777), "0o600")

    def test_child_matched_by_name_or_picked_from_a_list(self):
        babies = [nanit.Baby("u1", "Juniper ", "cam"), nanit.Baby("u2", "Nursery", "cam2")]
        io = Script("y", "2")
        pairs = guide.choose_children(io, babies, KIDS, self.env)
        self.assertEqual(pairs, {"u1": CHILD, "u2": "k2"})
        self.assertIn("Nanit's Juniper is Miara's Juniper (sample). Right?", io.text())
        self.assertIn(f"MIARA_CHILDREN=u1={CHILD},u2=k2", self.env.read_text())

    def test_shows_the_sleeps_and_sends_only_on_yes(self):
        n = nanit.Nanit(rest=bridge.SampleNanit(T0))
        state = Path(self.tmp.name) / "state.json"
        no = Script("n")
        self.assertFalse(guide.check_and_send(no, "tok", {"*": CHILD}, state, http=FakeHttp(Resp(500)), nanit_client=n, now=T0))
        self.assertIn("Nanit has 2 sleeps, 1 diaper to add to Miara:", no.text())
        self.assertIn("still asleep", no.text())
        self.assertIn("Nothing was sent.", no.text())

        http = FakeHttp(Resp(200, {"results": [{"source_event_id": "nanit:samplebaby:c1", "status": "saved"},
                                               {"source_event_id": "nanit:samplebaby:f7", "status": "saved"}]}),
                        Resp(200, {"saved": 2, "received": 2}))
        yes = Script("y")
        self.assertTrue(guide.check_and_send(yes, "tok", {"*": CHILD}, state, http=http, nanit_client=n, now=T0))
        self.assertIn("✓ Added", yes.text())
        self.assertTrue(state.exists())

    def test_background_service_runs_watch_from_this_folder(self):
        text = guide.plist_text("/x/.venv/bin/python")
        self.assertIn("<string>/x/.venv/bin/python</string>", text)
        self.assertIn(f"<string>{guide.HERE / 'bridge.py'}</string><string>watch</string>", text)
        self.assertIn("<key>KeepAlive</key><true/>", text)


class FakeRun:
    """Answers pmset, defaults and fdesetup like a Mac with the given settings."""

    def __init__(self, laptop=False, sleep="0", autorestart="1", auto_login=False, filevault=False):
        self.out = {
            "pmset -g batt": "Now drawing from 'AC Power'\n" + (" -InternalBattery-0 63%\n" if laptop else ""),
            "pmset -g custom": f"AC Power:\n sleep                {sleep}\n autorestart          {autorestart}\n",
            "fdesetup status": "FileVault is On." if filevault else "FileVault is Off.",
        }
        self.auto_login = auto_login

    def __call__(self, args, capture_output=True, text=True):
        key = " ".join(args)
        code = 0
        if args[0] == "defaults":
            code = 0 if self.auto_login else 1
        return type("R", (), {"returncode": code, "stdout": self.out.get(key, ""), "stderr": ""})()


class AlwaysOnTests(unittest.TestCase):
    def test_a_mac_mini_set_up_to_stay_on_needs_nothing(self):
        self.assertEqual(guide.always_on_tips(FakeRun(auto_login=True)), [])

    def test_a_mac_mini_that_sleeps_and_waits_for_login(self):
        tips = guide.always_on_tips(FakeRun(sleep="10", autorestart="0"))
        self.assertEqual(len(tips), 3)
        self.assertIn("Energy, turn on \"Prevent automatic sleeping when the display is off\"", tips[0])
        self.assertIn("Start up automatically after a power failure", tips[1])
        self.assertIn("Automatically log in as", tips[2])

    def test_filevault_means_log_in_after_a_restart(self):
        tips = guide.always_on_tips(FakeRun(filevault=True))
        self.assertEqual(len(tips), 1)
        self.assertIn("sudo fdesetup authrestart", tips[0])

    def test_a_laptop_only_hears_about_sleep(self):
        self.assertEqual(guide.always_on_tips(FakeRun(laptop=True)), [])
        self.assertIn("power adapter", guide.always_on_tips(FakeRun(laptop=True, sleep="15"))[0])


if __name__ == "__main__":
    unittest.main()
