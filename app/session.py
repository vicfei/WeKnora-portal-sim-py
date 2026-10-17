"""Session store with disk persistence and transparent re-bridge.

Session plumbing only (README 纪律1): the JWT is WeKnora's judgment of who
this is; this module never decides what they may do.

Lifecycle: a portal session outlives the 24h bridge token. Sessions are
persisted to disk so a restart keeps users logged in, and a token at/near
expiry is silently re-bridged (bridge is idempotent — WeKnora re-resolves
spaces and re-judges everything on every token).
"""
import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field

log = logging.getLogger("sim.session")

SESSION_ABS_TTL = 30 * 24 * 3600  # absolute portal-session lifetime (s)
TOKEN_REFRESH_SKEW = 5 * 60       # re-bridge this close to expiry


def token_expiry(tok: str, fallback: float) -> float:
    """Parse the exp claim out of a JWT WITHOUT verifying it — WeKnora is
    the verifier; we only schedule our own re-bridge."""
    parts = tok.split(".")
    if len(parts) != 3:
        return fallback
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        if not exp or exp <= 0:
            return fallback
        return float(exp)
    except Exception:
        return fallback


@dataclass
class PortalSession:
    token: str
    refresh_token: str
    uum_user_id: str
    display_name: str
    token_exp: float                      # expiry of the current JWT (bridge issues 24h)
    expires: float                        # absolute session expiry (cookie mirrors this)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def stale_token(self) -> bool:
        return self.token_exp - time.time() < TOKEN_REFRESH_SKEW


class SessionStore:
    """cookie id → bridge-issued JWT. path="" → memory only."""

    def __init__(self, path: str = ""):
        self.sessions: dict[str, PortalSession] = {}
        self.path = path
        self.mu = asyncio.Lock()
        if path:
            self._load_from_disk()

    # ── persistence ────────────────────────────────────────────────

    def _load_from_disk(self):
        try:
            raw = open(self.path).read()
        except FileNotFoundError:
            return
        except OSError as e:
            log.warning("session store: read %s failed: %s", self.path, e)
            return
        try:
            recs = json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("session store: parse %s failed, starting empty: %s", self.path, e)
            return
        now = time.time()
        for r in recs:
            if not r.get("id") or not r.get("uum") or not r.get("token"):
                continue
            if now > r.get("expires", 0):
                continue  # absolute lifetime over; user re-logs in
            # token_exp in the past is fine: first use will re-bridge
            self.sessions[r["id"]] = PortalSession(
                token=r["token"], refresh_token=r.get("refresh", ""),
                uum_user_id=r["uum"], display_name=r.get("name", ""),
                token_exp=float(r.get("token_exp", 0)), expires=float(r.get("expires", 0)),
            )
        log.info("session store: restored %d session(s) from %s", len(self.sessions), self.path)

    def _save(self):
        """Caller holds self.mu. Atomic write: tmp + rename, 0600 — the file
        holds live JWTs, so it must sit outside the repo with tight perms."""
        if not self.path:
            return
        now = time.time()
        recs = [
            {
                "id": sid, "uum": s.uum_user_id, "token": s.token,
                "refresh": s.refresh_token, "name": s.display_name,
                "token_exp": int(s.token_exp), "expires": int(s.expires),
            }
            for sid, s in self.sessions.items() if now <= s.expires
        ]
        tmp = self.path + ".tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(recs))
            os.rename(tmp, self.path)
        except OSError as e:
            log.warning("session store: write failed: %s", e)

    # ── operations ─────────────────────────────────────────────────

    async def put(self, sid: str, sess: PortalSession):
        async with self.mu:
            self.sessions[sid] = sess
            now = time.time()  # opportunistic sweep
            for k in [k for k, v in self.sessions.items() if now > v.expires]:
                del self.sessions[k]
            self._save()

    async def drop(self, sid: str):
        async with self.mu:
            self.sessions.pop(sid, None)
            self._save()

    async def resolve(self, sid: str, bridge) -> PortalSession | None:
        """Returns a session with a usable JWT, transparently re-bridging when
        the stored token is at/near expiry. None when the cookie matches no
        live session. A failed re-bridge falls back to the stale token
        (WeKnora will 401 it) rather than killing the session."""
        async with self.mu:
            sess = self.sessions.get(sid)
            if sess and time.time() > sess.expires:
                del self.sessions[sid]
                self._save()
                sess = None
        if not sess:
            return None
        if not sess.stale_token():
            return sess
        async with sess.lock:
            if sess.token_exp - time.time() >= TOKEN_REFRESH_SKEW:
                return sess  # refreshed while waiting
            try:
                token, refresh = await bridge(sess.uum_user_id)
            except Exception as e:
                log.warning("session store: re-bridge for %s failed (%s); keeping stale token",
                            sess.uum_user_id, e)
                return sess
            sess.token, sess.refresh_token = token, refresh
            sess.token_exp = token_expiry(token, time.time() + 24 * 3600)
            log.info("session store: re-bridged %s, token exp %d", sess.uum_user_id, sess.token_exp)
        async with self.mu:
            self._save()
        return sess
