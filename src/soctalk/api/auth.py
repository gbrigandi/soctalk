"""Opt-in authentication and authorization helpers.

Auth is disabled by default (AUTH_MODE=none). Enable one of:
  - AUTH_MODE=static: env-defined users + signed session cookie
  - AUTH_MODE=proxy: trust headers from a trusted reverse proxy
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from typing import Literal

import structlog
from fastapi import Depends, HTTPException, Request, Response

logger = structlog.get_logger()

AuthMode = Literal["none", "static", "proxy"]
Role = Literal["admin", "analyst", "viewer"]

SESSION_COOKIE_NAME = "soctalk_session"


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_auth_mode() -> AuthMode:
    mode = (os.getenv("AUTH_MODE") or "").strip().lower()
    if not mode:
        return "none"
    if mode in {"none", "static", "proxy"}:
        return mode  # type: ignore[return-value]
    raise ValueError(f"Unsupported AUTH_MODE: {mode!r}")


def is_auth_enabled() -> bool:
    return get_auth_mode() != "none"


@dataclass(frozen=True)
class UserIdentity:
    username: str
    roles: frozenset[Role]
    source: Literal["static", "proxy"]


@dataclass(frozen=True)
class StaticUserRecord:
    username: str
    password_hash: str
    roles: frozenset[Role]


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_roles(value: str | None) -> frozenset[Role]:
    if not value:
        return frozenset({"viewer"})
    roles: set[Role] = set()
    for part in value.replace(";", "|").split("|"):
        role = part.strip().lower()
        if not role:
            continue
        if role not in {"admin", "analyst", "viewer"}:
            raise ValueError(f"Unsupported role: {role!r}")
        roles.add(role)  # type: ignore[arg-type]
    roles.add("viewer")
    return frozenset(roles)


def parse_static_users() -> dict[str, StaticUserRecord]:
    raw = (os.getenv("AUTH_USERS") or "").strip()
    if not raw:
        return {}

    users: dict[str, StaticUserRecord] = {}
    for entry in _split_csv(raw):
        parts = entry.split(":", 2)
        if len(parts) < 2:
            raise ValueError("AUTH_USERS entries must be username:hash[:roles]")
        username = parts[0].strip()
        password_hash = parts[1].strip()
        roles = _parse_roles(parts[2].strip() if len(parts) == 3 else None)
        if not username:
            raise ValueError("AUTH_USERS username cannot be empty")
        users[username] = StaticUserRecord(username=username, password_hash=password_hash, roles=roles)

    return users


def verify_password(password: str, password_hash: str) -> bool:
    if password_hash.startswith("plain$"):
        expected = password_hash.split("$", 1)[1]
        return hmac.compare_digest(password, expected)

    if password_hash.startswith("pbkdf2_sha256$"):
        parts = password_hash.split("$")
        if len(parts) != 4:
            raise ValueError("Invalid pbkdf2_sha256 hash format")
        _, iter_str, salt_b64, digest_b64 = parts
        iterations = int(iter_str)
        salt = _b64url_decode(salt_b64)
        expected = _b64url_decode(digest_b64)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(derived, expected)

    raise ValueError("Unsupported password hash scheme")


def hash_password_pbkdf2_sha256(password: str, *, iterations: int = 260_000) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64url_encode(salt)}${_b64url_encode(digest)}"


def _get_session_secret() -> bytes:
    secret = os.getenv("AUTH_SESSION_SECRET")
    if secret:
        return secret.encode("utf-8")

    if not hasattr(_get_session_secret, "_generated"):
        setattr(_get_session_secret, "_generated", os.urandom(32))
        logger.warning("auth_session_secret_missing_generated_ephemeral")
    return getattr(_get_session_secret, "_generated")


def _get_session_ttl_seconds() -> int:
    return int(os.getenv("AUTH_SESSION_TTL_SECONDS", "43200"))  # 12h


def _cookie_secure_default() -> bool:
    return _parse_bool(os.getenv("AUTH_COOKIE_SECURE"), False)


def _create_session_token(user: UserIdentity) -> str:
    now = int(time.time())
    payload = {
        "sub": user.username,
        "roles": sorted(user.roles),
        "iat": now,
        "exp": now + _get_session_ttl_seconds(),
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = hmac.new(_get_session_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(sig)}"


def _verify_session_token(token: str) -> UserIdentity | None:
    try:
        body, sig_b64 = token.split(".", 1)
    except ValueError:
        return None

    expected_sig = hmac.new(_get_session_secret(), body.encode("ascii"), hashlib.sha256).digest()
    try:
        provided_sig = _b64url_decode(sig_b64)
    except Exception:
        return None

    if not hmac.compare_digest(expected_sig, provided_sig):
        return None

    try:
        payload = json.loads(_b64url_decode(body))
    except Exception:
        return None

    exp = payload.get("exp")
    sub = payload.get("sub")
    roles = payload.get("roles", [])
    if not isinstance(exp, int) or not isinstance(sub, str):
        return None
    if int(time.time()) >= exp:
        return None

    try:
        parsed_roles: frozenset[Role] = frozenset(_parse_roles("|".join(roles)))  # type: ignore[arg-type]
    except Exception:
        parsed_roles = frozenset({"viewer"})

    return UserIdentity(username=sub, roles=parsed_roles, source="static")


def set_session_cookie(response: Response, user: UserIdentity) -> None:
    token = _create_session_token(user)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure_default(),
        path="/",
        max_age=_get_session_ttl_seconds(),
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")


def _get_trusted_proxy_networks() -> list:
    raw = (os.getenv("AUTH_TRUSTED_PROXY_CIDRS") or "").strip()
    if not raw:
        return []
    networks = []
    for item in _split_csv(raw):
        networks.append(ip_network(item, strict=False))
    return networks


def _is_trusted_proxy(request: Request) -> bool:
    networks = _get_trusted_proxy_networks()
    if not networks:
        return False
    client = request.client
    if client is None:
        return False
    try:
        addr = ip_address(client.host)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def _extract_proxy_user(request: Request) -> UserIdentity | None:
    if not _is_trusted_proxy(request):
        return None

    username = (
        request.headers.get("X-Forwarded-User")
        or request.headers.get("X-Auth-Request-User")
        or request.headers.get("X-Auth-Request-Email")
    )
    if not username:
        return None

    groups_raw = request.headers.get("X-Forwarded-Groups") or request.headers.get("X-Auth-Request-Groups")
    groups = {g.strip() for g in (groups_raw or "").split(",") if g.strip()}

    admin_groups = set(_split_csv(os.getenv("AUTH_PROXY_ADMIN_GROUPS", "admin")))
    analyst_groups = set(_split_csv(os.getenv("AUTH_PROXY_ANALYST_GROUPS", "analyst")))

    roles: set[Role] = {"viewer"}
    if groups & analyst_groups:
        roles.add("analyst")
    if groups & admin_groups:
        roles.update({"analyst", "admin"})

    return UserIdentity(username=username, roles=frozenset(roles), source="proxy")


def get_optional_user(request: Request) -> UserIdentity | None:
    mode = get_auth_mode()
    if mode == "none":
        return None
    if mode == "proxy":
        return _extract_proxy_user(request)

    # static
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return _verify_session_token(token)


def require_authenticated(request: Request) -> UserIdentity | None:
    if not is_auth_enabled():
        return None
    user = get_optional_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_role(required: Role):
    def dep(user: UserIdentity | None = Depends(require_authenticated)) -> UserIdentity | None:
        if user is None:
            return None
        if required not in user.roles:
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return dep


require_analyst = require_role("analyst")
require_admin = require_role("admin")

