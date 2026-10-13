"""Employee attendance API. All persisted instants are UTC BSON datetimes."""

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from dotenv import load_dotenv
from bson.decimal128 import Decimal128
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Epoch = StrictInt
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True,
                     tzinfo=UTC, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])


def valid_day(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date must be YYYY-MM-DD")
    date.fromisoformat(value)
    return value


def valid_month(value: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("month must be YYYY-MM")
    return value


def epoch_to_dt(value: int) -> datetime:
    if type(value) is not int or not 100000000000 <= value <= 4102444800000:
        raise HTTPException(422, "timestamp must be integer epoch milliseconds in range")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    return int(value.replace(tzinfo=UTC).timestamp() * 1000) if value.tzinfo is None else int(value.timestamp() * 1000)


def employee_json(doc: dict) -> dict:
    return {k: (ms(v) if k == "created_at" else v) for k, v in doc.items() if k != "_id"}


def attendance_json(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k not in ("_id", "revision")}
    for key in ("punch_in", "punch_out"):
        result[key] = ms(result.get(key))
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result["history"] = [{"at": ms(h["at"]), "by": h["by"], "reason": h["reason"],
                          "changes": {k: {side: ms(val) if k in ("punch_in", "punch_out") else val
                                           for side, val in change.items()}
                                      for k, change in h["changes"].items()}}
                         for h in result.get("history", [])]
    return result


def attendance_day(punch: datetime, employee: dict) -> date:
    local = punch.astimezone(IST)
    day = local.date()
    if employee["shift_end"] <= employee["shift_start"] and local.time() < time.fromisoformat(employee["shift_end"]):
        day -= timedelta(days=1)
    return day


def shift_instant(day: date, clock: str, next_day: bool = False) -> datetime:
    return datetime.combine(day + timedelta(days=int(next_day)), time.fromisoformat(clock), IST).astimezone(UTC)


def derived(employee: dict, day: date, punch_in: datetime | None, punch_out: datetime | None) -> dict:
    if punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    elapsed = (punch_in - shift_instant(day, employee["shift_start"])).total_seconds()
    late = int(elapsed // 60) if elapsed > 600 else 0
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    seconds = int((punch_out - punch_in).total_seconds())
    hours = float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    end = shift_instant(day, employee["shift_end"], employee["shift_end"] <= employee["shift_start"])
    overtime = max(0, int((punch_out - end).total_seconds() // 60))
    return {"late_minutes": late, "work_hours": hours,
            "overtime_minutes": overtime if overtime >= 30 else 0, "half_day": hours < 4.5}


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def check_day(cls, value: str) -> str:
        return valid_day(value)

    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Epoch | None = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Epoch | None = None


class RegularizeIn(BaseModel):
    status: Status | None = None
    punch_in: Epoch | None = None
    punch_out: Epoch | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def validate_optional_epoch(body: BaseModel, field: str) -> datetime | None:
    value = getattr(body, field)
    if field in body.model_fields_set and value is None:
        raise HTTPException(422, f"{field} must be integer epoch milliseconds")
    return epoch_to_dt(value) if value is not None else None


@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_json(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    filt = {"department": department} if department is not None else {}
    items = db.employees.find(filt).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_json(item) for item in items], "total": db.employees.count_documents(filt),
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punch = validate_optional_epoch(body, "punched_at") or now()
    day = attendance_day(punch, employee)
    doc = {"emp_code": body.emp_code, "date": day.isoformat(), "status": body.status,
           "punch_in": punch, "punch_out": None, "history": [], **derived(employee, day, punch, None)}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "attendance already exists for this date")
    return attendance_json(doc)


def attendance_filter(emp_code=None, date_from=None, date_to=None, status=None):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not exceed date_to")
    filt = {}
    if emp_code is not None:
        filt["emp_code"] = emp_code
    if status is not None:
        filt["status"] = status
    if date_from or date_to:
        filt["date"] = {}
        if date_from:
            filt["date"]["$gte"] = date_from
        if date_to:
            filt["date"]["$lte"] = date_to
    return filt


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: date | None = None, date_to: date | None = None,
                    status: Status | None = None, page: int = Query(1, ge=1),
                    page_size: int = Query(20, ge=1, le=100)):
    filt = attendance_filter(emp_code, str(date_from) if date_from else None,
                             str(date_to) if date_to else None, status)
    items = db.attendance_logs.find(filt).sort([("date", DESCENDING), ("emp_code", ASCENDING)])\
        .skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_json(item) for item in items],
            "total": db.attendance_logs.count_documents(filt), "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punch = validate_optional_epoch(body, "punched_at") or now()
    original = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punch}},
                                           sort=[("punch_in", DESCENDING)])
    if original is None:
        if db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$gt": punch}}):
            raise HTTPException(422, "punch-out must be after punch-in")
        raise HTTPException(404, "no punch-in found")
    if original.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    start = original["punch_in"]
    if not start < punch <= start + timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    update = {"punch_out": punch, **derived(employee, date.fromisoformat(original["date"]), start, punch)}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": original["_id"], "punch_in": start, "punch_out": None, "status": original["status"]},
        {"$set": update}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "attendance changed concurrently")
    return attendance_json(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: date, body: RegularizeIn):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    day = date
    original = db.attendance_logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if original is None:
        raise HTTPException(404, "attendance not found")
    if body.status in ("ABSENT", "LEAVE") and ("punch_in" in body.model_fields_set or "punch_out" in body.model_fields_set):
        raise HTTPException(422, "absence or leave cannot have punch times")
    status = body.status if "status" in body.model_fields_set else original["status"]
    if status is None:
        raise HTTPException(422, "status cannot be null")
    if status in ("ABSENT", "LEAVE"):
        start = end = None
    else:
        start = validate_optional_epoch(body, "punch_in") if "punch_in" in body.model_fields_set else original.get("punch_in")
        end = validate_optional_epoch(body, "punch_out") if "punch_out" in body.model_fields_set else original.get("punch_out")
        if start is None or attendance_day(start, employee) != day:
            raise HTTPException(422, "punch-in must belong to the attendance date")
        if end is not None and not start < end <= start + timedelta(hours=24):
            raise HTTPException(422, "punch-out must follow punch-in within 24 hours")
    values = {"status": status, "punch_in": start, "punch_out": end,
              **derived(employee, day, start, end)}
    changes = {key: {"from": original.get(key, False if key == "half_day" else
                                 0 if key in ("late_minutes", "overtime_minutes") else None), "to": value}
               for key, value in values.items()
               if original.get(key, False if key == "half_day" else
                               0 if key in ("late_minutes", "overtime_minutes") else None) != value}
    if not changes:
        raise HTTPException(422, "correction changes nothing")
    history = original.get("history", [])
    history_match = history if "history" in original else {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": original["_id"], "history": history_match, "status": original["status"],
         "punch_in": original.get("punch_in"), "punch_out": original.get("punch_out")},
        {"$set": values, "$push": {"history": {"at": now(), "by": body.regularized_by,
                                                 "reason": body.reason, "changes": changes}}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "attendance changed concurrently")
    return attendance_json(updated)


def month_bounds(month: str) -> tuple[str, str]:
    valid_month(month)
    try:
        start = date.fromisoformat(month + "-01")
    except ValueError:
        raise HTTPException(422, "month must contain a valid year")
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start.isoformat(), end.isoformat()


def weekday_expr(field="$date"):
    return {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": field}}}, [2, 3, 4, 5, 6]]}


def present_expr():
    return {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, weekday_expr()]},
                      {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def round_expr(value, places=2):
    multiplier = 10 ** places
    scaled = {"$multiply": [{"$toDecimal": value}, Decimal128(str(multiplier))]}
    rounded = {"$trunc": {"$add": [scaled, Decimal128("0.5")]}}
    return {"$toDouble": {"$divide": [rounded, Decimal128(str(multiplier))]}}


def employee_monthly_pipeline(emp_code: str, month: str):
    first, next_month = month_bounds(month)
    # Construct the calendar in MongoDB, including employees without any logs.
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"date": {"$gte": first, "$lt": next_month},
                        "$expr": {"$eq": ["$emp_code", "$$code"]}}},
            {"$group": {"_id": None, "present_days": {"$sum": present_expr()},
                        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
        ], "as": "totals"}},
        {"$set": {"totals": {"$ifNull": [{"$arrayElemAt": ["$totals", 0]}, {}]},
                  "calendar_start": {"$max": ["$joined_on", first]}}},
        {"$set": {"working_days": {"$size": {"$filter": {"input": {"$range": [0, {"$max": [0, {"$dateDiff": {
            "startDate": {"$dateFromString": {"dateString": "$calendar_start"}},
            "endDate": {"$dateFromString": {"dateString": next_month}}, "unit": "day"}}]}]},
            "as": "offset", "cond": {"$in": [{"$dayOfWeek": {"$dateAdd": {
                "startDate": {"$dateFromString": {"dateString": "$calendar_start"}},
                "unit": "day", "amount": "$$offset"}}}, [2, 3, 4, 5, 6]]}}}}}},
        {"$project": {"_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": 1,
                      "present_days": {"$ifNull": ["$totals.present_days", 0]},
                      "leave_days": {"$ifNull": ["$totals.leave_days", 0]},
                      "late_count": {"$ifNull": ["$totals.late_count", 0]},
                      "total_late_minutes": {"$ifNull": ["$totals.total_late_minutes", 0]},
                      "total_overtime_minutes": {"$ifNull": ["$totals.total_overtime_minutes", 0]},
                      "attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]},
                          round_expr({"$multiply": [{"$divide": [{"$ifNull": ["$totals.present_days", 0]},
                                                                        "$working_days"]}, 100]}), None]}}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    rows = list(db.employees.aggregate(employee_monthly_pipeline(emp_code, month)))
    if not rows:
        raise HTTPException(404, "employee not found")
    return rows[0]


def department_summary_pipeline(month: str, department: str | None):
    first, next_month = month_bounds(month)
    end = (date.fromisoformat(next_month) - timedelta(days=1)).isoformat()
    match = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"date": {"$gte": first, "$lt": next_month},
                        "$expr": {"$eq": ["$emp_code", "$$code"]}}},
            {"$group": {"_id": None, "present_days": {"$sum": present_expr()},
                        "hours_sum": {"$sum": {"$cond": [{"$and": [
                            {"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]},
                            "$work_hours", 0]}},
                        "hours_count": {"$sum": {"$cond": [{"$and": [
                            {"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]}, 1, 0]}},
                        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}}}
        ], "as": "totals"}},
        {"$set": {"totals": {"$ifNull": [{"$arrayElemAt": ["$totals", 0]}, {}]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
                    **{key: {"$sum": {"$ifNull": [f"$totals.{key}", 0]}} for key in
                       ("present_days", "hours_sum", "hours_count", "late_count", "total_late_minutes",
                        "leave_count", "on_duty_count")}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
                      "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]},
                          round_expr({"$divide": ["$hours_sum", "$hours_count"]}), None]},
                      "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: str | None = None):
    return {"month": month, "items": list(db.employees.aggregate(department_summary_pipeline(month, department)))}


def leaderboard_pipeline(month: str, limit: int, department: str | None):
    first, next_month = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lt": next_month}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                     limit: int = Query(10, ge=1, le=50), department: str | None = None):
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))}


def trend_pipeline(department: str, first: date, last: date):
    # A generated day series in the pipeline guarantees rows for empty days.
    days = (last - first).days + 1
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "employees": {"$push": {"code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$project": {"employees": 1, "day": {"$map": {"input": {"$range": [0, days]}, "as": "offset",
            "in": {"$dateAdd": {"startDate": datetime.combine(first, time.min, UTC),
                                 "unit": "day", "amount": "$$offset"}}}}}},
        {"$unwind": "$day"},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
                  "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]}}},
        {"$set": {"active": {"$filter": {"input": "$employees", "as": "employee",
            "cond": {"$lte": ["$$employee.joined_on", "$date"]}}}}},
        {"$set": {"headcount": {"$size": "$active"}}},
        {"$lookup": {"from": "attendance_logs", "localField": "date", "foreignField": "date",
            "let": {"codes": "$active.code"}, "pipeline": [
            {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]}}},
            {"$group": {"_id": None, "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", list(PRESENCE)]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}
        ], "as": "totals"}},
        {"$set": {"totals": {"$ifNull": [{"$arrayElemAt": ["$totals", 0]}, {}]}}},
        {"$set": {"present_count": {"$ifNull": ["$totals.present_count", 0]},
                  "late_count": {"$ifNull": ["$totals.late_count", 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            round_expr({"$divide": ["$present_count", "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_raw": {
            "$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1, "attendance_rate": 1,
                      "moving_avg_7d": {"$cond": [{"$eq": ["$moving_raw", None]}, None,
                                                       round_expr("$moving_raw", 4)]}}},
    ]


def check_range(first: date, last: date):
    if last < first or (last - first).days + 1 > 92:
        raise HTTPException(422, "date range must contain 1 to 92 days")


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: date = Query(..., alias="from"),
                     date_to: date = Query(..., alias="to")):
    check_range(date_from, date_to)
    if db.employees.find_one({"department": department}) is None:
        raise HTTPException(404, "department not found")
    return {"department": department,
            "items": list(db.employees.aggregate(trend_pipeline(department, date_from, date_to)))}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                                        "late_leaderboard", "department_trend"],
                     emp_code: str | None = None, month: str | None = None, department: str | None = None,
                     limit: int = Query(10, ge=1, le=50), date_from: date | None = None,
                     date_to: date | None = None, status: Status | None = None,
                     from_day: date | None = Query(None, alias="from"),
                     to_day: date | None = Query(None, alias="to"),
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint == "attendance_list":
        filt = attendance_filter(emp_code, str(date_from) if date_from else None,
                                 str(date_to) if date_to else None, status)
        collection = "attendance_logs"
        operation = {"find": collection, "filter": filt, "sort": {"date": -1, "emp_code": 1},
                     "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint in ("employee_monthly", "department_summary", "late_leaderboard"):
            if month is None:
                raise HTTPException(422, "month is required")
            try:
                valid_month(month)
            except ValueError as exc:
                raise HTTPException(422, str(exc))
        if endpoint == "employee_monthly":
            if emp_code is None:
                raise HTTPException(422, "emp_code is required")
            collection, pipeline = "employees", employee_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            collection, pipeline = "employees", department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            collection, pipeline = "attendance_logs", leaderboard_pipeline(month, limit, department)
        else:
            if department is None or from_day is None or to_day is None:
                raise HTTPException(422, "department, from and to are required")
            check_range(from_day, to_day)
            collection, pipeline = "employees", trend_pipeline(department, from_day, to_day)
        operation = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    result = db.command("explain", operation, verbosity="executionStats")
    # FastAPI's JSON encoder cannot directly serialize BSON ObjectIds/datetimes.
    from bson.json_util import dumps
    import json
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(dumps(result))}
