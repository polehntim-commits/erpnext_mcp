# Employee file: one view of everything — design and contract

**Status: FROZEN (2026-10-03).** Tim approved the brief. Server **v0.223.0**, app **0.31.0**.
Read-only views ship ON (they are reads, gated by role); the packet export and every change to
login-card filing ship as described below. Contract first; the build notes at the end are frozen
with the code.

## 1. Login and enrollment cards are never filed permanently

**Today.** `generate_mobile_login_qr(archive=true)` files the card PNG — a live credential
(api_key + api_secret) — as a PRIVATE attachment on a new Governance Document and points
`Mobile Access Grant.qr_document` at it. Nothing removes it; one (`6lftcmkncj`) was deleted by hand.

**From v0.223.0:**

| Mode | Filing |
|---|---|
| Device keys on (`device_keys_enabled`, v0.218) | **Nothing is filed, ever.** `issue_enrollment_link` already files nothing (its QR carries no credential); `generate_mobile_login_qr` refuses `archive=true` with "device keys are on — use issue_enrollment_link". |
| Legacy (device keys off) | `archive=true` is refused unless the new setting **`login_card_archive_enabled`** (default **off**) is ticked. When it is, the filed card **deletes itself**: on the card's first successful sign-in (the login-card device's first `last_seen_on`), or when the card's `qr_expires_at` passes — whichever is first. |

- **The purge** (`login_cards.purge`): deletes the Governance Document and every File attached to
  it, clears `qr_document`, writes one MCP Action Log row (`login_card:purged`, which card, why) —
  never the image or the secret. Runs (a) inline when the login-card device is first seen, (b) in an
  hourly job, (c) once on migrate (patch) for every card already filed: a card referenced by a grant
  whose QR has expired or whose device has signed in, and any card no grant references any more
  that is older than the longest possible card life (168 h).
- **Only cards.** A Governance Document is a card only if it carries this app's card title prefix
  ("Farm Ops mobile enrolment card — ") **and** its only attachment is the card PNG
  (`mobile-enrolment-*.png`). Anything else is never touched.
- **The durable record** is the Mobile Access Grant and its device rows (issued when, by whom,
  device, platform, app version, last seen, last address, key protection, approved by, revoked when
  and why) — none of it secret. It gains an **`employee`** Link (filled from `Employee.user_id`
  whenever the grant is written; backfilled by the patch), so it appears in the Employee's
  Connections and in the file's Access section.

## 2. The Employee file

One read, `employee_file.build(employee, viewer, windows)`, behind three surfaces:

| Surface | What |
|---|---|
| MCP | **`get_employee_file`** (read; `employee`, optional `sections`, `warning_days`) |
| Desk | an **Employee file** button on the Employee form (a Client Script record, like the ID Card button — visible, removable, removed on uninstall) opening the file as a dialog, with **Export audit packet**; plus an **Employee file** group in the form's Connections |
| Phone | `get_employee_file` under `/farmops/api/mobile/` — the employee's own file (**My records → My file**), and any file for an authorised manager (**Employees → a person → File**) |

### 2.1 Sections

| Section | Contents |
|---|---|
| `identity` | I-9: status, hire date, section 1/2 signed (dates only), document path and titles, document numbers **last 4 only**, expiries (List A/B/C, receipt, work authorization), reverifications; SSN **last 4** (`ssn_last_four`) only. W-4: tax year, status, effective date, filing status, signed date — no amounts. |
| `badge` | badge ID(s) (Bucket Log Badge Map), photo URL, Card Print Jobs (status, requested by/when, printed when, reprint reason) |
| `access` | Mobile Access Grant (state, role, issued/expires/revoked, last seen) and each device (name, status, platform, app version, key protection, enrolled, approved by/how, last seen, last address, revoked when/why/kind); Frappe roles; the last 10 sign-ins (Activity Log); whether a login card is currently filed (it never should be for long) |
| `training` | Employee Training Records (type, completed, expires), training sessions attended/scheduled (Training Session attendee rows), certifications held (Certification `holder` = the employee) |
| `signed_documents` | discipline (Farm Incident Record: type, step, issued, acknowledged/declined, follow-up) and Signing Evidence grouped by document type — policy acknowledgments are Signing Evidence on `Compliance Policy`; each with signed when, method, sealed PDF present |
| `housing` | Housing Assignments (unit, from, to, status) |
| `tasks` | Farm Tasks assigned to the employee or about them (`subject` = this Employee — the Badge photo tasks): name, type, state, urgency, completed |

Each section: `{available, rows, flags, note}` — `available: false` with a reason when the doctype is
absent or the viewer may not see it (the section is named, never silently missing).

### 2.2 Expiring and expired

Every dated item that can lapse (work authorization, I-9 documents and receipt, training records,
certifications, sessions' `expires_date`) gets `expiry: {date, state: ok|expiring|expired,
days_left}`. Windows are the JSON setting **`employee_file_expiry_windows`** (days; defaults
`work_authorization 90`, `i9_document 60`, `i9_receipt 30`, `training 30`, `certification 60`), or
one `warning_days` argument for all. The file's top-level `flags` lists every expired and expiring
item, worst first; `summary` counts them.

### 2.3 Who may see what (HR permissions)

The viewer is the calling person (`security.caller_identity()` on MCP, the session user in the
Desk, the guard user on the phone), else the MCP System User — the same rule as every personnel tool.

| Viewer | Sees |
|---|---|
| The employee themself (their `user_id`) | their whole file |
| System Manager, HR Manager | every section |
| HR User | every section except `signed_documents.discipline` |
| Farm Manager | badge, access (devices only — no roles, no sign-ins), training, housing, tasks |
| Anybody else | refused ("not your file, and not a personnel role") |

Entity scope applies (`employee.require_company_scope`). A section a viewer may not see is
`available: false, note: "needs HR Manager"`.

### 2.4 Never in the file

SSN beyond `ssn_last_four`; `ssn_full`; I-9 / passport / A-number / I-94 / licence numbers beyond
the last 4; bank or routing numbers (bank details are not in the file at all); any api_key,
api_secret, enrollment token, access/refresh token or key material; signature images (only
"signed at"); the file path of any private attachment except sealed PDFs, which are linked by
File name for the packet. A test walks the whole answer for every forbidden key and for any
9-digit SSN-shaped or long numeric string.

## 3. The per-person audit packet

**`export_employee_file_packet`** (MCP, write, **default OFF**) and the Desk's **Export audit packet**
(HR Manager / System Manager): one PDF —

1. a cover and the whole file, section by section, with the expiry flags (rendered by
   `frappe.utils.pdf.get_pdf`, which v0.216.1's `pdf_base` made work inside the container; this
   app's own PDF writer when it is not there);
2. appended, when `pypdf` is present: the **sealed PDFs** of the person's discipline records and
   Signing Evidence, and the generated PDFs of Training Sessions they attended. **I-9 and W-4 PDFs
   are never appended** — they carry the full SSN and document numbers.

Saved as a PRIVATE File attached to the Employee (`employee-file-<employee>-<date>.pdf`), one MCP
Action Log row per export (who, which sections). The MCP tool returns the File name and URL, not
the bytes.

## 4. Surfaces

MCP tools +2: `get_employee_file` (read), `export_employee_file_packet` (write, OFF). Mobile route
+1: `get_employee_file`. Desk: `erpnext_mcp.api.employee_file.get`, `.packet`; Client Script
"Employee file"; DocType Link rows on Employee (I-9 Form, W-4 Form, Employee Training Record,
Signing Evidence, Housing Assignment, Farm Incident Record, Mobile Access Grant, Card Print Job).
Settings: `login_card_archive_enabled` (0), `employee_file_expiry_windows` (JSON). Field:
`Mobile Access Grant.employee`. Hourly job: `login_cards.purge_due`. Patch: purge filed cards +
backfill `employee`.

## 5. Decisions for Tim

1. Farm Manager's view (badge, devices, training, housing, tasks — no identity, roles, sign-ins or
   signed documents). Recommended.
2. HR User does not see discipline. Recommended (matches Farm Incident Record's own permissions).
3. The patch deletes every login card already filed that has expired or was used. Recommended —
   each is a live or dead credential image with no record value; the grant is the record.
