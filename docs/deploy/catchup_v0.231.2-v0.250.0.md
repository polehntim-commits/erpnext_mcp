**Deploy: catch-up v0.231.2 → v0.250.0 — one build, ONE migrate; FarmOps 0.38.7 → 0.39.0 (build 34)**

For both servers, which run v0.231.2 with FarmOps 0.38.7 (build 32). One image covers v0.231.3 through v0.250.0, and
a single `bench migrate` runs everything those releases need, in order. Every step is idempotent; everything new
ships **OFF** unless it is a read. Pay is unchanged (v0.235.0 moved the overtime rule into data with identical
output, proven on the payroll suite).

- **Server**: erpnext_mcp main **`d2c6575`** (`__version__ = "0.250.0"`).
- **App**: fafo_ios main **`340c61d`** — FarmOps **0.39.0 (build 34)**, scheme **FarmOps**. It needs v0.250.0's
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
| app 0.38.8 | Due dates and overdue on task rows and a Due section on Today, computed on the phone (works offline). |
| app 0.39.0 | Go / Hold chips and card, supervisor override, crop-stage picker (offline), Today: work notices, notices to decide, "Good to do today", SOPs to review; approved SOPs in "How this job is done". Spanish (tú). |

**1. Build (Tim)**

Push erpnext_mcp main (`d2c6575`), build the fafo-erpnext image with `ERPNEXT_MCP_VERSION=0.250.0`. Push fafo_ios main
(`340c61d`) and archive FarmOps 0.39.0 (build 34), scheme **FarmOps**.

**2. Each server: pull, restart, ONE migrate** — umbrel.local first, then OML (orchardmeadow-umbrel) when Tim
chooses. The full command list is in `~/Desktop/deploy-catchup-latest.txt`.

Expect from the migrate (first run only):

1. "payroll setting payroll_setting:overtime_rule@1 seeded — 40 hours a workweek, 1.5×, as before."
2. "rule farm_task_due seeded OFF …"
3. "daily check templates seeded: Daily equipment check, Daily check add-on — Tractor, Daily check add-on — Mini
   Excavator …"
4. "Go / Hold presets seeded OFF: go_hold_pruning_canker, go_hold_spray_wind, go_hold_burn, go_hold_sop_approved,
   go_hold_irrigation_rain, go_hold_harvest_heat."
5. "course videos moved in from video_url: …" only where a course had a video URL.
6. `__version__ = "0.250.0"`.

**3. Checks after the migrate**

1. `get_server_status`: 0.250.0. Desk → any Asset Register form: the map's build stamp reads 0.250.0.
2. Payroll: `preview_payroll_for_period` on last week matches the posted run (identical pay).
3. Desk → Compliance Rule: six Go / Hold presets, all disabled. ERPNext MCP Settings: every new write switch unticked;
   No-Work Notices, Knowledge Checks, Irrigation Schedule, Upload Links Enabled, Daily Equipment Checks unticked.
4. Phone 0.39.0: Today and a task open normally; a task with no rule shows no Go / Hold chip. Business card scan
   works for a Foreman.
5. Per feature, when Tim turns it on: the release's own file (`v0.236.0_task_dates.md` … `v0.250.0_phone_routes.md`,
   `app_0.39.0_work_timing.md`).

**4. Rollback**

Everything is additive and off by default. An older image ignores the new doctypes and fields; FarmOps 0.38.7 ignores
the new routes.
