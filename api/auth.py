"""Auth — signup / signin per store, returns token for merchant console.

Keeps DEMO_KEY as bootstrap fallback: if merchants table empty, DEMO_KEY still works.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from store import auth as auth_store

router = APIRouter(prefix="/auth", tags=["auth"])


class Signup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(description="merchant email")
    password: str = Field(min_length=6, description=">=6 chars")
    store_id: str = Field(default="default", description="store slug")


class Signin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    password: str
    store_id: str = Field(default="default")


@router.post("/signup", summary="Create merchant account for a store")
def signup(body: Signup):
    if auth_store.get_by_email(body.email, body.store_id):
        raise HTTPException(status_code=409, detail={"code": "already_exists", "message": "email already registered for this store"})
    try:
        row = auth_store.create_merchant(email=body.email, password=body.password, store_id=body.store_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"code": "bad_request", "message": str(e)}) from e
    return {"access_token": row["active_token"], "store_id": row["store_id"], "email": row["email"], "merchant_id": row["merchant_id"]}


@router.post("/signin", summary="Sign in, get token")
def signin(body: Signin):
    row = auth_store.verify_password(body.email, body.password, body.store_id)
    if not row:
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "wrong email, password or store"})
    # rotate token on each signin for freshness
    token = auth_store.rotate_token(row["merchant_id"])
    return {"access_token": token, "store_id": row["store_id"], "email": row["email"], "merchant_id": row["merchant_id"]}


@router.post("/signout", summary="Invalidate current token")
def signout(authorization: str | None = Header(default=None, alias="Authorization")):
    if not authorization or not authorization.startswith("Bearer "):
        return {"status": "ok", "message": "no token to invalidate"}
    token = authorization.removeprefix("Bearer ").strip()
    try:
        from store.db import get_connection, transaction
        conn = get_connection()
        with transaction(conn):
            conn.execute("UPDATE merchants SET active_token=NULL WHERE active_token=?", (token,))
    except Exception:
        pass
    return {"status": "ok"}


@router.get("/me", summary="Who am I")
def me(authorization: str | None = Header(default=None, alias="Authorization")):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Bearer token required"})
    token = authorization.removeprefix("Bearer ").strip()
    row = auth_store.get_by_token(token)
    if not row:
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "invalid token"})
    return {"email": row["email"], "store_id": row["store_id"], "merchant_id": row["merchant_id"]}
