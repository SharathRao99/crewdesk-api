from typing import List

from fastapi import APIRouter, Depends, HTTPException, Response

from sourcer import roles_store, templates_store
from sourcer.api.deps import Context, get_context
from sourcer.api.models import DefaultTemplateBody, RoleOut
from sourcer.role import role_from_dict

router = APIRouter(prefix="/roles", tags=["roles"])


def _out(ctx: Context, doc) -> RoleOut:
    role = role_from_dict(doc["data"])
    return RoleOut(
        name=role.name, languages=role.languages, keywords=role.keywords, locations=role.locations,
        min_years=role.min_years, max_years=role.max_years, default_template_id=doc.get("default_template_id"),
        candidates=ctx.db["candidates"].count_documents({"role": role.name}),
    )


@router.get("", response_model=List[RoleOut])
def list_roles(ctx: Context = Depends(get_context)) -> List[RoleOut]:
    return [_out(ctx, d) for d in roles_store.list_roles(ctx.db)]


@router.put("/{name}/default-template", response_model=RoleOut)
def set_default_template(name: str, body: DefaultTemplateBody, ctx: Context = Depends(get_context)) -> RoleOut:
    if body.template_id and not templates_store.get(ctx.db, body.template_id):
        raise HTTPException(404, "Template not found")
    if not roles_store.set_default_template(ctx.db, name, body.template_id):
        raise HTTPException(404, "Role not found")
    return _out(ctx, ctx.db["roles"].find_one({"_id": name}))
