# A finished task's evidence, and a stock entry the books can take: contract (frozen for v0.200.0)

Sources: OML App Feedback **AFB-2026-00024** and **AFB-2026-00025**.

- **00024:** "I cannot review photos etc after inspection". Wind machine 40-WM-SE shows a
  completed "Inspection task" (FT-2026-09-00004: 6 photos and a signature). Tapping the row does
  nothing, and no route lets the phone read a finished task's evidence. `get_task` drops it, and
  `get_attachment_content` refuses the files because they are private and unattached.
- **00025:** "Log stock → Received" on Orchard Meadow was refused with ERPNext's own message,
  HTML tags and all: *"Please enter <b>Difference Account</b> or set default <b>Stock
  Adjustment Account</b> for company <strong>Orchard Meadow, LLC</strong>"*. The company has
  perpetual inventory on, no `stock_adjustment_account`, no `default_inventory_account`, and no
  account on any warehouse. The warehouse chosen ("Stores") was not the cause. Nothing is created
  on OML by this release.

The phone (fafo_ios) builds against this document. Every change is additive.

## 1. A finished task's evidence, readable from the phone

`POST /farmops/api/mobile/list_task_evidence` is a **read**, open on enrolment. The gate is scope
plus `require_scoped_doc("Farm Task", task)`, the same as `get_task`.

| arg | |
|---|---|
| `task` | required, Farm Task docname |

```json
{"task": "FT-2026-09-00004", "task_name": "Routine walk …", "task_type": "Inspection",
 "asset": "40-WM-SE", "state": "Completed",
 "report_photo": {"file": "b12f99c64b", "file_name": "scan-report-….jpg", "is_image": true} | null,
 "assignments": [
   {"assignment": "FTA-2026-09-00004", "state": "Completed",
    "assigned_to": "HR-EMP-00001", "assigned_to_name": "Tim Polehn",
    "completed_at": "2026-09-27 16:58:46", "findings_text": "Looks operable",
    "completion_narrative": "Before (1): … · After (5): …", "witness": null,
    "farm_location_gps": null,
    "evidence": [
      {"file": "4fa19fbde9", "file_name": "FT-…_photo_before_….jpg", "evidence_type": "Photo",
       "phase": "before", "captured_on": null, "caption": "…", "is_image": true,
       "gps_latitude": null, "gps_longitude": null}
    ],
    "signature": {"file": "6252c7c787", "file_name": "…signature….png", "is_image": true} | null}
 ],
 "evidence_count": 7}
```

- `assignments` are newest first. Completed ones are the usual case; a live one lists what has
  been filed so far.
- `phase` is the stored value, or else the `_before_` / `_after_` token in the file name (the
  v0.198.0 rule).
- A Signature row is reported as `signature`, not as an evidence item. So is
  `Farm Task Assignment.signature_file`.

`POST /farmops/api/mobile/get_task_evidence` is a **read** with the same gate.

| arg | |
|---|---|
| `task` | required |
| `file` | required, a File docname that `list_task_evidence` names for this task |
| `max_bytes` | optional, default 2 MiB, ceiling 8 MiB |

```json
{"task": "FT-2026-09-00004", "file": "4fa19fbde9", "file_name": "…", "file_size": 312345,
 "content_type": "image/jpeg", "encoding": "base64", "content": "…"}
```

A File that is not this task's evidence, signature or report photo is refused with a
PermissionError: *"file X is not evidence on task FT-…. Nothing was read."*. The bytes are read
through `files.evidence_content`, the same reader `get_inspection_evidence` uses.

An older server answers 404 on both routes. The phone then says "This farm's server can't show a
finished task's photos yet".

## 2. A stock entry is checked against the books before ERPNext sees it

`create_stock_entry` (MCP and `/mobile`) runs a **preflight** when the company has perpetual
inventory on:

- **Difference account.** A Material Receipt or Material Issue needs a line `expense_account` or
  the company's `stock_adjustment_account`. With neither, the entry is refused: *"Orchard Meadow,
  LLC has no Stock Adjustment Account, so a stock receipt has no account to post the difference
  to. An accountant sets it once, with set_company_defaults (stock_adjustment_account) or on the
  Company in the Desk. Nothing was filed."*
- **Inventory account.** Every warehouse on the entry needs an account: its own, a parent
  warehouse's, or the company's `default_inventory_account`. With none, the entry is refused and
  the warehouse is named.

Both problems are reported in one refusal when both are true. The preflight is skipped when
perpetual inventory is off, when the company fields don't exist, or for a Material Transfer's
difference account (a transfer posts no difference).

`set_company_defaults` accepts **`default_inventory_account`** (Asset root, account type Stock).

## 3. Refusals reach the phone as text

`farmops_api.app._message_for` strips HTML tags and collapses whitespace in every error message it
returns (`frappe.utils.strip_html`, with a regex fallback). The phone also strips tags from a
server message as a backstop, for older servers.

## 4. Surface

Two new `/mobile` routes (`list_task_evidence`, `get_task_evidence`), both open on enrolment. No new
MCP tools or switches. `set_company_defaults` gains one field. No migrate is needed for this
release beyond the usual. fafo_ios SERVER_CHANGES §47.
