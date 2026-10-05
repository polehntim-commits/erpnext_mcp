**Deploy: FarmOps 0.38.8 (build 33) — due dates on tasks and Today (server v0.236.0 or later)**

App release. Ship instead of 0.38.7 (it contains it). On a server before v0.236.0 tasks simply show no dates.

**1. Build (Tim)**

Archive and ship FarmOps 0.38.8 (build 33) from fafo_ios main, scheme **FarmOps**.

**2. Phone checks**

1. Give a task a due date (`set_task_dates`, or a template with default due days). Its row shows "Due in 2 days";
   past the date it shows "Overdue" in red. Computed on the phone, so it is right offline too.
2. Today has a **Due** section: overdue first, then due in the next 2 days (the 06:00 reminder band, decision 32).
   Empty when nothing is due.
3. Spanish reads in tú.

**3. Rollback**

Ship 0.38.7. Nothing stored changes.
