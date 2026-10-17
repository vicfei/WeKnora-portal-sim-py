# WeKnora-portal-sim-py（v2 路线 · 门户替身 · Python 版）

`WeKnora-portal-sim`（Go 原作）的 Python 移植。**角色定位**（"B1 皮 + v2 芯"）：承载与 B1
路线同款的门户三页（登录/知识库/问答），但接线全走 v2——真实 JWT（bridge 换票）、权限裁决
全部在 WeKnora（grants 引擎 + 逐请求 RBAC）、`/api/*` 透明反代零过滤。与
WeKnora-portal-proxy（及本仓的 Python 版）构成同 UI 不同架构的 A/B 对照，另保留
`/sso/native` 进入 WeKnora 原生前端的对照路径。

## 与 Go 原作的关系

逐语义移植：bridge 换票、cookie 会话（落盘 0600 原子写、30 天绝对寿命、临期 5 分钟透明
重 bridge）、`/api/*` 透明反代（Authorization 注入 + SSE 即时 flush）、登录校验
（`idp_sim.employees` 只读 + bcrypt）。UI 模板由 Go html/template 机械转换为 Jinja2，
HTML/CSS/JS 逐字节保留。

| Go 件 | Python 件 |
|---|---|
| `net/http` ServeMux + 内嵌 SSR | FastAPI + Jinja2Templates |
| `httputil.ReverseProxy`（FlushInterval=-1） | httpx 流式转发 + StreamingResponse |
| `pgx/v5/stdlib` 只读查询 | asyncpg（libpq 关键字 DSN 自解析） |
| 会话落盘（0600 原子写） | `app/session.py`（tmp+rename 同语义） |

## 架构

```
浏览器 ──登录──▶ portal-sim(:8082) ──bridge(无tenant_id, platform key)──▶ WeKnora(:8080)
   ◀──302──┘                        （个人空间+grants 全在 WeKnora 落地）
浏览器 ──▶ WeKnora 前端(:5173，真实 JWT 直调，对照路径)
浏览器 ──/api/*──▶ 透明反代（注入会话 Authorization、SSE 即时 Flush、零过滤）
```

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # 填 WEKNORA_PLATFORM_KEY 等
.venv/bin/python -m app --env .env
```

端点与 Go 版一致：`GET /`（登录）、`POST /sso/authorize`、`GET /kb`、`GET /kb/{id}`、
`GET /chat`、`GET /admin`、`GET /sso/native`、`POST /logout`、`/api/*`（反代）、
`GET /healthz`。

## 纪律（与 Go 版同款红线）

1. 本服务**永远不出现权限判断代码**——出现即说明 v2 架构被破坏；分组/角色只是呈现服务端字段
2. platform key 与 JWT 只在服务端，不进任何响应/页面/日志
3. 测试账号与 B1 共用（REVIEW-U0001 / REVIEW-U0002 等，密码 review123456，建库见
   `fixtures/idp_sim.sql`）
