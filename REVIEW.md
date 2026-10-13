# Starter code review

| # | Where | Defect | How to notice | Fix |
|---|---|---|---|---|
| 1 | `health` | Reports ready without contacting MongoDB. | Stop MongoDB; health still returns 200. | Ping database and return 503 on failure. |
| 2 | `create_employee` | Check then insert races, and uniqueness is not enforced. | Two parallel creates can both return 201. | Unique `emp_code` index; catch duplicate key. |
| 3 | `create_employee` | Uses naive local `datetime.now()` and returns a datetime instead of epoch milliseconds. | Run in a non-UTC host zone. | Use aware UTC time and serialize to milliseconds. |
| 4 | `EmployeeIn` | Accepts malformed codes, email, dates and shift times, including equal shift endpoints. | Submit `EMPx`, invalid date, or `09:30`/`09:30`. | Field constraints and date/shift validation. |
| 5 | `list_employees` | `skip = page * page_size` skips page one. | Default list misses first 20 employees. | Use `(page-1)*page_size`. |
| 6 | `list_employees` | Total ignores department filter. | Filter to one department; total stays global. | Count the filtered query. |
| 7 | `list_employees` | No deterministic sort or pagination bounds. | Paging can repeat or miss employees; zero page accepted. | Sort by code and validate page/page size. |
| 8 | `compute_late_minutes` | Applies shift time to the UTC/naive punch date and treats 10:01 grace incorrectly through truncation. | 09:40:01 IST should be late by 10 minutes. | Build IST shift start; compare exact elapsed seconds before flooring minutes. |
| 9 | `compute_work_hours` | Python `round` uses ties-to-even and binary floats. | Exact half-cent-hour case rounds wrongly. | Decimal half-up on whole seconds. |
| 10 | `compute_overtime` | Ignores overnight shift end and 30-minute minimum. | 22:00–06:00 shift out at 06:40. | Resolve next-day IST shift end; apply minimum. |
| 11 | `punch_in` | Unknown employee causes a 500. | Punch code not in `employees`. | Return 404. |
| 12 | `punch_in` | `if punched_at` treats zero as omission; seconds-looking, floats and strings are accepted or crash. | Send `punched_at: 1234567890`. | Strict integer and range validation. |
| 13 | `punch_in` | Uses local date, not IST attendance day; misses overnight attribution. | 02:00 IST on an overnight shift. | Convert to IST and apply R1. |
| 14 | `punch_in` | Check then insert races; status accepts ABSENT or LEAVE. | Parallel calls or `status: LEAVE`. | Unique natural-key index and presence-status validation. |
| 15 | `punch_in` | Returns an undocumented `id` and raw BSON datetime fields. | Compare response with OpenAPI. | Omit `_id` and serialize instants. |
| 16 | `list_attendance` | Reads and sorts the whole collection in Python. | 100k records cause large memory use and slow pages. | Indexed database sort, skip and limit. |
| 17 | `list_attendance` | Missing page bounds, date-range validation and exact response serialization. | `page=0`, reversed dates, legacy record. | Validate; normalize missing fields and nested history times. |

The starter's use of `emp_code` to look up employees and `load_dotenv()` without overriding existing environment variables were correct, so I kept those conventions.
