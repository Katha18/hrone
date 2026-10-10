"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real environment variables win)

Layout of this file
  1. Configuration and database handles
  2. Pure helpers (stdlib only): time zones, rules R1-R5, rounding, calendar maths
  3. Request models and error helpers
  4. Serialisation (BSON <-> API)
  5. Index creation (startup)
  6. Pipeline / query builders (shared by the real endpoints and /admin/explain)
  7. Endpoints
"""
import calendar
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: variables already in the environment are never replaced

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes instead of naive ones.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]


# region: pure helpers (stdlib only - unit-testable without FastAPI or MongoDB)
# --------------------------------------------------------------------------- #
# 2. Pure helpers
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
MAX_SHIFT_SECONDS = 24 * 3600
MS_MIN, MS_MAX = 100_000_000_000, 4_102_444_800_000

HM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


def as_utc(dt: datetime) -> datetime:
    """Naive datetimes are assumed to be UTC (that is what BSON stores)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def trunc_seconds(dt: datetime) -> datetime:
    """R1: instants are truncated to whole seconds before any calculation."""
    return as_utc(dt).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime truncated to whole seconds (exact integer maths, no floats)."""
    return trunc_seconds(EPOCH + timedelta(milliseconds=ms))


def dt_to_ms(dt: datetime) -> int:
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_ymd(value: str) -> Date:
    """Strict YYYY-MM-DD. (date.fromisoformat alone also accepts '20260706' and ISO week dates.)"""
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError("expected YYYY-MM-DD")
    return Date.fromisoformat(value)


def hm_to_minutes(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hm_to_minutes(shift_end) <= hm_to_minutes(shift_start)


def _ist_instant(date_str: str, hm: str, plus_days: int = 0) -> datetime:
    d = parse_ymd(date_str) + timedelta(days=plus_days)
    h, m = divmod(hm_to_minutes(hm), 60)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch-in belongs to the day before."""
    local = trunc_seconds(punch_in).astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end):
        seconds_of_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_of_day < hm_to_minutes(shift_end) * 60:
            d -= timedelta(days=1)
    return d.isoformat()


def compute_late_minutes(punch_in: datetime, record_date: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes since shift start."""
    delta = int((trunc_seconds(punch_in) - _ist_instant(record_date, shift_start)).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out: datetime, record_date: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes past shift end, only if at least 30. The end of an overnight shift is on the next day."""
    end = _ist_instant(record_date, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    delta = int((trunc_seconds(punch_out) - end).total_seconds())
    minutes = delta // 60 if delta > 0 else 0
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def round_half_up(value: Decimal, places: int) -> Decimal:
    return Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up (Decimal, so no float or banker's rounding)."""
    seconds = int((trunc_seconds(punch_out) - trunc_seconds(punch_in)).total_seconds())
    return float(round_half_up(Decimal(seconds) / Decimal(3600), 2))


def derive_fields(status, punch_in, punch_out, record_date, shift_start, shift_end) -> dict:
    """The four stored derived fields for a record in its final state (see the table in DATA_MODEL.md)."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, record_date, shift_start)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(punch_out, record_date, shift_start, shift_end),
        "half_day": hours < 4.5,  # R5: on the rounded value
    }


def month_bounds(month: str) -> tuple:
    year, mon = int(month[:4]), int(month[5:7])
    first = Date(year, mon, 1)  # ValueError for year 0000
    return first, Date(year, mon, calendar.monthrange(year, mon)[1])


def working_days(first: Date, last: Date, joined_on: Date) -> int:
    """R7: Mon-Fri, counted from joined_on for mid-month joiners; no holiday calendar."""
    day, count = max(first, joined_on), 0
    while day <= last:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


def to_decimal(value) -> Decimal:
    if isinstance(value, Decimal128):
        return value.to_decimal()
    return Decimal(str(value))


# endregion


# --------------------------------------------------------------------------- #
# 3. Models and error helpers
# --------------------------------------------------------------------------- #
# Strict integer: floats (1.5e12, 1783312500000.0), strings and bools are rejected with 422.
EpochMs = Annotated[int, Field(strict=True, ge=MS_MIN, le=MS_MAX)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


def unprocessable(loc: tuple, msg: str):
    """Raise a 422 with FastAPI's standard validation body ({"detail": [ {...} ]})."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HM_PATTERN)
    joined_on: str = Field(pattern=DATE_PATTERN)

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        parse_ymd(v)  # e.g. 2026-02-30 -> ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Derived fields (work_hours, late_minutes, ...) are not declared, so a client that sends them is ignored.
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        return self


def parse_date_param(value: Optional[str], loc: tuple) -> Optional[Date]:
    if value is None:
        return None
    try:
        return parse_ymd(value)
    except ValueError:
        unprocessable(loc, "not a valid calendar date (YYYY-MM-DD)")


def parse_month(month: str) -> tuple:
    """-> (first_day, last_day) as ISO strings, comparable with the stored string dates."""
    try:
        first, last = month_bounds(month)
    except ValueError:
        unprocessable(("query", "month"), "not a valid month (YYYY-MM)")
    return first.isoformat(), last.isoformat()


def require(name: str, value):
    if value is None:
        unprocessable(("query", name), "Field required")
    return value


# --------------------------------------------------------------------------- #
# 4. Serialisation
# --------------------------------------------------------------------------- #
def _api_value(v):
    return dt_to_ms(v) if isinstance(v, datetime) else v


def serialize_employee(doc: dict) -> dict:
    created = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(created) if created is not None else None,
    }


def serialize_record(doc: dict) -> dict:
    """Stored document -> API record. Never exposes _id; fills gaps in legacy documents."""
    history = []
    for h in doc.get("history") or []:
        changes = {
            k: {"from": _api_value(c.get("from")), "to": _api_value(c.get("to"))}
            for k, c in (h.get("changes") or {}).items()
        }
        at = h.get("at")
        history.append({"at": dt_to_ms(at) if at is not None else None, "by": h.get("by"),
                        "reason": h.get("reason"), "changes": changes})
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _api_value(doc.get("punch_in")),
        "punch_out": _api_value(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# 5. Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
INDEXES = [
    # (collection, keys, options)
    (employees, [("emp_code", 1)], {"unique": True, "name": "emp_code_unique"}),
    (employees, [("department", 1), ("emp_code", 1)], {"name": "dept_emp_code"}),
    (employees, [("department", 1), ("joined_on", 1)], {"name": "dept_joined_on"}),
    (employees, [("joined_on", 1)], {"name": "joined_on"}),
    (logs, [("emp_code", 1), ("date", 1)], {"unique": True, "name": "emp_date_unique"}),
    (logs, [("date", -1), ("emp_code", 1)], {"name": "date_desc_emp"}),
    (logs, [("status", 1), ("date", -1), ("emp_code", 1)], {"name": "status_date_emp"}),
    (logs, [("date", 1), ("late_minutes", 1), ("emp_code", 1)], {"name": "date_late_emp"}),
]


def ensure_indexes() -> None:
    for coll, keys, opts in INDEXES:
        try:
            coll.create_index(keys, **opts)
        except PyMongoError:
            log.exception("could not create index %s on %s", opts["name"], coll.name)
            if opts.get("unique"):
                raise  # without the unique indexes the race guarantees are gone; surface it


@asynccontextmanager
async def lifespan(_app: FastAPI):
    for attempt in range(5):
        try:
            ensure_indexes()
            break
        except PyMongoError:
            log.warning("index creation failed (attempt %d), retrying", attempt + 1)
            time.sleep(1)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_request, _exc):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 6. Query / pipeline builders (used by the endpoints AND by /admin/explain)
# --------------------------------------------------------------------------- #
# Reusable aggregation expressions. Stored derived values are read as-is (never recomputed);
# missing late_minutes / overtime_minutes / half_day mean 0 / 0 / false.
_PRESENT = {"$in": ["$status", list(PRESENCE)]}
_HALF = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
_HALVES = {"$cond": [_PRESENT, {"$cond": [_HALF, 1, 2]}, 0]}  # presence in half-days: 2 = full day, 1 = half day
_WEEKDAY = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "onError": None, "onNull": None}}},
                    [2, 3, 4, 5, 6]]}  # $dayOfWeek: 1 = Sunday ... 7 = Saturday
_HALVES_WEEKDAY = {"$cond": [_WEEKDAY, _HALVES, 0]}  # R7: present_days counts Mon-Fri records only
_LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
_OT_MIN = {"$ifNull": ["$overtime_minutes", 0]}
_IS_LATE = {"$cond": [{"$gt": [_LATE_MIN, 0]}, 1, 0]}
_HAS_WH = {"$and": [_PRESENT, {"$isNumber": "$work_hours"}]}
_WH_DECIMAL = {"$cond": [_HAS_WH, {"$toDecimal": "$work_hours"}, 0]}  # Decimal128: exact sums, no float drift
_WH_ONE = {"$cond": [_HAS_WH, 1, 0]}


def _is_status(s: str) -> dict:
    return {"$cond": [{"$eq": ["$status", s]}, 1, 0]}


def attendance_list_spec(emp_code, date_from, date_to, status, page, page_size) -> dict:
    d_from = parse_date_param(date_from, ("query", "date_from"))
    d_to = parse_date_param(date_to, ("query", "date_to"))
    if d_from and d_to and d_from > d_to:
        unprocessable(("query", "date_from"), "date_from must not be after date_to")
    flt: dict[str, Any] = {}
    if emp_code is not None:
        flt["emp_code"] = emp_code
    if d_from or d_to:
        flt["date"] = {}
        if d_from:
            flt["date"]["$gte"] = date_from
        if d_to:
            flt["date"]["$lte"] = date_to
    if status is not None:
        flt["status"] = status
    return {"filter": flt, "sort": [("date", DESCENDING), ("emp_code", 1)],
            "skip": (page - 1) * page_size, "limit": page_size}


def monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "halves": {"$sum": _HALVES_WEEKDAY},
            "leave_days": {"$sum": _is_status("LEAVE")},
            "late_count": {"$sum": _IS_LATE},
            "total_late_minutes": {"$sum": _LATE_MIN},
            "total_overtime_minutes": {"$sum": _OT_MIN},
        }},
    ]


def department_summary_pipeline(first: str, last: str, department: Optional[str]) -> list:
    # Start from EMPLOYEES so people with zero logs still count (R9), then join each person's month of logs.
    match: dict[str, Any] = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "m",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$group": {
                    "_id": None,
                    "halves": {"$sum": _HALVES_WEEKDAY},
                    "late_count": {"$sum": _IS_LATE},
                    "late_minutes": {"$sum": _LATE_MIN},
                    "leave": {"$sum": _is_status("LEAVE")},
                    "on_duty": {"$sum": _is_status("ON_DUTY")},
                    "wh_sum": {"$sum": _WH_DECIMAL},
                    "wh_n": {"$sum": _WH_ONE},
                }},
            ],
        }},
        {"$unwind": {"path": "$m", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "halves": {"$sum": {"$ifNull": ["$m.halves", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$m.late_count", 0]}},
            "late_minutes": {"$sum": {"$ifNull": ["$m.late_minutes", 0]}},
            "leave": {"$sum": {"$ifNull": ["$m.leave", 0]}},
            "on_duty": {"$sum": {"$ifNull": ["$m.on_duty", 0]}},
            "wh_sum": {"$sum": {"$ifNull": ["$m.wh_sum", 0]}},
            "wh_n": {"$sum": {"$ifNull": ["$m.wh_n", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        # Covered by the (date, late_minutes, emp_code) index: no document fetch for the grouping.
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4). limit is applied AFTER ranking.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def validate_trend_range(from_: str, to: str) -> tuple:
    d_from = parse_date_param(from_, ("query", "from"))
    d_to = parse_date_param(to, ("query", "to"))
    if d_to < d_from:
        unprocessable(("query", "to"), "to must not be before from")
    if (d_to - d_from).days + 1 > 92:
        unprocessable(("query", "to"), "range must not exceed 92 days")
    return d_from, d_to


def _utc_midnight(d: Date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def department_trend_pipeline(department: str, d_from: Date, d_to: Date) -> list:
    f, t = d_from.isoformat(), d_to.isoformat()
    lower, upper = _utc_midnight(d_from), _utc_midnight(d_to + timedelta(days=1))
    return [
        {"$match": {"department": department, "joined_on": {"$lte": t}}},
        # Per employee: one small row per day with logs (index: emp_code, date).
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs",
            "pipeline": [
                {"$match": {"date": {"$gte": f, "$lte": t}}},
                {"$group": {"_id": "$date", "halves": {"$sum": _HALVES}, "late": {"$sum": _IS_LATE}}},
            ],
        }},
        # Turn every employee into "events": one join event (headcount +1) plus one event per logged day.
        {"$project": {"_id": 0, "events": {"$concatArrays": [
            [{"d": {"$cond": [{"$gt": ["$joined_on", f]}, "$joined_on", f]}, "halves": 0, "late": 0, "joins": 1}],
            {"$map": {"input": "$logs", "as": "l",
                      "in": {"d": "$$l._id", "halves": "$$l.halves", "late": "$$l.late", "joins": 0}}},
        ]}}},
        {"$unwind": "$events"},
        {"$group": {"_id": "$events.d", "halves": {"$sum": "$events.halves"}, "late": {"$sum": "$events.late"},
                    "joins": {"$sum": "$events.joins"}}},
        {"$addFields": {"dt": {"$dateFromString": {"dateString": "$_id"}}}},
        # Gap filling: one row per calendar day. The upper bound is pushed one day out and filtered below, so
        # the result is right whether the server treats it as inclusive or exclusive.
        {"$densify": {"field": "dt", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {"$match": {"dt": {"$gte": lower, "$lt": upper}}},
        {"$set": {"halves": {"$ifNull": ["$halves", 0]}, "late": {"$ifNull": ["$late", 0]},
                  "joins": {"$ifNull": ["$joins", 0]}}},
        # headcount(day) = number of join events up to and including that day (R9)
        {"$setWindowFields": {"sortBy": {"dt": 1}, "output": {
            "headcount": {"$sum": "$joins", "window": {"documents": ["unbounded", "current"]}}}}},
        {"$set": {"is_working": {"$in": [{"$dayOfWeek": "$dt"}, [2, 3, 4, 5, 6]]}}},
        # Rate in ten-thousandths, half-up, with integer maths only:
        #   rate = halves / (2 * headcount);  half-up(rate * 10^4) = floor((2N + D) / 2D), N = halves*10^4, D = 2*headcount
        {"$set": {"rate_e4": {"$cond": [
            {"$and": ["$is_working", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$halves", 20000]}, {"$multiply": [2, "$headcount"]}]},
                                    {"$multiply": [4, "$headcount"]}]}},
            None]}}},
        {"$set": {"has_rate": {"$cond": [{"$isNumber": "$rate_e4"}, 1, 0]}}},
        # 7-row moving window inside the requested range; $sum ignores null rates, has_rate counts the others.
        {"$setWindowFields": {"sortBy": {"dt": 1}, "output": {
            "ma_sum": {"$sum": "$rate_e4", "window": {"documents": [-6, 0]}},
            "ma_n": {"$sum": "$has_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}},
            "is_working_day": "$is_working", "headcount": 1, "halves": 1, "late": 1, "rate_e4": 1,
            "ma_e4": {"$cond": [{"$gt": ["$ma_n", 0]},
                                {"$floor": {"$divide": [{"$add": [{"$multiply": [2, "$ma_sum"]}, "$ma_n"]},
                                                        {"$multiply": [2, "$ma_n"]}]}},
                                None]},
        }},
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# 7. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# ----- employees ------------------------------------------------------------ #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # the unique index on emp_code is the guarantee, not a find_one first
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1, le=10_000_000),
    page_size: int = Query(20, ge=1, le=100),
):
    flt = {} if department is None else {"department": department}
    total = employees.count_documents(flt)
    cursor = employees.find(flt, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# ----- attendance ----------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": rec_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(body.status, ts, None, rec_date, emp["shift_start"], emp["shift_end"]),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): of N simultaneous requests exactly one insert succeeds
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    out_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record whose punch_in is at or before the punch-out. Records without a punch_in (ABSENT, LEAVE)
    # never match a date range comparison. Attendance dates and punch_ins are ordered the same way, so the
    # (emp_code, date) index serves the sort and the scan stops at the first hit.
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": out_at}}, sort=[("date", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    in_at = trunc_seconds(rec["punch_in"])
    if out_at <= in_at:
        unprocessable(("body", "punched_at"), "punched_at must be after the punch-in")
    if (out_at - in_at).total_seconds() > MAX_SHIFT_SECONDS:
        unprocessable(("body", "punched_at"), "punch-out is more than 24 hours after the punch-in")
    derived = derive_fields(rec["status"], in_at, out_at, rec["date"], emp["shift_start"], emp["shift_end"])
    # late_minutes is a punch-in value and stays as stored. The filter makes this a compare-and-set: only one of
    # several simultaneous punch-outs matches (punch_out still null); a concurrent correction also makes it miss.
    updated = logs.find_one_and_update(
        {"emp_code": rec["emp_code"], "date": rec["date"], "punch_out": None,
         "punch_in": rec["punch_in"], "status": rec["status"]},
        {"$set": {"punch_out": out_at, "work_hours": derived["work_hours"],
                  "overtime_minutes": derived["overtime_minutes"], "half_day": derived["half_day"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently or already punched out")
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1, le=10_000_000),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = attendance_list_spec(emp_code, date_from, date_to, status, page, page_size)
    flt = spec["filter"]
    # No filter: the collection's own counter is O(1); counting 100k documents on every call is not.
    total = logs.estimated_document_count() if not flt else logs.count_documents(flt)
    cursor = logs.find(flt, {"_id": 0}).sort(spec["sort"]).skip(spec["skip"]).limit(spec["limit"])
    return {"items": [serialize_record(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


_CHANGE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, datetime) and isinstance(b, datetime):
        return as_utc(a) == as_utc(b)
    if isinstance(a, float) or isinstance(b, float):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    try:
        parse_ymd(date)
    except ValueError:
        unprocessable(("path", "date"), "not a valid calendar date (YYYY-MM-DD)")
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    given = body.model_fields_set
    status = body.status if "status" in given else rec["status"]
    if status in PRESENCE:
        in_at = ms_to_dt(body.punch_in) if "punch_in" in given else rec.get("punch_in")
        out_at = ms_to_dt(body.punch_out) if "punch_out" in given else rec.get("punch_out")
        if in_at is None:
            unprocessable(("body", "punch_in"), "a presence status requires a punch_in")
        in_at = trunc_seconds(in_at)
        if attendance_date(in_at, emp["shift_start"], emp["shift_end"]) != date:
            unprocessable(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        if out_at is not None:
            out_at = trunc_seconds(out_at)
            if out_at <= in_at:
                unprocessable(("body", "punch_out"), "punch_out must be after punch_in")
            if (out_at - in_at).total_seconds() > MAX_SHIFT_SECONDS:
                unprocessable(("body", "punch_out"), "punch_out must be within 24 hours of punch_in")
    else:  # ABSENT / LEAVE clear both punch times
        if "punch_in" in given or "punch_out" in given:
            unprocessable(("body", "status"), f"status {status} cannot be combined with punch times")
        in_at = out_at = None

    old = {
        "status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    new = {"status": status, "punch_in": in_at, "punch_out": out_at,
           **derive_fields(status, in_at, out_at, date, emp["shift_start"], emp["shift_end"])}
    changes = {k: {"from": old[k], "to": new[k]} for k in _CHANGE_FIELDS if not _same(old[k], new[k])}
    if not changes:
        unprocessable(("body",), "the request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: the update only applies if the record is exactly as we read it (same history
    # length, same status and punch times). A concurrent correction, punch-out or any other writer makes it
    # miss, and the loser gets 409 instead of silently overwriting a history entry.
    history = rec.get("history")
    precondition = {"emp_code": emp_code, "date": date, "status": old["status"],
                    "punch_in": old["punch_in"], "punch_out": old["punch_out"],
                    "history": {"$size": len(history)} if isinstance(history, list) else None}
    update: dict[str, Any] = {"$set": {k: new[k] for k in _CHANGE_FIELDS}}
    if isinstance(history, list) or "history" not in rec:
        update["$push"] = {"history": entry}
    else:  # history present but null: $push would fail
        update["$set"]["history"] = [entry]
    updated = logs.find_one_and_update(precondition, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; reload and retry")
    return serialize_record(updated)


# ----- analytics ------------------------------------------------------------ #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    first, last = parse_month(month)
    emp = employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    agg = next(iter(logs.aggregate(monthly_pipeline(emp_code, first, last))), {})
    halves = int(agg.get("halves", 0))
    wd = working_days(Date.fromisoformat(first), Date.fromisoformat(last), parse_ymd(emp["joined_on"]))
    pct = None if wd == 0 else float(round_half_up(Decimal(halves) * 50 / Decimal(wd), 2))  # halves/2/wd*100
    return {
        "emp_code": emp_code, "month": month, "working_days": wd, "present_days": halves / 2,
        "leave_days": int(agg.get("leave_days", 0)), "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    first, last = parse_month(month)
    items = []
    for r in employees.aggregate(department_summary_pipeline(first, last, department)):
        n = int(r["wh_n"])
        avg = None if n == 0 else float(round_half_up(to_decimal(r["wh_sum"]) / Decimal(n), 2))
        items.append({
            "department": r["_id"], "headcount": int(r["headcount"]), "present_days": int(r["halves"]) / 2,
            "avg_work_hours": avg, "late_count": int(r["late_count"]), "total_late_minutes": int(r["late_minutes"]),
            "leave_count": int(r["leave"]), "on_duty_count": int(r["on_duty"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = parse_month(month)
    items = [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
        for r in logs.aggregate(late_leaderboard_pipeline(first, last, limit, department))
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=DATE_PATTERN),
    to: str = Query(..., pattern=DATE_PATTERN),
):
    d_from, d_to = validate_trend_range(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")

    def e4(v):  # ten-thousandths (integer valued) -> 4-decimal rate
        return None if v is None else float(Decimal(int(v)).scaleb(-4))

    items = [
        {"date": r["date"], "is_working_day": bool(r["is_working_day"]), "headcount": int(r["headcount"]),
         "present_count": int(r["halves"]) / 2, "late_count": int(r["late"]),
         "attendance_rate": e4(r["rate_e4"]), "moving_avg_7d": e4(r["ma_e4"])}
        for r in employees.aggregate(department_trend_pipeline(department, d_from, d_to))
    ]
    return {"department": department, "items": items}


# ----- admin ---------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                      "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
    to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    page: int = Query(1, ge=1, le=10_000_000),
    page_size: int = Query(20, ge=1, le=100),
):
    """executionStats explain of the endpoint's main query, built by the same builders the endpoint uses."""
    if endpoint == "attendance_list":
        spec = attendance_list_spec(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, "filter": spec["filter"], "sort": dict(spec["sort"]),
                   "skip": spec["skip"], "limit": spec["limit"]}
    else:
        if endpoint == "employee_monthly":
            first, last = parse_month(require("month", month))
            collection, pipeline = "attendance_logs", monthly_pipeline(require("emp_code", emp_code), first, last)
        elif endpoint == "department_summary":
            first, last = parse_month(require("month", month))
            collection, pipeline = "employees", department_summary_pipeline(first, last, department)
        elif endpoint == "late_leaderboard":
            first, last = parse_month(require("month", month))
            collection, pipeline = "attendance_logs", late_leaderboard_pipeline(first, last, limit, department)
        else:  # department_trend
            d_from, d_to = validate_trend_range(require("from", from_), require("to", to))
            collection = "employees"
            pipeline = department_trend_pipeline(require("department", department), d_from, d_to)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(raw))}
