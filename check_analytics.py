"""
Local verification script (NOT part of the submission). Needs a running MongoDB and:  pip install httpx
Usage:  MONGO_URI=mongodb://localhost:27017 python check_analytics.py
It seeds a scratch database (check_attendance_tmp), runs the real app in-process through FastAPI's TestClient,
and compares the analytics endpoints with a straightforward Python oracle written from the business rules.
"""
import os, random, sys, threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

os.environ["MONGO_DB"] = "check_attendance_tmp"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "submission"))
from fastapi.testclient import TestClient            # noqa: E402
from app import main                                  # noqa: E402

random.seed(7)
UTC = timezone.utc
P = ("PRESENT", "WFH", "ON_DUTY")
hu = lambda x, p: float(Decimal(x).quantize(Decimal(1).scaleb(-p), rounding=ROUND_HALF_UP))

# ---------------- seed ----------------
main.db.client.drop_database(main.MONGO_DB)
main.ensure_indexes()
depts = {"Eng": 8, "Sales": 6, "Ops": 5, "Ghost": 2}            # Ghost: employees but no logs at all
emps = []
n = 0
for d, k in depts.items():
    for _ in range(k):
        n += 1
        joined = random.choice(["2026-01-05", "2026-06-01", "2026-07-13", "2026-07-20", "2026-08-03"])
        overnight = d == "Ops" and random.random() < .5
        emps.append({"emp_code": f"EMP{n:04d}", "name": f"N{n}", "email": f"n{n}@x.io", "department": d,
                     "shift_start": "22:00" if overnight else "09:30", "shift_end": "06:00" if overnight else "18:30",
                     "joined_on": joined, "created_at": datetime(2026, 1, 1, tzinfo=UTC)})
main.employees.insert_many([dict(e) for e in emps])
docs = []
for e in emps:
    if e["department"] == "Ghost":
        continue
    day = date(2026, 6, 20)
    while day <= date(2026, 8, 10):
        if day.isoformat() >= e["joined_on"] and random.random() < .8:     # weekends included, gaps included
            st = random.choice(P + P + ("ABSENT", "LEAVE"))
            pi = datetime(day.year, day.month, day.day, 4, 0, tzinfo=UTC) if st in P else None
            late = random.choice([0, 0, 0, 11, 12, 25, 25, 40]) if st in P else 0   # forced ties
            wh = round(random.uniform(2, 10), 2) if st in P and random.random() < .9 else None
            doc = {"emp_code": e["emp_code"], "date": day.isoformat(), "status": st, "punch_in": pi,
                   "punch_out": None, "work_hours": wh, "late_minutes": late,
                   "overtime_minutes": random.choice([0, 0, 35, 70]) if st in P else 0,
                   "half_day": bool(wh and wh < 4.5), "history": []}
            if random.random() < .1:
                doc.pop("half_day"); doc.pop("history")                  # legacy shape
            docs.append(doc)
        day += timedelta(days=1)
docs.append({"emp_code": "EMP9999", "date": "2026-07-08", "status": "PRESENT", "punch_in": None, "late_minutes": 99})  # orphan
main.logs.insert_many(docs)
c = TestClient(main.app)
by_emp = {e["emp_code"]: e for e in emps}

# ---------------- oracle ----------------
def mrange(m):
    y, mo = map(int, m.split("-")); f = date(y, mo, 1)
    l = (date(y + (mo == 12), mo % 12 + 1, 1) - timedelta(days=1)); return f, l
def wdays(m, joined):
    f, l = mrange(m); d = max(f, date.fromisoformat(joined)); k = 0
    while d <= l: k += d.weekday() < 5; d += timedelta(days=1)
    return k
def inmonth(m): f, l = mrange(m); return [x for x in docs if f.isoformat() <= x["date"] <= l.isoformat()]
def present_days(rows):
    s = Decimal(0)
    for x in rows:
        if x["status"] in P and date.fromisoformat(x["date"]).weekday() < 5:
            s += Decimal("0.5") if x.get("half_day") else 1
    return s

fails = 0
def eq(name, got, exp):
    global fails
    if got != exp:
        fails += 1; print("FAIL", name, "\n  got", got, "\n  exp", exp)

for m in ("2026-06", "2026-07", "2026-08", "2026-09"):
    # employee monthly
    for e in emps:
        rows = [x for x in inmonth(m) if x["emp_code"] == e["emp_code"]]
        wd = wdays(m, e["joined_on"]); pd = present_days(rows)
        exp = {"emp_code": e["emp_code"], "month": m, "working_days": wd, "present_days": float(pd),
               "leave_days": sum(x["status"] == "LEAVE" for x in rows),
               "late_count": sum(x.get("late_minutes", 0) > 0 for x in rows),
               "total_late_minutes": sum(x.get("late_minutes", 0) for x in rows),
               "total_overtime_minutes": sum(x.get("overtime_minutes", 0) for x in rows),
               "attendance_pct": None if wd == 0 else hu(pd / wd * 100, 2)}
        eq(f"monthly {e['emp_code']} {m}", c.get(f"/analytics/employees/{e['emp_code']}/monthly", params={"month": m}).json(), exp)
    # department summary
    f, l = mrange(m); exp_items = []
    for d in sorted(depts):
        mem = [e for e in emps if e["department"] == d and e["joined_on"] <= l.isoformat()]
        if not mem: continue
        rows = [x for x in inmonth(m) if x["emp_code"] in {e["emp_code"] for e in mem}]
        whs = [Decimal(str(x["work_hours"])) for x in rows if x["status"] in P and x.get("work_hours") is not None]
        exp_items.append({"department": d, "headcount": len(mem), "present_days": float(present_days(rows)),
            "avg_work_hours": hu(sum(whs) / len(whs), 2) if whs else None,
            "late_count": sum(x.get("late_minutes", 0) > 0 for x in rows),
            "total_late_minutes": sum(x.get("late_minutes", 0) for x in rows),
            "leave_count": sum(x["status"] == "LEAVE" for x in rows), "on_duty_count": sum(x["status"] == "ON_DUTY" for x in rows)})
    eq(f"dept summary {m}", c.get("/analytics/departments/summary", params={"month": m}).json(), {"month": m, "items": exp_items})
    # leaderboard (with and without department, several limits)
    for dept in (None, "Eng"):
        tot = {}
        for x in inmonth(m):
            e = by_emp.get(x["emp_code"])
            if e and x.get("late_minutes", 0) > 0 and (dept is None or e["department"] == dept):
                t = tot.setdefault(x["emp_code"], [0, 0]); t[0] += x["late_minutes"]; t[1] += 1
        ordered = sorted(tot.items(), key=lambda kv: (-kv[1][0], kv[0]))
        for limit in (1, 3, 10):
            rows = [{"rank": 1 + sum(v2[0] > v[0] for v2 in tot.values()), "emp_code": k, "name": by_emp[k]["name"],
                     "department": by_emp[k]["department"], "total_late_minutes": v[0], "late_count": v[1]} for k, v in ordered]
            exp = {"month": m, "items": [r for r in rows if r["rank"] <= limit]}
            params = {"month": m, "limit": limit, **({"department": dept} if dept else {})}
            eq(f"leaderboard {m} {dept} {limit}", c.get("/analytics/leaderboard/late", params=params).json(), exp)

# trend
def trend_oracle(dept, a, b):
    out, rates = [], []
    d = a
    while d <= b:
        ds = d.isoformat(); mem = [e for e in emps if e["department"] == dept and e["joined_on"] <= ds]
        rows = [x for x in docs if x["date"] == ds and by_emp.get(x["emp_code"], {}).get("department") == dept]
        halves = sum((1 if x.get("half_day") else 2) for x in rows if x["status"] in P)
        working = d.weekday() < 5
        rate = None if not (working and mem) else Decimal(halves) / 2 / len(mem)
        r4 = None if rate is None else Decimal(rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        rates.append(r4)
        win = [r for r in rates[-7:] if r is not None]
        out.append({"date": ds, "is_working_day": working, "headcount": len(mem), "present_count": halves / 2,
                    "late_count": sum(x.get("late_minutes", 0) > 0 for x in rows),
                    "attendance_rate": None if r4 is None else float(r4),
                    "moving_avg_7d": float((sum(win) / len(win)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)) if win else None})
        d += timedelta(days=1)
    return out
for dept in depts:
    for a, b in ((date(2026, 7, 1), date(2026, 7, 31)), (date(2026, 6, 1), date(2026, 8, 31)), (date(2026, 7, 4), date(2026, 7, 5))):
        got = c.get(f"/analytics/departments/{dept}/trend", params={"from": a.isoformat(), "to": b.isoformat()}).json()
        eq(f"trend {dept} {a}..{b}", got.get("items"), trend_oracle(dept, a, b))
eq("trend 404", c.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code, 404)
eq("trend 422 range", c.get("/analytics/departments/Eng/trend", params={"from": "2026-01-01", "to": "2026-12-31"}).status_code, 422)

# explain: IXSCAN and no COLLSCAN
import json
cases = {"attendance_list": {}, "attendance_list ": {"emp_code": "EMP0001", "date_from": "2026-07-01"},
         "employee_monthly": {"emp_code": "EMP0001", "month": "2026-07"}, "department_summary": {"month": "2026-07"},
         "late_leaderboard": {"month": "2026-07"}, "department_trend": {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"}}
for name, params in cases.items():
    r = c.get(f"/admin/explain/{name.strip()}", params=params); txt = json.dumps(r.json())
    eq(f"explain {name} status", r.status_code, 200)
    if "IXSCAN" not in txt or "COLLSCAN" in txt: fails += 1; print("FAIL explain plan", name, "IXSCAN" in txt, "COLLSCAN" in txt)

# concurrency
def burst(fn, n=12):
    res = []; ts = [threading.Thread(target=lambda: res.append(fn())) for _ in range(n)]
    [t.start() for t in ts]; [t.join() for t in ts]; return sorted(res)
emp = emps[0]["emp_code"]; ms = int(datetime(2026, 9, 1, 4, 0, tzinfo=UTC).timestamp() * 1000)
eq("punch-in race", burst(lambda: c.post("/attendance/punch-in", json={"emp_code": emp, "punched_at": ms}).status_code), [201] + [409] * 11)
out = ms + 9 * 3600 * 1000
eq("punch-out race", burst(lambda: c.post("/attendance/punch-out", json={"emp_code": emp, "punched_at": out}).status_code), [200] + [409] * 11)
k = [0]
def patch():
    k[0] += 1; return c.patch(f"/attendance/{emp}/2026-09-01", json={"punch_out": out - 60000 * k[0], "reason": "race test", "regularized_by": "t"}).status_code
codes = burst(patch, 8)
hist = c.get("/attendance", params={"emp_code": emp, "date_from": "2026-09-01", "date_to": "2026-09-01"}).json()["items"][0]["history"]
eq("patch race: history == number of 200s", len(hist), codes.count(200)); eq("patch race: only 200/409", set(codes) <= {200, 409}, True)

print("ALL OK" if not fails else f"{fails} FAILURES")
