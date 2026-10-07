"""Request and response shapes. The web app's TypeScript types are generated from these."""

from typing import List, Optional

from pydantic import BaseModel, Field


class LoginBody(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    tenant: str
    tenant_name: str
    role: str
    must_change_password: bool


class PasswordBody(BaseModel):
    old: str
    new: str


class CandidateListItem(BaseModel):
    role: str
    login: str
    name: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    score: float
    est_years: Optional[float] = None
    last_active: Optional[str] = None
    status: str
    email: Optional[str] = None
    phone: Optional[str] = None
    linkedin_url: Optional[str] = None
    portfolio_url: Optional[str] = None
    resume_url: Optional[str] = None
    github_url: str
    repos: List[str] = []
    hireable: bool = False


class CandidateList(BaseModel):
    total: int
    items: List[CandidateListItem]


class Gate(BaseModel):
    name: str
    ok: bool
    detail: str


class Rule(BaseModel):
    name: str
    points: float
    max: float
    detail: str
    level: str


class Explanation(BaseModel):
    total: float
    gates: List[Gate]
    rules: List[Rule]


class CandidateDetail(CandidateListItem):
    bio: Optional[str] = None
    followers: int = 0
    public_repos: int = 0
    emails: List[str] = []
    phones: List[str] = []
    resume_links: List[str] = []
    notes: str = ""
    first_seen: Optional[str] = None
    updated_at: Optional[str] = None
    why: Optional[Explanation] = None


class CandidatePatch(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=2000)


class EventOut(BaseModel):
    type: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    note: Optional[str] = None
    by: str
    at: str


class StatusCount(BaseModel):
    status: str
    count: int


class Facets(BaseModel):
    total: int
    by_status: List[StatusCount]


class RoleOut(BaseModel):
    name: str
    languages: List[str]
    keywords: List[str]
    locations: List[str]
    min_years: float
    max_years: float
    default_template_id: Optional[str] = None
    candidates: int = 0


class DefaultTemplateBody(BaseModel):
    template_id: Optional[str] = None


class TemplateIn(BaseModel):
    name: str
    subject: str
    body: str
    role: Optional[str] = None


class TemplateOut(BaseModel):
    id: str
    name: str
    subject: str
    body: str
    role: Optional[str] = None
    version: int
    updated_at: str


class PlaceholderOut(BaseModel):
    name: str
    description: str


class PreviewIn(BaseModel):
    subject: str
    body: str
    role: Optional[str] = None
    login: Optional[str] = None
    sender: str = ""


class PreviewOut(BaseModel):
    subject: str
    body: str
    unknown: List[str]
    sample: bool


class DraftIn(BaseModel):
    template_id: Optional[str] = None
    sender: str = ""


class DraftOut(BaseModel):
    to: Optional[str] = None
    subject: str
    body: str
    template_name: str
    mailto_link: Optional[str] = None
    gmail_link: Optional[str] = None
    outlook_link: Optional[str] = None
