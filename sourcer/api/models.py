"""Request and response shapes. The web app's TypeScript types are generated from these."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class AvatarOut(BaseModel):
    style: str
    seed: str


class AvatarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    style: str = Field(max_length=20)
    seed: str = Field(max_length=24)


class UserOut(BaseModel):
    id: str
    email: str
    tenant: str
    tenant_name: str
    role: str
    must_change_password: bool
    modules: List[str] = []
    session_version: int = 0
    avatar: AvatarOut = AvatarOut(style="thumbs", seed="")


class PasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    old: str = Field(max_length=256)
    new: str = Field(max_length=256)


class SessionOut(BaseModel):
    session_version: int


class ScreeningSummary(BaseModel):
    current_ctc: float
    expected_ctc: float
    notice_days: int
    answered_at: str


class BudgetIn(BaseModel):
    min: Optional[float] = None
    max: float
    stretch_max: Optional[float] = None


class BudgetOut(BaseModel):
    min: Optional[float] = None
    max: float
    stretch_max: Optional[float] = None


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
    preferred: bool = False  # in one of the role's preferred places, so listed first
    screening: Optional[ScreeningSummary] = None
    budget_verdict: Optional[str] = None  # within | stretch | over, from the role's current budget


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
    budget: Optional[BudgetOut] = None
    preferred_locations: List[str] = []


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


class ScreeningAnswers(BaseModel):
    current_ctc: float
    expected_ctc: float
    notice_days: int
    comment: Optional[str] = None
    submitted_at: str


class ScreeningOut(BaseModel):
    state: str  # none | pending | answered | expired
    link_url: Optional[str] = None
    expires_at: Optional[str] = None
    answers: Optional[ScreeningAnswers] = None
    verdict: Optional[str] = None
    budget: Optional[BudgetOut] = None


class PublicStateOut(BaseModel):
    state: str  # open | answered | expired | invalid
    first_name: Optional[str] = None
    role_title: Optional[str] = None
    company_name: Optional[str] = None
    consent_version: Optional[str] = None


class PublicAnswersIn(BaseModel):
    current_ctc: float = Field(ge=0, le=500)
    expected_ctc: float = Field(ge=0, le=500)
    notice_days: int = Field(ge=0, le=365)
    comment: Optional[str] = Field(default=None, max_length=500)
    consent: bool
    website: str = ""  # hidden field: people leave it empty, form-filling bots do not


class DeletedOut(BaseModel):
    candidates: int
    events: int
    links: int
    responses: int


class RoleDefinition(BaseModel):
    name: str
    languages: List[str]
    keywords: List[str] = []
    locations: List[str]
    exclude_locations: List[str] = []
    preferred_locations: List[str] = []
    min_years: float = 0
    max_years: float = 50
    min_public_repos: int = 0
    active_within_months: int = 12
    min_language_share: float = 0.0
    min_keyword_repos: int = 0
    manifest_file: Optional[str] = None
    manifest_contains: List[str] = []
    role_title: Optional[str] = None
    budget: Optional[BudgetIn] = None


class RoleDefinitionOut(RoleDefinition):
    budget: Optional[BudgetOut] = None  # type: ignore[assignment]
    candidates: int = 0


class JobCreate(BaseModel):
    role: str
    pages: int = Field(default=2, ge=1, le=5)
    max_candidates: int = Field(default=0, ge=0, le=5000)
    remove_stale: bool = False


class JobOut(BaseModel):
    id: str
    role: str
    status: str  # queued | running | paused | cancelled | failed | done
    control: Optional[str] = None  # pause | cancel: asked for, not yet done
    phase: str
    total: int
    cursor: int
    kept: int
    not_primary: int
    dropped: int
    errors: int
    contacts_total: int
    contacts_done: int
    removed: int
    note: Optional[str] = None
    error: Optional[str] = None
    pages: int
    max_candidates: int
    remove_stale: bool
    created_by: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class JobEventOut(BaseModel):
    level: str
    message: str
    at: str


class JobDetail(JobOut):
    events: List[JobEventOut] = []
