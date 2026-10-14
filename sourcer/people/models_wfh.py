"""Request and response shapes for work from home."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from sourcer.people.models_leave import SkippedHoliday


class WfhRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: str = Field(max_length=10)
    end_date: Optional[str] = Field(default=None, max_length=10)
    day_part: str = Field(default="full", max_length=12)
    reason: Optional[str] = Field(default=None, max_length=300)


class WfhSelfPreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: str = Field(max_length=10)
    end_date: Optional[str] = Field(default=None, max_length=10)
    day_part: str = Field(default="full", max_length=12)


class WfhPreviewIn(WfhSelfPreviewIn):
    employee_id: str = Field(max_length=24)


class WfhRecordIn(BaseModel):
    """HR records work from home directly, without a request."""

    model_config = ConfigDict(extra="forbid")
    employee_id: str = Field(max_length=24)
    start_date: str = Field(max_length=10)
    end_date: Optional[str] = Field(default=None, max_length=10)
    day_part: str = Field(default="full", max_length=12)
    reason: Optional[str] = Field(default=None, max_length=300)
    note: Optional[str] = Field(default=None, max_length=500)
    approved_by_id: Optional[str] = Field(default=None, max_length=24)
    allow_over_limit: StrictBool = False


class WfhCancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Optional[str] = Field(default=None, max_length=300)


class WfhMonthEffect(BaseModel):
    month: str
    label: str
    days: float
    used_before: float
    limit: int  # 0 = no limit
    over: bool


class WfhPreviewOut(BaseModel):
    days: float
    skipped_weekend: int
    skipped_holidays: List[SkippedHoliday]
    months: List[WfhMonthEffect]
    over_limit: bool
    conflict: Optional[str] = None
    enabled: bool = True


class WfhOut(BaseModel):
    id: str
    employee_id: str
    employee_name: Optional[str] = None
    start_date: str
    end_date: str
    day_part: str
    days: float
    reason: Optional[str] = None
    note: Optional[str] = None
    approved_by_name: Optional[str] = None
    over_limit: bool
    status: str
    created_at: str


class WfhList(BaseModel):
    items: List[WfhOut]
    total: int
    page: int
    page_size: int


class WfhMine(BaseModel):
    """An employee's own month: the days used against the limit, and the records."""

    month: str
    label: str
    used: float
    limit: int
    enabled: bool
    items: List[WfhOut]


class WfhPerson(BaseModel):
    employee_id: str
    name: str
    days: float


class WfhDayPerson(BaseModel):
    employee_id: str
    name: str
    part: str


class WfhDay(BaseModel):
    date: str
    people: List[WfhDayPerson]


class WfhSummary(BaseModel):
    month: str
    label: str
    limit: int
    total_days: float
    people: List[WfhPerson]
    days: List[WfhDay]
