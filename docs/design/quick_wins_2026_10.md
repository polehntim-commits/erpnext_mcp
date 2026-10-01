# Quick wins from Tell the Farm (v0.211.0 / app 0.23.0)

**Status: frozen 2026-10-01.** Five notes filed on OML against app 0.20.1. Each section
says what is data, what is code, and on which side. Nothing here writes to a site by
itself except the two migrate items marked *(migrate)*.

| # | Note | Data only? |
|---|---|---|
| 1 | AFB-2026-00026 — capture where an incident happened | The wizard change is data (a published v2). The two built-in report screens and the stored coordinates are code. |
| 2 | AFB-2026-00021 — an asset's documents on its screen | Phone code only. No server change. |
| 3 | AFB-2026-00015 / -00019 — open the class PDF | Server: one list entry and one route. Phone code for the entry points. |
| 4 | AFB-2026-00029 — receipt total, merchant, line items | Receipt@2 is data, but it needs app 0.23.0 to obey it (see §4.1). |
| 5 | AFB-2026-00028 — inventory shows codes, not names | Code, both sides. |

## 1. Where an incident happened

**Server.** `Accident Report` gains `latitude`, `longitude` (Float, 6 places) and
`location_accuracy_m` (Float). `create_accident_report` (mobile route and MCP tool) accepts,
all optional:

| arg | meaning |
|---|---|
| `latitude`, `longitude` | decimal degrees; both or neither; refused outside ±90 / ±180 |
| `location_accuracy_m` | the fix's horizontal accuracy in metres |
| `location_point` | a GeoJSON Point `{type:"Point", coordinates:[lon, lat]}` or `"lat,lon"` — what a form's `geolocation` / `gps` field answers. Used when `latitude`/`longitude` are not sent. |

The answer and `get_accident_report` / `list_accident_reports` carry `latitude`, `longitude`,
`location_accuracy_m` (null when not captured). `location_description` is unchanged and still
what OSHA 300 column F prints.

**Wizard (data).** `wizard:accident_investigation` ("Report an Accident") v2 adds to step
`where` one optional field: `{key: "location_point", type: "geolocation", label: {en: "Pin
where it happened", es: "Marque dónde ocurrió"}}`. It also turns "Witnesses" from a multi-select with no options
(nothing to pick — the conversion from the legacy wizard left it that way, and the
validator refuses it) into a text box of names separated by commas, which is what
`create_accident_report` accepts. *(migrate)* A one-time patch drafts and
publishes v2 **only where the version in force is the untouched converted v1**; a site that
edited the wizard is left alone and told so. Fresh sites get it the same way.

**Phone.** `ReportIncidentView` and `QuickSafetyReportSheet` record the phone's position
when the report is opened (no prompt beyond the system's own; a refusal files the report
without one) and offer **Pin on map** to move it. They send `latitude`, `longitude`,
`location_accuracy_m`. A server older than 0.211.0 drops arguments it does not declare, so
the report files either way.

## 2. An asset's documents on its screen

No server change: `Asset Register` is already an attachment parent (enrolment + the asset's
company in the caller's scope; private files are served only through
`get_attachment_content`, which re-proves the parent), and `get_asset_detail` already
returns `title_documents` (the linked Expense Receipts, read through `get_receipt_image`).

**Phone.** The **Documentation** section shows for every asset type (it was equipment
only), and on the scan result as well as the detail screen. It lists the asset's File
attachments and its title documents, opens them in the document viewer, keeps what was
listed and opened for offline use, and asks the server for files up to its 8 MB ceiling
(it was silently capped at 2 MB).

**Not in this batch — Governance Documents.** There is no link between a Governance
Document and an asset in either direction, and that register is System Manager / Accounts
Manager only (trusts, operating agreements). Showing them on an asset needs a link field
and a decision on who may read them from a phone. Well logs belong on the asset itself
(`attach_asset_document`, or Attach in Desk), where this section shows them.

## 3. The class PDF

Builds on `fafo_ios/TRAINING_PDF_DELIVERY.md` (2026-09-24), which this batch brings up to
date and commits.

**Server.**
- `Training Type` joins `ATTACHMENT_PARENTS` (no role gate beyond enrolment — course
  handouts are for the people taking the course) and `BROKERED_PARENTS`. Training Type has
  no company, so there is no entity scope to apply. `get_training_curriculum` already lists
  a course's `attachments` when one `training_type` is named; the whole-curriculum listing
  gains `attachment_count`.
- New route `get_training_certificate(training_record)` → the certificate file of an
  Employee Training Record as `{training_record, file_name, content_type, encoding:
  "base64", content, content_base64, file_size}`. The caller's own record, or any record in
  scope for `SHIFT_ROLES`. No certificate → not found. The phone has called this since
  0.19; it answered 404.

**Unchanged, on purpose:** a Field Worker who is only an attendee is still refused the
Training Session folder (it holds the sign-in sheet with everybody's signature). Opening
that to attendees is Tim's call; it is one gate.

**Phone.** Today's *My training* section is always present when the person has any class,
upcoming or past, and ends with **Class papers** → every class (upcoming first, then past),
each opening its papers. Training → **Courses** opens a course to its handouts. Both use
the offline cache.

## 4. Receipt@2

### 4.1 What a config can and cannot do today

On receipts the phone's amount, merchant and line items are chosen by Swift
(`ReceiptTextParser`), not by the config: the config's `extractors` run only in the
server's preview. So a data-only Receipt@2 would change nothing on a phone. This batch
makes those three choices config-driven **once**, so Receipt@3 onward is data.

### 4.2 The `receipt` block (new optional top-level key)

```json
"receipt": {
 "total_labels":   ["GRAND TOTAL", "TOTAL", "AMOUNT DUE", "BALANCE DUE"],
 "subtotal_labels": ["SUBTOTAL", "SUB TOTAL"],
 "charge_patterns": ["(?i)\\bUSD\\s*\\$\\s*([0-9]{1,6}(?:,[0-9]{3})*\\.[0-9]{2})"],
 "never_amount":   ["SPEND", "SUMMARY", "YTD", "YEAR TO DATE", "POINTS", "SAVINGS",
                    "YOU SAVED", "CREDIT LINE", "REWARDS", "MEMBER STATEMENT"],
 "not_items":      ["SELF CHECKOUT", "USD$", "AUTH CODE", "PRO XTRA", "SPEND", "SUMMARY",
                    "YTD", "POINTS", "SAVINGS", "CREDIT LINE", "RETURN POLICY", "POLICY ID"],
 "merchant_domains": {"homedepot.com": "The Home Depot", "…": "…"}
}
```

Rules the phone applies, in order:

1. **Amount.** A line bearing a `total_labels` word and not a `subtotal_labels` word: its
   own last currency figure; else the figure alone on the line **directly above or below**
   it (tills print either way; this slip printed `$626.94` then `TOTAL`). Else the first
   `charge_patterns` match. Else the largest figure on a line carrying **no** `never_amount`
   word. A `never_amount` line is never the amount, labelled or not. When the labelled total
   and the card charge both exist and agree, amount confidence is 1.
2. **Merchant.** When the slip carries a domain in `merchant_domains`, the merchant is that
   name. Otherwise the top line, as before. (OCR confidence is no guide here: the logo read
   as "How doers" at confidence 1.0.) The server's own Supplier match by domain and phone is
   unchanged and still runs.
3. **Line items.** A line containing a `not_items` phrase, or that is a `never_amount`
   line, is not an item and is never the description of the next priced line.

All matching is case-insensitive on whole phrases. Lists are capped at 60 entries of at
most 60 characters; `charge_patterns` are held to the portable regex subset with one
capture group; `merchant_domains` at most 200 entries. An app older than 0.23.0 ignores
the block.

The server's preview mirrors rule 1 in `extractors` (ordered: figure-then-TOTAL,
TOTAL-then-figure, card charge), so `preview_extraction_config` shows the amount the phone
will pick. The model's instructions and fields are unchanged from Receipt@1: a merchant
named by the slip's web address is not offered to the model to rename.

### 4.3 Shipping and staging

`extraction/receipt.json` becomes the revision-2 built-in (fresh installs seed it as
Receipt@1 Published, as before). On a site that already has Receipt@1 nothing changes at
migrate: an operator runs

1. `update_extraction_config(document_type: "Receipt", from_builtin: true, notes: …)` → Draft Receipt@2
2. `preview_extraction_config(validation: <a stored Receipt validation>, version: 2)`
3. `stage_extraction_config(document_type, version, users: [...])` → **Staged**: those
   accounts' phones are served it; everyone else keeps the Published one
4. `publish_extraction_config(document_type, version)`

New: status `Staged`, field `staged_users`, tool `stage_extraction_config` (write, default
off, System Manager / Farm Manager), argument `from_builtin` on `update_extraction_config`.
`publish` accepts a Draft or a Staged version. One Staged version per document type.

Regression fixture: `tests_standalone/fixtures/receipts/EXR-2026-0018.txt` (the OCR text),
byte-identical in the phone's tests.

## 5. Inventory names

Item codes on OML are `PROWLER™` and `PROWLER®`; the names are "PROWLER Rat & Mouse Killer
Bait Station" and "PROWLER Place Pacs (22 × 3 oz)". The rule everywhere: **`item_name` is
the title, `item_code` is secondary**, and the name is read live from the Item.

**Server.** `get_stock_ledger` movements gain `item_name`. `search_items` matches the
search text against `item_name` **or** the item code (it matched the name only, so a typed
code found nothing once the two diverged). `record_asset_stock_movement`'s answer gains
`item_name`. `get_warehouse_summary` sorts by name.

**Phone.** The stock ledger shows the name with the code beneath. Receive / issue /
transfer asks for the **item** by name or code, with matches to pick from, and shows the
chosen item's name and code. The typed-item lookup accepts an exact code or an exact name.

## Counts

971 tools (476 read, 495 write): + `stage_extraction_config`. 153 mobile methods: +
`get_training_certificate`.

---

# Amendment 1 — multi-day classes (v0.212.0 / app 0.24.0)

**Frozen 2026-10-01.** Tim: a Training Session is one date, so day 2 of a class disappears
(the Applicator License renewal on Oct 28 and Nov 17 showed as "past sessions").

## M1. Model — no new doctype

A **day is a Training Session**, as it always was: its own date, start / end time, location,
attendee rows, signatures and sign-in sheet. A **course** is a Training Session that other
sessions point at.

| Doctype | New field | Meaning |
|---|---|---|
| Training Session | `parent_session` (Link Training Session) | Set on a day: the course it belongs to. |
| | `day_number` (Int) | 1-based, by date within the course. Kept in order by the server. |
| | `required_day` (Check, default 1) | Whether credit needs this day. |
| | `end_date` (Date, read-only) | On a course: its last day. `session_date` is its first. |
| Training Session Attendee | `reminded_on` (Datetime, read-only) | When the evening-before push went out, so it goes once. |
| | `scan_source` gains `Self` | The attendee checked themselves in. |
| Training Type | `renews_certification` (Data) | A Certification `cert_type` or `cert_name` this course renews (e.g. `Applicator License`). Optional. |

A course's attendee table is the **registration**: who is expected on every required day. A
course holds no attendance of its own and is never signed. Registering somebody on a course
puts them on each open day with `attended = 0`; a badge scan or check-in on a day sets
`attended = 1` there (and registers a walk-in on the course).

**Existing sessions are single-day courses already** — a session with no `parent_session`
and no days is a course of one, and behaves exactly as before. No data is rewritten.

## M2. Tools and routes

- `create_training_session` takes `days`: a list of `{session_date, start_time?, end_time?,
  location?, required?}`. Two or more makes a course plus one day each; times and location
  default from the call's own. (MCP tool and mobile route.)
- **`group_training_sessions(sessions, notes?)`** — new MCP tool (write, default off, shift
  roles): two or more existing open sessions of one Training Type and company become the days
  of a new course; the registration is the union of their attendees. For the Oct 28 / Nov 17
  pair.
- **`add_training_session_day(session, session_date, start_time?, end_time?, location?,
  required?)`** — new MCP tool (write, default off, shift roles): add a day to a course (or to
  the course of the day named; a standalone session becomes day 1 of a new course).
- A Foreman's completion now files its records. Until this, `complete_training_session`
  passed its own gate (the shift roles) and then every record was refused by
  `record_training`'s HR-only gate, leaving the session open with nothing filed. The
  completion now files on its own authority; a direct `record_training` is as HR-only as
  before.
- `complete_training_session`:
  - on a **day**: closes the day and files **no** records. Incomplete rows are judged as
    before. When every required day of the course is Completed, the course completes itself.
  - on a **course**: refused while a required day is open (the days are named). On
    completion one Employee Training Record is filed per attendee who **attended every
    required day** — `completed_date` the last required day, the signature from the latest
    day they signed. Anybody else is listed under `missed_days` and gets no record.
  - on a single-day session: unchanged.
- Every session answer gains `parent_session`, `day_number`, `day_count`, `required_day`,
  `end_date`, `is_course`, and `days: [{session, day_number, session_date, start_time,
  end_time, location, status, required, attended?}]` (empty for a single-day session).
  `attended` is the caller's own row, where the caller is on the day.
  `list_training_sessions` takes `view`: `courses` (the MCP default — one row per course, its
  days inside), `days` (one row per day and per single session, no course rows — the mobile
  route's default, and what an app older than courses can already draw) or `all`. The task payload's `training` key gains the
  same keys.
- **`get_training_cards()`** — new mobile route, any enrolled caller. The caller's own classes
  that are today or within `training_card_days_before`: `{cards: [{session, course,
  training_type, phase: "tomorrow"|"today"|"soon", title, subtitle, session_date, start_time,
  end_time, location, day_number, day_count, can_check_in, checked_in, check_in_opens_at,
  check_in_note, papers: [{doctype, docname}], task?}], evaluated_at}`. `title` and
  `subtitle` are rendered on the server from config (M4).
- **`check_in_training_day(session, latitude?, longitude?, accuracy_meters?,
  client_request_id)`** — new mobile route, any enrolled caller, and only
  for **the caller's own row** on a day they are registered for. Open from
  `training_checkin_opens_minutes_before` the start until `…closes_minutes_after` the end (all
  day when the session has no times). Sets `attended`, `scanned_at`, `scan_source = Self` and
  the fix. Idempotent: a second call answers `already: true`. A self check-in identifies the
  person as a badge scan does (it arrives on their own enrolled phone); the **signature** is
  still taken by whoever runs the class, at the end. A supervisor's badge scan remains the
  other way in.

## M3. The class's papers, for the people in the class

An attendee of a session (or of its course) may list and open that session's and that
course's attachments **except the generated sign-in sheet**, which stays with the roles that
run training. Everybody else is refused as before. This settles the question left open in
§3: the folder was closed because of the sign-in sheet, so the sheet is what stays closed.

## M4. Wording and timing are config (Farm Feature Flags, no seed needed)

| Key | Kind | Default |
|---|---|---|
| `training_card_days_before` | Threshold | 1 |
| `training_card_title_tomorrow` / `_es` | Text | `Class tomorrow` / `Clase mañana` |
| `training_card_title_today` / `_es` | Text | `Class today` / `Clase hoy` |
| `training_card_day_suffix` / `_es` | Text | ` — Day {day} of {days}` / ` — Día {day} de {days}` (multi-day only) |
| `training_checkin_opens_minutes_before` | Threshold | 60 |
| `training_checkin_closes_minutes_after` | Threshold | 120 |
| `training_reminder_hour` | Threshold | 18 (site time; 0–23; −1 switches the push off) |
| `training_reminder_title` / `_es`, `training_reminder_body` / `_es` | Text | `Class tomorrow` · `{training_type}{day_suffix}, {time} at {location}.` |

Placeholders: `{training_type} {day} {days} {day_suffix} {date} {time} {location}`.

## M5. Push and calendar

- **Push.** Hourly job `training_courses.send_reminders`: at `training_reminder_hour` each
  registered attendee of a class tomorrow with an active device gets one push (category
  `FARM_TRAINING`, keys `training_session`, `course`, `phase: "tomorrow"`), in their language.
  `reminded_on` makes it once. Capped at 200 a run; never raises.
- **Calendar (phone).** "Add to Calendar" makes one event per day, each with an alert the
  evening before and an hour before; "Share with papers" sends one `.ics` with every day and
  the papers beside it. The phone has write-only calendar access and no calendar chooser:
  events go to the phone's default calendar (iCloud, when that is the default). iOS calendars
  cannot hold attachments; the papers travel with the share, and the event notes name them.

## M6. A missed required day

Seeded rule **`training_day_missed`** (built-in scanner, Workforce, alert-only, enabled). One
alert per attendee per course: a registered attendee whose required day is Completed (or is
more than a day past) with `attended = 0`. Message: who, which day(s), and — when the Training
Type `renews_certification` and that person holds such a Certification — "their {cert}
expires {date}; a make-up day is needed before then or it lapses". `due_date` is that
expiry, else the course's `expires_date`, else 30 days after the missed day. Critical inside
30 days of the due date, else Warning. It reaches the attendee's own compliance inbox
(`subject_employee`) and Farm Manager / Foreman (`notify_roles`). It clears itself when the
person attends a later day of the same Training Type or a record is filed.

## M7. Counts

973 tools (476 read, 497 write): + `group_training_sessions`, `add_training_session_day`.
155 mobile methods: + `get_training_cards`, `check_in_training_day`. Swept rules 32. Hourly
jobs 3.

---

# Amendment 2 — crew clock-in and assign from Work (AFB-2026-00030)

**Frozen 2026-10-01.** Both already exist one level away: Crew Clock is a toolbar button on
Work, and long-press assign is on its task rows. What is missing: a crew list to pick from, a
visible action, certification filtering, and offline safety.

## W1. Clock in crew

**`clock_in_crew(employees?, badge_ids?, shift?, location?, shift_type?, client_request_id)`**
— new mobile route, **shift roles** (Farm Manager, HR, Foreman, Crew Leader; checked in the
wrapper and again in the tools). Joins `shift` when named, else the caller's own open shift,
else starts one (then `location` is required). Each worker is added in turn and answered on
their own line: `{employee, employee_name, outcome: "added"|"already"|"refused", reason?}` —
one refusal (a minor over hours, a second open shift, another entity) does not stop the rest.
Returns `{shift, started, results, added, already, refused}`.

Idempotent: Farm Shift gains `client_request_id`; a repeat with the same id finds the shift it
started instead of starting a second, and a worker already on the crew is `already`.

**`list_crew_candidates(search?, shift?)`** — new mobile route, shift roles. Active employees
of the caller's entities (name, badge, designation, photo initials), each marked `on_shift`
(the shift's docname) when already on an open one. For the multi-select list; a Foreman could
not use `search_employees`, which is HR-only.

## W2. Assign a task

**`list_assignable_workers(task, search?)`** — new mobile route, dispatch roles (Foreman, Farm
Manager). The crew under the caller's open shifts first, then the roster. Each row:
`{employee, employee_name, on_crew, holding_now, qualified, missing: [requirement…],
skill_match}`. `qualified` is the same check `assign_farm_task` refuses on
(`required_certification` plus the certifications the task's products need), so the list and
the refusal cannot disagree.

`assign_farm_task` gains `client_request_id` and answers `already: true` — not a refusal —
when the task is already held by the worker named. Its certification refusal is unchanged and
has no override.

A task has one holder and this app has no way to split one into copies, so the sheet
chooses **one** worker. ("Worker(s)" in the request: clocking in is many at once; a task
goes to one person, and a second person gets their own task from the same template.)

## W3. Tiles

`tiles.REPORTS` gains `crew_clock_in` and `assign_tasks`. Two tiles are seeded on the **work**
surface (create-only, `min_app_version` 0.24.0): **Clock in crew** (audience: the shift roles)
and **Assign tasks** (audience: Foreman, Farm Manager).

## W4. Phone

Work gets a multi-select **Clock in crew** sheet (crew list + badge scan) and **Assign** as a
swipe action beside the existing long-press, with unqualified workers shown and not
selectable. Both queue offline (`clock_in_crew`, `assign_farm_task` operations carrying their
`client_request_id`) and send when the phone is back.

## W5. Counts

158 mobile methods (+3). AFB-2026-00030 stays Open until Tim confirms it on a device.
