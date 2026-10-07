from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Response

from sourcer.api.deps import Context, get_context, require_service_key
from sourcer.api.models import LoginBody, PasswordBody, UserOut
from sourcer.tenancy import registry

router = APIRouter(tags=["auth"])


@router.post("/auth/login", response_model=UserOut, dependencies=[Depends(require_service_key)])
def login(body: LoginBody) -> UserOut:
    user = registry.verify_login(body.email, body.password)
    if not user:
        raise HTTPException(401, "Invalid email or password")  # the same answer whatever was wrong
    return UserOut(
        id=str(user["_id"]), email=user["email"], tenant=user["tenant"], tenant_name=user["tenant_name"],
        role=user["role"], must_change_password=user.get("must_change_password", False),
    )


@router.get("/me", response_model=UserOut)
def me(ctx: Context = Depends(get_context)) -> UserOut:
    u = ctx.user
    return UserOut(id=str(u["_id"]), email=u["email"], tenant=ctx.tenant, tenant_name=ctx.tenant_name,
                   role=u["role"], must_change_password=u.get("must_change_password", False))


@router.post("/me/password", status_code=204)
def change_password(body: PasswordBody, ctx: Context = Depends(get_context)) -> Response:
    try:
        ok = registry.change_password(ctx.user["email"], body.old, body.new)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if not ok:
        raise HTTPException(400, "Current password is incorrect")
    return Response(status_code=204)
