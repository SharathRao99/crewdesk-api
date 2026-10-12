from fastapi import APIRouter, Depends, HTTPException, Request

from sourcer import modules, ratelimit
from sourcer.api.deps import Context, get_context, require_service_key
from sourcer.api.models import LoginBody, PasswordBody, SessionOut, UserOut
from sourcer.tenancy import registry

router = APIRouter(tags=["auth"])

LOGIN_PER_IP = (30, 300)  # failed attempts, seconds: on top of the per-account lockout. Only failures count, so real sign-ins never use it up.


def _client(request: Request) -> str:
    # Set by the web server from the visitor's address; "unknown" shares one bucket, which is the safe default.
    return request.headers.get("x-client-ip", "unknown")[:64]


def _user_out(user: dict, tenant: str, tenant_name: str) -> UserOut:
    return UserOut(
        id=str(user["_id"]), email=user["email"], tenant=tenant, tenant_name=tenant_name, role=user["role"],
        must_change_password=user.get("must_change_password", False),
        modules=modules.modules_for(user), session_version=int(user.get("session_version", 0)),
    )


@router.post("/auth/login", response_model=UserOut, dependencies=[Depends(require_service_key)])
def login(body: LoginBody, request: Request) -> UserOut:
    ip = _client(request)
    if ratelimit.over(f"login:{ip}", *LOGIN_PER_IP):
        registry.log_event("login_throttled", body.email, ip=ip)
        raise HTTPException(429, "Too many sign-in attempts. Please wait a few minutes and try again.")
    user = registry.verify_login(body.email, body.password)
    if not user:
        ratelimit.allow(f"login:{ip}", *LOGIN_PER_IP)  # count the failure
        registry.log_event("login_failed", body.email, ip=ip)
        raise HTTPException(401, "Invalid email or password")  # the same answer whatever was wrong
    registry.log_event("login_ok", user["email"], user["tenant"], ip)
    return _user_out(user, user["tenant"], user["tenant_name"])


@router.get("/me", response_model=UserOut)
def me(ctx: Context = Depends(get_context)) -> UserOut:
    return _user_out(ctx.user, ctx.tenant, ctx.tenant_name)


@router.post("/me/password", response_model=SessionOut)
def change_password(body: PasswordBody, ctx: Context = Depends(get_context)) -> SessionOut:
    try:
        ok = registry.change_password(ctx.user["email"], body.old, body.new)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if not ok:
        raise HTTPException(400, "Current password is incorrect")
    registry.log_event("password_changed", ctx.user["email"], ctx.tenant)
    # Older sessions end (the version moved on); the caller keeps theirs by taking the new version.
    return SessionOut(session_version=registry.session_version_of(ctx.user["_id"]) or 0)
