"""Where the bridge keeps its few small files: the Nanit session, state, and (on Vercel) its settings.

On a computer that's the filesystem, as before (nothing here is used unless asked for). On Vercel
the filesystem is read-only and forgets everything, so the files go in a private Vercel Blob store
in the person's own Vercel account.

Blob serves reads through a cache, so a file that's overwritten can read back old for up to a minute.
Nanit rotates its refresh token on every refresh, so reading an old one would sign the bridge out.
So Blob files are never overwritten: every write is a new file named `<name>@<time>-<random>`,
the newest one wins, and older ones are deleted after. The run lock works the same way, from
file names alone.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path


class Busy(Exception):
    """Another run holds the lock."""


class Store:
    def get(self, name: str) -> bytes | None: raise NotImplementedError
    def put(self, name: str, data: bytes) -> None: raise NotImplementedError
    def delete(self, name: str) -> None: raise NotImplementedError
    def lock(self, ttl: float, now=time.time) -> str | None: raise NotImplementedError
    def unlock(self, token: str) -> None: raise NotImplementedError

    def get_json(self, name: str):
        raw = self.get(name)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def put_json(self, name: str, value) -> None:
        self.put(name, json.dumps(value, indent=2, sort_keys=True).encode())

    def leased(self, ttl: float = 240, now=time.time) -> "_Lease":
        """`with store.leased():` runs only if no other run holds the lock. Raises Busy otherwise."""
        return _Lease(self, ttl, now)


class _Lease:
    def __init__(self, store: Store, ttl: float, now):
        self.store, self.ttl, self.now, self.token = store, ttl, now, None

    def __enter__(self):
        self.token = self.store.lock(self.ttl, self.now)
        if self.token is None:
            raise Busy()
        return self

    def __exit__(self, *exc):
        try:
            self.store.unlock(self.token)
        except Exception:
            pass  # it expires on its own


_last_us = 0


def _stamp(now) -> str:
    """Microseconds, and always higher than the last one handed out, so the newest write sorts last."""
    global _last_us
    _last_us = max(int(now() * 1_000_000), _last_us + 1)
    return f"{_last_us:017d}"


def _lock_name(now, ttl: float) -> str:
    # created, expires, random: sorting by name sorts by who asked first
    return f"{_stamp(now)}-{int((now() + ttl) * 1000):015d}-{secrets.token_hex(3)}"


def _live(names, now) -> list[str]:
    out = []
    for n in sorted(names):
        try:
            if int(n.split("-")[1]) > now() * 1000:
                out.append(n)
        except (IndexError, ValueError):
            continue
    return out


class MemoryStore(Store):
    """For tests."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.locks: set[str] = set()

    def get(self, name): return self.files.get(name)
    def put(self, name, data): self.files[name] = bytes(data)
    def delete(self, name): self.files.pop(name, None)

    def lock(self, ttl, now=time.time):
        mine = _lock_name(now, ttl)
        self.locks.add(mine)
        live = _live(self.locks, now)
        self.locks = set(live) | {mine}
        if live and live[0] != mine:
            self.locks.discard(mine)
            return None
        return mine

    def unlock(self, token): self.locks.discard(token)


class FileStore(Store):
    def __init__(self, root: Path):
        self.root = Path(root)

    def _p(self, name: str) -> Path:
        return self.root / name

    def get(self, name):
        try:
            return self._p(name).read_bytes()
        except FileNotFoundError:
            return None

    def put(self, name, data):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._p(name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        tmp.replace(self._p(name))

    def delete(self, name):
        self._p(name).unlink(missing_ok=True)

    def _locks(self) -> list[str]:
        d = self.root / "locks"
        return [p.name for p in d.iterdir()] if d.is_dir() else []

    def lock(self, ttl, now=time.time):
        d = self.root / "locks"
        d.mkdir(parents=True, exist_ok=True)
        mine = _lock_name(now, ttl)
        (d / mine).write_text("")
        live = _live(self._locks(), now)
        for n in self._locks():
            if n not in live and n != mine:
                (d / n).unlink(missing_ok=True)
        if live and live[0] != mine:
            (d / mine).unlink(missing_ok=True)
            return None
        return mine

    def unlock(self, token):
        (self.root / "locks" / token).unlink(missing_ok=True)


class BlobStore(Store):
    """Private Vercel Blob. `blob` is vercel.blob.sync, passed in by tests."""

    PREFIX = "bridge/"

    def __init__(self, blob=None):
        if blob is None:
            from vercel.blob import sync as blob
        self.blob = blob

    def _versions(self, name: str) -> list[str]:
        prefix = f"{self.PREFIX}{name}@"
        return sorted(b.pathname for b in self.blob.query_blobs(prefix=prefix))

    def get(self, name):
        found = self._versions(name)
        if not found:
            return None
        try:
            return self.blob.get_blob(found[-1], access="private").body
        except self.blob.BlobNotFoundError:
            return None  # deleted between the listing and the read

    def put(self, name, data):
        path = f"{self.PREFIX}{name}@{_stamp(time.time)}-{secrets.token_hex(3)}"
        last = None
        for attempt in range(3):
            try:
                self.blob.put_blob(path, bytes(data), access="private", content_type="application/json",
                                   allow_overwrite=True)
                break
            except Exception as exc:  # a lost write of a rotated token signs the bridge out: try again
                last = exc
                time.sleep(0.5 * (attempt + 1))
        else:
            raise last
        old = [p for p in self._versions(name) if p != path and p < path]
        if old:
            try:
                self.blob.delete_blob(old)
            except Exception:
                pass  # harmless: only the newest is ever read

    def delete(self, name):
        found = self._versions(name)
        if found:
            self.blob.delete_blob(found)

    def _locks(self) -> list[str]:
        return [p.rsplit("/", 1)[-1] for p in
                sorted(b.pathname for b in self.blob.query_blobs(prefix=f"{self.PREFIX}locks/"))]

    def lock(self, ttl, now=time.time):
        mine = _lock_name(now, ttl)
        self.blob.put_blob(f"{self.PREFIX}locks/{mine}", b"", access="private", allow_overwrite=True)
        names = self._locks()
        live = _live(names, now)
        dead = [f"{self.PREFIX}locks/{n}" for n in names if n not in live and n != mine]
        if dead:
            try:
                self.blob.delete_blob(dead)
            except Exception:
                pass
        if live and live[0] != mine:
            self.unlock(mine)
            return None
        return mine

    def unlock(self, token):
        self.blob.delete_blob(f"{self.PREFIX}locks/{token}")


_active: Store | None = None
_resolved = False


def active() -> Store | None:
    """The store in use, or None to use plain files (a computer). On Vercel: the private Blob store."""
    global _active, _resolved
    if not _resolved:
        if os.environ.get("VERCEL") or os.environ.get("BRIDGE_STORE") == "blob":
            _active = BlobStore()
        _resolved = True
    return _active


def use(store: Store | None) -> None:
    """Choose the store by hand (tests, and the web app)."""
    global _active, _resolved
    _active, _resolved = store, True
