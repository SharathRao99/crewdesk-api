"""Request and response shapes for the People module. The web app's TypeScript types are generated from these."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=100)
    work_email: str = Field(max_length=254)
    joining_date: str = Field(max_length=10)
    employee_no: Optional[str] = Field(default=None, max_length=20)
    phone: Optional[str] = Field(default=None, max_length=20)
    designation: Optional[str] = Field(default=None, max_length=80)
    department: Optional[str] = Field(default=None, max_length=80)
    manager_id: Optional[str] = Field(default=None, max_length=24)


class EmployeePatch(BaseModel):
    """Only the fields sent are changed."""

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, max_length=100)
    work_email: Optional[str] = Field(default=None, max_length=254)
    joining_date: Optional[str] = Field(default=None, max_length=10)
    employee_no: Optional[str] = Field(default=None, max_length=20)
    phone: Optional[str] = Field(default=None, max_length=20)
    designation: Optional[str] = Field(default=None, max_length=80)
    department: Optional[str] = Field(default=None, max_length=80)
    manager_id: Optional[str] = Field(default=None, max_length=24)
    status: Optional[str] = Field(default=None, max_length=10)


class ProbationExtensionOut(BaseModel):
    from_date: str
    to_date: str
    reason: str
    at: str


class ProbationOut(BaseModel):
    status: str  # on_probation | review_due | confirmed
    end_date: str
    confirmed_on: Optional[str] = None
    extensions: List[ProbationExtensionOut] = []


class NoticeAdjustmentOut(BaseModel):
    from_date: str
    to_date: str
    reason: str
    requested_by: str
    at: str


class NoticeOut(BaseModel):
    given_on: str
    last_working_day: str
    basis: str  # probation | notice_period
    note: Optional[str] = None
    adjustments: List[NoticeAdjustmentOut] = []


class LoginOut(BaseModel):
    state: str  # none | invited | active | disabled
    last_login: Optional[str] = None


class LoginLinkOut(BaseModel):
    token: str  # shown once: only its hash is kept
    expires_at: str
    email: str
    name: str


class EmployeeOut(BaseModel):
    id: str
    employee_no: str
    name: str
    work_email: str
    phone: Optional[str] = None
    designation: Optional[str] = None
    department: Optional[str] = None
    joining_date: str
    manager_id: Optional[str] = None
    manager_name: Optional[str] = None
    status: str
    has_login: bool
    login: Optional[LoginOut] = None  # filled on the detail page
    probation: ProbationOut
    notice: Optional[NoticeOut] = None
    created_at: str
    updated_at: str


class EmployeeList(BaseModel):
    items: List[EmployeeOut]
    total: int
    page: int
    page_size: int


class EmployeeOption(BaseModel):
    id: str
    name: str
    employee_no: str
