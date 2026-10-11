"""
Employee Attendance & Analytics API - COMPLETE

Run:  uvicorn app.main:app --port 8000 --reload
Env:  MONGO_URI, MONGO_DB
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field
from pymongo import MongoClient, ASCENDING, DESCENDING

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    # Make both timezone-aware (IST) if they are naive
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=IST)
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=IST)
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=IST)
        
    return max(0, int((punch_out - end).total_seconds() / 60))


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).astimezone(IST)
    return dt.astimezone(IST)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeIn(BaseModel):
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str


# --------------------------------------------------------------------------- #
# Basic Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(IST)
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = (
        datetime.fromtimestamp(body.punched_at / 1000, tz=IST)
        if body.punched_at
        else datetime.now(IST)
    )
    d = ts.date().isoformat()

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    res = db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = (
        datetime.fromtimestamp(body.punched_at / 1000, tz=IST)
        if body.punched_at
        else datetime.now(IST)
    )
    d = ts.date().isoformat()

    log = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d})
    if not log:
        raise HTTPException(404, "no punch-in found for this date")
    if log.get("punch_out") is not None:
        raise HTTPException(409, "already punched out for this date")

    work_hours = compute_work_hours(log["punch_in"], ts)
    overtime = compute_overtime(ts, emp["shift_end"], d)

    update = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "half_day": work_hours < 4,
    }

    db.attendance_logs.update_one({"_id": log["_id"]}, {"$set": update})

    log.update(update)
    log["id"] = str(log.pop("_id"))
    return log


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q)
        .sort("date", DESCENDING)
        .skip(skip)
        .limit(page_size)
    )
    for d in docs:
        d["id"] = str(d.pop("_id"))
    return {"items": docs, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not log:
        raise HTTPException(404, "attendance record not found")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    history_entry = {
        "regularized_at": datetime.now(IST),
        "regularized_by": body.regularized_by,
        "reason": body.reason,
        "old_punch_in": log.get("punch_in"),
        "old_punch_out": log.get("punch_out"),
    }

    update = {}
    if body.punch_in is not None:
        new_in = datetime.fromtimestamp(body.punch_in / 1000, tz=IST)
        update["punch_in"] = new_in
        update["late_minutes"] = compute_late_minutes(new_in, emp["shift_start"])

    if body.punch_out is not None:
        new_out = datetime.fromtimestamp(body.punch_out / 1000, tz=IST)
        update["punch_out"] = new_out

    # Recalculate work_hours & overtime if both times present
    final_in = update.get("punch_in", log.get("punch_in"))
    final_out = update.get("punch_out", log.get("punch_out"))
    if final_in and final_out:
        update["work_hours"] = compute_work_hours(final_in, final_out)
        update["overtime_minutes"] = compute_overtime(final_out, emp["shift_end"], date)
        update["half_day"] = update["work_hours"] < 4

    db.attendance_logs.update_one(
        {"_id": log["_id"]},
        {
            "$set": update,
            "$push": {"history": history_entry},
        },
    )

    updated = db.attendance_logs.find_one({"_id": log["_id"]})
    updated["id"] = str(updated.pop("_id"))
    return updated


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-\d{2}$")):
    """month format: YYYY-MM"""
    start = f"{month}-01"
    # simple next month
    y, m = map(int, month.split("-"))
    if m == 12:
        end = f"{y+1}-01-01"
    else:
        end = f"{y}-{m+1:02d}-01"

    logs = list(
        db.attendance_logs.find(
            {"emp_code": emp_code, "date": {"$gte": start, "$lt": end}}
        )
    )

    total_days = len(logs)
    present_days = sum(1 for l in logs if l.get("status") == "PRESENT")
    late_days = sum(1 for l in logs if l.get("late_minutes", 0) > 0)
    total_late_minutes = sum(l.get("late_minutes", 0) for l in logs)
    total_work_hours = sum(l.get("work_hours") or 0 for l in logs)
    total_overtime = sum(l.get("overtime_minutes", 0) for l in logs)
    half_days = sum(1 for l in logs if l.get("half_day"))

    return {
        "emp_code": emp_code,
        "month": month,
        "total_days": total_days,
        "present_days": present_days,
        "late_days": late_days,
        "half_days": half_days,
        "total_late_minutes": total_late_minutes,
        "total_work_hours": round(total_work_hours, 2),
        "total_overtime_minutes": total_overtime,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-\d{2}$")):
    start = f"{month}-01"
    y, m = map(int, month.split("-"))
    end = f"{y+1}-01-01" if m == 12 else f"{y}-{m+1:02d}-01"

    pipeline = [
        {"$match": {"date": {"$gte": start, "$lt": end}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$group": {
                "_id": "$emp.department",
                "total_employees": {"$addToSet": "$emp_code"},
                "total_present": {
                    "$sum": {"$cond": [{"$eq": ["$status", "PRESENT"]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "total_employees": {"$size": "$total_employees"},
                "total_present_days": "$total_present",
                "total_late_minutes": 1,
                "total_work_hours": {"$round": ["$total_work_hours", 2]},
                "total_overtime_minutes": 1,
                "_id": 0,
            }
        },
        {"$sort": {"department": 1}},
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "departments": result}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(10, ge=1, le=50),
):
    start = f"{month}-01"
    y, m = map(int, month.split("-"))
    end = f"{y+1}-01-01" if m == 12 else f"{y}-{m+1:02d}-01"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start, "$lt": end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_days": {"$sum": 1},
            }
        },
        {"$sort": {"total_late_minutes": -1}},
        {"$limit": limit},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$project": {
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_days": 1,
                "_id": 0,
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "leaderboard": result}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    pipeline = [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "PRESENT"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "date": "$_id",
                "present_count": 1,
                "late_count": 1,
                "total_work_hours": {"$round": ["$total_work_hours", 2]},
                "_id": 0,
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    return {
        "department": department,
        "from": from_date,
        "to": to_date,
        "trend": result,
    }


# --------------------------------------------------------------------------- #
# Admin / Explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str):
    """
    Returns a simple explanation of which indexes would be used.
    In a real system this would call MongoDB explain().
    """
    explanations = {
        "attendance_list": {
            "endpoint": "GET /attendance",
            "recommended_indexes": [
                {"emp_code": 1, "date": -1},
                {"date": 1},
                {"status": 1},
            ],
            "expected_plan": "IXSCAN on {emp_code:1, date:-1} when emp_code filter is present",
            "note": "Create indexes for better performance",
        },
        "employee_monthly": {
            "endpoint": "GET /analytics/employees/{emp_code}/monthly",
            "recommended_indexes": [{"emp_code": 1, "date": 1}],
            "expected_plan": "IXSCAN on {emp_code:1, date:1}",
        },
        "department_summary": {
            "endpoint": "GET /analytics/departments/summary",
            "recommended_indexes": [{"date": 1}],
            "expected_plan": "COLLSCAN or IXSCAN on date + $lookup",
        },
    }

    if endpoint not in explanations:
        raise HTTPException(404, f"No explanation available for '{endpoint}'")

    return explanations[endpoint]


# --------------------------------------------------------------------------- #
# Create useful indexes on startup
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)])
    db.attendance_logs.create_index("date")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)])