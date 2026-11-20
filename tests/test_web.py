"""The password, the cookie, and the scheduled route's bearer check. Offline."""
import unittest

import auth

try:
    from fastapi.testclient import TestClient
    import app as web
except ImportError:  # the computer path doesn't install fastapi
    TestClient = None

SECRET = "correct horse battery"


class AuthTests(unittest.TestCase):
    def test_secret_must_be_set_and_long_enough(self):
        self.assertIn("CRON_SECRET", auth.secret_problem(None))
        self.assertIn("CRON_SECRET", auth.secret_problem("short"))
        self.assertIsNone(auth.secret_problem(SECRET))

    def test_bearer(self):
        self.assertTrue(auth.bearer_ok(f"Bearer {SECRET}", SECRET))
        self.assertFalse(auth.bearer_ok(None, SECRET))
        self.assertFalse(auth.bearer_ok("Bearer nope", SECRET))
        self.assertFalse(auth.bearer_ok(f"Bearer {SECRET}", None))  # unset secret never matches
        self.assertFalse(auth.bearer_ok("Bearer ", ""))

    def test_cookie_signed_expires_and_tied_to_the_secret(self):
        c = auth.make_cookie(SECRET, now=lambda: 1000)
        self.assertTrue(auth.cookie_ok(c, SECRET, now=lambda: 1001))
        self.assertFalse(auth.cookie_ok(c, SECRET, now=lambda: 1000 + auth.COOKIE_SECONDS + 1))
        self.assertFalse(auth.cookie_ok(c, "another secret value", now=lambda: 1001))
        stamp, mac = c.split(".")
        self.assertFalse(auth.cookie_ok(f"{int(stamp) + 99999}.{mac}", SECRET, now=lambda: 1001))
        self.assertFalse(auth.cookie_ok("junk", SECRET))


@unittest.skipIf(TestClient is None, "fastapi isn't installed")
class RouteTests(unittest.TestCase):
    def setUp(self):
        import os
        import store
        self.env = dict(os.environ)
        os.environ["CRON_SECRET"] = SECRET
        os.environ.pop("BRIDGE_SANDBOX", None)
        store.use(store.MemoryStore())
        self.c = TestClient(web.app, base_url="https://example.vercel.app")

    def tearDown(self):
        import os
        import store
        os.environ.clear()
        os.environ.update(self.env)
        store.use(None)

    def test_sync_needs_the_bearer(self):
        self.assertEqual(self.c.get("/api/sync").status_code, 401)
        self.assertEqual(self.c.get("/api/sync", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        ok = self.c.get("/api/sync", headers={"Authorization": f"Bearer {SECRET}"})
        self.assertEqual(ok.status_code, 200)  # not set up yet: nothing to do
        self.assertIn("skipped", ok.json())

    def test_sync_refuses_when_the_secret_is_unset(self):
        import os
        del os.environ["CRON_SECRET"]
        self.assertEqual(self.c.get("/api/sync", headers={"Authorization": "Bearer "}).status_code, 401)

    def test_setup_is_locked_without_a_good_secret(self):
        import os
        os.environ["CRON_SECRET"] = "short"
        self.assertIn("locked", self.c.get("/api/status").json())
        self.assertEqual(self.c.post("/api/login", json={"password": "short"}).status_code, 403)

    def test_setup_routes_need_the_password_cookie(self):
        self.assertEqual(self.c.get("/api/children").status_code, 401)
        self.assertEqual(self.c.post("/api/nanit/signin", json={}).status_code, 401)
        self.assertEqual(self.c.post("/api/disconnect").status_code, 401)
        bad = self.c.post("/api/login", json={"password": "wrong password!"})
        self.assertEqual(bad.status_code, 401)
        self.assertNotIn("set-cookie", bad.headers)
        good = self.c.post("/api/login", json={"password": SECRET})
        self.assertEqual(good.status_code, 200)
        cookie = good.headers["set-cookie"].lower()
        for flag in ("httponly", "secure", "samesite=strict"):
            self.assertIn(flag, cookie)
        self.assertTrue(self.c.get("/api/status").json()["signed_in"])

    def test_a_run_holding_the_lock_makes_setup_changes_wait(self):
        import store
        self.c.post("/api/login", json={"password": SECRET})
        with store.active().leased():
            self.assertEqual(self.c.post("/api/disconnect").status_code, 409)
        self.assertEqual(self.c.post("/api/disconnect").status_code, 200)

    def test_page_holds_no_secrets(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(SECRET, r.text)


if __name__ == "__main__":
    unittest.main()
