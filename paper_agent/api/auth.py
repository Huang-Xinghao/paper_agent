"""Single-owner authentication for the web boundary; no credentials in research files."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .store import connection

router = APIRouter(prefix="/api/auth")
COOKIE = "paper_agent_session"
TTL = 12 * 60 * 60


def enabled():
    return bool(os.environ.get("PAPER_AGENT_PASSWORD_HASH"))


def password_hash(password: str, salt: str | None = None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${digest}"


def initialize():
    with connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS web_sessions (digest TEXT PRIMARY KEY, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS web_login_attempts (created REAL NOT NULL);
        """)


def authenticated(request: Request):
    if not enabled():
        return True
    token = request.cookies.get(COOKIE, "")
    if not token or len(token) > 256:
        return False
    digest = hashlib.sha256(token.encode()).hexdigest()
    with connection() as db:
        return db.execute("SELECT 1 FROM web_sessions WHERE digest=? AND expires>?", (digest, time.time())).fetchone() is not None


class Login(BaseModel):
    username: str = Field(max_length=128)
    password: str = Field(max_length=1024)


@router.get("/session")
def session(request: Request):
    return {"enabled": enabled(), "authenticated": authenticated(request)}


@router.post("/login")
async def login(body: Login, response: Response):
    if not enabled():
        raise HTTPException(404, "登录尚未配置")
    now = time.time()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM web_login_attempts WHERE created<?", (now - 60,))
        if db.execute("SELECT count(*) FROM web_login_attempts").fetchone()[0] >= 10:
            raise HTTPException(429, "尝试过于频繁，请一分钟后重试")
        db.execute("INSERT INTO web_login_attempts VALUES(?)", (now,))
    expected = os.environ["PAPER_AGENT_PASSWORD_HASH"]
    try:
        scheme, salt, _ = expected.split("$")
        valid = scheme == "scrypt" and hmac.compare_digest(await asyncio.to_thread(password_hash, body.password, salt), expected)
    except ValueError:
        valid = False
    if not hmac.compare_digest(body.username.encode(), os.environ.get("PAPER_AGENT_ADMIN_USER", "admin").encode()) or not valid:
        raise HTTPException(401, "用户名或密码错误")
    token = secrets.token_urlsafe(48)
    with connection() as db:
        db.execute("DELETE FROM web_sessions WHERE expires<?", (now,))
        db.execute("INSERT INTO web_sessions VALUES(?,?)", (hashlib.sha256(token.encode()).hexdigest(), now + TTL))
    response.set_cookie(COOKIE, token, max_age=TTL, httponly=True, secure=os.environ.get("PAPER_AGENT_COOKIE_SECURE", "1") != "0", samesite="lax", path="/api")
    return {"authenticated": True}


@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE, "")
    with connection() as db:
        db.execute("DELETE FROM web_sessions WHERE digest=?", (hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie(COOKIE, path="/api")
    return {"authenticated": False}
