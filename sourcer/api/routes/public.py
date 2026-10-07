"""The candidate-facing screening endpoints. No login: the signed link is the credential.

They reveal as little as possible: a first name, the role title and the company name, and only for
an open link. Every outcome for an unknown, cancelled or tampered link looks the same.
"""

from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from sourcer import ratelimit, screening
from sourcer.api import queries
from sourcer.api.deps import require_service_key
from sourcer.api.models import PublicAnswersIn, PublicStateOut
from sourcer.tenancy import registry

router = APIRouter(prefix="/public/screening", tags=["public"], dependencies=[Depends(require_service_key)])


def _client(request: Request) -> str:
    # Set by the web server from the visitor's address; "unknown" shares one bucket, which is the safe default.
    return request.headers.get("x-client-ip", "unknown")[:64]


def _limit(key: str, limit: int, window: int) -> None:
    if not ratelimit.allow(key, limit, window):
        raise HTTPException(429, "Too many attempts. Please wait a few minutes and try again.")


@router.get("/{token}", response_model=PublicStateOut)
def state(token: str, request: Request) -> PublicStateOut:
    _limit(f"pub-get:{_client(request)}", 60, 60)
    found = screening.resolve(token)
    if not found:
        return PublicStateOut(state="invalid")
    db, slug, link, candidate = found
    company = registry.get_tenant(slug)["name"]
    current = screening.link_state(link)
    if current != "open":
        return PublicStateOut(state=current, company_name=company)  # answered or expired
    if not link.get("opened_at"):
        db["screening_links"].update_one({"_id": link["_id"], "opened_at": None}, {"$set": {"opened_at": screening._now()}})
    name = (candidate.get("name") or candidate["login"]).split()[0]
    return PublicStateOut(
        state="open", first_name=name, company_name=company, role_title=queries.resolve_outreach(db, candidate["role"], None)["role_title"],
        consent_version=screening.CONSENT_VERSION,
    )


@router.post("/{token}", status_code=201)
def submit(token: str, body: PublicAnswersIn, request: Request) -> Dict[str, bool]:
    _limit(f"pub-post:{_client(request)}", 20, 60)
    _limit(f"pub-post-token:{token}", 10, 3600)  # a real person needs one or two tries
    if body.website.strip():
        return {"ok": True}  # the hidden field was filled in: a bot. Pretend it worked and keep nothing.
    if not body.consent:
        raise HTTPException(422, "Please tick the box to agree before sending")
    found = screening.resolve(token)
    if not found:
        raise HTTPException(404, "This link is not valid")
    db, _, link, candidate = found
    answers = {"current_ctc": round(body.current_ctc, 2), "expected_ctc": round(body.expected_ctc, 2),
               "notice_days": body.notice_days, "comment": body.comment}
    try:
        screening.submit(db, link, candidate, answers)
    except screening.SubmitError as e:
        raise HTTPException(410 if e.state == "expired" else 409, e.state) from None
    return {"ok": True}
