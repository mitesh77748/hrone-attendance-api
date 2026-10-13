# Employee Attendance and Analytics API

Python 3.11+, FastAPI, and MongoDB 6.0+.

1. Set `MONGO_URI` and `MONGO_DB` in the environment. Defaults are `mongodb://localhost:27017` and `attendance_db` for local development. A local `.env` can also be used; real environment variables take priority.
2. Install with `python -m pip install -r requirements.txt`.
3. Start with `uvicorn app.main:app --port 8000` from this directory. Startup creates the required indexes. `GET /health` checks MongoDB readiness.

The OpenAPI UI is at `/docs`. Instants in requests and responses are integer epoch milliseconds. Calendar dates and shift times use IST. All endpoints in the supplied contract are implemented.

For a local smoke test, create an employee through `POST /employees`, punch in, and punch out. An actual MongoDB server is required for aggregation and concurrency checks.
