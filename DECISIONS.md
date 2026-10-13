# Design decisions

1. **Indexes.** Unique `employees.emp_code` and unique `(attendance_logs.emp_code, date)` enforce identities even under concurrent requests. Employee `department, joined_on` and `joined_on` support filtered headcount scans. Log `date, emp_code` supports the global list, monthly leaderboard, and trend lookups; `emp_code, date` supports employee history and monthly lookups. `status, date, emp_code` supports status-filtered lists, and `emp_code, punch_in` finds the latest eligible punch-in. I considered a separate index on `late_minutes`, but the month range is the selective predicate, so its write cost was not justified.

2. **Punch-in race.** Both requests may read the employee. Both try to insert the same `(emp_code, date)`. MongoDB's unique index accepts one insert; the other raises `DuplicateKeyError`, which becomes 409. There is no vulnerable existence check.

3. **Ties.** `$rank` assigns tied totals the same competition rank. Filtering by rank after ranking includes every employee at the cutoff, even if the result has more than `limit` rows. A final `emp_code` sort orders tied rows consistently.

4. **Headcount.** The summary begins with `employees`, filters by `joined_on`, and looks up each person's logs. Thus employees with no records remain in the department group.

5. **At 100x scale.** I would benchmark `explain` plans and partition or preaggregate historical monthly metrics, while keeping current records and their audit trail authoritative.
