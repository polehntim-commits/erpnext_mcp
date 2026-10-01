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
- An employee with no badge gets one issued, exactly as the ID Card button does today.
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
