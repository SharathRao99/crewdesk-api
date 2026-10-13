from fastapi import APIRouter, Depends, HTTPException, Request

from sourcer import avatars, modules, ratelimit
from sourcer.api.deps import Context, get_context, require_service_key
from sourcer.api.models import AvatarIn, AvatarOut, LoginBody, PasswordBody, SessionOut, UserOut
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
        avatar=AvatarOut(**avatars.of(user)),
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
    out = _user_out(ctx.user, ctx.tenant, ctx.tenant_name)
    out.can_approve = _can_approve(ctx)
    return out


def _can_approve(ctx: Context) -> bool:
    """HR can; a reporting manager can while the company allows manager approval."""
    from sourcer.people import policy as company_policy
    from sourcer.people import requests as people_requests
    from sourcer.people import store

    if ctx.user.get("role") == "admin":
        return True
    employee_id = ctx.user.get("employee_id")
    if ctx.user.get("role") != "employee" or not employee_id or "people" not in modules.modules_for(ctx.user):
        return False
    if not company_policy.get_policy(ctx.db).get("manager_approval", True):
        return False
    try:
        emp = store.get(ctx.db, employee_id)
    except store.NotFound:
        return False
    return emp.get("status") == "active" and people_requests.is_manager(ctx.db, emp["_id"])


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


@router.put("/me/avatar", response_model=AvatarOut)
def set_avatar(body: AvatarIn, ctx: Context = Depends(get_context)) -> AvatarOut:
    try:
        choice = avatars.validate(body.style, body.seed)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    registry.set_avatar(ctx.user["_id"], choice)
    registry.log_event("avatar_changed", ctx.user["email"], ctx.tenant)
    return AvatarOut(**choice)
