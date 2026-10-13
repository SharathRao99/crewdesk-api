"""Setting up an employee's login from the link HR gave them. No sign-in: the link is the credential.

They reveal as little as possible: the person's name and the company, and only while the link is open. Every
other outcome (unknown, used, expired, replaced) looks the same.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from sourcer import join, ratelimit
from sourcer.api.deps import require_service_key
from sourcer.api.models import UserOut
from sourcer.api.routes.auth import _user_out
from sourcer.tenancy import registry

router = APIRouter(prefix="/public/join", tags=["public"], dependencies=[Depends(require_service_key)])


class JoinStateOut(BaseModel):
    state: str  # open | invalid
    name: Optional[str] = None
    company: Optional[str] = None


class JoinIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=256)


def _client(request: Request) -> str:
    return request.headers.get("x-client-ip", "unknown")[:64]


def _limit(key: str, limit: int, window: int) -> None:
    if not ratelimit.allow(key, limit, window):
        raise HTTPException(429, "Too many attempts. Please wait a few minutes and try again.")


@router.get("/{token}", response_model=JoinStateOut)
def state(token: str, request: Request) -> JoinStateOut:
    _limit(f"join-get:{_client(request)}", 60, 60)
    who = join.peek(token[:100])
    return JoinStateOut(state="open", name=who["name"], company=who["company"]) if who else JoinStateOut(state="invalid")


@router.post("/{token}", response_model=UserOut)
def set_password(token: str, body: JoinIn, request: Request) -> UserOut:
    ip = _client(request)
    _limit(f"join-post:{ip}", 20, 60)
    _limit(f"join-token:{token[:100]}", 10, 3600)
    try:
        user = join.consume(token[:100], body.password)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if not user:
        registry.log_event("join_failed", "", "", ip)
        raise HTTPException(410, "This link is no longer valid. Ask HR for a new one.")
    registry.log_event("join_ok", user["email"], user["tenant"], ip)
    return _user_out(user, user["tenant"], user["tenant_name"])
