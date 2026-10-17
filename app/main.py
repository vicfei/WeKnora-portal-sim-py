"""portal-sim-py: the v2-route portal stand-in (Python port of cmd/sim).

"B1 皮, v2 芯": hosts the SAME three portal pages as the B1 route's
WeKnora-portal-proxy, but wired the v2 way — a real WeKnora JWT obtained
through the tenantless bridge, every permission decision made by WeKnora
(grants engine + per-request RBAC), zero filtering in this service.

    browser ──login──▶ sim ──bridge(platform key)──▶ WeKnora JWT
       │                   │ (cookie session, httponly)
       ├── /kb /chat pages ──▶ templated UI
       └── /api/v1/* ──▶ transparent reverse proxy (+ Authorization from
                           cookie when the request has none; SSE passthrough)

Discipline: NO permission logic may appear here (README 纪律1). Grouping in
pages is presentation of server-provided fields (space_type/my_role).
"""
import base64
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urlencode

import asyncpg
import bcrypt
import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from .config import Config
from .session import SESSION_ABS_TTL, PortalSession, SessionStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sim")

SESSION_COOKIE = "sim_portal_session"

# hop-by-hop headers never forwarded in either direction
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length",  # let httpx/starlette reframe bodies
}

cfg = Config.load(os.environ.get("SIM_ENV_FILE", ".env"))
missing = cfg.validate()
if missing:
    raise SystemExit(f"missing config: {missing} (see .env.example)")

templates = Jinja2Templates(directory="templates")
sessions = SessionStore(cfg.session_file)
http = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=None, write=120, pool=120))
db_pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    db_pool = await asyncpg.create_pool(**cfg.db_kwargs(), min_size=1, max_size=4)
    yield
    await db_pool.close()
    await http.aclose()


app = FastAPI(title="portal-sim-py", lifespan=lifespan)


# ── bridge: POST /identity/bridge WITHOUT tenant_id ────────────────

async def bridge(uum_user_id: str) -> tuple[str, str]:
    """WeKnora resolves the login space (personal bootstrap + active grants)
    per the v2 contract. The X-Tenant-ID header only satisfies the
    platform-key gate (must be a REAL tenant); bridge logic itself is
    driven by the request body."""
    resp = await http.post(
        cfg.weknora_base + "/api/v1/identity/bridge",
        json={"uum_user_id": uum_user_id},
        headers={"X-API-Key": cfg.platform_key, "X-Tenant-ID": cfg.tenant_hint},
    )
    try:
        env = resp.json()
    except Exception:
        env = {}
    if not env.get("success"):
        msg = (env.get("error") or {}).get("message", "unexpected response")
        raise RuntimeError(f"HTTP {resp.status_code}: {msg}")
    data = env.get("data") or {}
    return data.get("token", ""), data.get("refresh_token", "")


# ── session helpers ────────────────────────────────────────────────

async def resolve_from_cookie(request: Request) -> PortalSession | None:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    return await sessions.resolve(sid, bridge)


def render(request: Request, name: str, ctx: dict) -> Response:
    ctx.setdefault("request", request)
    return templates.TemplateResponse(request, name + ".html", ctx)


def _session_cookie(value: str, max_age: int) -> dict:
    return {"key": SESSION_COOKIE, "value": value, "httponly": True,
            "samesite": "lax", "path": "/", "max_age": max_age}


async def start_session(uum: str, token: str, refresh: str) -> str:
    display_name = ""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT display_name FROM employees WHERE uum_user_id = $1", uum)
        if row:
            display_name = row["display_name"]
    sid = "s%d" % (time.time_ns())
    await sessions.put(sid, PortalSession(
        token=token, refresh_token=refresh, uum_user_id=uum,
        display_name=display_name,
        token_exp=_exp_of(token), expires=time.time() + SESSION_ABS_TTL,
    ))
    return sid


def _exp_of(tok: str) -> float:
    from .session import token_expiry
    return token_expiry(tok, time.time() + 24 * 3600)


def avatar(name: str) -> str:
    return name[:1] if name else "?"


# ── routes: health & auth pages ────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return Response("ok")


@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return render(request, "login", {"Title": "登录", "Error": request.query_params.get("error", "")})


@app.post("/sso/authorize")
async def sso_authorize(request: Request, uum_user_id: str = Form(""), password: str = Form("")):
    """Validates the employee (same test accounts as B1), exchanges the user
    id for a real WeKnora JWT via the tenantless bridge, and opens a portal
    session. WeKnora did everything: personal-space bootstrap, grant
    materialization, token minting."""
    uum = uum_user_id.strip()

    def deny(reason: str):
        return RedirectResponse("/?" + urlencode({"error": reason}), status_code=302)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT password_hash, is_active FROM employees WHERE uum_user_id = $1", uum)
    ok = False
    if row is not None:
        try:
            ok = bcrypt.checkpw(password.encode(), row["password_hash"].encode())
        except ValueError:
            ok = False
    if row is None or not ok:
        return deny("工号或密码错误")
    if not row["is_active"]:
        return deny("账号已禁用")
    try:
        token, refresh = await bridge(uum)
    except Exception as e:
        return deny(f"WeKnora 换票失败：{e}")
    sid = await start_session(uum, token, refresh)
    resp = RedirectResponse("/kb", status_code=302)
    resp.set_cookie(**_session_cookie(sid, SESSION_ABS_TTL))
    return resp


@app.get("/sso/native")
async def sso_native(request: Request):
    """Earlier demo path: bridge + #bridge_result redirect into the native
    WeKnora frontend (decision 022 contract)."""
    uum = request.query_params.get("uum_user_id", "").strip()
    if not uum:
        return RedirectResponse("/", status_code=302)
    try:
        token, refresh = await bridge(uum)
    except Exception as e:
        return RedirectResponse("/?" + urlencode({"error": f"换票失败：{e}"}), status_code=302)
    payload = json.dumps({"token": token, "refresh_token": refresh})
    frag = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    return RedirectResponse(f"{cfg.frontend_url}/#bridge_result={frag}", status_code=302)


@app.post("/logout")
async def logout(request: Request):
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        await sessions.drop(sid)
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ── routes: portal pages (thin shells — data loads via /api proxy) ─

async def _require(request: Request) -> PortalSession | None:
    return await resolve_from_cookie(request)


@app.get("/kb", response_class=HTMLResponse)
async def kb_page(request: Request):
    sess = await _require(request)
    if not sess:
        return RedirectResponse("/?" + urlencode({"error": "请先登录"}), status_code=302)
    return render(request, "kb", {
        "Title": "知识库", "Nav": "kb", "UserName": sess.display_name, "Avatar": avatar(sess.display_name)})


@app.get("/kb/{kb_id}", response_class=HTMLResponse)
async def kb_detail_page(request: Request, kb_id: str):
    sess = await _require(request)
    if not sess:
        return RedirectResponse("/?" + urlencode({"error": "请先登录"}), status_code=302)
    return render(request, "kb_detail", {
        "Title": "知识库详情", "Nav": "kb", "UserName": sess.display_name,
        "Avatar": avatar(sess.display_name), "KbID": kb_id})


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    sess = await _require(request)
    if not sess:
        return RedirectResponse("/?" + urlencode({"error": "请先登录"}), status_code=302)
    return render(request, "chat", {
        "Title": "智能问答", "Nav": "chat", "UserName": sess.display_name, "Avatar": avatar(sess.display_name)})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    return render(request, "admin", {"Title": "v2 管理台"})


# ── transparent passthrough to WeKnora ────────────────────────────

@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_api(request: Request, path: str):
    """Transparent reverse proxy. Session plumbing only (NOT permission
    logic): attach the bridge-issued JWT when the caller didn't bring its
    own Authorization (the admin page logs in separately and does).
    Streaming both ways so SSE flushes every chunk immediately."""
    url = cfg.weknora_base + request.url.path
    if request.url.query:
        url += "?" + request.url.query

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    if not headers.get("authorization"):
        sess = await resolve_from_cookie(request)
        if sess:
            headers["authorization"] = f"Bearer {sess.token}"

    req = http.build_request(
        request.method, url, headers=headers,
        content=request.stream(),  # stream request body (uploads) untouched
    )
    upstream = await http.send(req, stream=True)

    resp_headers = {k: v for k, v in upstream.headers.items()
                    if k.lower() not in HOP_HEADERS and k.lower() != "content-encoding"}

    async def body_iter():
        try:
            async for chunk in upstream.aiter_raw():
                if chunk:
                    yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body_iter(), status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )
