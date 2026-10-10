# DECISIONS.md

1. **Indexes.** `attendance_logs`: unique `(emp_code, date)` (race guard, per-employee lookups, the `$lookup` join); `(date -1, emp_code)` and `(status, date -1, emp_code)` serve the list's filters and sort; `(date, late_minutes, emp_code)` covers the leaderboard grouping. `employees`: unique `emp_code`, `(department, emp_code)`, `(department, joined_on)` and `(joined_on)` for headcount. Rejected: `(emp_code, punch_in)` for punch-out, because date order equals punch-in order and the unique index already serves it.
2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. The unique index lets exactly one succeed; the other raises `DuplicateKeyError` and returns 409. There is no read-before-write.
3. **Ties.** Ranking uses `$rank` (1, 2, 2, 4) and `limit` filters on rank afterwards, so a tie at the cutoff returns all tied employees, possibly more than `limit` rows, ordered by `emp_code`.
4. **Headcount.** The pipeline starts from `employees`, not from logs, and `$lookup`s each person's logs with null-preserving `$unwind`; no logs just means zero contributions.
5. **100x data.** Pre-aggregate daily per-employee and per-department totals (updated on write) instead of scanning raw logs, and replace skip-based pagination with keyset pagination.
