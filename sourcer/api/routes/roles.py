from typing import List

from fastapi import APIRouter, Depends, HTTPException, Response

from sourcer import roles_store, screening, templates_store
from sourcer.api.deps import Context, get_context
from sourcer.api.models import BudgetIn, BudgetOut, DefaultTemplateBody, RoleDefinition, RoleDefinitionOut, RoleOut
from sourcer.role import role_from_dict

router = APIRouter(prefix="/roles", tags=["roles"])


def _out(ctx: Context, doc) -> RoleOut:
    role = role_from_dict(doc["data"])
    return RoleOut(
        name=role.name, languages=role.languages, keywords=role.keywords, locations=role.locations,
        min_years=role.min_years, max_years=role.max_years, default_template_id=doc.get("default_template_id"), budget=BudgetOut(**doc["budget"]) if doc.get("budget") else None, preferred_locations=role.preferred_locations,
        candidates=ctx.db["candidates"].count_documents({"role": role.name}),
    )


@router.get("", response_model=List[RoleOut])
def list_roles(ctx: Context = Depends(get_context)) -> List[RoleOut]:
    return [_out(ctx, d) for d in roles_store.list_roles(ctx.db)]


def _definition(ctx: Context, doc) -> RoleDefinitionOut:
    budget = doc.get("budget")
    return RoleDefinitionOut(**roles_store.data_to_definition(doc["data"]), budget=BudgetOut(**budget) if budget else None,
                             candidates=ctx.db["candidates"].count_documents({"role": doc["_id"]}))


def _apply_budget(ctx: Context, name: str, body: RoleDefinition) -> None:
    """The builder edits the budget too: sending it sets it, sending null removes it, leaving it out keeps it."""
    if "budget" not in body.model_fields_set:
        return
    if body.budget is None:
        roles_store.clear_budget(ctx.db, name)
    else:
        roles_store.set_budget(ctx.db, name, body.budget.min, body.budget.max, body.budget.stretch_max)


@router.post("", response_model=RoleDefinitionOut, status_code=201)
def create_role(body: RoleDefinition, ctx: Context = Depends(get_context)) -> RoleDefinitionOut:
    try:
        if body.budget:  # check it first, so a bad budget cannot leave a half-created role behind
            screening.validate_budget(body.budget.min, body.budget.max, body.budget.stretch_max)
        doc = roles_store.create_role(ctx.db, body.model_dump(exclude={"budget"}))
        _apply_budget(ctx, doc["_id"], body)
    except roles_store.RoleExists:
        raise HTTPException(409, "A role with this name already exists") from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return _definition(ctx, ctx.db["roles"].find_one({"_id": doc["_id"]}))


@router.get("/{name}", response_model=RoleDefinitionOut)
def get_role(name: str, ctx: Context = Depends(get_context)) -> RoleDefinitionOut:
    doc = ctx.db["roles"].find_one({"_id": name})
    if not doc:
        raise HTTPException(404, "Role not found")
    return _definition(ctx, doc)


@router.put("/{name}", response_model=RoleDefinitionOut)
def update_role(name: str, body: RoleDefinition, ctx: Context = Depends(get_context)) -> RoleDefinitionOut:
    """Change a role's rules. The name cannot change. People already found keep their scores until the next run."""
    try:
        doc = roles_store.update_role(ctx.db, name, body.model_dump(exclude={"budget"}))
        if doc:
            _apply_budget(ctx, name, body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if not doc:
        raise HTTPException(404, "Role not found")
    return _definition(ctx, ctx.db["roles"].find_one({"_id": name}))


@router.put("/{name}/default-template", response_model=RoleOut)
def set_default_template(name: str, body: DefaultTemplateBody, ctx: Context = Depends(get_context)) -> RoleOut:
    if body.template_id and not templates_store.get(ctx.db, body.template_id):
        raise HTTPException(404, "Template not found")
    if not roles_store.set_default_template(ctx.db, name, body.template_id):
        raise HTTPException(404, "Role not found")
    return _out(ctx, ctx.db["roles"].find_one({"_id": name}))


@router.put("/{name}/budget", response_model=RoleOut)
def set_budget(name: str, body: BudgetIn, ctx: Context = Depends(get_context)) -> RoleOut:
    """What the company can pay for this role, in lakh per annum. Verdicts on form answers follow it."""
    try:
        found = roles_store.set_budget(ctx.db, name, body.min, body.max, body.stretch_max)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if not found:
        raise HTTPException(404, "Role not found")
    return _out(ctx, ctx.db["roles"].find_one({"_id": name}))


@router.delete("/{name}/budget", response_model=RoleOut)
def clear_budget(name: str, ctx: Context = Depends(get_context)) -> RoleOut:
    if not roles_store.clear_budget(ctx.db, name):
        raise HTTPException(404, "Role not found")
    return _out(ctx, ctx.db["roles"].find_one({"_id": name}))
