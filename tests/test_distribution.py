import unittest
from datetime import date, datetime, timezone
from itertools import product
from zoneinfo import ZoneInfo

from app.distribution import PERIODS, best_distributions, parse_manual_date, previous_days


class DistributionTests(unittest.TestCase):
    def check_case(self, values: list[int], manager_count: int) -> None:
        counts = {date(2026, 9, i + 1): value for i, value in enumerate(values)}
        managers = ["Иван", "Анна", "Сергей"][:manager_count]
        results = best_distributions(counts, managers)
        scores = []
        for owners in product(range(manager_count), repeat=len(values)):
            loads = [sum(value for value, owner in zip(values, owners) if owner == i)
                     for i in range(manager_count)]
            scores.append((max(loads) - min(loads), sum(load * load for load in loads)))
        optimum = min(scores)
        self.assertTrue(results)
        self.assertEqual(results, best_distributions(dict(reversed(list(counts.items()))), managers))
        self.assertEqual(len(results), len(set(results)))
        self.assertLessEqual(len(results), 6)
        for result in results:
            loads = [allocation.total for allocation in result.allocations]
            self.assertEqual((result.spread, sum(load * load for load in loads)), optimum)
            self.assertEqual(result.total, sum(values))
            assigned = [day for allocation in result.allocations for day in allocation.days]
            self.assertEqual(sorted(assigned), sorted((d, n) for d, n in counts.items() if n))
            self.assertEqual([a.manager for a in result.allocations], managers)

    def test_required_scenarios(self):
        for values, managers in [
            ([20, 30, 40], 3), ([10, 17, 25, 40, 53, 62, 83], 3),
            ([10, 20, 30, 40, 50], 2), ([1, 5, 8], 1),
            ([1, 1, 1, 1000, 2, 3, 4], 3), ([20] * 7, 3), ([50], 3),
            ([10, 10, 20, 20], 2), ([0, 4, 5], 3),
        ]:
            with self.subTest(values=values, managers=managers):
                self.check_case(values, managers)

    def test_empty(self):
        self.assertEqual(best_distributions({}, ["Иван"]), [])
        self.assertEqual(best_distributions({date(2026, 1, 1): 0}, ["Иван"]), [])

    def test_alternatives_and_permutations(self):
        counts = {date(2026, 9, i): 10 for i in range(1, 4)}
        results = best_distributions(counts, ["Иван", "Анна", "Сергей"])
        self.assertEqual(len(results), 2)
        self.assertNotEqual(results[0].allocations, results[1].allocations)
        self.assertEqual(len(best_distributions(counts, ["Иван"])), 1)
        self.assertEqual(len(best_distributions(counts, ["Иван", "Анна"], limit=1)), 1)

    def test_invalid_input(self):
        for managers in [[], ["a"] * 2, list("abcd")]:
            with self.assertRaises(ValueError):
                best_distributions({}, managers)
        for value in [-1, 0.5, True]:
            with self.assertRaises(ValueError):
                best_distributions({date(2026, 1, 1): value}, ["a"])
        with self.assertRaises(ValueError):
            best_distributions({date(2026, 1, i): 1 for i in range(1, 17)}, ["a"])

    def test_large_periods(self):
        self.check_case([3, 7, 11, 13, 17, 19, 23, 29, 31, 37], 3)
        self.check_case(list(range(1, 16)), 2)
        for values in ([10] * 15, list(range(1, 16)), [1000] + [1] * 14):
            counts = {date(2026, 9, i + 1): value for i, value in enumerate(values)}
            results = best_distributions(counts, ["a", "b", "c"])
            expected_spread = 993 if values[0] == 1000 else 0
            self.assertEqual(results[0].spread, expected_spread)
            self.assertLessEqual(len(results), 6)
            self.assertEqual(results, best_distributions(counts, ["a", "b", "c"]))
            for result in results:
                self.assertEqual(result.total, sum(values))
                self.assertEqual(sum(len(a.days) for a in result.allocations), 15)


class DateTests(unittest.TestCase):
    def test_moscow_midnight_and_year_boundary(self):
        zone = ZoneInfo("Europe/Moscow")
        now = datetime(2025, 12, 31, 21, 5, tzinfo=timezone.utc)
        self.assertEqual(previous_days(3, zone, now),
                         (date(2025, 12, 29), date(2025, 12, 30), date(2025, 12, 31)))

    def test_all_periods_and_leap_day(self):
        zone = ZoneInfo("Europe/Moscow")
        for length in PERIODS:
            days = previous_days(length, zone, datetime(2026, 9, 21, tzinfo=zone))
            self.assertEqual(len(days), length)
            self.assertEqual(days[0], date(2026, 9, 21 - length))
            self.assertEqual(days[-1], date(2026, 9, 20))
        self.assertEqual(previous_days(3, zone, datetime(2024, 3, 1, tzinfo=zone))[-1], date(2024, 2, 29))

    def test_manual_date(self):
        today = date(2026, 9, 21)
        self.assertEqual(parse_manual_date("15.09", today), date(2026, 9, 15))
        self.assertEqual(parse_manual_date("31.12.2025", today), date(2025, 12, 31))
        for text in ["21.09", "22.09", "31.02", "15/09", "29.02.2025", "01.01.2027"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_manual_date(text, today)
