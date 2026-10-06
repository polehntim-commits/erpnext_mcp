**Deploy: catch-up v0.231.2 → v0.252.0 — one build, ONE migrate; FarmOps 0.38.7 → 0.43.0 (build 38)**

For both servers, which run v0.231.2 with FarmOps 0.38.7 (build 32). One image covers v0.231.3 through v0.252.0, and
a single `bench migrate` runs everything those releases need, in order. Every step is idempotent; everything new
ships **OFF** unless it is a read. Pay is unchanged (v0.235.0 moved the overtime rule into data with identical
output, proven on the payroll suite).

- **Server**: erpnext_mcp main **`f25973d`** (`__version__ = "0.252.0"`).
- **App**: fafo_ios main **`2f1ac6c`** — FarmOps **0.43.0 (build 38)**, scheme **FarmOps**. It needs v0.252.0's
  routes, so deploy the server first.
- v0.232.0 was never released: it shipped as v0.244.0 (upload links).

**What each release added, in plain words**

| Release | What it adds |
|---|---|
| v0.231.3 | Polish: the inbox counts per company correctly; spray, alert and task lists load in far fewer queries. |
| v0.233.0 | Rules can hold a "condition tree" (all / any / not over named values) evaluated by one generic engine. |
| v0.234.0 | One set of read tools for every kind of configuration: list, get, compare versions, preview. |
| v0.234.1 | The matching write tools (draft, stage, publish, roll back). AI-drafted versions are published only by a person. "Work Timing" rule category. Foremen may scan business cards. |
| v0.235.0 | The overtime rule (40 h/week, 1.5×) is stored as a setting with versions and a pay preview; pay output identical. |
| v0.236.0 | Tasks get start and due dates and "blocked by"; overdue list; an overdue rule (seeded OFF). |
| v0.237.0 | Daily equipment checks: the operator checks the machine before use (three check templates; switch OFF). |
| v0.238.0 | The Reference Library: PDFs searchable to the page and citable from SOPs, templates, training and rules. |
| v0.239.0 | Rules can read each block's weather forecast (rain risk this week, frost, wind) and its latest crop stage. Off until "Block Forecasts for Work Timing". |
| v0.240.0 | Go / Hold on tasks: checked at 06:00 and at start, clears itself, Advisory by default; a supervisor can override an Enforced Hold with a reason. Presets seeded OFF. |
| v0.241.0 | Record a block's crop stage (BBCH) and see its timeline; missing, unreadable or backwards stages are flagged, not refused. |
| v0.242.0 | Suggested tasks: Go-today work, with "window closing" (Go today, Hold tomorrow) first. |
| v0.243.0 | No-work notices: drafted the evening before for the supervisor, escalated to management if unanswered, never sent automatically. Switch OFF. |
| v0.244.0 | One-time upload links for large files (PDFs, photos) onto one record; no video; files private unless an admin allows public. Master switch OFF. |
| v0.245.0 | SOP review and approval: submit, approvers by the work or position the SOP covers, approval with a signature, new versions replace old ones only when approved. |
| v0.246.0 | Course videos per training course, and how much each person watched (approximate). |
| v0.247.0 | Knowledge checks (quizzes) per course, graded on the server; a trainer signs off and the training record is filed. Switch OFF. |
| v0.248.0 | Irrigation schedule per zone; each day's sets become Irrigation tasks (switch OFF); planned vs what the valves actually ran. |
| v0.249.0 | Growing degree days per block, and a stage estimate once a stage table is written in settings (none built in). |
| v0.250.0 | The phone's doors to all of the above (14 routes); tasks carry Go / Hold to the phone. |
| v0.251.0 | Punch review is a compliance item (AFB-2026-00032): missing clock-outs, late offline punches, edits, GPS outside the block, short breaks and Oregon ag overtime raise compliance alerts; a supervisor approves or fixes each punch or the whole period; reviewed punches lock for payroll; payroll preview warns on unreviewed ones; the DOL packet lists every review. Eight rules seeded ON. |
| v0.252.0 | Each person's own Start of Day and End of Day check (End of Day files hour-meter readings and crop stages; switch OFF). Fix: merging a business card into an existing contact now updates where and when you met and the company; the contact's address is reused and linked to the new Supplier. Phone routes to find a contact and correct where you met. |
| app 0.38.8 | Due dates and overdue on task rows and a Due section on Today, computed on the phone (works offline). |
| app 0.39.0 | Go / Hold chips and card, supervisor override, crop-stage picker (offline), Today: work notices, notices to decide, "Good to do today", SOPs to review; approved SOPs in "How this job is done". Spanish (tú). |
| app 0.40.0 | The course player: course videos with how much was watched, the knowledge check, trainer sign-off. |
| app 0.41.0 | Punch review for supervisors: this week's punches, why each is flagged, approve or fix with a reason; "Punches to review" on Today. |
| app 0.42.0 | My day: Start / End of Day on Today. Contacts: find a contact and correct where you met (Foreman+). |
| app 0.43.0 | Spanish sweep: 133 longer explanations that always showed in English now read in Spanish (tú). |

**1. Build (Tim)**

Push erpnext_mcp main (`f25973d`), build the fafo-erpnext image with `ERPNEXT_MCP_VERSION=0.252.0`. Push fafo_ios main
(`2f1ac6c`) and archive FarmOps 0.43.0 (build 38), scheme **FarmOps**.

**2. Each server: pull, restart, ONE migrate** — umbrel.local first, then OML (orchardmeadow-umbrel) when Tim
chooses. The full command list is in `~/Desktop/deploy-catchup-latest.txt`.

Expect from the migrate (first run only):

1. "payroll setting payroll_setting:overtime_rule@1 seeded — 40 hours a workweek, 1.5×, as before."
2. "rule farm_task_due seeded OFF …"
3. "daily check templates seeded: Daily equipment check, Daily check add-on — Tractor, Daily check add-on — Mini
   Excavator, Start of Day — me, End of Day — me …"
4. "Go / Hold presets seeded OFF: go_hold_pruning_canker, go_hold_spray_wind, go_hold_burn, go_hold_sop_approved,
   go_hold_irrigation_rain, go_hold_harvest_heat."
5. "course videos moved in from video_url: …" only where a course had a video URL.
6. "punch review rules seeded: punch_missing_clock_out, punch_runaway, punch_offline_window, punch_edited,
   punch_outside_block, punch_breaks_short, overtime_week_approaching, overtime_week_exceeded."
7. `__version__ = "0.252.0"`.

**3. Checks after the migrate**

1. `get_server_status`: 0.252.0. Desk → any Asset Register form: the map's build stamp reads 0.252.0.
2. Payroll: `preview_payroll_for_period` on last week matches the posted run (identical pay).
3. Desk → Compliance Rule: six Go / Hold presets, all disabled. ERPNext MCP Settings: every new write switch unticked;
   No-Work Notices, Knowledge Checks, Irrigation Schedule, Upload Links Enabled, Daily Equipment Checks, Personal Day Checks unticked.
4. Phone 0.43.0: Today and a task open normally; a task with no rule shows no Go / Hold chip. Business card scan
   works for a Foreman.
5. Punch review (`v0.251.0_punch_review.md`): this week's flagged punches appear as compliance alerts and under
   "Punches to review" on Today for a Foreman / Farm Manager; `preview_payroll_for_period` warns on unreviewed punches
   with the pay figures unchanged.
6. Business-card merge (`v0.252.0_day_checks.md` §5): on OML, re-run the Ben Sheppard merge with Met At
   "Sheppard's, 440 Riverside Dr, Hood River, OR 97031" (or correct it from the phone: Contacts → Where met); Met At
   now reads that, and Sheppards-Office-1 links to Supplier Sheppard's.
7. Per feature, when Tim turns it on: the release's own file (`v0.236.0_task_dates.md` … `v0.252.0_day_checks.md`,
   `app_0.39.0_work_timing.md`, `app_0.40.0_course_player.md`, `app_0.41.0_punch_review.md`,
   `app_0.42.0_my_day_contacts.md`, `app_0.43.0_spanish_sweep.md`).

**4. Rollback**

Everything is additive and off by default. An older image ignores the new doctypes and fields; FarmOps 0.38.7 ignores
the new routes. Punch-review rules switch off as data (Desk → Compliance Rule).
