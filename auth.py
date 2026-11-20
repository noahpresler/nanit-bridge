"""The password and the cookie for the setup page, and the bearer check for the scheduled run.

One secret, CRON_SECRET, does both jobs: Vercel sends it as `Authorization: Bearer <secret>` on
every scheduled run, and it's the password for the setup page. Nothing here logs or returns it.
"""
from __future__ import annotations

import hashlib
import hmac
import time

MIN_SECRET = 12
COOKIE = "nb_session"
COOKIE_SECONDS = 12 * 3600


def secret_problem(secret: str | None) -> str | None:
    """Why setup must not run, in words for the person who deployed it. None when the secret is fine."""
    if not secret:
        return ("Setup is locked because CRON_SECRET isn't set. In Vercel, open this project, then "
                "Settings, Environment Variables, and add CRON_SECRET with a password of "
                f"{MIN_SECRET} or more characters. Then open Deployments, choose the latest one, "
                "and pick Redeploy.")
    if len(secret) < MIN_SECRET:
        return (f"Setup is locked because CRON_SECRET is shorter than {MIN_SECRET} characters. In Vercel, "
                "open this project, then Settings, Environment Variables, and change CRON_SECRET to a "
                "longer password. Then open Deployments, choose the latest one, and pick Redeploy.")
    return None


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def bearer_ok(header: str | None, secret: str | None) -> bool:
    if secret_problem(secret) or not header:
        return False
    return same(header, f"Bearer {secret}")


def password_ok(given: str | None, secret: str | None) -> bool:
    if secret_problem(secret) or not given:
        return False
    return same(given, secret)


def _mac(secret: str, expires: int) -> str:
    return hmac.new(secret.encode(), f"cookie:{expires}".encode(), hashlib.sha256).hexdigest()


def make_cookie(secret: str, now=time.time) -> str:
    expires = int(now()) + COOKIE_SECONDS
    return f"{expires}.{_mac(secret, expires)}"


def cookie_ok(value: str | None, secret: str | None, now=time.time) -> bool:
    if secret_problem(secret) or not value or "." not in value:
        return False
    stamp, mac = value.split(".", 1)
    try:
        expires = int(stamp)
    except ValueError:
        return False
    return expires > now() and same(mac, _mac(secret, expires))
