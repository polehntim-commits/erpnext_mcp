# Crew tasks — contract

Server `erpnext_mcp` **v0.213.0**, app **0.25.0**. Frozen before code. AFB-2026-00030 follow-on
("So people can be added to a crew task as well please").

## 0. The problem

A Farm Task has one live holder. "These twelve people are pruning Block 4" cannot be said:
`assign_farm_task` sends one person, and a second assignment is refused by the doctype itself.

## 1. Model — no new doctype

**Farm Task** gains

| Field | Type | Meaning |
|---|---|---|
| `work_mode` | Select `Individual` / `Crew` | Default `Individual`. |
| `is_crew_task` | Check | Kept equal to `work_mode == "Crew"` by the controller; either may be sent. |
| `crew_piece_unit` | Data | What this task counts: `trees`, `bins`, `rows`, `buckets`, or any word. |
| `crew_sections` | Code (JSON) | §6. |
| `crew_headcount`, `crew_on_now`, `crew_person_minutes`, `crew_pieces` | Int/Int/Int/Float | The roll-up, stamped on every crew change and at close, so a report can query it. |

**Farm Task Template** gains `work_mode`, `is_crew_task`, `crew_piece_unit`, `crew_sections`;
`create_task_from_template` copies them. `create_farm_task` accepts the same four.

**Farm Task Assignment** — one row per worker per stint — gains two states and eight fields:

- States `On Crew` and `Off Crew`. They are **not** live states: the one-live-assignment rule,
  the claim limit and "what is this worker holding" do not see them.
- `end_reason` (Small Text), `added_by`, `ended_by` (Data — the login), `part_done` (Check),
  `pieces` (Float), `piece_unit` (Data), `section` (Data), `member_notes` (Small Text),
  `client_request_id` (Data).
- A crew row uses the columns that already exist: `claimed_at` = `started_at` = when they were
  put on; `completed_at` = when they came off; `actual_duration_minutes`; `farm_shift` = the open
  shift they were clocked into at that moment (null, with a warning, when they were on none).

**Individual tasks are unchanged**: same fields, same one-holder rule, same answers.

### The lead

A crew task still has exactly one ordinary holder assignment — **the lead**: the supervisor who
starts it and closes it. `assigned_to` on the task is the lead. Crew rows are everybody doing the
work. The lead is not automatically a crew member (their minutes are not labour on the block
unless they are added too).

- First crew added and no lead yet → the lead is `lead` if given, else the caller's own Employee.
  With neither, the add is refused and says to name one.
- The lead's assignment is started by the first add (through `start_farm_task`, so every guard
  on starting — a new spray inside the pre-harvest interval, an unsigned `before_start` approval,
  the bait notice — refuses the add).
- Starting or leading a crew task does not auto-pause the lead's other work, and starting other
  work does not pause a crew task. A foreman may lead two crews at once.
- `claim_farm_task` refuses a crew task: nobody takes a crew job from the pool; they are put on it.

## 2. Adding and removing — `add_to_crew_task`, `remove_from_crew_task`

**`add_to_crew_task(task, employees?, badge_ids?, shift?, lead?, section?, client_request_id?)`**

- `shift` adds everybody currently on that Farm Shift. `employees` and `badge_ids` add
  individuals. Any mix; at least one of the three (or `lead` alone, to set or change the lead).
- The task must be a crew task, and not Draft or finished.
- Each person is answered on their own line:
  `{employee, employee_name, badge_id?, outcome: "added"|"already"|"refused", reason?, assignment?,
  farm_shift, moved_from?, warning?}`. One refusal does not stop the rest.
- `already` when they are On Crew on this task — which is what makes a repeat safe.
- Somebody On Crew on **another** crew task is moved: that row is closed at this moment with
  `end_reason` "Moved to <task>", and the line carries `moved_from`. Nobody's minutes are on two
  tasks at once.
- Not clocked into any shift → still added, with `warning` and `farm_shift: null`.
- Answer: `{task, lead, started, results, added, already, refused, crew}` (`crew` is §3's block).
- Those added get the ordinary task push.

**`remove_from_crew_task(task, employees?, badge_ids?, all?, reason?, ended_at?, client_request_id?)`**

- Closes each person's On Crew row: `Off Crew`, `completed_at` (now, or `ended_at` — never before
  they started, never in the future), `actual_duration_minutes`, `end_reason`, `ended_by`.
  **Nothing is deleted**; the row is the history. Coming back later is a new row.
- Lines as above with outcome `removed` | `already` (not on the crew) | `refused`.

**Closed for them** — never deleted — when: they are removed; they are moved; they leave or are
taken off the shift their row names, or that shift ends; they mark their part done (§4); the task
is completed, rejected, cancelled or merged.

**`assign_farm_task` on a crew task** adds the person named to the crew (the same code, one
line of result) instead of replacing a holder; `reassign`/`reason` are not asked for. Its answer
carries `crew` and `already`. On an individual task it is exactly what it was.

## 3. Time, pieces, notes — the roll-up

`crew` (on `get_farm_task`, `list_crew_task_members`, every add/remove answer, and on the mobile
task shape as `crew`):

```
{ is_crew_task, lead, lead_name, piece_unit,
  on_now, headcount, person_minutes, pieces, buckets,
  members: [ { assignment, employee, employee_name, state: "On Crew"|"Off Crew", on_now,
               started_at, ended_at, minutes, farm_shift, pieces, piece_unit, buckets,
               section, part_done, notes, end_reason, qualified?, missing? } ],
  sections: [...], progress: {...} | null }
```

- `minutes`: closed rows, their stored duration; open rows, started → now.
- `pieces`: what a supervisor (or the member) entered on the row with
  **`update_crew_task_member(task, employee, pieces?, add_pieces?, piece_unit?, notes?, section?,
  part_done?)`** — trees, bins, rows.
- `buckets`: read, not entered — Bucket Log Entry rows for that picker inside their time on the
  task (the existing BucketLog path). Present only on a site with that doctype.
- `notes`: `member_notes` per row; the task's own notes and Task Notes are untouched.
- The four roll-up columns on the task are re-stamped on every change.

**Labour cost per block needs nothing new.** The payroll cost-centre split (`payroll_gl`,
v0.101.0) already reads every Farm Task Assignment by `assigned_to`, `started_at` and minutes and
places it on the task's block. Crew rows are assignments with exactly those columns, so twelve
people pruning Block 4 put twelve people's minutes on Block 4's cost centre.

**Pieces are not pay.** `pieces` is a new column deliberately **not** one of the names the
payroll run reads units from, so entering "40 trees" changes nobody's wage. Piece-rate pay still
comes from bucket scans. Making crew pieces payable is Tim's decision (§9).

## 4. Completion

- **One close, by the lead**, through the existing `complete_farm_task` — one evidence set
  (photos, findings, signature), one compliance record. Crew rows are then closed at the
  completion time with `end_reason` "Task completed", and the roll-up is stamped.
- A supervisor (Foreman, Farm Manager, Crew Leader) who is not the lead may close it: the lead
  passes to them in the same call and the old lead's assignment says so. A crew member who is
  not a supervisor is refused.
- A member never completes the task. A member may mark **my part done**
  (`update_crew_task_member` with `part_done: true` on their own row): the row closes with
  `end_reason` "Part done". The task stays open.
- Reject, cancel and merge close the crew rows the same way, with their own reason.

## 5. Certification, per person

Every add runs the same check `assign_farm_task` refuses on (`required_certification` plus what
the task's products need) and the under-18 bar for the task type. Someone who fails is a
`refused` line with the reason; nobody else is held up. `list_assignable_workers` on a crew task
adds `on_this_task` to each person, and still lists the unqualified with `missing`.

## 6. Sections

`crew_sections`: `[{key, label, from_row?, to_row?, done, done_at, done_by}]`. Set at creation
(`sections` on `create_farm_task` / the template) or later with
**`update_crew_task_sections(task, sections?, section?, done?)`** — `sections` replaces the list
(keeping the done-state of keys that survive); `section` + `done` ticks one.
`progress`: `{done, total, percent, summary}` — summary "Rows 1–20 done; rows 21–40 open".
A member row's `section` says which one they are on. Sections are optional and nothing requires
them at close.

## 7. Who may

| Act | Who |
|---|---|
| add / remove / set lead / sections / another member's row | **Foreman, Farm Manager, Crew Leader** (and System Manager), checked in the tools, so the MCP path and the phone path share one gate; entity scope applies. |
| Own row: `part_done`, `pieces`, `notes` | The member. |
| `list_crew_task_members` | The roles above, and a member of that task's crew. |
| Close the task | The lead, or one of the roles above. |

The four mutating MCP tools are **default OFF** (`allow_add_to_crew_task`, …).

**Offline.** Add and remove are safe to send twice by construction (`already`);
`client_request_id` is stored on the row for the trail. The phone queues both with one id.

## 8. Surfaces

**MCP tools (5):** `add_to_crew_task`, `remove_from_crew_task`, `update_crew_task_member`,
`update_crew_task_sections` (mutating), `list_crew_task_members` (read). `assign_farm_task`,
`create_farm_task`, `create_farm_task_template`/`update_farm_task_template`,
`create_task_from_template`, `get_farm_task`, `claim_farm_task`, `complete_farm_task` change as
above. Tools: 973 → **978** (477 read, 501 write).

**Mobile routes (6):** the five above plus `list_crew_tasks(company?)` — open crew tasks with
`crew` counts, for the roles in §7. 158 → **164**. `list_my_tasks` also returns the crew tasks
the caller is On Crew on (marked `my_crew_role: "member"`; the lead's own are `"lead"`).
Every task the phone is handed carries `is_crew_task`, `work_mode`, and `crew` when it is one.

**Tile:** report `crew_tasks`, seeded on **work** for the §7 roles (`min_app_version` 0.25.0).

**Phone:** a crew task shows its crew, lead, sections and totals. Assign on a crew task is
multi-select (roster + badge scan + "whole shift"), unqualified shown and not selectable;
swipe a member off; a member sees "My part is done". Complete is offered to the lead and to
supervisors only.

## 9. Decisions left for Tim

1. **Crew pieces are not payable** (§3). Reverse = name the column `piece_units` or add a switch.
2. **Crew Leader may add and remove crew.** Narrower = Foreman and Farm Manager only.
3. **Unpaid breaks are not subtracted** from a member's minutes on the task; the shift's
   attendance is the wage record, and the cost split already scales to paid hours.
4. **A person is on one crew task at a time** (adding to a second moves them).
