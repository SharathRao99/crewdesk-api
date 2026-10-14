"""Request and response shapes for company policy, leave types, holidays, leave records and balances."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class PeriodIO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: int = Field(ge=0, le=365)
    unit: str = Field(max_length=6)  # months | days


class PolicyOut(BaseModel):
    year_start_month: int
    weekend_days: List[int]
    probation_months: int
    notice_period: PeriodIO
    probation_notice_period: PeriodIO
    prorate_new_joiners: bool
    manager_approval: bool = True
    wfh_enabled: bool = True
    wfh_monthly_limit: int = 0
    updated_at: Optional[str] = None


class PolicyPatch(BaseModel):
    """Only the rules sent are changed."""

    model_config = ConfigDict(extra="forbid")
    year_start_month: Optional[int] = Field(default=None, ge=1, le=12)
    weekend_days: Optional[List[int]] = Field(default=None, max_length=7)
    probation_months: Optional[int] = Field(default=None, ge=0, le=24)
    notice_period: Optional[PeriodIO] = None
    probation_notice_period: Optional[PeriodIO] = None
    prorate_new_joiners: Optional[StrictBool] = None
    manager_approval: Optional[StrictBool] = None
    wfh_enabled: Optional[StrictBool] = None
    wfh_monthly_limit: Optional[int] = Field(default=None, ge=0, le=31)


class LeaveTypeOut(BaseModel):
    id: str
    code: str
    name: str
    days_per_year: Optional[float] = None  # null = no limit
    paid: bool
    carry_forward_max: float
    counts_weekends: bool
    active: bool


class LeaveTypeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(max_length=8)
    name: str = Field(max_length=60)
    days_per_year: Optional[float] = Field(default=None, ge=0, le=366)
    paid: StrictBool = True
    carry_forward_max: float = Field(default=0, ge=0, le=366)
    counts_weekends: StrictBool = False


class LeaveTypePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Optional[str] = Field(default=None, max_length=8)
    name: Optional[str] = Field(default=None, max_length=60)
    days_per_year: Optional[float] = Field(default=None, ge=0, le=366)
    paid: Optional[StrictBool] = None
    carry_forward_max: Optional[float] = Field(default=None, ge=0, le=366)
    counts_weekends: Optional[StrictBool] = None
    active: Optional[StrictBool] = None


class HolidayOut(BaseModel):
    id: str
    date: str
    name: str


class HolidayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str = Field(max_length=10)
    name: str = Field(max_length=80)


class HolidayPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: Optional[str] = Field(default=None, max_length=10)
    name: Optional[str] = Field(default=None, max_length=80)


class LeaveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: str = Field(max_length=24)
    type_id: str = Field(max_length=24)
    start_date: str = Field(max_length=10)
    end_date: Optional[str] = Field(default=None, max_length=10)  # same as start_date when omitted
    day_part: str = Field(default="full", max_length=12)
    reason: Optional[str] = Field(default=None, max_length=300)
    note: Optional[str] = Field(default=None, max_length=500)
    approved_by_id: Optional[str] = Field(default=None, max_length=24)
    allow_over_balance: StrictBool = False


class LeavePreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: str = Field(max_length=24)
    type_id: str = Field(max_length=24)
    start_date: str = Field(max_length=10)
    end_date: Optional[str] = Field(default=None, max_length=10)
    day_part: str = Field(default="full", max_length=12)


class SkippedHoliday(BaseModel):
    date: str
    name: str


class YearEffect(BaseModel):
    label: str
    days: float
    remaining_before: Optional[float] = None
    remaining_after: Optional[float] = None


class LeavePreviewOut(BaseModel):
    days: float
    skipped_weekend: int
    skipped_holidays: List[SkippedHoliday]
    years: List[YearEffect]
    over_balance: bool
    conflict: Optional[str] = None
    unlimited: bool


class LeavePatch(BaseModel):
    """Only details that do not change the days. For other changes, cancel and record again."""

    model_config = ConfigDict(extra="forbid")
    reason: Optional[str] = Field(default=None, max_length=300)
    note: Optional[str] = Field(default=None, max_length=500)
    approved_by_id: Optional[str] = Field(default=None, max_length=24)


class LeaveCancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Optional[str] = Field(default=None, max_length=300)


class LeaveOut(BaseModel):
    id: str
    employee_id: str
    employee_name: Optional[str] = None
    type_id: str
    type_code: str
    type_name: str
    start_date: str
    end_date: str
    day_part: str
    days: float
    reason: Optional[str] = None
    note: Optional[str] = None
    approved_by_id: Optional[str] = None
    approved_by_name: Optional[str] = None
    over_balance: bool
    status: str
    created_at: str


class LeaveList(BaseModel):
    items: List[LeaveOut]
    total: int
    page: int
    page_size: int


class BalanceRow(BaseModel):
    type_id: str
    code: str
    name: str
    paid: bool
    active: bool
    entitlement: Optional[float] = None  # null = no limit
    carried_in: float = 0
    adjusted: float = 0
    used: float
    remaining: Optional[float] = None


class BalancesOut(BaseModel):
    year: int
    label: str
    starts: str
    ends: str
    types: List[BalanceRow]


class AdjustmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type_id: str = Field(max_length=24)
    year: int = Field(ge=1970, le=2200)
    days: float = Field(ge=-100, le=100)
    reason: str = Field(max_length=200)


class AdjustmentOut(BaseModel):
    id: str
    type_id: str
    type_name: str
    year: int
    days: float
    reason: str
    created_at: str


class ProbationExtendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_end_date: str = Field(max_length=10)
    reason: str = Field(max_length=300)


class ProbationConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_on: Optional[str] = Field(default=None, max_length=10)  # today when omitted


class NoticeGiveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    given_on: str = Field(max_length=10)
    note: Optional[str] = Field(default=None, max_length=300)


class NoticeAdjustIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    last_working_day: str = Field(max_length=10)
    reason: str = Field(max_length=300)
    requested_by: str = Field(max_length=10)  # employee | company
