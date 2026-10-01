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
