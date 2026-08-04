"""Deterministic analytics over scanned production orders.

Every number the user ever sees is computed here, in plain Python.  The AI
layer (ask_ai.py) only translates a question into a query spec and phrases the
result — it never adds, averages or estimates anything itself, because a vision
model that quietly mis-sums a material total is worse than no answer at all.

Two things are computed:

* run_query()  — aggregation behind the natural-language Q&A
* forecast()   — material demand forecast, driven by the implied bill of
                 materials rather than by calendar time, since factory
                 consumption follows production volume, not the date.
"""
import datetime as dt
import difflib
import re
import statistics
import time
import unicodedata

import firestore_store as store

ORDER_CACHE_TTL = 120          # seconds; orders change slowly
OUTLIER_FACTOR = 5.0           # plan_total this many times the median is an OCR error
MIN_MONTHS_FOR_TREND = 3
FORECAST_TREND_WINDOW = 6      # months of history used to fit the trend
FORECAST_CLAMP = (0.5, 2.0)    # forecast must stay within this band of recent average

_cache = {"at": 0.0, "orders": None}


# ---------------------------------------------------------------------------
# Loading and normalisation
# ---------------------------------------------------------------------------
def load_orders(force=False):
    """All orders, cached briefly so a burst of questions hits Firestore once."""
    now = time.time()
    if not force and _cache["orders"] is not None and now - _cache["at"] < ORDER_CACHE_TTL:
        return _cache["orders"]
    data, _ = store.list_orders(limit=5000)
    _cache["orders"] = data
    _cache["at"] = now
    return data


def invalidate_cache():
    _cache["orders"] = None


def _norm(s):
    """Fold Thai text for matching: NFC, lowercase, no spaces."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", str(s or "")).lower())


def _num(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if n != n else n          # drop NaN


def _month(order):
    d = (order.get("document_date") or "")[:7]
    return d if re.match(r"^\d{4}-\d{2}$", d) else None


def catalog(orders):
    """Distinct materials and products present in the data."""
    materials, products = {}, {}
    for o in orders:
        if o.get("series_no"):
            products.setdefault(o["series_no"], o.get("product_name") or "")
        for ln in o.get("lines") or []:
            if ln.get("item_no"):
                materials.setdefault(ln["item_no"], ln.get("item_description") or "")
    return materials, products


def clean_orders(orders):
    """Drop orders whose production volume is an obvious OCR misread.

    A single order read as 25,200 KG instead of 252 KG would swamp a monthly
    total, so anything far above the median is set aside and reported rather
    than silently averaged in.
    """
    volumes = [_num(o.get("plan_total")) for o in orders if _num(o.get("plan_total")) > 0]
    if len(volumes) < 10:
        return orders, []
    ceiling = statistics.median(volumes) * OUTLIER_FACTOR
    kept, dropped = [], []
    for o in orders:
        (dropped if _num(o.get("plan_total")) > ceiling else kept).append(o)
    return kept, dropped


# ---------------------------------------------------------------------------
# Matching free-text terms to master data
# ---------------------------------------------------------------------------
def match_keys(term, mapping):
    """Resolve a user's wording to a set of item_no / series_no keys.

    Returns None when no filter was requested, or an empty set when the term
    matched nothing (which the caller must report as "not found" rather than
    silently treating as "everything").
    """
    if not term:
        return None
    t = _norm(term)
    if not t:
        return None
    hits = {k for k, v in mapping.items() if t in _norm(v) or t == _norm(k)}
    if hits:
        return hits
    best, score = None, 0.0
    for k, v in mapping.items():
        r = difflib.SequenceMatcher(None, t, _norm(v)).ratio()
        if r > score:
            best, score = k, r
    return {best} if score >= 0.55 else set()


# ---------------------------------------------------------------------------
# Query engine
# ---------------------------------------------------------------------------
ORDER_METRICS = {"order_count", "production"}
LINE_METRICS = {"quantity", "plan", "variance"}
GROUPS = {"material", "product", "month", "warehouse", "none"}


def _filter_orders(orders, filters, products):
    status = (filters.get("status") or "all").lower()
    date_from = filters.get("date_from") or ""
    date_to = filters.get("date_to") or ""
    prod_keys = match_keys(filters.get("product"), products)

    out = []
    for o in orders:
        if status != "all" and o.get("status") != status:
            continue
        d = (o.get("document_date") or "")[:10]
        if date_from and (not d or d < date_from):
            continue
        if date_to and (not d or d > date_to):
            continue
        if prod_keys is not None and o.get("series_no") not in prod_keys:
            continue
        out.append(o)
    return out, prod_keys


def run_query(spec, orders=None):
    """Execute a query spec and return rows plus a grand total.

    spec = {metric, group_by, filters:{material, product, status,
            date_from, date_to}, limit}
    """
    orders = orders if orders is not None else load_orders()
    orders, dropped = clean_orders(orders)
    materials, products = catalog(orders)

    metric = spec.get("metric") or "quantity"
    if metric not in ORDER_METRICS | LINE_METRICS:
        metric = "quantity"
    group_by = spec.get("group_by") or "none"
    if group_by not in GROUPS:
        group_by = "none"
    filters = spec.get("filters") or {}
    limit = int(spec.get("limit") or 20)

    scope, prod_keys = _filter_orders(orders, filters, products)
    mat_keys = match_keys(filters.get("material"), materials)

    if prod_keys == set() or mat_keys == set():
        return {"rows": [], "total": 0.0, "unit": "", "n_orders": 0,
                "not_found": filters.get("material") or filters.get("product"),
                "excluded_outliers": len(dropped)}

    buckets, units = {}, {}
    n_orders = 0

    for o in scope:
        month = _month(o) or "-"
        if metric in ORDER_METRICS:
            key = {"product": o.get("series_no") or "-",
                   "month": month}.get(group_by, "รวม")
            label = {"product": o.get("product_name") or key,
                     "month": month}.get(group_by, "รวม")
            value = 1.0 if metric == "order_count" else _num(o.get("plan_total"))
            buckets.setdefault(key, [label, 0.0])[1] += value
            units[key] = "ใบ" if metric == "order_count" else (o.get("plan_unit") or "KG")
            n_orders += 1
            continue

        matched_any = False
        for ln in o.get("lines") or []:
            item_no = ln.get("item_no") or "-"
            if mat_keys is not None and item_no not in mat_keys:
                continue
            matched_any = True
            if metric == "quantity":
                value = _num(ln.get("quantity"))
            elif metric == "plan":
                value = _num(ln.get("plan"))
            else:
                value = _num(ln.get("quantity")) - _num(ln.get("plan"))

            key = {"material": item_no,
                   "product": o.get("series_no") or "-",
                   "month": month,
                   "warehouse": ln.get("whse") or "-"}.get(group_by, "รวม")
            label = {"material": ln.get("item_description") or item_no,
                     "product": o.get("product_name") or key,
                     "month": month,
                     "warehouse": key}.get(group_by, "รวม")
            buckets.setdefault(key, [label, 0.0])[1] += value
            units.setdefault(key, ln.get("unit") or "KG")
        if matched_any:
            n_orders += 1

    active_days = len({(o.get("document_date") or "")[:10] for o in scope
                       if o.get("document_date")})

    rows = [{"key": k, "label": v[0], "value": round(v[1], 3),
             "unit": units.get(k, "")} for k, v in buckets.items()]
    if group_by == "month":
        rows.sort(key=lambda r: r["key"])
    else:
        rows.sort(key=lambda r: -abs(r["value"]))
    rows = rows[:limit]

    total = round(sum(v[1] for v in buckets.values()), 3)
    unit = rows[0]["unit"] if rows else ""
    return {"rows": rows, "total": total, "unit": unit, "n_orders": n_orders,
            "active_days": active_days, "excluded_outliers": len(dropped)}


def run_with_comparison(spec, orders=None):
    """Run a spec, plus an optional second period for period-over-period deltas."""
    orders = orders if orders is not None else load_orders()
    main = run_query(spec, orders)
    cmp_range = spec.get("compare_to")
    if not cmp_range:
        return main
    alt_spec = dict(spec)
    alt_filters = dict(spec.get("filters") or {})
    alt_filters["date_from"] = cmp_range.get("date_from")
    alt_filters["date_to"] = cmp_range.get("date_to")
    alt_spec["filters"] = alt_filters
    alt_spec.pop("compare_to", None)
    prev = run_query(alt_spec, orders)
    # An in-progress month against a finished one is not a like-for-like
    # comparison: four days of August against all of July reads as a 93% collapse
    # when nothing has actually changed.  Report the per-day rate alongside, and
    # say plainly when the two windows are not the same length.
    days_now = main.get("active_days") or 0
    days_prev = prev.get("active_days") or 0
    rate_now = (main["total"] / days_now) if days_now else None
    rate_prev = (prev["total"] / days_prev) if days_prev else None
    ratio = (days_now / days_prev) if days_now and days_prev else 1.0
    mismatch = bool(days_now and days_prev and not (0.7 <= ratio <= 1.43))

    main["comparison"] = {
        "total": prev["total"], "n_orders": prev["n_orders"],
        "date_from": alt_filters.get("date_from"),
        "date_to": alt_filters.get("date_to"),
        "delta": round(main["total"] - prev["total"], 3),
        "delta_pct": (round((main["total"] - prev["total"]) / prev["total"] * 100, 1)
                      if prev["total"] else None),
        "active_days": days_prev,
        "per_day": round(rate_prev, 3) if rate_prev is not None else None,
        "per_day_now": round(rate_now, 3) if rate_now is not None else None,
        "per_day_delta_pct": (round((rate_now - rate_prev) / rate_prev * 100, 1)
                              if rate_prev else None),
        "length_mismatch": mismatch,
    }
    return main


# ---------------------------------------------------------------------------
# Implied bill of materials
# ---------------------------------------------------------------------------
def implied_bom(orders=None, min_orders=3):
    """Derive each product's real recipe from what was actually issued.

    For every (product, material) pair the per-order ratio quantity/plan_total
    is collected; the median is the recipe and the spread tells us how
    trustworthy it is.  Median rather than mean so one fat-fingered quantity
    cannot move the recipe.
    """
    orders = orders if orders is not None else load_orders()
    orders, _ = clean_orders(orders)

    ratios = {}          # series_no -> item_no -> [ratio, ...]
    names = {}
    units = {}
    kinds = {}
    warehouses = {}
    for o in orders:
        volume = _num(o.get("plan_total"))
        series = o.get("series_no")
        if volume <= 0 or not series:
            continue
        names[series] = o.get("product_name") or series
        for ln in o.get("lines") or []:
            item_no = ln.get("item_no")
            qty = _num(ln.get("quantity"))
            if not item_no or qty <= 0:
                continue
            ratios.setdefault(series, {}).setdefault(item_no, []).append(qty / volume)
            units.setdefault(item_no, ln.get("unit") or "KG")
            names.setdefault(item_no, ln.get("item_description") or item_no)
            kinds.setdefault(item_no, ln.get("type") or "Item")
            warehouses.setdefault(item_no, ln.get("whse") or "")

    out = {}
    for series, items in ratios.items():
        rows = []
        for item_no, values in items.items():
            if len(values) < min_orders:
                continue
            median = statistics.median(values)
            spread = statistics.pstdev(values) if len(values) > 1 else 0.0
            rows.append({
                "item_no": item_no,
                "item_description": names.get(item_no, item_no),
                "ratio": round(median, 6),
                "cv": round(spread / median, 4) if median else None,
                "unit": units.get(item_no, "KG"),
                "type": kinds.get(item_no, "Item"),
                "whse": warehouses.get(item_no, ""),
                "samples": len(values),
            })
        rows.sort(key=lambda r: -r["ratio"])
        out[series] = {"product_name": names.get(series, series), "lines": rows}
    return out


# ---------------------------------------------------------------------------
# Forecasting
# ---------------------------------------------------------------------------
def _month_key(y, m):
    return f"{y:04d}-{m:02d}"


def _next_months(last, count):
    y, m = int(last[:4]), int(last[5:7])
    out = []
    for _ in range(count):
        m += 1
        if m > 12:
            y, m = y + 1, 1
        out.append(_month_key(y, m))
    return out


def _fit_next(series, steps=1):
    """Predict the next `steps` values of a monthly series by least-squares trend.

    Each step is clamped to a sane band around the recent average, because a
    short trend on noisy factory data will happily extrapolate to zero or to
    double within a few months.
    """
    values = [v for _, v in series]
    if not values:
        return [0.0] * steps
    recent = values[-3:] if len(values) >= 3 else values
    baseline = sum(recent) / len(recent)
    if len(values) < MIN_MONTHS_FOR_TREND:
        return [baseline] * steps

    window = series[-FORECAST_TREND_WINDOW:]
    n = len(window)
    xs = list(range(n))
    ys = [v for _, v in window]
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    slope = (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom) if denom else 0.0
    low, high = FORECAST_CLAMP
    return [max(baseline * low, min(baseline * high, my + slope * (n - mx + k)))
            for k in range(steps)]


def _active_days_per_month(orders, exclude_month):
    """Average number of distinct production days in a completed month."""
    days = {}
    for o in orders:
        month = _month(o)
        d = (o.get("document_date") or "")[:10]
        if not month or not d or month >= exclude_month:
            continue
        days.setdefault(month, set()).add(d)
    if not days:
        return None
    return sum(len(v) for v in days.values()) / len(days)


def _monthly_production(orders, exclude_month):
    """Completed-month production volume per product."""
    per = {}
    for o in orders:
        month = _month(o)
        series = o.get("series_no")
        volume = _num(o.get("plan_total"))
        if not month or not series or volume <= 0 or month >= exclude_month:
            continue
        per.setdefault(series, {}).setdefault(month, 0.0)
        per[series][month] += volume
    return per


def _monthly_usage(orders, exclude_month):
    per = {}
    for o in orders:
        month = _month(o)
        if not month or month >= exclude_month:
            continue
        for ln in o.get("lines") or []:
            item_no = ln.get("item_no")
            if not item_no:
                continue
            per.setdefault(item_no, {}).setdefault(month, 0.0)
            per[item_no][month] += _num(ln.get("quantity"))
    return per


def _confidence(cv, samples):
    if samples < MIN_MONTHS_FOR_TREND:
        return "low"
    if cv is None:
        return "low"
    if cv <= 0.15:
        return "high"
    if cv <= 0.35:
        return "medium"
    return "low"


def forecast(orders=None, months=1, today=None):
    """Forecast material demand for the coming month(s).

    Demand is not extrapolated from past usage directly.  Production volume is
    forecast per product, then multiplied by that product's implied recipe —
    so a change in the product mix moves the material forecast the way it
    actually would on the factory floor.
    """
    orders = orders if orders is not None else load_orders()
    orders, dropped = clean_orders(orders)
    today = today or dt.date.today()
    current_month = _month_key(today.year, today.month)

    production = _monthly_production(orders, current_month)
    usage = _monthly_usage(orders, current_month)
    bom = implied_bom(orders)

    all_months = sorted({m for per in production.values() for m in per})
    if not all_months:
        return {"ready": False,
                "reason": "ยังไม่มีข้อมูลเดือนที่สมบูรณ์เพียงพอสำหรับการพยากรณ์",
                "months_available": 0}

    horizon = _next_months(all_months[-1], months)

    # --- production forecast per product -----------------------------------
    product_rows = []
    forecast_volume = {}
    for series, by_month in production.items():
        series_points = [(m, by_month.get(m, 0.0)) for m in all_months]
        predicted = _fit_next(series_points, months)
        forecast_volume[series] = sum(predicted)
        values = [v for _, v in series_points if v > 0]
        mean = sum(values) / len(values) if values else 0.0
        cv = (statistics.pstdev(values) / mean) if len(values) > 1 and mean else None
        product_rows.append({
            "series_no": series,
            "product_name": bom.get(series, {}).get("product_name", series),
            "history": [{"month": m, "value": round(v, 1)} for m, v in series_points],
            "by_month": [{"month": m, "value": round(v, 1)}
                         for m, v in zip(horizon, predicted)],
            "avg_monthly": round(mean, 1),
            "forecast": round(sum(predicted), 1),
            "unit": "KG",
            "confidence": _confidence(cv, len(values)),
        })
    product_rows.sort(key=lambda r: -r["forecast"])

    # --- material forecast = Σ (product volume × recipe) --------------------
    material_rows = {}
    for series, volume in forecast_volume.items():
        for line in bom.get(series, {}).get("lines", []):
            row = material_rows.setdefault(line["item_no"], {
                "item_no": line["item_no"],
                "item_description": line["item_description"],
                "unit": line["unit"],
                "type": line.get("type", "Item"),
                "whse": line.get("whse", ""),
                "forecast": 0.0,
                "used_in": {},
                "_cv_weight": 0.0,
                "_cv_sum": 0.0,
            })
            contribution = volume * line["ratio"]
            row["forecast"] += contribution
            row["used_in"][series] = round(contribution, 2)
            if line["cv"] is not None:
                row["_cv_sum"] += line["cv"] * contribution
                row["_cv_weight"] += contribution

    out_materials = []
    for item_no, row in material_rows.items():
        history = usage.get(item_no, {})
        points = [(m, history.get(m, 0.0)) for m in all_months]
        actual = [v for _, v in points if v > 0]
        avg = sum(actual) / len(actual) if actual else 0.0
        recipe_cv = (row["_cv_sum"] / row["_cv_weight"]) if row["_cv_weight"] else None
        monthly_cv = (statistics.pstdev(actual) / avg) if len(actual) > 1 and avg else None
        cv = max(x for x in (recipe_cv, monthly_cv) if x is not None) \
            if (recipe_cv is not None or monthly_cv is not None) else None
        value = row["forecast"]
        band = (cv or 0.25) * 1.5
        # Over a multi-month horizon the fair comparison is the average per
        # month times the number of months, not one month's average.
        baseline = avg * months
        out_materials.append({
            "item_no": item_no,
            "item_description": row["item_description"],
            "unit": row["unit"],
            "type": row["type"],
            "whse": row["whse"],
            "used_in": row["used_in"],
            "avg_monthly": round(avg, 2),
            "baseline": round(baseline, 2),
            "forecast": round(value, 2),
            "low": round(max(0.0, value * (1 - band)), 2),
            "high": round(value * (1 + band), 2),
            "vs_avg_pct": round((value - baseline) / baseline * 100, 1) if baseline else None,
            "confidence": _confidence(cv, len(actual)),
            "history": [{"month": m, "value": round(v, 1)} for m, v in points],
        })
    out_materials.sort(key=lambda r: -r["forecast"])

    total_forecast = sum(forecast_volume.values())
    last_month_total = sum(per.get(all_months[-1], 0.0) for per in production.values())

    # --- demand split by storeroom, for space and receiving planning --------
    warehouses = {}
    for m in out_materials:
        if m["type"] != "Item":
            continue
        w = warehouses.setdefault(m["whse"] or "(ไม่ระบุ)",
                                  {"whse": m["whse"] or "(ไม่ระบุ)", "forecast": 0.0,
                                   "items": 0, "unit": m["unit"]})
        w["forecast"] += m["forecast"]
        w["items"] += 1
    warehouse_rows = sorted(
        ({**w, "forecast": round(w["forecast"], 1)} for w in warehouses.values()),
        key=lambda r: -r["forecast"])

    # --- product mix: what share of the line each product will take ---------
    last_by_product = {s: per.get(all_months[-1], 0.0) for s, per in production.items()}
    last_total = sum(last_by_product.values()) or 1.0
    mix_rows = []
    for p in product_rows:
        share = (forecast_volume.get(p["series_no"], 0.0) / total_forecast * 100) \
            if total_forecast else 0.0
        was = last_by_product.get(p["series_no"], 0.0) / last_total * 100
        mix_rows.append({
            "series_no": p["series_no"],
            "product_name": p["product_name"],
            "share": round(share, 1),
            "share_last_month": round(was, 1),
            "delta": round(share - was, 1),
        })

    # Active days per month, so hours can be shown as a daily workload.
    active_days = _active_days_per_month(orders, current_month)

    return {
        "ready": True,
        "warehouses": warehouse_rows,
        "mix": mix_rows,
        "avg_active_days": round(active_days, 1) if active_days else None,
        "target_months": horizon,
        "horizon_months": months,
        "products_index": {p["series_no"]: p["product_name"] for p in product_rows},
        "months_used": all_months,
        "months_available": len(all_months),
        "production_total_forecast": round(total_forecast, 1),
        "production_total_last_month": round(last_month_total, 1),
        "products": product_rows,
        "materials": out_materials,
        "excluded_outliers": len(dropped),
        "generated_for": current_month,
    }


# ---------------------------------------------------------------------------
# Production health: yield, plan variance, workload, weekly rhythm
# ---------------------------------------------------------------------------
YIELD_ALERT_DROP = 0.02        # a 2-point fall against the product's own average


def yield_trend(orders=None, today=None):
    """Track actual output against planned output for each product.

    This is the only measure here that says something is *going wrong* rather
    than how much to buy, so a product whose recent yield sits below its own
    historical average is flagged rather than left for the reader to spot.
    """
    orders = orders if orders is not None else load_orders()
    orders, _ = clean_orders(orders)
    today = today or dt.date.today()
    current_month = _month_key(today.year, today.month)

    per = {}    # series -> month -> [plan_sum, actual_sum]
    names = {}
    for o in orders:
        month = _month(o)
        series = o.get("series_no")
        plan = _num(o.get("plan_total"))
        actual = _num(o.get("actual_total"))
        if not month or not series or plan <= 0 or actual <= 0 or month >= current_month:
            continue
        names[series] = o.get("product_name") or series
        slot = per.setdefault(series, {}).setdefault(month, [0.0, 0.0])
        slot[0] += plan
        slot[1] += actual

    months = sorted({m for v in per.values() for m in v})
    rows = []
    for series, by_month in per.items():
        points = [{"month": m,
                   "value": round(by_month[m][1] / by_month[m][0] * 100, 2)}
                  for m in months if m in by_month and by_month[m][0] > 0]
        if not points:
            continue
        values = [p["value"] for p in points]
        latest = values[-1]
        average = sum(values) / len(values)
        rows.append({
            "series_no": series,
            "product_name": names.get(series, series),
            "history": points,
            "latest": round(latest, 2),
            "average": round(average, 2),
            "delta": round(latest - average, 2),
            "declining": latest < average - YIELD_ALERT_DROP * 100,
            "months": len(points),
        })
    rows.sort(key=lambda r: r["delta"])          # worst first — that is the point
    return {"ready": bool(rows), "months_used": months, "products": rows,
            "alert_count": sum(1 for r in rows if r["declining"])}


def plan_variance(orders=None, today=None, min_orders=5):
    """How much more (or less) than planned each material actually gets issued.

    The buffer suggestion is the median over-issue, not the mean: a single
    mis-keyed quantity should not become next month's purchase padding.
    """
    orders = orders if orders is not None else load_orders()
    orders, _ = clean_orders(orders)

    per = {}
    names, units, kinds = {}, {}, {}
    for o in orders:
        for ln in o.get("lines") or []:
            item_no = ln.get("item_no")
            plan = _num(ln.get("plan"))
            qty = _num(ln.get("quantity"))
            if not item_no or plan <= 0 or qty <= 0:
                continue
            per.setdefault(item_no, []).append((qty - plan) / plan)
            names.setdefault(item_no, ln.get("item_description") or item_no)
            units.setdefault(item_no, ln.get("unit") or "KG")
            kinds.setdefault(item_no, ln.get("type") or "Item")

    rows = []
    for item_no, ratios in per.items():
        if len(ratios) < min_orders:
            continue
        median = statistics.median(ratios)
        rows.append({
            "item_no": item_no,
            "item_description": names[item_no],
            "unit": units[item_no],
            "type": kinds[item_no],
            "over_pct": round(median * 100, 2),
            "worst_pct": round(max(ratios) * 100, 2),
            "spread": round(statistics.pstdev(ratios) * 100, 2) if len(ratios) > 1 else 0.0,
            "over_count": sum(1 for r in ratios if r > 0),
            "samples": len(ratios),
        })
    rows.sort(key=lambda r: -r["over_pct"])
    return {"ready": bool(rows), "materials": rows}


def workload(orders=None, today=None, recent_days=30):
    """Scan volume per day and per month, and what next month looks like.

    Drives data-entry staffing and gives a scan count that a per-scan AI price
    can be applied to — the price itself is the operator's to supply.
    """
    orders = orders if orders is not None else load_orders()
    today = today or dt.date.today()
    current_month = _month_key(today.year, today.month)

    per_day, per_month = {}, {}
    for o in orders:
        stamp = (o.get("scanned_at") or o.get("document_date") or "")[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", stamp):
            continue
        per_day[stamp] = per_day.get(stamp, 0) + 1
        month = stamp[:7]
        if month < current_month:
            per_month[month] = per_month.get(month, 0) + 1

    months = sorted(per_month)
    series = [(m, float(per_month[m])) for m in months]
    predicted = _fit_next(series, 1)[0] if series else 0.0

    cutoff = (today - dt.timedelta(days=recent_days)).isoformat()
    recent = sorted((d, n) for d, n in per_day.items() if d >= cutoff)
    active = [n for _, n in recent if n > 0]

    return {
        "ready": bool(per_day),
        "daily": [{"date": d, "count": n} for d, n in recent],
        "monthly": [{"month": m, "count": per_month[m]} for m in months],
        "forecast_next_month": round(predicted),
        "last_month": per_month[months[-1]] if months else 0,
        "avg_per_active_day": round(sum(active) / len(active), 1) if active else 0.0,
        "busiest_day": max(recent, key=lambda x: x[1])[0] if recent else None,
        "total": sum(per_day.values()),
    }


WEEKDAY_TH = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]


def weekday_pattern(orders=None):
    """Production rhythm across the week, for shift and workload levelling.

    The busiest day is ranked by labour and machine hours, not by kilograms.
    Products differ in how much work a kilogram takes, so a heavy-tonnage day
    of a simple line can be lighter than a smaller day of hand-assembled goods.
    Kilograms are still reported, they just do not decide the ranking.
    """
    orders = orders if orders is not None else load_orders()
    orders, _ = clean_orders(orders)

    counts = {i: 0 for i in range(7)}
    volume = {i: 0.0 for i in range(7)}
    hours = {i: 0.0 for i in range(7)}
    dates = {i: set() for i in range(7)}
    for o in orders:
        d = (o.get("document_date") or "")[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            continue
        try:
            day = dt.date.fromisoformat(d)
        except ValueError:
            continue
        w = day.weekday()
        counts[w] += 1
        volume[w] += _num(o.get("plan_total"))
        dates[w].add(d)
        for ln in o.get("lines") or []:
            if (ln.get("type") or "") == "Resource":
                hours[w] += _num(ln.get("quantity"))

    total_volume = sum(volume.values()) or 1.0
    total_hours = sum(hours.values())
    rows = []
    for w in range(7):
        n_days = len(dates[w])
        rows.append({
            "weekday": w,
            "name": WEEKDAY_TH[w],
            "orders": counts[w],
            "volume": round(volume[w], 1),
            "hours": round(hours[w], 1),
            "share": round(volume[w] / total_volume * 100, 1),
            "avg_orders_per_day": round(counts[w] / n_days, 1) if n_days else 0.0,
            "avg_volume_per_day": round(volume[w] / n_days, 1) if n_days else 0.0,
            "avg_hours_per_day": round(hours[w] / n_days, 1) if n_days else 0.0,
            "days_observed": n_days,
        })

    # Hours are the honest workload measure; fall back to volume only when the
    # forms carry no Resource lines at all.
    ranked_by = "hours" if total_hours > 0 else "volume"
    key = "avg_hours_per_day" if ranked_by == "hours" else "avg_volume_per_day"
    busiest = max(rows, key=lambda r: r[key])
    idle = [r["name"] for r in rows if r["days_observed"] == 0 or r["orders"] == 0]
    return {"ready": any(r["orders"] for r in rows), "days": rows,
            "busiest": busiest["name"], "ranked_by": ranked_by, "idle_days": idle}
