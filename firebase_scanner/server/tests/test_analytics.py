"""Tests for the deterministic analytics layer.

These guard the numbers the AI is not allowed to compute: if run_query or
forecast drifts, the natural-language answers silently become wrong.
"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analytics


def _order(order_no, date, series, product, plan_total, lines, status="approved"):
    return {
        "id": order_no, "order_no": order_no, "document_date": date,
        "series_no": series, "product_name": product,
        "plan_total": plan_total, "actual_total": plan_total,
        "plan_unit": "KG", "status": status,
        "lines": [{"row_no": i + 1, "item_no": n, "item_description": d,
                   "type": "Item", "quantity": q, "plan": p,
                   "whse": "P8-RM01", "unit": "KG"}
                  for i, (n, d, q, p) in enumerate(lines)],
    }


def _dataset():
    """Two months, one product, two materials, tidy round numbers."""
    out = []
    for i in range(4):
        out.append(_order(f"J{i}", "2026-06-10", "S1", "ขนมปัง", 100.0,
                          [("M1", "แป้งสาลี", 30.0, 30.0),
                           ("M2", "น้ำตาลทราย", 10.0, 8.0)]))
    for i in range(4):
        out.append(_order(f"K{i}", "2026-07-10", "S1", "ขนมปัง", 100.0,
                          [("M1", "แป้งสาลี", 30.0, 30.0),
                           ("M2", "น้ำตาลทราย", 12.0, 8.0)]))
    return out


class TestNormalisation(unittest.TestCase):
    def test_norm_strips_spaces_and_case(self):
        self.assertEqual(analytics._norm("  น้ำตาล ทราย "), analytics._norm("น้ำตาลทราย"))

    def test_num_handles_junk(self):
        self.assertEqual(analytics._num("abc"), 0.0)
        self.assertEqual(analytics._num(None), 0.0)
        self.assertEqual(analytics._num("12.5"), 12.5)

    def test_month_rejects_bad_dates(self):
        self.assertIsNone(analytics._month({"document_date": "ไม่ทราบ"}))
        self.assertEqual(analytics._month({"document_date": "2026-07-10"}), "2026-07")


class TestMatching(unittest.TestCase):
    def setUp(self):
        self.materials = {"M1": "แป้งสาลีอเนกประสงค์", "M2": "น้ำตาลทรายขาว"}

    def test_no_term_means_no_filter(self):
        self.assertIsNone(analytics.match_keys("", self.materials))
        self.assertIsNone(analytics.match_keys(None, self.materials))

    def test_substring_match(self):
        self.assertEqual(analytics.match_keys("แป้งสาลี", self.materials), {"M1"})

    def test_match_ignores_spacing(self):
        self.assertEqual(analytics.match_keys("น้ำตาล ทราย", self.materials), {"M2"})

    def test_match_by_code(self):
        self.assertEqual(analytics.match_keys("M2", self.materials), {"M2"})

    def test_unknown_term_returns_empty_not_none(self):
        # Empty set means "found nothing"; None would mean "no filter" and
        # would wrongly report the total for every material.
        self.assertEqual(analytics.match_keys("ทองคำแท่ง", self.materials), set())


class TestRunQuery(unittest.TestCase):
    def setUp(self):
        self.data = _dataset()

    def test_sum_one_material_in_one_month(self):
        r = analytics.run_query({"metric": "quantity", "group_by": "none",
                                 "filters": {"material": "น้ำตาลทราย",
                                             "date_from": "2026-07-01",
                                             "date_to": "2026-07-31"}}, self.data)
        self.assertAlmostEqual(r["total"], 48.0)
        self.assertEqual(r["n_orders"], 4)

    def test_group_by_material(self):
        r = analytics.run_query({"metric": "quantity", "group_by": "material",
                                 "filters": {}}, self.data)
        by_key = {row["key"]: row["value"] for row in r["rows"]}
        self.assertAlmostEqual(by_key["M1"], 240.0)
        self.assertAlmostEqual(by_key["M2"], 88.0)

    def test_group_by_month_is_chronological(self):
        r = analytics.run_query({"metric": "quantity", "group_by": "month",
                                 "filters": {"material": "แป้งสาลี"}}, self.data)
        self.assertEqual([row["key"] for row in r["rows"]], ["2026-06", "2026-07"])

    def test_variance_metric(self):
        r = analytics.run_query({"metric": "variance", "group_by": "none",
                                 "filters": {"material": "น้ำตาลทราย"}}, self.data)
        # June 4x(10-8) + July 4x(12-8)
        self.assertAlmostEqual(r["total"], 24.0)

    def test_order_count_metric(self):
        r = analytics.run_query({"metric": "order_count", "group_by": "none",
                                 "filters": {}}, self.data)
        self.assertEqual(r["total"], 8)

    def test_production_metric(self):
        r = analytics.run_query({"metric": "production", "group_by": "none",
                                 "filters": {}}, self.data)
        self.assertAlmostEqual(r["total"], 800.0)

    def test_status_filter(self):
        data = self.data + [_order("D1", "2026-07-11", "S1", "ขนมปัง", 100.0,
                                   [("M1", "แป้งสาลี", 30.0, 30.0)], status="draft")]
        r = analytics.run_query({"metric": "order_count", "group_by": "none",
                                 "filters": {"status": "draft"}}, data)
        self.assertEqual(r["total"], 1)

    def test_unknown_material_reports_not_found(self):
        r = analytics.run_query({"metric": "quantity", "group_by": "none",
                                 "filters": {"material": "ทองคำแท่ง"}}, self.data)
        self.assertEqual(r["rows"], [])
        self.assertTrue(r["not_found"])

    def test_bad_metric_falls_back_to_quantity(self):
        r = analytics.run_query({"metric": "หมุนขวา", "group_by": "ดวงจันทร์",
                                 "filters": {}}, self.data)
        self.assertAlmostEqual(r["total"], 328.0)


class TestOutlierGuard(unittest.TestCase):
    def test_ocr_misread_is_excluded(self):
        data = [_order(f"N{i}", "2026-07-10", "S1", "ขนมปัง", 100.0,
                       [("M1", "แป้งสาลี", 30.0, 30.0)]) for i in range(12)]
        data.append(_order("BAD", "2026-07-20", "S1", "ขนมปัง", 100000.0,
                           [("M1", "แป้งสาลี", 30000.0, 30000.0)]))
        r = analytics.run_query({"metric": "quantity", "group_by": "none",
                                 "filters": {"material": "แป้งสาลี"}}, data)
        self.assertAlmostEqual(r["total"], 360.0)
        self.assertEqual(r["excluded_outliers"], 1)

    def test_normal_variation_is_not_excluded(self):
        data = [_order(f"N{i}", "2026-07-10", "S1", "ขนมปัง", 100.0 + i * 20,
                       [("M1", "แป้งสาลี", 30.0, 30.0)]) for i in range(12)]
        kept, dropped = analytics.clean_orders(data)
        self.assertEqual(len(kept), 12)
        self.assertEqual(dropped, [])

    def test_small_datasets_are_left_alone(self):
        # Below the sample threshold there is no reliable median to judge against.
        kept, dropped = analytics.clean_orders(
            [_order("A", "2026-07-01", "S1", "x", 100.0, [("M1", "m", 1.0, 1.0)])])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])


class TestComparison(unittest.TestCase):
    def test_period_over_period_delta(self):
        r = analytics.run_with_comparison({
            "metric": "quantity", "group_by": "none",
            "filters": {"material": "น้ำตาลทราย",
                        "date_from": "2026-07-01", "date_to": "2026-07-31"},
            "compare_to": {"date_from": "2026-06-01", "date_to": "2026-06-30"},
        }, _dataset())
        self.assertAlmostEqual(r["total"], 48.0)
        self.assertAlmostEqual(r["comparison"]["total"], 40.0)
        self.assertAlmostEqual(r["comparison"]["delta"], 8.0)
        self.assertAlmostEqual(r["comparison"]["delta_pct"], 20.0)

    def test_zero_baseline_gives_no_percentage(self):
        data = _dataset()
        r = analytics.run_with_comparison({
            "metric": "quantity", "group_by": "none",
            "filters": {"material": "น้ำตาลทราย",
                        "date_from": "2026-07-01", "date_to": "2026-07-31"},
            "compare_to": {"date_from": "2026-01-01", "date_to": "2026-01-31"},
        }, data)
        self.assertIsNone(r["comparison"]["delta_pct"])


class TestImpliedBom(unittest.TestCase):
    def test_ratio_is_recovered(self):
        bom = analytics.implied_bom(_dataset())
        lines = {ln["item_no"]: ln for ln in bom["S1"]["lines"]}
        self.assertAlmostEqual(lines["M1"]["ratio"], 0.30, places=4)
        self.assertEqual(lines["M1"]["cv"], 0.0)

    def test_rare_materials_are_skipped(self):
        data = _dataset()
        data[0]["lines"].append({"row_no": 9, "item_no": "M9",
                                 "item_description": "ของหายาก", "type": "Item",
                                 "quantity": 5.0, "plan": 5.0,
                                 "whse": "P8-RM01", "unit": "KG"})
        bom = analytics.implied_bom(data, min_orders=3)
        self.assertNotIn("M9", {ln["item_no"] for ln in bom["S1"]["lines"]})


class TestForecast(unittest.TestCase):
    def test_flat_history_forecasts_flat(self):
        f = analytics.forecast(_dataset(), today=dt.date(2026, 8, 4))
        self.assertTrue(f["ready"])
        self.assertEqual(f["target_months"], ["2026-08"])
        self.assertAlmostEqual(f["production_total_forecast"], 400.0, places=1)

    def test_material_forecast_follows_the_recipe(self):
        f = analytics.forecast(_dataset(), today=dt.date(2026, 8, 4))
        rows = {m["item_no"]: m for m in f["materials"]}
        # 400 KG of production at a 0.30 recipe ratio
        self.assertAlmostEqual(rows["M1"]["forecast"], 120.0, places=1)
        self.assertLessEqual(rows["M1"]["low"], rows["M1"]["forecast"])
        self.assertGreaterEqual(rows["M1"]["high"], rows["M1"]["forecast"])

    def test_current_partial_month_is_excluded(self):
        data = _dataset() + [_order("AUG", "2026-08-02", "S1", "ขนมปัง", 100.0,
                                    [("M1", "แป้งสาลี", 30.0, 30.0)])]
        f = analytics.forecast(data, today=dt.date(2026, 8, 4))
        self.assertNotIn("2026-08", f["months_used"])

    def test_no_data_is_reported_not_guessed(self):
        f = analytics.forecast([], today=dt.date(2026, 8, 4))
        self.assertFalse(f["ready"])
        self.assertIn("reason", f)

    def test_multi_month_horizon_scales_the_total(self):
        # Three months of flat demand must forecast three months' worth, not one.
        one = analytics.forecast(_dataset(), months=1, today=dt.date(2026, 8, 4))
        three = analytics.forecast(_dataset(), months=3, today=dt.date(2026, 8, 4))
        self.assertEqual(three["target_months"], ["2026-08", "2026-09", "2026-10"])
        self.assertAlmostEqual(three["production_total_forecast"],
                               one["production_total_forecast"] * 3, places=1)
        m1 = {m["item_no"]: m for m in one["materials"]}["M1"]
        m3 = {m["item_no"]: m for m in three["materials"]}["M1"]
        self.assertAlmostEqual(m3["forecast"], m1["forecast"] * 3, places=1)

    def test_multi_month_baseline_is_comparable(self):
        # vs_avg_pct must compare like with like, or a 3-month horizon would
        # always look like a 200% surge against a one-month average.
        f = analytics.forecast(_dataset(), months=3, today=dt.date(2026, 8, 4))
        m = {x["item_no"]: x for x in f["materials"]}["M1"]
        self.assertAlmostEqual(m["baseline"], m["avg_monthly"] * 3, places=1)
        self.assertLess(abs(m["vs_avg_pct"]), 5.0)

    def test_material_reports_which_products_use_it(self):
        f = analytics.forecast(_dataset(), today=dt.date(2026, 8, 4))
        m = {x["item_no"]: x for x in f["materials"]}["M1"]
        self.assertIn("S1", m["used_in"])
        self.assertEqual(f["products_index"]["S1"], "ขนมปัง")

    def test_growing_history_forecasts_upward(self):
        data = []
        for idx, (month, volume) in enumerate(
                [("2026-04", 100.0), ("2026-05", 120.0), ("2026-06", 140.0),
                 ("2026-07", 160.0)]):
            data.append(_order(f"G{idx}", f"{month}-10", "S1", "ขนมปัง", volume,
                               [("M1", "แป้งสาลี", volume * 0.3, volume * 0.3)]))
        f = analytics.forecast(data, today=dt.date(2026, 8, 4))
        self.assertGreater(f["production_total_forecast"], 160.0)


if __name__ == "__main__":
    unittest.main()


def _health_dataset():
    """Six months, two products, a declining yield on one of them."""
    out = []
    yields = {"2026-02": 1.00, "2026-03": 1.00, "2026-04": 0.99,
              "2026-05": 0.97, "2026-06": 0.95, "2026-07": 0.93}
    for i, (month, ratio) in enumerate(yields.items()):
        for k in range(3):
            o = _order(f"A{i}{k}", f"{month}-1{k}", "S1", "ขนมปัง", 100.0,
                       [("M1", "แป้งสาลี", 33.0, 30.0),
                        ("R1", "แรงงานฝ่ายผลิต", 0.9, 1.0)])
            o["actual_total"] = 100.0 * ratio
            o["lines"][1]["type"] = "Resource"
            o["lines"][1]["unit"] = "Hour"
            o["lines"][0]["whse"] = "P8-RM01"
            o["lines"][1]["whse"] = "P8-PD05"
            o["scanned_at"] = f"{month}-1{k}T09:00:00"
            out.append(o)
        o = _order(f"B{i}", f"{month}-20", "S2", "แซนวิช", 50.0,
                   [("M1", "แป้งสาลี", 15.0, 15.0)])
        o["actual_total"] = 50.0
        o["scanned_at"] = f"{month}-20T09:00:00"
        out.append(o)
    return out


class TestYieldTrend(unittest.TestCase):
    def test_declining_product_is_flagged(self):
        y = analytics.yield_trend(_health_dataset(), today=dt.date(2026, 8, 4))
        rows = {r["series_no"]: r for r in y["products"]}
        self.assertTrue(rows["S1"]["declining"])
        self.assertFalse(rows["S2"]["declining"])
        self.assertEqual(y["alert_count"], 1)

    def test_worst_product_is_listed_first(self):
        y = analytics.yield_trend(_health_dataset(), today=dt.date(2026, 8, 4))
        self.assertEqual(y["products"][0]["series_no"], "S1")

    def test_stable_product_reports_full_yield(self):
        y = analytics.yield_trend(_health_dataset(), today=dt.date(2026, 8, 4))
        rows = {r["series_no"]: r for r in y["products"]}
        self.assertAlmostEqual(rows["S2"]["latest"], 100.0, places=1)

    def test_orders_without_actual_are_skipped(self):
        data = _health_dataset()
        for o in data:
            o["actual_total"] = None
        y = analytics.yield_trend(data, today=dt.date(2026, 8, 4))
        self.assertFalse(y["ready"])


class TestPlanVariance(unittest.TestCase):
    def test_median_over_issue(self):
        v = analytics.plan_variance(_health_dataset())
        rows = {r["item_no"]: r for r in v["materials"]}
        self.assertAlmostEqual(rows["M1"]["over_pct"], 10.0, places=1)   # 33 vs 30

    def test_under_issue_is_negative(self):
        v = analytics.plan_variance(_health_dataset())
        rows = {r["item_no"]: r for r in v["materials"]}
        self.assertAlmostEqual(rows["R1"]["over_pct"], -10.0, places=1)  # 0.9 vs 1.0

    def test_rare_materials_are_skipped(self):
        data = _health_dataset()
        data[0]["lines"].append({"row_no": 9, "item_no": "RARE",
                                 "item_description": "ของหายาก", "type": "Item",
                                 "quantity": 5.0, "plan": 1.0, "whse": "", "unit": "KG"})
        v = analytics.plan_variance(data, min_orders=5)
        self.assertNotIn("RARE", {r["item_no"] for r in v["materials"]})

    def test_lines_without_a_plan_are_ignored(self):
        data = [_order("Z", "2026-07-01", "S1", "x", 100.0,
                       [("M9", "ไม่มีแผน", 5.0, 0.0)]) for _ in range(6)]
        v = analytics.plan_variance(data, min_orders=5)
        self.assertEqual(v["materials"], [])


class TestWorkload(unittest.TestCase):
    def test_counts_and_forecast(self):
        w = analytics.workload(_health_dataset(), today=dt.date(2026, 8, 4))
        self.assertTrue(w["ready"])
        self.assertEqual(w["total"], 24)
        self.assertEqual(w["last_month"], 4)
        self.assertGreater(w["forecast_next_month"], 0)

    def test_current_month_excluded_from_monthly_trend(self):
        data = _health_dataset()
        extra = _order("NOW", "2026-08-02", "S1", "ขนมปัง", 100.0,
                       [("M1", "แป้งสาลี", 30.0, 30.0)])
        extra["scanned_at"] = "2026-08-02T09:00:00"
        w = analytics.workload(data + [extra], today=dt.date(2026, 8, 4))
        self.assertNotIn("2026-08", [m["month"] for m in w["monthly"]])
        self.assertEqual(w["total"], 25)   # still counted in the daily series

    def test_empty_input(self):
        w = analytics.workload([], today=dt.date(2026, 8, 4))
        self.assertFalse(w["ready"])
        self.assertEqual(w["total"], 0)


class TestWeekdayPattern(unittest.TestCase):
    def test_identifies_the_busiest_day(self):
        data = [_order(f"M{i}", "2026-07-06", "S1", "x", 500.0,      # Monday
                       [("M1", "แป้งสาลี", 30.0, 30.0)]) for i in range(3)]
        data += [_order("T1", "2026-07-07", "S1", "x", 50.0,         # Tuesday
                        [("M1", "แป้งสาลี", 30.0, 30.0)])]
        p = analytics.weekday_pattern(data)
        self.assertEqual(p["busiest"], "จันทร์")

    def test_days_never_worked_are_reported(self):
        data = [_order("M1", "2026-07-06", "S1", "x", 500.0,
                       [("M1", "แป้งสาลี", 30.0, 30.0)])]
        p = analytics.weekday_pattern(data)
        self.assertIn("อาทิตย์", p["idle_days"])

    def test_bad_dates_do_not_crash(self):
        data = [_order("X", "ไม่ทราบ", "S1", "x", 100.0,
                       [("M1", "แป้งสาลี", 30.0, 30.0)])]
        p = analytics.weekday_pattern(data)
        self.assertFalse(p["ready"])


class TestForecastExtras(unittest.TestCase):
    def test_warehouse_split_excludes_resources(self):
        f = analytics.forecast(_health_dataset(), today=dt.date(2026, 8, 4))
        names = {w["whse"] for w in f["warehouses"]}
        self.assertIn("P8-RM01", names)
        self.assertNotIn("P8-PD05", names)   # Resource rows are not stored goods

    def test_product_mix_sums_to_100(self):
        f = analytics.forecast(_health_dataset(), today=dt.date(2026, 8, 4))
        self.assertAlmostEqual(sum(m["share"] for m in f["mix"]), 100.0, places=0)

    def test_active_days_counts_distinct_dates(self):
        f = analytics.forecast(_health_dataset(), today=dt.date(2026, 8, 4))
        self.assertAlmostEqual(f["avg_active_days"], 4.0, places=1)


class TestWeekdayRanking(unittest.TestCase):
    def _day(self, order_no, date, volume, hours):
        o = _order(order_no, date, "S1", "x", volume,
                   [("M1", "แป้งสาลี", volume * 0.3, volume * 0.3),
                    ("R1", "แรงงาน", hours, hours)])
        o["lines"][1]["type"] = "Resource"
        o["lines"][1]["unit"] = "Hour"
        return o

    def test_busiest_is_ranked_by_hours_not_kilograms(self):
        # Monday moves more weight; Tuesday takes far more labour to make.
        data = [self._day("MON", "2026-07-06", 2000.0, 10.0),
                self._day("TUE", "2026-07-07", 800.0, 40.0)]
        p = analytics.weekday_pattern(data)
        self.assertEqual(p["ranked_by"], "hours")
        self.assertEqual(p["busiest"], "อังคาร")

    def test_falls_back_to_volume_without_resource_lines(self):
        data = [_order("MON", "2026-07-06", "S1", "x", 2000.0,
                       [("M1", "แป้งสาลี", 600.0, 600.0)]),
                _order("TUE", "2026-07-07", "S1", "x", 800.0,
                       [("M1", "แป้งสาลี", 240.0, 240.0)])]
        p = analytics.weekday_pattern(data)
        self.assertEqual(p["ranked_by"], "volume")
        self.assertEqual(p["busiest"], "จันทร์")

    def test_hours_are_averaged_per_occurrence_of_that_weekday(self):
        data = [self._day("A", "2026-07-06", 100.0, 8.0),
                self._day("B", "2026-07-13", 100.0, 12.0)]   # two Mondays
        p = analytics.weekday_pattern(data)
        monday = next(d for d in p["days"] if d["weekday"] == 0)
        self.assertEqual(monday["hours"], 20.0)
        self.assertEqual(monday["avg_hours_per_day"], 10.0)
