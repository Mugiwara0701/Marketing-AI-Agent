"""Verifies the Supabase access token (Bearer JWT) sent by the dashboard."""

import os
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)
ASYMMETRIC_ALGS = ["ES256", "RS256", "EdDSA"]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


def supabase_url() -> str:
    url = (
        os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL") or ""
    ).strip()
    if not url.startswith(("http://", "https://")):
        raise RuntimeError(
            "SUPABASE_URL is not set or is not an http(s) URL. Put it in "
            "eval/dashboardbackend/.env (see .env.example) and restart the server."
        )
    return url.rstrip("/")


@lru_cache(maxsize=1)
def _jwks_client() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"{supabase_url()}/auth/v1/.well-known/jwks.json", cache_keys=True)


def decode_token(token: str) -> dict:
    """Return the verified claims, or raise jwt.PyJWTError."""
    options = {"require": ["exp", "sub"]}
    secret = os.environ.get("SUPABASE_JWT_SECRET")
    alg = jwt.get_unverified_header(token).get("alg")
    if alg == "HS256":
        if not secret:
            raise jwt.InvalidAlgorithmError("HS256 token but SUPABASE_JWT_SECRET is not set")
        return jwt.decode(
            token, secret, algorithms=["HS256"], audience="authenticated", options=options
        )
    key = _jwks_client().get_signing_key_from_jwt(token).key
    return jwt.decode(
        token, key, algorithms=ASYMMETRIC_ALGS, audience="authenticated", options=options
    )


def current_user(creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> dict:
    """FastAPI dependency: 401 unless the request carries a valid Supabase access token."""
    if creds is None:
        raise _unauthorized("Missing bearer token")
    try:
        return decode_token(creds.credentials)
    except jwt.PyJWTError as exc:
        raise _unauthorized("Invalid or expired token") from exc
