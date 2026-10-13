"""Request and response shapes for employees' requests (leave, and a change to the last working day)."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from sourcer.people.models_leave import YearEffect


class LeaveRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type_id: str = Field(max_length=24)
    start_date: str = Field(max_length=10)
    end_date: Optional[str] = Field(default=None, max_length=10)
    day_part: str = Field(default="full", max_length=12)
    reason: Optional[str] = Field(default=None, max_length=300)


class LeaveRequestPreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type_id: str = Field(max_length=24)
    start_date: str = Field(max_length=10)
    end_date: Optional[str] = Field(default=None, max_length=10)
    day_part: str = Field(default="full", max_length=12)


class NoticeRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    last_working_day: str = Field(max_length=10)
    reason: str = Field(max_length=300)


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_over_balance: StrictBool = False  # HR only: approve a request that goes over the balance
    note: Optional[str] = Field(default=None, max_length=300)


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(max_length=300)  # the employee is owed a reason


class RequestLeaveOut(BaseModel):
    type_id: str
    type_name: str
    start_date: str
    end_date: str
    day_part: str
    days: float


class RequestNoticeOut(BaseModel):
    from_date: str
    to_date: str


class RequestBalanceOut(BaseModel):
    """What approving a pending leave request would do to the balance (worked out now, not when it was asked)."""

    years: List[YearEffect]
    over_balance: bool
    conflict: Optional[str] = None
    unlimited: bool


class RequestOut(BaseModel):
    id: str
    kind: str  # leave | notice
    status: str  # pending | approved | rejected | cancelled
    employee_id: str
    employee_name: Optional[str] = None
    reason: Optional[str] = None
    created_at: str
    leave: Optional[RequestLeaveOut] = None
    notice: Optional[RequestNoticeOut] = None
    balance: Optional[RequestBalanceOut] = None
    decided_by_name: Optional[str] = None
    decided_by_kind: Optional[str] = None  # hr | manager | employee | system
    decided_at: Optional[str] = None
    decision_note: Optional[str] = None
    can_decide: bool = False  # for the person asking: may they decide this one?


class RequestList(BaseModel):
    items: List[RequestOut]
    total: int
    page: int
    page_size: int


class RequestSummary(BaseModel):
    pending: int
    can_approve: bool


class RoutingOut(BaseModel):
    manager_name: Optional[str] = None  # who else (besides HR) can approve this employee's requests
    hr: bool = True
