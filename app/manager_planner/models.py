from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Id = Annotated[int, Field(strict=True, gt=0, lt=2**63)]
Minutes = Annotated[int, Field(strict=True, ge=0, le=1440)]
Quantity = Annotated[float, Field(ge=0, le=1_000_000_000, allow_inf_nan=False)]
Title = Annotated[str, Field(min_length=1, max_length=180)]
Text = Annotated[str, Field(max_length=4000)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EmployeeInput(Input):
    name: Annotated[str, Field(min_length=1, max_length=80)]
    telegram_id: Id | None = None
    bitrix_id: Id | None = None


class EmployeePatch(EmployeeInput):
    active: bool = True


class PlanInput(Input):
    day: date
    employee_id: Id | None = None
    from_previous: bool = False


class TaskInput(Input):
    title: Title
    description: Text = ""
    kind: Literal["quantitative", "general", "client"] = "general"
    planned_minutes: Minutes = 0
    target: Quantity | None = None

    @model_validator(mode="after")
    def check_target(self):
        if self.kind == "quantitative" and self.target is None:
            raise ValueError("Укажите количественную цель.")
        if self.kind != "quantitative" and self.target is not None:
            raise ValueError("Количественная цель доступна для количественной задачи.")
        return self


class TaskPatch(Input):
    revision: Annotated[int, Field(strict=True, gt=0)]
    title: Title | None = None
    description: Text | None = None
    planned_minutes: Minutes | None = None
    target: Quantity | None = None
    actual_value: Quantity | None = None
    actual_minutes: Annotated[int, Field(strict=True, ge=0, le=525600)] | None = None
    result: Text | None = None
    comment: Text | None = None
    status: Literal["todo", "in_progress", "done"] | None = None

    @model_validator(mode="after")
    def no_explicit_null(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Переданное поле не может быть null.")
        return self


class TimerInput(Input):
    action: Literal["start", "pause", "stop"]


class MoveInput(Input):
    day: date
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    revision: Annotated[int, Field(strict=True, gt=0)]


class GoalInput(Input):
    field: Literal["target", "planned_minutes"] = "target"
    proposed: Quantity
    reason: Annotated[str, Field(min_length=1, max_length=2000)]

    @model_validator(mode="after")
    def time_limit(self):
        if self.field == "planned_minutes" and (self.proposed > 1440 or not self.proposed.is_integer()):
            raise ValueError("Плановое время: целое количество минут от 0 до 1440.")
        return self


class DecisionInput(Input):
    decision: Literal["approved", "rejected"]
    comment: Annotated[str, Field(max_length=2000)] = ""
