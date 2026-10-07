from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Response

from sourcer import roles_store, templates_store
from sourcer.api.deps import Context, get_context
from sourcer.api.models import PlaceholderOut, PreviewIn, PreviewOut, TemplateIn, TemplateOut
from sourcer.outreach import PLACEHOLDERS, candidate_values, render, unknown_placeholders

router = APIRouter(prefix="/templates", tags=["templates"])

SAMPLE = {"name": "Alex Kumar", "login": "alex", "matching_repos": ["laravel-shop"], "location": "Bengaluru, India"}


def _out(doc: Dict[str, Any]) -> TemplateOut:
    return TemplateOut(id=str(doc["_id"]), name=doc["name"], subject=doc["subject"], body=doc["body"],
                       role=doc.get("role"), version=doc["version"], updated_at=doc["updated_at"].isoformat())


def _check(f):
    try:
        return f()
    except templates_store.TemplateError as e:
        raise HTTPException(422, str(e)) from None


@router.get("/placeholders", response_model=List[PlaceholderOut])
def placeholders(ctx: Context = Depends(get_context)) -> List[PlaceholderOut]:
    return [PlaceholderOut(name=n, description=d) for n, d in PLACEHOLDERS.items()]


@router.get("", response_model=List[TemplateOut])
def list_templates(ctx: Context = Depends(get_context), role: str | None = None) -> List[TemplateOut]:
    return [_out(t) for t in templates_store.list_templates(ctx.db, role)]


@router.post("", response_model=TemplateOut, status_code=201)
def create_template(body: TemplateIn, ctx: Context = Depends(get_context)) -> TemplateOut:
    return _out(_check(lambda: templates_store.create(ctx.db, body.name, body.subject, body.body, body.role, ctx.user["email"])))


@router.put("/{template_id}", response_model=TemplateOut)
def update_template(template_id: str, body: TemplateIn, ctx: Context = Depends(get_context)) -> TemplateOut:
    doc = _check(lambda: templates_store.update(ctx.db, template_id, body.name, body.subject, body.body, body.role, ctx.user["email"]))
    if not doc:
        raise HTTPException(404, "Template not found")
    return _out(doc)


@router.delete("/{template_id}", status_code=204)
def delete_template(template_id: str, ctx: Context = Depends(get_context)) -> Response:
    if not templates_store.archive(ctx.db, template_id):
        raise HTTPException(404, "Template not found")
    return Response(status_code=204)


@router.post("/preview", response_model=PreviewOut)
def preview(body: PreviewIn, ctx: Context = Depends(get_context)) -> PreviewOut:
    """Render unsaved text against a real candidate (or a sample) and flag unknown placeholders."""
    row, sample = SAMPLE, True
    if body.role and body.login:
        found = ctx.db["candidates"].find_one({"_id": f"{body.role}:{body.login}"})
        if found:
            row, sample = found, False
    role_title = "Developer"
    if body.role:
        try:
            role, _ = roles_store.get_role(ctx.db, body.role)
            role_title = role.outreach.get("role_title") or role.name
        except KeyError:
            pass
    subject, text = render(body.subject, body.body, candidate_values(row, ctx.tenant_name, role_title, body.sender.strip()))
    return PreviewOut(subject=subject, body=text, unknown=unknown_placeholders(body.subject, body.body), sample=sample)
