"""Configuration loader: real env vars win over the .env file (mirrors Go)."""
import os
from dataclasses import dataclass
from pathlib import Path


def load_env_file(path: str) -> dict:
    vals = {}
    p = Path(path)
    if not p.exists():
        return vals
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        vals[k.strip()] = v.strip()
    return vals


@dataclass
class Config:
    addr: str
    weknora_base: str
    frontend_url: str
    platform_key: str
    db_dsn: str
    tenant_hint: str
    session_file: str

    @classmethod
    def load(cls, env_file: str) -> "Config":
        file_vals = load_env_file(env_file)

        def get(key, default=""):
            if os.environ.get(key, ""):
                return os.environ[key]
            return file_vals.get(key, "") or default

        return cls(
            addr=get("SIM_ADDR", ":8082"),
            weknora_base=get("WEKNORA_BASE_URL", "").rstrip("/"),
            frontend_url=get("WEKNORA_FRONTEND_URL", "").rstrip("/"),
            platform_key=get("WEKNORA_PLATFORM_KEY", ""),
            db_dsn=get("PORTAL_DB_DSN", ""),
            tenant_hint=get("WEKNORA_TENANT_HINT", "1"),
            session_file=get("SIM_SESSION_FILE", ""),
        )

    def validate(self) -> list:
        missing = []
        for key, val in [
            ("WEKNORA_BASE_URL", self.weknora_base),
            ("WEKNORA_PLATFORM_KEY", self.platform_key),
            ("PORTAL_DB_DSN", self.db_dsn),
        ]:
            if not val:
                missing.append(key)
        return missing

    def db_kwargs(self) -> dict:
        """Translate the libpq keyword DSN (host=... password=... dbname=...)
        into asyncpg connect kwargs. asyncpg's own parser chokes on values
        like 'p@ss w0rd!#' that pgx accepts, so we tokenize ourselves."""
        kwargs = {"database": "postgres"}
        for token in self.db_dsn.split():
            if "=" not in token:
                continue
            k, _, v = token.partition("=")
            v = v.strip("'\"")
            if k in ("host", "port", "user", "password"):
                kwargs[k] = v
            elif k == "dbname":
                kwargs["database"] = v
            # sslmode and friends: ignored (dev stack is trust/local)
        if "port" in kwargs:
            kwargs["port"] = int(kwargs["port"])
        return kwargs
