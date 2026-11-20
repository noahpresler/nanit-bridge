"""The storage seam and the run lock. Offline."""
import tempfile
import unittest
from pathlib import Path

import nanit
import store


class FakeBlob:
    """Just enough of vercel.blob.sync: files by path, listed by prefix."""

    class BlobNotFoundError(Exception):
        pass

    def __init__(self):
        self.files, self.puts = {}, 0

    def put_blob(self, path, data, **kw):
        assert kw["access"] == "private"
        self.puts += 1
        self.files[path] = data

    def query_blobs(self, prefix=""):
        return [type("B", (), {"pathname": p}) for p in list(self.files) if p.startswith(prefix)]

    def get_blob(self, path, access):
        if path not in self.files:
            raise self.BlobNotFoundError()
        return type("G", (), {"body": self.files[path]})

    def delete_blob(self, paths):
        for p in [paths] if isinstance(paths, str) else paths:
            self.files.pop(p, None)


def stores():
    tmp = tempfile.TemporaryDirectory()
    return tmp, [store.MemoryStore(), store.FileStore(Path(tmp.name)), store.BlobStore(FakeBlob())]


class StoreTests(unittest.TestCase):
    def test_round_trip_and_replace(self):
        tmp, all_ = stores()
        for s in all_:
            self.assertIsNone(s.get("a.json"))
            s.put_json("a.json", {"n": 1})
            s.put_json("a.json", {"n": 2})
            self.assertEqual(s.get_json("a.json"), {"n": 2})
            s.delete("a.json")
            self.assertIsNone(s.get("a.json"))
        tmp.cleanup()

    def test_blob_never_overwrites_and_keeps_only_the_newest(self):
        fake = FakeBlob()
        s = store.BlobStore(fake)
        for n in range(4):
            s.put_json("session.json", {"n": n})
        self.assertEqual(fake.puts, 4)
        self.assertEqual(len(fake.files), 1)
        self.assertEqual(s.get_json("session.json"), {"n": 3})

    def test_lock_excludes_a_second_run_until_released_or_expired(self):
        tmp, all_ = stores()
        for s in all_:
            clock = [1000.0]
            now = lambda: clock[0]  # noqa: E731
            first = s.lock(60, now)
            self.assertIsNotNone(first)
            clock[0] += 1
            self.assertIsNone(s.lock(60, now))
            s.unlock(first)
            second = s.lock(60, now)
            self.assertIsNotNone(second)
            clock[0] += 61  # the run died: its lease runs out
            self.assertIsNotNone(s.lock(60, now))
        tmp.cleanup()

    def test_leased_raises_busy(self):
        s = store.MemoryStore()
        with s.leased():
            with self.assertRaises(store.Busy):
                with s.leased():
                    pass
        with s.leased():
            pass


class SessionSeamTests(unittest.TestCase):
    def tearDown(self):
        store.use(None)

    def test_session_goes_to_the_active_store_not_the_disk(self):
        mem = store.MemoryStore()
        store.use(mem)
        nanit.save_session({"access_token": "a", "refresh_token": "r1"})
        self.assertEqual(nanit.load_session()["refresh_token"], "r1")
        self.assertIn("session.json", mem.files)

    def test_every_rotated_refresh_token_is_saved(self):
        mem = store.MemoryStore()
        store.use(mem)
        nanit.save_session({"access_token": "a", "refresh_token": "r1", "email": "kept"})
        n = nanit.Nanit()
        n._tokens_refreshed("a2", "r2")
        n._tokens_refreshed("a3", "r3")
        saved = nanit.load_session()
        self.assertEqual((saved["refresh_token"], saved["email"]), ("r3", "kept"))

    def test_forget_and_missing(self):
        store.use(store.MemoryStore())
        with self.assertRaises(nanit.NanitError):
            nanit.load_session()
        nanit.save_session({"refresh_token": "r"})
        nanit.forget_session()
        with self.assertRaises(nanit.NanitError):
            nanit.load_session()

    def test_files_stay_the_default_on_a_computer(self):
        store.use(None)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            nanit.save_session({"refresh_token": "r"}, p)
            self.assertEqual(nanit.load_session(p)["refresh_token"], "r")


if __name__ == "__main__":
    unittest.main()
