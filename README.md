# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in a local .env
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotent). `GET /health` is 200 once MongoDB answers.

## Notes
- All endpoints in `openapi.yaml` are implemented, including `/admin/explain/{endpoint}`.
- Analytics read the **stored** derived values and never recompute them. Reported numbers are rounded half-up with exact
  integer / `Decimal` arithmetic (the trend computes rates in ten-thousandths inside the pipeline).
- Assumptions where the contract is open: `moving_avg_7d` averages the reported (4-decimal) `attendance_rate` values;
  "range exceeds 92 days" means more than 92 calendar days counted inclusively; a `PATCH` that only restates the stored
  values but would change a stale derived value is accepted (it changes something), one that changes nothing is 422;
  a losing concurrent `PATCH` gets 409 (compare-and-set on history length, status and punch times).
- Requires MongoDB 6.0+ (`$setWindowFields`, `$densify`, `$rank`, `$lookup` with `localField` and `pipeline`).
