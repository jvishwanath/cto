"""
API auth for the FastAPI surface.

Layer 1 (now): static bearer keys via CTO_API_KEYS — comma-separated
list. When unset, auth is DISABLED (open) so local dev keeps working.
Constant-time compare; keys are hashed in memory so they never appear
in tracebacks/repr.

Layer 2 (later, see compose `oidc` profile): an oauth2-proxy / ALB
sits in front and injects X-Forwarded-User / X-Forwarded-Email. When
CTO_TRUST_FORWARDED_USER=true, those headers are accepted as identity
and the bearer check is skipped — the proxy is the gate.
"""

import hashlib
import hmac
import os

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


def _load_keys() -> set[bytes]:
    raw = os.environ.get("CTO_API_KEYS", "")
    return {
        hashlib.sha256(k.strip().encode()).digest()
        for k in raw.split(",") if k.strip()
    }


_KEY_HASHES = _load_keys()
_TRUST_FWD = os.environ.get(
    "CTO_TRUST_FORWARDED_USER", "false").lower() == "true"

_bearer = HTTPBearer(auto_error=False)


def auth_enabled() -> bool:
    return bool(_KEY_HASHES) or _TRUST_FWD


def _check_key(token: str | None) -> bool:
    if not token or not _KEY_HASHES:
        return False
    h = hashlib.sha256(token.encode()).digest()
    return any(hmac.compare_digest(h, k) for k in _KEY_HASHES)


def require_auth(
    request: Request,
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str | None:
    """FastAPI dependency. Returns the authenticated principal
    (user id from proxy header, or "api-key") or None when auth is
    disabled. Raises 401 otherwise."""
    if _TRUST_FWD:
        user = (request.headers.get("X-Forwarded-User")
                or request.headers.get("X-Forwarded-Email"))
        if user:
            return user
    if _KEY_HASHES:
        if cred and cred.scheme.lower() == "bearer" \
                and _check_key(cred.credentials):
            return "api-key"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if _TRUST_FWD:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Forwarded-User (oauth2-proxy not in front?)",
        )
    return None
