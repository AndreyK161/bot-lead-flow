"""Pure date and exhaustive distribution logic; no network or Telegram imports."""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from math import sqrt
from typing import Mapping, Sequence

PERIODS = (3, 4, 5, 6, 7, 10, 15)


@dataclass(frozen=True)
class ManagerAllocation:
    manager: str
    days: tuple[tuple[date, int], ...]

    @property
    def total(self) -> int:
        return sum(count for _, count in self.days)


@dataclass(frozen=True)
class Distribution:
    allocations: tuple[ManagerAllocation, ...]
    spread: int
    squared_deviation: float
    standard_deviation: float

    @property
    def total(self) -> int:
        return sum(allocation.total for allocation in self.allocations)


def previous_days(days: int, timezone: tzinfo, now: datetime | None = None) -> tuple[date, ...]:
    if days not in PERIODS:
        raise ValueError("Выберите период: 3, 4, 5, 6, 7, 10 или 15 дней.")
    if now is not None and now.tzinfo is None:
        raise ValueError("Текущее время должно содержать часовой пояс.")
    today = (now or datetime.now(timezone)).astimezone(timezone).date()
    return tuple(today - timedelta(days=offset) for offset in range(days, 0, -1))


def parse_manual_date(text: str, today: date) -> date:
    value = text.strip()
    if not re.fullmatch(r"[0-9]{1,2}\.[0-9]{1,2}(\.[0-9]{4})?", value):
        raise ValueError("Введите дату в формате ДД.ММ.ГГГГ или ДД.ММ.")
    parts = [int(part) for part in value.split(".")]
    try:
        chosen = date(parts[2] if len(parts) == 3 else today.year, parts[1], parts[0])
    except ValueError:
        raise ValueError("Такой даты нет в календаре. Введите другую дату.") from None
    if chosen >= today:
        raise ValueError("Сегодняшние и будущие даты не учитываются. Выберите более раннюю дату.")
    return chosen


def best_distributions(
    counts: Mapping[date, int], managers: Sequence[str], limit: int = 6,
) -> list[Distribution]:
    """Exact branch-and-bound search, retaining only a bounded set of optima.

    Empty buckets are interchangeable; considering just one removes permutations
    of manager names. Nonempty buckets remain distinct to retain alternatives.
    SSE and standard deviation have the same ordering for fixed manager count.
    """
    if not 1 <= len(managers) <= 3 or len(set(managers)) != len(managers):
        raise ValueError("Требуется от 1 до 3 менеджеров с разными именами.")
    if limit < 1:
        raise ValueError("Количество вариантов должно быть положительным.")
    if any(type(count) is not int or count < 0 for count in counts.values()):
        raise ValueError("Количество лидов должно быть целым неотрицательным числом.")
    days = tuple(sorted((day, count) for day, count in counts.items() if count > 0))
    if len(days) > 15:
        raise ValueError("Распределять можно не более 15 непустых дат.")
    if not days:
        return []
    size = len(managers)
    total = sum(count for _, count in days)
    order = sorted(range(len(days)), key=lambda i: (-days[i][1], days[i][0]))
    loads = [0] * size
    owners = [0] * len(days)
    optimum = (total + 1, (size * total) ** 2 * size + 1)
    selected: list[tuple[int, ...]] = []

    def visit(depth: int, remaining: int) -> None:
        nonlocal optimum
        if len(selected) == limit and optimum[0] == (0 if total % size == 0 else 1):
            return
        high, low = max(loads), min(loads)
        lower_bound = max(0, high - low - remaining)
        if size > 1:
            lower_bound = max(lower_bound, high - (total - high) // (size - 1))
        if lower_bound > optimum[0]:
            return
        if depth == len(days):
            score = (high - low, sum((size * load - total) ** 2 for load in loads))
            if score < optimum:
                optimum = score
                selected.clear()
            if score == optimum and len(selected) < limit:
                selected.append(tuple(owners))
            return
        index = order[depth]
        count = days[index][1]
        empty_seen = False
        for manager in sorted(range(size), key=lambda m: (loads[m], m)):
            if loads[manager] == 0:
                if empty_seen:
                    continue
                empty_seen = True
            owners[index] = manager
            loads[manager] += count
            visit(depth + 1, remaining - count)
            loads[manager] -= count

    visit(0, total)
    if size > 1 and len(selected) < limit:
        selected.append(tuple((owner + 1) % size for owner in selected[0]))
    sse = optimum[1] / (size * size)
    return [Distribution(
        allocations=tuple(ManagerAllocation(
            manager, tuple(day for day, owner in zip(days, assignment) if owner == index),
        ) for index, manager in enumerate(managers)),
        spread=optimum[0], squared_deviation=sse, standard_deviation=sqrt(sse / size),
    ) for assignment in selected]
