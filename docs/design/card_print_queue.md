# Card print queue — contract (frozen, v0.208.0)

Tim, 2026-10-01:

> "This is the next project I would Like to get done please. I got the printer to do a few tests."

Source spec: the Claude Doc "Card Print Queue: Build Spec for Dispatch". This contract freezes what
the three parts say to each other, and fills in what the spec left to judgement.

**Goal:** anyone with permission asks for an employee ID card or an asset tag from the iPhone or
from Desk, and it prints on the Evolis Primacy 2 with no print dialog.

**Shape:** ERPNext holds the queue, one Mac prints, and the phone only ever talks to ERPNext.

**Out of scope:** card design changes, magnetic or chip encoding, and printing from off the farm
network.

---

## 1. What was found on the print Mac (read-only, 2026-10-01)

| | |
|---|---|
| Mac | Tim's MacBook Pro, 192.168.1.113 |
| CUPS queue | **`Primacy_2`**, `socket://192.168.1.196/`, idle, enabled, accepting, system default |
| Driver | EVOLIS Primacy 2 CUPS 8.1.0.10559 (`evorasterizer`) |
| Page size | exactly one: **`PageSize=Card`**, 155.53 × 243.85 pt (54.9 × 86.0 mm, **portrait**, with bleed) |
| Duplex | `Duplex=NONE` (default) or `Duplex=DuplexNoTumble` |
| Orientation | `Orientation=PORTRAIT` (default) or `LANDSCAPE_CC90` |
| Ribbon | `GRibbonType=RC_YMCKO` (colour) is the driver default |
| Python | `/usr/bin/python3` is 3.9.6; Homebrew's is 3.14 |

**What follows from that:**
- The spec's `-o media=<CR80>` does not exist on this driver. The agent sends `-o PageSize=Card`.
- The artwork is a landscape 85.6 × 54 mm page and the driver's page is portrait. `-o fit-to-page`
  lets CUPS rotate and fit it. Whether `Orientation=LANDSCAPE_CC90` is also needed can only be seen
  on a printed card, so it is a config line (`extra_options`), settled by the install test card.
- The agent has to run on Python 3.9, so it uses the standard library only.

Nothing was printed during discovery.

---

## 2. Records

### 2.1 Card Print Job

One row per request. Rows are never deleted: no role has delete permission, and Cancelled and
Failed jobs stay as history. `track_changes = 1`.

| field | type | notes |
|---|---|---|
| `job_type` | Select | `Employee ID` / `Asset Tag` |
| `reference_doctype` | Link DocType | `Employee` or `Asset Register`; set by the server from `job_type` |
| `reference_name` | Dynamic Link | the employee or asset |
| `reference_title` | Data | the person's or asset's name at request time, for lists |
| `company` | Link Company | copied from the reference; scopes who may see the job |
| `status` | Select | `Queued` / `Printing` / `Printed` / `Failed` / `Cancelled` |
| `print_station` | Link Card Print Station | chosen by routing (§2.3) |
| `copies` | Int | 1–5 |
| `sides` | Select | `Single` / `Dual` |
| `artwork` | Attach | the rendered PDF, a private File |
| `artwork_sha256` | Data | hash of the PDF the station printed |
| `requested_by` | Link User | from the session, never from the body |
| `requested_from` | Select | `iOS` / `Desk` / `API` |
| `client_request_id` | Data, unique | idempotency key |
| `claimed_at`, `printed_at` | Datetime | set by the station methods |
| `claimed_by` | Data | the station's API user |
| `attempts` | Int | +1 on each claim |
| `error` | Small Text | the last failure, in a sentence |
| `is_reprint` | Check | set by the server when a Printed job already exists for the reference |
| `reprint_reason` | Data | `Lost` / `Damaged` / `Details changed` / `Other: …`; required when `is_reprint` |
| `cups_job` | Data | the CUPS job id the station reported |

The docname is `CPJ-<year>-<5 digits>`.

### 2.2 Status moves

```
Queued ──claim──▶ Printing ──complete(success)──▶ Printed
   ▲                 │
   │                 ├─complete(failure, retryable, attempts < 3)──▶ Queued
   │                 ├─complete(failure, not retryable or attempts = 3)──▶ Failed
   │                 └─stuck > 10 min: attempts < 3 ──▶ Queued, else ──▶ Failed
Queued ──cancel──▶ Cancelled
Failed ──retry──▶ Queued (attempts reset to 0)
```

Every other move is refused in a sentence.

### 2.3 Card Print Station — routing as data

One row per printing place. This is what makes "CR80 card today, label printer later" a record
instead of a code change.

| field | notes |
|---|---|
| `station_name` | the docname, e.g. `primacy2-main` |
| `job_types` | which job types this station prints, one per line (`Employee ID`, `Asset Tag`) |
| `companies` | optional, one per line; empty means every company |
| `media` | a description, e.g. `CR80 PVC card 85.6 x 54 mm` |
| `artwork_width_mm`, `artwork_height_mm` | the page the server renders for this station (85.6 × 54) |
| `enabled` | a disabled station receives no jobs |
| `priority` | lower wins when several stations match |
| `last_seen_at`, `printer_state`, `printer_message`, `agent_version` | written by the agent's heartbeat |

**Routing** (`card_print.station_for(job_type, company)`): the enabled station that lists the job
type and matches the company, lowest `priority` first. No match is refused: "no print station is
set up for asset tags".

**Seeded:** `primacy2-main`, printing both job types on CR80.

**To move asset tags to a label printer later:** add a second station row with
`job_types = Asset Tag`, its own artwork size and a lower `priority`, and install a second agent
config. No code and no doctype change.

**Station state the requester sees:**
- `Ready`
- `Paused`
- `Printer error`
- `Offline` — no heartbeat for 2 minutes

A request is accepted in every state; it just waits.

---

## 3. Artwork

Rendered on the server at request time. The phone and the agent never build a card.

| job | how | pages |
|---|---|---|
| **Employee ID** | the **existing card design unchanged**: `badges._card()` for the facts and the badge QR, `badge_sheet.card_html()` and `badge_print_format.CARD_CSS` for the layout, through `frappe.utils.pdf.get_pdf` with the page set to exactly 85.6 × 54 mm and no margins | front; **Dual** adds the existing back (the 38.1 mm badge QR) as page 2 |
| **Asset Tag** | a new CR80 layout drawn with reportlab (the app has no asset card today, only a QR and label sheets) | one page; Dual is refused for asset tags |

**The asset tag layout:**
- the `generate_asset_qr` payload (`Asset Register.qr_url`) as a 30 mm QR with a 2 mm quiet zone,
  error correction M, black on white;
- the tag ID (the docname) large beside it;
- the asset type, description and company under that.

**Rules for both:**
- The generators' own outputs are not changed.
- An employee's existing badge is reused. An employee with **no badge** gets one issued only when
  the requester may issue badges (the hiring roles, as the ID Card button requires today);
  otherwise the request is refused, saying who can issue it. *(Amended during implementation:
  printing a card must not be a way around the badge-issuing rule.)*
- If the PDF cannot be made (no wkhtmltopdf, no reportlab, no QR encoder), the request is refused in
  a sentence and **no job is created**. A job with no artwork must never reach a station.
- The PDF is attached privately to the job, and its sha256 is recorded.

---

## 4. Methods

Three callers, one module (`erpnext_mcp/card_print.py`):
- **Desk and the agent** call `/api/method/erpnext_mcp.api.card_print.<name>` with a Frappe session
  or `Authorization: token <key>:<secret>`.
- **The phone** calls `/farmops/api/mobile/<name>` (guarded mobile routes).
- **MCP** has tools of the same names.

### 4.1 Requester methods

**`request_card_print(job_type, reference_name, copies=1, sides="Single", client_request_id, reprint_reason?)`**

Answers:
```
{job: <row>, created: bool, duplicate: bool, already_queued: bool, station: <station state>}
```

In order:
1. `client_request_id` is required (8–64 characters). If a job already carries it, that job is
   returned with `duplicate: true` and nothing is rendered.
2. The caller must hold **Card Print Requester** (or System Manager), and must be allowed to read
   the employee or asset: company scope on the phone, DocPerm in Desk.
3. If a `Queued` or `Printing` job already exists for the same reference and job type, that job is
   returned with `already_queued: true`. A double tap does not become two cards.
4. If a `Printed` job exists for the reference, `reprint_reason` is required (else refused: "this
   card was already printed on <date>; say why it is being reprinted"), and `is_reprint` is set.
5. Route to a station (§2.3), render (§3), attach, and insert as `Queued`.
6. Limits: `copies` 1–5; 10 requests a minute and **100 a day** per user.

**`list_card_print_jobs(status?, mine_only=true, reference_name?, limit=50)`**

Answers:
```
{jobs: [<row>], count, stations: [<station state>], can_request: bool}
```
- Newest first, at most 200.
- `mine_only=false` shows every job in the caller's companies, and needs the requester role.

**`cancel_card_print_job(name)`** — only while `Queued`; by the requester, or anyone with the role in
that company.

**`retry_card_print_job(name)`** — only while `Failed`; back to `Queued`, attempts 0, error kept in
the change history.

**A job row:**
```
{name, job_type, reference_doctype, reference_name, reference_title, company, status,
 print_station, copies, sides, requested_by, requested_by_name, requested_from, requested_at,
 claimed_at, printed_at, attempts, error, is_reprint, reprint_reason, can_cancel, can_retry}
```
The artwork URL is never in a row.

### 4.2 Station methods (role **Card Print Station** only)

**`claim_next_card_print_job(print_station, printer_state?, printer_message?, agent_version?)`**
- Records the heartbeat on the station.
- If `printer_state` is not `Ready`, it claims nothing.
- Otherwise it takes the oldest `Queued` job for the station under a **row lock**
  (`get_value(for_update=True)`, then a re-read), sets `Printing`, `claimed_at`, `claimed_by`,
  `attempts + 1`.
- Answers `{job: null}` or:
  ```
  {job: {name, job_type, copies, sides, reference_title, attempts,
         artwork_base64, artwork_sha256, file_name}}
  ```
- **The PDF travels inside this answer**, so the station user needs no file permission and
  `/private/files` is never opened to it.
- A station holds one `Printing` job at a time. A second claim while one is open returns that same
  job, so an agent that crashed between claim and print picks it up again.

**`complete_card_print_job(name, success, error?, retryable=false, cups_job?)`**
- Only the station that claimed it, and only while `Printing`.
- A repeat for an already Printed job is a success that says so.
- Outcomes are as §2.2.

**`card_print_heartbeat(print_station, printer_state, printer_message?, agent_version?)`** — the
heartbeat alone, for when the agent is not claiming.

### 4.3 The stuck-job sweep

Every 5 minutes (`scheduler_events` cron): a job that has been `Printing` for more than 10 minutes
goes back to `Queued` if `attempts < 3`, else to `Failed`, with the error "the print station did not
report back".

### 4.4 Desk

- **Employee form:** "Card › Print ID Card" and "Card › Print history". Both are seeded Client
  Scripts, like the existing Badge button.
- **Asset Register form:** "Tags › Print Asset Tag" and "Tags › Print history".
- **Reprints:** the print buttons ask for a reason when one is needed.
- **Card Print Job list:** status colours (Queued orange, Printing blue, Printed green, Failed red,
  Cancelled grey) and a **Retry** action on Failed rows.
- **Card Print Job form:** Retry and Cancel buttons.

### 4.5 MCP tools

| tool | kind |
|---|---|
| `list_card_print_jobs` | read |
| `list_card_print_stations` | read |
| `request_card_print` | write, default off |
| `cancel_card_print_job` | write, default off |
| `retry_card_print_job` | write, default off |

**Counts:** 967 tools — 476 read, 491 write. 151 mobile routes; the new ones are the four requester
methods.

---

## 5. Security and audit

- **Two new roles:**
  - **Card Print Requester** — may request, list, cancel and retry.
  - **Card Print Station** — the agent's API user only. It may claim, complete and heartbeat. It has
    no DocPerm on Employee or Asset Register, and no Desk access.
- **The caller's identity** comes from the session. `requested_by`, `requested_from` and
  `claimed_by` are never read from a request body.
- **Company scope:**
  - a requester prints only for records in companies they reach;
  - `mine_only=false` lists only those companies;
  - a station may be limited to companies.
- **Artwork** is a private File on the job.
  - It leaves the server only inside a claim answer to the station role.
  - A requester can open it from the job in Desk through ordinary File permissions.
  - The phone never downloads it.
- **Reprint trail:** every card for a reference is a job row; `is_reprint` and `reprint_reason` are
  on it; the forms' "Print history" shows them.
- **Limits:** 10 a minute and 100 a day per requester; `copies` ≤ 5; artwork ≤ 2 MB.
- **The agent** keeps nothing but a temp PDF, deleted after each job. Its API secret is in the macOS
  Keychain.
- **Every call** writes an MCP Action Log row (MCP), a `mobile:` audit row (phone) or a Frappe
  Version row (Desk and agent).

---

## 6. The Mac agent

**Where it lives:** `cardprint_agent/` at the root of the erpnext_mcp repo. It is not inside the
Frappe package, so it is not part of the server image.

| file | what |
|---|---|
| `cardprint_agent.py` | the service; standard library only; runs on Python 3.9 and later |
| `install.sh` | writes the config and the LaunchAgent, stores the credentials in the Keychain, offers a test card |
| `uninstall.sh` | unloads the LaunchAgent; leaves config and logs |
| `config.example.toml` | the config, with this Mac's discovered values |
| `tests/` | unit tests with a fake `lp` / `lpstat` |
| `README.md` | install and troubleshooting |

**Config** (`~/.config/cardprint/config.toml`):

```toml
erpnext_url    = "http://umbrel.local:5300"
station        = "primacy2-main"
queue          = "Primacy_2"
media_option   = "PageSize=Card"
single_option  = "Duplex=NONE"
duplex_option  = "Duplex=DuplexNoTumble"
extra_options  = ["fit-to-page"]
poll_seconds   = 5
keychain_service = "cardprint"
```

**The loop:**
1. Read the printer state:
   - `lpstat -p <queue>` — enabled, or disabled / paused;
   - `lpstat -a <queue>` — accepting;
   - `lpoptions -p <queue>` — `printer-state-reasons`.

   Anything but idle or printing with reasons `none` is `Paused` or `Printer error`.
2. Call `claim_next_card_print_job` with that state. Not `Ready` means it is a heartbeat only, and
   the jobs stay Queued.
3. On a job:
   1. write the PDF to a private temp directory;
   2. check its sha256;
   3. run `lp -d <queue> -n <copies> -o <media_option> -o <single|duplex option> -o <extra…> <file>`;
   4. read the request id from `lp`'s answer.
4. Watch `lpstat -W not-completed -o <queue>` until the id leaves it. Then read the job's final
   state through `ipptool` (`get-job-attributes`):
   - `completed` is success;
   - `aborted`, `canceled` or `stopped` is failure.
5. Call `complete_card_print_job`. The failure is **retryable** when the printer was the problem: a
   3 minute timeout, the printer going into error, or `aborted`. It is not retryable when the file
   was the problem: `lp` refusing it, or a sha mismatch.
6. Delete the temp file. Only then poll again — one job at a time.

**Other behaviour:**
- If ERPNext is unreachable, back off to 30 s and keep trying.
- Log to `~/Library/Logs/cardprint.log`, rotating at 1 MB × 5.
- Run as LaunchAgent `farm.fafo.cardprint` with `KeepAlive` and `RunAtLoad`.
- `--once`, `--status` and `--test-card` flags support install and diagnosis.

**The install script never prints without asking.** The test card is a prompt, default no.

---

## 7. iPhone

The app never renders artwork and never talks to the printer. It calls the four requester routes.

### 7.1 Who sees print actions

Print actions show when the user's `roles` include **Card Print Requester** or **System Manager**.
This is a courtesy; the server is the gate.

### 7.2 Print actions

| screen | action |
|---|---|
| Employee profile | "Print ID card" in the badge section |
| Onboarding success screen | "Print ID card now" |
| Asset detail | "Print asset tag" |
| Scan result (an asset) | "Print asset tag" |
| Asset registration success ("Tag Ready") | "Print tag now" (not while the registration is still queued) |
| People search results and the asset list | select several → "Print selected": one request per record |

**How a tap behaves:**
- It makes a UUID `client_request_id`, sends it, and shows "Sent to printer queue". It never blocks
  on the card.
- When the server answers that a reason is needed, the app asks **Lost / Damaged / Details changed /
  Other** and resends with the same id.
- When offline, the request is queued through `SyncManager` (kind `request_card_print`) with the
  same id, and shows as "Waiting to send".

### 7.3 Print queue screen

- **Rows:** Mine / All filter; each row shows the name, the job type, a status chip, who asked and
  when.
- **Station banner:** the station state is shown on top, e.g. "Printer is offline — cards will print
  when it is back".
- **Actions:** swipe to cancel a Queued job. A Failed job shows its error and a Retry button
  (`retry_card_print_job`).
- **Refresh:** pull to refresh, and poll every 5 s while the screen is open and a job is Queued or
  Printing.
- **Opened from:**
  - the confirmation after a print tap;
  - the profile and asset screens ("Print history");
  - the server-driven tile.

### 7.4 The tile

Seeded at version 1, Published, as tile `print_queue`:
- on Today;
- target `report: print_queue`;
- badge query `my_print_jobs` (my Queued, Printing and Failed jobs; critical when any Failed);
- audience roles `[Card Print Requester]`;
- `min_app_version 0.22.0`;
- icon `printer`.

Allowlist additions on both sides: report id `print_queue`, query `my_print_jobs`, icon `printer`
(49 icons).

### 7.5 Versions and docs

App 0.22.0 (3). SERVER_CHANGES §55.

---

## 8. Build order and acceptance

**Build order:** records and methods → artwork → Desk → agent → phone.

**What is tested here:** unit tests on each side, with a fake printer for the agent.

**What needs the real printer** — Tim's to run after deploy, since nothing is printed without
asking:

| # | test | covered by |
|---|---|---|
| 1 | Desk "Print ID Card" prints within 30 s | real printer |
| 2 | Phone "Print ID card": Queued → Printing → Printed | real printer; unit tests on each move |
| 3 | A new asset's printed tag scans back to it | real printer; the unit test checks the QR payload is `qr_url` |
| 4 | The badge QR on a printed card resolves to the employee | real printer; the unit test checks the payload is the badge id |
| 5 | Ten requests print in order, one at a time | unit: claim order, and one open job per station |
| 6 | Printer off: requests wait, then print in order | unit: a not-Ready state claims nothing; agent test |
| 7 | The same `client_request_id` twice gives one job | unit |
| 8 | No role: no buttons, and the API refuses | unit |
| 9 | Agent killed mid-job: back to Queued within 10 minutes | unit: the sweep, and re-claiming the open job |
| 10 | A reprint needs a reason | unit |
| 11 | An offline request prints after reconnecting | phone unit: queued with the same id |

---

## 9. Open questions for Tim

The defaults apply unless changed.

1. **Print Mac:** this MacBook now [yes]. A laptop that sleeps or leaves the farm stops the queue —
   the cards wait. An always-on Mac mini is the better home later.
2. **Server address for the agent:** `http://umbrel.local:5300` on the LAN [yes]. This is plain HTTP
   inside the farm network; the spec said HTTPS, and Frappe is only reachable over HTTPS through
   Tailscale, which funnels `/farmops` but not `/api/method`.
3. **Asset tags** on PVC cards with the Primacy 2 [yes], or on adhesive labels later.
4. **ID cards** single-sided [yes], or dual with the large QR on the back.
5. **Ribbon:** colour YMCKO [the driver default].
6. **Who gets Card Print Requester** [owners and office staff — Tim assigns it].
7. **Push on a Failed job** [no; it shows on the queue screen and the tile badge].
8. **Orientation:** whether the card needs `Orientation=LANDSCAPE_CC90` as well as fit-to-page. The
   first test card answers this.
9. **A photo-less employee** prints with initials, as the current card does [yes].

---

# Amendment 1 — approved artwork, Desk first, simplex and orientation (frozen, v0.209.0)

Tim, 2026-10-01, after hand-testing:

> "Ok can we get these sorted out so i can print the asset tags and employee id's please."
> "Using ERP next."

**Goal:** Tim prints real cards from ERPNext Desk as soon as stage (a) is deployed — through the
agent when it is installed, and by downloading the card PDF until then.

This amends §2.1, §2.3, §3, §4.4 and §6 above. Where the two disagree, this section wins.

## A1. What the hand tests found

| finding | consequence |
|---|---|
| The right Preview settings are Paper Size CR80 / ISO 7810, 100%, no fit, Auto Rotate off | the agent no longer sends `fit-to-page`; the PDF's pages are already card-sized |
| A 2-page PDF came out as two cards | the printer is running **simplex** (Evolis ships dual-side off, or this is a simplex model); stations get a `duplex` setting, default Simplex (A4) |
| The driver may need the back pre-rotated | stations get `front_orientation` and `back_orientation` (A3) |
| The Evolis preview could not open the PDF | the agent sends files with `lp` directly, as already built |
| Desk "Print" on Employee produced `format=undefined`, A4, blank | Employee and Asset Register get a card print format as their default (A6) |
| CUPS on this Mac, re-read 2026-10-01 | queue `Primacy_2`; `PageSize=Card` only; `Duplex=NONE` (default) / `DuplexNoTumble`; `Orientation=PORTRAIT` (default) / `LANDSCAPE_CC90`; three completed test jobs |

## A2. Artwork — the approved designs

**One layout, drawn by the server with reportlab** — a true card-sized PDF, one page per side, with
a vector QR. This replaces §3's HTML-through-wkhtmltopdf ID card and the first asset tag layout.
The old "Employee Badge Card" Letter sheet and the generators' own outputs are untouched.

**Reference files** (Tim's approved tests) are kept in `tests_standalone/fixtures/card_art/` as the
golden references. Tests compare each page's size and every text item's position against them.

Front: 85.6 × 54 mm landscape. Back: 54 × 85.6 mm portrait. Margins ≥ 4 mm; the QR's quiet zone is
inside the margin. Helvetica throughout. Positions are from the bottom-left, in mm, text by baseline.

**Asset Tag — front**

| element | where |
|---|---|
| logo | left, 39 mm square at x 5.5, y 7.5 |
| QR | right, 25.6 mm square at x 51.8, y 21.2 |
| asset ID | Helvetica-Bold 10 pt, centred under the QR, baseline y 14.4 |
| asset name | Helvetica 6.5 pt, centred under the ID, baseline y 11.2 |

**Asset Tag — back**

| element | where |
|---|---|
| QR | 34 mm square, centred, top margin 10 |
| asset ID | Helvetica-Bold 16 pt, centred, baseline y 31.1 |
| asset name | Helvetica 9 pt, centred, baseline y 25.6 |
| company | Helvetica 7 pt grey, centred, baseline y 16.6 |
| "Scan for asset record" | Helvetica 7 pt grey, centred, baseline y 12.6 |

The asset "name" is the Asset Register description's first line, else the asset type. The ID is
the docname.

**Employee ID — front**

| element | where |
|---|---|
| photo | 4:5 slot, 21.6 × 27 mm at x 4.9, y 22.1; the Employee image cropped to fill, else initials (Helvetica-Bold 26 pt grey on light grey) |
| band | green `#356B2E`, white Helvetica-Bold 7.5 pt, upper-cased. One line: 21.6 × 5.9 mm at x 4.9, y 9.8. Two lines (e.g. `OWNER /` `OPERATOR`): 21.6 × 9.3 mm at y 5.5, baselines y 11.9 and y 8.7. **The text is the badge category, upper-cased — Amendment 2** |
| name | Helvetica-Bold 12.5 pt at x 30, baseline y 43.5 |
| designation | Helvetica 8.5 pt at x 30, baseline y 38.5 |
| company | Helvetica 6.5 pt grey at x 30, baseline y 34 |
| "BADGE ID" | Helvetica 5.5 pt grey at x 30, baseline y 17.5 |
| badge ID | Helvetica-Bold 13 pt at x 30, baseline y 11.2 |
| logo | 15 mm square, top right at x 63, y 33.3 |
| QR | 15.8 mm square, bottom right at x 61.4, y 7.9 |

**Employee ID — back**

| element | where |
|---|---|
| badge QR | 33.5 mm square, centred, top margin 10.3 |
| badge ID | Helvetica-Bold 17 pt, centred, baseline y 32.9 |
| name | Helvetica 10 pt, centred, baseline y 26.9 |
| company | Helvetica 7.5 pt grey, centred, baseline y 21.9 |
| rule | hairline at y 14 |
| "If found, please return to" / company | Helvetica 6 pt grey, centred, baselines y 10 and y 7 |

**Text that does not fit** shrinks to a floor, then is cut with an ellipsis. It never overflows a
margin.

**Logo:** the Company's `badge_logo` (the field this app already adds), else ERPNext's
`company_logo`. With neither, the logo box is left empty and the card still prints.
- A logo under 600 px wide is reported as a warning on every answer: "the logo is 406 px wide; it
  will look soft on a card — upload one at least 600 px".
- Tim's file `~/Documents/Misc/OrchardMeadowLogo.png` is 406 × 404 px, so it will draw that warning
  until a larger one is uploaded.

**QR payloads are unchanged:** the badge ID for an employee, and `Asset Register.qr_url` for an
asset.

**Both job types are two-sided now.** `sides` defaults to `Dual`; `Single` prints the front only.

## A3. Orientation, per station

Two new Card Print Station fields. The server rotates a page before the PDF leaves; the agent sends
it as it arrives.

| field | values | default |
|---|---|---|
| `front_orientation` | `Landscape` / `Portrait, rotated CW` / `Portrait, rotated CCW` | `Landscape` |
| `back_orientation` | `Portrait` / `Landscape, rotated CW` / `Landscape, rotated CCW` | `Portrait` |

A rotated page keeps the same drawing, turned 90°, on a page of the other shape. These match the
`_back-rotCW` / `_back-rotCCW` reference files.

## A4. Simplex and duplex, per station

New station field **`duplex`**: `Simplex` (default) / `Duplex`.

| station | a Dual job |
|---|---|
| **Duplex** | one job, a two-page PDF; the agent adds the driver's duplex option |
| **Simplex** | the job prints the **front only** (a one-page PDF), ends `Printed`, and is marked **`back_pending`** |

**Printing the back on a simplex printer:** `request_card_back(name)` — a Desk button "Print back"
on the job and on the form's history, plus an MCP tool and a phone route.
- It queues a second job with `pages = Back`, linked by `front_job`.
- The person flips the card into the feeder.
- When the back job prints, `back_pending` clears on the front job.
- No reprint reason is needed for a back.

**New Card Print Job fields:**
- `pages` — `Both` / `Front` / `Back`: what this job's PDF holds.
- `back_pending` — Check.
- `front_job` — Link Card Print Job.

**The claim answer** gains `duplex: bool` and `pages`. The agent uses its duplex option only when
`duplex` is true. It reads the option names from `lpoptions`; a driver that offers no duplex value
makes a `Duplex` station report "this printer has no duplex option" as a printer message, and the
job is printed front only with `back_pending`.

**Switching to Duplex later:** after enabling dual-side in Evolis Premium Suite 2 (Printer settings
→ advanced → Printing → Ribbon → Front/Back options), set the station's `duplex` to `Duplex`. No
deploy.

## A5. Desk first

The Employee and Asset Register buttons ("Card › Print ID Card", "Tags › Print Asset Tag") open one
dialog:
- a **preview** of the front and back, drawn by the server as SVG from the same layout;
- the **station's state** in a sentence, and any artwork warning (e.g. the logo);
- **Send to printer** — `request_card_print`. This is the primary button when the station has
  checked in during the last 2 minutes;
- **Download card PDF** — `download_card_pdf`. This is the primary button when no agent is checking
  in. One card-sized PDF, front and back pages, to print from Preview with Paper Size CR80, 100%.

Neither button uses Frappe's generic print dialog.

**`download_card_pdf(job_type, reference_name, reprint_reason?)`** (Desk method and MCP tool):
- the same permission, read check and rate limits as a request;
- renders both pages using the default station's orientation settings;
- **records a Card Print Job with the new status `Downloaded`** — every card still has a record:
  who, when, for whom — with the PDF attached;
- returns the file.
- A `Downloaded` job does not count as `Printed` for the reprint rule: a hand print may be repeated
  while settings are being found.

Once the agent is installed and checking in, the same button's primary action becomes **Send to
printer**. Nothing needs changing.

**Statuses** are now `Queued` / `Printing` / `Printed` / `Failed` / `Cancelled` / `Downloaded`.

## A6. The blank-page bug

**Cause:** Employee had no usable print format, so Desk asked for `format=undefined` on A4.

**Fix:** two Print Formats, seeded create-only:
- **"Employee ID Card (CR80)"** on Employee;
- **"Asset Tag Card (CR80)"** on Asset Register.

Each has a Custom 85.6 × 54 mm page, zero margins, and draws the same layout through a Jinja global
(`erpnext_mcp_card_svg`): the front, then the back rotated to landscape. Each is set as its
doctype's **default print format** by a Property Setter, only where no default is set. So Frappe's
own Print button and bulk Print now produce the card instead of a blank A4.

The buttons in A5 remain the intended path; this makes the generic one harmless.

## A7. The agent

- `extra_options` defaults to empty: no `fit-to-page`.
- It reads `duplex` and `pages` from the claim answer (A4).
- `install.sh` reports whether the driver offers a duplex value.
- Version 1.1.0.

## A8. Order of delivery

| stage | what | usable on its own |
|---|---|---|
| **(a)** | renderer, new fields and statuses, `download_card_pdf`, `request_card_back`, Desk dialog, print formats and defaults | **yes** — print from Desk by download; queue when an agent exists |
| **(b)** | agent 1.1.0 and install script | the queue prints unattended |
| **(c)** | phone: omit `sides`, show `Downloaded` and back-pending, "Print back" | printing from the phone |

**Counts after (a):** 969 tools — 476 read, 493 write (`download_card_pdf`, `request_card_back`).
152 mobile routes (`request_card_back`).

---

## Amendment 2 — what the ID card says about a person (v0.209.1)

Tim, 2026-10-01: *"I am a manager and not payed as of now."* His Employee record
carries Designation **Operator** on purpose (owner-operator: draws, not payroll)
and that is not to change. The card therefore reads its own fields.

### B1. Fields (Custom Fields, added at migrate, removed by nothing)

| Field | Type | Meaning |
| --- | --- | --- |
| `Employee.badge_title` | Data | The line under the name. **Blank = the Designation.** |
| `Employee.badge_category` | Select | The colour bar. **Blank = the Employment Type's category.** |
| `Employment Type.badge_category` | Select | The category a person of this type gets by default. **This column is the mapping**, and it is edited on the Employment Type record. |

Options, both Selects: *(blank)*, `Employee`, `Management`, `Owner / Operator`,
`Contractor`, `Volunteer`, `Visitor`. The bar prints the option upper-cased;
`OWNER / OPERATOR` breaks after the slash as approved.

### B2. Resolution

1. `Employee.badge_category`, when set.
2. else the `badge_category` of the Employee's Employment Type, when set.
3. else **nothing**: the bar is drawn without text and the answer carries a warning
   naming the two places to set it. `EMPLOYEE` is never a fallback — it is printed
   only when one of the two fields says Employee.

### B3. The mapping's first values

Seeded once, at migrate, only into Employment Types whose `badge_category` is
blank; an operator's value is never overwritten, and after seeding the data is
the only authority (the code has no mapping at print time).

| Employment Type name contains | Category |
| --- | --- |
| operator, owner | Owner / Operator |
| contract, 1099 | Contractor |
| volunteer | Volunteer |
| visitor | Visitor |
| full, part, season, tempor, hourly, salar, piece, commission, intern, apprentice, probation, h-2a | Employee |
| anything else | left blank |

`Management` is never seeded; it is a manual choice on the Employee.

### B4. Surfaces

`preview_card` gains `badge_title` and `badge_category` (the resolved values, `""`
when unresolved). No route, tool, or job key changes. The phone fixtures keep their
shape; their `warnings` list gains the blank-bar sentence, because the fixture's
worker has no Employment Type. Tim's card: `badge_title` = Manager; category from Employment Type
Operator → `OWNER / OPERATOR`.
