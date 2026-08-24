# Tool catalogue

All 840 tools `erpnext_mcp` exposes, with arguments, return shape and a worked
example. The authoritative definitions live in `erpnext_mcp/registry.py`; this
document explains them.

All examples use the site `erp.example.com`, the company `Example Trading Co`
(abbreviation `ETC`) and a textbook chart of accounts. Nothing here is real.

## Conventions that apply to every tool

**Calling.** One `POST` per call, JSON-RPC 2.0:

```bash
curl -sS -X POST https://erp.example.com/api/method/erpnext_mcp.mcp.handle \
  -H 'Content-Type: application/json' \
  -H "X-MCP-Token: $TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"<tool>","arguments":{...}}}'
```

The result is an MCP tool result — the payload is JSON *inside* a text content
block, which is what MCP specifies:

```json
{"jsonrpc": "2.0", "id": 1,
 "result": {"content": [{"type": "text", "text": "{ ...the payload... }"}],
            "isError": false}}
```

**`company` is usually optional.** On a single-company site it is inferred. On a
multi-company site, omitting it raises an error that lists your companies rather
than guessing one. A company **abbreviation** is accepted wherever a name is.

**Accounts resolve three ways.** Anywhere an `account` is taken, you may pass the
docname (`1100 - Cash - ETC`), the account number (`1100`), or the exact account
name (`Cash`). Ambiguity is reported with the candidates listed — it is never
resolved by guessing. `search_accounts` exists to turn a description into a
docname.

**Dates** are `YYYY-MM-DD`. Loose forms like `2026-1-5` are normalised rather than
rejected; anything unparseable gets an error naming the expected format.

**`limit`** defaults to 100 and is hard-capped at 500. A larger value is clamped,
not refused, and the response carries `limit` and `truncated` so a client knows it
did not see everything.

**Some tools are not on every site.** A tool with an unmet site prerequisite —
no `hrms`, no Bank Statement doctype — is not advertised in `tools/list` and
cannot be called. That is different from an operator switching a tool off, and
the refusals say so differently: *"not available on this site: it requires the
Frappe HR (hrms) app... This is not something an operator can switch on here"*
versus *"switched off on this site. An operator must tick allow_&lt;tool&gt;"*.
Which tools carry a prerequisite is noted below.

**Errors are tool results, not JSON-RPC errors.** A bad argument or a missing
document comes back as `isError: true` with a message written to be acted on. A
JSON-RPC `error` object means the *call* was malformed — wrong method, non-object
params — which is a different problem.

**Balance signs.** Every balance is reported twice:

- `balance` — raw ledger convention, `debit - credit`.
- `balance_natural` — sign-flipped for `Liability`, `Income` and `Equity`, so an
  account with a normal credit balance reads positive.

The payload carries `sign_convention` spelling this out, because "the sales
account is at -84,000" is the single most reliable way for a model to misread a
ledger.

---

# Read-only tools

All 422 read tools are **on** by default and can be switched off individually. A
tool that is off does not appear in `tools/list` at all, and neither does one
whose site prerequisite is missing.

The first ten are the accounting surface v0.1.0 shipped; the rest arrived with
later releases and are grouped by what they touch. The numbered sections below
are catalogue order, which interleaves reads and writes by subject — the reads
of the v0.15.0 compliance framework are under Waves 2–4, and the v0.16.0
dispatch surface is under Wave 5.

---

## 1. `get_company_topology`

What kind of ERPNext install is this? Call it first — every other tool takes a
company, account or fiscal year whose names only exist on your site.

**Arguments:** none.

**Returns**

| Field | Meaning |
| --- | --- |
| `companies[]` | One entry per Company |
| `companies[].name`, `.abbr`, `.default_currency`, `.country`, `.chart_of_accounts`, `.parent_company`, `.is_group` | Company header fields present on this site |
| `companies[].default_cost_center` | Under whichever fieldname this version uses; `null` on a Company created before its chart of accounts |
| `companies[].fiscal_years[]` | Fiscal years applying to this company, including company-agnostic ones |
| `companies[].root_accounts[]` | Root accounts with `root_type` |
| `companies[].root_types[]` | Distinct root types present |
| `companies[].account_count` | Accounts belonging to this company |
| `count` | Number of companies |
| `site` | The Frappe site name |
| `optional_doctypes` | `{"Bank Transaction": true, "Bank Statement": false, …}` — check here before calling a tool that needs one |

**Example**

```json
{"name": "get_company_topology", "arguments": {}}
```

```json
{
  "companies": [
    {
      "name": "Example Trading Co",
      "abbr": "ETC",
      "default_currency": "USD",
      "country": "United States",
      "chart_of_accounts": "Standard",
      "is_group": 0,
      "default_cost_center": "Main - ETC",
      "fiscal_years": [
        {"name": "2026", "year_start_date": "2026-01-01", "year_end_date": "2026-12-31", "disabled": 0}
      ],
      "root_accounts": [
        {"name": "Application of Funds (Assets) - ETC", "account_name": "Application of Funds (Assets)", "root_type": "Asset", "is_group": 1}
      ],
      "root_types": ["Asset", "Equity", "Expense", "Income", "Liability"],
      "account_count": 84
    }
  ],
  "count": 1,
  "site": "erp.example.com",
  "optional_doctypes": {"Bank Transaction": true, "Bank Statement": false, "Bank Account": true, "Bank": true}
}
```

---

## 2. `get_account_balance`

Balance of one account as of a date, summed from GL Entry with cancelled entries
excluded — so it matches what ERPNext's own General Ledger report prints.

**Arguments**

| Name | Required | Notes |
| --- | --- | --- |
| `account` | yes | Docname, number or exact name |
| `as_of` | no | `YYYY-MM-DD`; defaults to today |
| `company` | no | Narrows resolution; required if the account name is ambiguous |

**Returns** `account`, `account_name`, `account_number`, `company`, `currency`,
`root_type`, `account_type`, `is_group`, `as_of`, `total_debit`, `total_credit`,
`gl_entry_count`, `balance`, `balance_natural`, `sign_convention`. Group accounts
additionally get a `note` explaining that GL Entries post to leaves.

**Example**

```json
{"name": "get_account_balance",
 "arguments": {"account": "1100", "company": "Example Trading Co", "as_of": "2026-06-30"}}
```

```json
{
  "account": "1100 - Cash - ETC",
  "account_name": "Cash",
  "account_number": "1100",
  "company": "Example Trading Co",
  "currency": "USD",
  "root_type": "Asset",
  "account_type": "Cash",
  "is_group": false,
  "as_of": "2026-06-30",
  "total_debit": 41200.0,
  "total_credit": 33875.5,
  "gl_entry_count": 218,
  "balance": 7324.5,
  "balance_natural": 7324.5,
  "sign_convention": "balance = debit - credit (raw ledger). balance_natural flips the sign for Liability/Income/Equity so a normal balance reads positive."
}
```

For an Income account the same call returns `"balance": -84000.0` and
`"balance_natural": 84000.0`.

---

## 3. `get_journal_entries`

Journal Entry **headers** in a date range, newest first. Headers only — a month of
entries with every line expanded is a lot of tokens for a question that is usually
"which one was it".

**Arguments**

| Name | Required | Notes |
| --- | --- | --- |
| `from_date` | yes | Inclusive |
| `to_date` | yes | Inclusive; must not precede `from_date` |
| `company` | no | |
| `account` | no | Entries with this account on **any** line |
| `docstatus` | no | `0`/`"draft"`, `1`/`"submitted"`, `2`/`"cancelled"`. Omit for all |
| `limit` | no | Default 100, max 500 |

**Returns** `journal_entries[]` (each with `name`, `posting_date`, `company`,
`voucher_type`, `total_debit`, `total_credit`, `user_remark`, `cheque_no`,
`cheque_date`, `bill_no`, `docstatus`, `docstatus_label`, `owner`, `creation`),
plus `count`, `limit`, `truncated` and the `filters` that were applied.

**Example**

```json
{"name": "get_journal_entries",
 "arguments": {"from_date": "2026-06-01", "to_date": "2026-06-30",
               "account": "1190", "docstatus": "submitted", "limit": 50}}
```

```json
{
  "journal_entries": [
    {"name": "ACC-JV-2026-00184", "posting_date": "2026-06-28",
     "company": "Example Trading Co", "voucher_type": "Bank Entry",
     "total_debit": 1450.0, "total_credit": 1450.0,
     "user_remark": "June clearing sweep", "docstatus": 1,
     "docstatus_label": "submitted", "owner": "mcp@example.test"}
  ],
  "count": 1,
  "limit": 50,
  "truncated": false,
  "filters": {"from_date": "2026-06-01", "to_date": "2026-06-30",
              "company": "Example Trading Co", "account": "1190", "docstatus": 1}
}
```

---

## 4. `get_journal_entry`

One entry in full.

**Arguments:** `name` (required) — the Journal Entry docname.

**Returns** every header field this site has, `docstatus_label`, `balanced` (a
boolean, computed), and `accounts[]` with `idx`, `account`, `party_type`, `party`,
`debit`, `credit`, account-currency amounts, `exchange_rate`, `against_account`,
`cost_center`, `project`, `reference_type`, `reference_name`, `user_remark`.

**Example**

```json
{"name": "get_journal_entry", "arguments": {"name": "ACC-JV-2026-00184"}}
```

```json
{
  "name": "ACC-JV-2026-00184",
  "posting_date": "2026-06-28",
  "company": "Example Trading Co",
  "voucher_type": "Bank Entry",
  "total_debit": 1450.0,
  "total_credit": 1450.0,
  "user_remark": "June clearing sweep",
  "docstatus": 1,
  "docstatus_label": "submitted",
  "balanced": true,
  "accounts": [
    {"idx": 1, "account": "1110 - Bank Checking - ETC", "debit": 1450.0, "credit": 0.0,
     "cost_center": "Main - ETC"},
    {"idx": 2, "account": "1190 - Cash Clearing - ETC", "debit": 0.0, "credit": 1450.0,
     "cost_center": "Main - ETC"}
  ]
}
```

---

## 5. `list_bank_transactions`

**Arguments**

| Name | Required | Notes |
| --- | --- | --- |
| `bank_account` | no | Docname or its `account_name`. Omit for all accounts |
| `from_date` | no | Either bound may be given alone |
| `to_date` | no | |
| `status` | no | As this site spells it: `Pending`, `Settled`, `Reconciled`, `Unreconciled` |
| `limit` | no | Default 100, max 500 |

**Returns** `bank_transactions[]` — every field this version has, plus
`amount_signed` (positive money in, negative money out) — with `count`, `limit`,
`truncated`, `amount_layout` (`deposit_withdrawal` or `signed_amount`),
`sign_convention` and `filters`.

**Example**

```json
{"name": "list_bank_transactions",
 "arguments": {"bank_account": "Operating", "from_date": "2026-06-01",
               "to_date": "2026-06-30", "status": "Unreconciled"}}
```

```json
{
  "bank_transactions": [
    {"name": "BT-2026-00412", "date": "2026-06-14",
     "bank_account": "Operating - Example Bank", "company": "Example Trading Co",
     "description": "ACH CREDIT CUSTOMER PMT", "status": "Unreconciled",
     "reference_number": "A7741208", "currency": "USD",
     "deposit": 2400.0, "withdrawal": 0.0,
     "allocated_amount": 0.0, "unallocated_amount": 2400.0,
     "docstatus": 1, "amount_signed": 2400.0}
  ],
  "count": 1,
  "limit": 100,
  "truncated": false,
  "amount_layout": "deposit_withdrawal",
  "sign_convention": "amount_signed is positive for money in, negative for money out.",
  "filters": {"bank_account": "Operating - Example Bank", "from_date": "2026-06-01",
              "to_date": "2026-06-30", "status": "Unreconciled"}
}
```

---

## 6. `get_bank_statement`

**Arguments:** `name` (required).

**Returns** every scalar field the doctype has on this site, plus `child_tables`
keyed by fieldname. Mirrors whatever is there rather than naming columns, because
the field set has changed across versions.

**Not present on every site.** Bank Statement shipped later than Bank Transaction.
Where it is absent this tool returns an error saying so and pointing at
`get_company_topology`, which reports the doctype's presence up front:

```
the 'Bank Statement' DocType is not installed on this site. It is only present on
ERPNext versions that ship the Bank Statement doctype; get_company_topology
reports whether this site has it.
```

---

## 7. `list_fiscal_years`

Worth calling before you choose a `posting_date`: ERPNext rejects dates outside a
fiscal year, which is otherwise a confusing failure.

**Arguments:** `company` (optional).

**Returns** `fiscal_years[]` (`name`, `year_start_date`, `year_end_date`,
`disabled`), newest first; `count`; `company`; `company_agnostic_years[]` — years
with no company links, which apply to every company; and a `note` saying so.

**Example**

```json
{"name": "list_fiscal_years", "arguments": {"company": "ETC"}}
```

```json
{
  "fiscal_years": [
    {"name": "2026", "year_start_date": "2026-01-01", "year_end_date": "2026-12-31", "disabled": 0},
    {"name": "2025", "year_start_date": "2025-01-01", "year_end_date": "2025-12-31", "disabled": 0}
  ],
  "count": 2,
  "company": "Example Trading Co",
  "company_agnostic_years": ["2025"],
  "note": "A Fiscal Year with no company links applies to every company; those are listed in company_agnostic_years."
}
```

---

## 8. `get_chart_of_accounts`

**Arguments**

| Name | Required | Notes |
| --- | --- | --- |
| `company` | yes | |
| `root_type` | no | One of `Asset`, `Liability`, `Income`, `Expense`, `Equity` |

**Returns** `accounts[]` — a nested tree, each node with `account_name`,
`account_number`, `parent_account`, `is_group`, `root_type`, `account_type`,
`account_currency`, `disabled` and `children[]` — plus `company`, `root_type`,
`flat_count` and a `note`.

Filtering by `root_type` can cut a group out from above a node it kept. Those
nodes surface at the top level of the response rather than disappearing.

**Example**

```json
{"name": "get_chart_of_accounts", "arguments": {"company": "ETC", "root_type": "Income"}}
```

```json
{
  "company": "Example Trading Co",
  "root_type": "Income",
  "accounts": [
    {"name": "Income - ETC", "account_name": "Income", "is_group": 1, "root_type": "Income",
     "children": [
       {"name": "4100 - Sales - ETC", "account_name": "Sales", "account_number": "4100",
        "is_group": 0, "root_type": "Income", "children": []}
     ]}
  ],
  "flat_count": 2,
  "note": "children[] is nested; flat_count is every account in the response."
}
```

---

## 9. `list_unreconciled_bank_transactions`

The reconciliation worklist for one bank account, oldest first.

**Arguments:** `bank_account` (required), `limit` (optional).

**Returns** `unreconciled[]` — each row with `amount_signed`, `gross_amount`,
`allocated_amount_effective` and `unallocated_amount_effective` — plus `count`,
`bank_account`, `limit`, `truncated`, `amount_layout`, and `unallocated_source`
(`column` where the site has an `unallocated_amount` field, `computed (gross -
allocated)` where it does not).

**Example**

```json
{"name": "list_unreconciled_bank_transactions",
 "arguments": {"bank_account": "Operating", "limit": 25}}
```

```json
{
  "unreconciled": [
    {"name": "BT-2026-00389", "date": "2026-06-03",
     "bank_account": "Operating - Example Bank", "description": "CHECK 1042",
     "status": "Unreconciled", "deposit": 0.0, "withdrawal": 812.4,
     "amount_signed": -812.4, "gross_amount": 812.4,
     "allocated_amount_effective": 0.0, "unallocated_amount_effective": 812.4}
  ],
  "count": 1,
  "bank_account": "Operating - Example Bank",
  "limit": 25,
  "truncated": false,
  "amount_layout": "deposit_withdrawal",
  "unallocated_source": "column"
}
```

---

## 10. `search_accounts`

The tool that saves a round trip. Ranked exact-number → exact-name → prefix →
substring, so the top hit is usually right.

**Arguments:** `query` (required), `company` (optional), `limit` (optional).

**Returns** `matches[]` (best first), `query`, `company`, `count`,
`total_before_limit`, `note`.

**Example**

```json
{"name": "search_accounts", "arguments": {"query": "cash", "company": "ETC"}}
```

```json
{
  "query": "cash",
  "company": "Example Trading Co",
  "matches": [
    {"name": "1100 - Cash - ETC", "account_name": "Cash", "account_number": "1100",
     "root_type": "Asset", "account_type": "Cash", "is_group": 0, "disabled": 0},
    {"name": "1190 - Cash Clearing - ETC", "account_name": "Cash Clearing",
     "account_number": "1190", "root_type": "Asset", "account_type": "Bank",
     "is_group": 0, "disabled": 0}
  ],
  "count": 2,
  "total_before_limit": 2,
  "note": "Ranked best-first: exact number, exact name, prefix, substring."
}
```
---

# Workflow tools

Four read tools and one write tool over Frappe's Workflow engine. All read-only
unless marked.

The hard part of a workflow question is not "what are the states" — it is
"can *this* user take *this* action on *this* document *right now*", which
depends on the transition's `allowed` role, its `allow_self_approval` flag and a
`condition` expression evaluated against the document. Frappe already answers
that in `frappe.model.workflow.get_transitions`, so `list_available_actions` and
`advance_workflow` call it rather than producing a second opinion. Every response
says which path ran.

---

## 11. `list_workflows`

Every Workflow on the site. Call it to learn a site's approval structure before
asking about any individual document.

**Arguments:** none.

**Returns** `workflows[]` with `name`, `workflow_name`, `document_type`,
`is_active`, `workflow_state_field`, `states[]`, `transitions[]`,
`terminal_states[]` and `roles[]`; plus `count` and `active_count`.

`terminal_states` are states with no outgoing transition — a document in one is
finished, not waiting. That distinction is what `list_pending_approvals` is
built on.

**Example**

```json
{"name": "list_workflows", "arguments": {}}
```

```json
{
  "workflows": [
    {
      "name": "Purchase Order Approval",
      "workflow_name": "Purchase Order Approval",
      "document_type": "Purchase Order",
      "is_active": 1,
      "workflow_state_field": "workflow_state",
      "send_email_alert": 1,
      "states": [
        {"state": "Draft", "doc_status": 0, "allow_edit": "Purchase User"},
        {"state": "Pending Approval", "doc_status": 0, "allow_edit": "Purchase Manager"},
        {"state": "Approved", "doc_status": 1, "allow_edit": "Purchase Manager"},
        {"state": "Rejected", "doc_status": 0, "allow_edit": "Purchase Manager"}
      ],
      "transitions": [
        {"state": "Draft", "action": "Submit for Approval", "next_state": "Pending Approval",
         "allowed": "Purchase User", "allow_self_approval": true,
         "has_condition": false, "condition": null},
        {"state": "Pending Approval", "action": "Approve", "next_state": "Approved",
         "allowed": "Purchase Manager", "allow_self_approval": false,
         "has_condition": false, "condition": null},
        {"state": "Pending Approval", "action": "Reject", "next_state": "Rejected",
         "allowed": "Purchase Manager", "allow_self_approval": true,
         "has_condition": true, "condition": "doc.grand_total > 0"}
      ],
      "terminal_states": ["Approved", "Rejected"],
      "roles": ["Purchase Manager", "Purchase User"]
    }
  ],
  "count": 1,
  "active_count": 1,
  "note": "terminal_states have no outgoing transition — a document there is finished, not waiting."
}
```

---

## 12. `get_workflow_state`

Where one document sits, and where it could go. Answers "who can move this";
`list_available_actions` answers "can I move this".

**Arguments:** `doctype` (required), `name` (required).

**Returns** `workflow`, `workflow_state_field`, `current_state`,
`current_state_detail`, `docstatus`, `docstatus_label`, `next_transitions[]`,
`is_terminal`. A document whose state field is empty — created before the
workflow was added — gets a `note` saying so rather than an error.

**Refused:** a doctype no active workflow governs (the error lists the ones that
are governed), an unknown document, an unknown doctype.

**Example**

```json
{"name": "get_workflow_state",
 "arguments": {"doctype": "Purchase Order", "name": "PUR-ORD-2026-00184"}}
```

```json
{
  "doctype": "Purchase Order",
  "name": "PUR-ORD-2026-00184",
  "workflow": "Purchase Order Approval",
  "workflow_state_field": "workflow_state",
  "current_state": "Pending Approval",
  "current_state_detail": {"state": "Pending Approval", "doc_status": 0,
                           "allow_edit": "Purchase Manager"},
  "docstatus": 0,
  "docstatus_label": "draft",
  "next_transitions": [
    {"state": "Pending Approval", "action": "Approve", "next_state": "Approved",
     "allowed": "Purchase Manager", "allow_self_approval": false,
     "has_condition": false, "condition": null}
  ],
  "is_terminal": false
}
```

---

## 13. `list_pending_approvals`

The worklist. Documents parked in a state that still has an action available,
grouped by workflow and state.

**Arguments**

| Name | Required | Notes |
| --- | --- | --- |
| `user` | no | Only states this user's roles can act on — the "what is waiting on me" question |
| `workflow` | no | Restrict to one Workflow |
| `limit` | no | Documents **per state**. Default 100, max 500 |

**Returns** `pending[]` — one entry per (workflow, state) with `actions[]`,
`allowed_roles[]`, `count`, `truncated` and `documents[]` — plus `group_count`,
`document_count`, `user_roles` and `limit_per_state`.

Terminal states are never listed. Cancelled documents (`docstatus 2`) are
excluded.

**Example**

```json
{"name": "list_pending_approvals", "arguments": {"user": "avi@example.com"}}
```

```json
{
  "pending": [
    {
      "workflow": "Purchase Order Approval",
      "doctype": "Purchase Order",
      "state": "Pending Approval",
      "actions": ["Approve", "Reject"],
      "allowed_roles": ["Purchase Manager"],
      "count": 2,
      "truncated": false,
      "documents": [
        {"name": "PUR-ORD-2026-00184", "owner": "bea@example.com",
         "modified": "2026-07-11 09:14:02", "docstatus": 0}
      ]
    }
  ],
  "group_count": 1,
  "document_count": 2,
  "user": "avi@example.com",
  "user_roles": ["Purchase Manager", "Purchase User"],
  "limit_per_state": 100,
  "note": "Only states with an outgoing transition are listed; terminal states are finished, not pending. Counts are per state and capped at limit_per_state."
}
```

---

## 14. `list_available_actions`

What the acting MCP user can do to this document right now.

"Acting MCP user" is the configured **MCP System User**, not the human at the
other end of the chat. That is the honest answer, because it is the user the
transition would actually run as.

**Arguments:** `doctype` (required), `name` (required).

**Returns** `user`, `user_roles[]`, `current_state`, `available_actions[]`,
`transitions[]`, `resolved_via`, `conditions_evaluated`.

`resolved_via` is `"frappe.model.workflow.get_transitions"` normally. On a
Frappe that does not export it, this app falls back to a role-only check,
`conditions_evaluated` is `false` and a `warning` says the list is a **superset**
— an action in it may still be refused. Transitions carrying a condition are
flagged `has_condition`.

A condition expression that raises is reported, not swallowed: a site bug should
not quietly become a laxer answer.

**Example**

```json
{"name": "list_available_actions",
 "arguments": {"doctype": "Purchase Order", "name": "PUR-ORD-2026-00184"}}
```

```json
{
  "doctype": "Purchase Order",
  "name": "PUR-ORD-2026-00184",
  "workflow": "Purchase Order Approval",
  "user": "mcp@example.com",
  "user_roles": ["Accounts User", "Purchase Manager"],
  "current_state": "Pending Approval",
  "available_actions": ["Approve", "Reject"],
  "transitions": [
    {"state": "Pending Approval", "action": "Approve", "next_state": "Approved",
     "allowed": "Purchase Manager", "allow_self_approval": 0}
  ],
  "resolved_via": "frappe.model.workflow.get_transitions",
  "conditions_evaluated": true
}
```

---

# Report tools

## 15. `list_reports`

**Arguments:** `module` (optional), `is_standard` (optional — `Yes`/`No`, or a
boolean).

**Returns** `reports[]` with `name`, `ref_doctype`, `report_type`, `module`,
`is_standard`, `disabled`, `prepared_report`; plus `count` and `by_report_type`.

`ref_doctype` is what the report reports on, and is what a permission check runs
against when you call `run_report`.

---

## 16. `run_report`

The highest-leverage tool in the catalogue. A site's reports are where its
accounting questions have already been answered correctly by somebody who knew
the schema.

**Arguments**

| Name | Required | Notes |
| --- | --- | --- |
| `name` | yes | Report docname, exactly as `list_reports` gives it |
| `filters` | no | The report's own filter fieldnames, as an object. A JSON string is accepted too |
| `user` | no | Run as this user instead of the configured MCP user |
| `limit` | no | Rows returned. Default 100, max 500; `total_rows` reports how many the report produced |

**Three engines, one tool.** Query and Script Reports run through
`frappe.desk.query_report.run` with `ignore_prepared_report=True` — without that,
a report configured as a Prepared Report queues a background job and hands back a
job id instead of rows. Report Builder reports have no server-side "run", so they
are materialised from their saved column/filter/sort config through
`frappe.desk.reportview.get`, falling back to `frappe.get_list` on versions where
that call has moved. `executed_via` names the path.

**Permissions apply here.** Unlike the ledger read tools, this runs through the
Desk APIs, which check the acting user's permission on `ref_doctype`. A report
the MCP System User may not read fails with a message naming the doctype.

**Returns** `report`, `report_type`, `ref_doctype`, `executed_as`,
`executed_via`, `filters_applied`, `columns`, `columns_normalised`, `rows`,
`row_count`, `total_rows`, `truncated`, `limit`, `row_format`, plus the report's
own `message` / `chart` / `report_summary` where it produced them.

`columns_normalised` parses the old colon-delimited column form
(`"Outstanding:Currency/USD:120"`) into `{fieldname, label, fieldtype, options,
width}` so a model does not have to.

**Example**

```json
{"name": "run_report",
 "arguments": {"name": "Accounts Receivable Summary",
               "filters": {"company": "Example Trading Co", "report_date": "2026-06-30"},
               "limit": 50}}
```

```json
{
  "report": "Accounts Receivable Summary",
  "report_type": "Script Report",
  "ref_doctype": "Sales Invoice",
  "executed_as": "mcp@example.com",
  "executed_via": "frappe.desk.query_report.run",
  "filters_applied": {"company": "Example Trading Co", "report_date": "2026-06-30"},
  "columns": [
    {"fieldname": "party", "label": "Customer", "fieldtype": "Link", "options": "Customer", "width": 200},
    "Outstanding:Currency/USD:120"
  ],
  "columns_normalised": [
    {"fieldname": "party", "label": "Customer", "fieldtype": "Link", "options": "Customer", "width": 200},
    {"fieldname": "outstanding", "label": "Outstanding", "fieldtype": "Currency", "options": "USD", "width": 120}
  ],
  "rows": [{"party": "Northwind Grocers", "outstanding": 2500.0}],
  "row_count": 1,
  "total_rows": 1,
  "truncated": false,
  "limit": 50,
  "row_format": "objects"
}
```

---

# Attachment tools

The three tools in this app that check Frappe permissions on the way in. A File
is whatever somebody uploaded — a signed contract, a passport scan, a payroll
export — and `is_private` is a promise the framework makes about who can see it.
The two read tools are here; `attach_file_to_document`, which writes, is **77**.

---

## 17. `list_attachments`

**Arguments:** `doctype` (required), `name` (required).

**Returns** `attachments[]` with `file_name`, `file_url`, `file_size`,
`size_human`, `mime_type`, `is_private`, `uploaded_by`, `uploaded_on` and
`retrievable` (false for anything over the default size cap); plus `count`,
`total_size` and `total_size_human`.

**Refused** unless the acting user may `read` the parent document. Listing what
is attached to a document you cannot read is itself a leak — filenames alone
often say enough.

---

## 18. `get_attachment_content`

**Arguments:** `name` (required — the **File docname**, not the filename),
`max_bytes` (optional; default 2097152, hard ceiling 8388608).

**Returns** `file_name`, `file_url`, `is_private`, `attached_to_doctype`,
`attached_to_name`, `uploaded_by`, `uploaded_on`, `file_size`, `size_human`,
`mime_type`, `encoding` (`"base64"`) and `content_base64`.

**Authorization**, in three cases: attached to a document → the parent's `read`
permission decides; unattached and private → only its owner or a System Manager;
public and unattached → readable. The File doctype's own `has_permission` is
consulted as well, so a site that has customised file access keeps it.

**Size.** Base64 inflates by a third and a token is roughly four characters, so a
2 MB file is on the order of 700k tokens. The cap is a guard against a hung
request, not a suggestion. A stale `file_size` does not get past it — the bytes
on disk are re-checked after reading.

```
payroll-export.csv is 5.0 MB, over the 2.0 MB cap. Raise max_bytes (hard ceiling
8.0 MB), or fetch it from /private/files/payroll-export.csv. Base64 inflates
content by a third, so anything past a few hundred kilobytes will not fit in a
model's context anyway.
```

---

# Comment and task tools

## 19. `list_comments`

**Arguments:** `doctype` (required), `name` (required), `comment_type`
(optional), `limit` (optional).

**Returns** `comments[]` with `comment_type`, `content`, `author`, `added_on`;
plus `count` and `by_comment_type`.

Frappe keeps framework chatter in the same table as things people typed.
`comment_type: "Comment"` is a human remark; `Info`, `Assigned`, `Workflow`,
`Edit` and friends are generated. Filter on it.

**Refused** unless the acting user may `read` the document.

---

## 20. `list_assigned_todos`

**Arguments:** `user` (optional), `status` (optional — `Open` by default; pass an
empty string for every status), `limit` (optional).

**Returns** `todos[]` with `assigned_to` (normalised), `status`, `priority`,
`date`, `description`, `reference_type`, `reference_name` and `overdue`; plus
`count`, `overdue_count` and `assignee_field`.

**A naming trap, handled.** Frappe's `owner` is whoever *created* the ToDo; the
assignee is `allocated_to` (and was `owner` on versions before that field
existed). The response carries both plus a normalised `assigned_to`, and
`assignee_field` says which column this site actually uses.

---

# HR tools

**Only present where the `hrms` app is installed.** On a site without it these
three are not advertised in `tools/list` and cannot be called.

---

## 21. `list_employees`

**Arguments:** `status` (default `Active`; empty for all), `department`,
`designation`, `company`, `limit`.

**Returns** `employees[]` with `name` (the `HR-EMP-…` docname the other HR tools
want), `employee_name`, `employee_number`, `department`, `designation`, `status`,
`date_of_joining`; plus `count` and `by_department`.

---

## 22. `get_attendance_summary`

**Arguments:** `from_date` (required), `to_date` (required), `employee`
(optional — docname, employee number, name or user id), `department` (optional).

**Returns** `employees[]`, each with `counts` keyed by every status seen on the
site plus the standard five, and `total_marked`; plus site-wide `totals`,
`records_counted` and `statuses[]`.

Aggregated, not day-by-day: a month for a team of forty is 1,200 rows that say
what a count says. Counts **submitted** Attendance only — a draft row is not
evidence anybody turned up. Days with no Attendance record at all are absent from
the counts rather than counted as Absent.

**Example**

```json
{"name": "get_attendance_summary",
 "arguments": {"from_date": "2026-06-01", "to_date": "2026-06-30",
               "department": "Operations"}}
```

```json
{
  "from_date": "2026-06-01",
  "to_date": "2026-06-30",
  "employees": [
    {"employee": "HR-EMP-00001", "employee_name": "Ada Orchard",
     "department": "Operations",
     "counts": {"Absent": 0, "Half Day": 0, "On Leave": 1, "Present": 19,
                "Work From Home": 2},
     "total_marked": 22}
  ],
  "employee_count": 1,
  "totals": {"Absent": 0, "Half Day": 0, "On Leave": 1, "Present": 19, "Work From Home": 2},
  "records_counted": 22,
  "statuses": ["Absent", "Half Day", "On Leave", "Present", "Work From Home"]
}
```

---

## 23. `get_leave_balance`

**Arguments:** `employee` (required), `leave_type` (optional — omit for every
type the employee has an allocation for), `as_of` (optional, defaults to today).

**Returns** `balances[]` of `{leave_type, balance}`, `total_balance`,
`computed_via`, and `failed[]` if one leave type errored.

Computed by HR's own `get_leave_balance_on`, which nets allocations against
applications and handles carry-forward and expiry. Do not reproduce this by
subtracting: those rules are the entire difficulty of the question. If the site's
HR app does not export the function, the tool refuses and points at the Leave
Balance report via `run_report` rather than guessing.

Only leave types with an allocation covering `as_of` are included — a balance for
a type nobody allocated is always zero and only adds noise. One misconfigured
type lands in `failed[]` without losing the others.

---

# Sales and purchasing tools

## 24. `list_sales_orders`

**Arguments:** `status`, `from_date`, `to_date`, `customer`, `company`, `limit`.

**Returns** `orders[]` with `customer`, `transaction_date`, `delivery_date`,
`grand_total`, `currency`, `status`, `per_delivered`, `per_billed`,
`docstatus_label`; plus `count`, `total_value` (of the rows returned — a partial
figure when `truncated`), and `by_status`.

---

## 25. `get_outstanding_invoices`

Submitted Sales Invoices with `outstanding_amount > 0`, aged against a date.

**Arguments:** `customer`, `company`, `as_of` (defaults to today), `limit`.

**Returns** `invoices[]`, each with `days_overdue` and `ageing_bucket`; plus
`total_outstanding` and `buckets` totalling count and outstanding per bucket.

**Buckets:** `current` (not yet due), `0-30`, `31-60`, `61-90`, `90+`, and
`unknown` (no `due_date`). `days_overdue = as_of - due_date`.

`current` exists because folding not-yet-due invoices into `0-30` makes an AR
summary look worse than the business is — an invoice issued yesterday on 30-day
terms is zero days of exposure, not thirty. `unknown` exists because an invoice
nobody put terms on is a real problem, and hiding it in `current` is how it stays
one.

**Example**

```json
{"name": "get_outstanding_invoices",
 "arguments": {"company": "Example Trading Co", "as_of": "2026-07-25"}}
```

```json
{
  "invoices": [
    {"name": "ACC-SINV-2026-00005", "customer": "Westbrook Cafe",
     "posting_date": "2025-12-02", "due_date": "2026-01-02",
     "grand_total": 5000.0, "outstanding_amount": 5000.0, "currency": "USD",
     "days_overdue": 204, "ageing_bucket": "90+"}
  ],
  "count": 1,
  "as_of": "2026-07-25",
  "total_outstanding": 5000.0,
  "buckets": {
    "current": {"count": 0, "outstanding": 0.0},
    "0-30": {"count": 0, "outstanding": 0.0},
    "31-60": {"count": 0, "outstanding": 0.0},
    "61-90": {"count": 0, "outstanding": 0.0},
    "90+": {"count": 1, "outstanding": 5000.0},
    "unknown": {"count": 0, "outstanding": 0.0}
  },
  "bucket_definition": "days_overdue = as_of - due_date. 'current' is not yet due (days_overdue <= 0); '0-30', '31-60', '61-90' and '90+' are days past due; 'unknown' is an invoice with no due_date."
}
```

---

## 26. `list_purchase_orders`

The mirror of `list_sales_orders`, with the nouns swapped: `supplier` instead of
`customer`, `schedule_date` instead of `delivery_date`, `per_received` instead of
`per_delivered`.

**Arguments:** `status`, `from_date`, `to_date`, `supplier`, `company`, `limit`.

---

## Purchasing & AP (v0.68.0)

Sprint 3 of the Gap Closure Plan: the rest of the purchasing pipeline
`list_purchase_orders` and `get_outstanding_invoices` did not cover — receiving
goods, billing them, and paying the bill. The mutating half (create/submit for
each of the four documents) is under **Mutating tools**.

### `get_purchase_order`

One Purchase Order in full, including its line items with `received_qty` and
`billed_amt`.

**Arguments:** `name` (required).

### `get_purchase_receipt`

One Purchase Receipt in full, including its line items.

**Arguments:** `name` (required).

### `list_purchase_receipts`

**Arguments:** `status`, `supplier`, `purchase_order`, `company`, `from_date`,
`to_date`, `limit`.

**Returns** `receipts[]` with `docstatus_label`; plus `count`, `total_value`,
`truncated`.

### `get_purchase_invoice`

One Purchase Invoice in full, including its line items.

**Arguments:** `name` (required).

### `list_purchase_invoices`

**Arguments:** `status`, `supplier`, `from_date`, `to_date`, `outstanding_only`,
`company`, `limit`.

**Returns** `invoices[]` with `docstatus_label`; plus `count`, `total_value`,
`total_outstanding`.

### `get_payment_entry`

One Payment Entry in full, including its `references[]` (the invoices it
allocates against, with `allocated_amount` and `outstanding_amount`).

**Arguments:** `name` (required).

### `list_payment_entries`

`payment_type='Pay'` and `party_type='Supplier'` only — the AP side. A Payment
Entry that receives money from a Customer does not appear here.

**Arguments:** `supplier`, `from_date`, `to_date`, `docstatus`, `company`,
`limit`.

**Returns** `payments[]` with `docstatus_label`; plus `count`, `total_paid`.

### `get_ap_aging`

Accounts Payable ageing for one company, grouped by supplier.

**Arguments:** `company` (required), `supplier`, `as_of` (defaults to today),
`limit`.

**THE TOTAL** comes from GL Entry against every account typed Payable —
`credit - debit` summed per supplier, the true ledger balance regardless of
what wrote it. **THE BUCKETS** come from open Purchase Invoices' own
`outstanding_amount` and `due_date` — the same approach `get_outstanding_invoices`
takes on the receivables side, and the only reliable one: a Payment Entry's own
GL rows do not say which invoice they paid, so there is no way to net a
payment's debit against one specific invoice's credit by reading GL Entry
alone. The two are cross-checked per supplier, and a `drift` field appears
where they disagree — usually a manual Journal Entry against the Payable
account outside the normal invoice/payment flow.

**Buckets:** `current`, `0-30`, `31-60`, `61-90`, `90+`, `unknown` (no
`due_date`) — same definitions as `get_outstanding_invoices`.

**Example**

```json
{"name": "get_ap_aging",
 "arguments": {"company": "Example Trading Co", "as_of": "2026-07-24"}}
```

```json
{
  "suppliers": [
    {
      "supplier": "Example Supplies Inc",
      "outstanding": 8500.0,
      "gl_balance": 8500.0,
      "invoices": [
        {"name": "ACC-PINV-2026-00002", "due_date": "2026-07-10",
         "outstanding_amount": 500.0, "days_overdue": 14, "ageing_bucket": "0-30"}
      ],
      "buckets": {"0-30": {"count": 1, "outstanding": 500.0}}
    }
  ],
  "count": 1,
  "as_of": "2026-07-24",
  "total_outstanding": 8500.0,
  "gl_total_outstanding": 8500.0,
  "buckets": {"0-30": {"count": 1, "outstanding": 500.0}}
}
```

A supplier whose GL balance and open-invoice total disagree carries `drift`
(the difference) and `drift_note` (why it likely happened) on that row.

### `create_purchase_order` — MUTATING, default off

Create a DRAFT Purchase Order against a Supplier. `docstatus 0`, affects no
balance. Cannot submit — that is `submit_purchase_order`, separately switched.

**Arguments:** `company`, `supplier` (required), `transaction_date` (defaults
to today), `schedule_date` (required — applied to every line that does not set
its own), `items[]` (required, each `item_code`, `qty`, `rate`, `warehouse`
required; `uom`, `cost_center` optional).

**Returns** `name`, `docstatus` (0), `grand_total`, `item_count`, `status`.

### `submit_purchase_order` — MUTATING, default off

`docstatus 0 → 1`, status moves to an active buying state (`To Receive and
Bill`, or further along if `per_received`/`per_billed` already progressed).
Takes a name, not a document — cannot create the order it submits.

**Arguments:** `name` (required).

**Refused:** already submitted, cancelled, or does not exist.

### `create_purchase_receipt` — MUTATING, default off

Create a DRAFT Purchase Receipt — goods received from a Supplier. `docstatus
0`, no stock ledger entries yet.

**Arguments:** `company`, `supplier` (required), `posting_date` (defaults to
today), `purchase_order` (optional — validated as submitted, for the same
supplier), `items[]` (required, each `item_code`, `qty`, `warehouse` required;
`rate`, `purchase_order`, `purchase_order_item`, `cost_center` optional).

**Returns** `name`, `docstatus` (0), `grand_total`, `item_count`, `status`.

**Refused:** `purchase_order` for a different supplier, or one that is not yet
submitted.

### `submit_purchase_receipt` — MUTATING, default off

`docstatus 0 → 1`. On a real site this is what creates the Stock Ledger
Entries that move the received quantity into the warehouse — this tool
triggers ERPNext's own controller; it computes nothing itself.

**Arguments:** `name` (required).

### `create_purchase_invoice` — MUTATING, default off

Create a DRAFT Purchase Invoice against a Supplier. `docstatus 0`, affects no
balance. Optionally linked to `purchase_order` and/or `purchase_receipt` for
provenance — neither is required, since a utility bill has no receipt behind
it.

**Arguments:** `company`, `supplier` (required), `posting_date` (defaults to
today), `due_date`, `bill_no`, `bill_date`, `purchase_order`,
`purchase_receipt`, `credit_to` (defaults to the company's
`default_payable_account`, or its sole account typed Payable), `items[]`
(required, each `item_code`, `qty`, `rate`, `expense_account` required;
`cost_center`, `warehouse`, `purchase_order`, `purchase_receipt` optional per
line).

**Returns** `name`, `docstatus` (0), `grand_total`, `outstanding_amount`,
`credit_to`, `item_count`, `status`.

This is the tool `create_purchase_invoice_from_receipt` (Receipt Enhancement &
Owner Draw, below) builds
on: it resolves the Supplier, expense account and Item from an Expense Receipt
and hands the same shape to this one.

### `submit_purchase_invoice` — MUTATING, default off

`docstatus 0 → 1`. **This writes GL Entries and moves a balance** — books
every line's `expense_account` and credits `credit_to` for the total, exactly
as ERPNext's own controller does.

**Arguments:** `name` (required).

**Returns** `name`, `docstatus` (1), `status`, `outstanding_amount`,
`gl_entries_created`.

### `create_payment_entry` — MUTATING, default off

Create a DRAFT Payment Entry paying a Supplier. `payment_type` is always
`Pay`, `party_type` always `Supplier` — the AP side only. `docstatus 0`,
affects no balance.

**Arguments:** `company`, `supplier` (required), `posting_date` (defaults to
today), `paid_amount` (required), `paid_from` (defaults to the company's
`default_bank_account` or `default_cash_account`), `paid_to` (defaults like
`credit_to` above), `reference_no`, `reference_date`, `mode_of_payment`,
`references[]` (optional — each `reference_name` and `allocated_amount`;
omit, or allocate less than `paid_amount` in total, for an on-account
payment).

**Returns** `name`, `docstatus` (0), `allocated_total`, `unallocated_amount`,
`paid_from`, `paid_to`.

**Refused:** a reference's `allocated_amount` exceeding that invoice's
`outstanding_amount`; a reference to an invoice billed to a different
supplier; references allocating more than `paid_amount` in total.

### `submit_payment_entry` — MUTATING, default off

`docstatus 0 → 1`. **This writes GL Entries and moves a balance** — debits
`paid_to`, credits `paid_from`, and reduces every referenced Purchase
Invoice's `outstanding_amount`, exactly as ERPNext's own controller does.

**Arguments:** `name` (required).

**Returns** `name`, `docstatus` (1), `gl_entries_created`, `references[]`.

## Stock & Inventory (v0.69.0)

Sprint 4 of the Gap Closure Plan. `list_warehouses` and `get_item` (v0.66.0)
can name a shed and a chemical; these are the tools that say how much of the
chemical is in the shed, how it got there, and when to buy more.

**Three doctypes, three questions, and they are easy to confuse.** **Stock
Entry** is the *instruction* and until it is submitted it has moved nothing.
**Stock Ledger Entry** is the *immutable history* — one row per item per
warehouse per movement, written by ERPNext at submit. **Bin** is the *current
balance*, which ERPNext maintains from that history. Nothing in this app writes
a ledger row or a Bin; `submit_stock_entry` asks ERPNext's own controller to,
and the read tools report what it did.

**One `warehouse` argument, two columns.** `Stock Entry Detail` has
`s_warehouse` (out of) and `t_warehouse` (into), and the entry type decides
where a line's warehouse goes: **Material Receipt** → it is where the stock
lands; **Material Issue** → where it leaves from; **Material Transfer** → the
source, with `target_warehouse` required and different. A `target_warehouse` on
a Receipt or an Issue is **refused rather than ignored** — the two readings of
"I passed both" have opposite consequences.

**A UOM this site cannot convert is a refusal.** `qty: 3, uom: "Case"` on an
item stocked in Lb needs a conversion on the Item's own UOMs table. Defaulting
the factor to 1 would post three pounds where thirty-six were meant, so an
unconvertible UOM raises with the stock UOM named and nothing written.

### `get_stock_entry`

One Stock Entry in full: every line with its source and target warehouse, the
quantity in both the entered and the stock UOM, its status, and where the
movement came from.

**Arguments:** `name` (required).

**Returns** `entry_type`, `status`, `items[]`, `item_count`, `total_qty`,
`total_value`, `source` (`{doctype, name, stored_on}` or null).

### `list_stock_entries`

The `warehouse` and `item_code` filters are applied against the **lines** —
neither is a column on the Stock Entry header — so an empty result carries a
`note` saying it is an empty match rather than an unfiltered list.

**Arguments:** `company` (required), `entry_type`, `warehouse`, `item_code`,
`from_date`, `to_date`, `limit`.

**Returns** `entries[]` with `entry_type`, `status` and `docstatus_label`; plus
`count`, `total_value`, `truncated`.

### `get_stock_balance`

On-hand quantity and value for one item, per warehouse, read from Bin.

**Arguments:** `item_code` (required), `warehouse`, `company`.

**Bin has no `company` column** — it is scoped only through its warehouse — so
a `company` argument resolves that company's warehouses and filters on them.

**Returns** `item_code`, `item_name`, `uom`, `balances[]` (`warehouse`,
`company`, `qty`, `valuation_rate`, `stock_value`, `reserved_qty`,
`projected_qty`), `warehouse_count`, `total_qty`, `total_value`.

**AN ITEM WITH NO BIN ROW HAS NEVER MOVED,** which is not the same as counted
and empty. ERPNext creates a Bin the first time an item touches a warehouse, so
an empty `balances[]` comes with a `note` saying which of the two it found.

**Example**

```json
{"name": "get_stock_balance",
 "arguments": {"item_code": "SURROUND-WP", "company": "Example Trading Co"}}
```

```json
{
  "item_code": "SURROUND-WP", "item_name": "Surround WP", "uom": "Lb",
  "balances": [
    {"warehouse": "Shop - ETC", "company": "Example Trading Co",
     "qty": 45.0, "valuation_rate": 2.5, "stock_value": 112.5},
    {"warehouse": "Stores - ETC", "company": "Example Trading Co",
     "qty": 80.0, "valuation_rate": 2.5, "stock_value": 200.0}
  ],
  "warehouse_count": 2, "total_qty": 125.0, "total_value": 312.5
}
```

### `get_stock_ledger`

Movement history, newest first. **This is the audit trail, not a balance** —
call `get_stock_balance` for on-hand. Cancelled rows are excluded where the site
marks them: a cancelled movement did not happen, and including it would double
every total built off this list.

**Arguments:** `item_code`, `warehouse`, `from_date`, `to_date`, `limit`.

**Returns** `movements[]` (`posting_date`, `posting_time`, `item_code`,
`warehouse`, `qty_change`, `balance_qty`, `valuation_rate`, `value_change`,
`voucher_type`, `voucher_no`); plus `count`, `net_qty_change`, `truncated`.
`net_qty_change` covers the rows returned, and says so when they were truncated.

### `get_warehouse_summary`

Everything on hand in one warehouse, with each item's reorder rule.

**Arguments:** `warehouse` (required), `company`.

**Returns** `items[]` (`item_code`, `item_name`, `uom`, `qty`,
`valuation_rate`, `stock_value`, `reorder_level`, `reorder_qty`,
`below_reorder`); plus `item_count`, `total_qty`, `total_value`,
`below_reorder_count`, `truncated`. This tool takes no `limit`, so a warehouse
holding more than 500 stocked items reports `truncated` and a `note` rather
than presenting a partial valuation as a total.

### `list_reorder_alerts`

Every item currently below its reorder level, worst shortfall first.

**Arguments:** `company`, `warehouse`.

**Returns** `alerts[]` (`item_code`, `item_name`, `uom`, `warehouse`,
`current_qty`, `reorder_level`, `reorder_qty`, `shortfall`, `stored_on`); plus
`count`, `rules_checked`, `truncated`.

**An item with a rule and no Bin row at all is reported at zero, not skipped.**
That is the opposite of `get_stock_balance`'s treatment of the same absence, and
deliberately so: there the question is "what is on hand" and "never moved" is
the honest answer; here the question is "what must be bought", and never having
arrived is the strongest possible yes. Disabled items are excluded — an alert to
reorder something nobody may buy is a purchase order somebody has to cancel.

### `create_stock_entry` — MUTATING, default off

Create a DRAFT Stock Entry. `docstatus 0`; no Stock Ledger Entry is written and
no balance moves. Cannot submit — that is `submit_stock_entry`, separately
switched.

**Arguments:** `entry_type` (required — `Material Receipt`, `Material Issue` or
`Material Transfer`), `company` (required), `items[]` (required), `posting_date`,
`source_doctype`, `source_name`, `remarks`.

**Each item** needs `item_code`, `qty` (positive) and `warehouse`; optionally
`uom`, `target_warehouse` (Material Transfer only, and required there),
`batch_no` and `basic_rate`.

**Source linkage** is stored in Stock Entry's own link field where ERPNext has
one for that doctype (`purchase_order`, `work_order`, `outgoing_stock_entry`,
`purchase_receipt_no`, `delivery_note_no`, `sales_invoice_no`, `pick_list`) and
otherwise as a `[source: <doctype> <name>]` marker on the first line of
`remarks` — which is how a farm's real sources (a Farm Task, a Scale Ticket)
get recorded without a custom field. `source` on the result reports `stored_on`
either way, so a caller knows whether the link is queryable or just legible.
Pass both `source_doctype` and `source_name` or neither.

**Returns** `name`, `entry_type`, `posting_date`, `status` (`"Draft"`),
`items[]`, `item_count`, `total_qty` (in the stock UOM), `total_value`,
`source`, `next_step`.

### `submit_stock_entry` — MUTATING, default off

`docstatus 0 → 1`. **This is the call that moves the stock**: ERPNext writes the
Stock Ledger Entries and updates every affected Bin, and undoing it is a
cancellation somebody has to explain, not an edit. Cannot create the entry it
submits.

**Arguments:** `name` (required).

**Returns** `name`, `status` (`"Submitted"`), `docstatus` (1), `entry_type`,
`total_qty`, `total_value`.

### `set_reorder_level` — MUTATING, default off

**A reorder rule belongs to a warehouse** — ERPNext stores it on an `Item
Reorder` row keyed by one, and "reorder at 50" with no shed named is not a thing
the doctype can hold — so both are required. Item is not submittable: the rule is
live the moment it is written. The write goes through the same
`masters._set_reorder` that `update_item` uses, so there is one answer to where a
reorder rule lives on a given ERPNext vintage.

**Arguments:** `item_code`, `warehouse`, `reorder_level`, `reorder_qty` (all
required). `reorder_qty` must be positive — ordering nothing is what leaving the
rule unset already does.

**Returns** `item_code`, `warehouse`, `reorder_level`, `reorder_qty`, `created`,
`stored_on`.

## Sales, Settlements & AR (v0.70.0)

Sprint 5 of the Gap Closure Plan. Sprints 2 and 3 built the two ends of the
grower-packer pipeline — the Scale Ticket that says a load was delivered, and
the Settlement Statement that says what the packer eventually paid for it.
These twelve are the middle and the end.

```
Scale Ticket(s) → Settlement Statement → Sales Invoice → Payment Entry
```

**There is no Delivery Note, and that is the design.** ERPNext's Delivery Note
is the *seller's* record of goods leaving on the seller's terms, priced, in
Items and UOMs the seller controls. In grower-packer the seller controls none of
that: the packer owns the scale, prints the ticket, decides the variety and the
grade, and states the price months later. The Scale Ticket **is** the delivery
evidence, so nothing here writes a second record of one delivery for the first
to disagree with.

**Two paths out of a settlement, and the invoice is the default.**
`create_sales_invoice_from_settlement` produces a Sales Invoice, which is what
gives AR ageing, payment allocation and every standard receivables report
something to work with. `post_settlement_to_gl` produces a **draft Journal
Entry** with the same three GL movements and no subledger, for operations that
reconcile settlements against a bank deposit rather than against an invoice.
**A settlement that has been through one is refused by the other** — two
revenue postings for one statement is a double count nobody finds until the year
end.

**The invoice totals to the settlement, not to a recomputation.** A settlement
line keeps a stated gross amount even where weight × price does not produce it
(a pool adjustment is real; the multiplication is not). ERPNext's Sales Invoice
Item has no such tolerance — it computes `amount = qty × rate` on every
validate — so a disagreeing line has its **rate** adjusted, never its amount.
The line comes back with `stated_price_per_unit` beside the rate that was used
and `rate_differs_from_statement: true`, so the adjustment is visible rather
than absorbed.

**Deductions are negative charge rows, not a netted revenue line.** Each
deduction becomes an `Actual` Sales Taxes and Charges row with a negative
amount against `deduction_account`: revenue is recognised **gross**, packing and
storage land in **expense**, and the receivable is the **net**. Netting them
into the revenue line would delete the number a grower most wants a year later —
what did storage cost me.

**Two link fields, set together.** Settlement Statement gains `sales_invoice`
(shipped in this app's own DocType JSON, so it needs `bench migrate`); Sales
Invoice gains `settlement_statement` (a Custom Field, created at install and
again lazily on first use). Either being absent is reported as
`links.{settlement_points_at_invoice, invoice_points_at_settlement}: false`
rather than silently skipped.

### `create_sales_invoice` — MUTATING, default off

A DRAFT Sales Invoice, from hand-written `items` **or** from a submitted
`settlement_statement`. Passing both is refused rather than merged. Cannot
submit.

**Arguments:** `customer`, `company`, `settlement_statement`, `posting_date`,
`due_date`, `items[]` (`item_code`, `qty`, `rate`, `amount`, `uom`,
`description`, `income_account`, `cost_center`), `taxes[]` (`charge_type`,
`account_head`, `description`, `rate`, `tax_amount`), `income_account`,
`debit_to`, `deduction_account`, `include_deductions`, `cost_center`, `notes`.

A settlement that is a draft, is cancelled, already has an invoice, or already
has a posted Journal Entry is refused **by name**. Each priced line lands
against a shared non-stock Item per variety and grade (`FRUIT-HONEYCRISP-XF`),
created once and reused — never one Item per statement.

**Returns** the invoice header, `items[]`, `taxes[]`, `settlement_statement`,
`lines_from_settlement[]` (per line: the Item, how it was resolved, the rate
used, the stated price, and whether they differ), `deductions_posted`,
`deduction_account`, `links{}`, `total_check{}` (the invoice grand total against
what the settlement said, with the variance named), `next_step`.

### `create_sales_invoice_from_settlement` — MUTATING, default off

The one-step form: `create_sales_invoice` with the settlement pre-filled, the
posting date defaulted to the settlement's own date and the due date to thirty
days after it. **Thin on purpose** — there is exactly one implementation of what
a settlement line becomes.

**Arguments:** `settlement_statement` (required; `settlement`/`statement`
aliases), `posting_date`, `due_date`, `income_account`, `debit_to`,
`deduction_account`, `include_deductions`, `cost_center`, `notes`.

**Returns** the same shape as `create_sales_invoice`.

### `get_sales_invoice`

One invoice in full: header, `items[]`, `taxes[]`, `payments[]` (every Payment
Entry allocated against it, with how much each one paid), `total_paid`,
`ageing{}` and `linked_settlement`.

**Arguments:** `sales_invoice` (required; `invoice`/`name` aliases), `as_of`.

Payments are read from the **Payment Entry Reference** table, not from GL Entry
— a payment's ledger rows do not say which invoice they settled. A **draft**
invoice is not aged at all: nothing is owed until it is submitted.

### `list_sales_invoices`

**Arguments:** `customer`, `company`, `status`, `from_date`, `to_date`,
`outstanding_only`, `settlement_statement`, `limit`.

**Returns** `invoices[]`, `count`, `total_grand`, `total_outstanding`,
`by_status{}`, `truncated`. Draft and cancelled invoices are included unless a
status filter excludes them — a draft owes nothing, so it inflates
`total_grand` and not `total_outstanding`.

### `submit_sales_invoice` — MUTATING, default off

Docstatus 0 → 1. **This is the tool that recognises revenue**: ERPNext's own
controller debits the receivable, credits every line's income account and posts
each charge row.

**Arguments:** `sales_invoice` (required; `invoice`/`name` aliases).

**Returns** the header plus `gl_entries[]`, `gl_entries_created`, `gl_totals{}`,
`settlement_statement`. The GL rows are **read back** from GL Entry rather than
computed here — an empty `gl_entries` on a site that has GL Entry means the
submit posted nothing, which is worth investigating before trusting the invoice.

### `receive_payment` — MUTATING, default off

A DRAFT Payment Entry for money received. `payment_type` is always **Receive**
and `party_type` always **Customer** — the AR side only, so a tool that can
collect money cannot be talked into spending it (`create_payment_entry` is the
Pay/Supplier mirror).

**Arguments:** `customer`, `paid_amount` (both required), `company`,
`posting_date`, `reference_no`, `reference_date`, `paid_to`, `paid_from`,
`invoices[]` (`sales_invoice`, `allocated_amount`), `mode_of_payment`.

**Allocation in two modes.** Pass `invoices` to say exactly which invoices a
cheque settles. Pass nothing and it allocates **oldest first** across the
customer's submitted, outstanding invoices — by due date, then posting date —
which is what a remittance with no advice attached means. Whichever ran comes
back as `allocation_method`.

**Money left over is left over:** a payment larger than everything outstanding
is not refused and is not spread onto invoices that do not exist. The remainder
is `unallocated_amount`, a real on-account balance.

**Returns** `payment_entry`, `allocation_method`, `allocated_invoices[]`,
`allocated_total`, `unallocated_amount`, `next_step`. Submitting it is
`submit_payment_entry`, with its own switch.

### `get_settlement_shrink`

Delivered against packed against culled, for one settlement.

**Arguments:** `settlement_statement` (required; `settlement`/`statement`/`name`
aliases).

**The unexplained remainder is reported separately from the cull**, and that
split is the point. Shrink is delivered minus packed; cull is the part of it the
packer reported as culled; the rest — juice, storage loss, fruit not yet run,
weight nobody accounted for — is `unexplained_weight`. A cull percentage is what
a grower renegotiates a contract over; an unexplained percentage is what a
grower asks a question about.

**Returns** `gross_delivered_weight`, `packed_weight`, `cull_weight`,
`shrink_weight`, `unexplained_weight`, `packout_pct`, `shrink_pct`, `cull_pct`,
`unexplained_pct`, `by_variety_grade[]`, `ticket_reconciliation{}`.

`by_variety_grade` takes packed from the settlement's own priced lines and
delivered from the grower's matched Scale Tickets. A variety on one side and not
the other is reported with `comparable: false` and a note rather than dropped —
a packer regrading a load produces exactly that. Tickets in another weight unit
are excluded, never converted.

### `get_packout_summary`

Packout across **submitted** settlements for a period. Packout is packed over
delivered; shrink is its complement.

**Arguments:** `company`, `customer`, `from_date`, `to_date`, `group_by`
(`variety` default, `grade`, `customer`, `field`, `month`).

**Read `basis` before the groups.** The overall figures come from the settlement
headers and are always exact. Per group, only what genuinely attributes is
reported:

| `group_by` | delivered | packed | culled |
|---|---|---|---|
| `customer`, `month` | headers | headers | headers |
| `variety`, `grade` | matched Scale Tickets | priced lines | **null** — a packer states one cull weight per statement |
| `field` | matched Scale Tickets | only where every ticket on a settlement names the **same** field | same |

Whatever cannot be attributed is reported under `unattributed` **rather than
allocated pro-rata**: a pro-rata packout by field is a made-up number that looks
exactly like a measured one.

**Returns** `summary{}`, `groups[]`, `unattributed{}`, `period{}`,
`by_weight_uom{}`, `basis`. More than one weight unit in `by_weight_uom` makes
every total meaningless and the answer says so in `warning`.

### `get_ar_aging`

Receivables ageing grouped by **customer** — the aggregate companion to
`get_outstanding_invoices`, which lists invoices. Both coexist because a
collections call and a dashboard want different shapes.

**Arguments:** `company` (required), `customer`, `as_of`, `limit`.

The mirror of `get_ap_aging`, down to the buckets and the cross-check. Each
customer's `gl_balance` is the true GL Entry balance against every account typed
Receivable; the `current`/`0-30`/`31-60`/`61-90`/`90+`/`unknown` breakdown comes
from open Sales Invoices' own `outstanding_amount` and `due_date`, because a
payment's ledger rows do not say which invoice they settled. A per-customer
`drift` field appears when the two disagree — a manual Journal Entry against the
Receivable account, or a settlement booked with `post_settlement_to_gl`, are the
two usual causes.

**Returns** `customers[]` (each with `total_outstanding`, `buckets{}`,
`invoices[]`, `gl_balance`, optional `drift`), `totals{}`, `buckets{}`,
`total_outstanding`, `gl_total_outstanding`, `as_of`, `invoice_count`.

### `get_season_summary`

The whole pipeline for one date range in one answer.

**Arguments:** `company`, `from_date`, `to_date` (all required), `customer`.

**The gaps are the point.** Any one register read alone looks fine; it is the
joins that go wrong, always in the same three places — fruit delivered that no
settlement claimed, settlements nobody invoiced or posted, and invoices nobody
collected. `pipeline_health` reads `complete` only when all three are empty, and
`gaps[]` says which one is not.

**Returns** `deliveries{}` (ticket count, net weight, `by_variety[]`),
`settlements{}` (count, gross, deductions, net proceeds, weighted
`avg_packout_pct`), `invoicing{}`, `unmatched_tickets{}` (also as
`unsettled_deliveries`), `uninvoiced_settlements{}`, `pipeline_health`, `gaps[]`.

Every stage is filtered by **its own** date, so a November delivery settled in
January is in this window and its settlement is not — which is why unmatched
tickets near the end of a season is normal rather than alarming. Draft Scale
Tickets count in the delivery totals and are excluded from the unmatched list: a
draft is not yet evidence. `avg_packout_pct` is **weighted** (total packed over
total delivered), not the mean of each statement's own percentage.

### `post_settlement_to_gl` — MUTATING, default off

A submitted settlement as a **DRAFT Journal Entry**:

```
debit   receivable_account   net proceeds      (party: the packer)
debit   deduction_account    total deductions
credit  income_account       total gross revenue
```

**Arguments:** `settlement_statement` (required; `settlement`/`statement`/`name`
aliases), `income_account`, `deduction_account`, `receivable_account`,
`cost_center`, `posting_date`.

**The alternative path, and one to choose deliberately.** It gives the same
three GL movements and **no subledger** — the receivable exists as a party
balance, not as a document anybody can age, and `get_ar_aging` will report it as
drift. Most operations want the invoice.

Always a draft: `mutate.insert_draft_journal_entry` is the one place this app
writes a Journal Entry and it cannot be talked into submitting. The settlement is
stamped `posted_journal_entry` and flipped to `Posted` **immediately**, because
the point of the column is that a second posting is refused and a draft nobody
notices is exactly how a second one gets written.

**Returns** `journal_entry`, `debit_total`, `credit_total`, `line_count`,
`accounts{}`, `amounts{}`, `settlement_linked`, `user_remark`, `next_step`.

### `reconcile_settlement_to_tickets` — MUTATING, default off

Match **late-arriving** Scale Tickets to a settlement that already exists.
`create_settlement_statement` matches tickets at capture; a driver's stub found
in a truck in December belongs to a statement filed in November, and this is the
only way to attach it afterwards.

**Arguments:** `settlement_statement`, `scale_tickets[]` (both required).

**The same four checks** run before anything is written, so a settlement is
never left with half its tickets claimed: a **draft** ticket is refused (its
weights can still change), one already matched to another settlement is refused
(two statements paying for one load is the overpayment this register exists to
surface), and so is one from another company or another packer.

**Nothing about the settlement's own numbers is touched.** What moves is the
comparison: `variance_change` is how far the disagreement with the packer
shifted and in which direction, and it is the whole reason to make the call.
Matching a ticket makes the variance **smaller**, so a negative
`variance_change` is the expected direction.

**Returns** `matched_count`, `matched_scale_tickets[]`, `reconciliation_before`,
`updated_reconciliation`, `variance_change`, `ticket_count_before/after`.

**Example**

```json
{"name": "reconcile_settlement_to_tickets",
 "arguments": {"settlement_statement": "SS-ETC-0001",
               "scale_tickets": ["ST-ETC-0007"]}}
```

## Receipt Enhancement & Owner Draw (v0.68.0)

Sprint 3 of the Gap Closure Plan, part two: what happens to an Expense Receipt
after it is captured — correction, reporting, Supplier matching, and the two
destinations `classify_receipt`'s `bill` and `Owner Draw` categories point at.

### `update_expense_receipt` — MUTATING, default off

Correct `cost_center`, `supplier`, `category` or `notes` on a receipt already
captured, in ANY status (Approved included). Never touches `merchant`,
`amount`, `receipt_date`, or any review field — those are either the OCR
reading or the record of a decision, and neither is this tool's business.

**Arguments:** `name` (required; `expense_receipt`/`receipt` aliases),
`cost_center`, `supplier`, `category`, `notes` — at least one required, and
each may be set to `""` to clear it except `category`.

**Returns** `name`, `merchant`, `amount`, `fields_changed[]`, `before{}`,
`after{}`, `alias_learned`.

**Refused:** no field named; every named field already reads what was asked
for; an unknown `category`; a `cost_center` that does not exist.

**v0.75.0: setting a `supplier` teaches a Merchant Alias.** This is the one
place this app's merchant resolution learns anything. Coding a `SIATAPING` slip
to Sawyer's Ace Hardware answers a question no algorithm could — those letters
are not in that name — and the same till prints the same string every week for
years, so the mapping is recorded and the **next** such receipt resolves itself
at capture with its supplier already set. Nothing is asked of the bookkeeper:
they were never shown a form about aliases, and the register grows out of work
they were doing anyway.

`alias_learned.action` is one of:

| Action | What happened |
|---|---|
| `created` | a spelling nothing had seen before |
| `repointed` | the key existed against a *different* Supplier — a later human decision beats an earlier one. The `match_count` is kept, not reset |
| `unchanged` | same key, same Supplier |
| `skipped` | the merchant already normalises to the Supplier's own name, so name matching finds it and a row would be one fact stored twice |

The receipt's own `resolution_method` becomes `Manual` at confidence `1.0` —
not this app being certain, but this app recording that it was not asked.
**Learning never fails the update:** it is a side effect of a write that has
already succeeded, and an alias register that refuses a row must not turn a
completed supplier correction into an error.

### `get_expense_summary`

Expense receipts totalled by category and bucketed into a trend series by
`period` (`week`, `month` or `quarter` — default `month`). Rejected receipts
are excluded from the totals by default; pass `status="Rejected"` to see them
on their own.

**Arguments:** `company`, `from_date`, `to_date`, `period`, `group_by`
(`merchant` or `supplier`, for a second breakdown), `status` (overrides the
Rejected exclusion).

**Returns** `count`, `total_amount`, `by_category{}`, `trend[]` (each with
`period`, `period_start`, `period_end`, `count`, `total`), and `by_merchant{}`
/ `by_supplier{}` when `group_by` is set.

### `get_expense_report`

Every expense receipt in a window, one row each — nothing excluded by default,
unlike the summary. `csv:true` adds a `csv` field with the same rows as a
ready-to-save comma-separated string.

**Arguments:** `company`, `from_date`, `to_date`, `status`, `category`, `csv`,
`limit`.

**Returns** `receipts[]`, `count`, `total_amount`, `truncated`, and `csv` when
asked for.

### `normalize_merchant`

The best-matching Supplier for a merchant string, and how confident that is.
Punctuation and legal-form words (`Co`, `LLC`, `Inc`, `Corp`, `Ltd` …) are
stripped from both sides before comparison, so `WILBUR ELLIS CO` scores high
against `Wilbur-Ellis Company LLC`. Plain string similarity — no ML. This
SUGGESTS a link; it never writes one, the same rule `submit_expense_receipt`'s
own `supplier` argument already follows.

**v0.75.0 — the name is no longer the only evidence.** Pass `merchant_url`,
`merchant_phone`, `store_number`, `card_last_four`, or just the whole
`ocr_raw_text` (they are read off it by anchored patterns), and a five-step
cascade runs under `resolution`:

| # | Step | What it is | Confidence |
|---|------|-----------|-----------|
| 1 | `Alias` | a person's own earlier link for this exact spelling, replayed | 1.0 taught by hand, 0.95 taught by this app or a model |
| 2 | `URL` | a domain, which is registered to one company | 0.90 (0.85 from other coded receipts) |
| 3 | `Phone` | a number, which rings in one building | 0.88 (0.83 from other coded receipts) |
| 4 | `OCR` | the name similarity above | whatever it scores |
| 5 | `LLM` | the raw text read as prose — **prepared here, never asked from here** | the caller's own |

That is how `SIATAPING` resolves to Sawyer's Ace Hardware, which no amount of
string similarity can do: the letters are not there.

`match` and `alternatives` are **unchanged** and still mean the name match
specifically, so a client written against the v0.68.0 shape keeps working.
`resolution.steps` lists every step *including the ones that found nothing*,
with the reason — "why didn't the URL match" is the interesting question on the
day a farm's receipts stop resolving. When nothing deterministic clears the
floor, `resolution.llm_context` carries the signals, the candidate Suppliers
and the question; **this app makes no model call, ever**, the same contract
`validate_document_extraction` follows.

**Arguments:** `merchant` (required), `merchant_url`, `merchant_phone`,
`store_number`, `card_last_four`, `ocr_raw_text` (`text` alias).

**Returns** `merchant`, `match` (`supplier`, `supplier_name`, `confidence`) or
`null`, `alternatives[]`, `threshold`, and `resolution` (`resolved_merchant`,
`supplier`, `method`, `confidence`, `steps[]`, `signals`,
`signals_from_raw_text[]`, `alias`, `llm_context`).

### `list_merchant_aliases`

Merchant strings already linked to a Supplier, grouped by which Supplier.
`aliases` is not a table of its own — it is built by reading every Expense
Receipt whose `supplier` is already set, so it changes for free the day a link
changes.

**v0.75.0** adds `taught` beside it: the **Merchant Alias** register, the
subset of that history somebody turned into a rule the resolver reads at step 1
of its cascade, with a `match_count` saying how many receipts each rule has
since resolved. A spelling in the first and not the second is usually not a
gap — one that already normalises to its Supplier's own name needs no rule,
because name matching finds it.

**Arguments:** `company`, `supplier`.

**Returns** `aliases[]` (each `supplier`, `supplier_name`, `receipt_count`,
`merchant_strings[]`), `count`, `taught[]` (each `alias`, `alias_key`,
`canonical_supplier`, `source`, `match_count`, `last_matched_on`,
`first_learned_from`), `taught_count`, `alias_register_installed`.

### `create_purchase_invoice_from_receipt` — MUTATING, default off

Turn one **Approved** Expense Receipt into a DRAFT Purchase Invoice, via
`purchasing.create_purchase_invoice` (above) — this tool's job is deciding
what to hand it, not writing the document itself.

The Supplier, in order: the receipt's own `supplier` link if set; the
`supplier` argument if given; a `normalize_merchant` match, used automatically
only above a high confidence bar; or a brand-new Supplier created from the
merchant string. Whichever ran is reported as `supplier_resolved_by` — the one
tool in the app that links or creates a Supplier with no human confirming the
match first. The expense account is matched from the receipt's category
against the company's leaf Expense accounts by a short keyword table, the same
shape `record_member_event` uses for equity accounts. The line bills against a
shared, non-stock Item per category (`EXP-<CATEGORY>`), created once and
reused after that — pass `item` to bill against a real stock Item instead.

**Arguments:** `receipt` (required; `expense_receipt` alias), `supplier`,
`expense_account`, `cost_center` (defaults to the receipt's own),
`posting_date` (defaults to `receipt_date`), `due_date`, `bill_no`,
`credit_to`, `item`.

**Returns** `purchase_invoice`, `docstatus` (0), `supplier`,
`supplier_resolved_by`, `expense_account`, `expense_account_resolved_by`,
`item`, `item_resolved_by`, `amount`, `posting_date`.

**Refused:** the receipt is not Approved; the receipt is categorised
`Owner Draw` (use `create_owner_draw` instead); the receipt is already linked
to another document.

### `create_owner_draw` — MUTATING, default off

Record an owner draw / member distribution as a DRAFT Journal Entry: debit an
equity "draw" account, credit bank or cash. **Requires the Member Manager
role** (or System Manager) — checked before anything else runs; an operator
creates that Role in the Desk. Not a new doctype, and independent of the cap
table / Member Event machinery — works whether or not a site has adopted it.

The draw account is matched from the company's leaf Equity accounts by name
(`Member Draws`, `Owner Draw`, `Distributions`, `Drawings` — the same keyword
table `record_member_event` uses for a Distribution or Withdrawal), or named
explicitly with `draw_account`. `receipt` optionally links an Expense Receipt
categorised `Owner Draw` to the Journal Entry this produces, via the same
`linked_doctype`/`linked_document` pair `create_purchase_invoice_from_receipt`
writes.

**Arguments:** `company`, `amount` (required, positive), `date` (required;
`effective_date` alias), `narrative` (required; `reason` alias),
`draw_account` (`equity_account` alias), `counter_account`, `cost_center`,
`party_type` + `party` (attribution on the equity line — pass both or
neither), `receipt` (`expense_receipt` alias).

**Returns** `name`, `docstatus` (0), `draw_account`,
`draw_account_resolved_by`, `counter_account`, `recorded_by`.

**Refused:** no Member Manager or System Manager role; `amount` not positive;
`narrative` too short; `receipt` not categorised `Owner Draw`; `receipt`
already linked to something.

---

# Site-customisation tools

## 27. `list_custom_fields`

The "why is my custom field not showing up" tool.

**Arguments:** `doctype` (optional), `limit` (optional).

**Returns** `custom_fields[]` in form order with `dt`, `fieldname`, `label`,
`fieldtype`, `options`, `insert_after`, `idx`, `reqd`, `hidden`, `read_only`,
`depends_on`, `default`; plus `count` and `by_doctype`.

A field that will not appear is usually hidden, gated by `depends_on`, or
inserted after a fieldname that does not exist on this version — all three are
visible in the response.

---

## 28. `list_client_scripts`

**Arguments:** `doctype` (optional), `enabled` (default `true`; `false` for
disabled only, `"any"` for both), `limit` (optional).

**Returns** `client_scripts[]` with `dt`, `view`, `enabled`, `script_preview`
(first 500 characters), `script_length` and `script_truncated`; plus
`source_doctype` (`Client Script`, or `Custom Script` on pre-v13 sites) and
`preview_chars`.

The full body is never returned. Thousands of lines of form JavaScript is
expensive and rarely the question; the useful facts are which doctype, which
view, and whether it is on.


---

# Mutating tools

**All seven are OFF on a fresh install** and stay off until an operator ticks the
matching `allow_<tool>` box in ERPNext MCP Settings. A call to a switched-off tool
is refused by name, before its arguments are looked at, and logged as `Blocked`:

```
the mutating tool 'submit_journal_entry' is switched off on this site. An operator
must tick 'allow_submit_journal_entry' in ERPNext MCP Settings to enable it.
```

Every write goes through ERPNext's own document methods, so doctype validation,
fiscal-year checks, period-closing vouchers, frozen accounts, mandatory
dimensions and `on_submit` hooks all apply. There is no raw SQL.

---

## 29. `create_journal_entry`

Creates a **draft** — `docstatus=0`, affecting no balance. It cannot submit, and
there is no argument that makes it.

**Arguments**

| Name | Required | Notes |
| --- | --- | --- |
| `company` | yes | |
| `posting_date` | yes | Must fall inside a fiscal year, or ERPNext refuses |
| `accounts` | yes | Array, minimum 2 entries (see below) |
| `user_remark` | yes | Why the entry exists. Recorded on the document and in the audit log |
| `cheque_no` | no | |
| `cheque_date` | no | |
| `voucher_type` | no | E.g. `Bank Entry`. Defaults to the doctype's default |

Each `accounts[]` entry takes `account` plus **exactly one** of `debit` or
`credit`, both positive. Optional per line: `party_type`, `party`, `cost_center`,
`project`, `against_account`, `reference_type`, `reference_name`,
`reference_due_date`, `user_remark`, `exchange_rate`, `account_currency`,
`is_advance`, `bank_account`. Any other key is rejected **by name** rather than
silently dropped.

Custom accounting dimensions go in a per-line **`dimensions`** object —
`{"member": "Member-01", "bbch_stage": "BBCH-8"}` — not alongside the fields
above. They get their own door because a dimension's fieldname is invented by
whoever created it, so there is no list this app could ship; and because simply
accepting unknown keys would turn `amount` (which a model will send, meaning
`debit`) from a corrected mistake into a silently dropped one. Every key is
checked against `Journal Entry Account`'s own fields and every Link value
against the records it can point at, so a dimension that does not exist yet is
refused rather than written to nothing. See tools 47 and 48.

**Refused before anything is written:** debits ≠ credits (tolerance half a cent),
fewer than two lines, a line with both debit and credit, a line with neither, a
negative amount, a group account, an account belonging to another company.

**Returns** `name`, `docstatus` (always 0), `docstatus_label`, `company`,
`posting_date`, `total_debit`, `total_credit`, `line_count`, `user_remark`, and
`next_step` stating that nothing has been posted.

**Example**

```json
{"name": "create_journal_entry",
 "arguments": {
   "company": "Example Trading Co",
   "posting_date": "2026-06-30",
   "user_remark": "Reclassify June clearing balance",
   "accounts": [
     {"account": "1190", "debit": 1450.00, "cost_center": "Main - ETC"},
     {"account": "1110", "credit": 1450.00, "cost_center": "Main - ETC"}
   ],
   "cheque_no": "A7741208"}}
```

```json
{
  "name": "ACC-JV-2026-00190",
  "docstatus": 0,
  "docstatus_label": "draft",
  "company": "Example Trading Co",
  "posting_date": "2026-06-30",
  "total_debit": 1450.0,
  "total_credit": 1450.0,
  "line_count": 2,
  "user_remark": "Reclassify June clearing balance",
  "dimension_fields_set": [],
  "next_step": "This is a draft and affects no balance. Submit it in ERPNext, or via submit_journal_entry if that tool is enabled."
}
```

Unbalanced input, on the other hand:

```
debits (1450.0) do not equal credits (1400.0); difference 50.0. Nothing was created.
```

---

## 30. `submit_journal_entry`

`docstatus 0 → 1`. **This writes GL Entries and moves balances.**

It takes a name and nothing else — it cannot create the entry it submits. Enabling
it means "post things a human or an earlier tool call already wrote down", never
"post something new right now".

**Arguments:** `name` (required) — a draft Journal Entry.

**Returns** `name`, `docstatus` (1), `docstatus_label`, `company`, `posting_date`,
`total_debit`, `total_credit`, `gl_entries_created`.

**Refused:** an entry that is already submitted, one that is cancelled, one that
does not exist.

**Example**

```json
{"name": "submit_journal_entry", "arguments": {"name": "ACC-JV-2026-00190"}}
```

```json
{
  "name": "ACC-JV-2026-00190",
  "docstatus": 1,
  "docstatus_label": "submitted",
  "company": "Example Trading Co",
  "posting_date": "2026-06-30",
  "total_debit": 1450.0,
  "total_credit": 1450.0,
  "gl_entries_created": 2
}
```

Audit row: `docstatus_delta = "0 → 1 (submitted)"`.

---

## 31. `cancel_journal_entry`

`docstatus 1 → 2`, writing reversing GL Entries. Nothing is deleted.

**Arguments:** `name` (required), `reason` (required, at least four characters).

`reason` is written twice — to the document's comment thread, where an accountant
looking at the JE will see it, and to the audit log, where it survives even if the
document is later removed.

**Returns** `name`, `docstatus` (2), `docstatus_label`, `company`,
`posting_date`, `reason`, `note`.

**Refused:** a draft (delete it in ERPNext instead — it affects no balance), an
already-cancelled entry, a placeholder reason.

**Example**

```json
{"name": "cancel_journal_entry",
 "arguments": {"name": "ACC-JV-2026-00190",
               "reason": "Duplicate of ACC-JV-2026-00184; clearing swept twice"}}
```

```json
{
  "name": "ACC-JV-2026-00190",
  "docstatus": 2,
  "docstatus_label": "cancelled",
  "company": "Example Trading Co",
  "posting_date": "2026-06-30",
  "reason": "Duplicate of ACC-JV-2026-00184; clearing swept twice",
  "note": "ERPNext keeps cancelled entries and their reversing GL rows; nothing was deleted."
}
```

Annotated `destructiveHint: true` — the only tool in the catalogue that is.

---

## 32. `create_bank_transaction`

Inserts a **draft** Bank Transaction. `amount` is signed as a human reads a
statement: positive money in, negative money out, mapped onto whichever columns
this ERPNext version has.

Left as a draft deliberately: a submitted Bank Transaction is eligible for
reconciliation and starts matching against payments. Submitting is a human step in
ERPNext, and this app ships no tool for it.

**Arguments**

| Name | Required | Notes |
| --- | --- | --- |
| `bank_account` | yes | Docname or `account_name` |
| `date` | yes | |
| `amount` | yes | Non-zero. Positive in, negative out |
| `description` | yes | Statement narrative |
| `reference_no` | no | |
| `company` | no | Taken from the Bank Account when it has one |

`currency` is taken from the linked Account when the site tracks it.

**Returns** `name`, `docstatus` (0), `docstatus_label`, `bank_account`, `date`,
`amount_signed`, `amount_layout`, `description`, `reference_no`, `next_step`.

**Example**

```json
{"name": "create_bank_transaction",
 "arguments": {"bank_account": "Operating", "date": "2026-06-30",
               "amount": -42.50, "description": "MONTHLY SERVICE CHARGE",
               "reference_no": "FEE-202606"}}
```

```json
{
  "name": "BT-2026-00431",
  "docstatus": 0,
  "docstatus_label": "draft",
  "bank_account": "Operating - Example Bank",
  "date": "2026-06-30",
  "amount_signed": -42.5,
  "amount_layout": "deposit_withdrawal",
  "description": "MONTHLY SERVICE CHARGE",
  "reference_no": "FEE-202606",
  "next_step": "Draft Bank Transactions are not reconcilable. Submit it in ERPNext to include it in bank reconciliation."
}
```

---

## 33. `reconcile_bank_transaction`

Attaches payment vouchers to a Bank Transaction.

Hands the work to ERPNext's own `BankTransaction.add_payment_entries` where the
site's version has it, because that method is where clearance dates, allocation
arithmetic and the transaction's status live. Reimplementing it here would mean
guessing at the parts of reconciliation ERPNext does after the child row is
written — and getting one of them wrong leaves a transaction that looks reconciled
and is not. The append-and-save fallback is only for versions predating the
method, and the response says which path ran.

**Arguments**

| Name | Required | Notes |
| --- | --- | --- |
| `name` | yes | Bank Transaction docname |
| `payment_entries` | yes | Array, minimum 1 |

Each `payment_entries[]` entry needs `payment_document` (the voucher doctype, e.g.
`Payment Entry`, `Journal Entry`, `Sales Invoice`), `payment_entry` (its docname)
and `allocated_amount` (positive). Every voucher's existence is checked before
anything is written.

**Refused:** allocating more than the transaction's remaining amount, a voucher
that does not exist, a doctype that does not exist on this site, a non-positive
allocation, a cancelled Bank Transaction.

**Returns** `name`, `bank_account`, `gross_amount`, `allocated_now`,
`allocated_total`, `unallocated_amount`, `status`, `payment_entries[]` as stored,
and `applied_via`.

**Example**

```json
{"name": "reconcile_bank_transaction",
 "arguments": {"name": "BT-2026-00412",
               "payment_entries": [
                 {"payment_document": "Payment Entry", "payment_entry": "ACC-PAY-2026-00088",
                  "allocated_amount": 2400.00}]}}
```

```json
{
  "name": "BT-2026-00412",
  "bank_account": "Operating - Example Bank",
  "gross_amount": 2400.0,
  "allocated_now": 2400.0,
  "allocated_total": 2400.0,
  "unallocated_amount": 0.0,
  "status": "Reconciled",
  "payment_entries": [
    {"payment_document": "Payment Entry", "payment_entry": "ACC-PAY-2026-00088",
     "allocated_amount": 2400.0}
  ],
  "applied_via": "ERPNext add_payment_entries"
}
```

Over-allocation:

```
allocating 3000.0 would exceed Bank Transaction BT-2026-00412's remaining 2400.0
(gross 2400.0, already allocated 0.0). Nothing was changed.
```

---

## 34. `advance_workflow`

**MUTATING (default OFF).** Take a workflow action on a document.

Runs `frappe.model.workflow.apply_workflow`, the same code path the Desk button
uses — so the state change, any `update_field` the target state sets, the
docstatus change and the resulting submit or cancel all happen exactly as they
would for a human. **A transition into a state with `doc_status: 1` submits the
document.** There is no fallback: on a Frappe that does not export
`apply_workflow`, this refuses rather than hand-rolling a state write.

**Arguments:** `doctype` (required), `name` (required), `action` (required — the
transition's action label, exactly as `list_available_actions` reports it),
`dry_run` (optional, default false).

**Returns** `action`, `user`, `state_before`, `state_after`, `docstatus_before`,
`docstatus_after`, `docstatus_label`.

### `dry_run` — do this first

With `dry_run: true` nothing is executed. The tool resolves the transition, reads
the target state's `doc_status` and reports what *would* happen:

```json
{"name": "advance_workflow",
 "arguments": {"doctype": "Purchase Order", "name": "PUR-ORD-2026-00184",
               "action": "Approve", "dry_run": true}}
```

```json
{
  "dry_run": true,
  "executed": false,
  "current_state": "Pending Approval",
  "current_docstatus": 0,
  "would_succeed": true,
  "would_move_to": "Approved",
  "would_set_docstatus": 1,
  "would_submit": true,
  "would_cancel": false,
  "effects": [
    "workflow_state: 'Pending Approval' → 'Approved'",
    "SUBMITS the document (target state has doc_status 1) — for a Journal Entry this writes GL Entries and moves balances"
  ],
  "available_actions": ["Approve", "Reject"],
  "conditions_evaluated": true,
  "refusal_reason": null,
  "next_step": "Call again with dry_run=false to execute."
}
```

**A dry run never raises for an unavailable action.** "It would be refused, and
here is why" is the answer to the question, not a failure to answer it — so that
case comes back as `would_succeed: false` with a `refusal_reason`. A malformed
question (unknown document, no workflow, two active workflows) still errors,
because there is nothing to answer.

**What it cannot tell you.** A dry run resolves the *transition*. It does not run
the document's own validation, so it cannot predict a submit that fails on a
mandatory field, a closed period or a doctype hook. `would_succeed: true` means
"this action is available to you", not "the resulting save will succeed".

**Refused** — with the available actions listed — if the action is not open to
the acting user in the document's current state. The check runs through the same
resolution `list_available_actions` uses, so the self-approval rule and any
condition apply.

**Example**

```json
{"name": "advance_workflow",
 "arguments": {"doctype": "Purchase Order", "name": "PUR-ORD-2026-00184",
               "action": "Approve"}}
```

```json
{
  "doctype": "Purchase Order",
  "name": "PUR-ORD-2026-00184",
  "workflow": "Purchase Order Approval",
  "action": "Approve",
  "user": "mcp@example.com",
  "state_before": "Pending Approval",
  "state_after": "Approved",
  "docstatus_before": 0,
  "docstatus_after": 1,
  "docstatus_label": "submitted"
}
```

Audit row: `docstatus_delta = "0 → 1 (submitted)"`.

Refusal:

```
'Approve' is not available to mcp@example.com on Purchase Order
PUR-ORD-2026-00184 in state 'Pending Approval'. Available: Reject.
```

---

## 35. `create_todo`

**MUTATING (default OFF).** Assign a ToDo to a user, optionally against a
document.

The gentlest write in the catalogue — it touches no ledger and submits nothing —
but it does put an item in somebody's queue and notify them, which is why it is
still off by default.

**Arguments**

| Name | Required | Notes |
| --- | --- | --- |
| `subject` | yes | One-line summary |
| `owner` | yes | The User it is assigned **to**. Must exist and be enabled |
| `description` | no | Longer detail, appended below the subject |
| `priority` | no | `Low`, `Medium` (default), `High` |
| `reference_doctype` | no | Pass with `reference_name` or not at all |
| `reference_name` | no | |
| `date` | no | Due date, `YYYY-MM-DD` |

**Two naming traps, both handled.** `owner` is the argument name because that is
what a caller means by "assign it to them" — it is written to `allocated_to`,
the assignee field, not to Frappe's `owner`, which is the creator. And stock ToDo
has no `subject` field, so `subject` becomes the first line of `description`
(and is set on a real `subject` field on sites that added one). The response's
`assignee_field` and `subject_handling` say what actually happened, so the caller
is never left guessing where its text went.

**Returns** `name`, `assigned_to`, `assignee_field`, `assigned_by`, `subject`,
`status`, `priority`, `date`, `reference_type`, `reference_name`,
`subject_handling`.

**Example**

```json
{"name": "create_todo",
 "arguments": {"subject": "Chase Westbrook Cafe — 204 days overdue",
               "owner": "avi@example.com", "priority": "High",
               "reference_doctype": "Sales Invoice",
               "reference_name": "ACC-SINV-2026-00005",
               "date": "2026-08-01"}}
```

```json
{
  "name": "abc123def4",
  "assigned_to": "avi@example.com",
  "assignee_field": "allocated_to",
  "assigned_by": "mcp@example.com",
  "subject": "Chase Westbrook Cafe — 204 days overdue",
  "status": "Open",
  "priority": "High",
  "date": "2026-08-01",
  "reference_type": "Sales Invoice",
  "reference_name": "ACC-SINV-2026-00005",
  "subject_handling": "folded into description (ToDo has no subject field)"
}
```


---

# Compliance packet tools

A packet is an artefact rather than an answer: a structured JSON document for
somebody who has to sign something off. Both tools are read-only — nothing is
stored, emailed or filed.

Every packet carries the same envelope on top of its own body:

| Field | Meaning |
| --- | --- |
| `packet_type`, `title`, `purpose`, `audience` | What this is and who reads it |
| `filters` | The arguments it was built from, echoed back |
| `flags[]` | `{code, severity, description, detail}`, worst first |
| `flag_summary` | Counts per severity, `worst`, and `signable` (false if any ERROR) |
| `generated_at`, `generated_by`, `site`, `generator`, `generator_version` | Provenance |
| `mcp_action_log_id` | The MCP Action Log row for the call that produced this packet |
| `external_sources[]` | Empty in v0.3.0. Reserved for external reconciliation sources |

**Severity means something.** INFO is context. WARN is "a human should look".
ERROR is "these numbers do not internally agree" — an arithmetic or integrity
failure inside the packet, not merely an unusual business fact. A packet with an
ERROR flag has `signable: false` and should not be signed.

---

## 36. `list_compliance_packets`

**Arguments:** none.

**Returns** `packets[]` (each with `packet_type`, `title`, `purpose`,
`audience`, `filters` schema, `required_filters`, `switch`), plus `disabled[]`
(a switch an operator can tick) and `unavailable[]` (a site prerequisite that
cannot be ticked).

Call it before `generate_compliance_packet`: packet types are site-dependent,
and its `filters` schema is how a client learns to call a type this app's own
MCP schema knows nothing about.

---

## 37. `generate_compliance_packet`

**Arguments:** `packet_type` (required), `filters` (object, per the type).

Unknown filter keys are rejected **by name** rather than ignored. Silently
generating an unscoped packet when the caller thought they had scoped it is the
worst outcome available.

### `reconciliation_packet`

**Filters:** `account` (required), `period_start` (required), `period_end`
(required), `company`.

| Key | Contents |
| --- | --- |
| `account` | `{name, number, type, root_type, company, currency, account_name}` |
| `period` | `{start, end}` |
| `opening_balance` | `{amount, as_of, source, gl_entry_count}` at `period_start - 1` |
| `closing_balance` | Same shape, at `period_end` |
| `movement_summary` | `total_debits`, `total_credits`, `net_change`, `count_transactions` |
| `journal_entries[]` | Submitted JEs touching the account, each with `this_account_debit` / `_credit` / `_net` |
| `unposted_drafts[]` | Same shape, `docstatus 0` — the movement that has not happened yet |
| `cancelled_entries[]` | Same shape, `docstatus 2` — invisible to a balance query, which is why they are here |
| `arithmetic_check` | `opening + net` vs `closing`, and whether it reconciles |

Balances are summed from GL Entry excluding cancelled rows, matching ERPNext's
own General Ledger report. The Journal Entry lists come from the
`Journal Entry Account` child table instead, because that is the only source
that can see drafts and cancellations.

**Flags it raises:** `BALANCE_DOES_NOT_RECONCILE` (ERROR),
`UNBALANCED_JOURNAL_ENTRY` (ERROR), `CANCELLED_ENTRIES` (WARN),
`UNPOSTED_DRAFTS` (WARN), `NEGATIVE_BALANCE` (WARN), `NO_ACTIVITY` (INFO),
`QUIET_PERIOD` (INFO), `FUTURE_DATED` (INFO), `LARGE_ENTRY` (INFO),
`TRUNCATED` (WARN).

**Example**

```json
{"name": "generate_compliance_packet",
 "arguments": {"packet_type": "reconciliation_packet",
               "filters": {"account": "1100", "period_start": "2026-01-01",
                           "period_end": "2026-06-30"}}}
```

```json
{
  "packet_type": "reconciliation_packet",
  "account": {"name": "1100 - Cash - ETC", "number": "1100", "type": "Cash",
              "root_type": "Asset", "company": "Example Trading Co", "currency": "USD"},
  "period": {"start": "2026-01-01", "end": "2026-06-30"},
  "opening_balance": {"amount": 0.0, "as_of": "2025-12-31", "source": "GL Entry", "gl_entry_count": 0},
  "closing_balance": {"amount": 750.0, "as_of": "2026-06-30", "source": "GL Entry", "gl_entry_count": 2},
  "movement_summary": {"total_debits": 1000.0, "total_credits": 250.0,
                       "net_change": 750.0, "count_transactions": 2},
  "journal_entries": [
    {"name": "ACC-JV-2026-00001", "posting_date": "2026-01-15", "docstatus": 1,
     "user_remark": "Opening sale", "total_debit": 1000, "total_credit": 1000,
     "this_account_debit": 1000.0, "this_account_credit": 0.0, "this_account_net": 1000.0}
  ],
  "unposted_drafts": [
    {"name": "ACC-JV-2026-00002", "posting_date": "2026-02-10", "docstatus": 0,
     "user_remark": "Stationery", "this_account_net": -250.0}
  ],
  "cancelled_entries": [
    {"name": "ACC-JV-2025-00009", "posting_date": "2026-03-05", "docstatus": 2,
     "this_account_net": -40.0}
  ],
  "arithmetic_check": {"opening_plus_net": 750.0, "closing": 750.0,
                       "difference": 0.0, "reconciles": true},
  "external_sources": [],
  "flags": [
    {"code": "CANCELLED_ENTRIES", "severity": "WARN",
     "description": "1 Journal Entry/Entries touching this account were cancelled in the period, 40.0 in gross movement. Cancelled entries leave no live GL rows, so they do not appear in the balance — somebody posted these and then unposted them.",
     "detail": {"count": 1, "gross_amount": 40.0, "entries": ["ACC-JV-2025-00009"]}},
    {"code": "UNPOSTED_DRAFTS", "severity": "WARN",
     "description": "1 draft Journal Entry/Entries dated in the period would move this account by -250.0 if submitted. The closing balance in this packet does not include them.",
     "detail": {"count": 1, "net_if_submitted": -250.0, "entries": ["ACC-JV-2026-00002"]}},
    {"code": "LARGE_ENTRY", "severity": "INFO",
     "description": "1 entry/entries account for at least 25% of the period's gross movement each. Materiality is a judgement — this points, it does not conclude.",
     "detail": {"threshold": 312.5, "entries": [{"name": "ACC-JV-2026-00001", "amount": 1000.0, "share_of_period": 0.8}]}}
  ],
  "flag_summary": {"ERROR": 0, "WARN": 2, "INFO": 1, "worst": "WARN", "signable": true},
  "generated_at": "2026-07-26 09:14:22.117045",
  "generated_by": "mcp@example.com",
  "site": "erp.example.com",
  "generator": "erpnext_mcp",
  "generator_version": "0.3.0",
  "mcp_action_log_id": "b7c41f9e2a"
}
```

### `fiscal_year_audit_packet`

**Filters:** `company` (required), `fiscal_year` (required).

| Key | Contents |
| --- | --- |
| `date_range` | `{start, end, disabled}` from the Fiscal Year |
| `trial_balance` | `by_root_type` (grouped rows), `totals_by_root_type`, `row_count` |
| `trial_balance_totals` | One cumulative aggregate over every account — where debits must equal credits |
| `income_statement` | `revenue`, `expenses`, `net_income`, movement within the year |
| `balance_sheet` | `assets`, `liabilities`, `equity`, `liabilities_plus_equity`, cumulative |
| `accounting_identity` | `Assets - (Liabilities + Equity)` vs `net_income`, and whether it holds |
| `top_20_entries_by_amount` | Submitted JEs ranked by absolute amount, for materiality |
| `intercompany_activity[]` | JEs whose account lines span more than one company |
| `document_counts` | Sales/purchase invoices, JEs by docstatus, bank transactions. `null` = doctype not installed |

**Two bases, stated per row.** Balance-sheet accounts carry forward, so their
`basis` is `cumulative`. Profit-and-loss accounts reset each year, so theirs is
`fiscal_year`. Mixing the two silently is how a trial balance stops balancing;
`trial_balance_totals` is computed separately on a single cumulative basis, which
is where debits and credits must agree.

**Flags it raises:** `TRIAL_BALANCE_IMBALANCE` (ERROR),
`ACCOUNTING_IDENTITY_FAILS` (ERROR), `INTERCOMPANY_ACTIVITY` (WARN),
`CANCELLED_ENTRIES` (WARN), `DRAFT_ENTRIES_AT_YEAR_END` (WARN),
`UNNATURAL_BALANCE` (WARN), `FISCAL_YEAR_NOT_LINKED` (WARN),
`FISCAL_YEAR_DISABLED` (INFO), `NO_ACTIVITY` (INFO).

Note on `ACCOUNTING_IDENTITY_FAILS`: for a year already closed to retained
earnings, the two sides legitimately differ. The flag says so.

---

# Chart of accounts

Five write tools and one planner, over the chart itself rather than postings
into it. They are grouped separately here for the same reason they have their
own settings section: a bad journal entry is one wrong number that a reversing
entry fixes, while a bad reparent changes what every balance-sheet subtotal has
meant, retroactively, for every period already reported.

## The docname, which explains most of the design

An Account's primary key is `"<number> - <name> - <abbr>"`, built by ERPNext's
`autoname` at insert and **never rebuilt afterwards**. Two consequences run
through everything below:

- Renaming an account means changing two fields *and* moving the document. Doing
  either half alone leaves an account that is called one thing and reports
  another, permanently. `update_account` delegates both halves to ERPNext's own
  `update_account_number`.
- A dry run has to *predict* the docname, since nothing has been inserted yet.
  `charts.account_docname` reproduces the rule; an in-bench test asserts the
  prediction matches what a real insert produces.

And one rule that is ERPNext's, not this app's: **a root account cannot be
saved at all.** `Account.validate_root_details` throws "Root cannot be edited"
on any save of an account with no parent. So roots cannot be re-typed, disabled,
moved, or created by `create_account` — only renamed, which goes down a
different path, and created by `import_chart_of_accounts` as part of a tree.

---

## 38. `create_account`

**MUTATING. Default OFF.**

**Arguments:** `company`, `account_number`, `account_name`, `root_type`,
`parent_account` (all required), `is_group`, `account_type`, `account_currency`,
`tax_rate`.

Checks, all before anything is written:

| Check | Refusal |
| --- | --- |
| Parent exists, in this company, and is a group | `is a ledger account, not a group` |
| `root_type` matches the parent's | `does not match parent_account …, whose root_type is …` |
| `account_number` free in this company | `already used by '1100 - Cash - ETC'` |
| The computed docname is free | `an Account named … already exists` |
| `account_type` is one this site offers | lists the site's own options |
| `account_type` can sit under this `root_type` | `belongs under root_type Liability, not 'Asset'` |
| `account_currency` exists | `no Currency named 'ZZZ'` |

`root_type` is required even though it is derivable from the parent. That is
deliberate: it makes the caller state its intent, so this tool can check it
rather than infer it.

```json
{"name": "tools/call", "arguments": {
  "company": "Example Trading Co", "account_number": "1150",
  "account_name": "Money Market", "root_type": "Asset",
  "parent_account": "1100", "account_type": "Bank"}}
```

```json
{
  "name": "1150 - Money Market - ETC",
  "account_number": "1150",
  "parent_account": "1100 - Current Assets - ETC",
  "root_type": "Asset",
  "report_type": "Balance Sheet",
  "account_type": "Bank",
  "is_group": false,
  "disabled": false,
  "company": "Example Trading Co",
  "next_step": "Ledger account, ready to post to."
}
```

---

## 39. `update_account`

**MUTATING. Default OFF.**

**Arguments:** `name` (required), `company`, `new_account_name`,
`new_account_number`, `new_account_type`, `disabled`. At least one `new_*` or
`disabled` is required.

Returns the account's new shape plus `previous_name`, `renamed`, `changes` (a
map of `field → [before, after]`) and `rename_method` — which will read
`erpnext update_account_number` on any current ERPNext, and names the legacy
fallback otherwise.

**It cannot reparent.** `new_parent_account` is not in the schema. Renaming is
routine and reversible; moving is neither, and keeping them apart means an
operator can enable one without the other.

Two refusals worth knowing about:

- **Root accounts** cannot have their type or disabled flag changed, because
  ERPNext will not save a root. Renaming and renumbering still work.
- **Crossing the Receivable/Payable boundary** on an account that already has GL
  entries is refused. ERPNext keys party balances off that flag; flipping it on
  an account with history leaves the ledger silently unreconciled.

---

## 40. `move_account`

**MUTATING. Default OFF. Destructive hint.**

**Arguments:** `name`, `new_parent_account` (both required), `company`.

Validates that the new parent is a group, in the same company, with the same
`root_type`, and that the move would not create a cycle — the cycle check walks
the `parent_account` chain rather than comparing `lft`/`rgt`, because a stale
nested set would turn a wrong answer into an infinite loop inside a tree rebuild.

The response carries `gl_entries_on_this_account` and this note, which is the
whole reason the tool is separate:

> Reparenting does not move a single GL Entry — every posting stays exactly
> where it was. What changes is which subtotal on the balance sheet and P&L those
> postings roll up into, for every period, including ones already reported.

---

## 41. `disable_account`

**MUTATING. Default OFF. Destructive hint.**

**Arguments:** `name`, `reason` (both required), `company`.

ERPNext's soft delete. Nothing is removed — the account, its history and its GL
entries all remain, and `update_account(disabled=false)` puts it back.

**Refuses any account with GL entries in the current fiscal year.** That is the
line between tidying the chart and breaking this year's reports: a disabled
account drops out of pickers and out of some period comparisons, and doing that
to an account the year is still posting through produces figures nobody can
reconcile. The current fiscal year is resolved the way ERPNext resolves it —
company-restricted years beat global ones. On a site where no fiscal year covers
today, the window falls back to the trailing 365 days, which is wider and
therefore errs towards refusing; the response says which window was used.

Also refuses a root account, and reports `child_accounts` when disabling a
group, because its children are **not** disabled by this call and stay postable.

`reason` goes onto the account's comment thread and into the audit log.

---

## 42. `import_chart_of_accounts`

**MUTATING. Default OFF.**

**Arguments:** `company`, `accounts_json` (both required), `dry_run`.

**`dry_run` defaults to `true`,** and that default is load-bearing: an
accidental call — a model retrying, a client replaying a message — must not be
able to rearrange a live chart of accounts, and the only way to guarantee that
is for the dangerous behaviour to be the one you ask for. An unparseable
`dry_run` is an error, not a vote for false.

`accounts_json` accepts a list of root accounts, a JSON string of the same, or
the whole `propose_clean_chart` response (its `accounts` key is used). Per node:
`account_number`, `account_name`, `root_type` (required on roots, inherited
below), `account_type`, `account_currency`, `tax_rate`, `is_group`,
`description`, `children`, and on a root node `parent_account` to graft the
subtree onto an existing group instead of adding a new root. Unknown keys are
rejected by name.

**The plan.** Every call returns `accounts[]` in dependency order — parents
before children — each row carrying `action`, the predicted `docname`, its
`parent_account`, `root_type`, `account_type` and `depth`:

| `action` | Meaning |
| --- | --- |
| `create` | Would be created. |
| `created` | Was created (real runs only). |
| `skip` | Already present with the same number *and* the same name. Left exactly as it is — this is what makes re-running an import safe. |
| `error` | Something has to be fixed first. `note` says what. |

Matching an existing account on the number alone would be how a reviewed chart
silently comes to mean something else, so anything beyond a name match — a
group/ledger mismatch, a different root type, a different parent — is an `error`
rather than a `skip`. An import will never reparent or rename an account that
already exists.

**`blocking_problems`** (dry runs only) is the list worth reading. One bad group
takes its whole subtree with it, so five real problems can produce seventy error
rows; `blocking_problems` holds only the causes, and every other error row is a
child of something that cannot be created.

**On a company built from a bundled ERPNext chart, expect collisions.** ERPNext's
"Standard with Numbers" numbers its own roots 1000/2000/3000/4000/5000 and its
groups 1100, 1200, 1700 and so on — the same convention this template uses,
because it is the convention. A company created that way will report thirty-odd
numbers already in use, and the import refuses until they are freed. Renumbering
the bundled accounts out of the way first with `update_account` (prefixing each
with a 9, say) is the straightforward fix, and `propose_clean_chart` lists
exactly which ones. Disabling one does not free its number, and this app has no
delete tool.

**Atomicity.** A real run is one transaction. The first failure rolls the whole
import back; there is no half-built tree to unpick, which matters more here than
anywhere else in this app because a partial tree has orphaned groups in it.

Capped at 400 accounts per call — ERPNext rebuilds the account nested set on
every insert, and refusing with a number beats timing out half way.

---

## 43. `propose_clean_chart`

**Read-only.** On by default.

**Arguments:** `company` (required), `template` (default `us_llc_farm`).

Returns a complete chart in exactly the shape tool 42 takes, plus what a
reviewer needs to judge it:

| Key | What it is |
| --- | --- |
| `accounts` | The tree, ready to pass to `import_chart_of_accounts`. |
| `optional_accounts` | Accounts a small operation will not need, so they can be struck first. |
| `account_type_adjustments` | Every `account_type` swapped for one this ERPNext actually offers, with the reason. |
| `existing_root_accounts` | What the company already has at the top of its chart. |
| `account_numbers_already_in_use` | Template numbers that would collide. |
| `notes` | Caveats from the template author. |
| `warning` | Set when the company already has roots — see below. |

**Importing adds roots, it does not replace them.** ERPNext will not let a root
be edited or moved once created, so a company that already has a chart ends up
with two sets of roots and the old ones have to be retired with
`disable_account`. The warning says so, and `existing_root_accounts` is how you
see what you are in for before running anything.

**Templates are static data.** They live in `erpnext_mcp/charts/` as plain
Python literals and never touch the database, which is what makes a proposal
reviewable, diffable and version-controllable before it runs. The package
auto-discovers them, so a new one is a single file drop.

### `us_llc_farm`

81 accounts — 17 groups, 64 ledgers — for a US farming LLC that also runs an
investment book. Numbering is the convention a US bookkeeper expects: 1000s
assets, 2000s liabilities, 3000s equity, 4000s income, 5000s COGS, 6000s
operating expenses, 7000s non-operating. Gaps are deliberate.

**Compact on purpose.** Nine flat operating-expense buckets, no sub-groups, and
at most two levels of grouping anywhere. A chart with a line for every
conceivable cost is one where nobody finds the right line; the intent is that a
sub-account gets added when a real transaction needs it, not in advance.

Four things it does that a generic chart does not:

- **Crop labour is separated from administrative wages** — `5150 Direct Farm
  Labor` under COGS, `6100 Payroll & Benefits` under operating expenses. A cost
  per bin means nothing if the two are mixed. `6150 Employer Payroll Tax
  Expense` splits out again, so wage cost and true cost of employment read
  apart, and neither is confused with `2140 Payroll Tax Withholdings` — money
  withheld from employees, which is a liability and never an expense.
- **The trading segment is a range set, not a dashboard.** The investment book
  has its own accounts on every side of the ledger:

  | Side | Range | |
  | --- | --- | --- |
  | Asset | `1800-1849` | stocks & ETFs, mutual funds, bonds, brokerage cash, open options |
  | Income | `4200-4249` | interest, dividends, realised gains, options premium |
  | Expense | `7300-7339` | realised capital losses, options losses, advisory fees, custodian & brokerage fees |
  | Equity | `3500` | unrealised gain/loss — mark-to-market, outside the P&L until a position closes |

  Filter a P&L or trial balance to those four and you have the trading business,
  running costs included; exclude them and you have the farm. Nothing else in
  the chart reaches into those numbers, which is what keeps the split true as
  the chart grows — a standalone test walks the whole tree and fails if an
  account outside the ranges starts reading as trading.

  Three splits inside the segment are there because a combined account just
  moves work downstream. `1810` (exchange-traded) and `1815` (mutual funds) are
  apart because a fund prices once daily at NAV and an ETF prices continuously.
  `7300` (equity, fund and bond losses) and `7310` (options losses) are apart
  because loss harvesting separates short-term from long-term anyway, and
  options treatment can differ again — Section 1256 for index options, ordinary
  for most others. `7320` (advisory, time- and asset-based) and `7330`
  (custodian and per-transaction) are apart because read together they tell you
  neither.

  **`1830 Brokerage Cash & Money Market` ships as an empty group.** It takes one
  child per linked brokerage cash-services account, added after the import with
  `create_account` (`account_type=Bank`) once you know which accounts exist.
  Per-account visibility is what makes anchor reconciliation, sweep tracing and
  per-account fee attribution possible — collapsed into one combined ledger, a
  paired-brokerage feed cannot say which account a movement belongs to. The
  template ships no default children because the account numbers are a property
  of the install, not of the chart. An empty group is legal in ERPNext and posts
  to nothing, which is the correct state until the first brokerage account is
  connected.

  **One exception, and it matters for Bank Bridge.**
  `1130 Cash Clearing - Brokerage` carries the word "brokerage" but is NOT part
  of the segment. It is the bridge for paired brokerage/companion transactions —
  the pattern where one movement appears on both a brokerage account and its
  linked cash-services account — and a reconciled pair passes through it on the
  way to its real accounts. The balance is transient and should read zero; a
  standing balance means a pair did not close. Leave it out of segment
  reporting, and do not remove it if a bank feed posts paired investment
  transactions, because that posting has nowhere else to land.
- **`2120 Current Pay Period - Due to Employees`** is a live balance, not an
  accrual. It is meant to be updated continuously as work lands — bucket picks
  as they are recorded, hours as they accumulate — so it reads at any moment as
  real-time wage exposure, and flushes to zero when payroll is processed. Its
  description says exactly that, because a month-end adjusting entry dropped in
  on top double-counts against the continuous postings and destroys the one
  property the account exists for. `2130 Employee Wage Advances` is its
  counterpart for wages paid before payday, and is distinct again from
  `1510 Employee Cash Advances`, which is money the business expects back.
- **Property tax is tracked in all three places it lives** — accrued in
  `2170 Property Tax Payable` (the county bills once or twice a year but the
  obligation accrues monthly), prepaid in `1420 Prepaid Property Tax`, and
  expensed through `6650 Property & Business Taxes` alongside vehicle
  registration, LLC filing fees and business licences.

Four accounts carry no `account_type` on purpose — `1810`, `1815`, `1820` and
`1840`.
ERPNext offers nothing that fits a securities or open-options position; the
nearest, `Stock`, means trading inventory and would pull them into the Stock
module's valuation. An account with no type still posts.

The 5000-series accounts are typed `Expense Account` rather than ERPNext's
`Cost of Goods Sold`. If the Stock module is used against `1300`, the Item or
Item Group default expense account has to be set by hand, because ERPNext looks
for the `Cost of Goods Sold` type when it picks one automatically.

Equity is the part that is entity-specific, which is why the entity type is in
the template key rather than a flag: `us_c_corp`, `us_s_corp` and
`us_partnership` will differ from this almost entirely in the 3000s, and
pretending one chart covers all four would put the wrong equity structure on
somebody's return. It is a starting point, not tax advice.

---

# Cost centers and accounting dimensions

Six tools for the *other* axes a posting is filed under. The chart of accounts
says what kind of money a transaction is; these say which part of the business it
belongs to, and whatever else the operator needs to slice by.

**The thing to understand before tool 47.** An ERPNext Accounting Dimension does
not hold its own values. It **points at a DocType**, and every record of that
DocType is a value. So "a Member dimension with three members" is three ideas: a
DocType to hold members, the dimension record plus the Link field it puts on each
accounting document, and one record per member.

```
create_accounting_dimension(dimension_name="Member", create_master_if_missing=true)
  ↓                                   the DocType, the dimension, the fields
create_dimension_value(dimension_name="Member", value_name="Member-01")
  ↓                                   one value
create_journal_entry(accounts=[{..., "dimensions": {"member": "Member-01"}}])
```

**"Journal Entry" means the line.** ERPNext carries dimensions on `Journal Entry
Account`, never on the Journal Entry header, because one entry books to several.
Ask for `"Journal Entry"` and tool 47 wires the child table and reports the
redirection in `redirected`.

---

## 44. `create_cost_center`

**MUTATING.** Off by default. Requires the Cost Center doctype.

**Arguments:** `cost_center_name` (required), `cost_center_number`,
`parent_cost_center`, `company`, `is_group` (default false).

Docnames follow the same rule accounts do — `"<number> - <name> - <abbr>"`, or
`"<name> - <abbr>"` when unnumbered. Unlike an account number, the cost center
number really is optional.

**Refused before anything is written:** a parent that does not exist, is a leaf
rather than a group, or belongs to another company; a number already used in that
company; a docname already taken.

**Roots.** ERPNext gives every company exactly one root cost center, named
exactly after the company (`CostCenter.validate_mandatory`). Omitting
`parent_cost_center` on a company that already has one is refused and the message
names the existing root. On a company with none — a half-built site — a root can
be created, and `cost_center_name` has to equal the company.

**Example**

```json
{"name": "create_cost_center",
 "arguments": {"company": "Example Trading Co", "cost_center_number": "3200",
               "cost_center_name": "Harvest",
               "parent_cost_center": "3000 - Farm Value Chain - ETC"}}
```

```json
{
  "name": "3200 - Harvest - ETC",
  "cost_center_number": "3200",
  "cost_center_name": "Harvest",
  "parent_cost_center": "3000 - Farm Value Chain - ETC",
  "is_group": false,
  "disabled": false,
  "company": "Example Trading Co",
  "next_step": "Leaf cost center, ready to be filed against on a posting."
}
```

---

## 45. `update_cost_center`

**MUTATING.** Off by default. Requires the Cost Center doctype.

**Arguments:** `name` (required — docname, number or name), `company`,
`new_cost_center_name`, `new_cost_center_number`, `disabled`.

Renaming writes the fields and *then* moves the docname, in that order, for the
reason set out under [The docname](#the-docname-which-explains-most-of-the-design):
a Cost Center's key encodes two of its own fields and is built once, so changing
one without the other leaves the tree showing one thing and reporting another.

Unlike `update_account`, this is hand-rolled rather than delegated to ERPNext.
ERPNext's own helper handles only the *number*, and the compensating behaviour
that makes delegation matter for Account — syncing a rename down into child
companies — has no cost-center equivalent. The docname rule itself is identical,
and an in-bench test asserts a real insert produces what this app predicts.

**Refused:** a value identical to the current one (nothing to change); a number
already in use; renaming the company's root, which ERPNext requires to be named
after the company; and any attempt to reparent — this app ships no
`move_cost_center`, because reparenting moves no posting but changes which
subtotal every existing one rolls up into, for periods already reported.

**Disabling deletes nothing.** The cost center, its history and its GL entries
all remain and still appear in reports covering them; it drops out of pickers.
The response carries `gl_entries_on_this_cost_center` and a `warning` that says
so — and, for a group, that its children were **not** disabled.

---

## 46. `list_cost_centers`

**Read-only.** On by default. Requires the Cost Center doctype.

**Arguments:** `company` (required), `include_disabled` (default false).

The same nested shape `get_chart_of_accounts` returns: `children[]` is the tree,
`flat_count` is every node in the response. Disabled cost centers are left out
and counted in `disabled_count_excluded`, so "the tree looks short" always has an
answer. `default_cost_center` is the company's, under whichever fieldname this
ERPNext uses.

```json
{
  "company": "Example Trading Co",
  "cost_centers": [
    {"name": "Example Trading Co - ETC", "cost_center_name": "Example Trading Co",
     "is_group": 1, "children": [
       {"name": "Main - ETC", "cost_center_number": null, "is_group": 0, "children": []},
       {"name": "3000 - Farm Value Chain - ETC", "is_group": 1, "children": [
         {"name": "3200 - Harvest - ETC", "is_group": 0, "children": []}]}]}],
  "flat_count": 4,
  "disabled_count_excluded": 1,
  "include_disabled": false,
  "default_cost_center": "Main - ETC"
}
```

---

## 47. `create_accounting_dimension`

**MUTATING, and a schema change.** Off by default. Requires the Accounting
Dimension doctype (ERPNext v12+).

**Arguments**

| Name | Required | Notes |
| --- | --- | --- |
| `dimension_name` | yes | The label. Its scrubbed form is the fieldname: `Member` → `member`, `BBCH Stage` → `bbch_stage` |
| `master_doctype` | no | The DocType whose records are the values. Defaults to `dimension_name` |
| `create_master_if_missing` | no | **Default false.** True generates a simple custom DocType |
| `document_types` | no | Default `["Journal Entry", "Sales Invoice", "Purchase Invoice", "Payment Entry"]` |
| `disabled` | no | Create it disabled — field added, dimension ignored |

**What it writes**, in one transaction, so a failure leaves none of it:

1. the master DocType, only when generated — `custom: 1`, so it lives entirely in
   the database, writes no files into an app and needs no developer mode. Named
   `field:dimension_value`, so the record's own name **is** the value and
   `Member-01` reads as `Member-01` everywhere it is linked. Three fields: the
   value, a description, a disabled flag.
2. the Accounting Dimension record;
3. one Link Custom Field per target doctype.

**Why the fields are written here rather than left to ERPNext.** Inserting an
Accounting Dimension makes ERPNext enqueue its own field-creation routine as a
*background job* over its own fixed list. Both halves are wrong for an MCP
caller: the next call is usually a Journal Entry that needs the field to exist
now, and the caller asked for a specific set of doctypes. ERPNext's job still
runs and still creates the rest of its list; both paths check for an existing
field first, so they do not collide.

**Refused before anything is written:** a dimension that already exists for that
label or that DocType (ERPNext allows one per DocType — its values *are* that
DocType's records, so a second would be the same dimension twice); a master that
is a Single, a child table or a core doctype; a target doctype this site does not
have; and any target that already has a field of that name which is not a Link to
this master.

**Not reversible through this app.** Removing a dimension means deleting the
record and its custom fields in the Desk.

**Example**

```json
{"name": "create_accounting_dimension",
 "arguments": {"dimension_name": "Member", "create_master_if_missing": true,
               "document_types": ["Journal Entry"]}}
```

```json
{
  "name": "Member",
  "label": "Member",
  "fieldname": "member",
  "master_doctype": "Member",
  "master_doctype_created": true,
  "disabled": false,
  "document_types_requested": ["Journal Entry"],
  "document_types_applied": ["Journal Entry Account"],
  "custom_fields_created": ["Journal Entry Account"],
  "custom_fields_already_present": [],
  "redirected": {"Journal Entry": ["Journal Entry Account"]},
  "next_step": "Add values with create_dimension_value(dimension_name='Member', value_name=…) — each one is a Member record. Then set 'member' in a journal entry line's `dimensions` object."
}
```

---

## 48. `create_dimension_value`

**MUTATING.** Off by default. Requires the Accounting Dimension doctype.

**Arguments:** `dimension_name` (required — the label, the DocType, or the
dimension's docname), `value_name` (required), `extra_fields`.

Creates one record in the DocType the dimension points at. Where that DocType
names itself from a field — which is how the masters tool 47 generates work, and
how ERPNext's own dimension masters work — `value_name` becomes both the field
and the docname. Where it names itself some other way, the value is created
anyway and the response reports the name it actually got, with a note.

Three ways to name the dimension because the Accounting Dimension record's own
docname is a version detail, and a caller who created it through this app knows
it by the label it asked for.

**Refused:** an unknown dimension (the message lists the ones this site has); a
dimension whose DocType is missing; a record of that name already present; an
`extra_fields` key the master does not have — a typo, not something to ignore.

```json
{"name": "create_dimension_value",
 "arguments": {"dimension_name": "Member", "value_name": "Member-01",
               "extra_fields": {"description": "Active since 2026-01-01"}}}
```

```json
{
  "name": "Member-01",
  "requested_name": "Member-01",
  "dimension": "Member",
  "dimension_record": "Member",
  "master_doctype": "Member",
  "named_by": "field:dimension_value",
  "extra_fields": {"description": "Active since 2026-01-01"},
  "note": "Ready to use: set it on a journal entry line's `dimensions` object."
}
```

---

## 49. `set_company_defaults`

**MUTATING.** Off by default.

**Arguments:** `company` (required), `defaults` (required) — an object of company
field → account. Accounts resolve three ways, as everywhere else. An empty string
clears a field.

**Supported keys**

| Key | Has to be | Because |
| --- | --- | --- |
| `default_receivable_account` | Receivable, Asset | ERPNext keys customer balances off `account_type` |
| `default_payable_account` | Payable, Liability | Same, for suppliers |
| `default_cash_account` | Cash, Asset | |
| `default_bank_account` | Bank, Asset | |
| `default_income_account` | Income | Sales lines with no account of their own |
| `default_expense_account` | Expense | Purchase lines with no account of their own |
| `cost_of_goods_sold_account` | Expense | Stock consumed against a sale |
| `round_off_account` | Expense or Income | The cent a rounded total leaves behind |
| `exchange_gain_loss_account` | Expense or Income | |
| `write_off_account` | Expense or Income | |
| `default_deferred_revenue_account` | Liability or Income | |
| `default_deferred_expense_account` | Asset or Expense | |
| `round_off_cost_center` | a **leaf Cost Center** | Where the rounding difference is filed |
| `disposal_account` | Income or Expense | ERPNext refuses to scrap or sell an Asset without it — and says so *from the Asset* |
| `capital_work_in_progress_account` | Capital Work in Progress, Asset | An asset being built, before it is in service |
| `expenses_included_in_asset_valuation` | Expense | Freight and duty that belong in an asset's cost, not the period |
| `asset_received_but_not_billed` | Asset Received But Not Billed, Liability | An asset delivered before its invoice |
| `stock_adjustment_account` | Expense | The difference a stock count found |
| `stock_received_but_not_billed` | Stock Received But Not Billed, Liability | Stock delivered before its invoice |
| `unrealized_exchange_gain_loss_account` | Income or Expense | Movement on an unsettled foreign-currency balance |
| `unrealized_profit_loss_account` | Income or Expense | Intra-group profit eliminated on consolidation |
| `default_advance_received_account` | Receivable, **Liability** | Money held for a customer is a liability, keyed so the party ledger picks it up |
| `default_advance_paid_account` | Payable, **Asset** | The mirror image, for money paid out early |
| `default_operating_cost_account` | Expense | What a production order adds to what it makes |
| `default_selling_cost_center` | a **leaf Cost Center** | Where a sale is filed when the document does not say |
| `default_buying_cost_center` | a **leaf Cost Center** | Same, for a purchase |

**Type-checked, not merely existence-checked.** ERPNext would accept a
`default_receivable_account` pointed at a plain Asset account and then produce
invoices that post but never age — and the symptom appears a quarter later with
nothing to point at. The check is cheap; the failure it prevents is not.

**Also refused:** a group account, a disabled account, an account belonging to
another company, a group cost center, an unsupported key (by name), and a key
this ERPNext version's Company does not have.

**Nothing is written unless every value validates**, so a partially-correct call
leaves the company exactly as it was.

**Idempotent.** Every field is compared before it is written; the response
separates `changed` from `unchanged`. That matters more than usual here, because
`Company.save` is not a cheap write — ERPNext's `on_update` walks the company
tree — and these are the fields a caller is most likely to set twice while
working out a chart.

```json
{"name": "set_company_defaults",
 "arguments": {"company": "Example Trading Co",
               "defaults": {"default_receivable_account": "1200",
                            "default_payable_account": "2100",
                            "round_off_cost_center": "Main"}}}
```

```json
{
  "company": "Example Trading Co",
  "changed": {
    "default_receivable_account": ["", "1200 - Accounts Receivable - ETC"],
    "round_off_cost_center": ["", "Main - ETC"]
  },
  "unchanged": ["default_payable_account"],
  "defaults_now": {
    "default_receivable_account": "1200 - Accounts Receivable - ETC",
    "default_payable_account": "2100 - Accounts Payable - ETC",
    "round_off_cost_center": "Main - ETC"
  },
  "idempotent": true,
  "note": "Company defaults decide which account a document reaches for when nothing on the document says. They do not touch a single existing posting — every document already written keeps the accounts it was written with."
}
```

A mismatch reads like this, and writes nothing:

```
default_receivable_account has to point at an account whose account_type is Receivable; '1100 - Cash - ETC' is Cash. ERPNext keys customer balances off that flag, not off the account's name or number, so this would post and then fail to reconcile. Fix it with update_account(new_account_type=…) first. Nothing was changed.
```

---

# Cap table, member events and governance

Ten tools for the things a family business holds for a generation: who owns it,
what happened to their interest, and which paper says so.

**The idea everything here rests on.** A chart of accounts and a cost center tree
are read by everyone who touches the books — a bookkeeper, a lender, an auditor,
a model summarising the year. A family name in either one leaks into every export
and cannot be taken out of a statement that has already been sent.

So a posting is tagged with an anonymous **Member accounting dimension** value,
and exactly one doctype says who that is:

```
Journal Entry line   →  3100 Member Capital, member = Member-01
Cap Table Entry      →  Member-01 = The Example Family Trust, admitted 2020-06-15, 60%
```

Read access to the ledger and read access to the mapping are then two different
grants. `list_cap_table` is the tool that joins them, which is why it has its own
switch.

**Members are a dimension, not a cost center.** Cost centers answer "which part
of the operation did this belong to", and a member is not a part of the
operation. `Cap Table Entry` keeps an optional `member_cost_center` for sites
whose convention already gives each member one, but every tool here files by the
dimension and carries the cost center along.

**Order of operations on a fresh site:**

```
create_accounting_dimension(dimension_name="Member", create_master_if_missing=true)
create_dimension_value(dimension_name="Member", value_name="Member-01")
create_cap_table_entry(member_id="Member-01", legal_entity_name="…", …)
record_member_event(event_type="Contribution", member="Member-01", …)
submit_member_event(name=…)          ← needs allow_submit_journal_entry too
```

`create_cap_table_entry` refuses a member id that is not already a dimension
value, so the first two steps cannot be skipped by accident. A site with no
Member dimension at all is allowed to build the register first, and is told so.

---

## 50. `list_cap_table`

Read-only, on by default. Requires the Cap Table Entry doctype (ships with this
app; run `bench migrate`).

**Arguments:** `company` (required), `include_retired` (default **true**).

Retired members are included by default because the postings they are tagged on
do not disappear when they leave.

```json
{
  "company": "Example Trading Co",
  "members": [
    {
      "name": "Member-01 - ETC",
      "member_id": "Member-01",
      "legal_entity_name": "The Example Family Trust",
      "entity_type": "Trust",
      "admission_date": "2020-06-15",
      "withdrawal_date": null,
      "ownership_percentage": 60.0,
      "retired": false,
      "member_cost_center": null
    }
  ],
  "count": 2, "active_count": 2, "retired_count": 0,
  "active_ownership_total": 100.0,
  "ownership_balances": true,
  "member_dimension": "Member"
}
```

`ownership_balances` false adds a `warning` naming the total. It is a warning
rather than a refusal: mid-transition is a real state.

---

## 51. `list_member_events`

Read-only, on by default.

**Arguments:** `company` (required), `member`, `event_type`, `from_date`,
`to_date`, `include_superseded` (default true), `limit`.

`member` takes a Cap Table Entry docname or a bare `member_id`. Legal names are
resolved from the register; the events themselves hold only the anonymous id.
`totals_by_event_type` sums what was returned — an event with `superseded_by`
set has been corrected by a later one and must not be counted twice.

---

## 52. `list_governance_documents`

Read-only, on by default.

**Arguments:** `company` (required), `category`, `include_superseded` (default
true), `limit`.

`operative` is true for a document nothing has superseded. Those are the ones in
force; the rest are history, and the archive keeps both.

---

## 53. `get_governance_document_content`

Read-only, on by default.

**Arguments:** `name` (required), `file`, `max_bytes`.

Returns the document's metadata, its place in the amendment chain, and the
attachment's bytes under `content`, in the same shape `get_attachment_content`
returns. Read permission on the Governance Document is enforced before anything
comes back, and the same 2 MB default / 8 MB hard cap applies. An entry with
several attachments returns the first and says so; an entry with none returns
its metadata with `content: null`.

---

## 54. `create_cap_table_entry`

**MUTATING.** Off by default.

**Arguments:** `company`, `member_id`, `legal_entity_name`, `entity_type`,
`admission_date` (all required), `ownership_percentage`, `member_cost_center`,
`member_dimension`, `notes`.

`entity_type` is checked against the doctype's own option list: Individual,
Trust, LLC, Corporation, Partnership, Other.

**Refuses**, writing nothing:

- a member id already registered for that company, naming the existing entry;
- a percentage outside 0–100;
- a member id that is not a value of the site's Member dimension, naming
  `create_dimension_value` as the remedy;
- `retired` or `withdrawal_date` — a member cannot be created already gone.

The docname is `"<member id> - <company abbr>"`, which is the key
`record_member_event` and everything else resolves.

---

## 55. `update_cap_table_entry`

**MUTATING.** Off by default. Idempotent in the sense that a call which would
change nothing is refused rather than reported as a success.

**Arguments:** `member` (required), `company`, `legal_entity_name`,
`entity_type`, `admission_date`, `ownership_percentage`, `member_cost_center`,
`notes`.

**Deliberately cannot do two things.** It cannot retire a member — that is tool
56, so an exit reaches the event trail rather than appearing only as a changed
checkbox. And it cannot change the `member_id`: that is the key every posting is
tagged with, so changing it would leave journal entry lines pointing at a member
that no longer exists.

---

## 56. `close_cap_table_entry`

**MUTATING.** Off by default.

**Arguments:** `member`, `withdrawal_date`, `notes` (all required), `company`.

Sets the withdrawal date, marks the entry retired, appends the reason to its
notes, and writes a `Withdrawal` Member Event carrying `notes` as the narrative.

**Moves no money.** A member leaving usually involves a final distribution, and
that is a separate `record_member_event` call with its own amount, accounts and
narrative. Bundling them would make the tool that closes a member also a tool
that can pay one.

Refuses a member already retired, a withdrawal date before the admission date,
and a placeholder reason.

---

## 57. `record_member_event`

**MUTATING.** Off by default.

**Arguments:** `company`, `event_type`, `effective_date`, `member`, `narrative`
(all required), `amount`, `counterparty_member`, `offset_je`, `capital_account`,
`counter_account`, `member_dimension`.

Always writes a Member Event. For the five types that book money it also writes
a **DRAFT** Journal Entry, unless `offset_je` names one that already does:

| `event_type` | Debit | Credit |
| --- | --- | --- |
| `Contribution` | the cash side | member capital |
| `Distribution` | member distributions | the cash side |
| `Withdrawal` | member distributions | the cash side |
| `Transfer` | capital of `member` | capital of `counterparty_member` |
| `Reallocation` | capital of `member` | capital of `counterparty_member` |
| `Admission` | — nothing is posted — | |

**Every line carries the member dimension, including the cash side.** Tagging
only the equity line makes a balance sheet filtered by member fail to balance,
and the first person to notice is usually an auditor. A transfer tags its two
lines with the two different members: same account, money never leaving the
company.

**Accounts are shortlisted, never guessed.** With no `capital_account`, the
company's leaf Equity accounts are matched by name — `member capital`,
`partner capital`, `capital contribution` for the capital side; `distribution`,
`draw` for a distribution. Zero matches or more than one is refused with the
candidates listed. The cash side falls back to the company's
`default_bank_account`, then `default_cash_account`.

```json
{
  "name": "8f3c…", "event_type": "Contribution", "amount": 25000.0,
  "member": "Member-01 - ETC", "member_id": "Member-01",
  "offset_je": "ACC-JV-2026-00042",
  "journal_entry_created": true,
  "journal_entry_lines": [
    {"account": "1110 - Bank Checking - ETC", "debit": 25000.0, "credit": 0, "member": "Member-01"},
    {"account": "3100 - Member Capital - ETC", "debit": 0, "credit": 25000.0, "member": "Member-01"}
  ],
  "accounts_used": {
    "capital_account": "3100 - Member Capital - ETC",
    "resolved_by": "name match",
    "counter_account": "1110 - Bank Checking - ETC"
  },
  "next_step": "The Journal Entry ACC-JV-2026-00042 is a DRAFT and has moved no balance. …"
}
```

**Refuses** a narrative too short to be an explanation, a negative amount (a
distribution is its own event type, not a contribution with a minus sign), a
posting event with no amount, a transfer with no counterparty, an `offset_je`
from another company, and a site with no Member dimension on `Journal Entry
Account`.

---

## 58. `submit_member_event`

**MUTATING.** Off by default. **This moves balances.**

**Arguments:** `name` (required).

**Checks two switches.** Its own, and `allow_submit_journal_entry`. That second
switch is where an operator decided whether an AI client may post at all, and a
second door into the same room with a different lock would make the decision
meaningless. With it off:

```
posting this event means submitting Journal Entry ACC-JV-2026-00042, and the submit_journal_entry tool is switched off on this site. That switch is where an operator decides whether an AI client may move a balance, so this tool honours it too. An operator must tick 'allow_submit_journal_entry' in ERPNext MCP Settings. Nothing was changed.
```

An event that books no money — an admission, a reallocation of percentages — has
nothing to post and is refused with that said.

---

## 59. `attach_governance_document`

**MUTATING.** Off by default.

**Arguments:** `company`, `category`, `title` (required), `effective_date`,
`execution_date`, `supersedes`, `file_content`, `file_name`, `file_url`,
`parties`, `notes`.

`category` is one of Operating Agreement, Trust Document, Advisory Agreement,
Board Resolution, Prior Statement, Amendment, Lease, Tax Filing, Audit Packet,
Succession Plan, Family History, Acreage History, EFU Enterprise, Other.

**The last four are narrative, and they exist so a report can ask for them.**
Succession planning, the family's own history, the ground added and released
over the years, and the other Exclusive Farm Use enterprises on the same land
are all things a grower association asks for annually and nobody wants to
retype. Filed under `Other` they are unreachable — a generator cannot query
`Other` and get anything but everything. They amend through `supersedes` like
the rest, though a family history is *revised* rather than superseded by a
later instrument, so `operative` on that chain only reads well if somebody
keeps it tidy.

**Content.** `file_content` is base64 of the document's bytes (no `data:`
prefix) and needs `file_name`; it is stored as a **private** File attached to
the record, readable back through tool 53. `file_url` instead records where an
externally hosted document lives without copying it. The two together are
refused. There is an 8 MB ceiling on content moved through a tool call.

**The chain.** `supersedes` writes the link in both directions — the older
document's `superseded_by` is filled in — so a reader can follow the chain
forward to whatever is current. Superseding a document that has already been
superseded is refused: an amendment goes on the end of the chain, not into the
middle. The doctype's controller separately refuses a cycle, walking the whole
chain rather than checking one hop.

A second document with the same company, category and title is refused, because
two entries claiming to be the same operating agreement is worse than none.

---

# Assets and depreciation

Five tools for assets that serve more than one part of the business, and assets
that are financed. They **layer on** ERPNext's Asset doctype rather than
replacing it.

**What ERPNext does not have.** A cost split — a tractor is not a Harvest asset
or a Perennial Care asset, it is 40% one and 60% the other. And note-tenor
discipline — when an asset is financed, the month the note is paid off and the
month it is fully depreciated should be the same month, and nothing enforces
that.

**Where this app keeps it.** In an `Asset Cost Profile`, one per Asset, not in
custom fields on ERPNext's Asset. Installing this app must change the behaviour
of nothing already on the site, and uninstalling it must give the site back;
grafting fields onto ERPNext's own doctype breaks both. An asset created here is
an ordinary ERPNext Asset an operator can open, edit and delete without knowing
this app exists.

**The most important line in the feature.** `create_asset` sets
`calculate_depreciation = 0` on the Asset. ERPNext runs a daily scheduled job
that posts depreciation for every asset with that flag set, using its own
schedule and its own single cost center. If it also ran, the asset would
depreciate twice — silently, monthly. This app owns the schedule for the assets
it creates; `run_depreciation_cycle` is the only thing that writes for them.

An asset you created in the Desk is untouched by any of this: it has no profile,
so these tools refuse it and ERPNext keeps depreciating it exactly as before.

---

## 60. `depreciation_note_alignment_check`

Read-only, on by default. Requires ERPNext's Asset doctype.

**Arguments:** `company` (required), `as_of` (default today).

```json
{
  "company": "Example Trading Co", "as_of": "2026-07-01",
  "assets": [
    {
      "asset": "ACC-ASS-2026-00003",
      "linked_note": "NOTE-0007", "linked_note_doctype": "Notes Payable",
      "useful_life_months": 84, "note_tenor_months": 60,
      "months_elapsed": 6,
      "remaining_depreciation_months": 78, "remaining_note_months": 54,
      "delta_months": 24, "aligned": false,
      "reading": "The asset still has 24 month(s) of depreciation left after the note is paid off — book value outlives the financing."
    }
  ],
  "checked": 1, "diverged_count": 1, "assets_without_a_note": ["ACC-ASS-2026-00001"]
}
```

Reports every financed asset, not only the broken ones, because "nothing is
wrong" is an answer somebody has to be able to see. A divergence is not
automatically an error — it is something that needs an explanation, and an
explanation nobody wrote down is what this surfaces.

---

## 61. `create_asset`

**MUTATING.** Off by default. Requires ERPNext's Asset doctype.

**Arguments:** `asset_name`, `item_code`, `asset_category`, `purchase_date`,
`purchase_amount`, `useful_life_months` (required), `company`, `salvage_value`,
`depreciation_frequency_months` (default 1), `depreciation_method` (default
Straight Line), `depreciation_start_date`, `cost_center_allocation`,
`linked_note`, `note_doctype`, `note_tenor_months`, `note_maturity_date`,
`depreciation_expense_account`, `accumulated_depreciation_account`, `location`,
`create_item_if_missing` (default true), `notes`.

```json
{
  "company": "Example Trading Co",
  "asset_name": "Tractor A",
  "item_code": "TRACTOR-A",
  "asset_category": "Farm Equipment",
  "purchase_date": "2026-01-01",
  "purchase_amount": 84000,
  "salvage_value": 12000,
  "useful_life_months": 84,
  "cost_center_allocation": [
    {"cost_center": "Harvest", "percentage": 40, "note": "hour meter, 2025 season"},
    {"cost_center": "Perennial Care", "percentage": 60}
  ],
  "linked_note": "NOTE-0007",
  "note_tenor_months": 84
}
```

**Writes** an ERPNext Asset (a **draft** — submit it in ERPNext when the purchase
is real), an Asset Cost Profile, and a fixed-asset Item when `item_code` does not
exist yet. The Asset's own `cost_center` is set to the largest share, so anything
ERPNext files against it lands somewhere sane.

**Refuses**, writing nothing: an allocation that does not total 100 (a 99% asset
under-depreciates the business for the rest of its life); a group or disabled
cost center; the same cost center twice; a frequency that does not divide the
useful life exactly; a salvage value at or above the cost; an asset category the
site does not have; an existing Item not flagged `is_fixed_asset` (flipping that
on an item with stock movements is an inventory decision, not an asset one); a
`bbch_stage` the site has no dimension for; and a `linked_note` whose tenor
differs from `useful_life_months`.

**Depreciation methods.** Straight Line, Written Down Value, Double Declining
Balance, Manual. The last period absorbs the rounding so the asset lands exactly
on its salvage value. Written Down Value with a salvage value of 0 is refused
rather than fudged — the rate `1 - (salvage/cost)^(1/n)` is undefined, because a
declining balance never reaches nought. `Manual` means this app computes nothing
for the asset.

---

## 62. `update_asset_allocation`

**MUTATING.** Off by default.

**Arguments:** `asset`, `new_cost_center_allocation` (required), `company`.

Replaces the split. **Not retroactive**, and that is correct: depreciation
already written keeps the split it was written with, because that is the
history, and rewriting it would change periods already reported. The response
carries `previous_allocation` and how many periods have already been written.

Refuses a total that is not 100, and refuses a change that would leave the
allocation exactly as it is.

---

## 63. `link_asset_to_note`

**MUTATING.** Off by default.

**Arguments:** `asset`, `note_doc_ref` (required), `note_doctype`,
`note_tenor_months`, `note_maturity_date`, `enforce_tenor` (default **true**),
`company`.

The tenor is taken from `note_tenor_months`, from `note_maturity_date`, or from
the note document's own maturity or term field where its doctype has one — and
`tenor_source` says which. `note_doctype` is worked out from the name where the
note is a Notes Payable, a Loan or a Journal Entry.

With `enforce_tenor` true, a mismatch is refused:

```
Asset ACC-ASS-2026-00003 depreciates over 84 month(s) but the note runs 60 — a 24-month divergence. Held apart, the asset is either fully depreciated while payments continue, or still on the books after the note is paid; either way the matching principle is broken and the mismatch is invisible until the final year. …
```

`enforce_tenor=false` links anyway and records the divergence, which tool 60
will keep reporting.

---

## 64. `run_depreciation_cycle`

**MUTATING.** Off by default. **`dry_run` defaults to TRUE.**

**Arguments:** `company` (required), `period_end` (default today), `asset`,
`dry_run`.

One **DRAFT** Journal Entry per asset per period: debit depreciation expense
split across the asset's cost centers, credit accumulated depreciation in one
line, posted on the period's end date. Accounts come from the profile if set,
otherwise from the Asset Category's row for that company.

```json
{
  "company": "Example Trading Co", "period_end": "2026-03-31", "dry_run": true,
  "periods": [
    {
      "asset": "ACC-ASS-2026-00003", "period_index": 1,
      "period_start": "2026-01-01", "period_end": "2026-01-31", "amount": 857.14,
      "lines": [
        {"account": "5200 - Depreciation - ETC", "debit": 342.86, "cost_center": "Harvest - ETC"},
        {"account": "5200 - Depreciation - ETC", "debit": 514.28, "cost_center": "Perennial Care - ETC"},
        {"account": "1810 - Accumulated Depreciation - ETC", "credit": 857.14, "cost_center": "Harvest - ETC"}
      ]
    }
  ],
  "period_count": 3, "total_depreciation": 2571.42,
  "journal_entries": [], "assets_skipped": [],
  "note": "DRY RUN — nothing was written. …"
}
```

- **Idempotent by record.** Every period written is stored on the profile with
  the entry that carries it, so a second run cannot repeat one. Amounts are
  computed from the profile each time rather than read back from saved rows, so
  a catch-up over eleven missed months produces exactly what month-by-month
  running would have.
- **The split adds up.** The last debit absorbs the rounding: 33.33 / 33.33 /
  33.34 of 1000 is three debits totalling exactly 1000. An entry that does not
  balance is not a rounding problem, it is a refused save.
- **Nothing is posted.** The entries are drafts; `submit_journal_entry` posts
  them.
- One misconfigured asset does not take the run down. Assets on the Manual
  method, assets with nothing due, and assets whose depreciation accounts are
  not configured are listed in `assets_skipped` with the reason.

---

## 65. `list_notes_payable`

**Read-only.** On by default. Needs the `Note Payable` DocType, which ships with
this app — run `bench migrate` after upgrading.

**Arguments:** `company` (or `borrower`, same thing), `status`, `include_closed`
(default true), `limit`.

```json
{
  "company": "Example Trading Co",
  "notes": [
    {
      "name": "Example Bank - Defect Sorter - ETC",
      "note_name": "Example Bank - Defect Sorter",
      "borrower": "Example Trading Co", "lender": "Example Bank",
      "status": "Active",
      "principal_original": 120000.0, "principal_outstanding": 110000.0,
      "interest_rate": 6.5, "interest_type": "Fixed",
      "origination_date": "2026-01-01", "maturity_date": "2027-01-01",
      "payment_frequency": "Monthly", "payment_amount": 10650.0,
      "linked_gl_account": "2310 - Notes Payable - ETC",
      "interest_expense_account": "5300 - Interest Expense - ETC",
      "related_asset": "ACC-ASS-2026-00003",
      "payment_count": 1, "last_payment_date": "2026-02-01",
      "next_payment_date": "2026-03-01", "closed": false
    }
  ],
  "count": 1, "active_count": 1,
  "total_original_principal_active": 120000.0,
  "total_outstanding_active": 110000.0,
  "note": "Outstanding balances are the figure maintained by record_loan_payment, not the balance of the linked GL account. …"
}
```

- **`principal_outstanding` is not the ledger.** It is maintained by
  `record_loan_payment`, and diverges from the account by every payment recorded
  as a draft nobody has posted — which in this app is the normal state.
  `get_account_balance` on `linked_gl_account` is the ledger's answer.
- **`next_payment_date` is a projection**, not a schedule the lender agreed to:
  it is the frequency applied to the last payment recorded, clamped to the
  maturity date. A `Balloon` note projects its maturity and nothing else; a
  `Custom` one projects nothing.
- Closed notes are listed by default. A note that has been paid off is part of
  the history.

---

## 66. `create_note_payable`

**MUTATING.** Off by default. Needs the `Note Payable` DocType.

**Arguments:** `note_name` (required), `lender` (required), `principal_original`
(required), `origination_date` (required), `borrower`/`company`,
`principal_outstanding` (defaults to the original), `interest_rate`,
`interest_type`, `maturity_date`, `payment_frequency`, `payment_amount`,
`linked_gl_account`, `interest_expense_account`, `related_asset`,
`enforce_asset_tenor` (default true), `document_reference`, `notes`.

The docname is `"<note_name> - <company abbr>"`, and `note_name` is unique per
borrower.

```json
{"name": "create_note_payable",
 "arguments": {"borrower": "Example Trading Co",
               "note_name": "Example Bank - Defect Sorter",
               "lender": "Example Bank",
               "principal_original": 120000,
               "origination_date": "2026-01-01",
               "maturity_date": "2027-01-01",
               "interest_rate": 6.5,
               "linked_gl_account": "2310",
               "interest_expense_account": "5300",
               "related_asset": "ACC-ASS-2026-00003"}}
```

- **Not ERPNext's Loan module.** That models the company as the *lender*, with an
  application, a disbursement and half a dozen doctypes. This is the other side.
- **`related_asset` runs the tenor check.** It delegates to `link_asset_to_note`,
  so an asset whose useful life does not equal the note's term is refused by the
  same code that refuses it from the other direction. Pass
  `enforce_asset_tenor=false` when the divergence is deliberate. The note and the
  link are one transaction — a refused link leaves no note behind.
- **Refuses:** a duplicate name for the same borrower, a non-positive principal, a
  negative outstanding balance, a maturity before origination,
  `interest_type: "Zero"` with a non-zero rate, a `linked_gl_account` that is not
  a plain Liability (a Payable- or Receivable-typed one would show the note's
  principal as a party balance that never ages out), an
  `interest_expense_account` that is not an Expense, and any attempt to create a
  note already closed.

---

## 67. `record_loan_payment`

**MUTATING.** Off by default. Needs the `Note Payable` DocType.

**Arguments:** `note` (required), `payment_date` (required), `total_amount`
(required), `offset_bank_account` (required), `principal_split`,
`interest_split`, `company`, `notes_payable_account`,
`interest_expense_account`, `narrative`.

Pass `principal_split`, `interest_split`, or one and let the other be derived.
They have to add up to `total_amount` or nothing is written.

```json
{
  "note": "Example Bank - Defect Sorter - ETC", "payment_date": "2026-02-01",
  "total_amount": 10650.0, "principal_split": 10000.0, "interest_split": 650.0,
  "principal_outstanding_before": 120000.0, "principal_outstanding_after": 110000.0,
  "journal_entry": "ACC-JV-2026-00051",
  "accounts_used": {
    "notes_payable_account": "2310 - Notes Payable - ETC",
    "interest_expense_account": "5300 - Interest Expense - ETC",
    "offset_account": "1110 - Bank Checking - ETC",
    "offset_bank_account": "Operating - Example Bank"
  },
  "lines": [
    {"account": "2310 - Notes Payable - ETC", "debit": 10000.0, "credit": 0},
    {"account": "5300 - Interest Expense - ETC", "debit": 650.0, "credit": 0},
    {"account": "1110 - Bank Checking - ETC", "debit": 0, "credit": 10650.0}
  ],
  "note_text": "Journal Entry ACC-JV-2026-00051 is a DRAFT and has moved no balance. …"
}
```

- **The split is the whole job.** A payment leaving a bank account is one number
  whose halves land in completely different places. Booked as a single line
  against the liability, the year's interest expense reads as nil and the balance
  sheet says the note was paid down by more than it was.
- **`offset_bank_account` takes either** a Bank Account record — preferred, since
  the journal line then carries it, which is what lets a bank reconciliation
  match this entry — or the GL account directly.
- **The entry is a DRAFT.** The note's outstanding figure is decremented
  immediately, so until it is submitted the record and the liability account
  disagree by the principal. The response says so every time.
- **Refuses:** a closed note, a payment dated before origination, a principal
  component larger than the balance outstanding, a negative component, a split
  that does not add up, and an interest component with no expense account to put
  it in.

---

## 68. `close_note_payable`

**MUTATING.** Off by default. Needs the `Note Payable` DocType.

**Arguments:** `note` (required), `disposition` (required — `Paid Off`,
`Refinanced` or `Written Off`), `disposition_date` (required), `narrative`
(required), `company`, `superseded_by`, `zero_remaining_balance`.

```json
{"name": "close_note_payable",
 "arguments": {"note": "Example Bank - Defect Sorter - ETC",
               "disposition": "Written Off",
               "disposition_date": "2026-06-30",
               "narrative": "Forgiven under the 2026 family settlement deed."}}
```

- **Writes NO journal entry, deliberately.** Relieving a written-off balance is a
  posting with real tax consequences — forgiven debt is usually income — and a
  refinance moves a balance between two liability accounts. Both belong to
  somebody who meant them. The response names the account still carrying the
  balance and the entry that is owed, so the omission cannot pass unnoticed.
- **`Paid Off` with a balance still showing is refused.** That means either a
  final payment was never recorded (`record_loan_payment` writes the entry that
  books it) or the balance carried here is stale. If it is stale,
  `zero_remaining_balance=true` writes it down and records an `Adjustment` row in
  the note's history saying exactly that.
- **`superseded_by`** names the note that replaced this one, for a refinance, so a
  reader following the chain forward lands on what is still owed. It is only
  accepted on a `Refinanced` disposition.
- Also refuses a note already closed, a disposition date before origination, and
  a narrative too short to be an explanation.

---

## 69. `set_opening_balance`

**MUTATING.** Off by default.

**Arguments:** `posting_date` (required), `entries` (required), `user_remark`
(required), `company`, `opening_equity_account`.

Each entry is `{account, dr_or_cr, amount, cost_center, dimensions, narrative}`.
`amount` is always positive — the direction lives in `dr_or_cr` (`dr`/`debit`/`d`
or `cr`/`credit`/`c`). **Do not include the equity line; it is computed.**

```json
{"name": "set_opening_balance",
 "arguments": {"company": "Example Trading Co",
               "posting_date": "2026-01-01",
               "user_remark": "Equipment transferred in on dissolution, per the bill of sale",
               "entries": [
                 {"account": "1710", "dr_or_cr": "dr", "amount": 52650,
                  "narrative": "Two forklifts and a sprayer"}]}}
```

```json
{
  "name": "ACC-JV-2026-00060", "docstatus": 0, "docstatus_label": "draft",
  "company": "Example Trading Co", "posting_date": "2026-01-01",
  "opening_equity_account": "3300 - Opening Balance Equity - ETC",
  "opening_equity_resolved_by": "account_number 3300",
  "opening_equity_side": "credit", "opening_equity_amount": 52650.0,
  "entered_debit": 52650.0, "entered_credit": 0.0, "balancing_difference": 52650.0,
  "line_count": 2, "total_debit": 52650.0, "total_credit": 52650.0,
  "flags_set": {"is_opening": "Yes", "voucher_type": "Opening Entry"},
  "note": "The 52650.0 offsetting line against 3300 - Opening Balance Equity - ETC was computed, not supplied …"
}
```

- **The plug is computed, not supplied.** Every historical fact brought onto a set
  of books balances against opening equity; a caller who works that out for
  itself gets it wrong by a few cents on the third event, after which the ledger
  never balances again.
- **The flags matter.** `is_opening` — and `Opening Entry` where the site's
  Journal Entry offers that voucher type — are what keep these amounts out of the
  period's activity in every report that separates the two. Nothing warns you
  when they are missing; the P&L simply reads as though the company earned its
  opening equity in January. Both are set only where this site's own meta has
  them, and `flags_set` reports what was actually written.
- **The equity account is found, not guessed:** account number `3300` first, then
  a leaf Equity account named after opening balances. Zero matches and more than
  one are both refusals, with the company's leaf equity accounts listed.
  `opening_equity_account` overrides it.
- **Entries that already balance get no plug at all**, and the response says the
  equity account was not touched.
- **Refuses** a group, disabled or wrong-company account on any line; a group or
  disabled cost center; a dimension value that does not exist; a non-positive
  amount; an unsupported entry field, by name; and an
  `opening_equity_account` that is not Equity. Nothing is written unless every
  line validates.
- **It is a DRAFT.** `submit_journal_entry` posts it — and an opening balance is
  the entry most worth reading first, because it is the one nobody will ever
  re-derive.

---

## 70. `create_bank_account`

**MUTATING.** Off by default. Needs the `Bank Account` DocType, which ships with
ERPNext's Accounts module.

**Arguments:** `account_name` (required), `bank_name` (required), `account`
(required for a company account), `company`, `account_no` /`bank_account_no`,
`iban`, `is_company_account` (default true), `party_type`, `party`, `disabled`.

ERPNext names the record `"<account_name> - <bank>"`. That string is what goes
into a bank feed's configuration, so it is worth choosing `account_name`
deliberately — the account mask is the usual way to tell two accounts at one bank
apart.

```json
{"name": "create_bank_account",
 "arguments": {"company": "Example Trading Co",
               "account_name": "Advisors Cash - ••3158",
               "bank_name": "Example Bank Advisors",
               "account": "1151",
               "account_no": "••3158"}}
```

- **A Bank Account holds no balance.** It is a mapping: this institution, this
  account number, posts to that GL account. Bank Transactions hang off it,
  reconciliation reads it, a feed writes into it. The money lives in the Account
  it points at.
- **Pre-create it before the first sync.** A feed that runs first makes its own,
  named whatever the feed calls the account and pointed at a GL account the feed
  picked. Renaming that afterwards is fine; *repointing* it is not — once
  transactions have been imported, the GL account named here is where they
  reconcile to.
- **Two doctypes, one transaction.** The `Bank` is created when the institution is
  new, and a failure anywhere after that leaves neither.
- **Refuses:** an unknown company; a GL account that does not exist, belongs to
  another company, is a group or is disabled; a GL account whose `root_type` is
  neither Asset (a bank account) nor Liability (a credit card); an **Asset**
  account whose `account_type` is not `Bank` or `Cash`, because ERPNext's own
  account picker and its reconciliation tool both filter on that flag and an
  untyped account saves here and then cannot be reconciled; an `account_name`
  already used in this company; `party`/`party_type` together with
  `is_company_account`; and a `bank_name` Frappe would refuse as a docname.
- **Warns**, rather than refuses, when the GL account is already another Bank
  Account's — legitimate for a sweep arrangement, a mistake everywhere else.

---

## 71. `delete_account`

**MUTATING, DESTRUCTIVE, IRREVERSIBLE.** Off by default. There is no undo, no
draft and no cancelled state; the record is gone.

**Arguments:** `name` (required), `company`, `force_check_gl_entries`,
`force_check_children`, `force_check_company_defaults`,
`force_check_bank_accounts` — all four checks default to **true**.

```json
{
  "deleted": "1190 - Cash Clearing - ETC",
  "account": {"name": "1190 - Cash Clearing - ETC", "account_number": "1190", "…": "…"},
  "checks_passed": {
    "gl_entries": "no GL entries, ever, and no journal entry line references it",
    "children": "no child accounts, enabled or disabled",
    "company_defaults": "no Company field points at it",
    "bank_accounts": "no Bank Account record posts to it"
  },
  "checks_skipped": [], "was_root": false,
  "note": "Gone. Unlike disable_account there is nothing left: no record, no history, and the account number 1190 is free …"
}
```

- **Prefer `disable_account`.** Almost always. Disabling keeps the postings, the
  reports still balance, and the account drops out of pickers.
- **The one thing disabling cannot do is free the number.** A disabled account
  still holds it, and on a company being renumbered onto a real chart — fifty
  accounts a bundled chart created that nobody ever posted to — that is the
  entire problem. That is what this tool is for.
- **Every check is a refusal, and they are all reported at once.** Four calls each
  naming one reason is how somebody deletes the wrong account trying to satisfy
  the last one.
- **Draft journal entry lines count.** A draft writes no GL row, so an account
  referenced by one reads as untouched; deleting it leaves a draft nobody can
  submit and nobody can fix.
- **Turning a check off does not make a referenced account deletable.** Frappe's
  own link-integrity check still runs on the delete. The flag changes which error
  you get, not the outcome.


---

## 72. `create_fiscal_year`

**MUTATING.** Off by default. Needs the `Fiscal Year` DocType, which ships with
ERPNext's Accounts module.

**Arguments:** `year_name` (required), `year_start_date` (required),
`year_end_date` (required), `companies`, `is_short_year`, `disabled`,
`auto_created`.

**This is the prerequisite for booking anything historical.** ERPNext refuses a
posting whose date falls outside a fiscal year, and it refuses it from inside the
document being saved — so on a site whose only year is 2026, a March 2025
equipment transfer fails with an error about a *date* rather than about a missing
*year*. `set_opening_balance` and `create_journal_entry` cannot reach that period
until this has run.

```json
{"name": "create_fiscal_year",
 "arguments": {"year_name": "2025",
               "year_start_date": "2025-01-01",
               "year_end_date": "2025-12-31"}}
```

```json
{
  "name": "2025", "year": "2025",
  "year_start_date": "2025-01-01", "year_end_date": "2025-12-31",
  "disabled": false, "is_short_year": false, "auto_created": false,
  "companies": [], "scope": "every company on this site",
  "expected_end_date_for_a_full_year": "2025-12-31",
  "note": "A Fiscal Year is a permission for a date, not a posting. Nothing was booked and no balance moved …",
  "next_step": "Historical events for this period can now be booked. …"
}
```

- **`companies` is optional, and omitting it is not an omission.** ERPNext models
  a global fiscal year as one with no company restrictions — the `companies`
  child table is a *restriction* — and that is what almost every site wants.
- **The overlap check is company-aware.** A global year collides with everything;
  two restricted years collide only if they share a company. Two years covering
  the same day for the same company make ERPNext's own `get_fiscal_year`
  ambiguous, and which year a posting lands in stops being a fact about the
  posting. **Disabling a year does not free its range.**
- **The one-year rule.** ERPNext's `FiscalYear.validate_dates` requires the end
  date to be exactly one year after the start, less a day, unless
  `is_short_year` is set — and its own message does not say which date it wanted.
  This computes it and names it. Leap days are clamped the way the calendar
  does: a year starting 29 February ends on the 27th.
- **ERPNext's own overlap check is company-blind on some versions** and refuses
  any date collision at all. Where the framework is stricter than this tool, its
  refusal is what a caller gets, unchanged — this never loosens a rule the
  framework enforces.
- **Also refuses** a `year_name` already on the site (a Fiscal Year names itself,
  so the name is the docname), an end date before the start, a one-day year, and
  a company this site does not have.
- Creating one **disabled** is accepted and warned about: ERPNext still refuses
  postings dated inside a disabled year.

---

## 73. `update_fiscal_year`

**MUTATING.** Off by default. Needs the `Fiscal Year` DocType.

**Arguments:** `year_name` (required), `new_year_start_date`,
`new_year_end_date`, `is_short_year`, `disabled`.

```json
{"name": "update_fiscal_year",
 "arguments": {"year_name": "2025", "disabled": true}}
```

- **RISK: moving the dates moves no posting.** It changes which year — or no year
  at all — every posting already written falls into, retroactively, including
  periods already reported. So the GL entries that would fall *out* of the new
  range are counted first, and any at all is a refusal naming the count. A
  posting in no fiscal year drops out of period comparisons and cannot be
  corrected without reopening a year that no longer covers it. Widening a range
  is fine; shrinking one with history in it is not.
- **Cannot rename.** ERPNext names a Fiscal Year after itself, so the name is the
  docname and is the string every Journal Entry, Budget and Period Closing
  Voucher that names a year holds. Passing `year` or `new_year_name` is refused
  by name.
- **Cannot change `companies`.** Narrowing the scope of a year with postings in it
  takes those postings out of any fiscal year for the companies it drops;
  widening it can create an overlap this tool would have refused at creation.
  Both are Desk decisions.
- Same company-aware overlap check as `create_fiscal_year`, against every other
  year.
- **Disabling deletes nothing.** The entries already in the range remain and still
  appear in reports covering them; ERPNext simply refuses *new* postings dated
  inside a disabled year. It is reversible with `disabled=false`.

---

## 74. `post_opening_balance_journal_entry`

**MUTATING.** Off by default. `submit: true` additionally requires
`allow_submit_journal_entry`.

**Arguments:** `posting_date` (required), `lines` (required), `user_remark`
(required), `company`, `offset_account`, `voucher_type`, `submit`.

Each line is `{account, side, amount, cost_center, dimensions, narrative}`.
`amount` is always positive — the direction lives in `side` (`debit`/`dr` or
`credit`/`cr`). Unlike `set_opening_balance`, **these lines are taken as given**;
the only line this tool adds is the balancing one.

```json
{"name": "post_opening_balance_journal_entry",
 "arguments": {"company": "Example Trading Co",
               "posting_date": "2026-01-01",
               "user_remark": "Trial balance at 2025-12-31 per the prior system, reviewed by TP",
               "offset_account": "3300",
               "submit": false,
               "lines": [
                 {"account": "1100", "side": "debit", "amount": 1700000},
                 {"account": "1710", "side": "debit", "amount": 52650},
                 {"account": "2310", "side": "credit", "amount": 200000}]}}
```

```json
{
  "name": "ACC-JV-2026-00061", "docstatus": 0, "docstatus_label": "draft",
  "company": "Example Trading Co", "posting_date": "2026-01-01",
  "offset_account": "3300 - Opening Balance Equity - ETC",
  "offset_side": "credit", "offset_amount": 1552650.0,
  "entered_debit": 1752650.0, "entered_credit": 200000.0,
  "balancing_difference": 1552650.0,
  "line_count": 4, "total_debit": 1752650.0, "total_credit": 1752650.0,
  "flags_set": {"is_opening": "Yes", "voucher_type": "Opening Entry"},
  "submitted": false, "gl_entries_created": 0,
  "note": "The 1552650.0 balancing line against 3300 - Opening Balance Equity - ETC was written because the 3 line(s) given were out by that much. …"
}
```

- **This or `set_opening_balance`?** Use that one when you know one side of one
  historical event and want the equity plug computed. Use this when you are
  transcribing a whole trial balance off the previous system: both sides are
  already in hand, and a one-event-at-a-time tool means one call and one stray
  equity line per account.
- **The offset is named, not found.** `offset_account` is required exactly when
  the lines do not balance, and the difference is written to it as a single line.
  Normally Opening Balance Equity (`3300`) — but retained earnings or a suspense
  account are legitimate and are not second-guessed, which is the one place this
  is more permissive than `set_opening_balance`. Naming an offset when the lines
  already balance writes no line, and the response says so.
- **`submit: true` posts it**, `0 → 1`, writing GL Entries. That path checks
  `allow_submit_journal_entry` **before anything is written**, so a site with
  posting switched off gets a refusal rather than a draft nobody asked for. The
  default is `false`.
- **`voucher_type` defaults to `Opening Entry`** where the site offers it, and is
  dropped with a note where it does not. A voucher type the caller names and the
  site does not have is a refusal — silently posting it as something else would
  mislabel an entry nobody re-reads.
- **Refuses** a group, disabled or wrong-company account on any line or on the
  offset; a group or disabled cost center; a dimension value that does not exist;
  a non-positive amount; and an unsupported line field, by name — including
  `dr_or_cr`, which is the *other* tool's spelling of `side`.

---

## 75. `bulk_submit_journal_entries`

**MUTATING.** Off by default. Additionally requires
`allow_submit_journal_entry`, checked before anything is touched.

**Arguments:** `names` (required) — up to 500 Journal Entry docnames.

```json
{"name": "bulk_submit_journal_entries",
 "arguments": {"names": ["ACC-JV-2026-00060", "ACC-JV-2026-00061", "ACC-JV-2026-00062"]}}
```

```json
{
  "total": 3, "submitted": 2, "skipped": 1, "failed": 0,
  "submitted_names": ["ACC-JV-2026-00061", "ACC-JV-2026-00062"],
  "failed_names": [],
  "results": [
    {"name": "ACC-JV-2026-00060", "ok": true, "skipped": "already_submitted", "error": null, "docstatus": 1},
    {"name": "ACC-JV-2026-00061", "ok": true, "skipped": "", "error": null, "docstatus": 1},
    {"name": "ACC-JV-2026-00062", "ok": true, "skipped": "", "error": null, "docstatus": 1}],
  "note": "Each entry was submitted in its own transaction …",
  "next_step": "Every entry in the batch is posted or was already posted. Nothing is outstanding."
}
```

- **One document's failure is not the batch's.** Each submit runs in its own
  transaction — committed on success, rolled back on failure — and the loop
  carries on. This is the only place in this app that commits mid-call, and it is
  deliberate: the alternative is a batch of five hundred where number four
  hundred fails and the request rolls back the three hundred and ninety-nine
  postings that were fine. It is what Frappe's own bulk submit does.
- **Already submitted is `ok`, not an error** — `skipped: "already_submitted"` —
  so a half-finished batch is safe to retry whole. **Cancelled is a failure:** it
  cannot be posted again.
- **It does not go round `submit_journal_entry`'s switch.** That switch is where
  an operator decided whether an AI client may move a balance at all, and this
  fails before touching anything if it is off.
- Duplicate names in one call are submitted once. More than 500 is refused before
  anything posts.

---

## 76. `delete_draft_journal_entry`

**MUTATING, destructive.** Off by default.

**Arguments:** `name` (required), `reason` (required).

```json
{"name": "delete_draft_journal_entry",
 "arguments": {"name": "ACC-JV-2026-00062",
               "reason": "duplicate of ACC-JV-2026-00061, keyed twice during the opening-balance load"}}
```

```json
{
  "deleted": {
    "name": "ACC-JV-2026-00062", "company": "Example Trading Co",
    "posting_date": "2026-01-01", "voucher_type": "Opening Entry",
    "user_remark": "Trial balance at 2025-12-31 per the prior system",
    "total_debit": 1752650.0, "total_credit": 1752650.0, "line_count": 4,
    "accounts": [{"account": "1100 - Cash - ETC", "debit": 1700000.0, "credit": 0.0}]},
  "reason": "duplicate of ACC-JV-2026-00061, keyed twice during the opening-balance load",
  "gl_entries_removed": 0,
  "note": "A draft writes no GL Entries … The MCP Action Log row for this call is now the only record that the entry existed."
}
```

- **The gap it fills.** `cancel_journal_entry` refuses a draft, correctly: there
  is nothing to reverse, because a draft has moved no balance. That left an
  unwanted draft with no MCP path at all.
- **Drafts only, whatever is asked.** A **submitted** entry has written GL
  Entries; deleting it would take those balances with it and leave nothing saying
  why — refused, and pointed at `cancel_journal_entry`. A **cancelled** entry and
  its reversing rows are the evidence that a posting was made and undone —
  deleting one leaves an audit trail with a hole in it, so that is refused too.
- **It is a real delete**, `frappe.delete_doc`, nothing left in the table. Which
  is why the response carries the entry's company, date, totals and every line:
  once the call returns, the MCP Action Log row is the only record that the
  document ever existed.

---

# Attaching evidence

## 77. `attach_file_to_document`

**MUTATING**, default OFF (`allow_attach_file_to_document`).

**Arguments:** `doctype` (required), `name` (required), `file_name` (required),
`file_content` (base64) **or** `file_url`, `is_private` (default `true`),
`company` (optional guard), `allow_cancelled` (default `false`), `dry_run`
(default `false`).

**Returns** `file` (the File docname), `file_url`, `file_size`, `size_human`,
`mime_type`, `sha256`, `is_private`, `attached_to_doctype`, `attached_to_name`,
`parent_docstatus`, `parent_company`, `attachments_before` and
`attachments_after`.

**What it is for.** A year of brokerage statements belongs on the Journal
Entries that book them; a receipt belongs on the Bank Transaction it explains; a
purchase contract belongs on the Asset. `attach_governance_document` (**59**)
files a *new* Governance Document and attaches to that, which is right for a
trust instrument and useless for putting December's statement on December's
entry. This one attaches to the record you name, and creates nothing else — no
balance moves, no docstatus changes, no existing row is touched.

**What it refuses, all of it read off the site rather than compiled into the
app:**

| Refusal | Where the rule comes from |
| --- | --- |
| Unknown `doctype` or `name` | the site's own schema and tables |
| Acting user cannot `write` the parent | Frappe's permission model — the same permission the Desk's attach control needs |
| Parent is **cancelled** (docstatus 2) | the parent's own state; `allow_cancelled=true` overrides |
| `file_name` the document already has | that document's existing attachments, with the clashing File named |
| Too many attachments | the parent DocType's `max_attachments` |
| Disallowed extension | whatever allowlist System Settings declares — nothing on a site that declares none, which is Frappe's own answer |
| `company` does not match the parent's | the parent's `company` field. A `company` passed for a doctype with **no** company field is an error, not a shrug: a guard the caller believes ran and did not is worse than no guard |

**Size.** `file_content` is base64 and capped at 8 MB, the same ceiling
`attach_governance_document` uses. Base64 in a JSON call is expensive — a large
statement is better uploaded in the Desk and recorded here with `file_url`.

**`dry_run` defaults to FALSE**, unlike `import_chart_of_accounts` and
`run_depreciation_cycle`. Those write many documents and are hard to unpick;
this adds a single File and changes no balance. Making the common case cost two
round trips would be safety theatre. A batch script should dry-run its target
list once, then run live.

**Audit.** The MCP Action Log row names the parent doctype and docname, the
filename, the size and the sha256 of the stored bytes. The base64 payload itself
is elided to a note of its length — it is logged as
`<11184812 characters elided>` rather than crowding every other argument out of
the row.

```
attached wfa-statement-2025-12-31.pdf (412.0 KB, sha256 9f2c1ab77e04) to
Journal Entry ACC-JV-2026-03369 as File a7f3c9e21b (private)
```

---

## 78. `list_parcels`

Read-only, default ON (`allow_list_parcels`).

**Arguments:** `owning_entity` (or `company` — the same thing), `county`,
`use_type`, `title_holder`, `linked_to_asset` (boolean), `limit`.

**Returns** `parcels`, `count`, `total_in_register`, `total_acreage`,
`total_appraised_value`, `average_per_acre`, `by_use_type`, `oldest_appraisal`,
`newest_appraisal` and `parcels_without_value`.

Totals cover the rows returned, not the whole register, and a `limit` that hides
part of it says so before the totals are trusted. `oldest_appraisal` is how you
find out the valuation is four years stale.

**Appraised value is not book value.** What the balance sheet carries is the
Asset's cost; this is market. They are meant to differ — see **82**.

---

## 79. `get_parcel`

Read-only, default ON (`allow_get_parcel`).

**Arguments:** `parcel` (required — a docname like `Red Camp - HLD`, or just
`Red Camp`), `owning_entity`.

**Returns** the parcel, its `asset` (with the gap between cost and appraised
value), every `lease` over it in either direction, `active_leases` and
`attachments`.

A bare parcel name matching parcels in two entities is refused with both named
rather than resolved to whichever came first.

---

## 80. `create_parcel`

**MUTATING**, default OFF (`allow_create_parcel`).

**Arguments:** `parcel_name` (required), `owning_entity` (or `company`),
`parcel_id`, `county`, `state`, `address`, `acreage`, `use_type`,
`title_holder`, `appraised_value`, `appraised_as_of`, `appraiser`,
`appraisal_document`, `related_asset`, `notes`.

**The docname is `<parcel_name> - <entity abbr>`**, so two entities in one family
may each have a "Home Place".

| Refusal | Why |
| --- | --- |
| A second parcel with the same name for one entity | the docname is built from it |
| A second parcel with the same `parcel_id` for one entity | that number is the county assessor's primary key; two of them means a typo |
| Negative acreage or appraised value | not opinions |
| An unknown `use_type` | the options are read off the DocType, so a customised site's own list is what applies |
| A `title_holder`, `appraisal_document` or `related_asset` on another company's books | a parcel and the records explaining it belong to one entity |

**Warns rather than refusing** when a value arrives with no as-of date, or with
no appraisal document behind it. A figure somebody remembered is worth recording;
it just should not be mistaken for a valuation.

---

## 81. `update_parcel`

**MUTATING**, default OFF (`allow_update_parcel`).

**Arguments:** `parcel` (required), plus any of `parcel_id`, `county`, `state`,
`address`, `acreage`, `use_type`, `appraised_value`, `appraised_as_of`,
`appraiser`, `title_holder`, `appraisal_document`, `notes`. An empty string
clears an optional field.

**Returns** the parcel and `changes`, every one as `[before, after]`.

Cannot rename it (the docname is built from `parcel_name`, and every lease and
asset link points at that docname), cannot move it between entities (a parcel
changing hands is a conveyance, not an edit), and cannot set `related_asset` —
that is **82**, which checks things this does not. A no-op update is refused
rather than reported as a success.

---

## 82. `link_parcel_to_asset`

**MUTATING**, default OFF (`allow_link_parcel_to_asset`).

**Arguments:** `parcel` (required), `asset` (required), `replace` (default
`false`), `dry_run` (default `false`).

**Returns** the parcel, an `asset` block with `gross_purchase_amount`,
`appraised_value` and `appraisal_over_book`, and `unrealised_appreciation` when
both figures exist.

**The gap is the point.** A parcel appraised at 3,100,000 sitting on the books at
a 1998 cost of 240,000 is not a discrepancy to be fixed — it is unrealised
appreciation, it is the single most important number in a succession
conversation, and neither record shows it alone. Nothing here posts it, because
unrealised appreciation is not a journal entry.

Refuses an asset on another company's books, an asset already claimed by a
different parcel, and a parcel that is already linked unless `replace=true`.

---

## 83. `list_leases`

Read-only, default ON (`allow_list_leases`).

**Arguments:** `owning_entity` (or `company`), `status`, `direction`, `parcel`,
`counterparty`, `active_on`, `expiring_within_days` (default 90), `limit`.

**Returns** `leases`, `annual_rent_receivable`, `annual_rent_payable`,
`net_annual_rent`, `rent_not_annualisable`, `expiring_soon`,
`active_past_expiration` and `as_of`.

**Rent is annualised for Active leases only.** A crop share and a one-time
payment have no annual rate: they are listed under `rent_not_annualisable`
rather than counted as zero, because a rent roll that quietly treats an unknown
as nothing understates the whole portfolio.

**Nothing here expires a lease.** A lease marked Active whose expiration date has
passed is reported under `active_past_expiration` and left exactly as it was.
Farm ground routinely runs on month to month past its stated term, and a status
that flipped itself on a calendar would erase the difference between "still
running" and "nobody has looked at this in years".

---

## 84. `get_lease`

Read-only, default ON (`allow_get_lease`).

**Arguments:** `lease` (required), `owning_entity`.

**Returns** the lease, `parcel_detail`, `attachments`, `annualised_rent`,
`past_expiration` and `in_force_today`.

Read `direction` before reading `rent_amount`: Outbound means the owning entity
collects it, Inbound means it pays it.

---

## 85. `create_lease`

**MUTATING**, default OFF (`allow_create_lease`).

**Arguments:** `lease_name`, `direction`, `lessor`, `lessee`, `effective_date`
(all required), `owning_entity` (or `company`), `expiration_date`, `status`
(default `Active`), `termination_date`, `termination_reason`, `parcel`,
`counterparty`, `rent_amount`, `rent_frequency` (default `Annual`), `rent_terms`,
`governance_document`, `lease_document_url` **or** `file_content` +
`file_name`, `notes`.

**Books nothing.** No journal entry, no receivable, no schedule. Recording an
agreement and booking its consequences are separate acts, and this is the first
one.

**Direction is stated, not guessed.** The result carries a `direction_check`
saying whether the party names agree with the stated direction —
`consistent`, `inconsistent` or `unverified`. Reported, never enforced: a legal
name ("Highland Ltd Liability Co.") and a Company docname ("Highland LLC")
routinely differ, and a refusal built on string matching is one nobody could get
past.

Refuses a duplicate lease name for one entity, the same party as both lessor and
lessee, an expiration or termination date before the effective date, `Terminated`
with no termination date, and negative rent (rent flowing the other way is a
lease in the other direction). `file_content` is base64 with the same 8 MB
ceiling every attachment tool uses; a large scan is better uploaded in the Desk
and recorded with `lease_document_url`.

---

## 86. `update_lease`

**MUTATING**, default OFF (`allow_update_lease`).

**Arguments:** `lease` (required), plus any of `status`, `expiration_date`,
`termination_date`, `termination_reason`, `rent_amount`, `rent_frequency`,
`rent_terms`, `lessor`, `lessee`, `parcel`, `counterparty`,
`governance_document`, `notes`.

Cannot rename it — a renewed lease is a **new** lease with its own term — and
cannot move it between entities. Marking one `Terminated` requires a
`termination_date` in the same call: "we ended it" without "when" is not a record
anybody can rely on later.

---

## 87. `list_related_parties`

Read-only, default ON (`allow_list_related_parties`).

**Arguments:** `company`, `party_type`, `relationship_to_company`, `supplier`,
`current_only` (default `false`), `limit`.

**Returns** `parties`, `count`, `distinct_people`, `current_count`,
`ended_count`, `by_relationship`, `by_party_type`, `linked_to_supplier`,
`linked_to_cap_table`, `without_governing_document` and `without_tax_id`.

**One person may appear more than once.** A Manager who is also a Member is two
entries, under two instruments, from two dates — `count` counts relationships and
`distinct_people` counts names. Ended relationships are listed by default: the
transactions they explain are still in the ledger.

`without_governing_document` is the first thing an examiner asks for.

---

## 88. `get_related_party`

Read-only, default ON (`allow_get_related_party`).

**Arguments:** `party` (required — a docname like
`Tim Polehn - Manager - OML`, or just the name), `company`.

**Returns** the relationship, `other_roles`, `cap_table_detail`,
`supplier_detail`, `parcels_titled` and `leases_as_counterparty`.

**Never returns more than four digits of a taxpayer id**, including from a linked
Supplier: `supplier_detail.tax_id` says only whether one is on file. A bare name
held in two capacities is refused with both docnames listed.

---

## 89. `create_related_party`

**MUTATING**, default OFF (`allow_create_related_party`).

**Arguments:** `party_name`, `party_type`, `relationship_to_company`,
`effective_date` (all required), `company`, `end_date`, `tax_id_type`,
`tax_id_last4`, `address`, `cap_table_entry`, `supplier`, `governing_document`,
`notes`.

**The docname is `<name> - <relationship> - <company abbr>`**, because somebody
who is both Manager and Member of an LLC is two entries under two instruments.
The same name and role twice is refused; a second role is expected.

**Four digits, never nine.** `tax_id_last4` takes exactly four digits and refuses
nine, naming the four to send instead. Not truncated, not masked, not accepted
with a warning. The controller enforces the same rule, because the Desk form is a
second door into the same field. The full number belongs on the signed W-9, on
paper.

**This is not the Party field on a Journal Entry.** ERPNext already answers "who
was this transaction with"; this answers "who is related to us, in what capacity,
since when, and under what document", which no transactional field can, because a
transaction is an event and a relationship is a state.

---

## 90. `update_related_party`

**MUTATING**, default OFF (`allow_update_related_party`).

**Arguments:** `party` (required), plus any of `party_type`, `effective_date`,
`end_date`, `tax_id_type`, `tax_id_last4`, `address`, `cap_table_entry`,
`supplier`, `governing_document`, `notes`.

`party_name`, `relationship_to_company` and `company` are the key and cannot
change: a change of role is a **new** relationship, so register it and set an
`end_date` on this one. An entry is never deleted when a relationship ends — the
transactions it explains are still in the ledger, and a prior year's disclosure
schedule still needs to know who was who at the time.

---

## 91. `generate_quarterly_investment_report`

**MUTATING**, default OFF (`allow_generate_quarterly_investment_report`).

**Arguments:** `quarter` (required, as `2026-Q2`), `company`, `output_format`
(`pdf` default, or `docx`), `output_path`, `overwrite`, `investment_accounts`,
`cash_clearing_account`, `holdings`, `benchmark_rate_percent`,
`manager_fee_percent` (default 1.00), `custody_fee_percent` (default 1.00),
`performance_fee_percent` (default 20), `high_water_mark`, `net_contributions`,
`title`, `dry_run`.

**Returns** `aum`, `activity`, `fees`, `performance`, `holdings`,
`cash_clearing`, `reconciliation`, `preconditions`, `governance_document`,
`document` (the attached file's metadata and sha256) and `written_to_disk`.

**It refuses a quarter that is not closed**, and names everything missing in one
reply:

| Precondition | Why it is a precondition |
| --- | --- |
| The quarter has ended | there is no such thing as a report on a quarter that is still happening |
| The custodian's statement is filed as a **Prior Statement** with an effective date inside it | a report written before the statement arrived is a report written from a guess |
| No journal entry touching the investment accounts is still a draft | an account that reconciles today and will not once three drafts post is not reconciled, it is about to not be |
| No bank transaction in the period is unreconciled | the same argument, from the other side of the ledger |

**It invents nothing.** Without `benchmark_rate_percent` the return over
benchmark and the performance fee are **not computed** and say so — they are not
zero and not estimated, because a performance fee against an assumed benchmark of
nothing overstates what the manager is owed. `high_water_mark` caps the
fee-eligible gain, and closing assets at or below it earn nothing however the
quarter went. `net_contributions` defaults to zero and the report says that is an
assumption.

**Holdings come from the caller.** This app reads one ERPNext site; the
custodian's positions are not on it. Pass `holdings` — a list of objects with
`symbol`, `description`, `quantity`, `price`, `market_value`, `cost_basis` — and
the report reconciles the snapshot against the ledger and reports the variance.
Omit it and assets under management are the ledger balance, stated as such.

The investment accounts are matched by name off the company's own chart and
**listed in the report**, or named explicitly; a chart with no match is refused
rather than guessed at.

**PDF is the default and the right answer.** `docx` exists for a report that has
to be edited before signing; a `.docx` is a file the recipient may not be able to
open.

---

## 92. `generate_1099_prefill`

**MUTATING**, default OFF (`allow_generate_1099_prefill`).

**Arguments:** `tax_year` (required), `company`, `threshold` (default 600),
`output_path` (a **directory**), `overwrite`, `payer_address`, `include_forms`
(default `true`), `title`, `dry_run`.

**Returns** `recipients`, `exempt_above_threshold`, `below_threshold`,
`total_box_1`, `related_party_recipients`, `excluded`, `basis`,
`governance_document`, `workbook` and `forms`.

**It is a pre-fill.** Recipient taxpayer ids print as `XXX-XX-nnnn`, because this
site holds four digits on purpose. Copy A must be the official scannable red-ink
form or an electronic filing; the Copy A page here is stamped as an information
copy. Copies B and C print on plain paper and are the ones that go out.

**Classification is never silent.** Every recipient is `reportable`, `exempt` or
`borderline` with the reason in a sentence:

| Signal | Verdict |
| --- | --- |
| Related Party says Individual, Partnership, Family Member or Trust | reportable |
| Related Party says Corporation | exempt — **unless** the name says law firm, which is borderline |
| Related Party says LLC, or the name does | **borderline** — a disregarded entity is reportable and one taxed as a corporation is not, and only the W-9 says which |
| The name looks like a law firm | **borderline** — attorneys are reportable **even when incorporated**, which is why "ends in PC, skip it" is the wrong rule |
| The name looks governmental | **borderline** — a name is a hint, not a determination |
| Supplier type is Individual / Proprietorship / Partnership | reportable |
| The name ends in a corporate suffix | exempt |
| Nothing on the site says | **borderline**, with the remedy: register it as a Related Party, or read the W-9 |

**Where the money comes from.** GL Entry rows carrying a Supplier party — every
voucher type, and only submitted ones, since cancelled vouchers leave no GL row.
Debits **only** on Payable-type accounts (a debit to payables is a bill being
paid; a credit is one being raised). Debits **minus** credits everywhere else
(the party sits on the expense line, so a credit is a refund). That rule is right
in both bookkeeping styles, and `by_account` shows both sides so the arithmetic
can be checked rather than believed.

**Excluded and said so.** Employees, because that is W-2 territory — and the
count and total of employee-party postings is reported anyway, so "nobody looked"
and "somebody looked and excluded them" are different-looking answers. Opening
entries. Anything under the threshold, listed with its total so a case near the
line is visible rather than absent.

Refuses a tax year that has not ended, naming the earliest date it could run.

```
1099-NEC pre-fill for Orchard Meadow LLC 2025: 4 recipient(s), 29,485.00 in Box
1, filed as GD-00214
```

---

## 93. `list_companies`

Read-only, default ON (`allow_list_companies`).

**Arguments:** `limit`.

**Returns** `companies`, `company_count`, `truncated` and `party_types`. Each
company carries `abbr`, `default_currency`, `country`, `parent_company`,
`is_group`, `chart_of_accounts`, `default_cost_center`, `tax_id_on_file`,
`tax_id_last4`, `fiscal_year_start_month`, `fiscal_year_first`,
`fiscal_year_last`, `fiscal_year_count`, `cost_center_count`, `account_count`,
`gl_entry_count`, `first_gl_entry`, `last_gl_entry` and
`pest_management_providers`.

**The GL counts are the point on a multi-company site.** A company with no
postings can still have its currency changed; one with postings cannot, and this
is where you find out which you are looking at.

**`pest_management_providers` is a table because one consultant is the
exception.** A farm running pome fruit and stone fruit commonly retains a
different adviser for each, and a single Link would hold whichever was typed last
while reading as the whole answer. Each row names a `provider` (a Supplier), the
`commodity` it covers, the `service_type` and a `license_number`; a row with **no
commodity** covers the whole operation and says so in `commodity_scope` rather
than reading as an unanswered question. Where the column has not been installed,
`pest_management_providers_installed` is `false` and no company is claimed to
have none.

`party_types` reports whether this app's `Family` and `Contact` Party Types are
registered on the site, with a hint naming the fix when they are not — because
"can I book a Journal Entry line to a family member" is exactly the question a
client calls this tool to answer.

Never returns more than four digits of a tax id.

---

## 94. `create_company`

**MUTATING**, default OFF (`allow_create_company`).

**Arguments:** `company_name` (required), `abbr` (required), `country` (default
`United States`), `default_currency` (default `USD`), `fiscal_year_start_month`
(1-12 or a month name, default 1), `tax_id`, `parent_company`,
`chart_of_accounts`, `notes`, `dry_run` (default `false`).

**Returns** the plan it worked from, `created`, `name`, `account_count`,
`cost_center_count`, `default_cost_center`, `chart_of_accounts`, the
`fiscal_year` it created or found, and `warnings`.

ERPNext's own Company controller builds the chart, the root cost centers and the
defaults on insert. This tool's job is to hand it correct arguments and then
report **what it actually got** — an `account_count` of zero means the named
chart does not exist on this site, and the result says so rather than looking
like a success.

It creates the fiscal year containing today for the start month given **and the
one before it**. April (4) is a farm year and is named for the span it covers
(`2026-2027`); January (1) is a calendar year and is named `2026`. Two years
because a company stood up in March is one whose first task is often last year's
closing balances, and an opening-balance journal entry with no fiscal year to
land in is refused by ERPNext with a message about a period that does not exist.
Years that already exist are left alone and reported as such.

`chart_of_accounts` defaults to `Standard with Numbers` — numbered because this
app resolves accounts by number as well as by name, and an unnumbered chart makes
`resolve_account("1100")` impossible on a brand-new company.

The result carries the `cost_center_tree`, `fiscal_years_created`, and a
`next_step` pointing at **56** — ERPNext books to a company's default account
fields without asking, and one whose defaults are empty fails at the first
invoice rather than here.

| Refusal | Why |
| --- | --- |
| A duplicate company name | it is the docname |
| A duplicate abbreviation | every account, cost center, parcel and lease docname ends in it; two companies sharing one makes those ambiguous |
| A non-alphanumeric abbreviation | it becomes the tail of a docname |
| A `country` or `default_currency` this site does not have | ERPNext ships the ISO lists, so this is a spelling — `United States`, not `USA` |
| A `parent_company` that is not a group | nothing can consolidate under a non-group company |
| A month outside 1-12, or an unparseable month name | refused rather than defaulted |
| An `abbr` outside 2-5 characters | one is not an abbreviation and collides immediately; past five, every account docname carries it |
| An `abbr` already used by docnames with no company behind them | a chart left behind by a deleted company. A new company reusing it would inherit docnames that look like its own and are not |
| A `chart_of_accounts` template this site does not offer | only checked where ERPNext's own template list is importable. A template it cannot find produces a company with no accounts, which looks like a success and is not |

```
create_company {"company_name": "Constancy Farms LLC", "abbr": "CF",
                "fiscal_year_start_month": 4}
→ created company Constancy Farms LLC (CF), 68 accounts, 3 cost centers,
  fiscal year 2026-2027
```

---

## 95. `update_company`

**MUTATING**, default OFF (`allow_update_company`).

**Arguments:** `company` (required — docname or abbreviation), `country`,
`tax_id`, `notes`, `company_logo`, `pest_management_providers`,
`default_currency`.

**The consultants table is replaced wholesale** when passed and left alone when
omitted; `[]` is how it is genuinely cleared. The WHOLE list is validated before
any of it is written — an unknown Supplier, an unknown Crop, an unrecognised key
or the same consultant named twice for one commodity refuses the lot, because a
half-written table leaves a company with some of its advisers and no way to tell
which half went.

**Returns** `changed` (each as `[before, after]`, with `tax_id` redacted to
`…nnnn`), `unchanged`, `tax_id_on_file`, `tax_id_last4` and the GL facts.

| Refusal | Why |
| --- | --- |
| `abbr` | it is the tail of every account, cost center, parcel and lease docname on these books. Changing it renames thousands of documents, which is a migration rather than an edit |
| `company_name` | it **is** the docname, and every document links to it by that name |
| `default_currency` **once anything is posted** | every one of those entries was measured in the old one; relabelling it restates the whole ledger without touching a number. The refusal names the entry count and the date range |
| `fiscal_year_start_month` | a year that changes shape mid-cycle produces two periods claiming the same days. A short year created deliberately with **72** is how that is done |

The currency rule is about the **ledger**, not the field: a company with no
postings can still have its currency corrected, which is the first-day case a
blanket refusal would have blocked.

---

## 96. `register_party_types`

**MUTATING**, default OFF (`allow_register_party_types`). Idempotent.

**Arguments:** `dry_run` (default `false`).

**Returns** `created`, `already_registered`, and `party_types` with the
`account_type` and the reason each one exists.

Registers `Family` and `Contact` as real Party Type records so a Journal Entry
line can carry them. Both settle against a **Payable** account, because both are
payees.

**Why these two.** ERPNext ships Customer, Supplier, Employee and Shareholder,
and a family operation pays two kinds of people that fit none of them:

- **`Family`** — a relative receiving money that is neither payroll nor a
  purchase. **92** excludes those postings and reports the count, total and
  names. A transfer below the IRS annual gift exclusion is not compensation for
  services: no W-9, no form. Recording them as Suppliers puts family money into
  vendor spend *and* onto a 1099 the recipient owes no tax on.
- **`Contact`** — the occasional consultant who is not a formal Supplier but IS
  paid for services. **92** reads those and classifies them **borderline**,
  naming the W-9, rather than dropping them.

**A PARTY TYPE'S NAME HAS TO BE A DOCTYPE.** `Party Type` names itself
`field:party_type`, and that field is a `Link` to `DocType`. A Journal Entry line
then carries `party`, a **`Dynamic Link`** resolved through `party_type`. So
`party_type = "Family"` needs a DocType called `Family`, and the party has to be
a record in it. `Contact` resolves to Frappe's own Contact DocType; `Family`
resolves to the register this app ships.

`resolves_to_doctype` in the result says which, and a party type whose DocType is
missing comes back under `skipped` with the reason rather than taking the call
down — the same behaviour the migrate patch has, and for the same reason.

They are also seeded on install and on every `bench migrate`; this tool is for a
site that cannot be migrated right now. **It changes nothing that already
exists** — every rule and Journal Entry using Shareholder, Employee or Supplier
keeps working exactly as it did.

---

## 97. `list_fields`

Read-only, default ON (`allow_list_fields`).

**Arguments:** `owning_entity` (or `company`), `parcel`, `county`, `crop`,
`variety`, `condition`, `food_safety_zone` (boolean), `organic_status`,
`organic_certified` (boolean), `linked_to_cost_center` (boolean), `limit`.

**Returns** `fields`, `field_count`, `total_acreage`, `average_acreage`,
`oldest_planting_year`, `newest_planting_year`, `by_variety`,
`known_varieties`, `without_acreage`, `spray_dates_from_farm_precision_ag`,
`organic_certified_acreage`, `organic_transitional_acreage`,
`acreage_by_organic_status`, `without_organic_status`, `counties` and
`acreage_by_county`.

**Certified acres are summed from the BLOCK, not from the crop.** Certification
attaches to ground. A farm running one variety on eight certified blocks and
twelve conventional ones has one `Crop` record and one boolean, and "total acres
organically certified" cannot be got out of it — which is why
`Crop.is_organic_certified` exists and does not answer this. `organic_status` is
`Conventional` / `Transitional` / `Certified Organic` rather than a checkbox,
because the three years of transition are the part a buyer and an inspector both
ask about, and a checkbox records the end state and loses them. A block with no
status is listed in `without_organic_status` rather than counted as conventional:
blank means nobody has answered.

**County is read through the parcel and stored on no block.** Every row carries
`county` from its parcel; `acreage_by_county` and `counties` roll it up, which is
"which counties do you operate in" as a query. Leased ground counts, because a
block's county is the county of the ground it sits on. A `county` filter is
resolved to that county's parcels, and a county this site holds no ground in is
refused **with the counties it does** — a filter that quietly matched nothing
reads as "we farm no ground there", and one that quietly matched everything reads
as "we farm all of it".

**`known_varieties` is the autosuggest.** It is what is already planted on this
site. A hardcoded list would be wrong the first time somebody puts a new variety
in the ground; what is already there cannot be.

**`last_spray_date` comes from two places and says which.** What is recorded on
the Field, and — where `farm_precision_ag` is installed — the newest Spray Log
against it. The later of the two is `last_spray_date`, with `last_spray_source`
naming where it came from, and both raw values are returned as
`last_spray_date_recorded` and `last_spray_date_observed` so they can be compared
rather than believed.

---

## 98. `get_field`

Read-only, default ON (`allow_get_field`).

**Arguments:** `field` (required — a docname like `Yellow Camp Block 3 - MC`, or
just `Yellow Camp Block 3`), `parcel`, `owning_entity`.

**Returns** the block, its `county`, `parcel_detail`, `zone_count`,
`zone_acreage`, `unzoned_acreage`, `water_rights` and every `zone` over it.

`county` and `organic_certified` are both derived and neither is stored where it
is read: the county comes from the parcel, the flag from `organic_status`. Two
copies of either would be two answers, and the wrong one is always the copy
nobody edited.

A bare field name matching blocks on two parcels is refused with both named
rather than resolved to whichever came first; `parcel` narrows it.

---

## 99. `create_field`

**MUTATING**, default OFF (`allow_create_field`).

**Arguments:** `parcel` (required), `field_name` (required), `owning_entity` (or
`company`), `acreage`, `crop` (default `Cherry`), `variety`, `rootstock`,
`planting_year`, `planting_density_per_acre`, `condition`, `block_number`,
`external_farm_app_id`, `last_spray_date`, `water_test_last_date`,
`wildlife_intrusion_last_report`, `food_safety_zone`,
`worker_hygiene_station_present`, `organic_status`, `organic_cert_agency`,
`transition_start_date`, `notes`.

**The docname is `<field_name> - <parcel abbr>`**, so every parcel may have a
"Block 3". The parcel's abbreviation is its `abbr`, or initials derived from its
name when it has none.

**The food-safety fields are part of the block, not a separate log.**
`last_spray_date` answers the re-entry interval question a crew is waiting at
the gate for before it answers a WPS report;
`worker_hygiene_station_present` decides whether a crew may work the block at
all.

| Refusal | Why |
| --- | --- |
| A second block with the same name on one parcel | the docname is built from it |
| A `external_farm_app_id` already on another block | that id is the other system's primary key; two of them makes the sync bridge ambiguous |
| Negative acreage or planting density | not opinions |
| Blocks whose acreage would sum to **more than the parcel** | two numbers that cannot both be true. Named with the parcel's acreage, the total and the excess |

Blocks summing to *less* than the parcel is the normal case and is left alone —
roads, ditches, headlands and the house are all real.

**Organic certification is set here because it attaches to ground.**
`organic_certified` is DERIVED from `organic_status` on every save and passing it
is refused — a derived flag a person can set independently is a flag that will
disagree with the status it came from.

**Warns rather than refusing** on no acreage; on a food-safety block with no
hygiene station or no water test; on a certifying agency recorded against a block
whose status is Conventional or unanswered; on Transitional with no transition
start date; and on Certified Organic with no agency. Every one of those is a fact
worth recording precisely because it is a problem, and each is a state some block
is genuinely in — a block mid-application really does have a certifier and no
certificate.

---

## 100. `update_field`

**MUTATING**, default OFF (`allow_update_field`).

**Arguments:** `field` (required), plus any of `acreage`, `crop`, `variety`,
`rootstock`, `planting_year`, `planting_density_per_acre`, `condition`,
`block_number`, `external_farm_app_id`, `last_spray_date`,
`water_test_last_date`, `wildlife_intrusion_last_report`, `food_safety_zone`,
`worker_hygiene_station_present`, `organic_status`, `organic_cert_agency`,
`transition_start_date`, `notes`.

**Returns** the block and `changed`, every one as `[before, after]`.

Cannot rename it (the docname is built from `field_name`, and every zone points
at that docname), cannot move it to another parcel (ground does not move — a
block on the wrong parcel was mis-registered), cannot set `organic_certified`
(derived from `organic_status` on every save, so a value written here is
overwritten by the next one), and cannot set `cost_center` — that is **101**. The parcel acreage rule applies here too. A no-op update is
refused rather than reported as a success.

---

## 101. `link_field_to_cost_center`

**MUTATING**, default OFF (`allow_link_field_to_cost_center`).

**Arguments:** `field` (required), `cost_center` (required — docname, number or
name), `owning_entity`, `replace` (default `false`), `dry_run` (default
`false`).

**Returns** `field`, `cost_center`, `previous_cost_center`, `acreage`,
`shared_with`, `changed` and — when other blocks book to the same cost center —
a `note`.

| Refusal | Why |
| --- | --- |
| A cost center on another company's books | a cost allocated across two companies is an intercompany transaction, not a dimension |
| A group cost center | ERPNext will not let a posting land on one |
| A disabled cost center | nothing can book to it |
| Repointing a block that is already linked, without `replace=true` | this season's costs and last season's would land in different places |

**Reports rather than refuses** when other blocks already book there. A cost
center per orchard is a legitimate design; it just is not per-block costing, and
the result says which one you have.

---

## 102. `get_parcel_field_summary`

Read-only, default ON (`allow_get_parcel_field_summary`).

**Arguments:** `parcel` (required), `owning_entity`.

**Returns** `field_count`, `planted_acreage`, `parcel_acreage`,
`unassigned_acreage`, `average_field_acreage`, `zone_count`, `zoned_acreage`,
`average_zones_per_field`, `total_flow_gpm`, `oldest_planting_year`,
`newest_planting_year`, `by_condition`, `by_variety`, `water_rights`,
`food_safety_blocks`, `blocks_without_hygiene_station`,
`zones_without_water_test` and a per-block `fields` list.

**`unassigned_acreage` is usually the interesting number.** Blocks summing to
less than the parcel is normal, but a large gap on a parcel somebody thinks is
fully blocked out is a missing Field.

---

## 103. `import_farm_app_fields`

**MUTATING**, default OFF (`allow_import_farm_app_fields`). **Dry run by
default.**

**Arguments:** `records` (required — an array of objects), `parcel` (a default
for records with no `parcel_hint`), `owning_entity`, `apply` (default `false`).

Each record may carry `name` (required), `parcel_hint`, `acreage`, `variety`,
`planting_year`, `block_number` and `farm_app_uuid`. **An unrecognised key is
refused rather than ignored** — a typo silently dropped is a field somebody
thinks they imported.

**Returns** `record_count`, `would_create`, `already_present`, `applied`, the
per-record `plan`, and `created` when applied.

**This is the schema-alignment foundation, not the sync.** It creates ERPNext
Fields carrying `external_farm_app_id` so a later sync engine has something to
match on. It never updates an existing Field, never deletes, and never writes
back to the Farm App.

**The whole batch is validated before the first insert.** A half-imported farm is
worse than an unimported one, because the second run has to work out which half.
A `parcel_hint` matching no Parcel, a batch repeating a name or a `farm_app_uuid`,
a negative acreage — any of those refuses the lot.

A block already registered under that name, or already carrying that Farm App
id, is **skipped** with the reason and the existing docname, so the same batch
re-runs safely.

---

## 104. `list_irrigation_zones`

Read-only, default ON (`allow_list_irrigation_zones`).

**Arguments:** `owning_entity` (or `company`), `field`, `parcel`,
`water_source`, `sprinkler_type`, `water_source_class`, `chlorination_active`
(boolean), `limit`.

**Returns** `zones`, `zone_count`, `total_area_acres`, `total_flow_gpm`,
`by_water_source`, `water_rights`, `without_water_test` and
`surface_water_without_a_right`.

**The last two lists are the report.** A zone with no agricultural water test is
one whose fruit cannot be cleared under FSMA Subpart E; a creek, pond or shared
diversion with no water right is not something Oregon treats as self-evident.

---

## 105. `get_irrigation_zone`

Read-only, default ON (`allow_get_irrigation_zone`).

**Arguments:** `zone` (required — a docname like `YC3-Zone2 - MC`, or just
`YC3-Zone2`), `field`, `owning_entity`.

**Returns** the zone, `field_detail`, `zones_on_this_field`,
`field_acreage_zoned`, `share_of_field` and `compliance_notes` — the gaps in
sentences rather than left to be inferred.

---

## 106. `create_irrigation_zone`

**MUTATING**, default OFF (`allow_create_irrigation_zone`).

**Arguments:** `field` (required), `zone_name` (required), `owning_entity`,
`zone_number`, `water_source`, `water_right_id`, `flow_rate_gpm`,
`sprinkler_type`, `area_sq_ft`, `water_test_last_date`, `water_source_class`,
`chlorination_active`, `notes`.

**The docname is `<zone_name> - <parcel abbr>`** — not the *field's*
abbreviation. A zone name already carries its block (`YC3-Zone2`), and suffixing
it with the block again gives `YC3-Zone2 - YC3`, which says the same thing twice
and drops the ground.

**`area_acres` is computed** from `area_sq_ft` at 43,560 to the acre and cannot
be passed. Two figures a caller sets independently are two figures that will
disagree; passing it is refused with the conversion offered.

| Refusal | Why |
| --- | --- |
| A second zone with the same name on one parcel | the docname is filed under the parcel |
| A `zone_number` already used on that block | that number is what somebody types into the controller at two in the morning; two answers means water goes somewhere nobody chose |
| Negative area or flow | not opinions |
| Zones whose area would sum to **more than the block** | reported in acres and in the square feet you typed |

**Warns rather than refusing** on no area, on surface water with no water right,
and on a zone watering a food-safety block with no water test.

---

## 107. `update_irrigation_zone`

**MUTATING**, default OFF (`allow_update_irrigation_zone`).

**Arguments:** `zone` (required), plus any of `zone_number`, `water_source`,
`water_right_id`, `flow_rate_gpm`, `sprinkler_type`, `area_sq_ft`,
`water_test_last_date`, `water_source_class`, `chlorination_active`, `notes`.

**Returns** the zone and `changed`, every one as `[before, after]`. `area_acres`
is recomputed.

Cannot rename it, cannot move it to another block (pipe does not move), and
cannot set `area_acres`. The block area rule and the zone number rule both apply.

---

## 108. `list_housing_units`

Read-only, default ON (`allow_list_housing_units`).

**Arguments:** `owning_entity` (or `company`), `parcel`, `unit_type`,
`condition`, `or_housing_law_compliant`, `fsma_worker_facility` (boolean),
`limit`.

**Returns** `units` (each with its current `occupants`), `unit_count`,
`residential_unit_count`, `total_capacity`, `currently_assigned`, `open_beds`,
`by_unit_type`, `overdue_inspections`, `uninhabitable`,
`fsma_worker_facilities` and `over_lawful_occupancy`.

**Capacity and lawful occupancy are different questions and both are reported.**
One is how the operation uses the unit; the other is what 50 square feet per
occupant allows. A gap between them is the finding.

Non-residential units — a shower block, a kitchen, a shop — are counted as units
but contribute no capacity. A unit never inspected counts as overdue, which is
the answer that gets somebody to go and look.

---

## 109. `get_housing_unit`

Read-only, default ON (`allow_get_housing_unit`).

**Arguments:** `unit` (required — a docname like `MC-Cabin-01 - MC`, or just
`MC-Cabin-01`), `owning_entity`.

**Returns** the unit, `currently_assigned`, `open_beds`, `assignment_count`,
`current_assignments`, the whole `assignment_history` newest first, and
`compliance_notes`.

The notes name what is missing in sentences: capacity over the lawful occupancy,
no habitability inspection in a year, no smoke or CO detector test on record,
Uninhabitable, subject to FSMA Subpart L.

---

## 110. `create_housing_unit`

**MUTATING**, default OFF (`allow_create_housing_unit`).

**Arguments:** `parcel` (required), `unit_name` (required), `owning_entity`,
`unit_type`, `square_footage`, `capacity`, `year_built`, `condition`,
`related_asset`, `access_card_zone`, `fsma_worker_facility`,
`or_housing_law_compliant`, `max_occupants_per_or_law`,
`last_habitability_inspection`, `smoke_detector_last_test`,
`co_detector_last_test`, `notes`.

**The docname is `<unit_name> - <parcel abbr>`**, so every camp may number its
cabins from one.

**The lawful occupancy is computed** from `square_footage` at 50 sq ft per
occupant — 29 CFR 1910.142(b)(1), which Oregon's agricultural labor housing
rules follow — unless you pass `max_occupants_per_or_law`. It is a **default,
not a derivation**: a cabin with a fixed bunk layout keeps the number somebody
worked out.

| Refusal | Why |
| --- | --- |
| A second unit with the same name on one parcel | every camp numbers its cabins from one, so the name has to be unique inside the parcel |
| An Asset on another company's books | a building and the asset carrying it belong to one set of books |
| An Asset already carrying a different unit | one asset, one building — split the cost first |

**Warns rather than refusing** a `capacity` over 20 outside a Multi-Unit
Building. A twenty-person cabin is barracks by another name, and some of them
really are. Also warns on missing square footage, missing detector tests and a
missing habitability inspection.

`or_housing_law_compliant` defaults to **Unknown**, which is a distinct answer
from No: an operator who has not looked should not be recorded as having found a
violation.

---

## 111. `update_housing_unit`

**MUTATING**, default OFF (`allow_update_housing_unit`).

**Arguments:** `unit` (required), plus any of `unit_type`, `square_footage`,
`capacity`, `year_built`, `condition`, `related_asset`, `access_card_zone`,
`fsma_worker_facility`, `or_housing_law_compliant`, `max_occupants_per_or_law`,
`last_habitability_inspection`, `smoke_detector_last_test`,
`co_detector_last_test`, `notes`.

**Returns** the unit, `changed` as `[before, after]`, and `compliance_notes`.

Changing `square_footage` **recomputes the lawful occupancy only when the stored
limit was itself the computed one**. A figure somebody typed is kept.

Cannot rename it (assignments point at the docname), and cannot move a building
between parcels — even a manufactured home that really was moved should be
re-registered where it stands, so the assignment history stays attached to the
ground it happened on.

---

## 112. `list_housing_assignments`

Read-only, default ON (`allow_list_housing_assignments`).

**Arguments:** `owning_entity` (or `company`), `unit`, `parcel`, `employee`,
`current_only` (**default `true`**), `from_date`, `to_date`, `limit`.

**Returns** `assignments`, `assignment_count`, `currently_assigned`,
`distinct_units`, `distinct_people`, `with_wage_deduction` and
`deposits_outstanding`.

**`with_wage_deduction` is the compliance answer.** ORS 653 and OAR 839-015
constrain deducting housing from wages, and this is where the assignments that
did are named. Pass `current_only=false` with a date range for the historical
roster.

---

## 113. `create_housing_assignment`

**MUTATING**, default OFF (`allow_create_housing_assignment`).

**Arguments:** `unit` (required), `assigned_date` (required), `employee` or
`employee_name` (one of the two required), `end_date`, `owning_entity`,
`deposit_paid`, `deposit_returned`, `housing_deduction_from_wages` (Yes / No /
Unknown), `allow_multi_occupancy` (default `false`), `notes`.

Auto-named `HA-YYYY-MM-<seq>`, sequenced within the month, so a camp's intake
sorts into seasons without a report.

**Returns** the assignment, `unit_capacity`, `occupants_after`,
`multi_occupancy`, `warnings` and a `section_119_note`.

**This record is the audit trail** for an IRS Section 119 exclusion — lodging on
the business premises, for the employer's convenience, required as a condition of
employment. It records the facts; it does not make the determination.

| Refusal | Why |
| --- | --- |
| An overlapping assignment on that unit | usually a typo. Names the assignment already there. Pass `allow_multi_occupancy=true` for a genuine bunk room |
| A unit typed Toilet-Shower, Kitchen, Bath House, Barn or Shop | nobody is assigned to a shower block; if people really sleep there, its type is wrong |
| A unit marked Uninhabitable | change the condition once it has been repaired and inspected, then assign |
| An `employee` not on file, **where an HR app is installed** | a roster naming somebody payroll has never heard of has already drifted |
| An `end_date` before `assigned_date` | nobody moved out before they moved in |
| A `deposit_returned` larger than `deposit_paid` | a refund of money nobody took |

Overlap is **inclusive at both ends**: somebody moving out on the 15th and
somebody moving in on the 15th shared the cabin that night, and a camp manager
told otherwise puts two people in one bed.

**Where no HR app is installed** the employee is stored as text and the tool says
so. A camp roster that cannot be written until an HR module exists is a camp
roster nobody keeps.

---

## 114. `end_housing_assignment`

**MUTATING**, default OFF (`allow_end_housing_assignment`).

**Arguments:** `assignment` (required), `end_date` (required),
`deposit_returned`, `notes` (appended, not replacing), `dry_run` (default
`false`).

**Returns** the assignment, `changed` and `warnings`.

**It never deletes.** An assignment removed when the person leaves cannot defend
a Section 119 classification, cannot answer a wage claim about a housing
deduction, and cannot tell an investigator who was in the camp the week in
question.

| Refusal | Why |
| --- | --- |
| An assignment that has already ended | re-dating a departure is a correction, not a close. The refusal names the date on record |
| An `end_date` before the start | nobody moved out before they moved in |
| A `deposit_returned` larger than the one on record as paid | a refund of money nobody took |

A deposit still held is **reported**, so it is either refunded or explained.

---

## 115. `get_housing_capacity`

Read-only, default ON (`allow_get_housing_capacity`).

**Arguments:** `owning_entity` (or `company`), `parcel`.

**Returns** `parcel_count`, `unit_count`, `total_capacity`,
`total_lawful_capacity`, `currently_assigned`, `open_beds`,
`overdue_inspection_count`, a `by_parcel` breakdown, `inspection_window_days`
and a plain `readout` — one sentence per parcel.

```
get_housing_capacity {}
→ Mill Creek - ETC: 25 residential units, capacity 100, currently 87 assigned,
  13 open. Overdue habitability inspections: 3.
```

Non-residential units are counted but contribute no capacity: a bath house and a
shop are part of the camp, nobody sleeps in them, and adding their zero capacity
to the total would make the register look thinner than it is.

---

## 116. `get_employee_housing_history`

Read-only, default ON (`allow_get_employee_housing_history`).

**Arguments:** `employee` (required — an Employee id, or the person's name as
the roster has it).

**Returns** `assignment_count`, `currently_assigned`, `units_lived_in`,
`first_assigned`, `last_assigned`, `deposits_paid`, `deposits_returned`,
`deposits_outstanding`, `wage_deduction_taken`, every `assignment`, and a plain
`readout`.

```
get_employee_housing_history {"employee": "Antony"}
→ Antony assigned MC-Cabin-12 - MC 2026-06-01 → 2026-07-15
  Antony is currently unassigned.
```

Matches on the employee id first and then on the name, because a site with no HR
app records the name and a site with one records the id.

---

## `update_farm_location`

**MUTATING**, default OFF (`allow_update_farm_location`).

**Arguments:** `doctype` (required — `Field`, `Irrigation Zone`, `Parcel` or
`Housing Unit`; `register` is an alias), `name` (required — the docname or the
name somebody typed, both resolve), `owning_entity` (alias `company`), plus any
of `acres`, `crop`, `variety`, `block_number`, `condition`, `county`, `state`,
`address`, `unit_type`, `capacity`, `water_source`, `flow_rate_gpm`, `notes`.

**Returns** whatever the register's own `update_` tool returns — the record and
`changed`, every entry as before → after — plus `doctype`, `location`,
`option` (the picker row, re-read after the save) and `arguments_mapped`, which
names the column each argument actually landed in.

**The register's own tool does the write.** `update_field`,
`update_irrigation_zone`, `update_parcel` and `update_housing_unit` each run with
every refusal they have always made: the parcel acreage rule, the zone number
already used on that block, the block's zones summing past its acreage, the
derived `organic_certified`, the GPS pair that moves together. This resolves the
register and maps thirteen argument names onto four vocabularies; it relaxes
nothing.

**A column the named register does not have is refused by name**, with the
registers that do take it named beside it. `capacity` on a block and `crop` on a
cabin are both somebody working from the wrong screen, and a silent drop is how
they come to believe they recorded it.

**`acres` on an Irrigation Zone becomes square feet.** That register computes
`area_acres` from `area_sq_ft` and refuses the former by name, so this converts
rather than setting a second figure that would disagree with the first.

**Cannot rename anything.** All four registers build the docname from the name
column and all four tools refuse to re-key, because every zone, assignment, task
and filed record holds that docname. `name` identifies the record here.

---

## `delete_farm_location`

**MUTATING**, default OFF (`allow_delete_farm_location`). **Irreversible.**

**Arguments:** `doctype` (required), `name` (required), `owning_entity` (alias
`company`), `dry_run` (default `false`), and `force_check_children`,
`force_check_references`, `force_check_activity`, `force_check_attachments` (all
default `true`).

**Returns** `deleted`, `location_row` (the picker row as it was), `checks_passed`,
`checks_skipped`, `found` (per failed check: the referring doctype, the column,
the count and up to eight examples) and a `note`.

**What this is for.** The duplicate — a block typed twice at six in the morning,
one of the two never used, sitting in every picker on the farm because nothing in
this app has ever removed a register row. A place with any history is the other
case and is refused: keep it, because a place with history is what the register
is for. There is no `disabled` column on any of the four to hide a row behind,
which is why the checks are strict rather than advisory.

| Check | Refuses when |
| --- | --- |
| `children` | a register row hangs off it — a Parcel holds blocks, zones and cabins; a Field holds zones. An Irrigation Zone and a Housing Unit are leaves |
| `references` | anything names it with a plain Link: scale tickets, lot codes, cost and revenue entries, biological assets, water tests, bin seals, leases, housing assignments, detector tests, inspections |
| `activity` | anything names it through a **dynamic** link: Farm Task, Spray Application Block, Spray REI, Crop Observation, Pest Pressure, IPM Recommendation, Inspection Session, Accident Report |
| `attachments` | a File is attached to it. A File names its parent by docname rather than by link, so nothing else would refuse |

**The `activity` check is the one that matters.** The other three are Links
Frappe's own integrity check would have refused the delete over anyway; a dynamic
link is two plain columns to a database. Turning off `force_check_activity` is
the only flag that genuinely removes a protection rather than changing which
error you get.

**`dry_run=true` runs all four and deletes nothing.** Make that call first — it
answers "why can I not remove this" without a failed write.

---

## 117. `set_field_boundary`

**MUTATING**, default OFF (`allow_set_field_boundary`). Needs `shapely` and `h3`.

**Arguments:** `field` (required), `boundary_geojson` (required),
`owning_entity`, `dry_run` (default `false`).

**Returns** `area_computed_acres`, `acreage_recorded`,
`area_disagreement_ratio`, `boundary_centroid`, `boundary_bbox_geojson`,
`h3_cell_counts`, `h3_resolutions`, `zones_outside_boundary`, `warnings` and
`changed`.

The polygon may arrive as a bare geometry, a Feature, or a FeatureCollection
holding exactly one Feature — whichever your export button produced. Coordinates
are `[longitude, latitude]` in degrees, as GeoJSON specifies.

**Everything else is derived and none of it can be set directly.** Centroid,
bounding box, H3 coverage at resolutions 6–10, and the area the polygon
encloses are all functions of the shape. A field a caller could edit
independently is one that will disagree with the polygon, and the disagreement
surfaces as a geofence saying no to somebody standing in the right place.

| Refusal | Why |
| --- | --- |
| Not valid JSON, or not a GeoJSON object | reported with the parser's own message |
| A Point, LineString or GeometryCollection | a boundary has to be an area |
| A ring that is not closed | a boundary that does not come back to itself does not enclose anything |
| A ring with fewer than four positions | a closed ring needs at least four, because the first and last are the same point |
| Coordinates off Earth | a latitude past 90 usually means the pair is the wrong way round, and the refusal says so |
| A self-intersecting polygon | a bow tie has an area a computer will report and a containment test nobody can trust. It is what two swapped vertices produce |
| An area more than **25%** from the recorded acreage | at that point one of the two is about a different piece of ground |

**Warns rather than refusing:** a 5–25% area difference (a deed, a GIS trace and
a tape measure routinely disagree, and both figures are kept); a shape spanning
more than a degree of latitude or longitude — about seventy miles, which is a
county rather than a block; coordinates at `[0, 0]`, which is what an unset
coordinate looks like; zones on this block that now fall outside it; and — from
v0.32.0 — a block that hangs over its **parcel's** boundary.
`boundary_contained_in_parcel` comes back `true`, `false`, or `null` where the
parcel has no shape of its own to check against. From v0.12.0 to v0.31.0 this
warning was unconditional and said a parcel had no boundary at all;
**118a** is the tool that gave it one.

```
set_field_boundary {"field": "Yellow Camp Block 3", "boundary_geojson": "{...}"}
→ Yellow Camp Block 3 - MC: boundary set, 25.7089 acres,
  centroid 45.6015,-121.178
```

---

## 118. `set_zone_boundary`

**MUTATING**, default OFF (`allow_set_zone_boundary`). Needs `shapely` and `h3`.

**Arguments:** `zone` (required), `boundary_geojson` (required),
`owning_entity`, `dry_run` (default `false`).

**Returns** everything **117** returns, plus `boundary_contained_in_field`.

**Containment is reported, never enforced.** The obvious rule is that a zone must
sit inside the field it waters, and it is wrong often enough to matter — a shared
water line crosses a boundary, a pump house sits on the headland, a mainline runs
down a road easement. Refusing those would make them unrecordable, so:

| `boundary_contained_in_field` | Means |
| --- | --- |
| `true` | the zone is wholly inside its block |
| `false` | it is not, which is allowed and warned about |
| `null` | the block has no boundary of its own, so nothing could be checked |

That last row matters: "we could not check" and "we checked and it is outside"
are different answers, and reporting the first as the second is a lie a report
would repeat.

The area comparison is against the zone's own acreage, which is computed from its
square footage — so a polygon and a design drawing disagreeing by a quarter means
one of them is a different zone.

---

## 118a. `set_parcel_boundary`

**MUTATING**, default OFF (`allow_set_parcel_boundary`). Needs `shapely` and
`h3`. v0.32.0.

**Arguments:** `parcel` (required), `boundary_geojson` (required),
`owning_entity`, `dry_run` (default `false`).

**Returns** everything **117** returns, with `outside_boundary` in place of
`zones_outside_boundary`.

**The outer shape — the one the deed and the tax bill both describe.** A parcel
is the unit the county assessor, the deed and the appraisal all agree on, which
is why the register is keyed on it; from v0.32.0 it is also the unit that carries
an outline. Everything registered on the parcel is expected to sit inside it, and
this is the tool that says which things do not.

`outside_boundary` is `{doctype: [names]}` over the three registers that hang off
a parcel:

| Register | Compared how |
| --- | --- |
| `Field` | polygon inside polygon |
| `Irrigation Zone` | polygon inside polygon |
| `Housing Unit` | its `gps_latitude` / `gps_longitude` inside the outline (v0.32.0 gave it coordinates) |

**Only things that have a position are tested.** A block with no polygon and a
cabin with no coordinates are not outside the parcel — they are *unmapped*, which
is a different answer. Listing them as violations would bury the two names that
mean something under fifty that do not.

**Containment is reported, never enforced**, and it matters more here than
anywhere else this app checks it: a planting that predates a deed split really
does straddle the line, and a cabin on the far side of a road easement is a real
cabin. Refusing those would make them unrecordable.

Refuses everything **117** refuses, comparing the polygon's area against the
parcel's own deeded or GIS acreage. On a parcel that recorded figure is usually
the one to trust, and the refusal message says so.

```
set_parcel_boundary {"parcel": "Mill Creek", "boundary_geojson": "{...}"}
→ Mill Creek - ETC: boundary set, 329.9367 acres,
  centroid 45.6005,-121.178
```

---

## 119. `find_fields_containing_point`

Read-only, default ON (`allow_find_fields_containing_point`). Needs `shapely`
and `h3`.

**Arguments:** `lat` (required), `lon` (required), `owning_entity`.

**Returns** `match_count`, the matching `fields` in full, the point's own
`h3_cells` at every stored resolution, `searched`, `candidates_after_bbox`,
`fields_without_a_boundary`, `boundary_inclusive` and a `note`.

**This is the geofence query.** "Is this pick inside an assigned block?" "Is this
worker on ground they are rostered to?"

**Bounding box first, then point-in-polygon exactly.** The prefilter is the
bounding box rather than the H3 index, and that is deliberate: a bbox is a
guaranteed superset of the shape it bounds, so a candidate set built from it
cannot miss the right answer. `candidates_after_bbox` reports how many survived
the cut, and the exact test settles every one of them.

**The boundary counts as inside.** A pick recorded on the edge of a block is in
the block; a geofence that excludes its own boundary tells a picker standing on
the headland that they are nowhere.

**`fields_without_a_boundary` is not decoration.** On a half-mapped farm an empty
result means "not inside any *mapped* block", not "not on the farm", and those
are different things to act on. The failure this guards against is the quiet one:
a geofence saying no because the ground was never traced, read as a policy
decision.

```
find_fields_containing_point {"lat": 45.6015, "lon": -121.1780}
→ [45.6015, -121.178] is inside 1 block(s): Yellow Camp Block 3 - MC
```

---

## 120. `find_fields_by_h3_cell`

Read-only, default ON (`allow_find_fields_by_h3_cell`). Needs `shapely` and `h3`.

**Arguments:** `cell` (required — an H3 index at any resolution),
`owning_entity`.

**Returns** `cell_resolution`, `matched_at_resolution`, `probe_cell`,
`stored_resolutions`, `match_count`, the matching `fields`, `searched` and a
`note`.

The spatial-index query, for joining against anything else keyed on H3 — a bucket
log, a crew track, a weather grid.

**Stored cells are every cell the shape TOUCHES**, not every cell whose centre is
inside it. H3's default polygon fill is centre-based, and an orchard block is
smaller than one cell at resolutions 6 through 8 — so the default returns an
empty set for most fields, and an index built on it would answer "in no field"
for a point plainly in one. The fill uses `contain="overlap"` instead, which is a
true superset.

Resolution handling, and the result says which was used:

| Query resolution | How it matches |
| --- | --- |
| 6–10 | directly against the stored cells at that resolution |
| finer than 10 | rolled up to 10, then matched |
| coarser than 6 | each block's resolution-6 cells are rolled up to the query's resolution and compared there |

**A match means the cell touches the block**, not that everything in the cell is
inside it. Use **119** when the question is about a specific position.

---

## 121. `import_field_boundary_geojson`

**MUTATING**, default OFF (`allow_import_field_boundary_geojson`). **Dry run by
default.** Needs `shapely` and `h3`.

**Arguments:** `feature_collection` (required — a GeoJSON FeatureCollection, as
an object or a JSON string), `parcel` (a default for features with no
`parcel_hint`), `owning_entity`, `apply` (default `false`).

Each Feature's `properties` needs `field_name`, and `parcel_hint` unless a
default `parcel` is given.

**Returns** `feature_count`, `would_set`, `skipped`, the per-feature `results`,
and `set` / `failed` when applied.

**Per-feature, not whole-batch — the opposite of 103, on purpose.**
`import_farm_app_fields` CREATES records, so a half-run leaves a farm somebody
has to reconcile and it refuses the whole batch on the first bad record. This one
only sets a field on records that already exist, so one bad feature in forty is a
bad feature: naming it and applying the other thirty-nine beats refusing the lot.

**It never creates a Field.** A feature naming a block that is not registered is
skipped with that said — register it first with **99** or **103**.

Every per-feature refusal **117** makes applies here too, including the 25% area
rule, and each is reported against its own feature index so a malformed
collection can be fixed one line at a time.

---

## 122. `list_family_members`

Read-only, default ON (`allow_list_family_members`).

**Arguments:** `active` (boolean), `relationship`, `related_to`, `limit`.

**Returns** `members`, `member_count`, `active_count`, `by_relationship`,
`with_related_party`, `without_related_party`, `without_relationship`,
`without_related_to`, `related_to_free_text` and a `note`. Every row carries
`described_as` — `"Alexander Polehn — Son of Tim Polehn"` — which is the sentence
this register exists to be able to say.

**The lists at the end are the point.** A missing related-party entry is not
a gap for most of these — a relative who only receives transfers needs no W-9 and
no disclosure. It IS a gap for one who also holds a role: a member, a lessor, a
trustee. A list that read as forty problems would be a list nobody acts on.

`without_related_to` is a different question: not "do they have a tax identity"
but "whose relative are they". Records written before v0.13.0 have no
`related_to`, **nothing backfilled them and nothing will** — which of two members
somebody is the child of is a fact only the family has — so they are listed and
warned about rather than guessed at.

---

## 123. `get_family_member`

Read-only, default ON (`allow_get_family_member`).

**Arguments:** `family_name` (required — the person's name, which is the
docname).

**Returns** the member, `related_party_detail`, `relationship_chain`,
`relationship_path`, and **every posting that names them**: `posting_count`,
`first_posting`, `last_posting`, `net_amount`, `companies`. Plus
`compliance_notes`.

**The chain crosses two registers to answer one question.** `related_to` goes to
another *person* and is followed as far as it goes; `related_party` goes to the
*same* person's entry in the register that holds roles and entities, and is
followed once, at the top. That is how `relationship_path` reads
`Alex → Son of Tim → Manager of Orchard Meadow, LLC`, which no single record
holds. It terminates on a cycle, on a depth limit, or on free text, and the last
entry says which in `chain_ends_because`.

The postings are read from the GL rather than kept on the record, so the count
cannot drift from what actually happened — which is the entire value of it. "We
moved money to Alex eleven times last year" is the question a family petty-cash
arrangement gets asked, and it has one true answer.

Never returns more than four digits of a taxpayer id, even from the linked
related-party record — the same rule **88** keeps, kept here because this is a
second door onto the same field.

---

## 124. `create_family_member`

**MUTATING**, default OFF (`allow_create_family_member`).

**Arguments:** `family_name` (required), `relationship`, `related_to`,
`related_party`, `active` (default true), `notes`.

`relationship` takes **Son** and **Daughter** as well as Child, Spouse, Parent,
Sibling, Grandchild, Grandparent, In-Law and Other. Son and Daughter arrived in
v0.13.0 *beside* Child rather than instead of it: records already saying Child
are still true, and asking somebody to re-pick a value that has not changed is
work with no answer at the end of it.

**WHY THE REGISTER HAS TO EXIST.** ERPNext resolves a posting's counterparty as a
Dynamic Link THROUGH its party type: `party_type` is a `Link` to `DocType`, so
`Family` only works because this app ships a Family DocType, and `party` only
works if the person is a record in it. Customer, Supplier, Employee and
Shareholder each have one.

**IT HOLDS NO TAX ID, ON PURPOSE.** A transfer below the IRS annual gift
exclusion is not compensation for services: no W-9, no 1099, which is the whole
reason this party type is separate from Supplier. A relative genuinely paid for
work is a Contact or a Supplier, and the posting should say so rather than the
exclusion being widened. Where a relative ALSO holds a role worth disclosing,
`related_party` points at the register that keeps four digits and never more.

**`related_to` ANSWERS "OF WHOM".** A register that says "Alexander Polehn —
Son" and cannot say whose son is ambiguous the moment an entity has two members.
Pass the other person's name: a Family docname, a Related Party docname or party
name, or — for somebody in neither register — their name as plain text. The
result reports which register answered as `related_to_doctype`, and `None` there
means free text rather than a failure.

It is a `Data` field rather than a `Link` on purpose: a Frappe `Link` points at
exactly one doctype, a `Dynamic Link` needs a discriminator column beside it, and
the answer here is one of three kinds of thing. Resolution happens on read.

| Case | What to do |
| --- | --- |
| Simple — Alex is Tim's son | `related_to="Tim Polehn"` |
| Complex — Alex is Tim's son AND Donella's grandson | `related_to="Tim Polehn"`, and `"also grandson of Donella Polehn"` in `notes` |
| Genuine genealogy | not this field. A child table of relationships would turn a register whose job is to make a posting resolve into a genealogy database; if it is ever really needed it is a Family Tree doctype of its own, and this field would point into it |

| Refusal | Why |
| --- | --- |
| A second record for the same name | the name is the docname, and it is what every posting points at |
| A `related_party` that does not exist | register it with **89** first, or leave it blank |
| An unknown `relationship` | the options are read off the DocType |
| Somebody related to themselves | a cycle of length one |

**Warns rather than refusing** when no relationship is given: "why did money go
to this person" is the first question these postings get asked, and a name alone
does not answer it. Same for no `related_to` — the result says "unassigned
parent" and the record is still created.

---

## 125. `update_family_member`

**MUTATING**, default OFF (`allow_update_family_member`).

**Arguments:** `family_name` (required), plus any of `relationship`,
`related_to`, `related_party`, `active`, `notes`. An empty string clears
`related_to`.

**Returns** the member and `changed`, every one as `[before, after]`.

**This is where an existing record acquires `related_to`.** Nothing backfilled it
on upgrade and nothing will: a migration that guessed which member somebody is
the child of would produce a register that looks complete and is wrong.

**Cannot rename them.** The name IS the docname and every journal entry that
named them points at it; renaming would orphan those postings.

**Retiring somebody is `active=false`, not a delete**, and the result reports how
many postings would have been orphaned — which is the argument for the flag
existing.

---

## 126. `update_journal_entry_party`

**MUTATING**, default OFF (`allow_update_journal_entry_party`).

**Arguments:** `journal_entry`, `line_index` (1-based, the way ERPNext numbers
them), `party_type`, `party`, `reason` — all required — plus
`allow_non_party_account` and `dry_run`.

**Returns** `updated`, `before` and `after` as `{party_type, party}`,
`line_index`, `line_name`, `account`, `debit`, `credit`, `gl_entries_updated`,
`comment_added`, `tables`, and a `note`.

**The case it is for.** A payment leaves a shared account and only afterwards
does anybody establish which of two sons it was for. The posting is right — right
account, right amount, right date — and one attribution column is empty or wrong.
The alternatives are cancel-and-repost, which replaces a clerical correction with
a cancelled voucher, a reversing pair and a new number that no statement
reconciles against; or the Desk, which is what an MCP server exists so nobody has
to open.

**It cannot move a balance.** Account, debit, credit, date, cost center and
remark are not arguments to it. The trial balance after the call is
arithmetically identical to the one before, which is what makes editing a
submitted document defensible at all: this is attribution, not restatement. No
journal entry is written and nothing is reversed.

**It writes in both places the party lives.** `tabJournal Entry Account` is what
the voucher shows; `tabGL Entry` is what every ageing report, party ledger and
statement of account reads. Updating one and not the other leaves the voucher and
the reports disagreeing with nothing to say which is right — worse than not
having edited. GL rows are matched on `voucher_detail_no`, the line's own
docname, so an entry with two lines to the same account for the same amount stays
distinguishable, and `gl_entries_updated` reports how many moved.

This is the one field-level exception to "every write goes through the document"
in `tools/mutate.py`, and it is fenced: still the ORM's db layer rather than raw
SQL, still incapable of touching an amount, and there is no supported
alternative — ERPNext marks `party` as not allowed on submit. A **draft** is
saved through the document instead, since it has written no GL Entries and full
validation can still run.

**The reason is written twice**: to the entry's own comment thread, where an
accountant with the voucher open will see it, and to the MCP Action Log, where it
survives whatever happens to the document.

| Refusal | Why |
| --- | --- |
| A cancelled entry | it and its reversing rows are the evidence a posting was made and undone; editing one makes that evidence say something that never happened |
| `line_index` outside the entry | the count is named in the message |
| A rounding or write-off line | ERPNext wrote it itself to absorb a fraction of a cent, and attributing that fraction to a person is not a fact about the person |
| A bank or cash line | that is the operation's own money, and a party there makes every ageing report claim they owe its balance. **No escape hatch** |
| A party type this site has not registered | with the registered ones listed, and `register_custom_party_types` named where it applies |
| A party that is not a record in its register | ERPNext resolves `party` as a Dynamic Link through `party_type` |
| `party_type` without `party`, or either omitted | one without the other is an unresolvable reference, not half an answer. Pass both empty to clear |
| A change that changes nothing | |
| An account type that does not normally carry a party | Receivable, Payable, Equity and blank go through silently; anything else needs `allow_non_party_account=true`, which is the way past rather than a wall. An ordinary expense account carries no `account_type` at all, which is the commonest case and needs nothing |

`dry_run: true` reports the whole plan, including how many GL rows would move,
without writing.

**A Family attribution stays out of the 1099.** `generate_1099_prefill` excludes
Family-party postings and reports the count; attributing a transfer correctly
does not make it reportable.

---

## 127. `convey_parcel`

**MUTATING**, default OFF (`allow_convey_parcel`).

**Arguments:** `parcel`, `target_company`, `effective_date`, `reason` — all
required — plus `owning_entity` / `company` (to narrow a bare parcel name),
`new_title_holder` and `dry_run`.

**Returns** `conveyed`, `from`, `to`, `from_entity`, `to_entity`,
`migrated_attachments`, `migrated_leases`, `migrated_housing_units`,
`migrated_fields`, `migrated_irrigation_zones`,
`migrated_housing_assignments`, `relinked_records`, `relink_detail`,
`title_holder_status`, `appraisal_document_status`, `refusals`, `warnings`, the
whole new parcel, and a `note`.

**This is the door `update_parcel` refuses to be.** Ground changing hands has a
date, an instrument behind it and consequences for two sets of books; a tool that
let it happen by editing a field would record none of them. `reason` is mandatory
and is the narrative — the deed, the assignment, the trust amendment.

**It deletes and recreates, which is the honest shape.** A Parcel's docname
encodes its entity (`Mill Creek - OML` vs `Mill Creek - HLD`), the same way every
Account docname carries a company abbreviation, so there is no field to change
that makes the move true. The order is: create the new record, repoint everything
at it, move the attachments, delete the old one, write the event. Frappe refuses
to delete a document another document links to, so a register missing from
`realestate.PARCEL_REFERRERS` fails the whole call rather than leaving a silent
orphan — and a standalone test checks that tuple against the shipped DocType JSON
so a register added later cannot be forgotten quietly.

**The parcel's own short key is preserved.** Every Field, Irrigation Zone and
Housing Unit is named `<its name> - <PARCEL abbr>` — the parcel's key, not the
company's — so all of a camp's cabins keep the docnames they have always had and
only their `parcel` link moves. `owning_entity` moves with them on the registers
that describe the *ground*; a **Lease's** does not, because a conveyance does not
change who signed a contract. That is a novation, and it is its own document.

**It writes no Journal Entry.** Basis transfer and any gain or loss recognised
are entries with real tax consequences that somebody should write on purpose,
with a narrative of their own — not produce as a side effect of filing a deed.
The result names the entries still owed. Same discipline as `close_note_payable`.

**The trail lives on the survivor.** A conveyance destroys one record and creates
another, so the new parcel's `conveyance_events` child table is the only place
the history can be: it names the entity the ground came from and the docname it
had there, so a reader who finds `Mill Creek - HLD` and remembers
`Mill Creek - OML` can join the two without either record still existing.

| Refusal | Why it is a different document's job |
| --- | --- |
| An **Active**, unterminated lease whose term covers the conveyance date, named | conveying out from under a live lease needs a novation or a termination first. A lease with **no expiration date** counts as running — reading a missing end date as "already over" is the one wrong answer that fails silently |
| A linked Fixed Asset | that is the balance-sheet side and it moves by posting, not by filing |
| A target with no chart of accounts, or no cost centers | a parcel filed against an entity that cannot carry a cost is one somebody finds again in six months |
| A parcel name, assessor id or abbreviation the target already uses | the last one because a silently changed key would file the parcel's future blocks under a different suffix from its existing ones |
| More referring records than the per-register ceiling | no silent caps: a half-conveyed parcel is worse than an unconveyed one |

**Every refusal comes back at once**, not one per round trip. `dry_run: true`
returns the whole plan and the whole refusal list without touching anything.

**The appraisal report does not follow** if it is filed in the old entity's
archive — a Governance Document belongs to a company, and pointing at it across
that boundary is what the archive exists to prevent. That is reported as
`appraisal_document_status: "unlinked_needs_reattach"` in the result *and* in the
conveyance event, never as a silent null. The appraised value and its as-of date
DO come across: they are facts about the ground.

A `title_holder` registered against the entity the ground just left is dropped
with a warning, because one filed under the old entity would read as current and
would be wrong. Pass `new_title_holder` to set the right one in the same call.

**Atomic by construction.** `registry.dispatch` rolls the transaction back before
it logs, so a conveyance that dies half way leaves neither parcel changed rather
than leaving two.

---

# v0.15.0 — the compliance framework

Thirty-two tools. They interlock, and reading them in wave order is the fastest
way to understand what any one of them is for.

The organising idea is one sentence: **compliance is a lens on operational data,
not a duplicate set of records.** Every spray IS an EPA and Worker Protection
Standard record; every hire IS an I-9 record; every bucket IS an FSMA
traceability record. The test for whether a feature is woven in or bolted on:
*does removing it break OPERATIONS, or only break COMPLIANCE REPORTING?*

## 128. `get_compliance_field_map`

Read-only. What compliance requires of an OPERATIONAL record on this site, field
by field: which DocType carries it, which framework wants it, why, and — the
column that matters — what breaks in the day-to-day WORK if it is missing.
Reports which fields are actually present here and which are not.

`docs/compliance_fields.md` is the same content in prose, and a test asserts the
two cannot drift apart.

## 129. `install_compliance_fields`

**MUTATING, and the only tool in this app whose switch ships ON.**

Adds the compliance columns to the DocTypes where the work happens: applicator,
EPA registration number, REI, PHI and weather on **Spray Log**; I-9 status, W-4
status, wage-law jurisdiction and farm labor contractor licensing on
**Employee**; picker, crew, block, bin and shipment on the **BucketLog bridge**.
Verifies (and does not touch) the compliance columns on **Housing Unit** and
**Field**, which this app already ships.

| Argument | Meaning |
| --- | --- |
| `dry_run` | Report what would be added, including the backlog counts, and write nothing |

**This is the one place erpnext_mcp extends a DocType it did not create**, and
`erpnext_mcp/compliance_fields.py` makes the argument at length. The short
version: compliance woven into the operational record is defensible under audit
and a shadow log beside it is not, and you cannot weave anything into a DocType
you refuse to touch. The cost is real and stated — uninstalling this app drops
those columns and everything typed into them, which `before_uninstall` now names
by hand.

Every field is a `Custom Field`, so the target app's repository and migrations
are untouched. Idempotent: the same installer runs on every `bench migrate` and
a second run creates nothing.

**The number worth reading is `backlog`.** Seven fields are required, and Frappe
binds `reqd` on save rather than retroactively — so history stays readable and
stops being re-saveable. The count of rows that would now fail is the operation's
compliance debt, stated in rows.

A DocType not on this site is skipped BY NAME with the app that would bring it.

---

## Wave 2 — the four external-evidence DocTypes

Four kinds of evidence arrive from OUTSIDE the operation and have no operational
act to hang off. Nobody writes a harvest hygiene SOP by harvesting.

## 130. `list_compliance_policies`
## 131. `get_compliance_policy`

Read-only. The SOP library, and one procedure in full with its whole version
chain (walked in BOTH directions) and every audit corrective action that cited
it.

`without_a_document` in the listing is the list worth acting on first: a policy
record with no attached procedure is a claim that a procedure exists, and an
auditor asks to read the procedure.

## 132. `create_compliance_policy`
## 133. `update_compliance_policy`
## 134. `supersede_compliance_policy`

MUTATING (off). **The version is a FIELD, not part of the name** — a policy at v3
is the same record every audit finding already cites — so a second policy under
an existing name is refused and points at `supersede_compliance_policy`.

Superseding writes **both ends of the chain in one act**, because "which
procedure was in force on the day this happened" is asked from whichever end the
auditor starts. Refuses a policy superseding itself, one already superseded (two
successors make the question unanswerable), and a successor whose effective date
PREDATES the one it replaces. The superseded policy is historical rather than
wrong, and audit packets covering the dates it governed still include it.

## 135. `list_certifications`
## 136. `get_certification`

Read-only. The certificate and licence register, **soonest expiry first** — the
order somebody works them in — with what has lapsed and what is inside its
renewal window.

`expired` is read from the DATE, never from the status field. Nothing in this app
rewrites a status when a date passes: a controller that did would only run on
documents somebody saved, so the expired certificates would be exactly the ones
still reading Active.

`get_certification` resolves the holder against the Related Party, Family,
Employee and Company registers and reports which answered. A name in none of them
is not a failure — an applicator licence held by a contractor on nobody's payroll
is exactly what the fallback is for — and it returns the full renewal history
including **every period the certificate was allowed to lapse.**

## 137. `create_certification`
## 138. `update_certification`
## 139. `renew_certification`

MUTATING (off). `renewal_window_days` is a LEAD TIME, not a reminder preference:
90 days by default because that is roughly what an Oregon farm labor contractor
renewal takes once the bond and background check are counted.

**Editing the expiration forward through `update_certification` is refused** and
points at `renew_certification` — editing it in place would produce a certificate
that looks as though it never expired, which is exactly the fact somebody would
want hidden and exactly the fact an auditor asks about. `renew_certification`
appends to a history, requires saying what was actually done to earn the new
term, and **reports any lapse rather than hiding it**: renewing late does not
close a gap that already happened.

## 140. `list_regulatory_filings`
## 141. `get_regulatory_filing`
## 142. `create_regulatory_filing`
## 143. `update_regulatory_filing`

Reads on, writes off. What went to an agency, when, under what docket number, and
what came back.

**A filing marked Submitted with no submission date is refused.** A filing nobody
can prove was made is a filing that was not made — the agency's position in a
dispute is that they have no record — and a half-filled record would be
assembled into an audit packet and read as evidence of something that may not
have happened. A Draft with no dates is exactly what a filing being prepared
looks like and is allowed.

Recording a response auto-dismisses the filing's response alert on the next
sweep. Nobody has to switch it off.

## 144. `list_audit_events`
## 145. `get_audit_event`
## 146. `create_audit_event`
## 147. `update_audit_event`
## 148. `close_audit_event`

Reads on, writes off. Every audit and inspection, its findings, and one row per
thing that has to be fixed — with the deadline the SCHEME set, an owner who is a
PERSON rather than a department, and what actually changed.

**An operation is not judged on having no findings.** Every audit produces some,
and a clean report usually means the auditor did not look hard. It is judged on
closing them.

`update_audit_event`'s `corrective_actions` REPLACES the whole table, which is
the only safe semantics for rows addressed by index — a merge would silently
reorder them and close the wrong finding. `add_corrective_actions` appends and
`close_corrective_action` closes one, so nobody has to resend every row exactly to
change one. Closing requires saying what changed: a tick in a box is what an
auditor is trained to disbelieve, and it is refused.

**`close_audit_event` REFUSES while any corrective action is open**, naming every
one — and the controller refuses it too, so there is no second door. A closure
date over an open finding is the most misleading thing this app could record:
`generate_audit_packet` reads it as "this audit is finished". An audit that raised
no findings at all is closeable; a clean PrimusGFS is a real event.

---

## Wave 3 — the Kairotic Compliance Calendar

**Chronos serves Kairos.** The clock runs the sweep; the sweep decides nothing.
Nine rules ask whether a condition is true RIGHT NOW, and fire on that state
rather than on the date the sweep happened to run.

## 149. `get_compliance_calendar`

Read-only, on by default — the main read of the whole framework. What is due and
what is late, worst first, grouped by category.

| Argument | Meaning |
| --- | --- |
| `severity_min` | `Critical`, `Warning` or `Info` — this severity and worse |
| `days_ahead` | Only alerts due within this many days. **Overdue alerts are always shown**, because they were due in the past |
| `category` | Certifications, Policies, Workforce, Records, Housing, Water and Sanitation, Spray and Pesticides, Filings, Audits, Other |
| `alert_type` | One rule's alerts |
| `regime` | **v0.19.2.** Only alerts that are evidence for ONE audit: FSMA, GAP, GlobalGAP, PrimusGFS, NOP, OTCO, WPS, OR-OSHA, Internal, Other |
| `include_snoozed` / `include_dismissed` | Default false. Snoozed alerts are hidden and COUNTED |
| `as_of` | Read the calendar as of a date |

Categories are chosen so a whole group can be cleared in one afternoon: every
housing item is one walk round the camp, every certificate is one trip to an
agency website.

`regime` is the other axis, and it is the one an inspection is read along:
"everything OR-OSHA will ask about in October" is one afternoon's work and
"everything" is not. Matching is by TAG, never by substring — `GlobalGAP`
contains `GAP`, and a substring match would put another scheme's findings in
front of a USDA GAP auditor. An unrecognised value is **refused**, because an
empty compliance calendar reads as a clean one. `Internal` means the
operation's own standard: real work with a real due date and no outside
auditor.

It reports which rules cannot run on this site at all, because **an empty
category is not the same as a clean one.**

## 150. `list_compliance_rules`

Read-only. Every rule with its `kairotic_gate` — the state that makes it ripe —
plus the framework it serves and whether it can run here. A rule listed as
unavailable raises nothing AND dismisses nothing: an absent DocType is not
evidence that anybody did the work.

**Since v0.22.0 these are records, not code.** Each rule is a Compliance Rule
document whose thresholds, scope, citations, regimes and message an operator can
edit without a release. `editable` says whether this site has migrated yet;
`shape` says how much of each rule is data. Filters: `regime`, `category`,
`target_doctype`, `shape`, `active`, `limit`.

The thirteen, what makes each one fire, and how each one migrated:

| Rule | Shape | Fires when | Silent when |
| --- | --- | --- | --- |
| `certification_expiring` | built-in | inside the lead time the certificate's OWN issuing body takes; Critical inside 30 days | 200 days out; superseded; revoked |
| `policy_review_overdue` | declarative | a procedure IN FORCE is past the review date IT committed to | a draft; a superseded or retired version |
| `water_test_stale` | built-in | a block **in active spray rotation** has no test inside 90 days | fallow ground; a block nobody has sprayed this season |
| `housing_inspection_overdue` | declarative | a cabin somebody can be ASSIGNED to has no walk inside a year | a shower block; a unit already Uninhabitable |
| `housing_detector_test_stale` | built-in | a **FSMA worker facility** has an untested smoke or CO detector | a shed on the same parcel |
| `i9_expired` | declarative | an ACTIVE employee's I-9 has expired | Pending (the lawful 3-day window); a former employee |
| `flc_license_expiring` | declarative | a crew boss's licence is inside 90 days; Critical inside 30 | an employee with no licence |
| `filing_response_due` | declarative | a SUBMITTED filing has no response and the deadline is near | a draft; a filing that was answered |
| `audit_action_overdue` | built-in | an action is past the deadline the SCHEME set | an action with no due date; a closed audit |
| `housing_corrective_action_open` | built-in | a camp finding is open AND unsuperseded | a later CLEAN record for the same unit; a closed action |
| `water_test_contamination` | built-in | a sample came back dirty and is still the latest word on that zone | a later clean sample from the same source |
| `training_expiring` | declarative | a worker's training is inside the 90-day window a retraining takes to arrange; Critical inside 30 | training with no expiry at all |
| `supervisor_review_lapsed` | built-in | an activity record has been on file a fortnight with nobody's §112.161(b) review | a record signed and dated by a supervisor |

**Declarative** means the whole rule is on its record. **Built-in** means every
tunable is on the record and only the SHAPE of the join is shipped code — a
finding superseded by a later clean record, a child table reduced to its worst
row, two date fields that only matter together. There is a third shape,
`custom_python`, and no shipped rule uses it. `docs/configurable_compliance_framework.md`
argues each one and names the primitive that would shrink the list.

## 151. `get_audit_readiness`

Read-only. Resolved alerts over alerts raised, as one percentage — because a
count only means something to somebody who already knows what normal looks like,
and a percentage is comparable to yesterday's.

It also reports **how the score was earned**: `resolved_by_hand_percent` splits
conditions that cleared themselves from dismissals somebody made. An operation at
95% through dismissals is a different operation from one at 95% because the work
got done, and a score that could not tell them apart would be worth gaming. A
single open Critical is called out regardless of the percentage.

## 152. `refresh_compliance_alerts`

MUTATING (off). Runs the whole rule set now instead of waiting for tonight's
scheduled sweep. Creates, refreshes, reopens and auto-dismisses.

**It touches no operational record.** Every rule is a read; the only rows written
are this app's own Compliance Alerts. That is why it is safe at any moment and
why the nightly scheduler calls the same function.

**It cannot duplicate an alert.** Each alert's docname is derived from its rule
and its source record and from nothing that changes daily, so tonight's sweep
finds and refreshes what last night's wrote — and a snooze somebody set last week
survives. A dismissal a PERSON made is never reopened.

**`regime` (v0.19.2) runs only the rules that raise one audit's evidence** — for
the morning before an inspection, when re-scanning every block's water is a
minute nobody has. A rule it skips raises nothing **and dismisses nothing**: a
narrowed sweep that cleared the rules it did not run would empty most of the
calendar and look like progress. `rules_skipped` names each one, `rules_run`
excludes them, and the reported counts are about that regime only.

## 153. `snooze_alert`

MUTATING (off). Hides one alert until a date. **Not a dismissal:** the condition
is still true, the alert still exists, and it reappears on its own — a snooze is
a date rather than a flag somebody has to clear. A date not in the future is
refused.

## 154. `dismiss_alert`

MUTATING (off). Takes one alert off the calendar, with a **mandatory reason**.
The reason is the only part of the record nobody can reconstruct — the alert
itself the sweep can rebuild from the source record — and it is the answer when
the same finding turns up next year. The alert is never deleted: the record that
somebody looked and decided is itself compliance evidence.

Dismissing an alert changes nothing underneath it. Dismissing one about an
expired certificate does not renew the certificate.

## 155. `dismiss_alert_bulk`

MUTATING (off). **Dry run defaults TRUE and the first call writes nothing.**

The whole calendar is one filter away: a `severity` typed where an `alert_type`
was meant matches everything, fails nothing, looks exactly like success, and
leaves an operation reading as compliant while nothing has been fixed. A call
with no filter at all is refused. Capped at 200 per run.

---

## Wave 4 — audit packets and the Command Center

## 156. `list_audit_packet_types`

Read-only. The eight regimes — FSMA, GAP, GlobalGAP, OSHA, DOL, EPA, USDA_NIFA
and an unscoped Other — with the sections each pulls, what it is scoped to, and
**which sections will be empty on this site** because the DocType behind them is
not installed.

## 157. `generate_audit_packet`

MUTATING (off). Assembles every piece of evidence for one audit type over one
period into a PDF and files it as a Governance Document in the company's archive.
Returns the file_url and the counts — never the bytes.

| Argument | Meaning |
| --- | --- |
| `audit_type` | FSMA, GAP, GlobalGAP, OSHA, DOL, EPA, USDA_NIFA, Other |
| `period_start` / `period_end` | A period that has not finished is refused |
| `regime` | Narrow the **training** and **open-items** sections to one scheme. Part of the idempotence key |
| `output_format` | `pdf` (default) or `docx` |
| `output_path` | ALSO write it under the site's private/files |
| `stage_via_chunks` | Checkpoint the assembly. Defaults on above 2 MB |
| `allow_open_actions` | Produce it over open findings, disclosing them at the FRONT |
| `overwrite` | Replace an existing packet for the same type and period |
| `dry_run` | Assemble it, report every count, write nothing |

**It pulls from the operational records, not from a copy.** The spray records ARE
the spray logs; the worker facility records ARE the housing register; the
traceability rows ARE the bucket log. Nothing in a packet is a compliance copy,
which is why nothing in one can have drifted from what was actually done.

**The kairotic gate is a REFUSAL, not a warning.** A packet asserts a compliant
period, and an open corrective action inside that period contradicts the
assertion — a warning at the top of a printed document is not read by the person
the document is handed to. Every open action is named in the refusal.

**Empty sections say why they are empty.** A packet on a site with no BucketLog
bridge says the bridge is not installed and the traceability has to be supplied
separately; a silently omitted section reads as an operation with nothing to
declare.

**It carries the open compliance-calendar items (v0.19.2)**, scoped to the same
regimes as the training section, and that is a disclosure rather than a
confession: the gate above has already refused the packet if any corrective
action from inside the period is open, so what is left is forward-looking work —
an operation demonstrating that it knows what it owes, from a list its own
records generated rather than somebody's memory the night before. It is the one
section NOT scoped to the period, because an expired licence is expired now
whatever quarter the packet covers. Snoozed and dismissed items are excluded:
neither is an open obligation.

Idempotent by (audit_type, company, period, regime).

### The Compliance Command Center

Not a tool — a Frappe Dashboard at `/app/compliance-command-center`, built
idempotently on every migrate. Six Number Cards, four Charts, and
`get_audit_readiness` for the one number somebody acts on.

Deliberately NOT shipped as `fixtures`, which `test_hooks.py` forbids by name: a
fixture cannot look at what is already there, so an operator who reordered their
cards would get it silently put back on every migrate.

---

## Journal Entry attribution drift

## 158. `find_drifted_je_attributions`

Read-only, on by default, **DIAGNOSTIC**. Every submitted Journal Entry in a date
range whose voucher and general ledger disagree about who a line belongs to.

v0.13.0's `update_journal_entry_party` looked its GL rows up by
`voucher_detail_no == line.name` — the Sales Invoice Item convention, not the
Journal Entry one. Every call against a submitted entry matched zero rows, wrote
the voucher, and left the ledger alone. This finds what that left behind.

**Not limited to that.** Drift also arrives from a direct database edit, a
restored backup, or a migration that moved one table and not the other, so
`by_vintage` is reported BESIDE the finding rather than used to filter it.

Lines whose GL rows cannot be identified with certainty are reported separately
as `ambiguous` and are NOT in `repair_input` — reporting a coin toss as a finding
would be worse than reporting nothing.

Three queries whatever the range, matched by the same function the repair writes
through.

## 159. `repair_drifted_je_attributions`

MUTATING (off). **Dry run defaults TRUE.** Takes `find_drifted_je_attributions`'
`repair_input` verbatim and brings each drifted ledger row back into step with its
voucher.

**Moves no balance, ever.** `party` is an attribution column: every debit, credit,
account and date is refused as an argument, so the trial balance after a repair of
two hundred lines is arithmetically identical to the one before it. That property
is what makes a batch write to submitted vouchers defensible at all.

It does not abort on the first failure — each item is a different voucher, and a
run that stopped half way would leave the ledger in a state neither report
describes. Capped at 200. Requires a `reason`, written onto every entry touched.

### `update_journal_entry_party` — what changed in v0.15.0

Its idempotence check now reads **both tables**. v0.14.0 read only the voucher: if
the line already said what was asked for it refused with "nothing to change",
which on a damaged line is precisely wrong — the voucher agreeing is the SIGNATURE
of the damage. Nothing to change now means nothing to change ANYWHERE; a voucher
that agrees over a ledger that does not is a GL-only repair and it proceeds,
reporting `gl_only_update: true`. New `force_gl_sync` writes the GL rows
regardless, which is what the batch tool passes.

---

## Wave 5 — Farm Task Dispatch (v0.16.0)

**Sprint 7 could tell an operation that fifty-four things were wrong. Nothing in
it could send anybody to fix one.** These twenty-three tools are the half that
can, and the loop they close runs:

```
Compliance Alert → Farm Task (with an evidence contract) → claim → start →
complete (photographs, signature, findings) → Housing Inspection / Detector Test
/ Water Test → the register moves → tonight's sweep auto-dismisses the alert
```

**No tool in this wave writes a Compliance Alert.** The only honest way an alert
goes away is to change the world and let the sweep notice.

## 160. `list_dispatch_board`

Read-only, on by default — the main read of the dispatch surface. Every Farm Task
grouped into its state column, worst urgency first.

| Argument | Meaning |
| --- | --- |
| `company` | Whose board |
| `state_filter` | One state, or a comma-separated list of the eight |
| `include_closed` | Include Completed, Rejected and Cancelled. Default false |
| `task_type` / `urgency` / `assigned_to` / `skill_required` | Narrow it |

Reports `in_the_pool`, `open_critical`, and **`generated_from_alerts`** — what
fraction of the board came from the compliance calendar, which is the honest
measure of whether the calendar is driving work or being read and ignored.

The same board renders in the Desk at
`/app/farm-task/view/kanban/Farm Task Dispatch`, where a foreman drags a card
between columns and Frappe writes the state field. There is no custom UI in this
release and none is needed.

## 161. `list_available_tasks`

Read-only. The pool: what a worker could pick up right now, narrowable by
`location`, `skill`, `task_type` and `urgency`. Pass `worker_id` and it also
reports their concurrent-claim count and whether they may take another.

**Only Self-pick and Either tasks appear.** Dispatched work is deliberately
absent from the pool: somebody has to be SENT to it by name, because that is how
this app marks work where the named licence holder matters.

## 162. `list_dispatched_tasks`

Read-only. What one worker is holding — Claimed and In-Progress — with the full
task behind each assignment. `include_finished=true` or an explicit `state` gives
their history.

## 163. `get_farm_task`

Read-only. One task in full: its evidence contract rendered as sentences, every
assignment it has ever had with the evidence filed against each, **every
rejection and the reason given**, the compliance record its completion produced,
and the alert it came from — including whether that alert has since
auto-dismissed, which is the loop visibly closing.

## 164. `create_farm_task`

MUTATING (off). Raises one piece of work.

| Argument | Meaning |
| --- | --- |
| `task_name` | What a foreman calls it out loud. **Required** |
| `task_type` | Inspection / Test / Spray / Repair / Harvest / Training / Compliance-Audit / Hiring / Housing-Cleanup / Water-Sampling / Other. **Required** |
| `evidence_required` | JSON: `photos`, `signature`, `findings_text`, `witness`. **Required** |
| `location_doctype` + `location` | Housing Unit, Field, Irrigation Zone or Parcel, and the docname |
| `skill_required`, `urgency`, `dispatch_mode`, `estimated_duration_minutes` | The shape of the job |
| `creates_record` + `creates_record_data` | What completing it produces, and a template |
| `source_alert` | The Compliance Alert this answers |
| `assigned_to` | Dispatch it straight away |
| `draft` | Hold it out of the pool |

**`evidence_required` is mandatory and is the point of the whole doctype.** A
task that requires no evidence is a task that gets closed with a tick in a box,
and a tick in a box is what an auditor is trained to disbelieve. Refused: a
missing contract; an empty one; one whose every requirement is false; and a
misspelt key, because `{"photo": true}` asks for nothing, refuses nothing, and
looks exactly like a photograph requirement right up until the audit.

**Also refuses** a `creates_record` naming a DocType this site does not have — a
task promising a record nobody can write is a promise that fails in front of a
worker stood in a cabin — a location that does not exist, and a `source_alert`
that already has a task.

Named `FT-YYYY-MM-<seq>`, so the same annual walk can be raised every year
without colliding with its own history.

## 165. `assign_farm_task`

MUTATING (off). The foreman's half of the dual mode: sends a named person.

Refuses to take work off somebody who already holds it unless you pass
`reassign=true` **and** a `reason`, which is written onto their assignment.
"Taken off them with no explanation" is a record nobody can defend. Refuses a
task that is already Completed, Rejected or Cancelled.

## 166. `claim_farm_task`

MUTATING (off). The worker's half: takes one from the pool, and returns the
evidence they will need to close it.

**Capped at three concurrent claims per worker.** A hoarding limit and not a
productivity one: completing or rejecting one frees a slot in the same instant.
Without it, one worker empties the pool onto their own name and the board looks
worked.

Refuses a Dispatched task outright — self-picking one would put the wrong
person's name on a regulated record — a task somebody else holds, and a Draft.

## 167. `start_farm_task`

MUTATING (off). Clocks in on **this task**, not on the shift. A worker on the
clock all morning did this particular cabin between ten and half past, and that
is what an hour charged to a job has to mean. Starting twice is refused: it would
move the clock-in forward and shorten the hour actually spent.

Takes `assignment`, or `task` and the live assignment is used.

## 168. `complete_farm_task`

MUTATING (off). The tool the release exists for.

| Argument | Meaning |
| --- | --- |
| `worker_id` | **Required.** Must be the worker holding the task |
| `evidence_files` | File docnames, file URLs, or objects with a type and caption. Max 40 |
| `signature_file` | The signature capture |
| `completion_narrative` | What they did |
| `findings_text` | What was **wrong** — pass `""` to record that nothing was |
| `witness` | Somebody else who was there |
| `actual_duration_minutes`, `completed_at` | The clock |
| `record_data` | Extra fields for the compliance record, merged over the task's own template |
| `materials_used` | v0.69.0. What was consumed: `[{item_code, qty, uom?, warehouse?}]` |

It checks the evidence against the contract, files it, and **writes the
compliance record the task promised** — the actual Housing Inspection, Detector
Test or Water Test, with the photographs on it. That record moves the register,
and the alert that asked for the work auto-dismisses on the next sweep.

**v0.69.0: the work moves the stock.** Each line of `materials_used` is issued
out of the warehouse as its own submitted `Material Issue`, tagged back to the
task — and on a **spray** task with nothing passed here, the tank mix already on
the task is what gets issued, because that is what the applicator was sent to put
on the block. `materials_consumed` in the result says which list was used, what
moved, and what did not.

**A movement that cannot be written NEVER fails the completion.** Insufficient
stock, an item with no warehouse, a site with no Stock module — every one of them
comes back as a warning on a completion that succeeded. A worker holding a
signature, two photographs and a finished spray is holding a compliance record,
and no shed count is worth destroying one. A **malformed** list is the one
exception and is refused before anything is written.

**A spray stamps its own intervals.** The longest `rei_hours` and the longest
`phi_days` among the chemicals in the tank become `rei_expires_at` (to the hour)
and `phi_clears_on` on the task — a mix is under the strictest product in it —
and the `rei_active_block_entry` and `phi_harvest_window` alerts are raised in
the same call. Both clear themselves when their own interval closes; neither is
dismissible by hand. `spray_windows` in the result reports them, and is null
where nothing sprayed restricts entry or harvest.

**REFUSES a submission short of the contract**, naming each requirement that is
missing. The `findings_text` case is the subtle one: passing an **empty string**
satisfies it, because a clean inspection is a positive statement and that is how
it is made — leaving the argument out entirely records that nobody was asked.

**REFUSES a completion filed by anybody other than the worker holding the task.**
A completion by somebody who was not there is not a chain of custody, it is a
rumour, and it is the first thing an auditor pulls on.

**Lands in `Awaiting-Review`** when the record it produced found something. The
work IS done and the register IS updated; what needs a person is the finding. A
clean completion goes straight to `Completed`, because routing clean work through
a review queue is how a review queue stops being read.

## 169. `reject_farm_task`

MUTATING (off). Hands one back with a **mandatory reason** and returns it to the
pool (or cancels it with `cancel=true`).

The reason is the most useful sentence in the doctype: it turns "nobody got to it
and dispatch never followed up" — the answer nobody can defend — into "the ladder
is broken and I could not reach the detector". **The rejected assignment stays on
the record**: it is the proof somebody was sent, went, and could not do it, which
answers an auditor in a way an absence never does.

## 170. `generate_tasks_from_compliance_alerts`

MUTATING (off). **The bridge.** Walks the open Compliance Alerts and raises one
Farm Task apiece, each carrying the evidence its completion must produce.

| Argument | Meaning |
| --- | --- |
| `company` | Whose alerts |
| `dry_run` | Report without writing. **Defaults FALSE** |
| `alert_types` | Only these rules. Omit for all |
| `limit` | Most alerts to consider. Default and maximum 500 |

Each rule maps to the shape of the work it actually is:

| Rule | Becomes | Mode | Evidence |
| --- | --- | --- | --- |
| `housing_inspection_overdue` | Inspection → Housing Inspection | Self-pick | photos, signature, findings |
| `housing_detector_test_stale` | Test → Detector Test | Self-pick | photos, findings |
| `water_test_stale` | Water-Sampling → Water Test | Self-pick | photos, findings |
| `water_test_contamination` | Water-Sampling → Water Test | Dispatched | photos, findings |
| `housing_corrective_action_open` | Repair | Dispatched | photos, findings |
| `certification_expiring`, `policy_review_overdue`, `filing_response_due`, `audit_action_overdue` | Compliance-Audit | Dispatched | findings |
| `i9_expired`, `flc_license_expiring` | Compliance-Audit | Dispatched | findings (+ signature for I-9) |
| `shift_heat_threshold_crossed` | Compliance-Audit | Dispatched **to the shift's own foreman** | findings, signature |

**Since v0.22.5 a rule that is not in this table can still become work.** Where
the table has nothing to say, the recipe is read off the Compliance Rule record
itself — `producer_farm_task_type`, `producer_skill_required`,
`evidence_contract`, and `producer_assigned_to_expression`. The table is still
consulted FIRST, which is the backward-compatibility guarantee: the thirteen
rules above produce exactly the tasks they always did. The record is read only
for rules the table could not cover, because they did not exist when it was
written.

Where a rule carries `producer_assigned_to_expression`, the task is assigned to
the person it names and **carries no skill**: a skill is a pool and an assignee
is a person, and a task that is both is a task whose holder depends on which one
the dispatcher read first. An expression that names nobody, or names somebody
payroll has never heard of, puts the task back in the pool and says so in
`routing_notes` — never `Dispatched` with nobody on it, which is a task sitting
in Available that no worker is allowed to claim.

Urgency follows severity — Critical → **High**, Warning → Normal, Info → Low.
Deliberately not the identity mapping: a board where everything is Critical is a
board nobody reads.

**Idempotent by construction.** A task carries `source_alert`, so a second run
finds the task the first raised and skips the alert. Re-running after fixing half
the camp raises tasks only for the half still outstanding — two people are never
sent to walk the same cabin.

**An alert type with no recipe is reported by name** rather than turned into a
generic task: a task with a made-up evidence contract produces a compliance
record nobody can rely on.

`dry_run` defaults FALSE, unlike `dismiss_alert_bulk`. The asymmetry is
deliberate — a mis-typed filter there *hides* non-compliance and leaves an
operation reading as clean while nothing was fixed. The failure mode here is too
many idempotent tasks on a board, none of which changes an operational record.

---

## The three compliance records (v0.16.0)

Written by a task completion, or directly by somebody who walked a cabin because
they were passing. Both doors are open on purpose: a compliance record that can
only be written by finishing a dispatched task is a compliance record nobody
writes on the day the dispatch board is down, and the walk still happened.

**The workflow branches on what was found, not on who pressed what:**

```
findings blank    →  Recorded
findings present  →  Corrective Action Required
```

The state is recomputed from the text on every save, so **somebody who has typed
"water stain, north wall, spreading" is not offered the option of marking the
walk as passed.** `workflow_state` is the framework's own field name, so a site
that wants Frappe's native Workflow layered on top attaches one and
`advance_workflow` drives it.

**The write-back only ever moves a date forward.** A back-dated record is filed
as evidence and does not drag a register that already knows about something
later — that would re-raise an alert about work which has since been done.

## 171–174. `list_housing_inspections`, `get_housing_inspection`, `create_housing_inspection`, `update_housing_inspection`

The annual habitability walk. OAR 437-004-1120, 29 CFR 1910.142, FSMA Subpart L.

`create_` takes `unit`, `inspection_date`, `inspector`, `findings`,
`corrective_action`, `signature`, `photos`, `source_task` and `keep_as_draft`,
and moves the unit's `last_habitability_inspection` forward — which is the whole
mechanism by which doing the work takes `housing_inspection_overdue` off the
calendar.

A walk that **found something still moves the date**. Doing the work and finding
a problem are two different facts and both are true.

`update_` corrects or closes one. Closing a finding requires a `closure_note`
saying what was actually done: a date with nothing beside it is what an auditor
is trained to disbelieve.

## 175–178. `list_detector_tests`, `get_detector_test`, `create_detector_test`, `update_detector_test`

Smoke and CO detectors. Results are `Pass`, `Fail` or `Not Present`.

**A failed test still writes the date.** The stale-detector alert asks whether
anybody *knows* the detector works, and a Fail answers it. The answer is bad, so
the record routes to Corrective Action Required and raises a Critical alert of
its own — but the ignorance is over, and a blank date would have the calendar
saying "nobody has tested this" about a building somebody tested this morning.

**`Not Present` writes no date**, for the mirror reason: there is nothing to have
tested, so nothing is known. It is also a finding in its own right — a building
somebody sleeps in with no CO detector is the most dangerous state this app
records.

**A replacement raises a Farm Task** to go and fit one. "Replacement needed" as a
checkbox with nobody dispatched against it is a finding that survives until next
year's test rediscovers it.

## 179–182. `list_water_tests`, `get_water_test`, `create_water_test`, `update_water_test`

Agricultural water. FSMA Produce Safety Rule 21 CFR 112 Subpart E.

`test_date` is when the **sample was taken**, which is what Subpart E's ninety
days count back from — not when the laboratory answered. `lab_reported_on` sits
beside it, and the gap between them is the operation's real turnaround.

**It writes two registers.** The sample came out of an Irrigation Zone, but
`water_test_stale` reads the *block* — Subpart E is engaged by water contacting a
crop, and the crop is on the block. A test filed only against the zone would
leave the calendar calling ground untested whose water was tested last week.

**Results are read both ways**, because a laboratory says the same thing eight
ways: words first (`Absent`, `Present`, `<1`, `Not Detected`), then any number,
where anything above zero is a detection and generic E. coli is compared against
the 112.44(b) criterion of **126 CFU/100 mL**.

**AN UNREADABLE RESULT IS NOT A CLEAN RESULT.** Where neither reading works the
record routes to Corrective Action Required and somebody has to go and look at
the report. Treating an uninterpretable result as a pass is how a compliance file
becomes a clean record of nothing.

**`farm_location_gps` (v0.19.1) says where the sample was drawn.** The zone
names which water; §112.161(a)(1)(i) also asks which standpipe somebody stood
at, and a zone that feeds four hydrants does not answer that. Free text, because
a coordinate nobody could take is worth less than a place name somebody can
stand in: `"45.5152,-122.6784"` where the phone had a fix, `"North standpipe"`
where it did not. Optional and additive — samples filed before v0.19.1 have it
blank.

**Draft is the normal first state here**: a sample is taken on Monday and
answered on Thursday. Use `keep_as_draft`, then file the answer with
`update_water_test` — clearing the flag publishes the same record, recomputes the
state and moves both registers. Filing the answer as a second record would
produce two rows about one sample whose only difference is which was typed
second.

---

## Two new alert rules (v0.16.0)

The rule set is now eleven. The two new ones are a different shape from the first
nine: rules 1–9 fire on **ignorance** — nobody has walked this cabin, nobody has
tested this water — and these fire on **knowledge**, because Sprint 8 gave the
operation a way to go and look.

| Rule | Fires when | Goes quiet when |
| --- | --- | --- |
| `housing_corrective_action_open` | a Housing Inspection or Detector Test found something and nobody has closed it | a **later clean record for the same unit** supersedes it, or the corrective action is closed by hand with a note |
| `water_test_contamination` | a Water Test came back dirty — or unreadable — and is still the latest word on that zone | a **later clean sample from the same source** supersedes it, or it is closed by hand |

Both close by being superseded rather than ticked, and that is deliberate: the
work that makes the finding untrue is the work anybody would want done, so it is
the work that silences the alert. Drafts raise nothing — a draft is a note, not
evidence.

---

# v0.17.0 — multi-entity scoping, mobile auth and the public endpoint

**Sprint 8 built a dispatch board. These sixteen tools are what makes it safe to
point forty phones at it from outside the LAN.**

Three facts frame everything below, and each one is a refusal somewhere:

1. **The role says what KIND of work; the User Permission says WHOSE.** No
   company name appears in any role definition. A `User Permission` row with
   `allow=Company, apply_to_all_doctypes=1` scopes every document that links to
   a Company, for that user, across every doctype — including ones this app has
   not written yet.
2. **An empty entity list means EVERY company, not none.** That is Frappe's
   rule, and it is why `create_mobile_user` refuses to make an account without
   entities and why `list_compliance_calendar_for_me` refuses to answer for one.
3. **The credential buys identity, not entry.** A mobile request presents
   `X-MCP-Token` (entry, still CIDR-gated) *and*
   `Authorization: token <api_key>:<api_secret>` (identity). Frappe
   authenticates the second before this app's endpoint runs, and
   `security.capture_calling_user` saves who it was in the one line before the
   MCP System User is assumed.

## The six roles

Created idempotently on every `bench migrate`. `list_mobile_users` returns the
whole catalogue with each role's `cannot` list, so a client needs no second call.

| Role | Reads | Writes | Notably cannot |
| --- | --- | --- | --- |
| Field Worker | Farm Task, the three compliance records, camp and ground registers | Farm Task **Assignment** only | read a Compliance Policy or the calendar; rewrite the job |
| Foreman | compliance registers, camp, ground | Farm Task, assignments, the three records, alerts | touch accounting; edit the certificate or SOP registers |
| Compliance Officer | Farm Task, camp, ground, governance | the compliance registers, the three records, alerts | **dispatch anybody** — Farm Task is read-only, deliberately |
| Farm Manager | governance | dispatch, compliance, ground, camp, leases | see the cap table; edit the governance archive |
| Family Member | cap table, member events, notes payable, ground, leases | governance documents, related parties | see the operating company's task board |
| Advisor | governance documents, related parties, regulatory filings | nothing, anywhere | everything else |

**The Custom DocPerm trap, in one paragraph.** Frappe ignores every *standard*
DocPerm on a doctype the moment ONE Custom DocPerm exists for it — for every role
on the site. So the installer mirrors the standard permissions into custom ones
before writing the first new row (exactly as Frappe's own Role Permission Manager
does), and **refuses outright** to write a permission onto a doctype another app
owns. A Custom DocPerm on `Employee` would have taken HR Manager off the Employee
register during a migration with nothing printed. `roles.py` argues it at length;
`tests_standalone/test_roles.py` asserts both halves.

## 183. `list_mobile_users`

Read-only, on by default. The roster **and everything wrong with it**.

| Argument | Meaning |
| --- | --- |
| `role` | One of the six |
| `company` | Only accounts whose entity access includes this Company |
| `state` | `Active`, `Expired` or `Revoked` |
| `include_revoked` | Default false — revoked accounts are history, not roster |

`entity_access` is read from the **live** User Permission rows, not from the
grant, so a scoping somebody changed in the Desk shows as drift rather than
agreeing with a stale record. Each account carries a `concerns` list; every entry
is a state that looks fine on a list and is not:

- no Company User Permission at all — which in Frappe means **unrestricted**;
- the grant and the live permissions disagree;
- marked Revoked and the token still works;
- marked Revoked and the login is still enabled;
- the review date has passed and the credential is still live;
- the grant names a role the account does not hold.

## 184. `get_current_user_context`

Read-only, on by default. The mobile app's first call after enrolment: the user,
their mobile roles, the entities their User Permissions allow, which entity to
open on, the credential's review date, and plain-language `can` / `cannot` lists
for an account screen.

**The identity comes from the request.** A client that sends
`Authorization: token …` is reported as that user, with
`identity_source: "authenticated request"`. A request that authenticated as one
person and passes `user` naming another is **refused** — an account that can name
somebody else in a request body is not scoped to anything. With no per-user
credential (an operator's desktop client), `user` is accepted, because that
caller already holds the operator's bearer token.

With no identity at all it returns `identified: false` and the header to send,
rather than guessing.

## 185. `validate_public_endpoint`

Read-only, on by default. Reaches this site **from outside** over HTTPS.

| Argument | Meaning |
| --- | --- |
| `url` | Base URL to probe. Defaults to `public_url` from settings. No path, no query |
| `authenticate` | Send this site's own `X-MCP-Token`. Default false |
| `timeout_seconds` | 1–30, default 8 |

Returns a `tls` block (issuer, subject, SANs, `not_after`, `days_until_expiry`,
protocol, latency), an `http` block (status, whether a JSON-RPC body came back,
how many tools were advertised), and a `working` boolean with a `summary` and a
`next_step`.

**A 401 to the default unauthenticated probe is the best possible result.** It
proves three things at once: the path is reachable, the certificate is valid, and
the token gate is holding. That is why the probe is unauthenticated by default.

**The reachable set is a short allowlist, not an argument.** This makes an
outbound request from inside the site's network, which is the shape of every
server-side request forgery there has ever been. So: the configured `public_url`
or a host under `.ts.net`, over HTTPS, base URL only, redirects not followed —
and `authenticate=true` refuses everything except the configured `public_url`,
because a tool that will POST your bearer token to a hostname in its arguments is
a tool that exfiltrates it.

## 186. `get_tailscale_funnel_config`

Read-only, on by default. The same question from the inside: which ports are
published, at which URLs, what the node's tailnet DNS name is, and whether the
configured `public_url` matches any of it.

**It degrades instead of failing.** A containerised Frappe worker normally has
neither the `tailscale` binary nor the host's socket, and on an Umbrel that is the
**expected** state rather than a fault — Funnel forwards to the port nginx already
serves and needs no cooperation from this process. The tool distinguishes "no
Tailscale anywhere" from "a daemon socket with no client", and points at
`validate_public_endpoint`, which needs none of it.

A config it cannot parse is reported as **unparsed**, not as empty: "no funnel
ports" and "I could not tell" are different answers.

**Nothing in this app can turn Funnel on or off**, and nothing will. See the
README section for the commands, and `funnel.py` for the two reasons.

## 187. `create_mobile_user`

**MUTATING, default OFF.** One call for what four Desk forms do in ten minutes.

| Argument | Meaning |
| --- | --- |
| `email` | Required. The address the account signs in with, and its docname |
| `full_name` | Required for a new account |
| `role` | Required. One of the six |
| `entity_access` | **Required, at least one.** Company names or abbreviations |
| `preferred_company` | Which entity the app opens on. Must be in `entity_access` |
| `token_expiry_days` | Review date, in days. Default 120 |
| `generate_token` | Issue a credential now. **True for a new account, false for an update** |
| `update_existing` | Rewrite a live account's roles and scoping. Default false |
| `notes`, `url` | Recorded on the grant |

```json
{"name": "create_mobile_user",
 "arguments": {"email": "ana@constancyfarms.example",
               "full_name": "Ana Ramos",
               "role": "Field Worker",
               "entity_access": ["Constancy Farms LLC"]}}
```

Writes the User, the role (plus the site's own `Employee` role where it has one),
one `User Permission` per entity, the Mobile Access Grant, and returns
`api_key`, `api_secret` and a ready-made `auth_header`. **That is the only time
the secret appears in a result.**

Refuses: an `entity_access` that is empty or names a Company this site does not
have; a `preferred_company` outside it; a User that already exists, unless
`update_existing=true` — re-running this on a live account rewrites its roles and
scoping, which is a decision rather than a retry. To only re-issue a credential,
use `generate_api_token`.

With `update_existing=true` it also **removes** entities no longer granted. A
stale permission is the failure this release exists to prevent: an account moved
between entities that still carries the old one.

**An update leaves the credential alone by default.** A new account with no token
cannot sign in, so issuing one is the only useful default there. An existing
account has a phone in somebody's pocket, and re-scoping them should not silently
knock it offline — so `generate_token` defaults to false on an update. Pass it
explicitly either way.

## 188. `revoke_mobile_user`

**MUTATING, default OFF.** Disables the login, destroys the credential, records
why.

| Argument | Meaning |
| --- | --- |
| `email` | Required |
| `reason` | Required, at least eight characters |
| `keep_user_permissions` | Keep the Company permissions as evidence. Default true |

`reason` is the point. "Left at the end of harvest", "phone lost in the orchard"
and "dismissed for cause" are three different answers to the question an auditor
asks about why somebody's access ended, and the grant is the only place any of
them survives — Frappe keeps the access and none of the story.

**The roles are left on the account, deliberately.** A disabled user with no live
token cannot sign in; keeping the roles means the record still says what this
person *was*, and an account stripped of its roles is one nobody can later be
asked "what could they see" about.

This is *they no longer work here*. For *they lost their phone*, use
`revoke_api_token`.

## 189. `generate_api_token`

**MUTATING, default OFF.** Mints a fresh Frappe API key/secret pair.

| Argument | Meaning |
| --- | --- |
| `user` | Required |
| `expiry_days` | Days until the credential should be **reviewed**. Default 120 |

The `api_key` is reused where one exists — it is the public half and appears in
access logs, so rotating it would orphan every log line naming it. The **secret**
is always new, which is what makes this the answer to a lost phone: issuing one
stops the previous one working.

**`expiry_days` sets a review date, not an expiry, and the result says so.**
Frappe API secrets do not expire on their own, and this app installs no scheduled
job that revokes one — a job rewriting another app's User records at three in the
morning is not a thing this app does. `list_mobile_users` flags an overdue grant;
`revoke_api_token` is what actually ends it. Calling a reminder an expiry would be
a false assurance about a credential, which is worse than none.

Refuses a disabled user: a token minted for a login that cannot sign in is a token
somebody will spend an afternoon debugging.

## 190. `revoke_api_token`

**MUTATING, default OFF.** Destroys one user's credential and leaves the account
enabled.

Both halves go, not just the secret: an `api_key` left on the row reads like a
live credential to anybody scanning the User list, and the whole value of a
revocation is that somebody can tell at a glance it happened.

A call against an account with no live credential says so rather than pretending
it revoked something.

## 191. `generate_mobile_login_qr`

**MUTATING, default OFF.** The enrolment card.

| Argument | Meaning |
| --- | --- |
| `user` | Required |
| `expiry_hours` | 1–168, default 24 |
| `rotate_token` | **Default TRUE.** Mint a fresh secret for the card |
| `url` | Base URL for the card. Defaults to `public_url`. Must be `https://` |
| `archive` | Also file it privately on a Governance Document. Default false |
| `company` | Which entity to file the archived copy under |
| `error_correction` | `L`, `M`, `Q` or `H`. Default `M` |

The payload the QR carries:

```json
{"v": 1,
 "url": "https://umbrel.tail1234.ts.net",
 "endpoint": "https://umbrel.tail1234.ts.net/api/method/erpnext_mcp.mcp.handle",
 "user": "ana@constancyfarms.example",
 "token": "<api_key>:<api_secret>",
 "api_key": "...", "api_secret": "...",
 "expires_at": "2026-08-02 09:00:00"}
```

`token` is the whole pair in the form the header wants, because the app's job at
enrolment is to store a string and put it after the word `token`. `api_key` and
`api_secret` are present separately for a client that wants them apart.

**The image is a live credential.** Anybody who photographs it over somebody's
shoulder has that account until the token is revoked. That is inherent to
enrolment by QR; the mitigations are all time-shaped. `expires_at` is the deadline
for **enrolling** — once stored, the credential works until revoked. With
`rotate_token` true (the default) the previous credential has already stopped
working, so any phone already enrolled must re-scan; that is what makes re-minting
a card a real revocation of every older copy.

Refuses a non-HTTPS endpoint outright: encoding a live credential for `http://`
would put it on the wire in the clear at every call, forever. Also refuses a
disabled user, an `expiry_hours` beyond a week, and a site that does not know its
own public URL.

`archive=true` files the PNG as a **private** attachment on a Governance Document
— the offline path for a camp office at the end of a gravel road — and the result
tells you to delete that document once the phone is enrolled. The durable record
is the Mobile Access Grant, which holds no secret.

**Site prerequisite.** Needs `segno` or `qrcode`. Without either, this one tool is
not advertised and everything else in the flow still works: `generate_api_token`
returns the same credential as text.

## 192–195. The mobile-ergonomic reads

`list_my_tasks`, `list_available_for_me`, `get_task_with_evidence_contract`,
`list_compliance_calendar_for_me`. All read-only, all on by default.

Every one is a thin wrapper over something Sprint 8 already shipped. They add
exactly three things:

1. **The worker, resolved from the authenticated request** through their Employee
   record. `list_dispatched_tasks` needs an Employee docname; a phone knows an
   email and a Keychain credential. That lookup lives here, once, rather than in
   every mobile client that will ever exist.
2. **A mobile-shaped payload.** `get_task_with_evidence_contract` returns the
   contract as a checklist — each requirement with what it means in a worker's
   words, a `satisfied` flag and a capture hint (`camera`, `signature pad`,
   `text field`) — plus a `next` block naming the tool the task is waiting for, so
   a screen draws the right button without owning the rule.
3. **The entity filter** the phone would otherwise have to guess.

**A login with no Employee record is refused by name.** Returning an empty list
would read on a phone as "nothing to do today", which is a different and much
worse answer. Same for a `company` outside the worker's entities: an empty result
is indistinguishable from a quiet day, and a refusal that names the entity is a
fact somebody can act on.

**`list_available_for_me` is honest about skills.** Nothing on a Frappe site
records what skills a worker HAS — there is no register on Employee, in this app
or in Frappe HR. So an explicit `skill` filters, a site that has added its own
skills field to Employee is read, and otherwise the whole pool comes back with
`skill_matching` saying it is unfiltered. Guessing from a job title was the
alternative, and hiding a spraying task from somebody because their title said
"Harvest Crew" would be hiding work with no way to tell.

**`list_compliance_calendar_for_me` asks once per entity.** This app reads through
`frappe.db.get_all`, which does **not** consult User Permissions — so asking the
calendar once with no company would return every entity on the site. An account
with no Company User Permission is refused rather than shown everything under a
name like that one, and an entity whose calendar could not be read is named in
`failed_entities` rather than silently contributing nothing. It forwards
`regime` (v0.19.2) like every other filter, and validates it BEFORE the loop —
inside it, every exception becomes a named per-entity failure, which is right for
a broken register and exactly wrong for a mistyped argument.

## 196–198. `claim_task_via_mobile`, `start_task_via_mobile`, `complete_task_via_mobile`

**MUTATING, all default OFF.** Sprint 8's `claim_farm_task`, `start_farm_task` and
`complete_farm_task` with the worker resolved from the authenticated request
instead of named in the body.

**They add no rule and weaken none.** The three-concurrent-claim limit, the
refusal to self-pick Dispatched work, the refusal of a Draft, the evidence
contract check, the refusal of a completion filed by anybody but the worker
holding the task, and the Awaiting-Review routing when the record found something
— all of it still comes from those tools, because it **is** those tools. A wrapper
with its own copy of the compliance rules would be a second set to keep in step,
and those are exactly the set that must never drift.

`complete_task_via_mobile` takes `evidence` as the mobile spelling of
`evidence_files`; both are accepted and both mean **file references, not bytes**.
Photographs go up first through `stage_file_chunk` / `commit_staged_file`; this
call carries their docnames.

**Note the `findings_text` rule survives the wrapper.** Passing an empty string
records that nothing was wrong — a clean inspection is a positive statement —
while leaving the argument out records that nobody was asked. The wrapper passes
the argument through a presence test rather than a truthiness test, because a
falsy value dropped in transit would silently turn the first into the second.

**`farm_location_gps` (v0.19.1) records where the work was done.** FSMA
§112.161(a)(1)(i) asks an activity record for the farm's name **and** its
location, and `task_name` was only ever the first half. Free text — a coordinate
pair where the handset had a fix, a place name like `"MC-Cabin-01"` where a
metal roof meant it did not. Optional, and written only when given: an empty
value leaves any location already on the assignment alone.

Over the HTTP mobile API the same field is filled from the `latitude`/
`longitude` the app has been sending since v0.18, which until v0.19.1 reached
only the audit row. An explicit `farm_location_gps` wins over the pair, and a
pair that will not parse is dropped rather than raised on — failing a completion
that carries photographs, a signature and a compliance record over a malformed
coordinate would trade the record for its least important field.

---

# Wave 8 — the training register (v0.19.0)

Eleven compliance rules watched certificates, policies, cabins, water, filings
and audits. None watched **training** — which is what WPS asks for every twelve
months (40 CFR 170.401/.501), what Oregon's heat rule asks for annually before
the first shift at 80 °F (OAR 437-004-1131), what FSMA Subpart C asks for on
hiring and periodically (21 CFR 112.21–.30), and what a GAP auditor asks for by
name with the signature attached. All of it lived in a binder, and the way an
operation found out a handler card had lapsed was that somebody looked, or that
an inspector did.

**One record, many regimes.** A single session covering hygiene, pesticide safety
and heat satisfies GAP, WPS and OR-OSHA at once — provided the trainer covered
all three curricula. So `regimes` is a **tag list** on one record rather than
three records, and every audit packet pulls the subset that audit is entitled to
see. Matching is by **tag, never by substring**: `GlobalGAP` contains `GAP`, and a
`LIKE '%GAP%'` filter would hand a USDA auditor evidence from a different scheme.

**Retention is the longest tag.** NOP five years (7 CFR 205.103(b)(4)), OR-OSHA
three, FSMA and WPS two (21 CFR 112.164(a)(1); 40 CFR 170.309). A record tagged
GAP *and* NOP is a five-year record — destroying it at two would destroy the NOP
evidence. `get_training` returns the number with its citation.

## 199. `record_training`

**MUTATING, default OFF.** One training event, tagged with every audit it
answers.

| Argument | Notes |
| --- | --- |
| `employee` | **Required.** Docname, employee number, name, or the linked login. |
| `training_type` | **Required.** 'PSA Grower Training', 'WPS Handler Training', 'Heat Illness Prevention'. |
| `completed_date` | **Required.** YYYY-MM-DD. A future date is refused — §112.161(a)(2). |
| `regimes` | **Required.** One or more of FSMA, GAP, GlobalGAP, PrimusGFS, NOP, WPS, OR-OSHA, Other. |
| `content_topics_covered` | **Required.** What was actually covered. |
| `expires_date` | **Empty means one-time** and the calendar never asks for a renewal. |
| `provider`, `training_source`, `completed_time`, `certificate_file`, `person_performed_signature`, `company`, `notes` | Optional. |

```bash
-d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
      "name":"record_training","arguments":{
        "employee":"HR-EMP-00002",
        "training_type":"WPS Handler Training",
        "completed_date":"2026-07-21","completed_time":"08:15",
        "regimes":["WPS","GAP"],
        "content_topics_covered":"Label reading, PPE, REI, decontamination",
        "expires_date":"2027-07-21",
        "person_performed_signature":"/files/ben-signature.png"}}}'
```

**`content_topics_covered` is required and that is the point.** Oregon's heat rule
names six topics that must be covered annually; a record claiming OR-OSHA without
them is a record an inspector will disallow. "Heat, water, shade, symptoms,
reporting, emergency response" is a curriculum; "safety meeting" is not.

**A near-miss tag is refused, not corrected.** `"OSHA"` where the vocabulary says
`"OR-OSHA"` would file the evidence where no packet looks for it, and nobody finds
that out until an inspector does. Regulator spellings are accepted and
canonicalised (`oregon osha`, `40 CFR 170`, `7 CFR 205`).

**A renewal ADDS a record and never edits one.** Last year's card is the evidence
about last year. The result names every earlier record it supersedes, so nobody
deletes them to tidy up.

## 200. `list_trainings`

**Read-only.** The register, filtered the four ways an audit or a calendar asks:
`regime`, `status`, `expiring_within_days`, `unreviewed_only` — plus `employee`,
`company`, `from_date`/`to_date`.

`status` and `expiring_within_days` are computed **as of today** from the expiry
date rather than read off the stored column: a record last saved in March holds
March's answer, and filtering on it would report the lapsed set as current.

Returns `by_regime` counts, `expired`, `expiring`, `without_supervisor_review`
(the FSMA §112.161(b) gap) and `without_trainee_signature` (§112.161(a)(4)).

## 201. `get_training`

**Read-only.** One record in full, with that person's whole training history, the
retention period the tags demand with its citation, the §112.161 elements the
record **lacks** in the rule's own terms, and `superseded_by`.

The gaps are listed rather than fixed: a signature added now would be a signature
dated now, and a record assembled before an inspection is what an inspector is
trained to spot.

## 202. `sign_training_supervisor_review`

**MUTATING, default OFF.** Records the FSMA §112.161(b) supervisor review.

**This is the gap a GAP-only operation has.** §112.161(b) requires worker training
records to be reviewed, dated and signed by a supervisor or responsible party
within a reasonable time after the record is made. USDA GAP does not ask for it,
so an operation with an immaculate GAP binder fails on it — and FDA writes it up
even where the underlying training was fine.

**A separate call from `record_training`, deliberately.** The rule's phrase is
"after the record is made" — a sequence, not a form field. A tool that took both
signatures at once would make simultaneous timestamps the default, and
simultaneous timestamps are the shape of a record an inspector reads as assembled
rather than kept. The result reports the lag and says so when it is long.

Refuses a self-review, a supervisor from another entity, a review dated before the
training, and — without `replace_reviewer=true` — overwriting a signature already
on the record.

### The training compliance matrix

#### `get_training_compliance_report`

**Read-only.** Every active employee on one axis, every curriculum this operation
runs on the other, each cell one of **`current`**, **`due_soon`**, **`expired`**
or **`missing`**. Takes `company` (required), `regime` or `training_type` to
narrow, and `as_of_date`.

**`missing` is why this is not `list_trainings`.** A register can only report
records that exist, so the person with no WPS training at all appears there as no
row — which is to say nowhere. Putting the roster on one axis and the
`Training Type` master on the other gives an absence a cell of its own, and it is
the cell an inspector finds first.

**As of a date, not as of today.** `as_of_date` reaches both the record selection
and the expiry arithmetic: a report run in January for last year's audit does not
know about training completed since, so it says what was true then rather than
what is true now. The two halves cannot disagree because they are given the same
date.

**It does not know who needed what, and says so.** This site has no per-role
training requirement table, so the matrix holds the whole active roster against
every active curriculum. That over-reports — a bookkeeper is not a pesticide
handler — so `requirement_basis` states the basis in the response rather than
presenting the over-report as a finding. `regime` and `training_type` are how a
caller asks the question they actually mean.

Returns the matrix, `by_requirement` counts per curriculum, and a `summary` of
`total_employees` / `fully_compliant` / `partially_compliant` / `non_compliant`.
Partially compliant means *holds some and lacks some*; non-compliant means
*holds none of them*.

### The curriculum and the group session

*Eight tools that close both ends of the training loop.* At one end a
`Training Type` was a name and a regime tag, so the matrix could name a gap and
could not deliver the training. At the other, every record was filed one person
at a time, so a crew leader who trained twelve people in a shed had twelve forms
to type — and twelve forms typed one at a time disagree about the date, the
topics and the trainer by the third.

**Nothing here replaces `Employee Training Record`.** The matrix reads per-person
records, `training_expiring` watches per-person expiry, `generate_audit_packet`
pulls per-person rows. The session is the **act**; the records are the
**evidence**; `complete_training_session` is the one moment the first becomes the
second, and it writes them *through* `record_training` so there is a single code
path on this site that knows what a training record means.

#### `update_training_type`

**MUTATING, default OFF.** Puts the content on a curriculum.

| Argument | Notes |
| --- | --- |
| `training_type` | **Required.** An existing Training Type. Not auto-created — see below. |
| `video_url` | An `http(s)` link a handset can open. A path is refused. |
| `materials_description` | What the trainer has to bring. |
| `duration_minutes` | Becomes the default duration of every session of it. |
| `description` | What the course covers, and the citation that says so. |
| `delivery_method` | `Video`, `Classroom`, `Field Demo`, `Online`, `Self Study` — or `field_demo` etc. |
| `regimes` | Which audits the course answers. Unknown tokens refused by name. |
| `active`, `retention_years` | |

**It refuses a curriculum nobody has filed against,** unlike `record_training`,
which takes free text because refusing it would leave an operation with the
training and no record. A *content update* against a name that does not exist is
a typo far more often than it is a new course.

**PDFs and slides go on as attachments,** through `attach_file_to_document`
against doctype `Training Type` — the ordinary Frappe path, so they inherit the
site's own extension allowlist and permission model rather than a second one.

**It touches nothing already filed.** Every session and every training record
carries its own copy of what actually happened, taken on the day.

#### `get_training_curriculum`

**Read-only.** One curriculum in the shape a handset renders it — video,
materials, minutes, method, regimes, attachments — or, with no name, the whole
list. `content_gaps` names what a screen would want and the record lacks (no
description, no delivery method, marked as video with nothing to play) rather
than refusing. Attachments are present on a single read and omitted from a
listing: one query per curriculum to count PDFs is a hundred round trips for a
screen that shows names.

#### `create_training_session`

**MUTATING, default OFF.** Opens a group event — curriculum, day, place, trainer.
Takes `training_type` (required), `company`, `session_date`, `start_time`,
`end_time`, `location`, `conducted_by` / `instructor_name` / `provider`,
`duration_minutes`, `delivery_method`, `regimes`, `content_topics_covered`,
`expires_date`, `training_source`, `status`, `notes`. Named `TRNS-2026-0001`,
where the year is the year the session **ran**.

**It writes nothing to anybody's file,** which is why it is a separate document
from the records it will produce: it can be opened a week early, cancelled, or
left half-filled while the crew arrives. Duration, delivery method and regimes
are inherited from the curriculum whenever the session's own column is empty —
the curriculum says what the course normally is, the session says what this
afternoon was, and an afternoon that ran short is entitled to say so. `Completed`
is refused at creation, and an end time before the start is refused as the typo
it is. An outside trainer is `instructor_name` and `provider`, not
`conducted_by`: forcing a PSA instructor to become an Employee record would put a
stranger on the personnel register to satisfy a form.

#### `add_session_attendee`

**MUTATING, default OFF.** One person on the sign-in sheet. Takes `session`
(required), `badge_scan`, `employee`, `scan_location`, `scanned_at`, `attended`,
`notes`.

**The scan is the identification and the employee link is its result.**
`badge_scan` alone is enough and it goes through the same `resolve_badge` path
the crew clock uses, so a retired card, a card belonging to somebody who has left
and a QR that is not a badge at all are each refused by their own sentence
*before* a name reaches a sheet. A badge and a name that disagree are refused: a
sheet recording one person's badge against another's name would be the one
document in this app that states something nobody believes.

A row with **no** badge is allowed and produces no training record — it says
somebody typed a name, which is true and is not evidence.

**The fix is `log_shift_location`'s, not a second format.** `scan_latitude` /
`scan_longitude` go through the same parser a shift breadcrumb does — the same
`lat`/`lon` aliases, the same range check, the same refusal of half a pair — and
the row stores the same four columns a `Shift Location Log` carries, H3 cell
included, so a scan at a shed door and a track across a block can be grouped by
place without comparing floats. Coordinates are optional and their absence is not
a refusal; a metal packing shed is where GPS goes to die.

#### `sign_session_attendance`

**MUTATING, default OFF.** Takes `session` and `signature` (both required),
`employee` or `badge_scan`, `signed_at`, `replace_signature`, `device_id`,
`gps_latitude` / `gps_longitude`.

**It is a door onto `collect_form_signature`, not a second implementation.**
`Training Session.signature` is a box in the same closed registry the I-9's three
boxes and the W-4's live in, so a training signature gets the whole chain: the
capture is size-limited and sniffed by its **magic bytes** rather than trusted by
its filename, the caller's `write` permission and company scope are checked
through Frappe's own system, the badge is resolved by `resolve_badge` and
**refused when it names somebody other than the person on the row**, the session
is fingerprinted before the mark is written, and a `Signing Evidence` row records
who, how, on what device and where. What this tool adds is the training-shaped
part: it names the box, turns an `employee` or a badge into the attendee row, and
reports the sheet's state back.

**The row is chosen by who, not by position.** A training session has no single
`employee` — twelve people sign one afternoon in their own names — so the
identity check reads its subject from the **attendee row**. Before that it had
nothing to compare a badge against and every scan passed, which is the failure
mode an identity check must not have, because it looks exactly like one that
works.

**`signed_at` is the server's clock and is not an argument.** The evidence row and
the column it is evidence about must say the same moment, and a signing time a
caller could choose is the one field on an attestation worth forging.

**A separate call from `add_session_attendee`, deliberately** — the same reason
`sign_training_supervisor_review` is separate from `record_training`. The badge
is scanned when somebody walks in and the signature is given when the session
ends; one call taking both would make a single timestamp the default, and thirty
scans and thirty signatures sharing a minute is the shape of a sheet an inspector
reads as filled in at the end. The result says so when it sees it. A door scan an
hour earlier is **not** recorded as the signature's verification method: it proves
who attended, and a scan at the pad is what proves who made the mark.

**The session is hashed before the signature is written,** which is the only
moment that answers what the signer was shown — the curriculum, the date, the
topics, the other names. A `Signing Evidence` row carries the hash, the
verification method (`Badge QR` where a badge was scanned), and the coordinates,
falling back to the badge scan's own fix where the phone had one at the door and
none an hour later.

#### `complete_training_session`

**MUTATING, default OFF.** Turns every provable attendance into its own
`Employee Training Record`. Takes `session` (required),
`content_topics_covered`, `regimes`, `expires_date`, `skip_incomplete`,
`completed_at`.

**It refuses by default when somebody marked present cannot be proved to have
been there** — no badge scan, or no signature — and names them. A sheet where
four of twelve never signed is a sheet somebody has to fix while the crew is
still on site, and quietly filing the other eight would take that Monday away
from them. `skip_incomplete=true` files the eight and names the four. Both calls
are legitimate; which is right is not a decision this app can make, and it will
not make it silently.

A row that fails does not take the others with it, and a second call files only
what is outstanding — a row that already produced a record is skipped, so this is
safe to retry. It refuses a cancelled session, a session with no regimes, a
session with no topics, and a session where nobody is ready.

#### `render_training_sign_in_sheet`

**MUTATING, default OFF.** Draws the sheet: the course at the top, a line per
person with their badge, when they were scanned, and their own mark on a ruled
line. Takes `session` (required) and `overwrite`.

**It is what makes the session sealable.** `seal_signed_document` staples its
verification appendix onto a *rendered* form and hashes the result; until this
existed a training session could collect signatures through the same chain as an
I-9 and could not produce the same tamper-evident copy at the end.
`get_document_preview` reads the same page, and `Training Session` resolves
through `signatures.FORM_HANDLERS` exactly as the other three forms do.

Drawn on the primitives the six tax forms and the pay stub share, with its own
footer — this is **not** a working copy of a government form, because there is no
government form for it. It is the employer's own record of an afternoon.

**An unsigned line is drawn ruled and empty** and says so. A sheet that hid the
four of twelve who never signed would be the one document in this app that
flatters the record. **The GPS fix is deliberately not on the page**: it is on the
record and in the evidence rows, and printing coordinates against a worker's name
on a document that gets handed around is tracking data on a page that does not
need it.

A snapshot, not a view: a second render refuses without `overwrite=true`.

#### `get_training_session` / `list_training_sessions`

**Read-only.** The sheet, and the register. Every attendee row carries a `state`
— `recorded`, `ready`, `absent` or `incomplete` — and a `missing` list, computed
in one place so a read cannot say *ready* about a row the completion will skip.
`completion_blockers` is session-level only: an attendee who has not signed does
not block a completion, because a session where eleven of twelve signed should
file eleven rather than nothing.

**Both reads take `SHIFT_ROLES` rather than `HR_ROLES` (v0.92.2):** System
Manager, HR Manager, HR User, Farm Manager, **Foreman** or **Crew Leader**. The
supervisor who holds a tailgate session is the person who needs the sheet from it,
and the same argument `Farm Shift` makes applies here. The **writes** are
unchanged and still take an HR role — completing a session puts a training record
on each attendee's own personnel file, which is a personnel change.

`list_training_sessions` filters by `company`, `training_type`, `status`,
`employee`, `conducted_by`, `regime` and period. **`employee` is what makes this
more than a diary:** it answers "which sessions was Ana at" off the attendee rows
rather than off the training register, so it includes the session she attended
and did not sign — the one that produced no record and is therefore invisible to
`list_trainings`. `with_unproved_attendance` names the open sessions holding
somebody in exactly that state. Attendee rows are omitted from a listing and the
counts are not.

## The twelfth alert rule and the packet section

`training_expiring` fires on the record's own `expires_date`: **Warning** at 90
days (what arranging a retraining actually takes — trainer, crew, language, room),
**Critical** at 30 (the next scheduled course may already be after the lapse) and
**Critical** once lapsed. Training with **no** expiry raises nothing at all,
because a renewal alert nobody can clear is how a calendar stops being read. The
message carries the regimes and what actually stops being lawful — a handler whose
WPS training lapsed cannot legally perform an application.

`generate_audit_packet` gained a **worker training** section scoped to each audit
type's own regimes (GAP → GAP + WPS; OSHA → OR-OSHA + WPS; EPA → WPS; FSMA → FSMA
+ WPS), plus a `regime` argument that narrows it further and is **part of the
idempotence key** so a narrowed packet never silently overwrites a full one.
`generate_compliance_packet` gained `regime`, which staples a training annex to an
accounting packet over that packet's own period.

# Crew shift and heat exposure tools

*v0.19.3. Ten tools that are one workflow with one actor.*

**Compliance anchors to a shift, not to a task.** A task completion carries a
point-in-time reading; a shift carries a timeline. Oregon OSHA does not ask what
the temperature was when one job closed — it asks whether the July 15 shift
complied with OAR 437-004-1131 from start to finish, and only a record spanning
the exposure period can answer.

**The foreman is the sole actor and there is no clock-in tool.** -1131 puts the
water, shade, rest-cycle and observation obligations on a **named** responsible
person, and FSMA §112.161(b) asks that person to sign. A crew of thirty each
clocking themselves in is a shift with thirty people responsible for the record,
which is a shift with nobody responsible for it — and the observable failure is
that nobody logged the water break because everybody assumed somebody else had.

**Per-worker attendance is not lost to this.** Every crew row carries its own
`joined_at` and `left_at`; `remove_worker_from_shift` sets the second rather than
deleting the row; and `end_shift` writes one submitted `Attendance` per person for
**their own** span. The bridge runs one way only — a shift is formed by a foreman
naming a crew, a location and a type, and an attendance row carries none of those,
so deriving shifts from attendance would invent all three on a record an inspector
reads.

## 203. `start_shift`

**MUTATING, default OFF.** Form a crew at a place and start the exposure period.

| Argument | Notes |
| --- | --- |
| `foreman` | **Required.** Docname, employee number, name, or the linked login. |
| `location` | Block, camp or facility. Free text — a shift can be at a place this site has no master for. |
| `shift_type` | Spray / Harvest / Prune / Irrigation / Housing Work / Detector Test Round / Maintenance / General. |
| `farm_location_gps` | `"45.52,-122.68"`. **The weather anchor** — v0.19.4 fetches conditions here every 15 minutes while the shift is open. |
| `crew_employees` | Who is on it at the start. Max 60. Their `joined_at` defaults to the **shift's** start, not to now. |
| `start_datetime`, `company` | Optional. |

```bash
-d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
      "name":"start_shift","arguments":{
        "foreman":"HR-EMP-00001",
        "location":"Block 7 North","shift_type":"Harvest",
        "farm_location_gps":"45.52,-122.68",
        "crew_employees":["HR-EMP-00002","HR-EMP-00004"]}}}'
```

**`joined_at` defaults differ between this and `add_worker_to_shift`, on purpose.**
Everybody rostered at the beginning was there at the beginning, so stamping them
with the moment the API call landed would shave minutes off every one of their
days. A worker added mid-shift arrived when somebody said so, so that one defaults
to now.

## 204. `add_worker_to_shift`

**MUTATING, default OFF.** A late arrival, or a transfer off another block.
`joined_at` defaults to now. Refuses a second row for somebody already on the crew
— two rows look deliberate on the form and become two Attendance days for one
person when the shift closes. Refuses a closed shift: the payroll rows are already
written.

## 205. `remove_worker_from_shift`

**MUTATING, default OFF.** **It sets `left_at`; it does not delete the row.** The
row is the only record that this person was on this shift at all — which is what a
wage claim turns on, and what says who was exposed on a hot afternoon before they
were sent home. Calling it twice without an explicit `left_at` is refused, because
a silent second call would move a departure that has already happened to now.

## 206. `log_shift_event`

**MUTATING, default OFF.** One thing the foreman did about the conditions, at the
moment it happened.

| Argument | Notes |
| --- | --- |
| `shift` | **Required.** SHIFT-2026-0001. |
| `event_type` | **Required.** Water Break / Shade Break / Rest Cycle / Supervisor Observation / Heat Illness Signs Check / Cool-Down / Threshold Crossed / Acclimatization Reminder / Other. |
| `event_datetime` | Defaults to now — the right answer when the call is made as it happens. |
| `logged_by` | Defaults to the foreman. Worth setting when a lead worker called it. |
| `description`, `producer_record_doctype`, `producer_record_name`, `evidence_file_token` | Optional. |
| `weather_snapshot_temp_f`, `weather_snapshot_heat_index_f` | Denormalised for audit convenience. v0.19.4 fills them. |

**The timeline is the evidence.** Oregon's heat rule does not ask whether water
was available in principle; it asks what happened during the shift, and four
water breaks with timestamps answer that in a way an annual policy document never
can. `create_heat_exposure_event` is the claim; this is what the claim rests on,
and an inspector asks for the second.

An event timestamped outside the shift is **kept and reported** rather than
refused: a clock five minutes out is not a false record, and refusing would mean
the break goes unlogged rather than logged approximately.

## 207. `end_shift`

**MUTATING, default OFF.** Close the shift with a signature, and write the crew's
payroll rows.

| Argument | Notes |
| --- | --- |
| `shift` | **Required.** |
| `supervisor_signature_file_token` | **Required.** File docname or file_url. One pointing at nothing is refused. |
| `end_datetime` | Defaults to now. Before the start, or before a crew member's recorded departure, is refused. |
| `foreman_notes`, `reviewed_on` | Optional. |

**The signature is required and it is why this is a tool.** An unsigned close is
an UPDATE setting a timestamp; the signature is what makes it the attestation
§112.161(b) asks for — a review that is dated **and signed**. Without one the
shift stays open and nothing is written.

**One Attendance per crew member, for that person's own span.** A worker who
arrived an hour late and left two hours early worked six hours of a nine-hour
shift, and a row claiming nine is wrong in the employer's favour — which is the
direction that gets litigated. Rows are **submitted**, because
`get_attendance_summary` counts `docstatus 1` only.

**The bridge never blocks the close.** A site without Frappe HR, an employee
archived since the shift ran, a day somebody already keyed in by hand — every one
is reported in `attendance` and none stops a signed shift closing. The signature
is the compliance act; the payroll row is the convenience.

## 207a. `cancel_shift`

**MUTATING, default OFF.** The third ending: the shift was formed and then not
worked.

| Argument | Notes |
| --- | --- |
| `shift` | **Required.** `name` is an alias. |
| `cancellation_reason` | **Required.** `reason` is an alias. A bare Cancelled flag is a gap somebody will be asked about. |
| `cancelled_at` | When the crew was stood down. Defaults to now. Earlier than the start is refused. |
| `foreman_notes` | Optional. |

**It is not a close and it writes no Attendance.** `end_shift` says the crew
worked and writes one payroll row per crew member; this says they did not.
Weather turned at 06:40 and everybody was sent home, the block was not ready, the
sprayer never came — before this tool the two ways of handling that were both
wrong. Left open, the shift is walked by the weather sweep for ever and reported
by `list_shifts` as work in progress; closed with a signature, it files a
§112.161(b) attestation that a day happened and pays a crew for a day nobody
worked.

**If the crew worked part of the day this is the wrong tool.** Close it with
`end_shift` at the hour they stopped, which pays them for the hours they were
there. The choice between the two is the choice between "they were paid for this"
and "they were not", so it is made by a person and never inferred.

**The crew rows and the event timeline are kept.** "They were rostered and stood
down" is what answers a wage claim about the people who turned up, and a water
break called before the stand-down happened — a cancellation does not unhappen
it.

`cancelled_at` is written to `end_datetime`, because status is computed from the
end time first: a Cancelled tick with no end time is still an Active shift the
weather sweep walks. A shift that is already closed is **refused** — cancelling
it would claim the day was not worked while the Attendance rows saying it was
stay on the register.

## 208. `create_heat_exposure_event`

**MUTATING, default OFF.** The OAR 437-004-1131 record for one shift, signed and
submitted.

| Argument | Notes |
| --- | --- |
| `farm_shift` | **Required and unique.** One shift has at most one heat record. |
| `supervisor_signature_file_token` | **Required.** Submitting is the attestation. |
| `water_provided`, `shade_provided`, `mandatory_rest_taken` | What the rule asks. Rest **taken**, not offered. |
| `heat_illness_signs_observed`, `worker_reported_symptoms`, `emergency_response_activated` | Signs observed with no response and no notes is refused. |
| `training_verified` | Checked against the training register **as of the day of the shift**. |
| `max_temp_f`, `max_heat_index_f`, `threshold_crossed_at` | Manual until v0.19.4 computes them from the weather timeline. |
| `acclimatization_plan` | Employees with under 14 days in the heat, per -1131(g). Somebody not on the crew is refused. |
| `event_date`, `notes`, `regulation_citation` | Optional. |

```bash
-d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
      "name":"create_heat_exposure_event","arguments":{
        "farm_shift":"SHIFT-2026-0001",
        "max_temp_f":96,"max_heat_index_f":101,
        "water_provided":true,"shade_provided":true,"mandatory_rest_taken":true,
        "heat_illness_signs_observed":false,"worker_reported_symptoms":false,
        "emergency_response_activated":false,"training_verified":true,
        "supervisor_signature_file_token":"FILE-00042"}}}'
```

**A verified-training claim the register contradicts is refused.** The same audit
packet carries both this record and the training register, and a packet that
contradicts itself is worse than a packet with a gap. Claiming `false` is accepted
and the missing names are reported: a shift that ran with an untrained worker
happened, and the record of it is what the operation needs to have.

**Signs seen and nothing done is the sequence that kills people.** There are
legitimate versions — the worker recovered in shade within minutes and declined
further help — and every one of them is a sentence somebody can write. What is
refused is the silence.

**Everything else is recorded with the gap stated.** A day where the shade trailer
broke down and the crew went home at eleven is a real shift with a real gap, and a
tool that would not let it be recorded would produce either a false record or no
record.

## 209. `list_shifts`

**Read-only.** `company`, `foreman`, `employee` (walks the crew tables),
`status`, `shift_type`, `from_date`/`to_date`, `limit`.

`status` is **computed** from whether the shift has an end time rather than read
off a stored column — an open shift is what the v0.19.4 weather sweep walks, and a
record last saved in March holding March's answer would drop a live shift out of
the fetch.

`closed_without_a_signature` is the one to read. `end_shift` cannot produce one,
so anything on that list was closed in the Desk or by an import.

## 210. `get_shift`

**Read-only.** The crew with each person's own span, the compliance-event
timeline, the weather timeline, and the heat record if one exists. **This is the
evidence chain an inspector is handed.**

Each crew row reports `present_until`, the honest reading of an empty `left_at`:
they were there to the end. Computed rather than written back, because writing it
would destroy the distinction between "left at 13:00" and "stayed to the end" the
moment the end time changed.

From v0.32.0 it also reports `location_log_count` — **a count and not the
track**. A shift with a fix every two minutes carries hundreds of points, and
returning them on every read of every shift would make each one pay for a map
nobody asked to see. **210b** is the tool that draws it.

## 210a. `log_shift_location`

**MUTATING**, default OFF (`allow_log_shift_location`). v0.32.0. What the iOS app
posts periodically while a shift is running.

| Argument | Notes |
| --- | --- |
| `shift` | **Required.** An open shift is *not* required — see below. |
| `latitude`, `longitude` | **Required.** `lat` / `lon` accepted for them. |
| `timestamp` | When the fix was **taken**, not when it arrived. Defaults to now, which is right for a phone posting live and wrong for one catching up. |
| `accuracy_meters` | Kept, never gated on. Past 50 m it is noted. |
| `employee` | Whose device. Optional. |
| `source` | `iOS` (default) or `Manual`. |
| `notes` | Empty for everything the phone posted. |

**It appends and never edits.** A breadcrumb somebody can correct is not a record
of where the phone was, it is a record of where somebody would like it to have
been, and those two documents are indistinguishable afterwards.

**This is the one tool on the shift surface a worker's phone drives rather than
the foreman**, and it does not contradict the sole-actor rule the rest of that
surface keeps. That rule is about who is *answerable* — who forms the crew, calls
the water break, signs the close — and none of it moves here. A breadcrumb
attests to nothing; it records where a device was, which is a measurement rather
than a claim.

**An open shift is not required.** A phone that could not reach the site until the
evening is posting about a shift the foreman has already closed, and refusing
those would throw away the evidence that is hardest to collect. A fix outside the
shift's own span is reported instead — a device still reporting after the crew
went home traces the drive to the shop.

| Refusal | Why |
| --- | --- |
| Coordinates off Earth | a latitude past 90 is the pair the wrong way round, and it is the only version of that mistake a computer can catch |
| A missing latitude or longitude | a breadcrumb with no position is a timestamp |
| An employee from another company | a fix filed against another entity's crew is evidence in the wrong packet |

## 210b. `get_shift_track`

**Read-only**, default ON (`allow_get_shift_track`). v0.32.0.

**Arguments:** `shift` (required), `employee`, `limit` (default and hard maximum
5000).

**Returns** `track` (each point with `lat`, `lon`, `timestamp`,
`accuracy_meters`, `h3_cell`, `employee`, `source`), `count`, `first_fix`,
`last_fix`, `gaps`, `employees_tracked`, `truncated` and the shift's own span.

**In the order the fixes were taken, not the order they arrived.** A phone out of
signal in a canyon posts an hour of breadcrumbs the moment the bars come back, so
a track sorted by insertion draws the crew standing still all morning where the
signal returned and then teleporting across the farm.

**`gaps` is the part a reader misjudges.** Every silence longer than ten minutes
is named with its length, because a straight line drawn between the two ends of
one is a line the crew did not walk. Nothing is interpolated: an invented position
on a record read in a wage dispute or a re-entry-interval question is the worst
thing this app could put on a map.

Empty is the ordinary answer for a shift worked before the phones were logging,
and it is **not** a gap in the compliance record — the shift's own location, crew
spans and event timeline are unaffected.

## 210c. `get_shift_crew_timeline`

**Read-only**, default ON (`allow_get_shift_crew_timeline`). v0.64.0.

**Arguments:** `shift` (or `name`), `employee` (one person's envelope instead of
the whole crew).

**Returns** `crew` — one envelope per rostered person, each with `joined_at`,
`left_at`, `present_until`, `hours_present`, `pay_type`/`pay_rate` (with
`pay_basis_from` saying whether the crew row or the shift answered), an
`exposure` block, `events` inside their span and a `breaks` block — plus the
shift's `thresholds`, `shift_first_crossing`, `sample_gap_minutes`,
`exposed_to_the_heat_threshold`, `arrived_after_the_first_crossing` and
`short_of_their_break_entitlement`.

**The shift is one record and the crew is not one person.** `get_shift` answers
what happened on the shift and `get_weather_timeline` answers how hot it got.
Neither answers the question a wage claim and a heat citation both turn on, which
is what happened *to Ana* — who joined at 09:40, left at 13:00, and was therefore
present for two of the shift's five water breaks and absent for the hour it was
hottest.

**Every figure is computed against that worker's own span, never the shift's.**
`peak_temp_f` is the conditions *they* stood in. `first_crossing_in_span` is when
OAR 437-004-1131's obligations started running *for them*, and
`present_at_shift_first_crossing` says whether they were even there when the
shift crossed. `care_events_in_span` counts the water, shade, rest and
observation events inside their envelope plus the Individual-scoped ones naming
them — a crew break at 08:00 is not care given to somebody who arrived at 09:40,
and counting it would flatter the operation in exactly the place an investigator
checks.

**Nothing is interpolated.** `minutes_bracketed_by_crossings` is a *bracket* from
the first at-or-above reading in their span to the last, not a sum of exposure:
the readings are samples and the temperature between two of them is a thing
nobody measured. `sample_gap_minutes` reports the real cadence, so an afternoon
reconstructed hourly from the archive cannot be read as a live quarter-hour one.

**`breaks` is per person and null without a policy.** Entitlement is a function
of hours worked, so the four-hour picker and the ten-hour foreman are owed
different numbers across the same afternoon.

## 211. `list_heat_exposure_events`

**Read-only.** `company`, `from_date`/`to_date`, `with_gaps_only`, `limit`.
Returns `with_signs_observed` (what an investigation reads first — and a shift on
that list is one where the observation obligation was **working**),
`without_verified_training` (a citation on the first hot morning) and
`without_a_signature` (which should be empty).

## 212. `get_heat_exposure_event`

**Read-only.** One record in full with the shift behind it, and the obligations it
does **not** claim were met, in the rule's own terms. Where the shift's event
timeline is empty it says so — the checkboxes here are the **assertion** and the
shift's logged water breaks are the **evidence** for it.

## The thirteenth alert rule, and the Attendance bridge

`supervisor_review_lapsed` watches a signature that was never put on a record —
the work was done, the record was written, and the second pair of eyes
§112.161(b) asks for never arrived. **Warning** at 14 days (one clear miss of the
weekly review cadence "reasonable time" is read against for records generated
daily), **Critical** past 30 (no reading of "reasonable" covers it, and a batch
signed the week before an inspection is the finding rather than the fix). It
auto-dismisses the moment somebody signs.

The clock runs from when the record was **made**, not from the activity date: a
training delivered in March and recorded yesterday has a one-day-old record, and
reading the activity date would raise a Critical on every season somebody
backfilled. It walks a **table** of doctypes carrying the §112.161(b) columns —
one row in v0.19.3, and Housing Inspection, Water Test, Heat Exposure Event and
Farm Task Assignment are each a one-line addition the day they grow them.

`Attendance` gains a **`farm_shift`** Custom Field, installed alongside the
v0.15.0 compliance fields and reported by `before_uninstall`. Without it a
shift-formed day is indistinguishable from a hand-keyed one, so nobody reading the
register can reach the conditions the person worked in — and the bridge, unable to
tell its own rows from anybody else's, would pay somebody twice for one afternoon.

## v0.19.2 — regimes as records, curricula as records

`Compliance Alert` gained a **`regime`** Table MultiSelect over a new
`Compliance Regime` master, written by the sweep rather than typed: ten rules
carry a constant (an overdue cabin inspection is an OR-OSHA item whoever is
asking) and the two that fire on many kinds of thing — certificates and training
— tag each alert from the RECORD, because an applicator licence is WPS evidence
and a GlobalGAP certificate is not. `get_compliance_calendar`,
`list_compliance_calendar_for_me` and `refresh_compliance_alerts` all take
`regime`.

The vocabulary did not move. `erpnext_mcp/training.py` still holds it, the master
is seeded FROM it on every migrate, and `Employee Training Record.regimes` is
still a delimited tag list — a child table and a comma-separated column that
disagreed about whether a row carries WPS is the failure that module exists to
prevent. Two tokens were added: **`OTCO`** (Oregon Tilth, the certifier that
holds the organic file, as against NOP the rule) and **`Internal`** (the
operation's own standard — real work, real due date, nobody coming to inspect).

`Employee Training Record.training_type` became a **Link to `Training Type`**,
migrated from the free text already in the column: the master names itself from
that text, so the ordinary record is not rewritten at all. It is still not a
Select — `record_training` creates a curriculum from free text the first time
somebody files a course this site has not run, and says so in the result. The
new curriculum takes the regimes its NAME implies rather than the session's,
because the record says what one afternoon covered and the curriculum says what
the course normally answers.

---

# The weather timeline (v0.19.4)

v0.19.3 shipped `Farm Shift Weather Reading` with nothing writing to it, so that
fixing the shape first meant v0.19.4 could wire a fetch instead of migrating a
schema under live compliance records. This is the fetch.

**The mechanism is mostly a schedule.** `services/weather.sweep_open_shifts` runs
on a `*/15 * * * *` cron, walks every Farm Shift with no `end_datetime` and a
`farm_location_gps`, asks Open-Meteo what the conditions are there, and appends a
reading. Fifteen minutes rather than hourly because OAR 437-004-1131 asks what the
conditions were across an exposure period, and nine readings on a nine-hour shift
is a sketch where thirty-six is a timeline.

**The heat index is computed, not read off the API.** Open-Meteo returns
`apparent_temperature`, which folds in wind and radiation and is a wind-chill
figure in winter. The NWS heat index is temperature and humidity, and it is what
the rule turns on: 88 °F at 70 % humidity is a **100 °F heat index**, and a shift
documented against apparent temperature would look compliant while somebody was
being cooked. Both inputs are stored beside the result so a disputed index can be
recomputed from the observation.

**A crossing logs an event. It never files a heat record.** A reading at or above
the threshold writes one `Threshold Crossed` compliance event — once per shift,
not once per reading, or a hot afternoon buries the water breaks under thirty-six
identical rows. It does **not** create a Heat Exposure Event, and that is the line
this release draws: that record says which crew was exposed, what water was
provided, whether the rest cycle was taken and whether anybody showed signs, and
it carries a signature. Those are five judgements by the person who was standing
there. The sweep surfaces the condition; the foreman decides whether it is a
record.

**Three things now fill themselves in.** A compliance event's
`weather_snapshot_temp_f` / `weather_snapshot_heat_index_f` come from the reading
current at its own instant (the last one at or before it, within half an hour —
earlier beats later, because that is the conditions the foreman was standing in).
A Heat Exposure Event's `max_temp_f`, `max_heat_index_f` and `threshold_crossed_at`
compute off the shift's timeline. **Manual entry always wins** in both cases: an
on-site reading beats a modelled figure for a grid square measured in kilometres,
and the computed value fills a blank rather than correcting an answer.

**Open-Meteo needs no API key, which is a reason to be more careful with it.** The
service caches by coordinate rounded to four decimals, skips any shift read within
`fetch_interval_minutes`, and treats a 429 or a 5xx as an instruction — exponential
backoff per coordinate, doubling, capped at an hour, during which no request goes
out for that place at all. Nothing raises: a failed fetch is a missing reading, and
a shift with a gap in its timeline is an infinitely better outcome than a scheduler
that stopped.

## 213. `fetch_weather_now`

**MUTATING (default OFF).** `shift`. Appends one reading to an **open** shift
immediately, bypassing the cache — for the foreman who wants the conditions on the
record now rather than in eleven minutes. Logs a `Threshold Crossed` event where
the reading crosses. Refuses a closed shift (a `current` reading filed against a
crew who went home is true about the place and false about the shift) and one with
no coordinates.

## 214. `backfill_weather_for_shift`

**MUTATING (default OFF).** `shift`. Reconstructs a **closed** shift's timeline
from the archive API, at that API's own **hourly** granularity, filtered to the
shift's own period so a six-hour morning does not acquire a timeline running to
midnight. Idempotent: every reading is matched against the minute already present,
and a reading is never edited — so running it over a shift that was also swept live
keeps the live readings and fills the gaps. Returns `added`, `skipped_as_duplicate`
and `failed`.

It writes **no** compliance events, and that is the one judgement call in the tool:
a `Threshold Crossed` row dated last July on a closed and signed shift would be an
observation nobody made, sitting beside water breaks somebody did. The crossings
are counted and reported instead, which is also the sentence that tells a foreman
whether the shift needed a heat record at all.

## 215. `list_shifts_missing_weather`

**Read-only.** `company`, `from_date`/`to_date`, `limit`. Closed shifts carrying
fewer than **one reading per hour** of their own length — the archive's granularity,
so a fully backfilled shift never appears and a live-swept one clears it four times
over. Shifts with **no `farm_location_gps`** are reported separately: no amount of
backfilling documents one, and the fix is a different action.

## 216. `get_weather_timeline`

**Read-only.** `shift`, optional `from_datetime`/`to_datetime`. The readings, the
extremes, the count at or above threshold and `first_crossing` — the instant
-1131's obligations start running from. Reports the `sources` present, and calls
out a **mixed** timeline by name: live fifteen-minute readings and an hourly
archive reconstruction are both true and are not equally strong.

## 217. `get_weather_settings`

**Read-only.** No arguments. The kill switch, the cadence, the three thresholds and
the per-company overrides — because "the threshold is 80" is false on a site where
one entity set 75. It works even when weather is switched **off**, because it is
the tool somebody calls to find out why nothing is being fetched.

**There is no `update_weather_settings`, and there will not be one.** Those are
three outbound URLs and three numbers that decide whether a hot afternoon is logged
at all. A tool that could write them would be one sentence away from pointing this
site's weather somewhere else, or from raising the heat threshold past anything
Oregon produces — leaving a site that behaves normally and never says anything is
wrong. The Desk form is the write surface, where a person types the number and
Frappe's version trail records who did.

## v0.19.5 — what the year actually earned per acre

**The first release in this run that no regulator asked for.** Everything from
v0.19.0 onward answered somebody with a citation; this answers a lender, a buyer
and the person deciding whether a good year was earned or borrowed.

$$\text{Sustainable CF/Acre} = \frac{\text{Normalized OCF} - \text{Maintenance Capex}}{\text{Productive Acres}}$$

**Headline operating cash flow lies in two directions at once.** It is *flattered*
by money that came in and will not come in again — an insurance recovery, a
settlement, a gain on a tractor sale that landed in the operating section — and it
is flattered *again* by maintenance that was not done. A farm running its
irrigation to failure to make a year look good is destroying the thing the year
was earned with, and the headline number goes **up** while it happens.

**The output is itemized and that is not a convenience.** A single number here is
worthless, because the number's whole claim is that it has been adjusted, and an
adjusted number nobody can inspect is indistinguishable from an arranged one.
`get_sustainable_cf_per_acre` returns every approved adjustment with its
justification and the name behind it, every maintenance-capex asset with its
purchase date and portion, and every productive block with the days it was in
service. The figure is the last key in the payload rather than the only one.

**AI proposes, human approves**, and the two are separate tools with separate
switches. Finding a non-recurring item scattered through a ledger nobody reads
line by line is worth a great deal and is something a model is good at; deciding
that a hailstorm in a region that hails every third year is non-recurring is a
judgement somebody defends across a table.

**Maintenance capex is actual spend, never a percentage of revenue.** The common
shortcut destroys the only interesting signal: an operation that spent nothing on
replacement did not have a cheap year, it borrowed the year from the orchard, and
a formula substituting 3 % of revenue reports a well-maintained farm every time —
including in the years it matters. Assets with no `capex_type` are **excluded and
counted**, in neither direction.

**The denominator is what is productive, not what is owned.** Fallow ground and
pre-yield plantings are out and counted separately, and a block that came into
bearing in February is weighted for the part of the period it was actually
earning — inclusive days at both ends, because `period_end` is an inclusive date
everywhere else in this app.

**Raw OCF is computed from GL Entry by the direct method**, not read off
ERPNext's Cash Flow report. Cash and bank movement per submitted voucher,
apportioned to operating / investing / financing by the accounts on the other
side, with a mixed voucher split proportionally rather than assigned to whichever
line is biggest. A report's output cannot be traced back to rows, and the whole
argument of this KPI is that it has to be.

## 218. `create_normalization_adjustment`

**MUTATING (default OFF).** `company`, `fiscal_year`, `period_start`,
`period_end`, `amount`, `direction`, `category`, `justification`, optional
`supporting_document_file_token`. **Creates a `Draft`, always** — a draft does not
count towards the KPI and nothing in this tool can make it count.

`amount` is **always positive**; the sign lives in `direction`. A negative amount
beside a `Subtract from OCF` is a double negative, and a double negative is how an
adjustment ends up moving the number the wrong way in a pack somebody is borrowing
against.

`justification` has a **forty-character floor**. Not a quality bar — no character
count is one — but a floor under "one-time" and "per Tim", which are what gets
written when the field is merely required and which an auditor reads as an
admission that nobody thought about it.

## 219. `approve_normalization_adjustment`

**MUTATING (default OFF).** `name`, `approver_signature_file_token`, optional
`approver_employee`. Status to `Approved`, signature attached, `approved_on`
**written rather than taken as input** — an approval date somebody can set is one
they can set to before the quarter closed.

**There is no unsigned path through this tool.** The whole argument for the record
is that a normalization is a judgement with somebody's name against it. Refused
where another approved adjustment already covers the same company, period and
category: two approved adjustments are two answers to one question, and the one a
reader finds will be whichever sorted first. A correction **supersedes**.

`approver_employee` defaults to the Employee linked to the acting user, and is
empty where the app runs as a service principal — which is the ordinary
configuration and is not a failure. The signature is the identity that matters.

## 220. `reject_normalization_adjustment`

**MUTATING (default OFF).** `name`, `rejection_reason`. The rejection is **kept**
rather than deleted, for the same reason a rejected insurance claim is kept: a
refusal with a reason teaches the next proposal, and a register with only its
successes in it says nothing about how hard the successes were to get.

An already-approved adjustment **cannot** be rejected. It has been counted, and
rewriting a decision is not the same as recording one — supersede it instead.

## 221. `backfill_asset_capex_type`

**MUTATING (default OFF).** `default_capex_type` (`Maintenance` by default),
optional `cutoff_purchase_date`, `company`, `dry_run` (**default TRUE**).
Classifies Assets that have **no** `capex_type`, never one somebody made, so a
second run finds nothing to do.

The heuristic is one sentence: *everything bought before the operation started
tracking is generally maintenance, because it is the existing productive plant
carrying on.* `Mixed` is refused as a bulk default — a split is a judgement about
one invoice, and applying one to a hundred assets would be inventing a hundred
splits.

**A starting position, not an answer**, and the result says so. The new block
planted in year six was growth and will read as maintenance until somebody fixes
it on the Asset, which *understates* the KPI; a register of nulls *overstates* it,
because unclassified purchases are excluded entirely.

## 222. `list_normalization_adjustments`

**Read-only.** `company`, `fiscal_year`, `status`, `limit`. The register, scoped
to the companies the caller may see. `counted_in_the_kpi` is the list that
matters — only `Approved` rows move the number. `awaiting_a_decision` is the other
one worth reading at quarter end: a proposal nobody has decided is not a neutral
state, it is a figure that will change after the pack goes out.

## 223. `get_sustainable_cf_per_acre`

**Read-only.** `company`, optional `as_of`, `window_type`, `window_months`,
`computation_step`, `historical_lookback_years`, `include_historical_averages` —
and the deprecated `period_start` / `period_end` pair.

**Since v0.19.6 it defaults to a trailing twelve months.** Call it with only a
company and you get the TTM window ending at the last completed month, the month
just finished beside it, and five years of prior TTM values with their mean,
median, spread and the two deltas. The window's `components` carry everything the
single-period payload carried — the summed adjustments with their justifications,
the aggregated maintenance capex per asset, the time-weighted acres per block.
See `docs/reporting_ttm_standard.md` for the shape and the boundary rule.

**Passing `period_start` and `period_end` returns the v0.19.5 payload, exactly**,
with a deprecation sentence at the head of `computation_warnings`: `raw_ocf` with
its sourcing note and the investing and financing sections, the itemized
adjustments, `normalized_ocf`, `maintenance_capex` with the unclassified count
and amount called out, `productive_acres` per block with days in service, the
figure. That path is kept because this number is quoted in packs that were sent
before the window existed, and a release that changed what an unchanged call
returned would silently alter a figure somebody had already given a bank. One of
the two without the other is refused rather than guessed at.

`sustainable_cf_per_acre` is **null, not zero**, where there are no productive
acres: a division nobody performed is not an answer, and a zero would be read as
one. **Read `computation_warnings` before quoting the figure** — undated blocks,
unclassified assets and a period with no approved adjustments at all are each a
sentence there rather than a silence.

## v0.19.6 — the window standard

**Every financial report now defaults to a trailing twelve months.** Not a
feature on one metric: the shape every financial figure in this app takes from
here on. `docs/reporting_ttm_standard.md` is the full argument; the short version
is that agricultural revenue is aggressively seasonal, so Q3 is harvest and Q1 is
pruning, and two single periods set against each other say the operation
collapsed in January and recovered in September — every year, on every farm,
whether or not anything happened.

**Three blocks, and each is the correction for the other two.** `point_in_time`
is the period just finished. `window` (also `ttm` when the type is TTM) is the
same figure over twelve rolling months, so the whole annual cycle is inside it
exactly once however it is read. `historical_averages` is what that window has
been worth for this operation before — which is the only thing that says whether
the current number is good. A TTM figure means one thing above its five-year mean
and the opposite below it, and the first two blocks cannot say which.

**The window ends at the last completed step, never a part-finished one.** Read
on 2026-08-03 with a Monthly step, the window is 2025-08-01 to 2026-07-31. Three
days of August against twelve months of everything else is a figure that falls
every first of the month and recovers by the thirty-first, and an operator
reading it on the fourth will believe the fall.

**Quarterly and Yearly steps follow the company's own fiscal year**, and the
payload reports `fiscal_year_start_month` rather than leaving it to be inferred.
A July-year operation stepping its history by calendar quarters would put every
year-end close in the middle of a bucket.

**Partial history is said out loud and never annualized.** A site with four
months of ledger gets four months of ledger, labelled, because annualizing it
would invent eight months of a season that has not happened.

**The history is cached in `Financial KPI History`**, filled by an overnight
sweep at 02:00. A five-year Monthly history is sixty full computations over
twelve months of GL each; a live query reads the cache and computes at most
twenty-four missing snapshots before stopping and naming the tool that fills the
rest. Approving a normalization adjustment for a period the cache already covers
**deletes** the snapshots whose window contained it — a stale components list is
worse than a missing one, because it is a set of ingredients that does not
produce the number printed above it.

## 224. `get_windowed_report`

**Read-only.** `report_name` (required), `company`, optional `as_of`,
`window_type`, `window_months`, `computation_step`,
`historical_lookback_years`, `include_historical_averages`.

`report_name` selects among the registered computers — `sustainable_cf_per_acre`,
`ocf` (raw and normalized operating cash flow, for a covenant test that needs the
figure without an acreage denominator attached) and `revenue`. The payload lists
what this site has under `available_reports`.

**This is the generic entry point and it is why the standard generalizes.** A
report registered in `erpnext_mcp/services/financial_reports.py` is reachable
through it without another tool, another switch and another section here — a
framework whose every KPI costs a tool is a framework with six KPIs in it.

**It warms a cache, and that is the one thing it writes.** Snapshots it had to
compute are saved to `Financial KPI History` so the next caller does not
recompute them. Nothing in your ledger is touched: no Account, no GL Entry, no
Journal Entry, no Asset, no Field, no adjustment. Deleting every cached row
changes no answer this tool gives — only how long it takes to give it.

## 225. `list_financial_kpi_history`

**Read-only.** Optional `kpi_key`, `company`, `computation_step`, `window_type`,
`from_date`, `to_date`, `limit`. The precomputed cache as a plain series — use it
to draw a line or export one, where `get_windowed_report` would send sixty copies
of the components dict to deliver sixty numbers.

**A gap here is not a gap in the business.** It is a window nobody has computed
yet, or one invalidated by a retroactively approved adjustment and not yet
rebuilt, and plotting it as a continuous line draws a trend that did not happen.
`source_versions` matters on a long series: where a release changed how a figure
is computed, a series spanning the change holds two definitions of one KPI on one
line with nothing marking the join.

## 226. `recompute_kpi_history`

**MUTATING (default OFF).** `kpi_key` (required), optional `company`,
`back_years` (default 5), `force` (default FALSE).

**The mildest mutating tool in this catalogue.** The only thing it can change is
a cache: every row it writes is what the live computation would have produced,
and every row it deletes comes back on the next read or the next overnight sweep.
The worst outcome of running it at the wrong moment is time spent.

**It is the answer to a retroactive approval.** Approving a normalization
adjustment for a period the history already covers invalidates the snapshots
whose window contained it; this rebuilds them *now*, with the result in front of
you, which is what you want when the pack goes out this afternoon. A Field
productive-date backfill is the other case — it moves the denominator of every
window containing the corrected block.

`force=true` **clears and rebuilds** rather than filling gaps. Use it after a
release changes how a figure is computed: an incremental fill leaves the old rows
in place, and a series holding two definitions of one KPI is a line with an
unmarked join in it.

---

## 227. `list_visits`

Read. Optional `company`, `worker`, `location`, `from_date`, `to_date`, `limit`
(default 100, maximum 500).

**A worker does not go to a task, they go somewhere.** They drive to the north
block, walk five cabins, close five task assignments and drive back. The board
records five completions with five timestamps; this reports the trip.

```json
{
  "visits": [
    {
      "visit_id": "5C1F0A64-…",
      "first_completion_datetime": "2026-08-03 09:12:04",
      "last_completion_datetime": "2026-08-03 10:41:55",
      "duration_minutes": 89,
      "location": "MC-Cabin-01",
      "locations": ["MC-Cabin-01", "MC-Cabin-02"],
      "company": "Example Trading Co",
      "completing_user": "HR-EMP-00007",
      "task_assignment_names": ["FTA-2026-00311", "FTA-2026-00312"],
      "total_tasks": 2,
      "total_evidence_files": 5,
      "logged_duration_minutes": 55
    }
  ],
  "count": 1, "single_task_visits": 0, "ungrouped_completions": 14
}
```

**The grouping is the handset's, not a guess from timestamps.** The app mints a
`visit_id` when a worker arrives and reuses it for every task closed before they
leave, because the phone is the only thing that was there. Two cabins forty
minutes apart on one unhurried walk are one trip; two a minute apart from
opposite ends of the property are two — and no threshold gets both right.

**A completion with no `visit_id` is in no visit.** Not a synthetic one-task
visit, not an "unassigned" bucket dressed as a trip. Everything filed before
v0.20.1 has the column blank; `ungrouped_completions` says how many were skipped.

**The identifier is checked where it is written.** A UUID as 8-4-4-4-12, matched
in either case — anything else is refused at the completion, naming the value and
the shape, rather than stored. The grouping is by exact value, so a garbled
identifier would not read as a bad row here: it would read as a second visit, and
the rollup would look complete while being wrong. Sending none is still fine and
still counts as no visit.

**One-task visits are returned.** Somebody drove out, did one job and drove back
— which is precisely what a question about wasted travel is looking for. Filter
on `total_tasks` if the question is about multi-stop rounds; `single_task_visits`
tells you exactly what you would be dropping.

`duration_minutes` is **first completion to last** and excludes the drive out and
the walk in; a one-task visit measures zero, because one completion is one
instant. `logged_duration_minutes` is the sum of what the workers themselves
recorded per task, which is a different number and deliberately reported beside
it. `total_evidence_files` counts distinct **Files**, not evidence rows: one
signature filed against three cabins is one photograph.

The `location` filter matches a visit **any** of whose tasks is at that place,
and returns the visit whole — reporting a trip with half its work missing would
answer a different question.

There is no `Farm Visit` doctype and there should not be one: a visit has no
facts of its own. Every field above is derived from the completions in it, and a
row that had to be created before them could not be created by a client that was
offline when the trip started.

---

# Templated inspection sessions (v0.21.0)

A worker does not go to a task. They walk into MC-Cabin-01, do everything the
cabin needs, and walk out. Compliance sees three regulated cadences that must
stay separate — a Housing Inspection is annual under 29 CFR 1910.142, a Detector
Test is on the fire code's cycle, a Water Test is Subpart E's ninety days — and a
merged record would be due on three schedules at once.

**The UX is grouped; the records stay separate.** An *Inspection Session* is the
afternoon. The records it produces are the register, produced exactly as they
would have been from three separate trips: same doctypes, same registers
advanced, same alerts dismissed, same audit-packet rows. What changes is that the
photographs and the signature are captured once, and an auditor holding a Housing
Inspection can ask which visit produced it.

## Templates are data

An *Inspection Template* is a Frappe record. It says which sections a visit
consists of, what evidence each section needs, which renderer draws it and which
compliance record it produces. `create_inspection_template` writes one and it is
live: it reaches the handset on the next fetch, the rule engine can match it on
the next sweep, and no code, no DocType JSON and no app build was involved.

Four ship seeded, on install and on every migrate, and an edited one is never
overwritten: **Pre-season Cabin Opening**, **Mid-season Habitability**,
**Post-harvest Cabin Close-down** and **Spray Day Inspection**.

## The runtime is deterministic

`generate_tasks_from_compliance_alerts` bundles by set inclusion and nothing
else. Alerts are grouped by the place they point at; a place with **two or more
alerts of different types** is a candidate; each alert type is translated into
the compliance record it would produce through the same `ALERT_TASK_MAP` the
per-alert path uses; and a template matches when its sections produce a
**superset** of those records. Ties break on (extra sections, total sections,
docname), so the choice is the same on every run and every site.

No match is a first-class answer and the common one. One alert at a place is one
task, unchanged.

## Versions are pinned, and edits supersede

`update_inspection_template` does not edit the row. It writes a **new** row at
version+1, deactivates the old one and points it at the new one. A session links
the row it was worked from, so April's session is still readable in November
against the sections the worker actually saw — and a session started against v1
while v2 is being authored is untouched, because v2 is a different document.

## 228. `list_inspection_templates`

Every template with what it produces, which regimes it answers and which version
is live. Filter by `applies_to_asset_type`, `active` or `regime` — matched by
token, never substring, so a GlobalGAP template never answers a GAP question.

Superseded and inactive templates are listed too: the sessions worked from them
are still readable, and an auditor asking what last October's close-down looked
like is asking about one of those. `live_templates` is the set a new session can
start from.

## 229. `get_inspection_template`

One template in full — every section in working order with its evidence
contract, renderer hint, produced-record doctype and field prompts.

**This is what a client renders a sectioned form from.** `renderer_hint` is a
hint and not a contract: a client that does not know one falls back to a freeform
form and the submission is still valid, which is what lets a template using a
renderer added later reach a handset nobody has updated. The refusal lives in the
evidence contract, never in the renderer.

Takes a docname (one exact version) or a template name (whichever is live).

## 230. `list_inspection_sessions`

Every templated visit — who went, where, from which template and pinned version,
and which compliance records the trip produced. Filter by company, location,
worker, template, state, `visit_id` or date range.

## 231. `get_inspection_session`

One visit in full: the pinned template version with all its sections, every
section submission with what was ticked and measured, the shared evidence tray,
and the compliance record each section produced.

## 232. `create_inspection_template`

**MUTATING.** Authors a template, live immediately.

Each section names `produces_record_doctype` — `Housing Inspection`, `Detector
Test`, `Water Test` — or leaves it empty, which is a real and common answer:
nobody regulates a photograph of an emptied refrigerator as its own document. A
section naming a doctype this app cannot build is refused **here**, at authoring
time, rather than at submission time while somebody is standing in a cabin.

Refuses a template with no sections (that is a name), two sections sharing a name
(the name is the key a submission matches on), a second **live** template with a
name one already holds, and any evidence-contract key outside the vocabulary —
`photos`, `signature`, `findings_text`, `witness`, `checklist_items`,
`measurements` — because `{"photo": true}` asks for nothing and looks like it
asks for something.

```json
{"template_name": "Pre-harvest Block Walk",
 "description": "The walk a block gets before the first pick.",
 "applies_to_asset_type": "Field",
 "skill_required": "food_safety",
 "regimes": ["FSMA", "GAP"],
 "sections": [
   {"section_name": "Animal intrusion check",
    "produces_record_doctype": "",
    "renderer_hint": "multi-photo",
    "required": true,
    "evidence_contract": {"photos": true, "findings_text": true}}]}
```

## 233. `update_inspection_template`

**MUTATING.** Supersedes rather than edits — see above. Arguments left out mean
unchanged; passing `sections` replaces the whole list, because a section list
edited one entry at a time by index is a section list somebody reorders by
accident.

## 234. `deactivate_inspection_template`

**MUTATING.** Stops new sessions starting from a template and records why. It
destroys nothing: every session already worked from it stays readable, every
compliance record those sessions produced stays in the register and in the audit
packet. There is deliberately no delete.

## 235. `start_inspection_session`

**MUTATING.** Opens one visit at one place and pins the template version.

**It writes no compliance record and moves no register.** A started session has
dismissed nothing — the records are created by `submit_inspection_session` and
not before, exactly as a Draft Housing Inspection writes nothing to the camp
register.

## 236. `submit_inspection_session`

**MUTATING, and the one with teeth.** In order: sections are read off the version
the session **pinned**; a submission naming a section that version does not have
is refused; a **required** section that is missing is refused by name; each
submitted section is checked against its own evidence contract and the shortfalls
are named.

**Nothing is written if any of those refuses.** Half a visit is a set of
compliance records that look complete and are not, which is worse than no records
at all — an auditor reading them has no way to know the detector was never
tested.

**Two sections producing the same record for the same subject produce one
record.** A Detector Test carries both a smoke result and a CO result, and both
are required fields, so testing them as two sections — the right shape for a
worker who walks to one detector and then the other — must not file two records
that each assert something they were never told about the other. Both section
submissions link the one record; the trail from either is intact.

An **optional** section may be skipped and its produced-record link stays empty.
That is how a template covering more than is due today stays usable, and the skip
is recorded as something somebody said, because an empty space is not.

```json
{"name": "INSPS-2026-0001",
 "section_submissions": [
   {"section_name": "Habitability walk",
    "evidence_file_tokens": ["1a2b3c"],
    "signature_file": "9f8e7d",
    "notes": ""},
   {"section_name": "Smoke Detector Test",
    "checklist_values": {"smoke_alarm_sounds": true},
    "record_data": {"smoke_detector_result": "Pass"},
    "notes": ""},
   {"section_name": "CO Detector Test",
    "checklist_values": {"co_alarm_sounds": true},
    "record_data": {"co_detector_result": "Pass"},
    "notes": ""}]}
```

`notes` as an **empty string** records that nothing was wrong; leaving it out
records that nobody was asked, and the two are different answers. `record_data`
names fields on the produced compliance record — it is where a Water Test section
names the Irrigation Zone, which a session at a cabin cannot supply, because one
cabin can draw from several sources and this app will not guess.

A record whose findings are alarming is still filed: it routes itself to
Corrective Action Required and raises its own Critical alert, exactly as it would
from a single-task completion.

## 237. `propose_inspection_template_from_regulation`

**MUTATING (default off).** Declared in v0.21.0, **wired in v0.37.0.** Draft an
Inspection Template read off a regulation — its sections, their evidence
contracts, the compliance records they produce. It lands **inactive**, marked
`AI-proposed` with the source it was read from, and no handset fetches it until a
person approves it with `approve_inspection_template`.

**It calls no model.** The AI doing the proposing is the *client*: you read the
regulation, you draft the sections, you pass them here. The tool is the validator
and the gate — it refuses the wrong shape, stamps the provenance you do not get
to choose, lands it off, and flags what needs more than a skim.

Takes every argument `create_inspection_template` takes, plus `regulation_url`,
`regulation_section`, `regulation_text` (a short excerpt is quoted onto the
record) and `read_on`. One of the url, the section or an explicit
`ai_source_citation` is **required**: a draft that does not name the text it read
cannot be checked against it, which is the whole of what the approval does.

It will not write `active`, will not write `authored_by` as anything but
`AI-proposed` (passing `Operator` is refused, not corrected), and will not fill in
the approver or the approval date. A draft for a `template_name` that is already
live is written at version+1 and **touches nothing** — the worker starting a visit
this afternoon gets the form somebody approved.

Flags a section with an **empty evidence contract** (it can be filed empty and
still looks complete) and a draft whose approval will stand a live template down.

## 238. `approve_inspection_template`

**MUTATING (default off).** Accept an inactive template and turn it on, recording
**who** and **when** on the record itself. The counterpart to
`approve_compliance_rule`, and it exists for the same reason: a form a worker is
asked to fill in is a compliance artefact whoever wrote it.

Where a live template already holds the name at a lower version, that row is
deactivated and pointed here — superseded, never edited, so every session already
worked from it stays readable against the sections the worker actually saw.

Works on any inactive template, not only a proposed one: reinstating one somebody
withdrew is the same act and deserves the same name against it.

## 239. `get_compliance_rule`

Read-only. One rule in full: the condition it evaluates, the thresholds and scope
filters it evaluates it against, the regulation it cites, the regimes it answers
to, the kairotic gate saying what makes it ripe, and **who approved it and
when**.

Takes a docname (one exact version) or a `rule_id` such as `training_expiring`,
which resolves to whichever version is live — what somebody asking about a rule
today means. Pass the docname of a **superseded** row to read the definition an
older alert was raised under; those rows are never edited and never deleted,
which is the whole point of versioning by copy.

## 240. `test_compliance_rule`

Read-only. Runs ONE rule against the data as it stands and reports every
observation it WOULD make — with the alert docname each would take — writing
nothing.

**The tool to call between authoring a rule and approving it.** It takes the same
code path the nightly sweep takes, deliberately: a dry run with its own second
implementation is a dry run that can disagree with the real one.

What to look for: a rule that observes four hundred rows is a rule whose
condition is wrong — almost always a field that is empty everywhere rather than
stale on a few. `computation_warnings` names anything the engine worked around,
such as a scope filter on a column this site has not got.

## 241. `create_compliance_rule`

**MUTATING (default off).** Authors a new compliance rule — a condition the
nightly sweep evaluates against this site's records — with no code release. It
arrives as a **Draft** and fires nothing until `approve_compliance_rule` turns it
on.

The runtime stays deterministic and there is no model in it: a rule is a
declarative expression over record state — query `target_doctype`, apply
`scope_filters`, measure `date_field` against `cadence_days` and the thresholds,
render `message_template`. That is what lets every alert be traced to a rule, a
citation, an approver, and the specific field that crossed a threshold.

Refuses a duplicate `rule_id` (two rules sharing one collide on the alert
docname); a `rule_id` with a colon or a space (the docname is
`<rule_id>:<doctype>:<name>`); a `target_doctype` this site has not got; no
kairotic gate; a malformed scope filter or unknown operator; and any
`custom_python` the sandbox would not run.

**Since v0.22.1** the declarative vocabulary also covers a finding superseded by
a later clean record (`superseded_by_later_clean`), a second date used only as a
gate (`gate_date_field` / `gate_within_days`), several anchors of the same kind
with per-field labels (`date_fields`), an ordered lookup reading regimes or a
category off a name (`regime_heuristics`, `category_heuristics`), a date that is
a timestamp rather than a deadline (`date_field_role`), and one rule walking two
kinds of record (`target_doctypes`).

**Since v0.22.5** it also covers a rule that fires on a **data state** rather than
on any distance from a date: `latest_child_field_threshold` folds a child table
to the newest row per record and reads a number off it — against a literal, or
against a per-company setting via `threshold_source`, so the alert layer and the
operational sweep cannot disagree about what "hot" means on the same afternoon.
`date_field_role: "State"` says the rule has no clock, `default_severity` says
what it raises at instead, and `producer_assigned_to_expression` sends the
producer task to one named person (`row.foreman`) rather than into a skill pool.

Twelve of the fourteen shipped rules are now fully declarative and none uses
`custom_python` — `docs/configurable_compliance_framework.md` §4 has the table of
questions that are already fields.

## 242. `approve_compliance_rule`

**MUTATING (default off).** Accepts a rule and turns it on, recording who
accepted it and when **on the record itself**.

This is the gate, and there is no way round it: the DocType refuses `enabled`
without an approver and a date. So no rule — least of all one a model proposed —
starts firing without a person having put their name to it. Also reactivates a
rule that was disabled. Optionally attaches the approver's signature as a File.

**Since v0.37.0 it does two more things, both for AI-proposed drafts.** It refuses
a draft carrying model-written code — `custom_python`, or a producer assignee
expression — until the approver passes `accept_ai_authored_code`, and the refusal
prints the program back at them. And where the draft is a proposed *replacement*
for a rule that is already live, approving it supersedes that rule: disabled,
pointing at the new row, never edited, and every alert it raised left exactly as
it was.

## 243. `update_compliance_rule`

**MUTATING (default off).** Changes a rule by **superseding** it: a new record at
version+1, the old one disabled and pointing at it. The old row is never edited.

That is why an alert from April is still explicable in November, and why a sweep
that started against v1 finishes against v1. Arguments left out mean unchanged.
The new version inherits the old one's approval — a threshold moved is not a new
rule, and forcing re-approval on every tuning edit trains people to click through
approvals. A rule that was off stays off.

The result carries a field-by-field before → after, so the MCP Action Log row
records what the rule said **before** rather than only what was asked for.

**The most common edit this exists for**: when OR-OSHA renumbered heat illness
from -1130 to -1131, `regulation_citations` was the only thing that had to move.

## 244. `deactivate_compliance_rule`

**MUTATING (default off).** Stops a rule firing, and records why.

**It dismisses nothing.** Every alert the rule already raised stays on the
calendar exactly as it was, and the next sweep will not touch it — the same
reading a rule skipped for a missing DocType gets, and for the same reason:
switching a rule off is not evidence that anybody did the work. The result says
how many are left standing.

There is deliberately no delete. The rule stays on the site, disabled, with the
reason on the record — so the operator who asks next season why the calendar
stopped mentioning the thing that then went wrong gets an answer from the record
rather than from somebody's memory.

## 245. `propose_compliance_rule`

**MUTATING (default off).** Declared in v0.22.0, **wired in v0.37.0.** Draft a
compliance rule read off a regulation. It lands **disabled**, marked
`AI-proposed`, with the source on the record, and sits in the review queue until a
person approves it.

**It calls no model, and that is the whole design.** The AI doing the proposing is
the *client*: you read the regulation, you draft the record, you pass it here as
arguments. What the tool does is the part a proposer cannot do for itself —
refuse the wrong shape, stamp the provenance, land it off, and put what needs a
second pair of eyes where the approver will see it. A validator and a gate, not an
author.

Takes every argument `create_compliance_rule` takes, plus `regulation_url`,
`regulation_section`, `regulation_text` and `read_on`. Draft **declaratively** —
`target_doctype`, `date_field`, `cadence_days`, the thresholds, `scope_filters`,
`message_template` — because a rule that is a set of fields is a rule an approver
can check against the regulation in a minute.

**Four things it will not let you do.** Write `enabled`. Write `authored_by` as
anything but `AI-proposed` — passing `Operator` is refused rather than corrected,
because that argument is an attempt to launder provenance. Fill in the approver,
the approval date, the approver's employee or their signature. And there is no
propose-a-delete and no propose-a-disable anywhere in this app: a proposal for a
`rule_id` that already exists is drafted at version+1 and **touches nothing**, so
the live rule goes on running on its own definition until a person approves the
replacement. The result carries the field-by-field diff, because what a reviewer
of an edit needs is what changed.

**`custom_python` is flagged for extra review.** The sandbox refuses what it
refuses at authoring time; what it cannot say is whether the program asks the
right question. A draft carrying one — or carrying a producer assignee expression
— gets `ai_review_flags` on the record, and `approve_compliance_rule` refuses it
until the approver passes `accept_ai_authored_code`. The refusal prints the
program back at them: an acknowledgement of code nobody displayed is not one.

## 246. `list_regulation_feeds`

Read-only. The regulation register: every source this site watches for change,
with the URL, the regime it serves, how often it is checked, when it was last
looked at, and when it last **moved**.

**What the register is for.** v0.22.0 made a compliance rule a record and v0.37.0
let a model draft one from a regulation. Neither of those knows anything about the
regulation six months later, when OR-OSHA renumbers a subsection or a certifier
reissues a handbook. A feed is the pointer that was missing.

Read `status` as a report rather than as a setting. **Error** is what the last
check said, not a decision anybody made — the sweep retries an errored feed and a
successful check clears it back to Active. **Paused** is the decision, and it is
the only state that keeps a feed out of the sweep.

`never_checked` is the list to act on first: a source nothing is known about looks
exactly like a source that has not changed.

| Parameter | Required | Description |
|---|---|---|
| `status` | | `Active`, `Paused` or `Error` |
| `regime` | | One audit: OR-OSHA, FSMA, WPS, GAP, GlobalGAP, PrimusGFS, NOP, OTCO, Internal |
| `company` | | Company name or abbreviation |
| `due_only` | | Only feeds the sweep would check right now |
| `limit` | | Default 100, hard maximum 500 |

## 247. `get_regulation_feed`

Read-only. One source in full, **including its change log** — every change, error
and recovery it has seen, one timestamped entry each, newest first.

The change log is the only account anywhere of what a source has done over time,
and it is append-only: no entry is ever edited, and when it reaches its cap the
*oldest* lines are dropped with a line saying so. A dropped-newest log would be a
detector that had quietly switched itself off.

A `CHANGED` entry carries the hash it moved from, the hash it moved to, the size
of the normalised text, and the `rule_id` of every Compliance Rule derived from
that source. **A rule named in an entry was not touched.** The link is
informational in one direction: nothing in this app edits, disables or supersedes
a rule because a web page changed. It says where to look.

| Parameter | Required | Description |
|---|---|---|
| `name` | yes | Regulation Feed docname, or part of the feed name |
| `feed` | | Alias for `name` |
| `log_limit` | | Change log entries to return, newest first |

## 248. `list_regulation_changes`

Read-only. Which regulations have moved since a date, and which compliance rules
were written from them.

**The question this whole surface exists to answer** — *what regulations moved
since our last compliance review* — and the tool a quarterly review opens with. It
is a filter on `last_change_detected` and nothing cleverer: the fact was recorded
when it happened, by the sweep, so answering it later costs one query and no
network at all.

`rules_to_review` is a **reading list, not a changelog of your calendar.** Every
rule named there is still running on exactly the definition a person approved.
Where one genuinely needs to change, read the source and use
`propose_compliance_rule` — the draft lands disabled with its citation on it, and
`approve_compliance_rule` is where a name goes on the replacement.

| Parameter | Required | Description |
|---|---|---|
| `since` | | `YYYY-MM-DD`. Default 90 days ago |
| `regime` | | Only sources for one audit |
| `company` | | Company name or abbreviation |
| `limit` | | Default 100, hard maximum 500 |

## 249. `create_regulation_feed`

**MUTATING (default off).** Register a regulatory source — a URL, the regime it
serves, what it covers and how often to look — so the site notices when the
regulation moves. It writes a pointer and fetches nothing until a check runs.

**Point it at the narrowest page that carries the rule**: a division of the
rulebook rather than the rulebook's index, a specific Federal Register document
rather than the search that found it. A broad page changes for reasons that have
nothing to do with this operation, and every one of those is a person asked to
read a regulation for nothing.

`affected_rules` is the link back to the rules this source produced, by docname or
by `rule_id`. Informational in one direction only: a detected change names those
rules in the log so a reader knows where to look, and **nothing in this app edits,
disables or supersedes a rule because a page changed.**

Refuses a feed name already on the site (the name is the docname); a URL that is
not `http(s)`, because that field is handed to an outbound request by a scheduled
job; a description shorter than a sentence, because the description is what
somebody reads when the log says this moved; a regime the vocabulary does not
hold; and an `affected_rules` entry that resolves to no Compliance Rule.

| Parameter | Required | Description |
|---|---|---|
| `feed_name` | yes | The docname. Name it after the regulation, not the website |
| `url` | yes | The `http(s)` URL that is checked |
| `description` | yes | What is at that URL, and what on this operation turns on it |
| `regime` | | The audit this source answers to |
| `check_frequency` | | `Daily`, `Weekly` (default) or `Monthly` |
| `status` | | `Active` (default) or `Paused`. `Error` cannot be set by hand |
| `company` | | Company name or abbreviation |
| `affected_rules` | | Compliance Rules written from this source, by docname or `rule_id` |

## 250. `update_regulation_feed`

**MUTATING (default off).** Edit a source's URL, description, regime, frequency,
status or rule links. Pausing one here is the kill switch: a paused feed is
skipped by the sweep and keeps its whole change log.

**It cannot write the detector's own memory.** `last_content_hash`,
`last_checked`, `last_change_detected` and `change_log` are *refused* as arguments
rather than ignored: a hash somebody typed is a change that will never be
reported, and a change log somebody edited is the one record here whose entire
value is that nobody edited it.

**Changing the URL clears the stored hash**, and logs that it did. A hash taken
over one page says nothing about another, so leaving it would make the next check
report a change that is really a change of subject.

| Parameter | Required | Description |
|---|---|---|
| `name` | yes | Regulation Feed docname, or part of the feed name |
| `url` | | A new `http(s)` URL. Clears the stored hash |
| `description` | | What this source covers |
| `regime` | | The audit this source answers to |
| `check_frequency` | | `Daily`, `Weekly` or `Monthly` |
| `status` | | `Active` or `Paused` |
| `company` | | Company name or abbreviation |
| `affected_rules` | | Replaces the whole set of linked rules |

## 251. `check_regulation_feed`

**MUTATING (default off), and it makes an outbound request.** Fetch one source now
and say whether its content changed since the last check.

**It detects and it does not remediate,** and that line is the design rather than
a limitation. A changed page is evidence that somebody should read a regulation
again; it is not evidence about what the regulation now says, and it is not
authority to rewrite a rule firing on somebody's compliance calendar. So a change
writes a hash, a timestamp and a log line naming the rules derived from that
source, **and stops.**

**The hash is of normalised text, not of the bytes** — tags, scripts, comments,
entity escapes, ISO and US and month-name dates, clock times and long hex strings
taken out, whitespace collapsed — because a page that stamps itself with the
minute it was served would otherwise report a change on every check, and a
detector that always fires detects nothing. **The cost is real and stated: a
change that is *only* a date is invisible to it.**

The first check is a **baseline** and cannot be a change, because there is nothing
to compare against. A fetch that fails sets the feed to `Error` with the message
and **does not move `last_checked`**, so the next sweep retries rather than
waiting out the whole frequency.

| Parameter | Required | Description |
|---|---|---|
| `name` | yes | Regulation Feed docname, or part of the feed name |
| `force` | | Check even a Paused feed. Default false |

## 252. `check_all_regulation_feeds`

**MUTATING (default off), and it makes several outbound requests.** Run the sweep
now: every source that is not Paused and is older than its own `check_frequency`.
Returns which ones **moved** and which could not be reached.

The same function the daily scheduler calls, with the same due logic —
deliberately, because a manual sweep with a second implementation is one that can
disagree with the nightly one. One source's failure is one source's failure: an
agency site behind a WAF does not stop the other eleven being checked, and nothing
here raises.

`force` checks every unpaused feed regardless of when it was last looked at —
right before a certification audit, and rude to a public server nobody is paying
for as a habit.

| Parameter | Required | Description |
|---|---|---|
| `company` | | Company name or abbreviation |
| `force` | | Ignore each feed's frequency. Default false |

---

# The Financial KPI Framework (v0.39.0)

**v0.19.6 made the window standard generalize across three *shipped* reports.
This makes the KPI itself a record.**

Before this release, adding a KPI meant a Python function, a registration call, a
test, a review, a release and a deploy — a perfectly good process for a KPI this
app's authors chose, and no process at all for a KPI somebody's lender asked
about on a Tuesday. Every operation has two or three ratios that are genuinely
its own: a packing house watches cost per bin, an operation carrying an equipment
note watches debt service coverage on the *covenant's* definition and not on
anybody else's. None of those belong in a shipped app and all of them belong on
the dashboard of the farm that needs them.

So a KPI is now a `Financial KPI Definition`, and the seven tools below author
and run one.

**The engine does not move.** `formula_type` has exactly two values and both are
deterministic. `Built-in` delegates to a computer that ships with this app,
reviewed like any other code, while the record still owns the window, the step,
the lookback, the thresholds, the dashboard order and the switch. `Expression`
evaluates arithmetic over named inputs in a sandbox that parses to an AST and
checks every node against an allowlist. **There is no third value and there is no
field on the record that holds Python** — which is a deliberate difference from
`Compliance Rule.custom_python`, because a compliance rule can need to express a
shape no set of fields captures, and a financial KPI is a number divided by
another number.

**A definition holds the question and never an answer.** Every figure is computed
from the ledger when somebody asks, cached in `Financial KPI History` *with the
components that produced it*, and derivable again by rerunning the same
computation. Nothing on a definition is a number that came out of the books.

**`kpi_id` is the cache key, so it is unique and it cannot move.** A Compliance
Rule is versioned by copy — an alert raised in April can still be read against
the definition that raised it. A KPI is a *line*, and a line assembled from two
definitions of one number is a chart with an unmarked join in it. Changing the
arithmetic of a live KPI is a new `kpi_id` beside the old one.

**Thresholds go to the compliance calendar, not to a second alerting system.** A
KPI past its critical threshold raises a `Compliance Alert` under the new
`Finance` category, through the same sweep, with the same dismissal, the same
snooze and the same auto-clear when the value comes back inside. An operation
with two alerting systems reads neither. The threshold scan **reads the cache and
never computes**: the alert sweep runs hourly beside somebody's real work, so it
reads the newest cached snapshot — which is the same figure the dashboard is
showing, so an alert and a dashboard can never disagree about the number.

**The overnight refresh is at 03:00**, between the shipped-report sweep at 02:00
and the regulation feed at 04:00. It is *one job that iterates*, which here is
load-bearing rather than tidy: the whole point of the release is that an operator
adds a KPI without a code release, and a KPI that needed its own scheduler entry
would be one they could not add. It shares `enable_kpi_history_sweep` with the
02:00 job rather than getting a second checkbox.

## 253. `create_financial_kpi_definition`

**MUTATING (default OFF).** `kpi_id` and `title` required; everything else
optional.

| Parameter | Required | Description |
|---|---|---|
| `kpi_id` | yes | Lower-case letters, digits and underscores. The cache key — it cannot be changed later |
| `title` | yes | What it is called on a dashboard |
| `description` | | What it means and which direction is good |
| `category` | | Profitability, Liquidity, Leverage, Efficiency, Operational or Custom (default) |
| `unit` | | Currency (default), Percentage, Ratio, Days, Acres or Units |
| `formula_type` | | `Built-in` (default) or `Expression` |
| `builtin_function` | | For Built-in: `sustainable_cf_per_acre`, `ocf` or `revenue` |
| `expression` | | For Expression: the arithmetic over the input variable names |
| `expression_inputs` | | For Expression: a JSON object mapping each variable to its source |
| `company` | | One entity, or **empty for every company** — the ordinary case |
| `enabled` | | Default true |
| `default_window_type` | | Snapshot, TTM (default), MTD, QTD, YTD, Custom |
| `default_window_months` | | Default 12 |
| `default_computation_step` | | Daily, Weekly, Monthly (default), Quarterly, Yearly |
| `historical_averaging_enabled` | | Default true |
| `historical_lookback_years` | | Default 5, maximum 10 |
| `threshold_warning_low` | | Warning at or below. **Omit where low is not bad** |
| `threshold_critical_low` | | Critical at or below. Must be at or below the warning floor |
| `threshold_warning_high` | | Warning at or above |
| `threshold_critical_high` | | Critical at or above. Must be at or above the warning ceiling |
| `dashboard_visible` | | Default true |
| `display_order` | | Position within the category, lowest first |

**`expression_inputs` has four sources.**

```json
{
  "current_assets":      {"source": "gl", "root_type": "Asset", "balance": true},
  "current_liabilities": {"source": "gl", "root_type": "Liability", "balance": true},
  "sales":               {"source": "report", "report_name": "revenue", "path": "total"},
  "cf_per_acre":         {"source": "kpi", "kpi_id": "sustainable_cf_per_acre"},
  "sqft_per_acre":       {"source": "constant", "value": 43560}
}
```

`gl` sums ledger movement over the window; narrow it with `root_type`,
`account_type`, `accounts` or `account_number_prefix`. **`"balance": true` makes
it a position at the window's end rather than a movement across it** — a current
ratio built from twelve months of movement in a cash account is not a current
ratio, it is a cash flow with a ratio's name on it. `report` reads a component
off a shipped computer. `kpi` is another definition's value, with cycles refused
at save time. `constant` is a number with a name, which is what a magic number in
a formula should always have been.

**What the expression grammar allows:** `+ - * / // % **` on numbers and variable
names, unary minus, parentheses, comparisons inside a conditional, and calls to
`min`, `max`, `abs`, `round`. **What it refuses, by name, at save time:** imports,
attribute access, subscripts, lambdas, comprehensions, assignment, string
constants, and any call to anything else. A division by zero is not an error — it
is a null value with a warning naming the variables that were zero, because a
farm with no acres in production has no cash flow per acre and a zero there would
be read as one.

**Enabled on creation**, unlike a Compliance Rule, and the difference is what the
two do when wrong: a rule that fires wrongly accuses somebody of a compliance
failure; a KPI that is wrong reports a number beside its own ingredients and its
own warnings, which a reader can check.

## 254. `update_financial_kpi_definition`

**MUTATING (default OFF).** `kpi_id` required; every field above optional.

**It edits rather than superseding.** See the note above on why a KPI is not
versioned by copy. `kpi_id` cannot be changed at all — renaming it orphans the
whole cached series.

**Changing the arithmetic is reported as a decision**, with the cached row count
in front of you. The usual right move is a new `kpi_id` beside the old one; where
it is genuinely a correction rather than a redefinition,
`refresh_kpi_cache(force=true)` rebuilds the whole series under the new formula
so the line holds one definition again.

The result carries the field-by-field diff **with the previous values**, so the
MCP Action Log row answers "who changed this and what did it say before" without
anybody reading a git history they have no access to.

## 255. `list_financial_kpi_definitions`

**Read-only.** Optional `company`, `category`, `formula_type`, `enabled`,
`dashboard_only`, `limit`.

The register: what this site computes, how, and what it alerts on.
`builtin_functions_available` names the shipped computers a Built-in definition
may point at.

**`thresholded_count` is the number worth reading first.** A KPI with no
thresholds is one nothing is watching for anybody, and it can never appear in
`compute_all_kpis`'s `breached` list however bad it gets.

## 256. `get_financial_kpi_definition`

**Read-only.** `kpi_id` (by kpi_id or docname) required.

One definition in full, with how much history is cached under it and whether it
would compute at all. **`problems` is the field to read:** a Built-in naming a
computer this site has not got, an expression that no longer parses, an input the
expression never reads — each produces nothing at compute time and says so in a
warning rather than reporting a zero, and this is where to see it before somebody
quotes the KPI.

## 257. `compute_kpi`

**Read-only.** `kpi_id` and `company` required; optional `as_of`, `window_type`,
`window_months`, `computation_step`, `historical_lookback_years`,
`include_historical_averages`.

**It goes through the same window standard `get_windowed_report` does**, so a KPI
somebody typed into a form this morning and the one that shipped in v0.19.5
behave identically at the fiscal year boundary, on a partial ledger, and against
the cache. **The window comes from the definition by default**, which is what
keeps a dashboard, its alerts and its cache agreeing without anybody passing
anything.

**Four blocks.** `point_in_time` is the period just finished, which on a farm
flatters harvest and demonizes pruning. `window` is the rolling figure with its
components — for an Expression KPI, every input with what it matched, how many
accounts, how many entries, and whether it was read as a balance or a movement.
`historical_averages` is what that window has been worth before. `threshold_status`
is where the value sits against the lines somebody drew, and **`No thresholds`
means nobody drew any**, which is not the same as being inside them.

**A null value is an answer.** A ratio whose denominator was zero is a division
nobody performed. Read `computation_warnings` before quoting anything.

**It warms a cache and that is the one thing it writes:** no Account, no GL Entry,
no Journal Entry, no Asset, no Field.

## 258. `compute_all_kpis`

**Read-only.** `company` required; optional `dashboard_only`, `category`,
`as_of`, the window overrides, `include_historical_averages`.

The whole financial dashboard in one call. **One call rather than N**, for the
reason `get_windowed_report` is one tool rather than one per report: a framework
whose every KPI costs a round trip is a framework with six KPIs in it.

**One broken definition does not empty the dashboard.** Each KPI is computed
independently and a failure becomes a null value with a warning on that row — the
same promise the compliance sweep makes about one rule that throws.

**Read `breached` first and `unwatched_note` second.** An empty `breached` list is
not a healthy operation: a KPI with no thresholds can never appear there, and the
note says which ones those are.

`include_historical_averages=false` across the board answers much faster on a
dashboard with many KPIs.

## 259. `refresh_kpi_cache`

**MUTATING (default OFF).** Optional `kpi_id` (omit for every enabled
definition), `company`, `back_years` (default 5), `force` (default false).

**The only thing it can change is a cache** — the same promise
`recompute_kpi_history` makes, extended to KPIs that are records rather than
code. Every row it writes is what the live computation would have produced for
that window; every row it deletes comes back on the next read or the next
overnight run.

**It is the answer to a changed formula**, and to a KPI created this morning,
which has no history at all until this runs or until the 03:00 job reaches it —
and a chart with one point on it is not a trend.

`recompute_kpi_history` will also take a `kpi_key` that names a definition and
delegates here, so a caller who already knows that tool does not have to learn
this one.

---

## v0.26.0 — field-initiated task creation from asset scan

Worker scans an asset's QR tag and taps "Flag needs repair" to create a Farm Task
linked to the asset, with skill and location auto-filled from the asset type.

### `report_asset_issue`

**MUTATING (default OFF).** Convenience wrapper: report a problem on a specific
asset. Looks up the asset, auto-fills `skill_required` from the asset type
(Housing Unit → camp_maintenance, Irrigation Valve → irrigation, etc.), then
creates a Farm Task linked to the asset.

Delegates to `report_field_task` under the hood — same anti-spam, same photo
requirement, same urgency cap. The difference is the caller names an asset
instead of manually providing location and skill.

| Parameter | Required | Description |
|---|---|---|
| `asset_name` | yes | Asset Register docname from the QR/NFC tag |
| `reported_by` | yes | Employee id of the reporting worker |
| `photo_file_token` | yes | File docname from `finalize_staged_file` |
| `description` | | What the problem is |
| `urgency` | | Normal or High (Critical restricted to Foreman/Manager) |
| `task_type` | | Default Repair |
| `skill_required` | | Override the auto-mapped skill |
| `gps_lat` / `gps_lon` | | GPS coordinates |
| `company` | | Defaults to the asset's company |

Also in this release:

- `report_field_task` gains an optional `asset` parameter to link a task to an asset
- `scan_asset` response includes `can_report` and `suggested_skill`
- Farm Task doctype gains an `asset` Link field to Asset Register
- Tasks linked to an asset appear in `get_asset_detail`'s history timeline

---

## v0.31.0 — expense receipt capture

A foreman photographs a receipt at the fuel pump or the parts counter, iOS Vision
OCR reads the merchant, the total and the date off it **on the device**, and the
phone posts the extracted fields, the image and the raw OCR text here in one call.

The extraction runs on the phone on purpose. The photograph is the largest thing
in the payload and the extraction is the cheapest part of the job; doing it there
means the foreman sees what the machine read before they put the phone away and
can correct it while the paper is still in their hand. By the time these tools see
a receipt, a person has already looked at the reading.

Two DocTypes: **Expense Receipt** (the register) and **Expense Receipt Item** (the
line detail, a child table).

### `list_expense_receipts`

**READ (default ON).** Receipts filtered by `status`, `employee`,
`company`, `category`, `farm_task` and a `from_date`/`to_date` range on the
receipt date. Returns each receipt's extracted fields, its image URL and the
scanner's confidence, plus `count` and `total_amount` for everything that matched.

**Ordered lowest OCR confidence first.** The receipt nobody can read is the one
somebody has to open the photo for; sorting it last would put it where it is never
looked at.

| Parameter | Required | Description |
|---|---|---|
| `company` | | Company name or abbreviation |
| `status` | | `Draft`, `Submitted`, `Approved` or `Rejected` |
| `employee` | | Who submitted it — docname or employee name (`submitted_by` is an alias) |
| `category` | | `Fuel`, `Equipment Parts`, `Supplies`, `Hardware`, `Feed`, `Seed`, `Fertilizer`, `Other` |
| `farm_task` | | Only the receipts booked against one task |
| `supplier` | | Only the receipts **linked** to one Supplier (v0.67.0) |
| `from_date` / `to_date` | | Receipt date range, `YYYY-MM-DD` |
| `limit` | | Default 100, hard maximum 500 |

### `get_expense_receipt`

**READ (default ON).** One receipt in full: the extracted fields, the photograph,
the line items, `ocr_raw_text` — everything the scanner read, unedited — and the
review trail: who approved or rejected it, when, and on what grounds. `items_total`
is the sum of the lines and is **not** expected to equal `amount`.

| Parameter | Required | Description |
|---|---|---|
| `name` | yes | Expense Receipt docname (`expense_receipt`, `receipt` are aliases) |

### `submit_expense_receipt`

**MUTATING (default OFF).** Capture one expense. Creates the receipt as
`Submitted`; pass `status: "Draft"` for a client holding an offline queue.
Approval and rejection are separate tools with separate switches, so this call
cannot create an already-approved receipt.

| Parameter | Required | Description |
|---|---|---|
| `merchant` | yes | The vendor as it reads on the receipt |
| `amount` | yes | The receipt total, including tax |
| `receipt_date` | yes | `YYYY-MM-DD` |
| `submitted_by` | yes | Employee who photographed it — docname or name (`employee` is an alias) |
| `company` | | Required on a multi-company site |
| `category` | | Defaults to `Other` |
| `supplier` | | The Supplier this receipt is from, where the merchant is one you keep a record for (v0.67.0) |
| `farm_task` | | The job the expense was incurred for |
| `status` | | `Draft` or `Submitted`. Defaults to `Submitted` |
| `receipt_image` | | File URL of the photograph |
| `ocr_raw_text` | | Everything the scanner read, kept for audit |
| `ocr_confidence` | | A **fraction from 0 to 1**, not a percentage |
| `items` | | `[{description, item, quantity, unit_price, line_total}, …]` |
| `notes` | | Anything the person capturing it wants to add |
| `card_last_four` | | v0.75.0. The **last four** digits of the card. More than four is refused, never truncated |
| `merchant_phone` | | v0.75.0. The phone number on the slip, in any format |
| `merchant_url` | | v0.75.0. The domain on the slip, with or without a scheme |
| `store_number` | | v0.75.0. The store or location number |
| `resolved_merchant` | | v0.75.0. The caller's own answer, which short-circuits the cascade. Needs `resolution_method` |
| `resolution_method` | | v0.75.0. `OCR`, `URL`, `Phone`, `Alias`, `LLM` or `Manual` |
| `resolution_confidence` | | v0.75.0. A **fraction from 0 to 1**. Defaults to 0.95 |

`ocr_confidence` is range-checked rather than rescaled. A scanner reporting `87`
meant `0.87` and is refused with that sentence — silently dividing by 100 would
put a guessed number in the field an approver uses to decide what to look at, and
`1.4` is genuinely ambiguous between a percentage and a bug.

A line item with a `quantity` and a `unit_price` but no `line_total` gets the
product; one that carries its own total keeps it. OCR reads a bold receipt total
far more reliably than a column of line arithmetic, and a receipt that charges
four at $3 and totals $11.50 after a discount is telling the truth.

**v0.67.0: `supplier` and `items[].item`.** Both optional, both **additive**.
`merchant` and `description` keep saying exactly what the paper said; the links
sit beside them. That is the point — a slip printed `VALLEY CO-OP #14` and a
Supplier called `Valley Co-operative` are the same vendor, and replacing the
first with the second loses the evidence in the act of improving the data.
Keeping both is what lets a bookkeeper total a year of fuel per vendor **and**
still show an auditor the string the machine read.

Neither is ever inferred. No fuzzy match from merchant to Supplier, no lookup
from an OCR'd line to an Item — `HYD HOSE 1/2` matches four items in a real
catalogue, and a guess would put a fabricated consumption figure somewhere a
person later reads as a measurement. On a bench with no ERPNext, both are
**refused by name** rather than written as dangling links, and the receipt
captures fine without them.

**v0.75.0: multi-vector resolution runs on every capture.** The merchant line
is no longer the only evidence on the paper. Send the four signals above — or
just `ocr_raw_text`, and they are read off it by anchored patterns — and the
five-step cascade under `normalize_merchant` runs, writing its answer to
`resolved_merchant`, `resolution_method` and `resolution_confidence`. The
result carries `resolution_steps`, which says what every step found *and what
it did not*, plus `signals_from_raw_text[]` naming which signals this app read
rather than the caller sent (worth different amounts of trust when one turns
out wrong).

**`merchant` is never overwritten by `resolved_merchant`.** Two columns, for
ever: the first is what the scanner read and the second is what it means, and
collapsing them would delete the only evidence anybody could use to find out
the conclusion was wrong.

**Nothing is still inferred, with exactly one exception.** An exact **alias**
hit sets the `supplier` link, because it is not an inference — it is a link a
person already made for this exact spelling, replayed, and the call says so
under `supplier_resolved_by`. A domain match, a phone match, a name score and a
caller's own LLM answer all stop at `resolved_merchant` and leave the link to a
human.

**The patterns are anchored, and the refusals are the point.** A bare
four-digit run is never read as a card — a receipt is full of times, totals and
item codes — and a payment-processor, survey or social domain is never read as
the merchant, because it identifies the till's supplier rather than the shop.

**This app makes no model call, ever.** When the four deterministic steps are
silent, the result carries `llm_context`: the signals off the paper, the
candidate Suppliers and the question, for a caller that has a model. Answer it
and hand the answer back as `resolved_merchant` with
`resolution_method: "LLM"`. Same contract `validate_document_extraction`
follows, and for the same reason — a step that quietly dialled an API would put
a network call, a key and a bill inside a `bench migrate`.

A site whose Expense Receipt lacks the seven Custom Fields still **runs and
reports** the resolution; only the storing of it is skipped, and
`intelligence_fields_installed: false` says so rather than pretending.

### `approve_expense_receipt`

**MUTATING (default OFF).** Sets `status` to `Approved` and records
`approved_by` and `approved_date`.

| Parameter | Required | Description |
|---|---|---|
| `name` | yes | Expense Receipt docname |
| `approved_by` | yes | Approving Employee — docname or name |
| `approved_date` | | `YYYY-MM-DD`. Defaults to today |

### `reject_expense_receipt`

**MUTATING (default OFF).** Sets `status` to `Rejected` and records
`rejected_by`, `rejected_date` and `rejection_reason`.

| Parameter | Required | Description |
|---|---|---|
| `name` | yes | Expense Receipt docname |
| `reason` | yes | Why it was refused (`rejection_reason` is an alias) |
| `rejected_by` | yes | Rejecting Employee — docname or name |
| `rejected_date` | | `YYYY-MM-DD`. Defaults to today |

The reason is required and is stored **on the record**, not in a comment, so
`get_expense_receipt` returns it to the phone that submitted the thing. A
rejection with no sentence beside it is the state that generates the next three
messages asking why.

**Neither decision can be taken twice.** Only a `Draft` or `Submitted` receipt can
be approved or rejected — deciding an already-decided one would overwrite the name
and date of whoever decided it first, which is the one thing an approval record
exists to preserve.

---

## v0.32.0 — geo map views and crew tracking

Two halves of one idea: **the geography this app has been storing since v0.12.0
becomes something a person can look at, and a shift stops being a place and
starts being a path.**

### The map

A Leaflet map is injected into seven Desk forms through `doctype_js`, all seven
of them doctypes this app created.

| Form | What it draws |
| --- | --- |
| `Parcel` | its own outline |
| `Field` | its boundary, over its parcel's |
| `Irrigation Zone` | its boundary, over the block it waters |
| `Housing Unit` | a marker, over its parcel's outline |
| `Asset Register` | a marker at `gps_latitude` / `gps_longitude` |
| `Farm Task` | the location resolved through whichever register `location_doctype` names |
| `Farm Shift` | the shift's anchor, plus the crew's track as a coloured polyline |

**Drawing the containing shape underneath is the point of the whole widget.** The
boundary tools *report* containment and never enforce it, and a disagreement
reported in a warning string is a disagreement nobody pictures. Drawn, the
difference between "that is the corner we always farmed across" and "two vertices
are in the wrong order" takes a second to see.

**Nothing on any of these forms writes.** There is no drag-to-move marker, no
draw-a-polygon tool and no save path of any kind. A boundary is compliance
evidence and it is set through the three boundary tools, which validate the shape,
refuse a self-intersection, compare the area against the recorded acreage and
recompute every derived field. *(v0.33.0 added a draw tool to the three
boundary-carrying forms. The sentence after the full stop is what mattered and is
unchanged — see that release's section below.)*

**The library comes from a CDN and the tiles from OpenStreetMap**, so a bench with
no outbound internet gets no map. That is handled rather than left to fail: the
section says the library could not be reached and prints the coordinates
underneath. The record is the coordinates; the map is a reading of them.

`doctype_js` was a **forbidden hook** until this release, spelled "this app adds
no client script to a doctype it does not own" — and the clause after the comma
was always the real rule. `test_hooks.py` now asserts that every doctype named is
one this app shipped, that every file named exists, and that the shared widget is
listed first in every entry (Frappe concatenates them in order, and a widget
listed second is a `ReferenceError` that takes the whole form script down).

### The track

**Shift Location Log** — one GPS fix, taken during a shift, at a time. Standalone
rather than a child table of the shift: a nine-hour shift at a fix every two
minutes is two hundred and seventy rows, and a child table is loaded whole every
time anybody opens the shift form.

- **210a `log_shift_location`** (write, default OFF) — what the phone posts.
- **210b `get_shift_track`** (read, default ON) — what the map draws.

See those sections above for the ordering rule, the gap reporting and why an
open shift is not required.

### The parcel outline

**118a `set_parcel_boundary`** (write, default OFF) closes the gap
`set_field_boundary` had been apologising for on every call since v0.12.0. Parcel
now carries the same derived suite Field and Irrigation Zone do, and
`set_field_boundary` returns `boundary_contained_in_parcel` — `true`, `false`, or
`null` where the parcel has no shape to check against.

**Housing Unit gains `gps_latitude` and `gps_longitude`**, accepted by
`create_housing_unit` and `update_housing_unit`, and returned as a `gps` object
(or `null` — never `{lat: 0, lon: 0}`, because null island is a real place and it
is what an unset Float pair looks like). The pair moves together or not at all:
passing one is filled in from the stored other, and a genuine half-pair is
refused.

---

## v0.33.0 — drawing a boundary, and importing one from the county

**NO NEW MCP TOOLS.** The tool count is unchanged and every number above still
holds. What this release adds is a second *caller* of three tools that already
existed, reached from a Desk form rather than from the model — so it is
documented here rather than in the catalogue proper.

### Two whitelisted methods, and neither is on the MCP surface

| Method | What it does |
| --- | --- |
| `erpnext_mcp.api.gis.save_boundary` | takes a drawn or imported polygon and calls `set_parcel_boundary`, `set_field_boundary` or `set_zone_boundary` |
| `erpnext_mcp.api.gis.query_county_parcels` | asks a county's ArcGIS parcel layer for a shape, by tax lot number, by assessor's account number or by a point |

They are reached at `/api/method/erpnext_mcp.api.gis.<name>` by a signed-in Desk
user. `security.authorize()` — the master switch, the shared `X-MCP-Token`, the
CIDR allowlist — does not run on that path and cannot, so the gate is rebuilt in
`api/gis.py`: a named user, `frappe.has_permission(doctype, "write", doc=name,
throw=True)` on the **specific** document, and a closed list of three doctypes
with no dispatcher and no method-name argument.

That permission check is the one that would have been easy to skip. The boundary
tools end in `doc.save(ignore_permissions=True)` — correct for them, because the
MCP transport authorised three layers earlier — so a wrapper that trusted the
framework would have handed every signed-in account a write to every parcel.

**The `allow_<tool>` switches are deliberately not consulted**, the same call
`api/__init__.py` made for the phone. Those switches are the model's leash;
`allow_set_parcel_boundary` off means "the model may not redraw the farm", and
reading it here would mean an operator who distrusts the AI also loses the
ability to trace a parcel by hand.

### The draw tool does not go round anything

Parcel, Field and Irrigation Zone get a Leaflet.draw toolbar — polygon, rectangle,
edit, delete — and a **Save Boundary** button. Nothing is written until it is
pressed, and what it presses is the same boundary tool the AI calls: the polygon
is parsed, a self-intersection is refused, the enclosed area is compared against
the recorded acreage and a disagreement past a quarter is **refused outright**,
containment against the shape above is reported, and every derived field is
recomputed. A vertex nudged by accident gets an area disagreement on screen, not
a quiet save.

Two shapes drawn on one record become a `MultiPolygon` rather than a refusal — a
parcel cut in half by a county road is two pieces of one parcel.

**No area is computed in the browser.** Leaflet.draw offers a live acreage readout
while you drag; taking it would put a second area implementation in the app, in a
different language, and the day it disagreed with `geo.area_acres` by three per
cent nobody would know which figure the compliance record was built on. The
server answers with the area it actually stored.

The other four map forms — Housing Unit, Asset Register, Farm Shift, Farm Task —
stay exactly as read-only as they were. None of them carries a shape anybody
should be redrawing from a form.

### Satellite is the default layer

Esri World Imagery (free, no key) with OpenStreetMap one click away in the layer
control. A street map cannot be traced against: an orchard block's corner is a
change in canopy, a headland or a road edge, none of which is on a street map,
which for most of this county draws two roads and a lot of white. Both
attributions are the condition of use rather than a courtesy.

### The county import, on Parcel and no other form

Wasco County publishes its tax lots as an ArcGIS FeatureServer — free, no key,
WGS84 on request (`outSR=4326`; the layer's native grid is Oregon Stateplane North
in feet, WKID 2913). That polygon is what the assessor, the deed and the tax bill
are all describing, which makes it a far better starting point than tracing an
outline off a satellite image by eye.

Three ways to ask: type a **tax lot number** (`2N 11E 1 CC 4039`, or the compact
spelling off a deed, `2N11E35BA-01600`), type the assessor's **account number**
(`7503`), or press **Find Under a Point** and click the parcel on the satellite
map. One per search — asking two together would hide which one matched. Either
way the result is **drawn on the map first**, dashed, next to the block the
operator already knows — because a tax lot number typed with one character wrong
returns a real parcel somewhere else and every number on it looks plausible.
**Apply** is what commits it, through `save_boundary` like everything else.

**The account number is the search that cannot be mistyped into a different
farm.** A tax lot is five fields in a fixed order; the account number is four
digits off the top of a tax statement and the layer's own integer key. It is
printed in the preview beside the tax lot, the taxpayer and both acreages —
nothing on the Parcel form holds it, so the preview and the import summary are
where it lives.

**The request is proxied by the server, never made by the browser.** CORS is not
ours to promise, the URL belongs in one place rather than in a cached JavaScript
file, and the `where` clause is a query language that the browser is the wrong
place to be careful about. The tax lot is checked against an **allowlist** —
each part matched against digits and a known letter — rather than escaped,
because escaping means implementing somebody else's SQL dialect correctly without
being able to test it. The account number never becomes a string at all: it is
parsed to an `int` and formatted from the `int`, into an unquoted
`AccountNum=7503`, because `AccountNum` is an integer column on the layer.

**The county does not store the spelling anybody types, and v0.126.0 is where
that was found.** `MapTaxlot` on Wasco's server is space-delimited and unpadded
— `2N 11E 1 CC 4039` — while every deed and tax bill uses the compact ORMAP
spelling. v0.33.0 sent the compact one, so `MapTaxlot='2N11E35BA-01600'` matched
nothing, ever, and an ArcGIS query that matches nothing is an HTTP 200 with an
empty feature list: the form reported "no parcel matching that tax lot number"
for every tax lot in the county. The old allowlist refused a space, so the
county's own spelling could not be typed in either. Both are now translated to
the one the server will match. An unpadded run-together value like `2N11E7200`
is refused rather than guessed — it could be section 7 lot 200 or section 72 lot
00, and both are real parcels.

`x` is longitude and `y` is latitude in an ArcGIS point geometry, which is the
opposite order from every other pair in this app. Swapping them asks about a point
in the Southern Ocean, which comes back **empty rather than wrong** — so nothing
would ever say what happened. There is a test that reads the outgoing parameters.

**An ArcGIS error is an HTTP 200.** The service answers `{"error": {...}}` with a
200 and a JSON content type, so a client that checked only the status code would
report a malformed query as "the county has never heard of your parcel". It is
checked by name, first.

### What the import fills in, and what it refuses to

An empty field being filled is not an overwrite; a field that already has a value
is left exactly where it is and the difference is reported.

| Form field | From the county |
| --- | --- |
| `parcel_id` | `MapTaxlot` — filled if blank |
| `acreage` | `CalculatedAcres` — filled if blank |
| `county` | the service's own label, trimmed to "Wasco" |
| `title_holder` | **never**. `Taxpayer` is free text off a tax roll; `title_holder` is a Link to a Related Party on this site, and matching one to the other by string is how a parcel ends up owned by the wrong entity in an accounting system. It is shown and left to a person. |

Both acreages are always reported side by side — the county's, computed on its own
projected grid, and this app's, computed spherically from the same polygon. They
agree to a fraction of a per cent when the import is right, and a reader who can
see both can tell a projection difference from the wrong parcel.

### Degradation

`requests` is imported defensively, like shapely, h3 and segno before it: a bench
without it loses the county lookup **by name**, with the pip command, rather than
failing to import the module and taking `save_boundary` down with it. Drawing a
boundary by hand needs no network at all. Leaflet, Leaflet.draw and both
stylesheets are still fetched from a CDN when a form that needs them is opened —
there is no `app_include_js` or `app_include_css`, so the draw plugin is not
fetched at all on the four read-only map forms.

---

## v0.34.0 — tax form generators

Three releases put the arithmetic in place: federal withholding (v0.28.0), the
Oregon and Washington engines (v0.29.0), salary structures and the payroll engine
(v0.30.0). These turn a year or a quarter of it into the six forms an
agricultural employer in those two states actually files.

| Form | Scope | Agency | Due |
| --- | --- | --- | --- |
| `W-2` | one employee, one calendar year | IRS | 31 January |
| `1099-NEC` | one contractor, one calendar year | IRS | 31 January |
| `941` | one company, one quarter | IRS | 30 Apr / 31 Jul / 31 Oct / **31 Jan** |
| `OR-WR` | one company, one year | Oregon DOR | 31 January |
| `OQ` | one company, one quarter | Oregon DOR / OED | as 941 |
| `WA-ESD` | one company, one quarter | WA Employment Security | as 941 |

**Nothing here files anything.** No transmission, no official scannable Copy A,
no deposit schedule. What comes out is box and line values with the inputs that
made them; a person reads them onto the real form or into the agency's portal.

### The generators are pure

`erpnext_mcp/form_generators.py` reads no database and has no side effects, on
the same contract as `payroll_calc.py`. A W-2 can be computed from a fixture and
checked against a number somebody worked out on paper — which is the only way an
arithmetic claim about somebody's wages is worth making.

### `warnings` is the part to read first

Every form returns one, and it is where the form says which of its numbers is a
floor rather than a figure. Three things a generator cannot work out for itself:

* **Which state a dollar was earned in.** A slip carries one `work_state`, but a
  cross-state pay period splits gross between two. The slip's `state_wages` is
  that allocation where the caller has it; without it the whole gross lands on
  `work_state`. Only a slip that genuinely ran two state engines raises the flag
   — a single-state slip has nothing to get wrong.
* **Year-to-date wages before the first slip in hand.** The Social Security wage
  base and Washington's UI taxable wage base are annual per-employee caps, and a
  quarterly form cannot see the quarters before it. Pass
  `ytd_wages_by_employee`; without it the cap applies to the quarter alone,
  which is exactly right for Q1 and an overstatement after it.
* **Additional Medicare, separately.** A slip's `medicare` is the ordinary 1.45%
  and the 0.9% surcharge added together, because that is what comes out of a
  paycheck. Line 5d needs the surcharge alone, and where the two were never
  stored apart the line is zero and says so.

### The wage bases are consumed per employee

Two workers at $100,000 each are **$200,000** of Social Security wages on a 941,
not $176,100. An annual per-person ceiling applied to a company's grand total
would let one high earner's headroom absorb everybody else's excess — a mistake
that produces a plausible number and an assessment letter.

### What is stored is what was computed, then

`form_data_json` is written once at generation and read back verbatim. A filed
form is a statement about what an employer told an agency, on a date; payroll
gets corrected afterwards, and a `get_tax_form` that recomputed from today's
data would quietly return a different W-2 than the one in the envelope.

### `list_tax_forms`

**READ (default ON).** Forms filtered by `form_type`, `fiscal_year`, `quarter`,
`employee`, `company` and `status`. Returns `by_status` alongside the rows, which
is the answer to "what is still outstanding for this quarter".

| Parameter | Required | Description |
|---|---|---|
| `company` | | Company name or abbreviation |
| `form_type` | | `W-2`, `1099-NEC`, `941`, `OR-WR`, `OQ` or `WA-ESD` |
| `fiscal_year` | | Calendar year as `YYYY` (`year` is an alias) |
| `quarter` | | `Q1`–`Q4` |
| `employee` | | Only the forms for one employee |
| `status` | | `Draft`, `Generated`, `Filed` or `Amended` |
| `limit` | | Default 100, hard maximum 500 |

### `get_tax_form`

**READ (default ON).** One form in full, including every computed box and line
value **as it was calculated at generation time**. `form_data.warnings` first.

| Parameter | Required | Description |
|---|---|---|
| `name` | yes | Tax Form docname (`tax_form` is an alias) |

### `generate_tax_form`

**MUTATING (default OFF).** Computes a form from the payroll already in the
system and records it as a Tax Form in `Generated` status.

Only **`Calculated` and `Submitted`** payroll entries are counted — a `Draft`
payroll has not been paid and a `Cancelled` one was not.

| Parameter | Required | Description |
|---|---|---|
| `form_type` | yes | `W-2`, `1099-NEC`, `941`, `OR-WR`, `OQ` or `WA-ESD` |
| `fiscal_year` | yes | Calendar year as `YYYY` (`year` is an alias) |
| `company` | | Required on a multi-company site |
| `quarter` | | Required for `941`, `OQ` and `WA-ESD`; refused on the annual forms |
| `employee` | | Required for a `W-2` |
| `related_party` | | Required for a `1099-NEC` |
| `company_address` | | The employer address to print — ERPNext does not store one on Company |
| `state_ids` | | `{"OR": "1234567-8"}`, overriding the State Tax Configuration |
| `ui_rate` | | The state's assigned unemployment-insurance rate, as a percent |
| `deposits` | | Form 941 line 13 — total federal deposits for the quarter |
| `ytd_wages_by_employee` | | `{"HR-EMP-00001": 42000}` — prior-period wages, for the wage bases |
| `oq_reported` | | `OR-WR` only: what was actually filed on each OQ, the thing it reconciles against |
| `notes` | | Stored on the form |

**A quarter on an annual form is refused rather than ignored.** Ignoring it would
produce a year's figures under a quarter's label, which is the one way this tool
could be wrong and look right.

**A second form for the same period and recipient is refused** while the first is
not `Amended`, and the error names the existing docname. The guard is per
recipient, so two employees get their own W-2s and each quarter gets its own 941.

### `regenerate_tax_form`

**MUTATING (default OFF).** Recomputes an existing form from current payroll —
after a slip was corrected, a rate changed, or a missing shift was added. Returns
`changes`: which values moved, `was`, `now` and `delta`.

| Parameter | Required | Description |
|---|---|---|
| `name` | yes | Tax Form docname (`tax_form` is an alias) |
| `allow_filed` | | Recompute even though the form is `Filed` |
| everything `generate_tax_form` takes bar the identifying ones | | Re-supplied per run |

**A `Filed` form is refused unless `allow_filed` is passed**, because recomputing
one replaces the record of what was actually sent to the agency. An `Amended`
form is refused outright — regenerate its successor.

### `mark_tax_form_filed`

**MUTATING (default OFF).** Records that a form was filed: status to `Filed`, the
filing date, and whatever the agency gave back.

| Parameter | Required | Description |
|---|---|---|
| `name` | yes | Tax Form docname (`tax_form` is an alias) |
| `filed_date` | | `YYYY-MM-DD`. Defaults to today |
| `confirmation_number` | | An EFTPS trace number, a Frances Online confirmation, an ESD receipt |
| `notes` | | Stored on the form |

**This transmits nothing.** Filing the same form twice is refused: it would
overwrite the date and confirmation number of the filing that actually happened.

### Four digits, never nine

Every form prints `XXX-XX-1234`, read off the I-9 — the one record on this site
that legitimately holds any of a Social Security number, and even there it stores
four digits. The person filing completes the rest from the paper. Same judgement
`generate_1099_prefill` made in v0.11.0, for the same reason: nine stored digits
would trade a real breach risk for a saved minute.

### One field on an existing doctype

`State Tax Configuration` gains `employer_account_number` — Oregon's Business
Identification Number, Washington's ESD account number. It is printed in W-2 box
15 and at the head of every state return. Without it those print blank, and the
alternative — an argument the model must be told each time — is worse than a
field an operator sets once per state.

---

## v0.36.0 — tax form PDF rendering

v0.34.0 computed the boxes. These two draw them: a letter-size portrait page per
form, in the official box and line numbering, attached privately to the Tax Form
record's `generated_pdf` field. Six layouts — **W-2** (Copy B), **1099-NEC**
(Copy B), **Form 941**, **OR-WR**, **OQ** and the **Washington ESD quarterly
report**.

**Nothing is recomputed.** The page is a rendering of `form_data_json` exactly as
it was calculated at generation time, so it cannot disagree with the record it
claims to render — which is the whole reason v0.34.0 stored the values instead of
recomputing them on read. Rendering moves no status and changes no figure.

**Every page is a working copy and says so twice**: a header note on every page,
and a footer block naming the agency and the channel the form is really filed
through. Copy A of a W-2 or a 1099 is red-ink scannable stock or an electronic
filing (BSO, IRIS/FIRE); 941 goes on the official form with deposits through
EFTPS; OR-WR through Revenue Online, OQ through Frances Online, the ESD report
through EAMS. These pages are for the farmer's records, for review before filing,
and for keying into the portal that does the filing.

**The generator's `warnings` print in full** at the foot of every form, under
*Before this form is filed*. They are the only place a form says which of its
figures is a floor rather than a figure, and a page that dropped them would look
more certain than the arithmetic behind it.

### `render_tax_form_pdf`

**MUTATING (default OFF).** Renders one Tax Form and attaches the PDF.

| Parameter | Required | Description |
|---|---|---|
| `name` | yes | Tax Form docname (`tax_form` is an alias) |
| `overwrite` | | Render even though `generated_pdf` is set, repointing the field |
| `company_address` | | The employer address to print, where the stored form data has none |

**A form that already has a PDF is refused** unless `overwrite` is passed: that
field may hold the copy somebody reviewed, or the one the agency issued, which
nothing here can reproduce. Overwriting repoints the field and **leaves the
earlier File attached** — the record gains and never loses.

**A form with no computed values is refused** rather than drawn. A page of zeroes
from an empty record is indistinguishable from a page of real zeroes.

### `bulk_render_tax_form_pdfs`

**MUTATING (default OFF).** Renders a set — every W-2 for a tax year, every 941
for a company.

| Parameter | Required | Description |
|---|---|---|
| `names` | | Explicit Tax Form docnames, instead of filters |
| `company`, `form_type`, `fiscal_year`, `quarter`, `status`, `employee` | | The same filters `list_tax_forms` takes |
| `overwrite` | | Render forms that already have a PDF |
| `company_address` | | The employer address to print |
| `limit` | | Default 100, hard maximum 500 |

**At least one selector is required.** Rendering every form on the site because
nobody said which is not a default this offers.

**A form that already has a PDF is skipped and counted, not refused** — one
rendered form should not stop a batch of ninety. A form that fails to render is
recorded by name with its reason and the run continues, so `rendered`, `skipped`
and `failed` between them account for every form matched.

**A selection larger than `limit` is refused, not truncated.** A bulk render that
silently stopped short would look like it had covered everything.

### One optional dependency

`reportlab` draws the pages. It is a declared dependency and a normal install has
it, but it is imported defensively like `shapely`, `h3` and `segno` before it: a
bench without it loses exactly these two tools — which say so by name, with the
pip command to fix it — and nothing else. Every generator and every other tax
form tool works without it, because the numbers are the deliverable and the page
is a convenience.

---

## v0.35.0 — payroll off the shift register

v0.30.0 built a payroll engine and v0.19.3 built a shift register. The join
between them was a stub: it returned the **crew's** whole span for every worker,
zero overtime and zero piece units, so every figure past "how long was the
shift" had to be keyed in by hand. This release is the join.

`erpnext_mcp/payroll_integration.py` is pure — no database reads, no side
effects — on the same contract as `payroll_calc.py` and `form_generators.py`.

### The unit is a segment, not a shift

A shift is crew-shaped and payroll is person-shaped. Every crew row already
carries its own `joined_at` and `left_at`, so what gets aggregated is one
person's own stretch of one shift. The crew worked 06:00 to 15:00 **and** Ana
joined at 07:10 and left at 13:00; the one payroll reads is Ana's.

### Overtime is weekly, and walked in time order

Oregon HB 4002 and Washington SB 5172 both put agricultural overtime at 40 hours
in a **workweek**, fully phased, at 1.5x. A biweekly period is two workweeks:

| | hours | overtime |
| --- | --- | --- |
| Week 1 | 45 | **5** |
| Week 2 | 35 | 0 |
| Period | 80 | **5**, not 0 |

Weeks are anchored at `pay_period_start` unless `workweek_anchor` says
otherwise. Walking each week chronologically rather than allocating pro rata is
what decides **which state** the overtime happened in: four ten-hour Oregon days
plus a Washington Friday is eight hours of Washington overtime.

### Two kinds of break

| | inside `total_hours`? | counts toward 40? |
| --- | --- | --- |
| `break_hours` — paid rest | yes | yes |
| `unpaid_break_hours` — meal | no, subtracted | no |

Paid rest is handed to the engine separately so the piece-rate path pays it at
the average hourly the period earned (WAC 296-131-020). Five nine-hour shifts
with a half-hour lunch each is 42.5 hours, so 2.5 of overtime rather than five.

### Minimum wage is per state, and PAID as of v0.49.0

Washington's $16.66 and Oregon's $14.70 are different floors, and a single check
against the period total would let a compliant Washington week paper over an
Oregon week that was not. Each state's hours are tested against its own floor,
with the overtime premium in it — `regular × minimum + overtime × minimum × 1.5`,
so fifty Oregon hours are owed $808.50 and not $735.

**Gross is the greater of what the work earned and what the hours are owed.**
FLSA §6, ORS 653.025 and RCW 49.46.020 all make the minimum wage a floor under
the wage: piece rate measures pay, it does not license paying less than the hours
were worth. Forty-seven buckets at $1.50 in an eight-hour Oregon day pays
$117.60.

**And the top-up is never silent.** Through v0.48.2 the shortfall was reported
and gross was left alone, so that a rate set below the lawful floor would stay
visible; folding the makeup into gross would have lost exactly that. So it is not
folded in — `earned_gross` and `minimum_wage_makeup` are separate figures on the
slip and separate columns on the stored row, and `totals.topped_up_to_minimum_wage`
names every worker whose rate needed one. Nobody on that list is underpaid. Every
name on it is a rate worth looking at.

**One exception, on purpose: a Salary structure is reported, not topped up.**
Whether a salaried employee is exempt from the minimum wage at all is a fact
about their job this app does not hold, so a salaried shortfall still appears in
`totals.below_minimum_wage` for somebody who knows the answer to decide.

### A day can be paid two ways

`pay_type` and `pay_rate` on a Farm Shift — or on one crew row of it — say that
this stretch of work was paid differently from the way the worker's salary
structure says. Blank is the ordinary day and means "the structure's way".

Six hours picking at $1.50 a bucket and two hours of irrigation at $16.00 is
$167, not $135 and not $128. Each stretch is paid its own way at straight time,
and the overtime premium is half of ONE regular rate blended across all of them
(29 CFR 778.115). `hourly_rate` on the salary structure is the standing answer to
"what is an hour of this worker's non-piece time worth"; the shift only has to
carry a rate when the day was priced differently from the standing one.

An hourly stretch with no rate anywhere earns nothing **at the rate** — paying it
per bucket would look deliberate — and those hours are carried by the minimum
wage makeup, where they are visible.

### Piece units, and saying where they did not come from

Bucket Log Entry, a count column on Farm Task Assignment, and a count column a
site has added to the crew row are all looked for and all **summed** rather than
preferred. A bucket log row with no count column is **one bucket** — the row is
the record of the bucket. Piece work matched to no shift is still paid, carries
no hours, and is counted in `piece_rows_without_a_shift`.

Every absent source is named in `sources.notes`, because a piece-rate run that
found nothing has produced zeros, and whether that means nobody picked or means
the bridge is not installed is the difference between a payroll and a mistake.

### `get_employee_timesheet_summary`

**READ (default ON).** One employee's hours in a date range: their own spans, the
weekly overtime breakdown, the state split, breaks by kind, piece units, and
every shift behind the total. **No money** — which is why it needs none of the
payroll switches. "Why is my cheque this?" is answered by the timesheet.

| Parameter | Required | Description |
|---|---|---|
| `employee` | yes | Docname or employee name (`name`, `employee_name` are aliases) |
| `start_date` | yes | `YYYY-MM-DD` (`pay_period_start` is an alias) |
| `end_date` | yes | `YYYY-MM-DD` (`pay_period_end` is an alias) |
| `company` | | Scopes the shift read |
| `overtime_threshold` | | Hours in a workweek before the premium. Default 40 |
| `workweek_anchor` | | First day of the declared workweek |

### `preview_payroll_for_period`

**READ (default ON).** A whole company's period computed and **not written** —
the same arithmetic through the same code path as the run. Read the three lists
before the totals: `employees_missing_structures`, `totals.below_minimum_wage`
and `totals.with_open_shifts`.

| Parameter | Required | Description |
|---|---|---|
| `company` | | Company name or abbreviation |
| `pay_period_start` | yes | `YYYY-MM-DD` |
| `pay_period_end` | yes | `YYYY-MM-DD` |
| `pay_frequency` | | `Weekly`, `Biweekly`, `Semimonthly`, `Monthly`. Default `Biweekly` |
| `employee` | | Limit the run to one person |
| `include_unworked` | | Keep structures with no shift on the run. Default true |
| `overtime_threshold` | | Default 40 |
| `workweek_anchor` | | First day of the declared workweek |
| `detail` | | Add the per-shift timesheet and the tax working. Large; off by default |

### `run_payroll_for_period`

**MUTATING (default OFF).** The identical calculation, stored as a Farm Payroll
Entry in **Calculated** status. Submitting stays `submit_payroll`, behind its own
switch: arithmetic anybody can redo and a statement about what the farm is paying
are two different acts.

| Parameter | Required | Description |
|---|---|---|
| `company` | | Company name or abbreviation |
| `pay_period_start` | yes | `YYYY-MM-DD` |
| `pay_period_end` | yes | `YYYY-MM-DD` |
| `pay_frequency` | | Default `Biweekly` |
| `employee` | | Limit the run to one person |
| `include_unworked` | | Default true |
| `overtime_threshold` | | Default 40 |
| `workweek_anchor` | | First day of the declared workweek |

**A run with problems in it is not refused.** A rate that needed a minimum wage
makeup, a shift nobody ended, a picker with no salary structure — all reported,
none of them a reason to hold up everybody else's pay. The one refusal is a run where
*nobody* can be paid, and it names them.

### What the v0.30.0 tools got out of it

`preview_payroll` and `calculate_payroll` read through the same aggregation, so
the single-employee preview now reports the same hours, the same overtime and
the same piece units as the company-wide run. `_load_shifts` also carried a real
bug — the same filter key twice in a Frappe dict, where only the second
survives, so the start-date bound was silently discarded — and it is fixed.

Year-to-date carries across periods too: the Social Security wage base is an
annual per-person cap, and a period run that could not see the ones before it
would restart it.

---

## v0.40.0 — payroll into the general ledger

Four tools. v0.30.0 computed payroll, v0.35.0 fed it the shift register's hours
and v0.36.0 drew the tax forms — and a completed run produced Farm Payroll Slips
and **no Journal Entries**. Wages are the largest number on a farm's income
statement and they were the one number somebody keyed into the ledger by hand,
off a report, every fortnight.

### No account name is shipped, and that is the design

The mapping from a payroll component to a general ledger account is a **record**,
per company: `Farm Payroll Account Mapping`, one row per component. A shipped
default would be right on the chart of accounts it was written against and
quietly wrong everywhere else — and "quietly wrong" in a chart of accounts means
a year of wages in an expense line nobody notices until the tax preparer asks.

### The eleven components

Six are **employee-side**, and together they are the two sides of gross pay, so
all six are required whatever the amounts are:

| Component | Side | What it is |
|---|---|---|
| `Gross Pay` | debit | Total earnings — the wage expense itself |
| `Federal Tax` | credit | Federal income tax withheld |
| `SS Employee` | credit | The worker's 6.2% share |
| `Medicare Employee` | credit | The worker's 1.45% plus Additional Medicare |
| `State Tax` | credit | Everything a state withholds from the worker |
| `Net Pay` | credit | What is actually paid — clearing, bank or cash |

Five are **employer-side**. Each is an expense *and* a liability, so each takes
both accounts, and each is required only where the run has an amount for it:

| Component | Sides | What it is |
|---|---|---|
| `SS Employer` | debit + credit | The farm's matching 6.2% |
| `Medicare Employer` | debit + credit | The farm's matching 1.45% |
| `FUTA` | debit + credit | Federal unemployment, first $7,000 per worker |
| `SUTA` | debit + credit | State unemployment at this employer's own rate |
| `State Employer Other` | debit + credit | Paid Leave Oregon employer share, OR workers' comp, WA PFML employer share, WA L&I |

They stay five rather than one because they are remitted to five different
places on five different schedules — the 941, the 940, the quarterly state
return, and two more besides. A farm that genuinely wants them in one account
can point five components at one account, which is a decision it made rather
than one this app made for it.

`State Employer Other` is the component the release specification did not name.
It exists because the state engines compute employer amounts that are **not**
unemployment insurance, and a mapping with nowhere to put them would drop real
money out of the books quietly.

### `get_payroll_account_mapping`

**READ (default ON).** Which accounts a company's payroll posts to, which
components are still unmapped, and what each one is for.

| Parameter | Required | Description |
|---|---|---|
| `company` | | Company name or abbreviation |

### `preview_payroll_gl`

**READ (default ON).** Every line of every entry the run would produce, both
totals and the balance check, with nothing written. **It refuses nothing** — an
incomplete mapping, a run already posted, a slip that does not balance are all
reported in `blockers` with `would_post: false`, because an unpostable run is
exactly what somebody calls a preview to find out about.

| Parameter | Required | Description |
|---|---|---|
| `payroll_entry` | yes | The Farm Payroll Entry docname (`name` is an alias) |
| `mode` | | `consolidated` (default) or `per_employee` |
| `posting_date` | | `YYYY-MM-DD`. Defaults to the run's pay period end |
| `cost_center` | | Set on every line that is not split. Defaults to the mapping's |
| `split_by_cost_center` | | Default true. Splits the expense lines across the blocks the work was done on |
| `include_employer` | | Default true. False books the wage half only |

### `configure_payroll_accounts`

**MUTATING (default OFF).** Sets the mapping. Rows **merge** into what is there
unless `replace=true`, so a mapping is built up a few accounts at a time.

| Parameter | Required | Description |
|---|---|---|
| `company` | | Company name or abbreviation |
| `components` | yes | List of `{component, debit_account, credit_account, notes}` (`accounts`, `mapping` are aliases) |
| `replace` | | Default false. True discards every row not in this call |
| `default_posting_mode` | | `Consolidated` (default) or `Per Employee` |
| `cost_center` | | Set on every payroll line |
| `is_active` | | Default true on creation |
| `notes` | | Who decided this mapping and against what |

**Group accounts are refused here**, not at posting time. A mapping is written
once and posted from every fortnight afterwards, so a group account stored now
is a payroll refused in six weeks by somebody who did not configure it.

### `post_payroll_to_gl`

**MUTATING (default OFF).** Turns a Farm Payroll Entry into **DRAFT** Journal
Entries and stops. Submitting stays `submit_journal_entry`, behind its own
switch, exactly as it does for `create_journal_entry`.

| Parameter | Required | Description |
|---|---|---|
| `payroll_entry` | yes | The Farm Payroll Entry docname |
| `mode` | | `consolidated` (default) or `per_employee` |
| `posting_date` | | Defaults to the run's pay period end |
| `cost_center` | | Defaults to the mapping's. Also where unattributed time lands |
| `split_by_cost_center` | | Default true. See **Labour by block**, below |
| `include_employer` | | Default true |

**Five refusals, all reported at once** rather than one per round trip: a
payroll entry that is not Calculated or Submitted, a company with no account
mapping, a mapping with a hole in it, a run that already has live journal
entries against it, and an entry that does not balance.

**The idempotency check is about the ledger, not the link table.** Every draft
is linked back onto the run's `gl_postings`, and a run with live entries against
it is refused by name. A run whose drafts were *deleted*, or whose entries were
*cancelled*, can be posted again — because then there is genuinely nothing in
the books.

### Labour by block — the cost center split (v0.101.0)

`split_by_cost_center` is **on by default** and splits the **expense** lines of a
payroll entry across the cost centers the work was actually done on. The chain
already existed and nothing read it:

```
Farm Task Assignment   who, and how many minutes (actual_duration_minutes)
  → Farm Task          where, through location_doctype = "Field" + location
    → Field            the block, and its cost_center (link_field_to_cost_center)
      → Cost Center    what the P&L groups by
```

**Only debits split.** Every component with a debit side is an expense — gross
pay is the wage expense, each employer component's debit is a tax expense — and
every credit is a liability: what is withheld, what is owed, what is left to
pay. A cost center on a liability answers no question anybody asks, so the
credits keep the blanket `cost_center`.

**Paid time no task placed keeps the blanket cost center.** A picker paid for
eight hours with two hours dispatched to Block 7 books a quarter of the wage to
Block 7, not all of it. The denominator is the slip's own `total_hours` — the
hours the gross was computed from — so the split and the payroll cannot
disagree. A slip carrying no hours is split by the attributed time alone.

**It is exact.** Each component is split by largest remainder, so a third of
$1,000.00 three times is $1,000.00 and the entry still balances. Totals, the
balance check, the entry count and the idempotency rule are all unchanged.

**Nothing is guessed at.** A task raised against a parcel, an irrigation zone or
a cabin is not block work and is left unattributed. A block with no Cost Center,
or one pointed at a group or disabled cost center — which ERPNext would reject
the whole entry over — is reported by name in `cost_center_allocation.
blocks_without_cost_center` and its time falls to the blanket. **A site that
dispatches nothing posts exactly what it posted before this release.**

`cost_center_allocation` in the result carries the minutes behind every share,
per employee, so a preview can be read rather than trusted. Take the preview
both ways before the first posting: the money is identical, the dimension is
not.

### What the payroll engine got out of it

Employer taxes have been computed since v0.28.0 and stored nowhere: the slip
carried the worker's deductions and none of what the farm owed on top. Farm
Payroll Slip now holds `social_security_employer`, `medicare_employer`, `futa`,
`state_unemployment`, `state_employer_other` and `total_employer_taxes`, and
`calculate_full_payroll` returns them plus `total_cost_of_employment`. No total
moved — these are the same figures the engines already returned, lifted to where
they can be kept.

**Slips written before v0.40.0 carry zeros**, so posting such a run books the
wages and leaves the employer's taxes off the ledger. That is reported as a
warning rather than inferred from four zeros: an employer with no employer taxes
is a real thing, and an entry that quietly left them out is not.

**State unemployment is new and defaults to zero.** `State Tax Configuration`
gains `suta_rate` and `suta_wage_base`, both employer-entered for the same
reason workers' compensation is — a SUTA rate is assigned to one employer by one
agency out of that employer's own experience rating, and there is no table
anybody could ship. A site that enters no rate computes exactly what it computed
before this release. The wage base is consumed by year-to-date gross, the way
FUTA's $7,000 is; a base of zero means no cap.

## v0.41.0 — farm task templates

Five tools. Since v0.16.0 the shape of a recurring job — its type, its skill, its
duration, its evidence contract, what its completion produces — has lived in
three places that could not be edited together: a Python dict called
`ALERT_TASK_MAP`, three loose `producer_*` fields on each Compliance Rule, and
whatever a foreman typed into `create_farm_task` that morning. Two rules asking
for the same job stated it twice, in full, and drifted.

A **Farm Task Template** is the missing record. It says what one job looks like,
once, and a rule, a foreman or a worker raises tasks from it.

### It is not an Inspection Template

They are different sizes of thing and the difference is load-bearing:

| | Inspection Template (v0.21.0) | Farm Task Template (v0.41.0) |
|---|---|---|
| What it defines | a **multi-section visit** | the shape of **one task** |
| What one produces | several compliance records, at their own cadences | at most one |
| Versioned | by copy — a session submits against its pinned version weeks later | **edited in place** — a task copies it once and is self-contained |
| Raised by | `_bundle_into_sessions`, matching sections against pending alerts | `create_task_from_template`, or a rule's `producer_task_template` |

Collapsing them would make every smoke detector test carry a sections table with
one row in it.

### A task snapshots its template

`create_task_from_template` **copies** the task type, the skill, the duration,
the dispatch mode, the evidence contract, the produced record and its defaults,
the instructions and the whole checklist onto the task. After that the task is
self-contained — it can be claimed, worked, evidenced and closed with the
template deleted.

Three things follow, and they are the reason the design is worth having:

* **Editing a template changes what future tasks look like and nothing else.** A
  worker halfway through a five-item walk whose template lost an item does not
  find their evidence attached to a list that no longer contains it. Nobody's
  contract tightens under them mid-job.
* **The template needs no versioning by copy.** Nothing reads back from it, so
  there is no live document an edit could change underneath. Versioning would
  buy an audit trail `track_changes` already keeps, at the cost of a register in
  which the eight templates an operation runs are lost among forty superseded
  rows.
* **`Farm Task.template` is provenance, never a lookup.**

### The checklist

Optional, and most templates should have none — a detector test is a checklist
and "renew the certificate" is not, and a one-item list saying *do the task* is a
form people learn to tick without reading. Where there is one, it is snapshotted
onto the task as `checklist_status` and `complete_farm_task` **refuses a
completion with a required item unticked**, by name, before any compliance
record is written. An optional item left undone does not refuse: that is what
optional means, and it is what keeps a template that covers more than today needs
usable.

Mark items with the completion's `checklist` argument — a list of item names, or
objects carrying `item_name`, `done` and a `note`. A name the task's checklist
does not hold is **refused rather than ignored**: a typo that silently marks
nothing looks exactly like a tick right up until the completion is refused for a
different item.

### `create_farm_task_template`

```json
{"template_name": "Smoke Detector Test",
 "task_type": "Test",
 "skill_required": "camp_maintenance",
 "estimated_duration_minutes": 20,
 "dispatch_mode": "Self-pick",
 "evidence_required": {"photos": true, "findings_text": true},
 "creates_record": "Detector Test",
 "instructions": "Test every detector in the unit, not a sample.",
 "checklist": [{"item_name": "Every smoke detector pressed and heard", "evidence_type": "Photo"},
               {"item_name": "Every CO detector pressed and heard", "evidence_type": "Photo"},
               {"item_name": "Batteries replaced where it chirped", "required": false}],
 "compliance_regimes": ["OR-OSHA"]}
```

`evidence_required` is **mandatory**, for exactly the reason it is mandatory on a
task: a template with no contract raises tasks with no contract, `create_farm_task`
refuses those, and the failure would land in front of whoever is stood in the
cabin rather than in front of whoever wrote the template.

A checklist of bare strings is accepted and means *required, no evidence, in this
order* — the commonest checklist anybody writes through a chat client.

### `create_task_from_template`

```json
{"template": "Smoke Detector Test",
 "location_doctype": "Housing Unit", "location": "MC-Cabin-01 - MC",
 "assigned_to": "HR-EMP-00002", "urgency": "High"}
```

`location`, `assigned_to` and `urgency` are the three overrides, because they are
the three things that are true of the **case** rather than of the job. The task
name defaults to the template name and the place — `Smoke Detector Test —
MC-Cabin-01` — because *Smoke Detector Test* fifty-four times is a board nobody
can work from.

### `producer_task_template` was repointed

`Compliance Rule.producer_task_template` linked to Inspection Template from
v0.22.0 to v0.40.0, and **nothing on the dispatch side ever read it**:
multi-section visits are raised by matching a template's sections against the
records a place's pending alerts ask for, which never consults this column. It
now names a Farm Task Template, and it has behaviour for the first time.

Where a rule names one, the template is the **whole recipe** and the rule's
inline `producer_farm_task_type`, `producer_skill_required` and
`evidence_contract_json` are not read. Those three stay as the fallback for a
rule with no template, which is most of them.

`generate_tasks_from_compliance_alerts` therefore resolves a recipe in three
steps: the rule's template, then `ALERT_TASK_MAP`, then the rule's inline fields.
The alert still supplies what only it knows — the severity that becomes urgency,
the place, and its own message, which goes on the task **after** the template's
standing instructions because that is the order a worker needs them in.

The `repoint_producer_task_template` patch clears any value naming an Inspection
Template and prints every one by name. It clears rather than converts: there is
no honest automatic translation from a four-section visit to a single task shape,
and picking one section would be this app inventing an operator's intent and then
generating work from the invention.

### Five seeded templates, and nothing wired

`bench migrate` seeds *Cabin Habitability Inspection*, *Smoke Detector Test*,
*Water Quality Test*, *Certification Renewal* and *Training Record*. Every type,
skill, duration, dispatch mode and evidence contract on them matches
`ALERT_TASK_MAP` to the letter — asserted by a test — so pointing a shipped rule
at its template raises exactly the task it raised in v0.16.0, plus a checklist.

**Seeding wires no rule.** `producer_task_template` is left exactly as it was on
every rule, so an upgrade changes no task any sweep produces. Pointing a rule at
a template is a deliberate act, by somebody who has read what the template asks
for:

```bash
update_compliance_rule
  {"rule": "housing_detector_test_stale",
   "producer_task_template": "Smoke Detector Test"}
```

The seeder checks by template name and creates only what is not there, so an
operator who added an item to the detector checklist keeps it and one who
disabled a template their operation does not run keeps it disabled. There is no
`delete_farm_task_template`: `enabled=false` retires one while keeping every task
it ever raised readable, which is what an auditor asking *what did this job ask
for last season* needs.


---

## v0.42.0 — budget + variance alerts

Seven tools. A **Budget** is one company's plan for one fiscal year: which
general ledger accounts and which Financial KPI Definitions it tracks, what it
planned for each, and — once `refresh_budget` has run — what actually
happened and how far apart the two are.

**The arithmetic is not in the tool layer.** `budget_engine.py` is pure and
reads no database, the same split `payroll_gl.py` keeps: `compute_budget_actuals`
fills in `actual`/`variance` from plain dicts, `check_budget_variances` finds
the rows whose variance has crossed their own threshold, and `refresh_budget`
is the two run together. `tools/budget.py` is the only place that reads the
ledger or the KPI cache.

### Severity is a ratio of the variance to its own threshold

Every line item and every KPI target carries its own `threshold_pct` (default
10). A breach is **Warning** at 1×–2× that number and **Critical** past 2× —
so a tightly-watched line and a loosely-watched one escalate on the same rule
wherever their own threshold was set, rather than against one shared number
that would be too tight for one line and too loose for another.

### `create_budget`

```json
{"budget_name": "FY2026 Operating Budget", "company": "Highland Orchards",
 "fiscal_year": "2026",
 "line_items": [{"account": "5300 - Field Labor", "budgeted_amount": 180000, "threshold_pct": 10},
                {"account": "5400 - Fertilizer", "budgeted_amount": 42000}],
 "kpi_targets": [{"kpi_definition": "cost_per_bin", "target_value": 25, "threshold_pct": 15}]}
```

Every actual and variance column starts at zero — nothing here touches the
ledger or the KPI framework. `threshold_pct` is optional on either kind of row
and defaults to 10.

### `update_budget`

Edits a budget in place. `line_items` and `kpi_targets`, if passed, **replace
the whole table** — including every figure already computed on it, which
`refresh_budget` then rebuilds. Everything else — `budget_name`, `company`,
`fiscal_year`, `status`, `notes` — is a normal partial update.

### `get_budget` / `list_budgets`

Read-only. `get_budget` returns one budget in full with its breach state as of
its last refresh; `list_budgets` is the register, filterable by `company`,
`fiscal_year` and `status`, with each row's line item and KPI target counts.
`last_refreshed` is the field to check first — empty means every actual and
variance figure is a placeholder, not a figure.

### `refresh_budget`

**MUTATING (default OFF).** Recomputes one budget's actual/variance columns
and saves them.

* **Account actuals** are year-to-date GL movement within the budget's own
  fiscal year — from its start date through today, or its end date, whichever
  is sooner — compared against the line's full-year `budgeted_amount`. A
  budget six months into its year is not "50% under" on every line; it has
  simply not finished yet.
* **KPI actuals read the KPI framework's own cache** (`compute_kpi(...,
  use_cache=true)`) rather than recomputing — the same figure the dashboard is
  showing, filled by the 03:00 KPI history sweep.

**It does not write a Compliance Alert directly.** It saves the computed
fields; the `budget_variance_breach` compliance rule reads them the way
`financial_kpi_threshold_breach` reads the KPI cache, and the hourly sweep is
what turns a breaching **Active** budget into an alert on the calendar — with
the same dismissal, snooze and auto-clear every other alert gets. A Draft or
Closed budget's breaches never reach the calendar, and the overnight cron
(`erpnext_mcp.tools.budget.refresh_all_active_budgets`, 03:15 — fifteen
minutes after the KPI cache job, so every KPI target reads a same-night
figure) only ever touches budgets whose `status` is Active.

### `get_budget_variance_report`

Read-only. The full breakdown — every line item, every KPI target, which of
them breach, worst first — read from whatever `refresh_budget` last computed.
Never touches the ledger.

### `close_budget`

**MUTATING (default OFF).** Sets `status=Closed`. Nothing is deleted; a closed
budget keeps every figure it last computed and simply stops being refreshed
overnight or scanned for variance alerts.

---

## v0.43.0 — ML Model Registry

Seven tools. Volume Vision (the training side) trains models and holds the
weights; this app never sees a weight and never computes a metric. What ERPNext
adds is the one fact Volume Vision has no reason to know: which trained model
is **deployed** for one company and one piecework activity — the record
`get_active_model` is queried against by an iOS app (BucketLog, Farm Ops)
deciding what to pull.

**The arithmetic is not in the tool layer.** `model_registry.py` is pure and
reads no database, the same split `budget_engine.py` keeps:
`validate_model_registration` checks a candidate record's shape,
`build_model_manifest` reshapes an ERPNext record into Volume Vision's own
`to_dict()` shape (`uuid`/`name`/`class_names`/`metadata`), and
`check_model_conflicts` says what activating a candidate would supersede.
`tools/ml_model.py` is the only place that reads or writes an ML Model
document.

### `register_model`

```json
{"model_name": "Cherry Fill Detection", "version": "3.2",
 "company": "Highland Orchards", "piecework_activity": "bucket_fill_detection",
 "source_uuid": "4b6f6e1a-2c3d-4e5f-8a9b-0c1d2e3f4a5b",
 "source_server": "http://umbrel.local:8095",
 "model_kind": "Detection", "model_format": "CoreML",
 "class_names": ["empty", "partial", "full"],
 "metrics": {"accuracy": 0.94}}
```

**MUTATING (default OFF).** Creates an ML Model record. Starts as `Draft` —
`activate_model` is what makes it the model an iOS app pulls. Refuses a
duplicate `(company, model_name, version)`; `update_model` edits the existing
record instead.

### `update_model`

Edits metadata in place. `status`, `company` and `piecework_activity` **cannot
be changed here** — `activate_model`/`deprecate_model` own status, and a
model's company and activity are the identity a caller resolves the record by,
not a field being renamed out from under an in-flight lookup. `class_names`
and `metrics`, if passed, replace the whole value.

### `get_model` / `list_models`

Read-only. `get_model` returns one record in full, resolved by docname or by
`model_name` (narrowed with `company`/`version` when more than one matches);
`list_models` is the register, filterable by `company`, `status` and
`piecework_activity`.

### `activate_model`

**MUTATING (default OFF).** Sets `status=Active` and `deployed_at=now`.
Whichever **other** model was Active for the same `(company,
piecework_activity)` auto-transitions to `Deprecated` — never more than one
model is Active for one activity at one company, which is what
`get_active_model` reads. The invariant is enforced twice: this tool computes
and reports what it is superseding before saving, and the DocType controller
separately guarantees the database cannot disagree, regardless of which door a
save came through. Activating an already-Active model is a no-op that still
refreshes `deployed_at`.

### `deprecate_model`

**MUTATING (default OFF).** Sets `status=Deprecated`. Nothing is deleted — a
deprecated model keeps every field it had and simply stops being returned by
`get_active_model`.

### `get_active_model`

Read-only. **The tool an iOS app queries** to find out which model to pull for
one company and one piecework activity:

```json
{"company": "Highland Orchards", "piecework_activity": "bucket_fill_detection"}
```

Returns the full record and its manifest when a model is Active; a clear
`"active": false` result — **not an error** — when none is, so a scanning app
polling at startup does not have to treat "nothing deployed yet" as a failure.

---

## v0.53.0 — The badge in the worker's own wallet

One tool. `generate_employee_badge_qr` has minted `ETC-0001` and drawn its QR
since v0.50.0, and what came back was a **PNG somebody has to print**. That means
a trip to an office in the middle of a hire day, a laminator, and a card that
goes through a wash cycle in August. Every worker in the orchard is already
carrying a phone with a wallet on it.

**It is the same badge, not a second credential.** The identifier, the minting
and the Bucket Log Badge Map row are `generate_employee_badge_qr`'s, called
underneath — so a bucket scanned off a phone screen and one scanned off a
laminated card produce the identical string and resolve through the identical
`resolve_badge`. If this tool ever needed its own serial the design would have
gone wrong.

### `generate_employee_badge_pass`

```json
{"employee": "HR-EMP-00042", "company": "Example Trading Co", "platform": "both"}
```

**MUTATING (default OFF).** Builds an Apple Wallet `.pkpass` — farm logo,
employee photograph, name, company, badge number and the QR — and a Google Wallet
pass object with a save link. Issues a badge to anybody who has none and reuses
the live one where there is one, so it is **idempotent without `regenerate`** for
the same reason its QR counterpart is. `regenerate: true` mints a new ID and
retires the old.

The `.pkpass` is attached **privately to the Employee** and returned as
`apple.file_url`; regenerating replaces that one file rather than growing the
attachment list. `include_base64: true` puts the bytes in the result instead,
which is what the mobile route sets — a handset authenticates with
`X-FarmOps-Token` and a private `file_url` is a login page to it.

| Argument | Meaning |
| --- | --- |
| `employee` | **Required.** Docname, number, name or login. |
| `company` | Which entity issues it. Defaults to the employee's. |
| `platform` | `both` (default), `apple`, or `google`. |
| `regenerate` | Mint a new ID and retire the old — the lost-card path. |
| `attach` | Default true. False builds without filing a File. |
| `include_base64` | Default false. True returns the `.pkpass` bytes inline. |

**A site with no Apple certificate still gets a pass.** It is complete and
correct — right `pass.json`, right images, right manifest — with **no signature
member**, and the result says `apple.signed: false` with the `site_config.json`
keys it needs. Apple Wallet will refuse to open that file, and that refusal is
the honest outcome: a self-signed blob with a `signature` in it fails just as
hard while looking upstream like it worked. `docs/wallet-passes.md` is what to
obtain and where to put it; nothing in this app changes the day the certificate
lands.

**Google fetches images; it is not sent them.** A Google Wallet pass is a signed
JWT that becomes a `pay.google.com/gp/v/save/…` link, and Google builds the pass
server-side — so a photograph at `/private/files/…` is a 403 to it and is simply
not on the Android pass. Every image left out for that reason is named in
`google.warnings` rather than written as a URL that renders as a grey box on a
worker's phone. The Apple pass carries its pixels inside the file and has no such
problem.

Refused: an employee who has left, an employee of another entity, an entity this
caller cannot reach.

---

## v0.52.0 — ML Model File Serving

Two tools. Through v0.51.1 an ML Model record said which model was deployed and
where it was trained, and an iOS app read `source_server` off the manifest to
pull the binary from Volume Vision directly. This release lets ERPNext own and
serve the binary itself, so a phone reads it back through the same
`X-FarmOps-Token` door — the farmops-api sidecar — it already authenticates
every other call through, instead of opening a second connection to Volume
Vision with a second credential.

### `attach_model_file`

```json
{"model": "MLM-2026-0001", "file_token": "f7a2c8e1b3"}
```

**MUTATING (default OFF).** Gives an ML Model record the binary — the
upload-once step that lets `get_model_file_chunk` serve it afterward. Exactly
one of `file_token` (a File docname already on the site — what
`commit_staged_file`, `tools/uploads.py`, hands back after a large binary went
up in pieces) or `file_content` (base64 in the call itself, for something
small) is required. Re-attaching **replaces** `model_file`; the previous File
is left on the site rather than deleted.

**v0.59.0: a zip is read as a bundle.** The first four bytes decide — `PK`
magic, not the file name, because the name is whatever a browser called the
download. A bundle's `manifest.json` supplies `class_names` (in
model-output-index order), `metrics` and the rest, `manifest_source` on the
record records that they came from training, and the whole zip is what gets
stored. Anything that is not a zip attaches exactly as it did in v0.52.0 and
reports that its labels are unverified. A zip that will not open, or one with
no `manifest.json`, is **refused** with nothing written. `force: true` is
needed to attach a bundle whose manifest names a different `source_uuid` than
the record's.

### `get_model_file_chunk`

```json
{"model": "MLM-2026-0001", "chunk_index": 0}
```

Read-only. One base64 slice of the attached binary — the same shape
`stage_file_chunk` takes uploads in, read backwards. `chunk_index` counts from
0; a caller that does not know `total_chunks` yet asks for index 0 and reads it
off the answer. Refuses **by name** when `attach_model_file` has not run yet,
rather than reaching for `source_server` on the caller's behalf — there is no
proxy back to Volume Vision here, deliberately.

Serves whatever is attached: `total_bytes` and `total_chunks` are computed from
the stored bytes on every call, so a bundle simply has more pieces than the raw
model it contains. `is_bundle` says which shape arrived, read from the bytes
rather than from the record, so a client can branch on unzip-vs-compile from
the first chunk.

---

## v0.59.0 — Model Bundles from Volume Vision

One tool. A **bundle** is one zip carrying `model.mlmodel` beside a
`manifest.json` Volume Vision writes at export time from the training config —
`class_names` in model-output-index order, plus `metrics`, `class_roles` and
the `preprocessing` block (input size, normalization) an iOS app needs at
inference. It exists because three lists could previously disagree about what
output index 2 means — the labels typed onto the ML Model record, the labels
Volume Vision held, and the labels a phone cached months ago — and when they
did, nothing failed. Inference went on returning confident numbers against the
wrong names.

`get_active_model`'s manifest gains `metadata.bundle` —
`is_bundle`, `manifest_source`, `class_roles`, `preprocessing`,
`training_version` — so a client reads the preprocessing parameters without
unpacking anything.

### `pull_model_from_vv`

```json
{"model": "MLM-2026-0001"}
```

**MUTATING (default OFF).** The whole manual procedure — curl on a laptop,
base64, `bench console` — as one call, with the provenance check nobody was
doing by hand.

Asks `GET <source_server>/training/models/<uuid>/bundle` **first**. Falls back
to `GET <source_server>/training/models/<uuid>/download` — the endpoint LiDAR
Capture and BucketLog have always used, unchanged — only when the bundle
endpoint answers 404/405/501, which is what a Volume Vision without the bundle
export deployed answers. **The fallback is reported**, in `warnings` and in the
summary, because a raw file has no manifest and leaves `class_names`
unverified; `allow_raw_fallback: false` refuses instead of taking it.

`source_server` and `source_uuid` default to the ML Model record's own — host
**and port** are read from the record, never assumed — and either can be passed
to override. A record can also be found by its `source_uuid` alone, which is
how a caller holding only Volume Vision's identifier finds the ERPNext record
for it.

**Refuses a bundle whose manifest names a different `source_uuid`** than the
record's: that is the wrong file for this record, and attaching it would make
every iOS cache keyed on that uuid wrong. `force: true` overrides, and says so
in the warnings.

The manifest's `training_completed_at` is ISO 8601 (`2026-07-08T02:38:43Z`) and
the column it lands in is a MariaDB `DATETIME`, which refuses one. It is
converted before the write, with any offset applied so the column holds UTC for
every bundle; an unreadable timestamp leaves the field unset with a warning
rather than failing an attach that has otherwise succeeded.

This is the only tool in this app that fetches a file from another server. It
enforces http/https only, no credentials in the URL, no redirects followed, and
a 512 MB ceiling checked against `Content-Length` before the body is read and
against the body after — see `erpnext_mcp/services/volume_vision.py` on why the
allowlist posture is different from `validate_public_endpoint`'s.

---

## v0.68.0 — ML Model Format Migration

Three tools, two of them read. Three releases have each defined what an ML
Model record carries, and a site running since v0.43.0 holds all three shapes
at once: labels typed onto a record with no file, a raw `.mlmodel` attached
beside them, and a bundle manifest stored exactly as Volume Vision's exporter
wrote it. `get_active_model` served all three identically and a client could
not tell them apart — the same "nothing fails, the answer is just wrong" shape
the bundle format was introduced to close, one level up.

**The current schema** is `schema_version` `1.0`, and on top of v0.59.0's
bundle contract it requires two things this app can always supply itself:

- **`schema_version`**, so a manifest cached on a handset months ago says what
  shape it is without asking the site what release it is running.
- **`userDefined`** — CoreML's own string-to-string metadata dictionary, the
  one an iOS client reads off the *compiled model* as
  `modelDescription.metadata[.creatorDefinedKey]`, carrying the label list in
  the spelling that lives in the weights. That mirror is the point: labels
  agreeing with the model's own embedded metadata have been corroborated by
  something other than whoever typed them. It is comma-joined, except where a
  label contains a comma — splitting `"bucket, full"` in two would renumber
  every output index after it, so that case is written as JSON instead.

**`manifest_origin`** is the third addition and the one that changed an
existing answer. Until now "this record has a `bundle_manifest`" and "the
attached file is a zip" were the same fact, and `is_bundle` was computed from
the first. A migrated record breaks that — it has a manifest built from its own
fields and a raw model beside it — so `is_bundle` now reads the origin, and a
manifest predating the field still reads as the bundle it could only have been.
`get_active_model`'s `metadata.bundle` block gains `schema_version`,
`manifest_origin` and `user_defined` alongside it.

**A model attached today is already current.** `attach_model_file` and
`pull_model_from_vv` normalize on the way in, additively — every key the
exporter wrote survives untouched — so this is a migration for records that
predate the format, not a queue that refills.

### `list_models_needing_migration`

```json
{"company": "Example Trading Co"}
```

Read-only, and reads no files — the cheap metadata pass over the whole
register. A record appears for no `bundle_manifest` at all (the pre-v0.59.0
shape), a manifest with no `schema_version` or an older one, a missing or
disagreeing `userDefined` mirror, an unrecognised `model_kind`/`model_format`,
or a record whose own `class_names` disagree with its manifest's. Each row
carries its own `reasons`, because the fix differs.

**Read `blockers` first.** A record with one cannot be migrated as it stands —
no `class_names` anywhere to build a manifest out of, or a `model_format`
nothing recognises — and wants `update_model` or `pull_model_from_vv` before
`migrate_model_format` will touch it. `ready_to_migrate` counts the rest.
`include_current: true` returns the up-to-date records too; the counts split
them either way.

### `validate_model_bundle`

```json
{"model": "MLM-2026-0001"}
```

Read-only. One record, held to the current schema, reporting **every** issue
rather than the first — split into errors and warnings, with a `code` per issue
and a `checks` block of what was actually looked at. Three layers:

1. **The manifest**, against the schema above: required fields, `class_names`
   an ordered array of labels, `model_kind`/`model_format` recognised, and the
   `userDefined` mirror agreeing with the array it mirrors.
2. **The record against its manifest.** Two label lists that disagree about
   what output index 2 means is an *error*, not a note — only one of them is
   the output-index order and nothing here can say which.
3. **The file references.** `model_file` resolving to a File on this site
   (`get_model_file_chunk` would refuse otherwise, and an iOS app would find
   out on a handset); the bytes being the shape `manifest_origin` claims, in
   both directions — a manifest saying `bundle` over a raw model, and a stored
   zip whose `manifest.json` nobody ever read; and, for a real bundle, that the
   zip still contains its manifest and a model payload.

`check_payload: false` skips layer 3's byte reads. Frappe reads a File whole,
so the metadata checks alone are the cheap pass over a compiled model of any
size. **Nothing is corrected**, including nothing that is obviously wrong —
`migrate_model_format` is the tool that writes, and the split is what makes
this one safe to run across a register.

### `migrate_model_format`

```json
{"model": "MLM-2026-0001"}
```

**MUTATING (default OFF).** Restates one record's manifest in the current
schema. **Metadata only** — nothing is uploaded, downloaded or re-attached, and
the binary is never read, so `get_model_file_chunk` serves the same bytes
afterwards.

A record that **already had a bundle** keeps that provenance and every key the
exporter wrote, and gains the schema fields. A record that **never had one**
gets a manifest assembled from its own fields, with `manifest_origin: "record"`
and a `manifest_source` naming this tool — it does not claim the labels came
out of training when somebody typed them, which is the distinction
`manifest_source` was added to preserve, and `is_bundle` stays false so no
client tries to unpack a raw model.

**Refuses rather than inventing.** A record with no `class_names` anywhere has
nothing to build a manifest out of, and an unrecognised `model_format` is
preserved rather than quietly replaced with the default; both are refused by
name with the tool that settles them. **Already current is not an error** — it
returns a result saying so, which is what makes this safe to run straight down
`list_models_needing_migration` without filtering first. `dry_run: true`
computes everything and saves nothing; `force: true` rewrites a record that
needed nothing.

---

## v0.44.0 — BucketLog → ERPNext Piecework Bridge

Eight tools. `payroll_integration.py` has read a `bucket_logs` row off a shift
since v0.35.0 and `tools/payroll.py` has speculatively queried a doctype
called `Bucket Log Entry` since the same release — this release creates it,
as erpnext_mcp's **own** doctype, and everything that gets a capture from an
iPhone in an orchard into it: the sync endpoint, the badge → Employee
register, and the reads a foreman or a payroll run asks afterward.

**Bucket Log Entry is no longer a hypothetical external app's doctype.**
Through v0.43.0, `compliance_fields.py` grafted five FSMA traceability
columns onto it the way it grafts columns onto Spray Log
(`farm_precision_ag`) or Employee (`farm_hr`/`hrms`). Now that erpnext_mcp
ships the doctype itself, its `Target` entry moved to `mode="verify"` —
declared fields, checked present, the same treatment Housing Unit and Field
already get — and `picker_id` retired in favour of a proper `employee` Link
resolved from `worker_badge`. `crew_id`/`block_id`/`bin_id`/`shipment_id`
ship as declared fields, so the FSMA chain of custody did not regress.

**The arithmetic is not in the tool layer.** `bucket_bridge.py` is pure and
reads no database, the same split `model_registry.py` keeps:
`validate_bucket_entry` checks a capture's shape, `resolve_badge_to_employee`
reads a pre-fetched `{badge_id: employee}` map, `aggregate_session` computes
a session's totals from its own entries, and `entries_to_payroll_shape`
reshapes captures into exactly the `bucket_logs` row
`payroll_integration._piece_units_for` already reads — **no change to that
function was needed**. `tools/bucket_log.py` is the only place that reads or
writes a Bucket Log Entry, Bucket Log Session or Bucket Log Badge Map
document.

**Only an Accepted verdict is piece work.** A Rejected capture is the
on-device ML model saying the bucket was not actually filled;
`entries_to_payroll_shape` filters it out at the one place every
payroll-facing read passes through, and `tools/payroll.py`'s own bucket-log
loader filters the same way for the production path — it also now recognises
`timestamp` as this doctype's own date column.

**The model is a binary gate — there is no partial credit anywhere.** A bucket
is full or it is not, the phone decides that once on device, and the only thing
that crosses to this app is which way it went:

```
Accepted → 1 bucket        Rejected → 0 buckets
piecework pay = number of Accepted buckets × piece rate
```

A capture the model judged 51% full and one it judged 99% full are worth exactly
the same thing if both were Accepted — one — and nothing if both were Rejected.
`coverage_percent` is **diagnostic**: the model's own record of why the gate went
that way, kept for the same reason `model_uuid` is, so somebody auditing a model
version can see what it was looking at. **It is never an input to pay.** It is
deliberately absent from `payroll_integration._UNIT_KEYS` and
`entries_to_payroll_shape` deliberately does not emit it, and
`test_bucket_bridge.TheGateIsBinary` asserts both directions against
`_UNIT_KEYS` itself so the two cannot drift. Paying a partial bucket would be a
change to the *gate* — a third verdict, priced deliberately — not a
multiplication by a figure that was only ever evidence.

### `sync_bucket_entries`

```json
{"entries": [
  {"entry_uuid": "e1a2...", "session_uuid": "s9c1...", "company": "Highland Orchards",
   "worker_badge": "QR-0042", "timestamp": "2026-06-01 08:12:00", "verdict": "Accepted",
   "coverage_percent": 94.2, "model_uuid": "4b6f6e1a-...", "gps_lat": 45.31, "gps_lon": -123.02,
   "h3_cell": "8a2830828447fff", "device_id": "iPhone-Ana"}
]}
```

**MUTATING (default OFF).** Creates Bucket Log Entry records, resolves each
one's `employee` from `worker_badge` against the Bucket Log Badge Map
register, and keeps the Bucket Log Session each belongs to up to date.
**Deduplicates by `entry_uuid`** — resyncing a batch already on the site is a
no-op, not a duplicate record. An entry that fails validation (bad verdict,
no timestamp, no badge or employee) is reported and **skipped** rather than
failing the whole call, up to 500 entries per batch.

### `list_bucket_entries` / `list_bucket_sessions`

Read-only registers, filterable by `company`, `employee`, `status` and a
date range; entries additionally by `badge`, `session` and `verdict`.

### `get_bucket_session`

Read-only. One session, by docname or `session_uuid`, with its totals
**computed live** from its own current entries (`bucket_bridge.aggregate_session`)
rather than only the stored counters — the two can drift if a badge resolves
after the session was last synced.

### `link_badge_to_employee`

**MUTATING (default OFF).** Maps a QR badge ID to an Employee. Repoints an
existing mapping (a lost card reissued to somebody else) rather than
refusing. **Backfills `employee`** onto any already-synced entry and session
carrying that badge with none resolved yet — a badge mapped after the fact
still pays for what was already picked.

### `link_entries_to_shift`

**MUTATING (default OFF).** Associates Bucket Log Entries with a Farm Shift
— pass `entries` (a list of entry_uuid/docname) or `session` (every
not-yet-Paid entry in it) — so they are picked up as piece units when that
shift's payroll runs. An entry already `Paid` is left untouched: status only
ever advances Pending → Linked → Paid.

### `get_piecework_summary`

Read-only. The payroll-ready summary for one employee over a date range:
accepted buckets, sessions worked, acceptance rate.

### `reconcile_bucket_payroll`

Read-only. Compares accepted Bucket Log Entries against what Farm Payroll
Slips actually paid for the same company and period, per employee. **A
discrepancy is not necessarily an error** — a bucket entry with no slip
covering it yet is simply unpaid so far, the same posture
`check_minimum_wage_by_state` takes.

## v0.50.0 — Issuing a badge, and refusing a soda can

Three tools, and they close the two ends of the pipeline v0.44.0 built the
middle of. The badge audit of 2026-08-07 found it about sixty per cent built,
with the missing forty in two places: **nobody issued a badge** on this side, and
**nobody sent a bucket capture** off the phone. These are the first of those; the
second is the iOS half (`BucketEntryQueue`, `BucketSyncEngine`).

**`link_badge_to_employee` never minted anything.** It *recorded* a string an
operator typed or scanned. So the only real badge cards in the business were
printed by `farm_app` (Flask) and encoded a uuid4 `QRToken` that ERPNext had
never heard of, whose revocation lifecycle lived in another application, and
which nobody can read off a scuffed card or say aloud over a radio at 6am.

**A minted badge is `<company abbreviation>-<sequence>`** — `ETC-0001`. Piece-rate
attribution is a payroll record, so the identifier that decides who gets paid for
a bucket lives in the system that pays, with a retirement flag an HR manager can
flip. **The cutover is incremental, not a reprint day:**
`bucket_bridge.resolve_badge_to_employee` matches a badge as an exact string with
no format assumption, so an old uuid mapped with `link_badge_to_employee` and a
new `ETC-0042` both resolve to the same person for as long as the transition
takes. `generate_employee_badge_qr(badge_id=…)` adopts an existing card outright.

### `generate_employee_badge_qr`

```json
{"employee": "HR-EMP-00042", "company": "Example Trading Co"}
```

**MUTATING (default OFF).** Mints a readable badge ID if this person has none,
records it in the Bucket Log Badge Map register, and returns the QR for the card
as a base64 PNG (or `format: "matrix"` for the raw grid) alongside the name,
designation, photograph URL and the initials that go where a photograph is
missing.

**It also answers where somebody belongs.** The Employee form's **Company
Details** section comes back with the card: `department` and `branch` as
printable labels rather than Link docnames, `reports_to_name` for the supervisor
(with `reports_to` for the docname, `reports_to_designation` for their title and
`reports_to_chain` for the ladder above them), plus `grade` and
`employment_type`. `housing` is their current Housing Assignment as
`<camp> · <cabin>`, with `camp`, `cabin`, `unit` and `housing_assignment`
returned separately so a caller need not parse the printed line back apart.

**`crew` is a different question and keeps a different answer.** It is the
foreman of the one open Farm Shift this person is standing on — the oversight
link, which is not the same relationship as `reports_to` — and it is deliberately
*not* printed on the card, because a shift is one morning's roster and a card is
laminated. Two open shifts answer nothing rather than guess.

A site that records none of it still gets its badge: every lookup is best-effort
and none can refuse a card.

**Idempotent without `regenerate`** — somebody who already holds a live badge
gets *that* badge's QR back. Reprinting a card that went through a wash cycle is
the common request and must not consume an identifier. `regenerate: true` is the
lost-card path: it mints a new ID and **retires the old one in the same call**,
because a replacement that leaves its predecessor resolving is how a badge found
in an orchard keeps earning. A retired number is never reissued.

Refused: an employee who has left, an employee of another entity, a `badge_id`
that is live for somebody else, and a `badge_id` that is not badge-shaped.

### `generate_employee_badge_sheet`

**MUTATING (default OFF).** The same thing for a crew — up to 100 cards per call,
each carrying name, photograph (or initials), designation, department, branch,
who they report to, camp and cabin, badge ID and QR.
Issues to anybody with no badge and reuses the live one where there is one.
**One employee's failure does not lose the sheet**: a name that resolves to
nobody is reported in `errors` and every other card is still printed.

**In the Desk**, this is the Employee list's Actions menu → *Print Badge Sheet*,
over whichever rows are ticked. That button is a **Client Script row**, not a
hook: an operator can see it, untick `enabled`, or delete it, and the app will
not put it back.

### `generate_employee_id_card`

```json
{"employee": "HR-EMP-00007"}
```

**MUTATING (default OFF).** One employee's card, **attached to their own
Employee record** so it is in the Attachments sidebar of the form somebody
already has open.

**The problem it solves is findability, not printing.** Badges were being issued
and then being unfindable: `generate_employee_badge_qr` answers with base64,
which is exactly right for a handset drawing a card on a screen and is nothing
at all in the Desk. This issues a badge if there is none, **reuses the live one
if there is** — it delegates to `generate_employee_badge_qr` rather than
reimplementing the mint, so a reprint still cannot consume an identifier — and
leaves two files on the Employee: the QR as a PNG, the card as a PDF.

**The layout is the print format's**, not a second opinion about a card: the same
markup and the same millimetres as *Employee Badge Card*, so the card off this
call and the card off the Desk Print button are the same card.

**The PDF is best-effort and the call still succeeds without it.** A card needs a
photograph and a QR, so it is the one document this app cannot draw with its own
dependency-free writer — it asks Frappe for wkhtmltopdf, which some bench images
have and some do not. Without it the badge is still issued, the QR is still
attached, `card_html` still comes back, and `card_attachment.note` says what is
missing. The two attachments are reported separately because they fail for
different reasons.

**In the Desk**, this is the **ID Card** button on the Employee form, under the
*Badge* group. It shows the card in a dialog, links to whichever files landed,
and prints from the browser on a bench with no PDF renderer.

### `resolve_badge`

```json
{"badge_id": "ETC-0042", "shift": "SHIFT-2026-00019"}
```

**Read-only, on by default.** Who holds this badge: `employee`, `employee_name`,
`designation`, `status`, `photo_url`. **The read that did not exist** —
`add_worker_to_shift` takes an Employee docname and a camera produces a badge
string, so a crew clock could scan a whole crew and roster none of it.

**It refuses rather than answering empty.** Never issued, retired, and belonging
to somebody who has left are three sentences, because they are three situations
with three fixes. A string that is not badge-shaped at all is refused before the
register is read.

Pass `shift` and the answer carries `on_shift` and `joined_at` — whether this
person is clocked in right now, which is what turns an identification into an
admission.

### The badge shape, and `badge_policy` on `sync_bucket_entries`

Two layers, and they refuse different things.

**The shape** (`bucket_bridge.validate_badge_id`) is a floor, not a grammar:
letters, digits, `-`, `_` and `.`, between 4 and 64 characters, not starting a
JSON document. That accepts a minted `ETC-0001` *and* a 36-character legacy uuid,
and refuses every URL, every Wi-Fi join code and every `api_key:api_secret` —
including a `generate_mobile_login_qr` payload held in front of a badge scanner,
which used to become a badge ID with a secret inside it. It applies under both
policies, because no later `link_badge_to_employee` will rescue a capture whose
badge is a Wi-Fi code.

**The register** is `badge_policy`. `lenient` is the default and is the v0.44.0
behaviour: a capture whose badge is not mapped yet is filed with no employee and
a later mapping backfills it — right for a Desk import of a morning taken before
anybody got to the cards. `strict` refuses it, and is what
`api/mobile.sync_bucket_entries` sends and does not let a handset relax: badges
are minted here now, so a phone scanning a string this site never issued has
scanned a barcode on a soda can. Under `strict`, a retired badge and one
belonging to somebody who has left are refused too, and passing `shift` also
refuses a picker who is not clocked in on that crew — each by name, with the
rest of the batch still filed.

## v0.47.1 — Form I-9 as a document

Four releases collected the data. These two produce the **form**, and file the
copy that comes back signed.

Everything through v0.47.0 filled in an I-9 Form record: Section 1, Section 2's
List A/B/C documents, receipts, Supplement B reverifications, a retention clock,
an append-only audit log. What an operator could actually put in a folder was a
Desk print of the doctype — a two-column dump of every field in the order the
JSON declares them. That is not Form I-9, and an inspection under
**8 U.S.C. §1324a(b)(3)** asks to see Form I-9.

**The page is the government's own.** This app now ships the USCIS fillable PDF
at `erpnext_mcp/templates/i9_form.pdf` — OMB No. 1615-0047, Edition 01/20/25,
four pages, 133 named AcroForm fields — byte for byte, with its SHA-256 asserted
by the test suite. `erpnext_mcp/i9_pdf.py` writes values into those fields and
hands back a copy; the file on disk is never edited. So the output is Form I-9
with the boxes filled, not a reproduction of it.

**Four things are deliberately left blank**, and `i9_pdf.py` argues each at
length:

| Left blank | Why |
|---|---|
| Both signature boxes | An electronic I-9 signature has to meet 8 CFR 274a.2(h)'s own requirements. A name typed into a `/Tx` field would *render* as a signature and would not be one. The capture and timestamp this app really holds go into Additional Information as what they are. |
| The SSN comb | Nine digits are printed only when `include_full_ssn` is passed **and** `store_full_ssn` is on. Otherwise the box is left for the employee's pen — which is how the paper form has always worked. |
| The alternative-procedure tick | Nothing in this app records whether the employer used the DHS remote-examination procedure, and a tick nobody chose is a false attestation. |
| Supplement B's new-name boxes | They are for a worker whose legal name changed. `I-9 Reverification` records the reason but not the new name. |

**One field in the government's file is on two pages at once.** `Document Title 1`
is a single AcroForm field with widgets in Section 2's List A block *and* in
Supplement B's second reverification row — so filling either box fills both.
`i9_pdf._split_shared_title` promotes the page-4 widget into a field of its own
**in the generated copy** before any value is written. It is the only place this
app edits the form's structure rather than its values, and it is asserted by name
in the tests.

### `render_i9_pdf`

**MUTATING (default OFF).** Fills the USCIS form from one I-9 Form record and
attaches the PDF privately to `generated_pdf`.

| Parameter | Required | Description |
|---|---|---|
| `i9_form` | yes* | The I-9 Form docname, e.g. `I9-2026-0001` |
| `employee` | yes* | The person instead of the form (`employee_name`, `name` are aliases) |
| `overwrite` | | Render even though `generated_pdf` is set, repointing the field |
| `include_full_ssn` | | Print the nine-digit SSN. Refused unless `store_full_ssn` is on **and** this form has one stored |
| `additional_information` | | Extra lines for the form's Additional Information box |

\* one of the two.

**A snapshot, not a view.** Anything that edits the form afterwards leaves the
PDF stale, so a second render is **refused** unless `overwrite` is passed — that
field probably holds the copy somebody already printed and had signed.
Overwriting repoints the field and **leaves the earlier File attached**.

**Refused on a Destroyed I-9.** Reconstituting a printable copy of a record
`destroy_i9` certified as disposed of is the one thing that certificate says did
not happen.

**Rendering moves no status**, because an I-9 is retained by the employer rather
than filed with anybody. The result names every required box the record left
empty, so `incomplete: []` is the check for "this page is ready to sign", and
reports `reverifications_not_on_page` when a long-returning seasonal worker has
more than Supplement B's three rows.

`include_full_ssn` is the only read of the encrypted `ssn_full` column anywhere
in this app, it needs the site switch as well as the argument, and it writes
`full_ssn: true` into the I-9 Audit Log row so a page carrying somebody's number
is findable afterwards.

### `attach_signed_i9`

**MUTATING (default OFF).** Files an already-uploaded signed or scanned I-9
against its record as the official signed copy.

| Parameter | Required | Description |
|---|---|---|
| `i9_form` / `employee` | yes | Which form, by docname or by person |
| `file_token` | yes* | The File docname, as `finalize_staged_file` hands it back |
| `file_url` | yes* | The File's URL, for something attached through the Desk |
| `overwrite` | | Replace a signed copy filed in error |

\* one of the two.

**This is the copy that matters.** Everything else on the record is the data that
was collected; this is the page two people signed. `generated_pdf` is only the
page it was printed from, and the doctype's own field descriptions say so.

**The file is uploaded first and named here.** No bytes cross this boundary — a
base64 body would be a second upload path with its own size limit and its own way
of failing halfway up a hill. **It is made private on the way in**, whatever it
was: a signed I-9 names a person, their date of birth and their immigration
status. Scans only: `.pdf`, `.jpg`, `.jpeg`, `.png`, `.heic`, `.heif`, `.tiff`,
`.tif`.

**A second signed copy is refused** unless `overwrite` is passed. It is the one
write on this doctype that could not be undone from the record itself.

### `collect_form_signature`

**MUTATING (default OFF).** Attaches one signature capture to the box on an I-9
or a W-4 that a missing-signature alert found empty, closes the Farm Task that
asked for it, and brings the rendered PDF back into step.

| Parameter | Required | Description |
|---|---|---|
| `doctype` | yes | `I-9 Form` or `W-4 Form`. A signature task carries it in `subject_doctype` |
| `name` / `form` / `employee` | yes | Which form, by docname or by person |
| `field` | | Which box. Optional where the doctype has only one |
| `signature_base64` | yes* | The capture's bytes, PNG or JPEG, up to 512 KB. A `data:` prefix is stripped |
| `file_token` | yes* | A File already on the site, for a capture uploaded in chunks |
| `row` | | Supplement B only: which reverification row. Defaults to the newest unsigned one |
| `task` | | The Farm Task to close. Found from the form and the alert type when omitted |
| `overwrite` | | Replace a signature filed in error |

\* one of the two.

**The other end of the missing-signature rules.** `i9_section_1_unsigned`,
`i9_section_2_unsigned`, `i9_supplement_b_unsigned` and `w4_signature_missing`
find the empty boxes; `generate_tasks_from_compliance_alerts` puts each on the
phone of whoever can fix it — the employee's supervisor for the two the *worker*
signs, an authorized signer for the two the *employer* signs — and this is what
that phone calls when the signature has been drawn.

**Four boxes, and the list is closed:** `I-9 Form.section_1_signature`,
`I-9 Form.section_2_signature`, `I-9 Form.section_3_signature` (Supplement B, a
child row) and `W-4 Form.signature`. A field outside them is refused with the
list — an endpoint that wrote an image into any column somebody named would be an
arbitrary write with an Attach-shaped hat on.

**It takes base64, which `attach_signed_i9` refuses to**, and the difference is
what is being sent. That one files a *scan of a page*: megabytes, taken on a
camera, chunked because a link that drops halfway through eight megabytes has to
be resumable. This one carries what a finger drew on glass: a few kilobytes of
monochrome PNG, complete in one gesture, where chunking would be three round
trips to move less data than the JSON around it. The **512 KB** ceiling is what
separates the two, and something over it is told which door to use. The format is
read off the first bytes, never off a filename.

**Refused before anything is stored:** a caller who may not `write` the form; a
caller not on the authorized-signer roster, *for the two employer boxes only*
(Section 1 and the W-4 are signed by the worker, who is on nobody's roster and
must not need to be); a box that already carries a signature, unless `overwrite`;
a destroyed I-9.

**Not refused, and reported instead:** a task that could not be closed — this
delegates to `complete_farm_task`, which will not take a completion from an
account that was not holding the task — and a PDF that could not be redrawn.
Neither undoes the signature. The capture is the compliance artefact; the task
and the PDF are bookkeeping about it, and a phone that stored a signature and
then lost the renderer has done the part §274a asks for.

The PDF step is **regeneration only**: a form that has never been rendered gets
nothing, because producing a federal form nobody asked for is not this call's
decision to make. Stored private, always. Logged to **I-9 Audit Log** as
`Signature Collected`.

## Signature evidence (v0.60.0)

A signature image proves that somebody drew a shape on a piece of glass. The
question an auditor asks second is **how do you know it was him**, and until
v0.60.0 the honest answer was that a phone said so.

**Signing Evidence** is one row per signature event, across every form that
carries one: who signed, in what capacity, which badge was scanned to prove they
were standing there, on what device, at what coordinates, from what address, and
against a **hash of the record as it stood when they were shown it**. It is
written by `collect_form_signature` and by nothing else — the doctype is
append-only, its controller refuses every write after the insert, `in_create`
keeps the Desk's New button off it, and **there is no tool that creates one**.
That absence is the design: a tool that could add a row would be a tool that
could manufacture an identity check that never happened.

**The capacity comes off the box, not off the caller.** Section 1 of a Form I-9
is a worker attesting under their own penalty of perjury and Section 2 is the
employer attesting that it examined that worker's documents. A caller may state a
`signature_role` and it is *checked* against the signature box; it is never
believed over it.

**A badge that resolves to the wrong person is refused**, before the image is
stored, on the boxes the worker signs in their own name. Verification that fails
open is not verification. No badge at all is not an error — an operator signing a
941 at a desk has no card to scan — and the row says `Unverified` rather than
claiming a check that did not happen.

**The hash is taken before the signature is written**, which is the only moment
that answers "what did they see". It covers **the columns that held something
when the record was presented**, and not the signature columns, the rendered PDF
or the workflow status — all of which this app writes as a consequence of
somebody signing. So information *added* later does not trip it (the employer
completing Section 2 in August on a form the worker signed in July), and
information *changed* or *erased* does. An integrity check that fired on every
correctly-handled form would be one nobody reads.

That rule cannot be re-derived after the fact — by the time anybody checks, some
of those columns hold something — so the row stores the field list beside the
hash. It doubles as the answer to a question an auditor would otherwise take on
trust: **which parts of this form does this signature vouch for.**
`get_signing_evidence` recomputes over exactly those fields on every read.

**A replaced signature appends.** `overwrite=true` writes a new evidence row
naming the old one in `supersedes`; the old row is never edited and never
deleted. A chain of custody that can be revised is not one.

**Permissions.** Farm Manager and Compliance Officer read it; System Manager and
HR Manager hold it fully. **No role in this app gets write access** — including
the two that read it, because the register's whole value is that nobody edits it.

| Tool | Default | What it does |
|---|---|---|
| `list_signing_evidence` | **on** | Signature events by document, signer, badge, capacity, company or date range. Reports `unverified_count` separately — the rows that cannot answer "how do you know it was him". |
| `get_signing_evidence` | **on** | One event in full, with the document hash re-checked against the record as it stands now, and `superseded_by` where the attestation was later replaced. |

## The document either side of a signature (v0.63.0)

The register above records steps 1 to 4 of the evidence chain and takes step 5's
hash of the *record*. Two of those steps had no artefact behind them, and both are
about the PDF rather than the row.

**Step 1 — the signer saw the form.** `API_CONTRACT.md` §17.5 records why the iOS
app could not show it to them: `render_i9_pdf` and `render_w4_pdf` answer with a
**private** `file_url`, the handset authenticates to the FarmOps sidecar with
`X-FarmOps-Token` rather than to Frappe, and a private URL is a login page to it.
So the app could show the *completed* form after signing — those bytes travel in
`submit_form_signature`'s answer — and not the blank one before. §17.5 called that
a server-side gap and said the fix is one route.

`get_document_preview` is that route. The page travels as base64 under `content`,
`content_base64` and `base64` — three spellings of one string, so a client written
against the contract, against this app's file tools or against the signature
answer all read the same page. It takes no signature and writes no signature
column. It **will draw the page once** where the record has none, which on a fresh
I-9 is every time; it will **not** silently replace one that exists, because
that field is the copy somebody printed and had signed. `stale` says whether the
record has changed since the page was drawn, and `refresh=true` asks for a
redraw. Showing a stale page to a signer means hashing something other than what
they read.

**The same gap, one register over (v0.92.2).** A worker's own pay stub is
attached to the payroll run, and `get_my_pay_stub_pdf` answered with a private
`file_url` nothing on the handset could open. `get_attachment_content` cannot
serve that one either: it asks Frappe for `read` on the **parent**, and the parent
is a run holding a slip for every person on it — HR-readable, correctly. So the
statement travels in that route's own answer, under the same three spellings. The
run itself joins `ATTACHMENT_PARENTS` as a **personnel** parent, so an HR account
can open a run's folder from a handset and nobody else can.

**Step 5 — the artefact is tamper-evident.** Flattening a form makes it tamper
*resistant*: there is no annotation to delete and no field to clear, so altering it
takes a real PDF editor. That says nothing about **detection** — an edited page
looks exactly like an unedited one. `seal_signed_document` adds the two things
that make an alteration noticeable:

- a **verification page**, appended, stating for every signature on the form who
  signed, the badge scanned, how identity was established, the moment, the device,
  the coordinates and the fingerprint of the record as it was presented;
- a **SHA-256 of the finished file**, recorded on every Signing Evidence row for
  the document. It cannot be printed on the file it is taken over — printing it
  would change the bytes — so the page carries the *document* fingerprint and the
  register carries the *file* hash. Two hashes, two questions: "is this the form
  they saw" and "is this the file we produced".

**An unsigned form is refused.** A verification page on a form nobody signed is an
official-looking appendix that vouches for nothing, and somebody would file it. A
signed form with **no** evidence row — every signature collected before v0.60.0 —
is sealed anyway, with the page saying in as many words that the identity, device
and location were never captured and cannot be reconstructed.

The sealed copy does **not** repoint `generated_pdf`: that is the working page
somebody prints, this is the retained artefact, and collapsing the two would mean
the next signature's redraw threw the seal away. No sealed copy is ever deleted,
so the chain of them is itself a record.

`submit_form_signature` takes the seal automatically and reports it under `seal`,
never fatally — a bench without reportlab gets `sealed: false` with the reason and
a signature that is on the federal record regardless. This tool is for the two
cases that step cannot cover: a form signed before v0.63.0, and one whose second
signature arrived through the Desk.

| Tool | Default | What it does |
|---|---|---|
| `get_document_preview` | **on** | The rendered form as bytes, so the person about to sign it can be shown it. Lists the form's signature boxes, which already carry one, and each box's verbatim attestation. |
| `seal_signed_document` | off | Append the verification page to a signed form, hash the finished file, and record that hash on every Signing Evidence row for it. |

## Authorized signers (v0.48.0)

Section 2 of Form I-9 is an attestation **under penalty of perjury** that a named
person examined the employee's documents, and until v0.48.0 `verifier_name` was
whatever string the caller sent. Form W-4's Employers Only block had the same
shape and the same gap. The roster is an **Authorized Signer** child table on
**I-9 Settings**: an account, the printed name, a title, a flag per form, and an
active flag.

**An empty roster authorises everybody, and that is the design.** A site that has
never added a signer behaves exactly as it did before this release — which is
what every site is on the day it upgrades, and a version that started refusing
signatures on migrate would break the I-9 flow on every farm running this app.
**So the first row is the switch.** Adding one signer turns enforcement on for
the whole site, and `add_authorized_signer` says so in its own result.

**Who signs is not who calls.** The calling account is matched against `user`;
the name written onto the form is `full_name`. `submit_i9_section_2` and
`submit_w4` take both off the roster row, so `verifier_name` becomes optional —
pass it only to file on behalf of **another signer on the roster**, which is a
real workflow (the foreman examined the documents, the office files the form) and
is checked against the roster rather than accepted as typed.

**Nothing is ever deleted.** `remove_authorized_signer` clears `active` and keeps
the row; there is no delete tool. A form signed last season was signed by whoever
was authorised last season, and a roster that forgets its own history cannot
answer the question a federal inspection asks. Deactivating the last active
signer leaves the roster *configured and empty*, which refuses every caller — the
tool warns when a call does that.

| Tool | Default | What it does |
|---|---|---|
| `list_authorized_signers` | **on** | The roster, plus `configured` — false means signing is unrestricted. Filter by `form_type` or `include_inactive`. |
| `add_authorized_signer` | off | Authorize one User. `full_name` falls back to the User's own. Refuses a second row for one account. |
| `update_authorized_signer` | off | Printed name, title, `can_sign_i9`, `can_sign_w4`, `active`. `active=true` is the way back from a removal. |
| `remove_authorized_signer` | off | Deactivate. Warns when it leaves nobody. |

## Form W-4 as a document (v0.48.0)

`render_w4_pdf` is `render_i9_pdf`'s counterpart and takes the same position: the
IRS publishes Form W-4 as a plain-paper fillable PDF, the employer retains it
rather than filing it, so the right output is **the government's own page with
the boxes filled in**. The template ships at
`erpnext_mcp/templates/w4_form.pdf`; `erpnext_mcp/w4_pdf.py` is the field table.

**The employer block is resolved at render time, not stored.** Step 5's Employers
Only row asks for three things the site already held and no W-4 could reach: the
employer's name and address and EIN come from I-9 Settings or the Company — the
*same* source Section 2 of the I-9 uses — and the first date of employment from
`Employee.date_of_joining`. Resolved rather than copied onto every row, so a farm
that changes its registered address does not have a hundred W-4s carrying the old
one. What *is* stored is who processed it: `employer_signer_name`,
`employer_signer_title` and `employer_signed_at`, written by `submit_w4` off the
authorized signer roster.

**Four things it does not put on the page.**

| Left blank | Why |
|---|---|
| The signature and date | The IRS form has **no signature field at all** — Step 5's lines are printed rules, not boxes. There is nothing to leave empty even by accident. |
| The SSN, Step 1(b) | A W-4 is completed by the *employee* and the number is theirs to write. `render_i9_pdf`'s gated decryption exists because the employer verified that number; adding a second call site to fill a box the employee is holding a pen over would be risk for no gain. |
| The exempt tick | It claims exemption from withholding for a whole year under penalty of perjury, and nothing in the W-4 Form doctype records that claim. |
| Pages 3 and 4 | The Multiple Jobs and Deductions worksheets. The IRS's own instruction is "keep the worksheet for your records" — the doctype stores the *result*, and the result is what goes on page 1. |

**The XFA payload is removed from the generated copy.** The IRS file is a hybrid:
an ordinary AcroForm plus an XML payload describing the same form. Acrobat renders
the XFA and ignores the AcroForm, so a fill that left it in place would produce a
file holding every right answer and printing blank in the reader an accountant is
most likely to open. The template on disk keeps its XFA; the copy does not.

### `render_w4_pdf`

**MUTATING (default OFF).** Fills the IRS form from one W-4 Form record and
attaches the PDF privately to `generated_pdf`.

| Parameter | Required | Description |
|---|---|---|
| `w4_form` | yes* | The W-4 Form docname, e.g. `W4-2026-0001` |
| `employee` | yes* | The person instead of the form (`employee_name`, `name` are aliases) |
| `tax_year` | | Which year's active W-4, when resolving by employee. Defaults to the most recent |
| `overwrite` | | Render even though `generated_pdf` is set, repointing the field |

\* one of the two.

**A snapshot, not a view**, and a second render is **refused** unless `overwrite`
is passed — same reasoning as the I-9's. **Rendering moves no status.** The
result names every required box the record left empty, reports
`employer_block_truncated` when the name-and-address line had to be cut to fit,
and reports `template_tax_year_matches`: the IRS revises W-4 every year, and a
2025 election printed on the 2026 page is a readable record where no form at all
is not, so a mismatch is reported rather than refused.

### One optional dependency, and a fallback that needs none

`pypdf` fills the form. It is a declared dependency and a normal install has it,
but it is imported defensively like `shapely`, `h3`, `segno` and `reportlab`
before it: a bench without it — or without the shipped template — loses exactly
`render_i9_pdf`, which says so by name with the pip command to fix it.
`attach_signed_i9` is unaffected, every I-9 value stays readable through
`get_i9_form`, and the **I-9 Form Print Format** (seeded on migrate, v0.47.1)
still lays the record out as the form's own sections in the Desk with no PDF
library involved at all.

---

# Adding a tool

Everything a tool needs is in two places:

1. A handler in the right module under `erpnext_mcp/tools/` — `read`, `mutate`,
   `workflow`, `accounts`, `banking`, `dimensions`, `fiscal`, `governance`,
   `assets`, `notes`, `opening`, `reports`, `files`, `collab`, `hr`, `trade`,
   `meta`, `packets`, `realestate`, `parties`, `investment_report`, `tax`,
   `company`, `farm`, `housing`, `compliance`, `evidence`, `calendar`,
   `auditpacket`, `dispatch`, `inspections`, `mobile`, `funnel`, `training`,
   `shifts`, `heat`, `kpi`, `kpidefs`, `payroll`, `payroll_gl`, `visits`,
   `sessions`, `rules`, `tasktemplates`, `budget`, `ml_model`, `bucket_log`,
   `asset_tags`, `feeds` or `fieldwork` —
   returning a `ToolResult(data, summary, docstatus_delta="")`. A new *compliance
   packet type* is not a new tool: it is one file in `erpnext_mcp/packets/`, and
   `docs/development.md` has the recipe.
2. An entry in `TOOLS` in `erpnext_mcp/registry.py`. If the tool needs something
   not every site has, give it an `available` predicate and a `requires`
   sentence; a tool that is advertised and always fails is worse than one that
   is absent.

Then add an `allow_<tool_name>` Check field to the ERPNext MCP Settings doctype —
default `"1"` for a read tool, `"0"` for a mutating one. The standalone test
`ShippedDefaults.test_every_tool_has_a_switch` fails if you forget, because a tool
with no switch is one an operator cannot turn off.

Read the switch, the audit row, the rollback-on-failure and the never-raise
contract come from `registry.dispatch`; a handler gets all four for free and
cannot opt out.

---

## v0.57.0 — Closing a compliance row from the field

### `dismiss_compliance_alert`

**MUTATING (default OFF).** The same dismissal `dismiss_alert` makes, gated on
the alert's own say-so.

| Parameter | Required | Description |
|---|---|---|
| `alert` | yes | The Compliance Alert docname. `get_compliance_calendar` lists them |
| `reason` | yes | Why this does not need doing. A sentence, not a word |

**The gate is the whole difference.** This refuses any alert whose `can_dismiss`
is not set, and `can_dismiss` defaults false on every alert the sweep raises.
`dismiss_alert` — unchanged, ungated — is the route for somebody at a desk with
the source record open in the next tab. This one is for the callers who are not
there: the Farm Ops app calls it from the compliance tab, and a model reading a
calendar is in the same position.

**Why almost nothing is dismissible.** An overdue housing inspection is not a
notification. Waving it off leaves a cabin uninspected and the calendar quiet
about it, which is why the mobile surface shipped with no dismiss at all. The
alerts that genuinely are stale — one raised against a lease terminated in May, a
duplicate of a filing already made elsewhere — are marked one at a time, on the
Compliance Alert's **May Be Dismissed From The Field** box, by somebody who can
see the whole picture. **The nightly sweep never writes that column**: it neither
grants the permission nor takes it away, exactly as it leaves a snooze alone.

The reason lands on `dismissed_reason` with `dismissed_by` and `dismissed_on`
beside it, through the same code the desk-side route uses. It is the entire audit
trail for an obligation nobody discharged, and dismissing the alert changes
nothing underneath it.

### What an alert now carries

`get_compliance_calendar`, `list_compliance_calendar_for_me` and the mobile
`list_compliance_alerts` gained two optional keys on every alert, and neither
changes anything that was already there:

| Key | Meaning |
|---|---|
| `can_dismiss` | Whether this alert may be closed without the work being done. False unless somebody said otherwise |
| `signature_request` | `{doctype, docname, signature_field, …}` for the four missing-signature rules — the blank box the alert is about, addressed |

`signature_request` is **derived at read time** from `tools/signatures.py`'s
closed table of signature boxes, which is the table the write path gates on. So a
pad can only ever be opened at a column `collect_form_signature` would accept ink
into, an alert raised before this release gets its address with no patch and no
sweep, and there is no second copy of the address to fall out of step with the
rule. Farm Tasks raised from those alerts carry the same object, off the same
alert, so the pad opened from the task list and the pad opened from the calendar
are addressed identically.

---

## v0.61.0 — What the operation pays, as opposed to what one person earns

Until this release a piece rate was a number on one worker's **Farm Salary
Structure**. $1.25 a bucket was typed once per picker, a season's raise was a
hundred edits nobody could audit, and the question "what does this farm pay for a
bucket" had no record to answer it from. Two registers replace that, and **what
each one is for is different, which is the whole design**:

| | Read | When | Effect of editing it |
|---|---|---|---|
| **Piecework Rate** | `(company, activity)` → rate per unit | on **every payroll run**, for every worker whose structure names no rate | the next run pays the new rate |
| **Position Wage Default** | `(company, designation)` → hourly rate | **once**, when a salary structure is created | nothing that already exists |

**The asymmetry is deliberate.** A piece rate is a property of the *work* — a
bucket is a bucket, and the operation pays what the operation pays — so a table
that governs it live is what makes a mid-season raise one row. An hourly wage is a
property of the *employment*: it is what a person was hired at, it is what a wage
claim asks about, and a table that could silently restate somebody's agreed rate
for a period already worked would be a table that rewrites history. So the hourly
default is **copied onto** the structure and the piece rate is **inherited by** it.

### The lookup order, which is the only thing payroll asks

```
1. the employee's Farm Salary Structure base_rate, where it is > 0
2. the active Piecework Rate for that employee's company and activity
3. neither — and that is an error, reported by name
```

**Step 1 wins because it is the more specific record.** A rate on one person's
structure is a rate somebody negotiated with that person, and a company table
cannot know about it. `> 0` rather than "is set" is what makes the fallback
reachable at all: `base_rate` is a required Currency field, so a structure created
without one holds `0.0`, not `None`.

**Step 3 is an error and not a zero,** and that is the failure this release
exists to make loud. A piece-rate worker paid at a rate of nothing earns nothing
at the rate, and what they are then paid is the **minimum wage makeup** — a real,
correct number. The slip balances, the run reports no failure, and the only
symptom is a makeup figure that looks like a rate set too low rather than a rate
never set at all.

**What a batch run does with that refusal is not to abort.** A picker with no rate
is reported into `employees_missing_piece_rates` and everybody else is paid — the
posture `run_payroll_for_period` has taken towards a missing salary structure
since v0.35.0. A single-employee `preview_payroll` has nobody else to hold up, so
there the refusal is the answer.

### Which activity, when the hours do not say

Neither Bucket Log Entry nor Farm Task Assignment records *what kind* of piecework
a count is. So the activity comes from the salary structure, which gained an
optional **`piecework_activity`** field — the worker's half of the
`(company, activity)` pair, in the same vocabulary `ML Model` already uses.

Where a structure names none, **one unambiguous company rate is used and several
are refused.** That is an observation rather than a guess: a company with one
piecework rate in force has already answered the question, and a company with
three has not. The refusal lists the candidates, because *"set piecework_activity
to one of: bucket_segmentation, thinning"* is an instruction somebody can act on.

Activities are compared case-folded, with spaces and hyphens read as underscores,
so `Bucket Segmentation` typed into the Desk and `bucket_segmentation` posted by
the iPad are the same activity.

### Raising a rate is a new row, not an edit

`select_effective` gives the row with the **latest `effective_from` that covers the
date**, so adding a row from 1 June and leaving the old one open-ended pays June
onwards at the new rate — and leaves the old row paying the periods it already
paid. That is what the dates are for, and it is why `update_piecework_rate` warns
on every edit that moves the rate on a row already in force.

**There is no delete on either table.** `is_active=false` takes a row out of every
future lookup and leaves it readable, because a rate that paid a period is the
record of what that period paid. At most one *active* row per
`(company, activity, effective_from)` — two rows starting the same day are two
answers to one question, and a rate that depended on which was created first is a
rate nobody can predict.

### Permissions

Farm Manager gets **read and write, not create**; Compliance Officer **read only**.
No name appears on either row — this is the *price list*, not somebody's pay — which
is what makes it safe in front of a role that cannot read Farm Salary Structure and
still cannot. Adding a row is how a raise happens, so the person who runs the
operation may correct the table and the person who sets what the operation pays
(System Manager, HR Manager) is who adds to it. The officer who checks whether a
rate cleared the minimum wage floor is not the account that set it.

| Tool | Default | What it does |
|---|---|---|
| `list_piecework_rates` | **on** | Every rate matching the filters, plus `in_force` — the one row per (company, activity) a run dated `on_date` would read |
| `get_piecework_rate` | **on** | One rate in full, whether it is the live row, and which row covers `on_date` instead if not |
| `list_position_wage_defaults` | **on** | Every default, plus the one row per (company, designation) a structure created on `on_date` would be seeded from |
| `get_position_wage_default` | **on** | One default in full, and whether it is the row a new hire in that job starts on |
| `create_piecework_rate` | off | Sets what a unit pays from a date. Also how a raise is made; names what it superseded |
| `update_piecework_rate` | off | Corrects a rate typed wrong, or retires it. `company` and `activity` are locked |
| `create_position_wage_default` | off | Sets the default hourly rate for a job title from a date |
| `update_position_wage_default` | off | Edits or retires a default. Structures already seeded from it are unchanged |

### What changed in payroll

`create_salary_structure` takes `piecework_activity`, seeds `hourly_rate` from the
position wage default when the caller names none, and **accepts `base_rate` 0 on a
Piece Rate structure** — where it now checks, at creation, that the inheritance will
actually resolve. A structure that would fail on payday fails in front of the person
creating it instead.

`get_salary_structure` reports `effective_rate` and `rate_source`, because a read
that showed the zero and stopped would be a read that says a picker earns nothing.
`list_salary_structures` flags `inheriting_company_piecework_rate` rather than
resolving every row. `preview_payroll` carries `piece_rate_source` and the rate's
docname, and the period runs carry two lists that are opposite facts:
`piece_rates_from_company` (the fallback working) and
`employees_missing_piece_rates` (the fallback finding nothing).

---

## v0.65.0 — one scan, whatever was on the tag

Farm Ops already had four scanners and every one of them required the person
holding the phone to know what they were about to scan **before** they scanned
it. In an orchard that is backwards: a worker walks up to a thing with a sticker
on it, and which register that sticker belongs to is the question, not the
premise.

### `universal_scan`

**WRITE (default OFF).** Takes the raw string a camera produced and resolves it
itself, then answers with the thing, the work outstanding on it, and what may be
done next.

| Argument | | |
|---|---|---|
| `content` | **required** | The scan as read. `scan`, `raw` and `code` are accepted spellings |
| `company` | | Resolve only within this company's registers |
| `shift` | | Farm Shift docname — badge branch only, adds `on_shift`/`joined_at` |
| `scanned_by` | | The User who scanned. Recorded on the asset branch only |
| `gps_lat` / `gps_lon` | | The scanner's fix. Recorded on the asset branch only |
| `history_limit` | | Timeline entries. Default 10, hard maximum 100 |

**The cascade, first match wins, on the exact docname:**

| # | Register | `entity_type` | Behind it |
|---|---|---|---|
| 1 | Bucket Log Badge Map | `employee` | `resolve_badge`, plus the open shift this person is on |
| 2 | Asset Register | `asset` | `scan_asset` — **the only branch that writes** |
| 3 | Housing Unit | `housing_unit` | `get_housing_unit` |
| 4 | Field | `field` | `get_field` |
| 5 | — | `unknown` | the string as scanned, and which registers were searched |

**The badge is first because a badge is a person.** A string that is somehow in
both the badge register and the Asset Register resolves to the worker, because
attributing somebody's piece work to a sprayer is the one confusion here with a
payroll consequence and the one nobody can unpick afterwards.

**The match is an exact docname.** `asset_row`, `unit_row` and `field_row` all
fall back to a `LIKE` search on a partial name — right for an operator typing
half a name into a tool, wrong for a cascade, where it would let a cabin's
sticker resolve to whichever valve happened to share its prefix.

**A printed tag encodes a URL.** `Asset Register` builds `qr_url` as
`<public url>/scan/<name>`, so what a camera actually hands over is
`https://erp.example.com/scan/MC-Valve-05`. That path is unwrapped — and
percent-decoded — before any register is read. A bare string is passed through
untouched.

**Refusals pass through rather than falling through.** A retired badge, one
belonging to somebody who has left, and a record in another company each get the
sentence the tool that owns them writes. A card that *was* issued is a badge
whatever its state, and demoting it to "unknown tag" would be the wrong sentence
in every one of those cases.

**Unknown is an answer, not an error.** A supplier's carton barcode or a
hand-written label comes back with `entity_type: "unknown"`, the content whole,
`searched` naming the registers that were actually consulted, and `create_task`
still offered — the scan that resolves to nothing is the one most worth raising a
job about, and the task needs the string.

**One scan is refused instead: a credential document.** `generate_mobile_login_qr`
mints `{"url":…,"api_key":…,"api_secret":…}`, and what a camera reads by accident
is whichever QR is nearest. That string is refused before any register is read and
is **not quoted back** — the same decision `resolve_badge` makes at the badge step,
made here at the door in front of all four registers.

**Response**

```json
{
  "content": "https://erp.example.com/scan/MC-Valve-05",
  "resolved_from": "MC-Valve-05",
  "entity_type": "asset",
  "entity": { "name": "MC-Valve-05", "asset_type": "Irrigation Valve", "...": "..." },
  "entity_name": "MC-Valve-05",
  "pending_tasks": [], "pending_task_count": 0,
  "overdue_tasks": [], "overdue_task_count": 0,
  "due_compliance": [], "due_compliance_count": 0,
  "recent_history": [],
  "available_actions": ["create_task", "log_state_change", "report_issue"],
  "scan_recorded": true
}
```

Every key is present on every answer, empty where it does not apply, so one
client struct decodes all five entity types.

| `entity_type` | `available_actions` |
|---|---|
| `employee` | `create_task`, `view_compliance`, `view_i9` |
| `asset` | `create_task`, `log_state_change`, `report_issue` |
| `housing_unit` | `create_task`, `start_inspection`, `log_state_change` |
| `field` | `create_task`, `view_irrigation` |
| `unknown` | `create_task` |

**`overdue_tasks` is a subset of `pending_tasks`, not a partition of it.** A Farm
Task carries no due date of its own, so *overdue* here means the Compliance Alert
the task answers was due before today; a hand-raised task has no date and is
never overdue. Every task carries `due_date` and `overdue`, so a client rendering
only `pending_tasks` still shows the late work and nothing has to be re-derived on
the handset.

**Government IDs are deliberately absent from the cascade.** A licence's PDF417
and a passport's MRZ are parsed on the handset and routed to the hiring wizard
there. Those barcodes carry a date of birth, a document number and an address, and
posting them to a server so it can name what the phone already knows would put an
identity document into an HTTP body, an audit row's arguments and a log file for
no answer the phone did not already have.

### The mobile route

`POST /farmops/api/mobile/universal_scan` publishes the same call to a handset.
It is **metered as a read** (sixty a minute, `resolve_badge`'s limit) and
**declared as a write**, and both are deliberate: a crew clock scanning a queue at
a bin trailer is forty pure reads in a minute, and `WRITE_LIMIT` would refuse the
crew rather than the abuse. The company comes from the caller's own scope — a
`company` in the body may narrow it and can never widen it — and every task and
alert leaving the route is checked against that scope on the way out.

---

# Master data (v0.66.0)

*Nineteen tools — ten reads and nine writes — over the records every other
ERPNext document points at: `Item`, `Item Group`, `Supplier`, `Customer`,
`Warehouse`, `Price List` and `Item Price`.*

Until this release the app could read an order book and age a receivable but
could not name the chemical, the supplier who sold it or the shed it is locked
in — so every workflow ending in a document ended instead at "open the Desk and
create the master first".

**Read the next three paragraphs before calling anything here. `company` means a
different thing on each of these doctypes, and every tool reports which of the
three it applied.**

**A Warehouse really is company-scoped.** `Warehouse.company` is a column, the
docname carries the company's abbreviation (`Chemical Shed - CFL`), and
`list_warehouses(company=…)` is an exact filter.

**An Item is not.** ERPNext moved per-company defaults out of `Item` and into the
`item_defaults` child table in v12. An Item with **no default row at all** is
usable by every company on the site — so `list_items(company=…)` means "the items
this company has set a default for" and **hides the rest**. The response says so
in `company_scope`; a caller reading the count as "this company's catalogue" is
wrong, and the shorter list will not tell them.

**A Supplier and a Customer are neither.** Stock ERPNext puts no company column
on either. The argument is accepted (a model will send it), **validated** — a
company that does not exist is still a mistake worth hearing about — and reported
back as not applied in `company_scope`. It is never silently dropped.

**Nothing here is submittable, so nothing here is a draft.** None of these
doctypes has a docstatus: an Item is live the moment it is inserted, and there is
no "submit it later" step to hold it back. Every create tool returns
`"submittable": false` and says so. The nearest thing to a draft is `disabled`,
which is a real field on Item, Supplier, Customer and Warehouse and which
`create_item` will set on request.

**A reorder level needs a warehouse, and that is ERPNext's rule.** A reorder rule
lives on an `Item Reorder` row keyed by the warehouse it applies to; "reorder at
50" with no shed named is not a thing the doctype can store. `update_item` takes
`reorder_warehouse`, falls back to the item's own default warehouse, and refuses
with that sentence when there is neither — rather than writing the rule against
whichever warehouse sorted first.

**A price is not a field on an Item.** Prices are `Item Price` rows: one per item
per price list per UOM, optionally narrowed to one customer or supplier and to a
date window. `set_item_price` matches on the **whole** key before deciding
whether to create or update, because matching on the item and the list alone
would overwrite a customer's negotiated rate with the list rate.

## `list_item_groups`

The Item Group tree, flat. Each row names its `parent_item_group`; `roots` lists
the nodes with no parent separately. Call it before `create_item`.

Arguments: `parent_item_group`, `is_group`, `limit`.

```json
{"item_groups": [{"name": "All Item Groups", "item_group_name": "All Item Groups",
                  "parent_item_group": null, "is_group": true},
                 {"name": "Farm Chemicals", "item_group_name": "Farm Chemicals",
                  "parent_item_group": "All Item Groups", "is_group": false}],
 "count": 2, "roots": ["All Item Groups"]}
```

`is_group` comes back as a real boolean. A Check field read with a bare `bool()`
reports `"0"` as true, which would make every leaf look like a branch — and a
branch is exactly what `create_item_group` demands as a parent.

## `create_item_group` — MUTATING, default off

Arguments: `item_group_name` (required), `parent_item_group` (default `All Item
Groups`), `is_group`.

Refused, each with `Nothing was created`: a name already taken (ERPNext names an
Item Group after itself, so the name **is** the docname), a parent that does not
exist (the site's own group nodes are listed), and a parent that is a **leaf**.

## `list_items`

Arguments: `item_group`, `is_stock_item`, `disabled`, `company`, `search`
(substring of `item_name`), `limit`.

Returns `item_code`, `item_name`, `item_group`, `stock_uom`, `is_stock_item` and
`disabled` per row, plus `by_item_group` counts and the usual `truncated` flag.
See the `company` paragraph above for what a company filter does and does not
include.

## `get_item`

One Item in full. On top of the list fields: `description`, the flags, and the
two child tables that matter —

```json
{"item_code": "SURROUND-WP", "item_name": "Surround WP", "stock_uom": "Lb",
 "item_defaults": [{"company": "Constancy Farms LLC",
                    "default_warehouse": "Chemical Shed - CFL",
                    "default_price_list": null, "buying_cost_center": null,
                    "selling_cost_center": null, "expense_account": null,
                    "income_account": null}],
 "reorder_levels": [{"warehouse": "Chemical Shed - CFL", "reorder_level": 50.0,
                     "reorder_qty": 200.0, "material_request_type": "Purchase"}],
 "default_warehouse": "Chemical Shed - CFL",
 "default_warehouse_note": "this site keeps default warehouses in the item_defaults child table…"}
```

`default_warehouse` at the top level is a convenience, filled **only** when there
is exactly one default row, with a note saying where the real answer lives. On a
pre-v12 site with a flat `default_warehouse` field, that field is reported
directly and the note is absent.

## `create_item` — MUTATING, default off

Arguments: `item_code` (required), `item_name` (defaults to the code),
`item_group` (defaults to `All Item Groups`), `stock_uom` (defaults to `Nos`),
`is_stock_item` (defaults to true), `description`, `disabled`,
`default_warehouse`, `company`.

The `stock_uom` is checked against this site's own UOM list and refused with the
units it actually has — ERPNext ships around a hundred and a farm uses six, and
`Lbs` where the site says `Lb` should get the list rather than a link error from
inside the insert. A `default_warehouse` lands on the `item_defaults` row for the
company, which is **inferred from the warehouse itself** when `company` is
omitted; a warehouse belonging to another company is refused.

## `update_item` — MUTATING, default off

Changes `description`, `item_name`, `item_group`, `disabled`,
`default_warehouse`, `reorder_level` / `reorder_qty` / `reorder_warehouse`.
**Never renames** — the `item_code` is the docname.

```json
{"name": "SURROUND-WP",
 "changed": {"description": ["Kaolin clay particle film", "Kaolin clay, OMRI listed"]},
 "reorder": {"stored_on": "Item Reorder row", "warehouse": "Chemical Shed - CFL",
             "created": true, "reorder_level": 50.0, "reorder_qty": 200.0}}
```

A second reorder write against the same warehouse updates that row rather than
adding a second one. A value that already matches what is stored is reported as
an empty `changed` map rather than saved.

## `list_suppliers` / `list_customers`

One implementation with the nouns swapped, for the reason `list_sales_orders` and
`list_purchase_orders` are one implementation: a fix to the company reporting or
the truncation must not be able to land on one side only.

Arguments: `supplier_group` / `customer_group`, `territory` (customers),
`sales_channel` (customers), `disabled`, `company`, `search`, `limit`. An unknown
group, territory or channel is refused **with the site's own list**, rather than
answering with zero rows.

`list_customers` also returns `by_sales_channel` and `without_sales_channel`, and
**the second is the half that matters**: a direct-marketed percentage computed
over invoices is only as complete as this classification, and a report that
showed the percentage without showing how many customers were never classified
would present a partial answer as a whole one. A blank channel is not
not-direct.

## `get_supplier` / `get_customer`

Accepts a docname or the display name. On top of the stored fields:

- `company_accounts` — the per-company payable/receivable **overrides**. Empty is
  the normal case and means the party posts to each company's own control
  account.
- `addresses` — found the way ERPNext links them, through the `Dynamic Link` row
  on the **Address**, not through a field on the party. A site without ERPNext's
  address module gets `[]` rather than an error.

## `create_supplier` / `create_customer` — MUTATING, default off

`supplier_name` / `customer_name` required. Groups default to `All Supplier
Groups` / `All Customer Groups`, and a Customer's territory to `All Territories`
— each used only where the site actually has that record, and otherwise refused
with what the site does have.

`sales_channel` (customers) is `Direct` / `Wholesale` / `Packer` / `Processor` —
a Custom Field this app installs at migrate time, and the thing that makes "what
percentage of each commodity do you direct-market" a query over Sales Invoice
rather than a number somebody types. `Crop.pct_direct_marketed` is the typed
fallback for the year the invoice data is not clean, and it is labelled as typed.

`supplier_type` / `customer_type` are matched **case-insensitively against this
site's own Select options** and stored in the doctype's casing, so `company`
becomes `Company` and `Partnership` is refused with the real choices listed. The
options are read off the site's meta rather than hardcoded, so a customised
Select answers with its own values.

## `update_supplier` / `update_customer` — MUTATING, default off

Group, type, territory, `sales_channel` (customers), `disabled` and the tax
identifiers, in place, never renaming. **This is where an existing customer
register gets classified** — nothing backfilled `sales_channel` and nothing will,
because whether a buyer is a farm stand or a packer is a fact only the farm has,
and a migration that guessed would produce a direct-marketed percentage that
looks computed and is invented. Returns `changed` as before/after pairs, and refuses when nothing sent
differs from what is stored — an update that changes nothing and reports success
is how a caller concludes a value was written when it was not.

## `list_warehouses`

Arguments: `company`, `is_group`, `disabled`, `parent_warehouse`, `limit`. Flat,
each row naming its parent, with `roots` listed separately. This is the one place
in this section where the obvious reading of `company` is the right one.

## `create_warehouse` — MUTATING, default off

Arguments: `warehouse_name` (required), `company`, `parent_warehouse`,
`warehouse_type`, `is_group`, `city`.

ERPNext names a Warehouse `"<warehouse_name> - <company abbr>"`, and **that
docname is predicted before anything is written**, so a collision comes back as a
sentence naming the docname rather than as a framework error. The same mechanism
is why two companies can each have a `Stores`. The parent defaults to the
company's own root group (`All Warehouses - <abbr>` on a stock install); a parent
that is a leaf, or belongs to another company, is refused.

## `list_price_lists`

`enabled`, `buying`, `selling`, `limit`. A Price List holds no rates itself.

## `get_item_price`

Every `Item Price` for one item, optionally narrowed by `price_list`, `uom`,
`customer` or `supplier`.

Pass `as_of` and the response adds `applicable` — the subset whose
`valid_from`/`valid_upto` window covers that date, **with an open end treated as
still in force**, which is what most rows on a real site look like. The full
`prices` list survives the filter: "what was it in March" and "what prices exist"
are both questions somebody asks.

`price_list_rate` is filled **only** when exactly one row applies. Two rows and
no date is not a price, and choosing between them is ERPNext's pricing rules to
do, not this tool's.

## `set_item_price` — MUTATING, default off

Arguments: `item_code`, `price_list`, `rate` (all required), `uom`, `customer`,
`supplier`, `currency` (defaults to the price list's own), `valid_from`,
`valid_upto`.

```json
{"name": "IP-0002", "created": false, "item_code": "SURROUND-WP",
 "price_list": "Standard Buying", "price_list_rate": 2.65, "previous_rate": 2.40,
 "currency": "USD", "uom": "Lb", "valid_from": "2026-07-01"}
```

Refused: a negative rate, a `customer` **and** a `supplier` together (an Item
Price is one or the other), an inverted validity window, and — the one worth
knowing — a key that matches **more than one** existing row. That means the site
has duplicates ERPNext's own check would have refused, and picking one of them
silently is how a rate somebody negotiated disappears.

---

# v0.67.0 — Receipt capture: scale tickets and settlements

Nine tools over the two documents that stand between a load of fruit leaving the
orchard and the money arriving for it. Five reads, four writes, and one of the
reads touches no doctype at all.

## The idea: the receipt is the financial atom

A foreman photographs a piece of paper. Which piece of paper it is decides which
register it lands in, and that decision is the only branch in an otherwise
identical flow — photograph, extract, file, review:

| The paper | Lands in | Tool |
|---|---|---|
| a fuel or parts slip | Expense Receipt | `submit_expense_receipt` (v0.31.0) |
| a scale ticket | **Scale Ticket** | `create_scale_ticket` |
| a packout settlement | **Settlement Statement** | `create_settlement_statement` |
| a vendor invoice | Purchase Invoice | `create_purchase_invoice_from_receipt` (v0.68.0), on an Approved receipt |

`classify_receipt` is that branch, published as a tool.

## Why a Scale Ticket is not a Delivery Note

ERPNext has a Delivery Note and it is the wrong shape. A Delivery Note is the
*seller's* document about goods leaving on the seller's terms: priced, against a
Sales Order, in Items and UOMs the seller controls. A scale ticket is a third
party's weight record — the packer owns the scale, the packer prints the slip,
the variety is whatever the packer's clerk wrote, and **there is no price on it
at all**. The price arrives months later, on a settlement.

Forcing it onto a Delivery Note would mean inventing an Item per variety per
grade before a foreman could photograph a slip at a tailgate. A capture that
requires master data to exist first is a capture that does not happen.

## The two registers are independent, and that is the whole audit

A Settlement Statement's `gross_delivered_weight` is what the **packer** says
arrived. The sum of the Scale Tickets matched to it is what the **grower's** own
copies say arrived. Nothing here reconciles them, overwrites one with the other,
or refuses a settlement whose figure disagrees.

`get_settlement_statement` reports both and names the difference in
`delivery_reconciliation`, because the difference *is* the answer. It is the
entire reason a grower keeps ticket stubs, and a tool that quietly agreed them
would delete the only audit either document has.

## Both registers are submittable, and create is always a draft

`create_scale_ticket` and `create_settlement_statement` leave the document at
docstatus 0. Submitting a scale ticket makes a third party's weight record
immutable, and an operator who wants a phone able to *capture* tickets does not
necessarily want the same phone able to *freeze* them. One switch cannot express
that; two can.

`status` on both is **computed from `docstatus`**, never typed:

| Scale Ticket | | Settlement Statement | |
|---|---|---|---|
| `Draft` | docstatus 0 | `Draft` | docstatus 0 |
| `Submitted` | docstatus 1, no settlement | `Submitted` | docstatus 1, no JE |
| `Matched` | docstatus 1, settlement set | `Posted` | docstatus 1, JE set |
| `Cancelled` | docstatus 2 | `Cancelled` | docstatus 2 |

**Nothing in v0.67.0 sets `posted_journal_entry`.** The tool that books
settlement proceeds to the ledger is a later sprint; the column and the state
exist now so that sprint does not have to migrate every statement captured
before it.

## `list_scale_tickets`

**READ (default ON).** Filters: `customer`, `company`, `status`, `variety`,
`field`, `settlement`, `unmatched`, `from_date`/`to_date`, `limit`.

`unmatched: true` is the question the register exists to answer — which
delivered loads no settlement has claimed yet.

```json
{"scale_tickets": [{"name": "ST-ETC-0004", "ticket_number": "44718",
                    "date": "2026-09-14", "customer": "Blue Ridge Packing",
                    "variety": "Honeycrisp", "gross_weight": 18400.0,
                    "tare_weight": 6200.0, "net_weight": 12200.0,
                    "weight_uom": "Lb", "status": "Submitted",
                    "settlement": null}],
 "count": 1, "total_net_weight": 12200.0,
 "by_status": {"Submitted": 1}, "by_weight_uom": {"Lb": 1}}
```

`total_net_weight` sums the rows returned, and `by_weight_uom` is beside it
because **kilos and bins do not add**. A single total spanning both units is a
fiction, and the count is how a reader knows whether they are looking at one.

## `get_scale_ticket`

**READ (default ON).** One ticket in full, plus `weight_check` — the subtraction
restated so a reader can see the sum rather than being asked to trust it:

```json
{"weight_check": {"gross_weight": 18400.0, "tare_weight": 6200.0,
                  "net_weight": 12200.0, "weight_uom": "Lb",
                  "computed_as": "18400.0 - 6200.0 = 12200.0"}}
```

`settlement_detail` is present when a settlement has claimed the load.

## `create_scale_ticket` — MUTATING, default off

Required: `ticket_number`, `date`, `customer`. Also takes `company`, `variety`,
`grade`, `gross_weight`, `tare_weight`, `weight_uom`, `field`, `block`,
`truck_id`, `driver`, `destination`, `ticket_image`, `notes`.

**`net_weight` is not an argument.** It is gross minus tare, computed by the
controller and read-only in the Desk. A net weight somebody typed is the number
a settlement dispute turns on with no arithmetic behind it. Where the slip's own
printed net disagrees with the subtraction, **that disagreement is the finding**
— it goes in `notes` beside the photograph, not silently into the field the
grower will later argue from.

A `tare_weight` above the `gross_weight` is refused: it is two numbers off
different tickets, or a gross in pounds against a tare in kilos, and a negative
net propagating into a settlement check would make the check say the packer
overpaid.

`ticket_number` is the **packer's** number and is not unique on this site. Two
packers will both have a ticket 4471 sooner or later, and a uniqueness
constraint would refuse the second grower's real ticket. The docname carries the
company abbreviation instead — `ST-OML-0001` — because the question asked of a
scale ticket is "whose fruit", not "which season".

## `submit_scale_ticket` — MUTATING, default off

Takes `name` (aliases `scale_ticket`, `ticket`). Refuses an already-submitted or
cancelled ticket by name. A ticket with **no weight at all** is refused at this
moment rather than at capture — a draft is a record in progress, and a foreman
at a tailgate may have the truck before they have read the scale.

## `list_settlement_statements`

**READ (default ON).** Filters: `customer`, `company`, `status`,
`from_date`/`to_date`, `limit`. Returns each statement's packout and cull
percentages, deductions and net proceeds, plus site totals for the rows matched.

## `get_settlement_statement`

**READ (default ON).** Lines, deductions, computed percentages, the money, the
Scale Tickets it claims — and `delivery_reconciliation`, which is the part to
read first:

```json
{"delivery_reconciliation": {"packer_gross_delivered_weight": 48000.0,
                             "matched_ticket_net_weight": 49850.0,
                             "weight_uom": "Lb", "variance": -1850.0,
                             "matched_ticket_count": 4,
                             "tickets_in_other_units_excluded": 0}}
```

A negative variance means the tickets say more fruit was delivered than the
packer paid for. Neither figure is derived from the other and neither is
corrected. Tickets in a different weight unit are **counted and excluded**
rather than converted — there is no bins-to-kilos conversion this app knows, and
a fabricated one would put a fabricated variance on the answer.

## `create_settlement_statement` — MUTATING, default off

Required: `statement_number`, `date`, `customer`. Also takes `company`,
`period_start`, `period_end`, `gross_delivered_weight`, `packed_weight`,
`cull_weight`, `weight_uom`, `line_items`, `deductions`, `statement_image`,
`notes`.

```json
{"line_items": [{"variety": "Honeycrisp", "grade": "XF",
                 "packed_weight": 31200, "price_per_unit": 0.62,
                 "price_uom": "Lb", "gross_amount": 19344.0}],
 "deductions": [{"deduction_type": "Packing", "description": "Pack charge", "amount": 6240.0},
                {"deduction_type": "Cold Storage", "description": "Sep–Nov", "amount": 1120.0}]}
```

**Five numbers are computed and cannot be passed:** `packout_pct`, `cull_pct`,
`total_gross_revenue`, `total_deductions`, `net_proceeds`. The two percentages
are how one packer is compared with another, and a percentage nobody recomputed
is a percentage nobody checked.

A line's `gross_amount` is filled from `packed_weight × price_per_unit` where the
statement left it blank, and left alone where the statement gave one — a packer
who applied a promotion to a line is telling the truth and the multiplication is
not. `price_uom` is a separate field because a packer quotes per box and weighs
in pounds often enough that assuming they agree would misprice a line by a
factor of forty.

**Nothing is reconciled.** The lines are never checked against `packed_weight`
(fruit still in storage and fruit repacked into a later pool live in the gap),
and `cull_weight` is never derived from delivered minus packed (juice and shrink
live in that one). Deriving either would manufacture a number growers
renegotiate contracts over.

A **negative deduction is refused**. A deduction is already a subtraction, so a
negative one would add to the proceeds; if the packer credited something back,
that is a line item.

## `submit_settlement_statement` — MUTATING, default off

Takes `name` (aliases `settlement_statement`, `settlement`, `statement`).
Refuses an already-submitted or cancelled statement. **It posts nothing to the
ledger.**

## `classify_receipt`

**READ (default ON), and it touches no doctype at all.** Takes `merchant`,
`description`, `text` (alias `ocr_raw_text`) and `amount`; returns which of
`expense`, `scale_ticket`, `settlement` or `bill` the document is.

```json
{"receipt_type": "scale_ticket", "confidence": 0.71,
 "default_applied": false,
 "matched_signals": ["scale ticket", "gross wt", "tare wt", "net wt", "bins"],
 "scores": {"scale_ticket": 10, "settlement": 1, "bill": 0, "expense": 0},
 "alternatives": [{"receipt_type": "settlement", "score": 1,
                   "matched_signals": ["packed"]}],
 "suggested_tool": "Scale Ticket (create_scale_ticket)"}
```

**Rules, not a model, and the reason is accountability rather than cost.** The
classifier runs while somebody is standing in front of the thing they
photographed, and it is *allowed* to be wrong — the app shows its answer as a
pre-selected tab a person can change. What it is not allowed to be is
unarguable. "Why did it file my ticket as an expense" has to have an answer, and
`matched_signals` is that answer; a keyword table is auditable by reading it.

**Confidence is the winner's share of all matched evidence, scaled down when
there was little evidence to share** (full weight at a score of 4), and capped at
**0.95** — a keyword rule is never certain, and 1.0 is an instruction to the
client to stop asking.

**Nothing matching returns `expense` with confidence 0 and
`default_applied: true`** — a fallback, stated as one, rather than a guess
wearing a number. Expense is where an unrecognised slip does least harm, because
it is the register a person reviews anyway.

**Ties break toward the more specific document** — `settlement`, then
`scale_ticket`, then `bill`, then `expense`. A settlement quotes weights and so
always picks up scale-ticket words; a scale ticket almost never says "packout".
`expense` is last because it is also the fallback, and a fallback that could win
a tie would swallow the two registers this release exists to fill.

**`amount` is echoed back and never used to classify.** A $9,000 fuel bill and a
$9,000 settlement are the same number, and a rule on it would be a rule on farm
size.

`bill` currently has no register to land in, and `suggested_tool` says so rather
than pointing at something that does not exist.

## The four mobile routes

`POST /farmops/api/mobile/` — `classify_receipt`, `create_expense_receipt`,
`create_scale_ticket`, `list_scale_tickets`.

`submit_scale_ticket`, `create_settlement_statement` and
`submit_settlement_statement` have **no route**, each for its own reason:
submitting freezes a third party's weight record, and a settlement is a
multi-page document that arrives at an office rather than a thing anybody
photographs at a tailgate. A method with no route 404s, which is the design of
that table.

On `create_expense_receipt` and `create_scale_ticket` the **company comes from
the caller's scope** and `submitted_by` from the authenticated account — an
account that can name somebody else in a request body is not scoped to anything.

---

# v0.67.1 — Correcting a Section 1 that is already filed

## The hole this closes

Every I-9 tool before this one moves a form **forward**. `submit_i9_section_1`
takes a Draft and leaves it at `Section 1 Complete`; nothing takes it back. So a
Section 1 filed with a blank date of birth — because the caller that filed it
never sent one — had **no route to a date of birth through any tool in this
app, on any status**. The form read `Complete`, its PDF was rendered and
attached, and the retained federal record was missing a box Section 1 asks for.

That is the whole of the case for `patch_i9_section_1`, and it is why the tool
is as narrow as it is.

## `patch_i9_section_1` — MUTATING, default off

| Argument | Required | Meaning |
| --- | --- | --- |
| `i9_form` / `name` / `employee` | yes | Which form, by docname or by the person it belongs to — resolved the same way `render_i9_pdf` resolves it |
| `date_of_birth` | one of the four | `YYYY-MM-DD` |
| `email` | one of the four | Email address, as Section 1 asks for it |
| `phone` | one of the four | Phone number, as Section 1 asks for it |
| `ssn_last_four` | one of the four | Last four of the SSN. A longer number is stripped to its last four; a shorter one is refused |
| `reason` | no | Recorded verbatim in the audit row. Worth sending — it is the sentence an inspection reads beside the change |

**Four columns, and it will not be talked into a fifth.** Each of the four is a
*transcription* of something the employee already told the employer, so a wrong
one is a typing mistake and correcting it changes nothing the form attests to.

**The name, the address, the citizenship status and the immigration identifiers
are refused BY NAME.** Those are what the employee swore to under penalty of
perjury above their own signature, and a form whose sworn answers were edited
after the signature was made is a form whose signature no longer covers what it
says. They are changed by re-attesting, not by patching. A call naming one is
refused outright — and the patchable field sent alongside it is **not** written
either, because a partial success would leave the caller believing the refused
one landed.

`ssn` — the nine-digit argument `submit_i9_section_1` takes — is refused here
too. It reaches the encrypted `ssn_full` column through its own site switch, and
a correction path that quietly wrote it would route around that switch.

**A value is required for every field named.** This corrects a field to the
right answer; it does not clear one. A blank column on a filed I-9 is the gap
this tool exists to close, not one for it to open.

**Statuses.** `Section 1 Complete` and `Complete` only. A `Draft` is refused and
told to use `submit_i9_section_1`, which is the tool carrying Section 1's own
rules — an Alien Authorized to Work still has to answer with one of the three
identifiers, and a patch tool that took a Draft would be a second way in that
skips them. A `Destroyed` record is refused for the reason `render_i9_pdf`
gives. A form resting at `Awaiting Verification` is **also** refused today, and
that is a known gap rather than a decision: its Section 1 is filed and it has no
correction path.

**Moves no status and signs nothing.** A `Complete` form stays Complete and both
attestation timestamps are untouched — fixing a typo does not make the employee
have signed on a different day.

**Requires System Manager, HR Manager or HR User** on the account this app acts
as. Narrower than the personnel tools by one role: `Farm Manager` may hire on
this site, and amending a retained federal record afterwards is a different
question from hiring.

## What the audit row says, and what it does not

Logged to I-9 Audit Log as **`section_1_correction`**, carrying `fields` (which
changed), `was_blank` (which were empty before), `status`, `corrected_by` and
`reason`.

**It does not carry the values.** Same rule `submit_i9_section_1` follows for
the immigration identifiers: an audit row is a second doctype, and a date of
birth or four SSN digits copied into a JSON blob on it is one more place a
personal identifier lives. What an inspection asks of a corrected I-9 is *who
changed what, and when* — which is exactly what a lined-through, initialled and
dated paper correction records. The values themselves are on the form, and
Frappe's own Version row carries the before.

## The rendered page is redrawn

The attached PDF is the copy an inspection is shown. One still carrying the
empty date of birth the correction just filled in is the record and its
printable copy disagreeing about the fact somebody would print it to prove — so
`generated_pdf` is redrawn with `overwrite`, and the File that was there stays
attached to the record.

A form that has **never** been rendered gets nothing: producing a federal form
nobody asked for is this app deciding something that is not its to decide, and
`render_i9_pdf` is one call away. The redraw also never raises — a bench without
`pypdf` ends with a corrected record and a stale page, which is a smaller
problem than a correction thrown away because the redraw failed. `pdf` in the
result says which of the three happened, and always has the same shape.

---

# v0.68.0 — Container-Agnostic Fill Pipeline

## What this connects

`sync_bucket_entries` has taken a segmentation model's `coverage_percent` since
v0.44.0, and as of this release its raw `mask_area_px` / `container_area_px`
too — the pixel counts the model actually measured. This release is where that
number meets a threshold a foreman controls, for a container type that can be
anything a device and a foreman have agreed to call one: `cherry_bucket` and
`pear_bin` are examples, never a hardcoded vocabulary.

**Not pay.** The binary gate — Accepted is one bucket, Rejected is none,
`coverage_percent` never scales it — is unchanged. A fill determination is
quality-control information a foreman or checker reads; nothing here writes to
`verdict` or to anything payroll reads.

## `get_fill_determination`

**READ (default ON).** Pass `entry` (docname or `entry_uuid`) for one capture,
or `session` (docname or `session_uuid`) for every capture in it.

```json
{"entry": "OML-BLE-2026-0042"}
```

```json
{"entry": "OML-BLE-2026-0042", "entry_uuid": "…", "container_type": "cherry_bucket",
 "mask_area_px": 41200.0, "container_area_px": 48000.0,
 "computed_fill_percentage": 85.83, "stored_coverage_percent": 86.0,
 "fill_percentage": 85.83,
 "math_explanation": "41200 mask px ÷ 48000 container px × 100 = 85.83%",
 "threshold_applied": {"container_type": "cherry_bucket", "lower_bound_pct": 85.0,
                       "upper_bound_pct": null, "version": 2},
 "result": "Pass",
 "explanation": "85.83% is at or above the 85% lower bound (this container type has no upper bound)"}
```

**Computed from pixel areas when both are present, falling back to the stored
`coverage_percent` otherwise.** An entry synced before this release, or from a
device that only ever sent `coverage_percent`, still gets an answer — just
without the pixel-area explanation. `result` is `Pass`, `Underfill`, `Overfill`
or `Unknown` (no fill percentage available, or no threshold set yet for that
container type).

## `get_fill_thresholds`

**READ (default ON).** Required: `container_type`. `company` is required on a
multi-company site.

```json
{"configured": true, "company": "Orchard Meadow, LLC", "container_type": "pear_bin",
 "lower_bound_pct": 80.0, "upper_bound_pct": 115.0, "version": 1,
 "last_updated_by": "ana@example.test", "last_updated_at": "2026-08-12 09:00:00"}
```

A container type nobody has set a threshold for yet answers `configured: false`
rather than a refusal — a fresh install has none, and that is a fact worth
returning.

## `update_fill_threshold` — MUTATING, default off

Required: `container_type`, `company`, `lower_bound_pct`. Optional:
`upper_bound_pct`, `reason`. **Foreman or above only — never Checker**, because
this app has no Checker role and gating on an ordinary personnel role would let
anybody holding it move the number a checker is asked to trust.

**A full definition, not a patch.** Omitting `upper_bound_pct` clears any
existing one, every call — which is how a container type that cannot overfill
(a cherry bucket) stays that way: nobody ever sends an upper bound for one. A
caller changing only the lower bound on a container type that HAS an upper
bound must resend both.

Bumps `version` and writes a Fill Threshold Change Log row recording
who/when/old→new, which `list_fill_threshold_changes` reads and
`acknowledge_threshold_update` attaches checker sign-off to.

## `list_fill_threshold_changes`

**READ (default ON).** Filters: `container_type`, `company`. Every change, who
made it, old and new bounds, and `acknowledged_count` — how many checkers have
signed off on that specific version so far.

## `acknowledge_threshold_update` — MUTATING, default off

Required: `employee`, `container_type`, `company`. A checker acknowledging the
CURRENT threshold — records their Employee, a timestamp and the version
acknowledged. **Idempotent**: acknowledging a version already acknowledged by
the same employee changes nothing (`already_acknowledged: true`, no new row).

## `list_pending_threshold_acknowledgments`

**READ (default ON).** Required: `container_type`, `company`. The population is
every **Active** Employee whose `designation` is `Checker` — the same
Link-to-Designation field `Position Wage Default` already reads. Answers with
`checkers_total`, `acknowledged_count` and `pending` (the ones still owed).

## `get_compliance_alert`

**READ (default ON).** Required: `alert`. One Compliance Alert, described
exactly as `get_compliance_calendar` describes it — the single-row twin, for a
caller that already has a docname rather than a filter. Sprint 3 (v0.68.0)
added it for `api/rectify.py` to re-read one alert's state after a
rectification action; nothing about `get_compliance_calendar` changed.

## `materialize_task_for_alert`

**MUTATING, default off.** Required: `alert`. The single-alert twin of
`generate_tasks_from_compliance_alerts` — turns exactly the one named alert
into its dispatchable Farm Task, using the identical recipe lookup and
task-shaping code, rather than sweeping every open alert of that type.
**Idempotent**: an alert that already has a task returns it
(`already_answered: true`) and writes nothing. Refused with the same "no
recipe" explanation `generate_tasks_from_compliance_alerts` reports in
`skipped_unmapped` for an alert type this app cannot turn into work. This is
what the mobile sidecar's `rectify_alert` calls for every alert whose
rectification names a task-shaped fix — see `api/rectify.py`.

## Compliance alert rectification — the `rectification` field and its routes

Every Compliance Alert the mobile sidecar shapes now carries a **`rectification`**
object saying what fixes it and where to start:

| key | meaning |
| --- | --- |
| `action_type` | the verb, e.g. `submit_w4`, `reverify_i9`, `claim_task`, `create_task` |
| `action_label` | the words a worker reads off the button |
| `action_endpoint` | a real sidecar route, absolute from `/farmops/api/` |
| `action_params` | what to prefill, omitting anything that could not be resolved |
| `can_rectify_mobile` | whether there is a fix this app can start from a phone |
| `explanation` | present on a refusal, saying **why** there is none |

It is **always an object, never a missing key** — a phone has to be able to tell
"this app has no fix for that" from "this app did not decode the row", and only
one of those is worth a support call.

**All 27 seeded rule types are covered**, and `tests_standalone/test_rectify.py`
keeps the map closed in both directions: every rule the app seeds has an entry,
and every entry names a rule it seeds. It also joins every endpoint string back
to the mounted route table, because the paths are written as constants on
purpose and nothing else would catch a typo that 404s on the one tap that
mattered.

### The six new mobile routes

`POST /farmops/api/mobile/` — `renew_certification`, `record_training`,
`sign_training_supervisor_review`, `update_regulatory_filing`,
`advance_policy_review`, `rectify_alert`.

The first five are the fixes that are **one small form**; each is a narrow door
onto a shipped tool rather than a second implementation of it —
`advance_policy_review` takes the two fields its alert is about and not the
version chain `update_compliance_policy` also accepts.

**`rectify_alert` is the one route every task-shaped fix shares** — walk the
cabin, sample the water, test the detectors, document the heat break. **It does
not take an action name.** The mapping from alert to mechanism is decided
server-side from the alert's own `alert_type`, never from an argument the caller
sends, for the same reason no wrapper in `api/mobile.py` takes a doctype and a
docname and calls whatever tool a body names. `confirm` is required and changes
nothing by itself, so a mis-tap on the calendar cannot raise work. It returns
the **task**, not the compliance record; completing it is
`complete_task_via_mobile`, unchanged.

### Where an alert routes at a shipped endpoint instead

`submit_w4` (both W-4 alerts), `collect_signature` (the four signature boxes plus
I-9 Supplement B), `submit_i9_section_2` (verification overdue), `reverify_i9`
(an expired I-9, and one expiring), and `claim_task` for a field report nobody
picked up — that last because **the task already exists**, which is what the
alert is complaining about, and raising a second would answer an unclaimed task
with an unclaimed task.

Seven of these alert types also sit in `ALERT_TASK_MAP`, so the nightly sweep
still raises its task for them. A tap takes the direct route; the sweep is
unchanged. A list somebody works through and a button somebody presses are
answering different questions.

### The three deliberate refusals

`i9_retention_destruction_eligible` reports that an I-9 may now be destroyed —
irreversible, role-gated, and reviewed before it is taken rather than tapped
through on a handset. `financial_kpi_threshold_breach` and
`budget_variance_breach` report a computed figure crossing a line an operator
set, which no single act moves back. Each says so in its own words instead of
falling through to the generic sentence: "a lawyer signs off on this" and
"nobody has written this yet" are different facts, and only the second invites
somebody to go looking for a button that does not exist.

**The handset side is separate, tracked work.** The server names the fix and
mounts the route for every alert; `ComplianceAlertDetailView.swift` does not
draw the button yet.

---

## v0.69.0 — Document Intelligence

Five tools, two of which write. **A phone reads a piece of paper; these decide
whether to believe it.**

**The pipeline is three stages and they are three stages on purpose.** Vision
on the device reads a pesticide label at a chemical shed; on-device extraction
pulls `rei_hours`, `phi_days` and an EPA registration number out of what it
read. Both are fast, both work offline, and neither can tell whether what it
read is *true* — `0` and `O` are the same shape at 200 dpi in a dusty shed.
Stage two is the deterministic rules in `document_intel.py`; stage three is
judgement, and it comes from the caller.

**There is still no model call anywhere in this app.** `proposals.py` argues the
architecture at length and it holds here: the AI is the MCP client. A model
reading a scanned label calls `validate_document_extraction` and may hand its
own assessment along in the same call, shaped `{status, issues, confidence,
reasoning}`. What the tool does is what a tool can do and a model cannot do for
itself — run the checks a model is bad at, refuse an assessment that is the
wrong shape, and record which model said it.

**Which is why a regex beats a model here.** A model asked whether `524-537` is
a well-formed EPA registration number will usually say yes, and occasionally say
yes about `S24-S37`. `EPA_REG_PATTERN` never does. The deterministic stage is
not a cheap approximation of the judgement stage; it is better than it at the
work it covers.

### The rules, by document type

| Type | What is checked |
| --- | --- |
| `Pesticide Label` | EPA registration number against 40 CFR 152.132's shape, with an OCR-look-alike repair *proposed* where one produces a well-formed number; signal word against the four a US label may carry; REI within plausible bounds **and against the active ingredient it names**; PHI within bounds and naming a crop; **PHI against REI** — you cannot harvest a block you may not walk into; ingredient concentrations summing past 100%; a rate that parses as amount-per-area; PPE, required outright on a `Danger` label |
| `Applicator License` | Expiry present, readable, not passed, not before issue, not ten years out; licence number; issuing state; categories; **holder's name against the record it is filed against** |
| `WPS Certificate` / `Training Certificate` | Completion date present, readable, not in the future, not more than a year old; trainer; course; name against the record |
| `Insurance Certificate` | Policy expiry and number, carrier, a coverage limit that is a positive number |
| `I-9 Document` | Document title and issuing authority; document number; expiry — and an expired one says *this person cannot lawfully be put on a crew tomorrow*, because that is what it means |
| `Receipt` | A positive total, a merchant, a date that is not in the future, and lines that sum to the total once extracted tax and tip are counted |
| `Inspection Evidence` / `Task Evidence` | Capture time present and not in the future — deliberately thin, because what makes a photograph evidence is the task it hangs off |
| `Signature` | A signer, a signing time, neither absent nor in the future |

Every check that compares a field to what was actually *printed* needs
`ocr_text`. Without it those checks are skipped and an `info` issue says so:
the values were checked against each other and against the rules, and against
nothing on the page.

### `validate_document_extraction`

```json
{"document_type": "Pesticide Label",
 "ocr_text": "IMIDAN 70-W … EPA Reg. No. 10163-169 … RESTRICTED ENTRY INTERVAL 5 days …",
 "extracted_fields": {"epa_registration_number": "1O163-169", "signal_word": "Warning",
                      "rei_hours": 120, "phi_days": 14, "phi_crop": "Cherries",
                      "active_ingredients": [{"name": "phosmet", "concentration": 70, "unit": "%"}],
                      "application_rate": "2.125 lb/acre", "ppe_requirements": "coveralls, gloves"},
 "source_doctype": "Item", "source_name": "CHEM-IMIDAN-70W"}
```

**MUTATING (default OFF).** Returns `validation_id`, `status`, `confidence`
(0–1), `issues[]`, `corrected_fields` and `reasoning`. On the example above it
comes back `Pending` — nothing judged it — with one warning:
`epa_registration_number_repairable`, and `corrected_fields` proposing
`10163-169`, naming the rule that proposed it and carrying the reading it would
replace.

**`corrected_fields` are proposals and nothing applies them.** They come back
*beside* the extraction, never in place of it, so a screen can show "the phone
read 1O163-169, the shape of an EPA number says 10163-169" and let a person
decide. An OCR correction applied silently is an OCR error nobody can find
afterwards, which is the whole reason `ocr_text` sits next to `extraction_json`
on the record.

**`auto_store` defaults to true, and storing is the point.** A validation that
is computed, returned and forgotten cannot be revalidated when a label is
revised, cannot be counted when somebody asks how many labels on this site have
never been read by a person, and cannot be found again when a residue detection
sends somebody looking. Pass `false` for the one honest case — a client checking
an extraction mid-capture, before the worker has decided to keep the photograph.

**The status rules, in the order they bind.** A deterministic **error** outranks
everything: an expired licence is expired whatever a model thinks of the
photograph, so the merged status stays `Flagged` (or `Rejected` if the model
went further). Otherwise the worst reading wins —
`Rejected` > `Flagged` > `Pending` > `Validated` — because the cost of looking
at a document that turns out to be fine is a minute. With no assessment at all
the status is `Pending` and an `llm_validation_unavailable` issue says so by
name, so a Pending record can always be traced to either *nothing judged it* or
*something judged it and was unsure*.

**The confidence is the lower of the two readings, not their mean.** They are
two independent looks at one document, and if either is unconvinced the document
is not convincing. The deterministic half is itself two penalties *multiplied* —
how much of what was read is wrong, times how much of the document was read at
all — so a clean extraction that captured three fields out of eight does not
score as though it had been checked.

### `get_document_validation` / `list_document_validations`

Read-only. `get_document_validation` returns one record in full, including the
stored OCR text and extraction; `list_document_validations` is the register,
filterable by `document_type`, `source_doctype`, `source_name`, `status` and
`human_confirmed`, and it carries **neither** the OCR text nor the extraction —
a list of forty validations carrying forty pages of OCR text is a payload a
phone on a field connection cannot use.

### `revalidate_document`

**MUTATING (default OFF).** Re-runs the checks against what the record already
stores. **Nothing is re-photographed and nothing is re-extracted** — which is
the entire reason `ocr_text` and `extraction_json` are kept. A label whose
registered intervals were revised, or a licence that has since expired, gets a
fresh answer without anybody walking back to the chemical shed.

`revalidation_count` is incremented and `last_revalidated` stamped every run, so
a document revalidated four times and still `Flagged` is distinguishable from
one flagged this morning. The stored LLM assessment is **reused** unless a new
one is passed: a re-run that silently dropped it would move a `Validated` record
to `Pending` and look like the document had gone stale, when what changed was
only that nobody re-sent the assessment. Passing `extracted_fields` replaces the
stored extraction, for the case that is not a re-run — somebody corrected a
misread field and wants the checks made against the corrected reading.

### `list_revalidation_due`

Read-only. Which stored validations are due to be re-checked, soonest first,
with `days_overdue` on each and a count of how many have never been confirmed by
a person.

**The document's own expiry wins over the cadence.** A licence is not due for
revalidation on the anniversary of somebody scanning it; it is due when it
expires. And a document type that never goes stale — a receipt, task evidence, a
signature — carries no due date and is **never** in this list, which is what
stops a site with ten thousand receipts and forty licences returning ten
thousand rows.

### The Item columns this release adds

Nine Custom Fields on ERPNext's `Item`, so the label's own numbers live on the
product rather than only on each application: `epa_registration_number`,
`signal_word`, `rei_hours`, `phi_days`, `phi_crop`, `active_ingredients`,
`application_rate`, `ppe_requirements`, and `label_scan_validation` linking back
to the `Document Validation` they were read off. None is `reqd` — an Item is a
picking bag and a length of irrigation pipe as well as a jug of captan — and a
`depends_on` hides them on anything that is not a chemical. See
`docs/compliance_fields.md` for the full argument, including why that
`depends_on` also fires on any Item already carrying an EPA number.

### The two mobile routes, and why they are spelled differently

`POST /farmops/api/mobile/validate_document` and
`POST /farmops/api/mobile/get_document_validation`. The Sprint 4 contract named
`POST /farmops/api/validate-document` and
`GET /farmops/api/document-validation/<name>`; this transport builds every path
from the method's own name under `/mobile`, takes POST only, and matches whole
paths rather than patterns. A hyphen is not a Python identifier and a path
parameter has nowhere to land, so honouring the spelling would mean forking a
router whose closed, readable-in-one-screen table is its entire design. **The
bodies and the answers are the contract's, unchanged.** `image_data` is accepted
and deliberately not stored — stage the image and pass `scan_file_url`.

`list_document_validations`, `list_revalidation_due` and `revalidate_document`
have no route: two are an office's registers rather than anything a phone at a
shed reads, and the third re-decides a stored status, which is a supervisor's
call at a desk.

---

## CFL Banking (v0.71.0)

Sprint 6 of the Gap Closure Plan, and its capstone. Sprints 2 to 5 built the
paper: a photographed receipt, a bill from a supplier, a settlement from the
packer, a cheque received, a payroll run. Every one of those is a *claim* that
money moved. These ten are about the **other** record of the same movement — the
bank's — and about the gap between the two, which is the only place a farm finds
out that a claim was wrong.

**Three questions, deliberately not one feature.** They get confused constantly
and they have different answers:

| | Question | Answered by | Fixed by |
|---|---|---|---|
| **Allocation** | Is this transaction settled against a Payment Entry or a Journal Entry, so the ledger balance is the statement balance? | ERPNext itself | `reconcile_bank_transaction` (v0.1.0) |
| **Evidence** | Is there a *receipt* behind this line — the slip from the pump, the invoice from the parts counter? | Sprint 6 | `match_receipt_to_bank_transaction` |
| **Categorisation** | What *kind* of expense was it? `CHEVRON 0093746 PASCO WA` is not a category. | Sprint 6 | `apply_categorization_rules` |

`get_bank_reconciliation_status` answers all three side by side and **never adds
them together**. A transaction can be perfectly allocated and have no paper
behind it — which is exactly what an audit asks about — and one with a receipt
can be uncategorised and still tie out.

**Matching is proposed, never committed in bulk.** `auto_match_receipts` is a
**read** tool. It scores every unmatched receipt against every unmatched
withdrawal, hands back a ranked list with the exact call that would commit each
one, and writes nothing. That is not timidity about a hard problem: a wrong
receipt-to-bank link is *invisible* afterwards — both documents exist, both
amounts are right, and the only thing wrong is which slip is filed against which
withdrawal. So a person accepts each one, and `bank_match_method` records that a
machine proposed it.

**Categorisation is different and is allowed to write.** A rule is deterministic
and inspectable, and its output names the rule that produced it, so an operator
who disagrees reads the rule, fixes it and runs again. A fuzzy amount-and-date
match has none of those properties.

**Nothing in this sprint posts to the ledger.** Not one of the ten writes a GL
Entry, a Journal Entry, a Payment Entry or an allocation. Categorising a
transaction says what it *was*; turning that into a posting is
`create_journal_entry`, which has its own switch and its own review.

**The columns this release adds.** Expense Receipt gains four in this app's own
DocType JSON (`bench migrate`): `bank_transaction`, `bank_match_method`,
`bank_match_confidence`, `bank_matched_on`. Bank Transaction gains three Custom
Fields — `farm_category`, `farm_expense_account`, `categorization_rule` — created
at install and again lazily on first use, all three `allow_on_submit` because a
bank feed's transactions are submitted and a bookkeeper correcting a category
must not have to cancel one.

### `match_receipt_to_bank_transaction` — MUTATING, default off

**Arguments:** `expense_receipt` (required), `bank_transaction`, `bank_account`,
`match_method` (`Manual` | `Proposed`), `replace`, `amount_tolerance`,
`date_window_days`, `limit`.

Two modes in one tool. **With** a `bank_transaction` it writes the link; **without**
one it scores every eligible transaction and returns ranked candidates, writing
nothing. It is one tool rather than two because the second call is the first call
with one more argument.

Refused by name, all of it before anything is written: a **deposit** (an expense
receipt is money out, and that one cannot be overruled), a receipt and a
transaction in **different companies**, a **rejected** receipt, a transaction
that already has a **different receipt**, and a receipt already matched
elsewhere unless `replace=true` — in which case the result names what was
unlinked.

The **score** is not in that list. An amount two cents out or a date eight days
late is a judgement, and a person naming both documents outranks an algorithm:
the link is made, the objections come back in `blockers`, and the stored
confidence is `0.0` so the pair surfaces in any later review.

### `auto_match_receipts`

Read-only. **Arguments:** `company`, `bank_account`, `from_date`, `to_date`,
`category`, `status`, `min_amount`, `max_amount`, `min_confidence` (default
`0.70`), `amount_tolerance` (default `0.02`), `date_window_days` (default `7`,
maximum `60`), `limit`.

Each proposal carries `confidence`, the three `signals` behind it (amount gap,
days between, merchant against the bank's memo line) and a `commit_with` block —
the tool name and arguments that would commit it.

**Confidence is amount 0.5, date 0.3, merchant 0.2, capped at 0.95.** The amount
is half of it because it is the only signal that is nearly impossible to coincide
by accident at farm transaction volumes; the merchant is worth least because a
bank memo line is a mangled version of a name at best and a terminal ID at worst.
Tuned so an exact amount within a day clears the threshold even when the memo is
unreadable, and an exact amount a week later with no name agreement does not.

**Contested proposals are reported, not resolved.** Two slips for the same amount
on the same day at the same vendor are a real thing on a farm with two trucks.
The higher scorer is proposed; the other comes back under `contested` with the
transaction named, rather than dropped.

**v0.75.0: the card fingerprint settles the case that used to be contested.**
Two trucks carry two cards, and the bank's memo line prints the last four of
the one that was swiped. Where a receipt's `card_last_four` matches those digits
**within a day**, the proposal comes back with `card_fingerprint: true`, gains
`+0.15` toward the 0.95 ceiling, and **outranks a higher-scoring receipt with
no fingerprint** — the bank naming the physical card is better evidence about
*which slip this is* than any margin in a similarity number.

Three conditions, all required, and each is a way the check would otherwise be
wrong: the receipt has to carry a card last four (an absent one is silence, and
never lowers a score); the memo line has to name those digits **as a card** —
masked (`XXXX4417`) or introduced by a word that means card, because a bare
four-digit run in a memo is as likely a terminal id or an authorisation code;
and the dates have to be within a day, because the seven-day window is for
*finding* a match while this is for *confirming* one, and a same-card
same-amount charge a week later is next week's fill-up.

Receipts with no card score **exactly** what they scored before this release —
the fingerprint is a bonus, not a fourth weight, so no evidence was taken away
from the amount to pay for it. `card_fingerprint_count` totals the confirmed
ones, and `settings.card_last_four_available` says whether this site has the
column at all.

### `get_bank_reconciliation_status`

Read-only. **Arguments:** `bank_account`, `company`, `from_date`, `to_date`.

Returns `transactions` (count, deposits, withdrawals, net) plus
`ledger_allocation`, `receipt_evidence` and `categorization` — each with
`matched`, `unmatched`, the two gross amounts, `total` and `matched_pct`, and
each carrying the question it answers and the tool that closes its gap.
`matched_pct` is `null` rather than `0` when there is nothing in the period.

### `list_unmatched_receipts`

Read-only. **Arguments:** `company`, `from_date`, `to_date`, `category`,
`status`, `min_amount`, `max_amount`, `limit`.

Oldest first, because the receipt that has been waiting longest is the one whose
charge is most likely never to have landed at all. Rejected receipts are excluded
unless a status is named.

### `list_unmatched_bank_transactions`

Read-only. **Arguments:** `bank_account`, `company`, `from_date`, `to_date`,
`direction`, `min_amount`, `max_amount`, `require` (`any` | `receipt` |
`allocation` | `both`), `limit`.

Every row carries `unmatched_reasons`: `no allocation in the ledger` is a
bookkeeping gap, `no receipt on file` is an evidence gap, and a transaction can
have either, both or neither.

### `create_bank_categorization_rule` — MUTATING, default off

**Arguments:** `rule_name`, `company`, `category`, `pattern` (all required),
`match_field` (`description` | `reference_number` | `bank_party_name`),
`match_type` (`contains` | `starts_with` | `equals` | `regex`), `direction`,
`priority`, `account`, `cost_center`, `party_type`, `party`, `amount_min`,
`amount_max`, `enabled`, `notes`.

**The account is optional and never guessed.** A rule with a category and no
account still sorts a statement, which is most of the value; picking a leaf
expense account by name on the operator's behalf would put a season of spraying
somewhere nobody chose. When one is given it is checked against the company,
against being a group and against being disabled — all three before anything is
written. A `regex` that will not compile is refused on save, so a bad rule fails
here rather than in the middle of a categorisation run.

**Overlap is reported, not refused.** Rules are *meant* to overlap: `CHEVRON` at
priority 10 and `FUEL` at 100, first match wins. The result names which existing
rules also match and which of them would win.

### `list_bank_categorization_rules`

Read-only. **Arguments:** `company`, `enabled`, `category`, `limit`.

In **evaluation order** — priority ascending — because reading the list top to
bottom is how somebody works out why a transaction got the category it did.
`never_fired` names the rules that have matched nothing, which usually means an
earlier rule is swallowing their transactions.

### `apply_categorization_rules` — MUTATING, default off

**Arguments:** `company` (required), `bank_account`, `from_date`, `to_date`,
`rule`, `dry_run`, `overwrite`, `limit`.

First match by priority wins. Writes `farm_category`, `farm_expense_account` and
`categorization_rule` with `db.set_value` — a bank feed's transactions are
submitted, and `save()` on one either refuses or drags the whole document through
validation it does not need. A rule's `party` is set only on a transaction that
names nobody.

**A category somebody typed by hand is never overwritten** unless
`overwrite=true`, in which case the result reports what was there before.
`dry_run=true` does the whole run and writes nothing — the sensible first call
after seeding, because what it does *not* categorise is the list of rules the
farm still needs.

### `get_cash_flow_summary`

Read-only. **Arguments:** `company` (required), `bank_account`, `from_date`,
`to_date`.

**The one thing it refuses to do is add them up.** A settlement, a sales invoice
and a bank deposit can all be the same money arriving three times in three
doctypes at three different moments; a single total would triple a season's
revenue and look entirely reasonable. So:

- `cash` — the bank statement. Deposits, withdrawals, net. The only section where
  the money actually moved, and the only total in the response.
- `inflows` / `outflows` — the **documents**: payments received and made, sales
  and purchase invoices, settlements, payroll, expense receipts. Each says its
  doctype, count, amount and **basis**, or why it is absent on this site.
- `by_category` — outflow by category, **deduplicated**: a receipt matched to a
  withdrawal is one purchase, so the withdrawal is dropped and the receipt kept
  (the receipt carries the category somebody chose from the paper; the bank line
  carries one a pattern guessed from a memo field). `deduplicated_transactions`
  says how many were dropped.

### `seed_farm_categorization_rules` — MUTATING, default off

**Arguments:** `company` (required), `account_map`, `dry_run`.

Seeds a starting book across Fuel, Chemicals/Spray, Equipment Parts, Labor
Services, Irrigation, Insurance, Utilities, Feed, Supplies, Professional Services
and Owner Draw. Specific merchant patterns sit at priority 10–40 and generic
words at 100+, because the first match wins. **Every seeded rule is
`Withdrawal`-only** — a refund from Chevron is not a tank of diesel.

Nothing here matches a bare transfer or an ATM withdrawal: `TRANSFER` is an owner
draw on one farm and a sweep between the operating and payroll accounts on the
next, and a seed that guessed would file a year of internal movements as equity
leaving the company.

**Idempotent by (company, rule_name).** A second run creates nothing and leaves
every edit — pattern, priority, account, `enabled` — alone. A **deleted** rule
comes back on the next run: disable one that does not fit rather than deleting
it, which is stated rather than solved because a tombstone register is more
machinery than the problem deserves.

`account_map` is `{category: account}` and every account in it is vetted
**before any rule is created**, so a bad one produces nothing rather than half a
book. Categories left out of it are named in `categories_without_account`.

---

## The foreman's crew-task dashboard (v0.72.0)

Sprint 7. Five tools built in Sprint 8 and never reachable from a handset get a
route: `list_dispatched_tasks`, `assign_farm_task`, `create_farm_task`,
`list_farm_task_templates` and `create_task_from_template`, all at
`POST /farmops/api/mobile/…`. No tool changed; the rules stay where they are.

### These are the first mobile routes a Field Worker cannot call

Every path published before them is a picker acting on their own work and is
gated on `guard.FARM_OPS_ROLES`, which admits a Field Worker. Each of these five
calls `guard.require_dispatch_role` in its own body — **Foreman or Farm Manager**,
the same two names `dispatch.py` already draws the line between for Critical
urgency on a field report, and for the same reason.

The gate is in the wrapper rather than delegated to the tool because **these five
tools have no role check at all**. What stands in front of them on the MCP
transport is the operator's own `allow_…` switch, and a phone does not go through
that switch.

### `list_dispatched_tasks` is scoped to the crew, and does not take `worker_id`

The tool reads one named worker's assignments and will read anybody's. On a
handset that is not a scope — it is "walk the payroll one docname at a time" — so
the wrapper computes the workers instead: the caller's own OPEN shifts
(`end_datetime` unset, not cancelled), everybody rostered on them, and the caller.
`employee` may narrow that set; a name outside it is **refused by name** rather
than answered with an empty list. `worker_id` is not declared at all, so a body
carrying it has the key dropped and gets the whole crew.

A crew member with a `left_at` is kept and reported, not dropped: whoever clocked
out at noon still holds what they were sent to that morning. A foreman with no
open shift gets their own board and a sentence saying why.

### What the three writes will not accept

`assigned_to_name` (it replaces the register's name on both records),
`creates_record` and `creates_record_data` (`record_data` under another name),
`draft` (invisible work), `source_alert` (`rectify_alert` owns that link, one task
per alert) and `materials_used` (a tank mix decided before anybody drives
anywhere). Work that must produce a compliance record comes off a template, which
is why both template routes are in the same set.

`reassign=true` and `reason` are forwarded rather than restated: the refusal is
`assign_farm_task`'s, it is conditional on somebody actually holding the task, and
a wrapper demanding a reason for dispatching unclaimed work would refuse the
ordinary case to guard the rare one.

### Three template tools with no route

`get_farm_task_template`, `create_farm_task_template` and
`update_farm_task_template`. Reading one template in full is what the list already
carries enough of, and authoring the shape of a recurring job — its evidence
contract, the record it builds, its checklist — is a desk decision with the
regulation open.

---

## The Bank Bridge consolidation (v0.73.0)

Fourteen tools, three doctypes and two whitelisted endpoints that move the
**reconciliation truth** out of a Flask sidecar and into the system that already
holds the transactions, the ledger and the company.

**The question none of the earlier bank tools could answer: is the data
COMPLETE.** `get_bank_reconciliation_status` answers three questions about a
transaction that is *present* — allocated, receipted, categorised. A transaction
the feed never delivered leaves no row to inspect, no gap in a sequence and no
trace of any kind. The only thing that finds it is arithmetic across a whole
period:

```
computed_closing = anchored_opening + transaction_sum
variance         = anchored_closing - computed_closing
```

Where the variance is not zero, the difference **is** the missing movement, to
the cent, before anybody knows what it was. Positive is money **in**, on every
number in this section and on ERPNext's own Bank Transaction, so nothing here
flips a sign.

### What a Statement Anchor is

One row per `(bank_account, statement period)`. Unique on that triple, enforced
in the controller because Frappe has no composite unique index in a DocType JSON
— and it has to be, because two anchors for one October is two answers to
whether October tied out, and the push endpoint's idempotency depends on the
site being unable to hold both.

`computed_closing`, `variance`, `reconciled` and `chain_gap_from_prior` are
**read-only and recomputed on every save**. A payload carrying its own variance
gets it recomputed rather than adopted: a pipe that could assert a variance could
assert a zero, and a zero variance is indistinguishable from a reconciled
account.

`Statement Anchor Line` is an optional child table holding the statement's own
lines. It is **not** a second copy of a Bank Transaction — it is the other
party's record of the same week, and the difference between the two is the only
thing that can name a movement the feed dropped.

### `get_statement_anchor_chain`

**Arguments:** `bank_account` (docname or four-digit mask), `plaid_account_mask`,
`company`, `from_date`, `to_date`, `limit`.

In **period** order, because the list is the chain and reading it top to bottom
is how somebody finds the month it broke. `cumulative_variance` is the number to
read first: periods that vary a few hundred either way are timing differences, a
cumulative variance that grows every month is a recurring charge nobody has
booked, and the two look identical period by period.

A **chain gap** — one period's opening balance is not the prior period's closing
balance — is a *missing statement*, which no amount of per-transaction checking
will find. Gaps are warned about, with the sentence saying that a chain broken at
March says nothing reliable about April.

A mask that matches more than one account is **refused by name**. A
reconciliation answer for the wrong account looks exactly like a right one.

### `list_unreconciled_anchors`

**Arguments:** `company`, `bank_account`, `tolerance`, `include_explained`,
`from_date`, `to_date`, `limit`.

The same data worst-first by **absolute** variance, which is the worklist
ordering — the sort happens in Python because a database ordering on the signed
variance would put the largest overstatement and the largest understatement at
opposite ends of the list.

A period carrying a `variance_reason` is **still listed**. It is a recorded fact
about an account — a quarterly advisory fee deducted outside the feed — and
hiding it would make somebody rediscover it every quarter. `include_explained:
false` drops them when that is genuinely what you want.

`tolerance` re-judges every period rather than reading the stored `reconciled`
flag, because "what is off by more than a hundred dollars" is a different
question from the one each record was saved with.

### `get_anchor_variance_breakdown`

**Arguments:** `anchor`, or `bank_account` + `period_start` + `period_end`;
`day_window`, `tolerance`, `limit`.

Three sums that are routinely confused, reported separately and **never added**:

| Field | What it is | What a mismatch means |
|---|---|---|
| `anchored_transaction_sum` | what the *statement* said moved | — |
| `ledger_transaction_sum` | what the Bank Transactions on file add up to | the two records of the period disagree before any variance is computed — a FEED problem |
| `variance` | the statement disagreeing with its own opening and closing | a movement outside the transaction list, e.g. a fee deducted at source |

`diagnosis` is prose rather than a code, because it is a hypothesis: every number
it is drawn from is in the payload beside it, so a reader who disagrees has
everything needed to say so.

### `list_unmatched_statement_lines`

**Arguments:** `bank_account`, `company`, `from_date`, `to_date`, `day_window`
(default 3), `tolerance`, `limit`.

The one list that **names** a missing movement rather than its size — the date
the bank printed, the memo, the amount.

Matching is nearest-in-time among exact-enough amounts, and **each transaction is
consumed once**. Two identical $184.62 fuel purchases in one week is the ordinary
case on a farm, and a matcher that let one transaction satisfy both lines would
report the account complete while a movement was genuinely missing.

**An empty list is not automatically a clean result.** Where no anchor in scope
carries statement lines the tool says so, loudly, because "nothing is missing"
and "we have nothing to check against" are opposite answers. The reverse list —
transactions with no statement line — is reported separately: one is a gap in the
feed, the other is a transaction that should not be on the account at all.

It writes nothing, including the matches it works out.

### `get_statement_recon_report`

**Arguments:** `bank_account`, `company`, `from_date`, `to_date`, `limit`.

The **statement**, the **feed** and the **ledger** for the same periods, side by
side and never summed. Feed against ledger disagreeing is the ordinary backlog —
transactions have arrived and nobody has posted them. Statement against feed
disagreeing is the serious one, because two independent records of one period do
not match and every categorisation built on the feed inherits the difference.

`ledger_movement` is debit minus credit on the GL account the Bank Account names,
and is **null** where it names none — reporting zero would claim the ledger
disagrees with the feed by the whole month.

### `get_account_pairing` and `pair_bank_accounts` — the second MUTATING, default off

**Arguments (pair):** `bank_account` (required), `paired_bank_account`
(required), `pairing_type`, `paired_pairing_type`, `replace`.

A pairing is a **property of an account**, not an entity: `paired_bank_account`
and six Plaid metadata columns are Custom Fields on ERPNext's Bank Account,
created at install and lazily on first use. A brokerage and the cash-services
account its trades settle through are one relationship seen from two sides, and
an anchor chain reconciles cash and sweep only because the securities leg lives
on the companion.

`pair_bank_accounts` **writes both sides**, which is why it is a tool rather than
a note telling somebody to set a field: a pairing stored on one account reads as
working from that end and as absent from the other, and the missing half is the
one a reconciliation run happens to start from. Naming one role names the other,
since the pair has exactly two. Refused: pairing across companies, an account
with itself, and silently breaking an existing pairing — `replace=true` repoints
it and names the account left with no companion.

### `set_anchor_variance_reason` — MUTATING, default off

**Arguments:** `anchor` or `bank_account` + period; `variance_reason`, `clear`.

Writes **one** prose field, and it does **not** mark the period reconciled.
`reconciled` is arithmetic; this is a human judgement beside it; both are
reported and neither overwrites the other.

Why a sentence beats a wider tolerance: a managed account's quarterly advisory
fee is $3,774.81 this quarter and a different number next quarter, so a tolerance
wide enough to swallow this one is wide enough to swallow a genuinely missing
deposit — while the sentence stays true and hides nothing.

### `rebuild_anchor_chain` — MUTATING, default off

**Arguments:** `bank_account` (required), `from_date`, `to_date`,
`recompute_transaction_sum`, `day_window`, `tolerance`, `dry_run`.

Recomputes what is **derived** — closing, variance, reconciled, chain gaps, and
the Bank Transaction each statement line matches. A chain built one anchor at a
time gets the gap flags wrong whenever a statement arrives out of order, which is
what this exists to fix.

**It leaves `anchored_opening`, `anchored_closing` and `transaction_sum` alone.**
Those came off a bank statement this app did not read, and rewriting them from
the transaction feed would replace the independent record with a restatement of
the thing it exists to check — after which every period ties out perfectly and
the chain proves nothing. `recompute_transaction_sum=true` overrides it for
accounts whose anchors came from the feed to begin with, reports the before and
after per period, and warns in those words.

### `create_advisory_agreement`, `update_advisory_agreement` — MUTATING, default off; `get_advisory_agreement_summary`, `list_advisory_agreements` — read

An advisory fee is the one recurring cost on a farm's books that arrives
**already deducted**. Nobody approves it, no invoice precedes it, and on a
statement it looks like every other line. The only thing that says whether it is
the right number is the agreement it was charged under.

Every creation refusal is a number that would otherwise **compute wrong rather
than fail**: a `Percent of AUM` agreement with no percentage computes zero, which
looks exactly like an account managed for free; a `Hybrid` missing its flat half
computes the percentage alone and looks reasonable. A second **Active** agreement
on one account is refused, because two live sets of terms is two answers to what
the fee is.

`get_advisory_agreement_summary` computes what the terms say a period costs and
**always names its basis** in `aum_source`. Where no portfolio value can be
established the fee is **null, not zero** — a fee of zero and a fee nobody can
compute are opposite findings. A bank balance is deliberately never used as a
substitute: it is a fraction of the portfolio, so a fee computed against it would
be wrong and plausible.

`update_advisory_agreement` **amends by versioning**: a new record, `amended_from`
pointing back, the old one Superseded. Last quarter's fee was charged under last
quarter's terms, and an in-place edit would leave the site holding a charge it
cannot justify. `in_place=true` corrects a description — a name, a client's
spelling, a document link — and **refuses** to touch a fee, a date or a status.
`terminate=true` with a `termination_date` ends an agreement without creating a
version, because terms that stopped did not change. The date is required rather
than defaulted to today: an agreement that ended in March and is being recorded
in August would otherwise claim five months of coverage it did not have.

### `create_bank_categorization_rules` — MUTATING, default off

**Arguments:** `company` (required), `rules` (required array), `dry_run`.

A whole book in one call. The point is not saving round trips — a single-rule
call can only see the rules that already exist, so thirty of them get their
priorities wrong relative to each other. **Vetted in full before anything is
written**: every account resolved and checked, every name checked against the
site *and* against the rest of the batch. A batch produces the book you described
or produces nothing, because half a book categorises a month of statement on its
own and looks like it worked.

### What v0.73.0 changed about categorization rules

Six new match types beside v0.71.0's four operators: `merchant_exact`,
`merchant_contains`, `description_regex`, `plaid_category_matches`,
`amount_range`, `combined`. **Nothing was migrated** — `contains CHEVRON on
description` and `merchant_contains CHEVRON` are the same rule, and rewriting
sixty of them would change what a site's audit trail says its rules were.

- **Direction and the amount bounds AND onto every match type**, not just onto
  `amount_range` and `combined`. That is what makes `amount_range` a match type
  rather than a modifier, and why a regex-plus-ceiling rule needs no special
  support.
- **The merchant match falls back to the description** where the feed left
  `bank_party_name` empty — disproportionately the small local suppliers a farm
  actually buys from, so a rule reading only that column would match the national
  chains and miss the co-op.
- **`plaid_category_matches` is a PREFIX match**: `TRANSPORTATION` catches
  `TRANSPORTATION_GAS` today and `TRANSPORTATION_TOLLS` the day the aggregator
  adds it.
- **`combined` requires at least two criteria.** One criterion is not a
  combination, it is a simpler match type written the long way round — and it
  would behave subtly differently, because `combined` compares text as a
  substring whatever `match_field` says.
- New fields: `bank_cost_center`, `party_name`, `plaid_category`. The two cost
  centers are **reported** by `apply_categorization_rules` and never written onto
  a Bank Transaction: a cost center is a property of a posting, and nothing here
  posts.
- `seed_farm_categorization_rules` takes `categories`, so an orchard with no
  livestock need not seed a Feed rule that will never fire and will sit in
  `never_fired` forever looking like a problem.

### The three push endpoints

```
POST /api/method/erpnext_mcp.bank.push_statement_anchor
POST /api/method/erpnext_mcp.bank.push_account_pairing
POST /api/method/erpnext_mcp.bank.push_account_metadata   (v0.74.0)
```

Not MCP tools — whitelisted Frappe methods a bank pipe calls with its own ERPNext
credential.

**Idempotent on `(bank_account, period_start, period_end)`.** A pipe retries, a
parse gets re-run, an operator syncs by hand while the scheduled job is still
going. An endpoint that appended would leave two anchors for October that
disagree.

- **A batch tolerates a bad row; a single push does not.** Per-row tolerance is
  right for thirteen months and wrong for one: a payload that failed and came
  back `200` with `failed_count: 1` is a pipe that believes it succeeded.
- **Statement lines are replaced, not appended.** A re-parse produces the same
  lines again, and doubling them would make every line match a transaction and
  the unmatched count — the entire product — quietly wrong. Omitting the key
  leaves what is on file alone; `[]` clears it.
- **`variance_reason` is the one field a later push will not overwrite.** It
  lands on an anchor that has none — which is what makes a one-time migration of
  a pipe's own variance tags work — and never over one that does, because after
  consolidation the sentence is a person's and a nightly sync would otherwise
  erase it.
- **Neither endpoint creates a Bank Account.** An auto-created account has no GL
  account, no company anybody chose, and no way to be noticed, and the anchors
  hanging off it would look exactly like reconciliation.
- **Both account endpoints write only the keys actually sent**, so a sync that
  knows the mask and not the subtype does not blank a subtype somebody typed.
  `sync_enabled` is the exception: `0` is a value, not an absence.
- **`push_account_metadata` is `push_account_pairing` with the pairing taken
  out** (v0.74.0). A nightly metadata refresh has no business being able to
  repoint which two accounts are companions, so this one cannot: a payload
  carrying `paired_bank_account` or `pairing_type` is **refused by name**. Both
  run one implementation, so the two can never drift on what "only the keys sent"
  means. The pairing keys are *declared in order to be refused* — Frappe drops
  undeclared kwargs silently, and a dropped key that returns `200` is
  indistinguishable from an honoured one.
- **The ERPNext docname is the identifier; the Plaid id is not** (v0.74.0). A
  re-linked bank connection issues fresh account ids, so an endpoint that found
  its target by id would stop finding it exactly when the record most needs
  correcting. `bank_account` takes a docname, or a four-digit mask when that is
  all the pipe has — refused when two accounts share it.
- **A superseded aggregator id is appended to `plaid_account_id_history`, in the
  same write** (v0.74.0). Overwriting the id is right; overwriting *alone* loses
  the only handle tying a year of stored feed rows, an aggregator's support logs
  and this site's records to the same account. The reply names the change under
  `repointed` rather than leaving it to be spotted. Idempotent — a nightly push
  of an unchanged id appends nothing — and an id that becomes current again comes
  back out of the history, because an id that is both current and superseded
  reads as two accounts to anything matching on it.
- **A chain the pipe kept is merged, not swapped in** (v0.74.0). Both endpoints
  take an optional `plaid_account_id_history`. Neither source is complete: the
  pipe's chain holds the re-links that happened before this site knew the
  account, the observed chain is what this site watched and is all there is when
  the pipe sends nothing. A backfill alone is not reported as a `repointing`.
- **The gate** is a named user plus `frappe.has_permission(..., "write")` — the
  `api/gis.py` choice, not `api/guard.py`'s, whose Mobile Access Grant is the
  wrong gate for a server-to-server credential. **Every call writes an MCP Action
  Log row, refusals included**, prefixed `push:`.

### Nothing in this release posts to the ledger

Not one of the fourteen tools writes a GL Entry, a Journal Entry, a Payment Entry
or an allocation — the same promise v0.71.0 made, for the same reason. An anchor
says whether the books tie out. Making them tie out is `create_journal_entry`,
which has its own switch and its own review.

---

## Irrigation runtime and the closing cascade (v0.76.0)

One new read tool, and one change to how `log_asset_state_change` behaves on a
valve. Both rest on a structure that has been in the register since v0.25.0:
`Asset Register.location` is a Link to Asset Register, so a valve sits under a
zone, a zone under a block, a block under a ranch.

### `close_valve` carries downhill

`log_asset_state_change(asset_name, action="close_valve")` on an asset of type
`Irrigation Valve` now closes **every valve below it** in that tree, each with
its own Asset State Log row naming the cause in `cascaded_from`. The reply grows
four keys:

| Field | Meaning |
| --- | --- |
| `cascaded[]` | `{asset_name, asset_type, from_state, to_state, log_name}` per valve closed |
| `cascaded_count` | How many |
| `cascade_skipped[]` | `{asset_name, asset_type, reason}` — retired, already closed, or not a valve |
| `cascade_truncated` | Whether the walk hit its 500-record or 12-level bound |

**Opening does not cascade.** Closing an upstream valve stops the water for
certain; opening it only makes water *available* to whatever is below, each of
which is open or shut on its own account.

**Every descendant is accounted for**, applied or skipped with the reason: the
valve that was already winterized is precisely the one somebody needs to know
the main did not shut.

**The GPS fix and the photograph stay on the record that was scanned.** A
cascaded row carries the time, the transition and its cause; it does not claim
somebody was standing there.

### `get_irrigation_runtime`

How long the water ran, summed from open/close pairs already in the log.
Read-only, and it creates nothing — a run *is* two log rows.

**Arguments:** `asset` (required), `from_date`, `to_date`, `irrigation_zone`,
`flow_rate_gpm`, `company`. The window defaults to the last 30 days and includes
the whole of `to_date`.

**Returns**

| Field | Meaning |
| --- | --- |
| `valve_count`, `valves[]` | Every valve at or below `asset`, each with its own `runtime_minutes`, `run_count`, `still_open` |
| `runtime_minutes`, `runtime_hours` | Finished runs only — does not change between two identical calls |
| `open_run_minutes`, `valves_open_now[]` | Water moving right now, counted to the window's end or now, whichever is earlier |
| `total_minutes_including_open` | The two above, added |
| `runs[]` | Each run: `opened_at`, `closed_at`, `minutes`, `open_at_window_start`, `closed_after_window`, `actual_closed_at`, `still_open`, `closed_by_cascade`, `cascaded_from` |
| `cascaded_closes` | Runs ended by a main valve closing rather than by somebody at this valve |
| `flow_rate_gpm`, `flow_rate_source` | The rate used and where it came from — always stated |
| `gallons`, `acre_inches`, `gallons_per_acre`, `inches_applied` | Present only when a rate is known; the last two only when the zone carries an acreage |

**Runs that cross the window are counted at both ends.** The last event before
the window is read, so water opened on 28 June and closed on 2 July is July's
irrigation and not a close with no open. The first event after it is read too, so
the same run is June's hours clipped at midnight rather than something reported
as "still running" when the report is written in August.

**Gallons are never guessed.** Nothing on the Asset Register carries a flow rate,
so volume comes from `flow_rate_gpm` or from an `irrigation_zone` whose record
has one. With neither, the answer is minutes and `flow_rate_source` says why.

**Example**

```json
{"name": "get_irrigation_runtime",
 "arguments": {"asset": "MC-Zone-3", "from_date": "2026-07-01",
               "to_date": "2026-07-31", "irrigation_zone": "ZONE-3"}}
```

```json
{
  "asset": "MC-Zone-3", "asset_type": "Irrigation Zone",
  "from": "2026-07-01 00:00:00", "to": "2026-07-31 23:59:59",
  "valve_count": 2, "run_count": 2,
  "runtime_minutes": 180.0, "runtime_hours": 3.0,
  "open_run_minutes": 0, "valves_open_now": [],
  "cascaded_closes": 1,
  "flow_rate_gpm": 250.0,
  "flow_rate_source": "Irrigation Zone 'ZONE-3' (flow_rate_gpm)",
  "gallons": 45000.0, "gallons_per_acre": 4500.0, "inches_applied": 0.166
}
```

### `universal_scan` answers which state change

Three keys on every branch, populated on the asset branch:

| Field | Meaning |
| --- | --- |
| `state_asset` | The Asset Register record the actions apply to, or `null` |
| `current_state` | `closed`, `open`, `winterized` … or `null` |
| `state_actions[]` | `{action, from_state, to_state}` — the shape `get_available_actions` returns |

`available_actions` is unchanged: it is the handset's own five-string vocabulary,
and `log_state_change` in it names a *screen*. A worker at a valve is choosing
between open and close, and `state_actions` is that choice — so the scan draws
the card without a second round trip.

A Housing Unit scan carries no actions despite having states: they live on an
Asset Register row and nothing links a unit to one (`Housing Unit.related_asset`
points at ERPNext's own fixed-Asset doctype). Scanning the cabin's own asset tag
is what carries them.

---

## Equipment, insurance and clocks (v0.77.0)

### Timestamps now say which zone

Every timestamp on the endpoints below keeps its existing key, unchanged and
naive, and gains a `*_local` twin carrying the offset:

```json
{"performed_at": "2026-07-24 06:00:00",
 "performed_at_local": "2026-07-24T06:00:00.000-07:00",
 "timezone": "America/Los_Angeles",
 "timezone_source": "System Settings.time_zone",
 "stored_timezone": "America/Los_Angeles"}
```

Additive rather than in place, because a shipped handset parses the naive form
and cannot be upgraded from the server. Applied to `log_asset_state_change`,
`list_asset_state_history`, `get_irrigation_runtime`,
`export_insurance_schedule`, `list_shifts`, `get_shift`, and the mobile
`get_task` / `list_my_tasks`.

**`timezone` is accepted on all of them** — any IANA name. An unknown one is
refused rather than answered in UTC. Display only: no stored value moves, no
duration changes, and the days a window covers stay the site's days.

**What is stored is the SITE's zone, not UTC.** Frappe writes naive site-local
datetimes and every timestamp in this app is one; `stored_timezone` says so in
every payload. Writing UTC into new columns while the old ones stay site-local
would put two zones in one table — an `open_valve` at 06:00 local paired with a
`close_valve` at 13:00 UTC is an hour of irrigation that was sixty seconds.

**There is no `utc_offset` key** anywhere. Pacific is -07:00 in July and -08:00
in January; each timestamp carries the offset in force at its own instant.

### `export_insurance_schedule`

Every capital asset as an insurance schedule line. Read-only.

**Arguments:** `company`, `asset_types`, `include_retired`, `acquired_after`,
`acquired_before`, `limit`, `timezone`. Defaults to Tractor, Vehicle, Implement
and Sprayer — a valve is a fitting and a block is land.

**Returns**

| Field | Meaning |
| --- | --- |
| `schedule[]` | One line per machine |
| `.serial_number`, `.model`, `.acquired_on` | What an adjuster matches a claim against |
| `.purchase_value`, `.replacement_value` | Both, always; separate columns for ever |
| `.insured_value`, `.value_basis` | The number the schedule is written against, and whether it came from `replacement`, `purchase` or `none` |
| `.location_path[]`, `.location` | The chain of assets above it — `MC-Ranch › MC-Shed` |
| `.photos[]`, `.photo_url`, `.photo_count` | File attachments, newest first; private URLs reported as stored |
| `gaps` | Every missing serial, value, photograph and acquisition date, listed **and** counted |
| `by_asset_type` | Count and insured value per type |
| `total_insured_value` | **Withheld unless scoped to one company** — two entities are two policies |

**Replacement value falls back to purchase value** and says so per row. A 2011
price presented as today's cover understates the loss on exactly the machines
most likely to be old.

### `register_asset` takes the whole registration

New arguments: `parent_asset` (a second spelling of `location` — both work;
disagreeing values are refused rather than resolved by precedence),
`serial_number`, `model`, `acquired_on`, `purchase_value`, `replacement_value`,
and `photo_file_token`.

**The photograph is a two-step flow.** Upload with `stage_file_chunk` →
`finalize_staged_file` (which verifies a SHA-256), then pass the File docname
that returns as `photo_file_token`. A failed attach does **not** undo the
registration — the reply carries `photo_error` and the asset name, and
`attach_file_to_document` completes it.

### `action_menu`: what to offer for what was scanned

On `universal_scan`, `scan_asset` and `get_available_actions`.

| Field | Meaning |
| --- | --- |
| `action` | The action name a method takes |
| `label` | The worker's words — `open_valve` is `Turn On` |
| `kind` | `state_change`, `inspection`, `task` or `record` |
| `method` | The endpoint that performs it, or `null` |
| `implemented` | Whether this server can do it yet |
| `available` | Whether it can be done *right now*, given the current state |
| `from_state`, `to_state` | On an available state change |
| `unavailable_reason` | Why not — an illegal move and an unbuilt action read differently |
| `note` | On unbuilt rows: what is missing, and why the obvious shortcut is wrong |

`state_actions` remains the subset that is a legal transition right now.
`available_actions` on a scan is unchanged — it is the handset's own five-string
button vocabulary.

**Unbuilt actions are published, marked.** Publishing only the finished ones
gives iOS no way to lay out a screen it will need next month; publishing them
undifferentiated gives a worker a button that fails after they have walked to the
machine. v0.78.0 built two of the four that were unbuilt here — engine hours and
the REI timer — so the remaining unbuilt rows are application rates and
calibration records, each with a note saying what building it would take.

**Tractors and vehicles gained `check_out` / `check_in`.** Who has the machine is
a different question from whether it runs. `start_maintenance` reaches from
`checked_out` because that is where a breakdown happens; `put_in_service` does
not, because a machine coming back from the field is checked in by the person who
has it.

**Which tractor an implement is on is the register tree, not a state.** `attach`
records that it is hitched and when; `update_registered_asset(parent_asset=…)`
records what to. Duplicating the link inside a state blob would give one fact two
homes.

---

## The machine, the water and the block (v0.78.0)

Six features that finish what the asset register was for. The tag on a valve has
been scannable since v0.25.0 and the state log has been filling up ever since;
this release is what that accumulated log can finally answer.

### The hole this closes

A worker scanned a tractor and got a name, a state and a menu. Everything else
they needed existed somewhere — the hours it had run, whether it was due for
service, what was open against it, whether the block it was parked in was closed
after a spray — and assembling it took seven calls. On a rural cell at the end of
a row, nobody makes seven calls.

And the one thing that could hurt somebody was not recorded at all.
`stock_bridge.spray_windows` has computed a restricted-entry interval since
v0.69.0 and stamped it on the Farm Task a spray closed, which is the right
arithmetic in the wrong shape: keyed on the task rather than the block, one
location per task where a tank goes out over four, nothing at all when the spray
came off a state change rather than a task, and never closed — so "is this block
clear right now" could not be asked.

### `record_spray_application` — MUTATING, default off

Files a completed spray and opens the window it creates: **one `Spray REI`
record per block**, so 40 CFR §170.407 is answered by asking about a block.

| Argument | Meaning |
| --- | --- |
| `blocks` | The blocks sprayed — `Field` docnames or `Asset Register` block tags, resolved against both |
| `block_doctype` | `Field` or `Asset Register`. Only needed where a name is in both |
| `materials_used` | The tank mix, `[{item_code, qty, uom}]` — the same shape a Farm Task stores |
| `sprayer` | Optional. Refused if it is not a Sprayer |
| `rei_hours` | State the interval outright, overriding every label in the tank |
| `completed_at` | When the application **finished**. The window runs from here |
| `source_task`, `applicator`, `end_spray`, `company`, `notes` | |

**The longest interval in the tank wins.** A four-hour product and a
twenty-four-hour one together restrict the block for twenty-four hours; it does
not become half-enterable at hour twelve. Every product in the mix is stored
beside the one that set the window, because the question asked after somebody
feels ill is about the mix.

**A spray with no computable interval creates nothing and says so.** Where
nothing in the tank has `rei_hours` on its Item, this refuses rather than writing
a zero-hour window — a window of no hours reads as *this block is clear*, which
is the one wrong answer that puts somebody in a treated row.

**The state change is allowed to fail and the record is not.** A sprayer never
marked `in_use` still gets its restrictions written, with the state machine's
refusal returned as a warning. A compliance record must not depend on somebody
having pressed the right button first.

### `get_active_rei` and `list_active_reis`

One block, or the whole farm. **The longest live window is the answer** — two
applications a day apart leave two records and the block clears when the last of
them does.

`warning` is one sentence and it is the same sentence everywhere:

```
REI active — Warrior II — 3.4 hours remaining — do not enter without PPE.
Block Home-7 was sprayed at 2026-08-15 06:10:00; entry is permitted from
2026-08-15 18:10:00.
```

A scan of the block, a scan of a machine parked in it, and a task dispatched to
it all render that string. A worker who reads one wording at a gate and a
different one on a work order has been given two rules.

**Closing is an act, not a comparison.** `status` is a real column, so "every
restriction on this farm right now" is one indexed query. `close_expired_reis`
maintains it — scheduled hourly, *and* run by every read in the module, so a
bench whose scheduler is wedged still answers correctly at a gate.

A dispatch to a restricted block is **warned, not refused**: §170.607 permits
early entry for specific tasks with the label's PPE, so a server refusing it
would be inventing a rule stricter than the regulation and training foremen to
route around this app.

`cancel_spray_rei` withdraws one, with a required reason. Cancelled rather than
deleted — *why did this block show as closed on Tuesday morning* has an answer,
and a deleted row has none.

### Engine hours

`Asset State Log.engine_hours` is the **series**; `Asset Register.current_hours`
is a **cache** of the highest reading seen. Every figure is computed from the
series, so a wrong cache is cosmetic and the next reading corrects it.

Readings arrive on a state change rather than through a call of their own —
`log_asset_state_change(action="check_out", engine_hours=1240.5)` — because the
moment somebody reads an hour meter is the moment they are sitting in the
machine. A check-in with a reading at both ends also records `hours_used` for
that session.

**A meter only counts up.** A reading below the last on record is refused as a
typo; `allow_meter_reset=true` says the instrument was swapped, and the
discontinuity is recorded in the log row's notes.

`get_engine_hours_summary` reads it back: the meter now, total recorded, hours
this season (`season_start`, default 1 January), hours since the last service,
and every session paired up. A machine still out is an **open** session with a
start and no length — nobody has read the meter since it left the yard.

### Maintenance scheduling

`Asset Register` gained `service_interval_hours`, `service_interval_days`,
`last_service_date` and `last_service_hours`. Both intervals are optional and
either alone is a complete schedule: a tractor on hours, an extinguisher on the
calendar, a pump on whichever comes first — and `due_on` names the one that bit.

`check_maintenance_due` answers for one asset or sweeps the register.
**Unmeasured is not overdue**: an hours interval with no reading ever recorded
comes back not-due with the reason, rather than filling a board with work nobody
can verify on the day an operator first sets an interval.

`trigger_maintenance_tasks` raises one Farm Task per due asset through
`create_farm_task`, so the work carries an evidence contract and lands on the
same board as everything else. `dry_run` **defaults to true** — this raises work
for other people. It will not raise a second task where one is already open
against the asset, through either the `asset` column or the location pair; a job
that re-raised it nightly would produce the exact backlog that teaches a crew to
ignore the board. `erpnext_mcp.tools.maintenance.sweep_due_maintenance` is the
bare scheduler entry point, and it iterates every company.

`record_service` closes one out. Without it every machine is overdue from the day
it was registered, which is the state that trains people to ignore the alert.

### `get_water_usage_report`

Valve runtime rolled up by `zone`, `block`, `week`, `month`, `day` or `valve`
over a date range, with an optional `field` filter — the report a water-rights
filing and a cost allocation are both written from.

**The measurement is `get_irrigation_runtime`'s, reused.** The same code handles
the four hard cases — a run that started before the window, one that ended after
it, one still open, and a close written by the cascade when somebody shut a main
— so two reports cannot disagree about how long a gate was open.

**Gallons are per-valve and never guessed.** `Asset Register.irrigation_zone` is
new: the one link out of the asset tree, because the tree could not carry it —
an asset's parent is another asset, so a valve could name the zone-shaped *asset*
above it and never the `Irrigation Zone` record that holds the flow rate, the
water right and the block. A valve with the column empty contributes its
**minutes** to every total and no gallons, and is named in `unpriced_valves`
rather than quietly dropped from a figure somebody is about to file with a
district. `flow_rate_gpm` prices every valve at one rate for a single-pump
system.

**A run is billed whole to the period it started in.** A set opened on Saturday
night that ran into Sunday is Saturday's irrigation — which is how the person who
opened it would describe it, and what makes the report reconcilable against the
valve log.

`get_irrigation_runtime` is deliberately untouched: its arguments mean exactly
what they meant, because a report somebody has been running all season must not
change its answer on an upgrade.

## The QR valve workflow

Six tools for the thing a person actually does with a valve: walk up to it, scan
the sticker, see what it is doing, press one button.

**There is no `Irrigation Valve` doctype and there is not going to be one.** A
valve has been an `Asset Register` row of type `Irrigation Valve` since v0.25.0,
with a QR label whose payload is its docname, a parent in the `location` tree, a
state machine, a closing cascade, and an `Asset State Log` that
`get_irrigation_runtime` sums into water minutes. A second table of valves would
be a second account of the same pipe — two rows for one gate, two states that
disagree the first time somebody corrects one, and two answers to "how long did
zone 3 run" of which the wrong one is whichever a water district happened to
read. These six are the *workflow* on top of the register that is already there.

Two columns are new on `Asset Register`: `valve_type` (Main, Sub-Main, Lateral)
and `installed_date`. A third, `last_state_change`, is a **cache of the log**
stamped in the same save that moves `current_state`, from the same timestamp the
log row carries — so a list of forty valves can say "open since 06:12" without
forty queries. Runtime is still summed from the log and never from the cache.

### `create_irrigation_valve` — MUTATING, default off

`register_asset` with the valve's own three refusals in front of it. The docname
is the printable tag ID and the QR is derived from it.

**The parent is the hierarchy; the rank is not.** `parent_valve` writes
`location`, which is the column the closing cascade walks. `valve_type` is what a
worker calls the thing. They are checked against each other exactly once, here,
because this is the only moment somebody states both — and a Main filed
underneath a Lateral is refused, because the cascade would otherwise honour it
and shut a line from the wrong end. Two laterals in a row are fine; that is real
plumbing.

**The zone is the only link to a flow rate**, so it is required — but a valve
under a parent that already has one inherits it, and `zone_source` says which
happened. Inheriting from anywhere else would be a guess, and this column is what
`get_water_usage_report` prices gallons with.

### `list_irrigation_valves`, `get_irrigation_valve`

**A valve nobody has ever toggled is closed, not unknown.** `current_state` is
empty until the first change is logged and the machine's default is what that
means, so the `state` filter is applied to the *resolved* state rather than to
the column — a SQL filter would drop every valve on a new install from
`state=closed`.

`get_irrigation_valve` returns the zone with its flow rate, the chain of valves
above (what would have to be shut to dry this out), the valves below and which
of them are open, and `runtime_today` — which carries **two** figures on purpose:
`minutes` for this valve and `subtree_minutes` for everything it commands. A main
with four laterals running under it has not itself been open four times as long.

### `toggle_irrigation_valve` — MUTATING, default off

Opens a closed valve, closes an open one. The state is read here so the caller
does not have to know it, which is the whole reason this exists rather than an
open/close pair: `log_asset_state_change` wants `close_valve`, and a phone that
has just read a QR cannot know the gate was open. That is what makes a scan
resolve to a *button* rather than to a menu.

**Closing carries down the line and opening does not.** Shutting a main stops the
water below it for certain, so every valve beneath is closed too and each gets a
real log row naming the main in `cascaded_from`; `cascaded` lists them and
`cascade_skipped` names every descendant that was *not* closed, with the reason.
Opening a main only makes water *available* to what is below, each of which is
opened on its own account — an opening cascade would mark every lateral as
running, and those events are exactly what `get_water_usage_report` prices into
gallons. A child closes without touching its parent. See
`asset_tags._CASCADING_ACTIONS` for the argument in full.

Refuses a winterized valve (send `reopen` through `log_asset_state_change` —
un-winterizing a line mid-season is a deliberate act), a retired one, and an
`expect_state` that no longer matches, which is for a screen drawn before
somebody else moved the gate.

### `get_valve_runtime`

Hours one valve ran over a window with its whole zone's total beside it, so the
two can be read against each other — a lateral that ran three hours on a zone
that ran forty is a sentence somebody can act on. Both figures come from
`irrigation._runs_for`; it is not a second sum. `date_from`/`date_to` and
`from_date`/`to_date` are the same two arguments under both spellings.

### `scan_valve_qr` — MUTATING, default off

**The string a camera produces is a URL**, not a docname — a printed tag encodes
`<site>/scan/<docname>`, unwound here by the same parser `universal_scan` uses. A
bare valve ID typed into a manual-entry box passes through untouched, so both are
one call. A credential QR is refused before any register is read and is never
quoted back.

**What is written is the scan stamp and nothing else** — `last_scan_at`,
`last_scan_by` and the GPS fix. **The valve is not toggled.** Scanning a tag is
looking at a thing; opening water onto a block is a decision, and closing a main
is a decision that dries out everything beneath it. `next_action` in the answer
is the button; `toggle_irrigation_valve` is what it posts to.

A tag that is not a valve is refused **by naming what it is**, so a worker who
scanned a tractor learns which screen they wanted rather than being sent back to
a menu with nothing.

### The mobile route

`POST /api/method/erpnext_mcp.api.mobile.scan_valve` is the iOS scan-to-action:
it resolves the tag, records the scan, and — only when the body sends
`toggle: true` — opens or shuts the gate in the same POST, picking the action
from the state the phone cannot know. **`toggle` defaults to false**, which is
the whole safety of it: a camera that fired on recognition would water a block
because somebody walked past with a phone. The company comes from the scope
check and never from the body, so a tag belonging to another entity resolves as
though it were not there.

A refused toggle rolls the scan stamp back with it — that is the framework's
transaction, not a choice — but the audit row survives, because `guard.endpoint`
commits its failure rows apart from the request. `last_scan_at` records completed
scans, not attempts.

### `get_asset_status_report`, and what a scan returns now

`scan_asset` and `universal_scan` both return the whole picture under `status`,
composed once in `tools/asset_status.py`. `get_asset_status_report` is the same
block for a caller who is **not** scanning — so it stamps no `last_scan_at`,
because nobody was standing there.

| Key | What it carries |
| --- | --- |
| `state` | Current state, the type's default, last scan |
| `maintenance` | Due on hours / days, by how much, next service date |
| `engine_hours` | Meter now, this season, since service, sessions |
| `runtime` | For a valve: minutes today, this week, this season, running now |
| `parent_valve` | The valve above, and whether it is shutting off the water |
| `open_tasks`, `compliance_alerts`, `recent_activity` | |
| `active_reis` | Restrictions on the ground this asset stands on |
| `applied_reis` | For a sprayer: what **it** closed |
| `warnings` | Ordered by what would hurt somebody first |
| `sections_unavailable` | Named, not silent |

`active_reis` and `applied_reis` are kept apart deliberately. A screen that
merged them would tell a worker they may not enter the tractor shed.

**Sections degrade; the call does not fail.** A scan is the most
latency-sensitive call this app makes and it is made by somebody standing in the
sun. A section whose doctype has not migrated or whose register will not answer
comes back empty and is **named** — "no open tasks" and "the task register would
not answer" are different sentences, and only one of them is a reason to call
somebody.

`needs_attention` and `warnings` are hoisted to the top level of a scan, because
they are what a screen colours the whole card on. The flat keys every shipped
handset already decodes — `state`, `open_tasks`, `action_menu` — are untouched.

### The three mobile routes iOS was blocked on

`register_asset`, `generate_asset_qr` and `attach_file_to_document` are now on
`/farmops/api/mobile/…`. Field registration is one flow: photograph the plate,
register the asset, get the QR back to print, attach the photograph.

`attach_file_to_document` is the general tool behind a **doctype allowlist** in
the wrapper. The tool will attach to any document on the site; a phone may attach
to the registers a field app actually writes into, and everything else is refused
by name. `allow_cancelled` is not passed through at all.

---

## The day as it actually happens (v0.79.0)

Ten features, six new doctypes, nineteen new mobile routes. Every one of them is
about the gap between how the app modelled a day and how a day goes.

### `pause_farm_task` / `resume_farm_task` — MUTATING, default off

A worker sets an irrigation line at nine and is called to a broken valve at half
past. The irrigation is not finished, not abandoned, and not being done — and
until now the app had three bad answers: leave it In-Progress and lie about who
is working on what, complete it and lie about it being done, or reject it and
throw away the morning.

**`Paused` is a state, not a flag.** A boolean beside In-Progress would leave the
Kanban board showing two tasks in one column with one of them not being worked,
which is the picture a dispatch board exists to prevent.

**The hour is the sum of the segments.** A run is now `Task Time Segment` rows —
start to pause, resume to pause, resume to completion — and
`actual_duration_minutes` is their total. The wall clock across an interruption
bills the valve repair to the irrigating, on exactly the fragmented afternoons
where an hour charged to a job matters most. Assignments written before this
release have no segments and fall back to the old arithmetic; a duration that
came back zero for a season of history would be the worse bug.

**One task In-Progress per worker, enforced by pausing rather than by refusing.**
Starting or resuming a second task auto-pauses the first, marks it `auto_paused`,
and **says so in the answer** — a silent stand-down would leave a worker
discovering at the end of the day that their morning went to a task they thought
they were still on. Refusing would be defensible and routed around within a week.
Claiming does **not** auto-pause: claiming is planning a morning, and a worker may
hold three.

A paused task can be rejected without resuming it first. "I was called away and
the ladder is still broken" is the sentence a board needs.

### `link_farm_tasks` / `merge_farm_task` — MUTATING, default off

Two workers walk past the same leaking valve an hour apart and both do the right
thing. **A dispatch board that silently deduplicated them would be guessing that
two reports of a valve are the same valve** — and on a farm with four hundred of
them, sometimes they are not.

So `claim` and `start` return a `duplicate_hint`:

```
There is already an open task for this: Leaking valve at Home-7 (FT-2026-08-00031)
held by Ana Ramos. Link to it?
```

…and nothing merges itself. `link_farm_tasks` writes a row on **both** sides —
a relationship stored on one record only is invisible from whichever of the two
somebody opens. `merge_farm_task` folds a duplicate into a primary:

- the **primary keeps its state and its clock** — a merge says which record the
  work is under, it is not an event in the work
- the duplicate goes to **`Merged`** with `merged_into` naming the primary
- its **evidence is copied** onto the primary's completion
- its **assignments and time segments stay where they are**, so `combined_minutes`
  is the effort both people actually put in

Nothing is deleted. Merging a *finished* task is refused — link it instead; a
completed job is a record of work that happened.

### Sub-tasks, and work that does not finish today

`Farm Task.parent_task` is a **Dynamic Link**, because the thing with steps under
it is as often an `Accident Report` as another task. An investigation's steps are
"interview the witness", "photograph the scene", "write the root cause" — each
with its own assignee, its own clock and its own state.

**A parent does not close while a step is live.** Without that rule the first
person to finish their piece closes the investigation, and the camera footage
nobody pulled becomes a finding nobody made. The refusal names the steps.

One level of nesting only: a tree of sub-tasks is a project plan, and a dispatch
board that became one would stop being readable at a tailgate.

**Nothing auto-closes at the end of a shift.** `end_shift` ends a *shift*; a task
is not a shift.

### Narrative notes, typed and spoken

`Task Note` is **one child table with three parents** — Farm Task, Accident
Report, Farm Incident Record — because appending an account of what happened is one
act, and three near-identical tables would drift the first time one grew a column.

**Entries are appended and never edited.** An investigation spanning four days is
four entries with four timestamps, and the reason a hearing believes any of it is
that Monday's account was written on Monday.

`attach_audio_note` is the call this release exists for on the day somebody is on
the ground. A foreman at an accident scene has a phone in one hand and about
ninety seconds of clear memory; typing is not what happens next. iOS's Speech
framework transcribes **on-device**, so a foreman in a block with no signal still
gets text, and this stores what the phone produced.

**The transcript is the required half and the audio is optional** — the opposite
of what the name suggests and the right way round. The text is what a report, a
search and a reviewer read; the recording is evidence *about* it. A failed file
link comes back as `audio_error` with the words already saved.

`source_type` and `source_language` are on every entry. A transcript presented as
a verbatim quote is being presented as something it is not, and a Spanish account
tagged as English is a translation nobody knows is needed.

### Progressive discipline

**In a wrongful-termination claim the documentation is the case.** The question
is almost never whether the final step was deserved; it is whether the employer
can produce a documented, escalating, acknowledged series.

| Tool | What it does |
| --- | --- |
| `create_incident_record` | One step, auto-linked to the prior one |
| `acknowledge_incident_record` | Signed, or declined with a witness |
| `get_incident_record` | One step with its narrative and predecessor |
| `list_incident_history` | The chain in order, current level, next step |
| `get_incident_report` | The document for legal review — **including its gaps** |
| `expire_incident_record` | Age one out, or withdraw it. Never deletes |

**The prior record is found, not asked for.** Asking whoever is typing which
docname the last warning was is asking them to go and look, which is how a chain
acquires a missing link.

**A skip has to be explained.** More than one rung at a time is refused until
`supersedes_note` says why. It may be entirely right — a safety violation is not
a progressive matter — and the reason is worth more written today than
reconstructed in two years. The same rung twice needs no explanation; nor does a
step *down*, because somebody being generous never has to be defended.

**A refusal to sign is an outcome, not a gap.** An acknowledgement with neither a
signature nor an explicit refusal is refused; a refusal with no witness named is
refused. What the file may not contain is silence presented as agreement.

**`get_incident_report` names the holes**, the same argument
`export_insurance_schedule` makes about missing serial numbers:

```
4 gap(s) in this chain. Each is listed above with what is missing and why it
matters. THESE ARE FIXABLE NOW AND NOT LATER: an acknowledgement obtained today
for a warning issued in March is worth something; the same signature obtained
after a claim is filed is worth very little.
```

It reports whether the *documentation* is complete and says plainly that it is
not legal advice and never whether a step was warranted.

**Nothing expires on a schedule.** The look-back window that discounts an old
warning differs by employer and by state; a server quietly ageing steps out would
be making that decision for somebody.

### Accident investigation

29 CFR 1904 is the regulation. **The design problem is the first ten minutes.**
This record is opened on a phone, in a block, by a foreman whose attention is on
somebody sitting on the ground. Asking for a root cause at that moment produces
exactly one outcome: nobody opens the record until the evening, and the evening's
account is worth a fraction of the scene's.

So it is four calls: `create_accident_report` (when, what, who was hurt, who saw
it, what was done) → `update_accident_investigation`, as many times as it takes →
`close_accident_investigation`. Status walks Open → In Progress → Corrective
Actions Pending → Closed.

**Witnesses are rows, not a string.** A witness is somebody an investigator has to
go back to, and *we still have not interviewed Miguel* is the most useful thing a
half-finished investigation knows. A comma-separated string is accepted and split
into rows so the outstanding-statement flag works either way.

**Recordability is a person's determination and this app does not infer it.** It
would be easy to map severity onto the 300 log and wrong: 1904.7 turns on medical
treatment beyond first aid, and the consequence of being wrong is a citation.
`osha_recordable` defaults to `Undetermined`, changing it requires the **basis**,
and an investigation cannot close while it stands.

A fatality, hospitalisation, amputation or loss of an eye returns the 1904.39
telephone obligation in `urgent_obligations` — this record does not make that
call and cannot.

**Closing is checked.** No corrective action, no follow-up date, an undetermined
recordability, an untaken witness statement or an open sub-task and it does not
close — and **everything outstanding is named at once**, because a close that
fails four times naming one more field each time is how somebody learns to stop
closing things. `outstanding` is on every read, not just the close: an
investigation that tells you on day three what it is waiting for is one somebody
finishes.

### The OSHA 300 log and its 300A summary

#### `get_osha_300_log` / `get_osha_300a_summary`

**Read-only.** Both take `company` and a four-digit **calendar** `year` — 1904.4
keeps the log by calendar year, and a fiscal year or a season is a different
document.

**The filter is the whole difference from the register.** Only cases determined
recordable appear on the log. A near miss is not a case; a first-aid-only injury
is not a case. That determination is a person's and this app never inferred it.

**The log reports its own incompleteness.** `undetermined_cases` names every
report of the year whose recordability nobody has decided. They cannot be on the
log — the determination is what puts them there — but a log that omitted them
silently would present a partial year as a finished one, which is the shape of a
document somebody signs without noticing.

**Every case is counted once**, at its most severe outcome: death → days away →
restricted → other recordable. Adding the columns of a correct 300A gives the
case count, and it only does that if the classification is exclusive. Day counts
are capped at 180 for the totals per 1904.7(b)(3)(viii), with the raw figures
reported beside them.

**The rates need hours and will not invent them.** TRIR, DART and LTIR are all
`cases × 200,000 / hours worked`. The denominator comes off the Farm Shift
register through the same span arithmetic payroll runs on — which counts only
people who clocked through this app, so it is a **floor**, and every rate built
on it is therefore a **ceiling**. `total_hours_worked` and `average_employees`
override it, which is the right answer where payroll lives elsewhere. Where
neither supplies it the rates come back `null` **with a note**, never `0.0`: a
zero rate reads on every screen as a perfect safety year.

**Privacy cases are not applied.** 1904.29(b)(7) withholds the name from the
posted log for six categories plus any case the employee asks be kept private,
none of which this app can determine. Every name comes back and a person
withholds them before posting. Posting between 1 February and 30 April and the
executive certification are likewise acts a tool cannot perform, and neither has
happened because one of these ran.

### Wizards as data

**The problem is the App Store.** A flow compiled into Swift needs a release to
change a question, and the questions change when the law does. A farm that
discovers in July that its state now requires a heat-illness acknowledgement at
hire cannot wait three weeks for a build and a review.

`get_wizard_definition` returns ordered steps, the fields on each, their
validation, and the conditional logic that decides what comes next. The field
types are the **handset's** — `photo`, `signature`, `qr_scan`, `audio_note`,
`employee_select` — things a phone does and a Desk form does not.

Five shipped: `accident_investigation`, `progressive_discipline`,
`asset_registration`, `employee_onboarding`, `inspection_session`. **A shipped
wizard is never overwritten** once an operator has edited it — a flow somebody
tuned for their own crew being reset by a migration is what would make "config
not code" a lie.

**The validation is a courtesy, not a control.** It is there so a worker finds out
about a malformed VIN at the machine rather than at submission; the endpoint in
`submit_method` applies its own refusals to whatever arrives.

### Bilingual, and loud about the gaps

Every label, title, description, help text and select option carries `_en` and
`_es`, and `get_wizard_definition` resolves them against the caller's own
`Employee.preferred_language` — a new compliance field, because OSHA 1910.1200(h)
and WPS 40 CFR 170.501 both require training in a language the worker understands,
and an employer who cannot say which language they used cannot show they did.

**Never inferred from a device locale.** A phone set to English by whoever handed
it over says nothing about who is holding it now.

**A missing translation falls back to English and is listed** in `untranslated`.
Silently serving English means nobody finds out until a worker is in front of a
screen they cannot read; refusing to serve the wizard locks the crew out over a
missing string. Fall back, and be loud about it.

### What a scan says now

The status report gained two sections. `paused_tasks` is keyed on the **worker**
rather than the asset, because it is a fact about the person holding the phone:

```
You have a paused task: Irrigate Block 3 (paused 22 min ago)
```

That belongs on a scan because the scan is the moment they have forgotten the
line — the sentence is not on the screen of the job they walked away from, it is
on the screen in front of them now. `subtasks_by_parent` shows the steps of any
open investigation on the asset.

### The nineteen mobile routes, and why the gates differ

| Group | Gate | Why |
| --- | --- | --- |
| `pause` / `resume` | enrolment | A worker's own work. `worker_id` is not on the signature, so the argument filter stops an account stopping a stranger's clock |
| `link_tasks` | enrolment | An observation. Noticing two jobs are one valve is what a worker in a block sees and a foreman at a desk does not |
| `merge_task` | Foreman+ | A decision. It takes somebody's work off the board under another name |
| narrative trio | enrolment | A worker's account of their own job. The Farm Incident Record parent takes HR |
| five discipline routes | **HR role** | A personnel document. A field credential has no business in one |
| `create_accident_report`, `get_accident_report` | enrolment | **The person who finds somebody on the ground is whoever finds them.** A server that refused their report because they are not a foreman is one people work around at the exact moment that matters |
| update / close / list accidents | Foreman+ | The investigation is somebody's job |
| wizard reads | enrolment | Answered in the caller's own language |

`expire_incident_record` has no route: ageing a step out of a chain is a policy
decision made at a desk with the handbook open.

## Trade documentation across three tiers (v0.80.0)

Sixteen tools, five doctypes, and one idea: **fruit leaves a farm three ways and
the paperwork is the only thing that differs.** A truck to the packing house down
the road, a truck across a state line, and a reefer on a vessel. The tier decides
how much paper, not which system.

### The mistake this prevents

A desk that keeps local deliveries in a spreadsheet, interstate freight in a
folder and exports in a broker's portal is a desk where **the export paperwork is
the only paperwork anybody checks** — because it is the only one that lives
somewhere that looks like paperwork. Then a domestic load moves without a cold
chain record and nobody notices until a buyer asks, by which time the truck
arrived three weeks ago and the record cannot be made honestly.

So: one `Trade Shipment`, one checklist built from the destination's own rules,
one register of documents.

### Config, not code

| Doctype | What it is |
| --- | --- |
| `Trade Document Template` | The SHAPE of a kind of paper — its schema, where it populates from, who signs it, whose system files it |
| `Destination Document Requirement` | Shipping HERE needs THAT |
| `Trade Shipment` | One load, its route, and its frozen checklist |
| `Trade Shipment Document` | One checklist line |
| `Trade Document` | One piece of paperwork, any of the sixteen types |

**Nothing in this app's code names a country.** Adding Vietnam is adding rows —
`create_trade_document_template` and `set_destination_requirements`. If it were a
release, the release would be the bottleneck and the desk would go back to the
broker's portal.

Thirteen document types are **one polymorphic doctype** rather than thirteen.
They share a lifecycle (drafted → reviewed → approved → sealed), a home, and an
evidence requirement; they share almost no fields. So the lifecycle is columns
and the content is `document_data`, because the alternative is thirteen
near-empty doctypes or one table that is ninety per cent NULL.

### Advisory by default — and the default is the load-bearing part

`update_shipment_status` is the only gate, and it guards one transition: **Ready
to Ship**. `trade_document_enforcement` ships **off**.

A two-truck operation locked out of its own delivery by a phytosanitary
certificate it will never need turns this module off within a week — and an
operation that has turned it off gets no warnings either, which is strictly worse
than an advisory gate that reports the gaps and lets the truck go. Advisory mode
returns the identical readiness answer. An operation big enough to want the gate
turns it on, per site or per shipment via the shipment's own `enforcement` field.

**The override is recorded.** Where enforcement is on, releasing anyway needs
`override_reason`, written to the shipment. A bypass nobody recorded is a bypass
nobody can review — which is the difference between an advisory control and no
control.

### Four ways a document that looks done is not

`get_shipment_readiness` names each, and every one has held a container:

1. it is not approved yet;
2. it has been **voided** — a withdrawn certificate is not a certificate;
3. it has **expired**. An ePhyto approved in June for a September sailing is one a
   border rejects, and a checklist that counted it because the status column said
   Approved would report a shipment ready that is not;
4. it requires an **external filing** and carries no reference back.

### This app files nothing

An ePhyto is lodged in PCIT. An EEI is filed in AES. An eBL is issued on a DCSA
platform. **This app records that somebody filed and what reference came back** —
and a document whose template says it needs an external filing and which has no
reference is reported outstanding however approved it looks. A module that
implied it had transmitted a certificate would be the most dangerous thing in
this repository.

Field names follow the published data models so a broker's schema and this app's
can be reconciled **by reading**: IPPC/ISPM-12 (ePhyto), the DCSA data model
(eBL), 15 CFR 30 EEI elements (AES), WCO origin criteria. Those are the
standards' *names*, not an implementation of their transports.

### The seal

`seal_trade_document` fingerprints an approved document and closes it to editing.
Until then, "this is the certificate we presented" is an assertion about a record
anybody could have edited since; after it, the claim is checkable. A sealed
document **refuses content edits** — a seal over a row that can still change is a
timestamp wearing a seal's clothes. Correcting one means voiding it and issuing a
replacement, which is what happens to a real certificate that is withdrawn.

The hash covers an **allow-list** of columns, stored beside it, so a column added
in a later version cannot make every previously sealed document fail
verification. `get_trade_document` recomputes on every read, so a document
changed underneath its seal reports the seal as broken rather than looking intact.

`generate_shipment_packet` bundles the lot and hashes the bundle over its
members' hashes. It refuses unsealed documents by default — a packet is something
somebody carries into a room and defends — and `allow_unsealed=true` names them
at the front rather than dropping them, because a bundle that quietly omitted
them would read as a shipment with less paperwork than it has.

### Two things that are reported and never applied

**The checklist is a snapshot.** It is built once, at creation, and is never
silently rebuilt: a destination's rules changing in March must not quietly add a
requirement to a February shipment that has already sailed.
`get_shipment_readiness` reports `requirement_drift` instead.

**Removed requirements are disabled, not deleted.** A shipment made under a rule
is audited against what was asked for *then*, and a deleted rule cannot answer
why it was needed.

### The five mobile routes, and what is deliberately not among them

| Route | Gate | Why |
| --- | --- | --- |
| `list_shipments`, `get_shipment` | enrolment | What is going out, scoped to the entities the caller may reach |
| `get_shipment_readiness` | enrolment | The question somebody standing next to a truck actually has |
| `list_trade_documents` | enrolment | The paperwork on a load |
| `confirm_shipment_movement` | enrolment | A driver says "I have left" / "I have arrived" — nobody needs a certificate to perform that act |

`confirm_shipment_movement` takes `departed` or `delivered` and **nothing else**.
It does not forward `override_reason`, cannot release a shipment to Ready to Ship
and cannot cancel one. A release is an assertion that the paperwork is in order,
made by somebody with a trade role at a desk; an account that could make it from
a phone in a yard would make the gate worth nothing. Same argument that keeps
`cancel=true` off `reject_farm_task`.

`approve_trade_document` and `seal_trade_document` have no route at all, and
require one of **System Manager, Farm Manager, Compliance Officer, Sales Manager
or Accounts Manager**. A commercial invoice is a customs declaration and a
phytosanitary certificate is a claim about a pest; neither is anonymous.

## Governance, ITGC and disclosure (v0.81.0)

Twenty-nine tools, seven doctypes, five control points — IPO readiness Phases 4
to 6. Every control is **bypassable**, and that is the design: same code, same
data trail, two strictnesses.

```
                 evaluates   reaches a finding   files the alert   lets it through
ADVISORY            yes            yes                 yes               yes
ENFORCED            yes            yes                 yes               no
```

Nothing differs but the last column. An operation that spends a season in
Advisory ends it holding **exactly the register of findings it would hold had it
been enforcing** — which is what turns switching enforcement on from a gamble
into a decision made with the evidence already in hand. Everything ships
Advisory; the switch is `enforcement_mode` on a Compliance Rule, flipped with
`update_compliance_rule`.

### The five control points

| Control point | Refuses, when enforced |
| --- | --- |
| `related_party_transfer_pricing` | Booking a related-party transaction with no documentation covering it |
| `access_review` | Granting access when the last review is older than the review period |
| `change_approval` | Recording a system change with no approver, or with its own author as approver |
| `backup_verification` | Declaring recovery readiness with no verified restore in the window |
| `disclosure_completeness` | Marking a filing complete with required disclosures outstanding |

### Phase 4 — related parties

| Tool | What it does |
| --- | --- |
| `get_related_party_transactions` | Every dealing in a window with somebody on the register, one row per voucher, stamped with whether an arm's-length case covers it |
| `flag_related_party_transaction` | Runs one dealing through the control. Advisory reports and allows; Enforced refuses. **Mutating** — it writes compliance alerts |
| `list_related_party_disclosures` | The disclosure register and the gaps behind each relationship |
| `generate_related_party_disclosure` | The period's schedule: parties, totals, coverage, and what could not be resolved |
| `create_transfer_pricing_doc` | The arm's-length case: the price, the reference it was tested against, why they agree |
| `get_transfer_pricing_doc` | One memo, plus the transactions it actually covers |
| `list_transfer_pricing_docs` | The memos on file, and which Complete ones were reviewed by their own author |
| `update_transfer_pricing_doc` | Change one — including the promotion to Complete, which is what makes it cover anything |

**The match is never a guess.** A ledger party becomes a related party by the
register row's `supplier` **link** (`match: supplier_link`) or, failing that, by
an exact case-folded name match (`match: name`) — always labelled. Everything
that resolved to neither comes back in `unmatched_parties` with its total, rather
than being dropped: the commonest way a related-party schedule is wrong is not a
mispriced dealing, it is a relationship nobody wrote down.

**Coverage is one of four values**, because the remedies are different work:
`documented`; `draft_only` (a memo exists, unfinished); `amount_exceeds_
documentation` (a memo for $12,000 does not document $140,000 — 10% tolerance);
`undocumented` (nothing was ever written). `covers_row()` is the single
definition of "documented" on the site, called by the gate, the register and the
year-end schedule alike, so none can grow its own opinion.

### Phase 5 — IT general controls

| Tool | What it does |
| --- | --- |
| `generate_access_control_report` | Who has what, computed now and **stored nowhere**: logins, roles, what those roles reach, last login |
| `create_change_management_log` | One system change: what, who, who approved, how to undo |
| `get_change_management_log` / `list_change_management_logs` | One change; the log, with unapproved and self-approved rows called out |
| `get_change_management_report` | Volume, approval rate, high-risk untested changes — and how much of the log is self-attested |
| `create_backup_record` | A backup ran: kind, when, where, how it ended, and the RPO/RTO it is measured against |
| `get_backup_record` / `list_backup_records` | One event with its RTO verdict; the fleet with its verification picture |
| `record_backup_test` | A test restore — the event that turns a job into a control |

**Access is computed, never stored.** A permissions snapshot in a table is wrong
the moment somebody adds a role, and a stale one is worse than none because it is
the document an operator hands an auditor while believing it. What *is* stored is
that a review happened: a `Change Management Log` row of type `Permission`.

**No `doc_events`.** `hooks.py` promises this app installs none, and hanging a
hook on every Custom DocPerm write would break that promise for every site. The
change log is populated from this app's own dispatcher path instead; those rows
carry `source = MCP Tool` and a link to the MCP Action Log row for the call.
`get_change_management_report` reports the self-attested split rather than hiding
it, because "how much of your change log did you write about yourself" is the
first question worth asking about a change log.

**An approver who is the person who made the change is not an approver** —
refused in the tool *and* the controller, so a Desk row obeys it too. A
one-person finance function records that honestly as `Not Required` with the
compensating control in the notes: a documented exception is defensible, a
self-approval is a finding.

**A green job log is not a verification.** `Not Tested` is the default and is not
a failure; `Partial` does not verify; a `Fail` is the most valuable row in the
table, found on a day somebody chose rather than a day chosen for them. An
undated result is refused — it could not be counted by any window.

### Phase 6 — reporting and disclosure

| Tool | What it does |
| --- | --- |
| `create_reporting_template` / `get_` / `list_` / `update_reporting_template` | The SHAPE of a periodic report: sections, in order, each naming the tool that fills it |
| `generate_mda_data_feed` | The figures an MD&A is written **from**, each carrying its window |
| `generate_segment_report` | Revenue, expense and result by cost centre, with ASC 280's test applied and its working shown |
| `create_disclosure_checklist` / `get_` / `list_` / `update_disclosure_checklist` | Which DISCLOSURES a filing must make, and who decided |
| `complete_disclosure_item` | Settle one — made, or decided not to apply |
| `generate_quarterly_report_skeleton` | The pieces assembled: headings, sources, figures, and every gap |

A section and a disclosure are **deliberately different objects**. "Results of
Operations" is a section that may carry four disclosures or none; folding them
together would mean either a report with sixty headings, or a disclosure that
could exist only where somebody had already written a section for it — which is
precisely how a disclosure gets omitted.

**Nothing here files anything.** Every generator returns a working paper, and the
skeleton contains **no prose and never will**: a management discussion that
arrived pre-written is the one nobody reads before it is filed. Sections with no
runnable source come back marked `to_be_written_by_a_person`.

**A feed is honest about its holes.** `generate_mda_data_feed` attempts every
source and names every failure in `unavailable` with its reason. A generator that
raised on its first missing input is one nobody on a farm mid-setup could ever
run, and would therefore never be run at all.

**Segments are applied and shown, not decided.** The ten-per-cent test is
computed per cost centre with its working, plus the 75% coverage check. Whether
these cost centres are genuinely different *operating segments* is a judgement
about how the business is managed and how the chief operating decision maker
reviews it — no query can make that call. Postings with no cost centre belong to
no segment and are reported separately, which is why the segments can sum to less
than the company.

**`Not Applicable` is a decision; `Outstanding` is not.** The whole value of a
checklist is that somebody decided about every line. So Not Applicable settles an
item and **requires a reason** — the reason *is* the disclosure — while
`In Progress` does not settle it, because work started is not a decision reached.
Reopening an item clears who completed it.

### Bilingual, where it applies

Section headings and disclosure items carry `label_es`, and the three shipped
templates (`10-K Sections`, `10-Q Sections`, `MD&A`) ship with theirs. A missing
translation serves the English and reports the gap in `untranslated` — the
posture `list_wizard_definitions` has taken since v0.79.0. The ledger-facing
records are not translated, and that is a decision: they are read by accountants
and auditors in the language of the filing, and translating half of a financial
statement would be worse than translating none of it.

## Activity-based costing (v0.84.0)

Ten tools, six doctypes, no new control point. It answers the question a chart of
accounts cannot: **what did the Home Block cost per acre.**

Spend arrives labelled by supplier and by account — never by block. Dividing the
overhead account by total acreage is arithmetically fine and managerially
useless: it charges the mature Gala and the newly grafted replant the same spray
cost, when one was sprayed nine times and the other four. ABC's answer is a
two-step. Gather cost into a **pool** per **activity**; push each pool out to
blocks in proportion to how much of that activity each block actually consumed —
the **cost driver**.

```
GL Entry ──▶ Activity Cost Pool ──▶ ABC Cost Assignment ──▶ per acre, per phase
 (by account,     (by activity,          (by block,
  by cost centre)  one per year)          every intermediate stored)
```

### The register and the pools

| Tool | What it does |
| --- | --- |
| `create_cost_activity` | Define one thing the operation does that costs money, its driver and its phase. **Mutating** |
| `get_cost_activity` | One activity with its ledger scope, its accounts and every pool ever built for it |
| `list_cost_activities` | The register, counted by phase, naming the drivers nobody can derive |
| `update_cost_activity` | Change its type, phase, driver, scope or accounts. **Mutating** |
| `create_activity_cost_pool` | Gather one activity's cost for one year, off the ledger or by hand. **Mutating** |
| `list_activity_cost_pools` | Every pool for a company and year, with the ledger/manual split separated out |

### The engine and the reads

| Tool | What it does |
| --- | --- |
| `compute_abc_allocation` | Push every Ready pool out to the blocks that consumed it, and store the run whole. **Mutating**; `dry_run` writes nothing |
| `get_abc_assignment` | One stored run in full — every line with the driver quantity, share and acres behind it |
| `get_abc_report` | Per-acre cost grouped by field, activity or phase. **The primary management report** |
| `get_phase_waterfall` | Cost accumulating through Growing → Harvest → Post-Harvest → Packing → Sales |

### The engine will not estimate a driver

Two drivers are **derivable** from what the site already holds:

* **`Acres`** — each Field's acreage weighted by the days it was productive in
  the period, computed by the same code the Sustainable CF/Acre KPI uses so the
  two reports cannot grow separate opinions about what an acre is.
* **`Direct Assignment`** — the pool names the one block the cost was incurred
  for, which is the honest way to model a replant rather than inventing a driver
  to spread it.

Every other driver — hours, applications, bins, deliveries — is a **measurement
somebody took**. Supply it in `driver_quantities`, or the activity comes back
`UNALLOCATED` with its full amount and the sentence naming what would fix it. Its
money lands in `unassigned_amount`: not in the assigned total, and **not spread
evenly across blocks**. An even spread is indistinguishable in the output from a
measured one, and it is precisely the answer activity-based costing was adopted
to stop giving.

```json
{"activity": "Dormant spray", "cost_object": "FIELD-0001", "quantity": 42}
```

Supplying quantities for an `Acres` activity overrides the derived acreage, and
every line it touched says so in `driver_source`.

### The intermediates are stored, and that is the point

A per-acre cost is a quotient of two numbers that **both moved during the year**.
An operation keeping only the quotient can watch it rise for four seasons and
never learn whether the block got dearer or simply smaller. So every
`ABC Cost Assignment Line` carries the driver quantity, the share, the pool, the
amount assigned *and* the acres. Reruns **append** — the history of what this
operation believed its costs were is itself a record — and
`total_assigned + unassigned_amount = total_pool_amount` is stored on the
document so a reader can check the identity without rerunning the engine.

The **rounding residual is placed, not dropped**: on the largest consumer, where
it is proportionally smallest, so the lines add up to the pools exactly.

### `field` narrows the rows and never the arithmetic

Shares are always computed against every block that consumed the activity,
because a share computed against one block is 100% by construction. The stored
document holds the whole run for the same reason; the argument filters what comes
back.

### The denominator changes with the grouping

| `group_by` | Divided by |
| --- | --- |
| `field` | That block's **own** time-weighted productive acres |
| `activity` | The **whole operation's** productive acres |
| `phase` | The **whole operation's** productive acres |

"What did the Home Block cost me per acre" and "what did spraying cost me per
acre across the farm" are different numbers, and a reader who assumes the wrong
one is wrong by the ratio of one block to the farm. So `acres_basis` says which
was used on every single row.

A group with no productive acres reports `cost_per_acre` as **null, never zero** —
zero is a per-acre figure for a division nobody performed.

### The waterfall: read the shape, not the total

Cost does not land on a bin all at once. It accumulates as fruit moves through
five phases, and "where did this get expensive" is a question about the
accumulation rather than the sum — the total is available from any ledger, the
accumulation is not. Each stage reports what it **added** and what the fruit is
**carrying** as it leaves, per acre always and per unit when `units` is supplied.

**It will not invent a unit count.** With no `units`, the per-unit column is null
and the report says why — the same rule `get_absorption_cost_report` follows.

**A phase nothing is mapped to is reported at zero with a note**, not omitted. An
unmapped phase and a free one look identical in a total and are not the same
finding. Unallocated pool money is broken out **by phase**, so a reader can see
which stage of the pipeline is under-measured rather than only that something is.

### Two kinds of evidence, kept apart

A ledger pool is totalled off `GL Entry` over the activity's cost centre and
accounts and **itemised by account**, so the figure walks back to the books. The
scope is an **AND** and the trail is a **breakdown of it** — totalling each
filter independently would double-count every entry matching both and produce a
plausible pool whose evidence quietly disagreed with it, which the controller
refuses.

A manual pool is a legitimate figure and an entirely different kind of evidence.
That is why `amount_source` is a column rather than a footnote, and why
`list_activity_cost_pools` separates the two totals: nobody should have to take a
report's word for which is which.

A **negative** pool is refused — allocating it credits every block in proportion
to how much of the activity it consumed. A **zero** pool is stored: "this
activity cost nothing" and "nobody has computed this activity" are different
statements, and only one is worth acting on.

## Multilingual support and the shadow log (v0.85.0)

Six tools, two doctypes, one whitelisted mobile route. Two features that look
unrelated and share one property: **both are about somebody other than the
person doing the work being able to understand what happened.**

### The string register

| Tool | What it does |
| --- | --- |
| `list_translations` | What this site says, in which languages — and, with `missing_only`, which keys have no translation |
| `get_translation` | One key resolved into the caller's language, with every language's version and whether this answer fell back |
| `update_translation` | Write or correct one string in one language. **Mutating** |

`Farm Translation` is **not** Frappe's own `Translation` doctype and does not
replace it. Frappe's is keyed by the **source string**, which is right for
translating the framework's own UI and wrong for an app's, in three ways that all
fail silently:

* **Rewording the English orphans the Spanish.** Change "Bucket rejected —
  coverage too low" to "Bucket not counted — not full enough" and nothing errors;
  Spanish-speaking pickers just start seeing English.
* **One English word, two Spanish ones.** "Open" as a shift status is *abierto*;
  "Open" as a button is *abrir*. One source key cannot hold both.
* **"What is missing?" has no answer.** There is no register of *keys*, only of
  translations that happen to exist.

So a row is keyed by a stable dotted key — `shift.status.open`,
`error.task.already_done`, `task_type.harvest` — and the English is a row like
any other. The prefix is the grouping: `key_prefix='error.'` hands a translator
one afternoon's work.

**A missing translation serves English and says so.** Never a blank (a screen
nobody can act on), never the raw key (what a system shows when it has given
up), never a refusal (a crew locked out of a flow over one sentence). Every read
path reports what fell back, so the gap is findable from the Desk rather than
discoverable by somebody standing in front of a screen they cannot read.

### Whose language, and why `Accept-Language` loses

`Employee.preferred_language` is the authority. `Accept-Language` is consulted
**only where that column is empty**, and the ordering is a compliance position
rather than a preference: OSHA 1910.1200(h) and the Worker Protection Standard
(40 CFR 170.501) require hazard communication "in a manner the employee can
understand", and this app's claim to have done that rests on a column somebody
filled in about a person — not on a device setting. A phone set to English by
whoever handed it over says nothing about who is holding it now.

The header is honoured on an empty column because a site that has not filled it
in yet is better served by the phone's guess than by English. Every response says
which of the four decided — `explicit`, `employee`, `header`, `default` — so "why
is this worker seeing English" is answerable without reading the server.

`/mobile/get_translation_bundle` is what a handset pulls once at login instead of
asking for one label at a time, and mobile refusals now carry `error_key`,
`error_message` and `error_language` beside the unchanged English sentence. The
key is the contract; the string is the courtesy for a key the client's release
predates.

A wizard label of the form `tr:wizard.field.photo` is a **reference** resolved
through this register. Anything without the `tr:` prefix is a literal and behaves
exactly as it did before v0.85.0 — the per-wizard `label_en` / `label_es` columns
stay right for a string that belongs to one wizard. The prefix is for the string
that appears in nine, where nine copies drift and the ninth is the one nobody
fixed.

### The shadow log RACI feed

| Tool | What it does |
| --- | --- |
| `list_shadow_log_entries` | What went up the chain, by recipient, level, or whether it has been acknowledged |
| `get_shadow_log_entry` | One copy in full: the frozen snapshot, whether its hash still matches, whether the source still exists |
| `acknowledge_shadow_log` | Record that the recipient has seen it. **Mutating**, one-way, safe to retry |

Four events propagate: a **bucket session synced**, a **shift closed**, a
**compliance alert raised**, a **farm task completed**. Each writes one entry per
level of the chain above whoever the event was about.

```
Bucket session synced ──▶ level 1  direct supervisor   (Employee.reports_to)
                     ├──▶ level 2  their supervisor
                     └──▶ level 3  the one above that
        each carrying a FROZEN JSON snapshot, not a link
```

**This is not a notification list.** A notification tells you to go and look at a
record; a shadow copy carries the record's values *at that moment*, and the
recipient reads the copy. A supervisor who acknowledged a session of 412 buckets
acknowledged 412 — if a recount later makes it 380, that is a second fact and not
a silent rewrite of the first. The controller refuses any change to a snapshot
after insert, and `snapshot_hash` makes "frozen" checkable rather than merely
promised.

**It is also a backup.** Three levels hold three copies, and `source_doctype` /
`source_name` are `Data` rather than Links on purpose: a Dynamic Link would let a
delete cascade into the backup, which would make the backup worthless in exactly
the case it exists for. `get_shadow_log_entry` says plainly when the source has
gone and the snapshot is now the only record of it.

**The level is a distance, not a job title.** A crew whose `reports_to` chain is
two deep produces two copies, not three with the third addressed to whoever
happened to be around. A cycle in `reports_to` — two people reporting to each
other, which is a data-entry error somebody makes — stops the walk and is
reported rather than recursed. An event about the *operation* rather than about a
person (a stale water test, an uninspected cabin) has no chain to walk and goes
to whoever sits at the top of the company, at level 3 only.

**Propagation can never fail the work being filed.** Every call site wraps it;
what could not be written comes back under `shadow_log` on a result that
succeeded. That is the same trade `bridge_to_attendance` makes: the compliance
act is the thing being filed, and the convenience built on top of it does not get
to veto it.

`shadow_log_feed_enabled` in **ERPNext MCP Settings** switches the propagation
off for an operation that does not run a chain of command. It is a feature switch
and not a tool switch — the three reads stay usable for rows already written.

## Agricultural master data (v0.82.0)

Eleven tools, five doctypes and five child tables. The three registers everything
else in this app had been taking on trust from whoever was calling it: **what is
grown**, **where it is sold**, and **in what units**.

Before this release a spray check asking for a crop's pre-harvest interval, a
settlement asking what a bin weighed, and a breakeven asking what a market's
grades pay all got their answer from the caller. The site could not tell a
considered figure from a plausible one. Now each is a row somebody can read,
correct, and be held to.

### None of the three is company-scoped

Worth stating plainly, because `company` is accepted-and-reported-as-not-applied
on several tools in this app and here it is **not accepted at all**:

| Register | Why it belongs to no company |
| --- | --- |
| `Crop` | A species. A sweet cherry is a sweet cherry on both sides of a corporate boundary, and the days between bloom and harvest do not consult the deed |
| `Market` | A place in the world. Two growers shipping into the Pacific Northwest fresh cherry market are shipping into **one** market — per-company copies would give the site two answers to what a No. 1 is |
| `Agricultural UOM Context` / `Conversion` | A bin holds what it holds regardless of whose name is on it |

Per-company narrowing already exists on the layer where it belongs: a
**settlement** names both the market and the company.

### `Field.crop` stays free text

Nothing here turns it into a Link, and that is deliberate twice over. It has been
free text since it shipped on the stated grounds that "a block of table grapes is
not a schema change"; a Link beside it would give a site two answers to what
grows on a block with no rule for which wins. And a Link would make migrate
**order** load-bearing for every other feature that records a crop as a string.
The upgrade, if somebody wants it, is a patch that seeds `Crop` rows from the
distinct strings already on the site and only then changes the column — a release
of its own, not a field option.

### The crop tools

| Tool | What it does |
| --- | --- |
| `list_crops` | The register, varieties counted, plus the crops with no PHI, no harvest window, no varieties and no direct-marketed share |
| `get_crop` | One crop in full: varieties with rootstock and pollination group, water demand by growth stage, the markets that buy it, the conversions recorded for it |
| `create_crop` | Register one, with both child tables. **Mutating** |
| `update_crop` | Change one. Cannot re-key it. **Mutating** |
| `get_variety_care_recipe` | One variety's **resolved** water schedule — each Kc and weekly depth reconciled against the crop default per field and labelled with its source — plus its cultural practice protocol grouped by practice (v0.114.0) |

**A blank PHI is not a PHI of zero.** Zero means genuinely no interval; blank
means nobody has recorded one. They are reported apart at every level, because a
gate that conflates them clears fruit a label would hold. And every tool that
reports a PHI carries the caveat that the **binding** interval is the one printed
on the label of the material actually applied — on one crop that ranges from zero
days to thirty, and `default_phi_days` is only the floor for when nothing more
specific is known.

**Half a harvest window is refused; a wrapped one is not.** A start with no end
is a season nothing closes. But November to February is a real harvest, so the
obvious `start <= end` check is deliberately absent — that would be a rule about
integers wearing the costume of a rule about farming. `harvest_months` is
computed and wraps correctly, because a caller doing that subtraction itself gets
it wrong about half the time.

**A contradiction is refused; a judgement is reported.** `maturity_years` on a
variety of an *Annual* crop is refused — an annual has no non-bearing years to
capitalise, and both facts cannot be true. Every recorded variety sitting in one
pollination incompatibility group is only **reported**: they will not set fruit
for each other and the block finds out four years later, but the pollinizer may
be in a neighbouring block or simply unrecorded here.

### The market tools

| Tool | What it does |
| --- | --- |
| `list_markets` | The register, grades counted, plus the active markets with no grades and the ones with no USDA shipping point |
| `get_market` | One market, its grade ladder sorted by what each grade pays, and the premium spread across it |
| `create_market` | Register one with its grade standards. **Mutating** |
| `update_market` | Change or retire one. Cannot re-key it. **Mutating** |

**`active_without_grade_standards` is the list to read first.** A market with no
grades has no packout assumption behind it, so any breakeven quoting it is
quoting a number somebody typed rather than a standard the market enforces.

**`premium_spread_pct` is why a packout forecast is worth making.** It is the
distance between the best and worst grade a market pays. Where the spread is
narrow, an error in the projected split costs little; where it is wide, the split
is the largest single assumption in a breakeven.

**Sizes are millimetres, not row sizes.** Cherries trade by row and apples by
count per box, and both are *inverse* scales where the bigger fruit carries the
smaller number — a column holding those would sort backwards in every report.
Convert once; the sales desk keeps speaking rows.

**A negative premium is normal.** Juice against fresh, orchard run against fancy.
Bounded below at -100%, past which it is a sign error. A defect tolerance outside
0–100 is refused outright: stored, it would make every tolerance comparison pass.

**`shipping_point` is a join key, not a description.** USDA Market News publishes
daily terminal and shipping-point prices keyed on that exact string and on
nothing else.

### Units, and why ERPNext's own conversion table is not enough

| Tool | What it does |
| --- | --- |
| `list_ag_uom_contexts` | Which units are valid for which work, and the default for each |
| `get_uom_conversions` | How many of one unit are in another, for a given crop, and where the number came from |

ERPNext's `UOM Conversion Factor` holds **one global factor per unit pair**. A
bin of cherries is about 800 lb and a bin of apples about 900, so entering both
overwrites one with the other and entering either makes the site quietly wrong
about the other crop. Hence a register where **the crop is part of the key**.

```
                    resolution order
crop-specific row  ─┐
generic row        ─┼─►  factor, plus WHICH of these produced it
inverted row       ─┤
one-hop chain      ─┘
```

`get_uom_conversions` **resolves rather than looks up**. It will invert a row
recorded the other way round — "one bin is 800 lb" and "one lb is a 800th of a
bin" are the same fact, and requiring both to be entered would require an
operator to keep two rows in step by hand. It will chain through **one**
intermediate unit (bins → pounds → tons) and no more: past that it is multiplying
three nominal figures together and the compounding error exceeds the answer's
worth. A chain reports the **weaker** of its two bases, because an exact hop
composed with a nominal one is nominal.

**`basis` is not bookkeeping.** `Exact` is a definition — 128 fluid ounces to a
gallon, 43,560 square feet to an acre — and cannot carry a crop, because a
quantity that varies by fruit is not a definition. `Nominal` is the trade's rule
of thumb: right enough to plan with, not right enough to settle a dispute with.
`Operation Average` is the farm's own weighed figure, it must cite a source, and
it wins every lookup. A shrink dispute turns on whether the weight was defined,
assumed, or weighed.

**Three different refusals, because they need three different actions.** No row
at all; no *active* row (a superseded factor is kept switched off so last
season's settlements stay explicable, and is not consulted); or rows for other
crops and none for this one — which is correct rather than missing. Guessing
there is how a settlement goes wrong by a factor nobody traces.

**Harvest and Scale Ticket are two contexts, not one list.** A bin is a
*container* and a pound is a *weight*. A field crew hands in bins and the shed
reports pounds — two measurements of one delivery, and a single list accepting
either is a list that lets them be summed.

### What is seeded, and what that is worth

Install and every migrate lay down a starting book: three crops (Sweet Cherry,
Apple, Pear) with their varieties, rootstocks, pollination groups and water
demand by growth stage; three markets with grade ladders; four unit contexts; and
the conversions between them. It only ever creates what is not there, checked by
docname, so an operator's own figure is never overwritten and a deleted record is
never resurrected.

**The numbers are a starting book, not your farm's.** Every conversion but the
three definitions is `Nominal`, the yields are expectations, and the grade
premiums are illustrative *shapes* rather than this season's prices. An operation
that leaves them untouched and quotes them at a lender has misused them —
`list_markets` reports which markets still carry no reviewed grades precisely so
the ones nobody looked at stay visible.

On a Frappe bench with no ERPNext there is no `UOM` master, so the units and the
conversions that link to them are skipped **by name** while the crops and markets,
which link to neither, are seeded anyway.
---

# Breakeven calculator (v0.87.0)

*What price does this crop have to make?* Five tools over one record, and the
record is a **perspective on the ledger the farm already keeps**. Nothing here
posts, nothing is a journal entry, and no number it produces changes what the
financial statements say. What a Breakeven Analysis adds to the chart of accounts
is the one thing a chart of accounts cannot hold: which accounts stop mattering
when the crop gets bigger.

| Tool | What it does |
| --- | --- |
| `create_breakeven_analysis` | Register a crop, a volume and a price to model. Creates; does **not** compute |
| `compute_breakeven` | Read the expense accounts, classify them, answer the question — and store every intermediate |
| `get_breakeven_analysis` | One analysis in full, with every cost line and **who classified it** |
| `list_breakeven_analyses` | The register, naming the rows that are not answers |
| `get_breakeven_sensitivity` | What-if over one variable across a range. Stores nothing |

## A fruit farm has two volumes, and everything follows from that

A textbook breakeven has one. Picking, hauling and field bins are bought for
**everything that comes off the trees**; cartons, packing labour, freight and
commission are bought only for **what packs out**. So when packout falls from
85% to 60%, the second pile falls with it and the first does not — it spreads
over fewer sellable boxes. Every cost line therefore carries a `volume_basis`,
and the arithmetic is:

```
variable cost per sellable unit  =  vh / p  +  vs
cull credit per sellable unit    =  c · (1 − p) / p
contribution margin per unit     =  P + cull credit − variable cost
```

Each packed box carries `1/p` harvested units of picking with it and brings
`(1−p)/p` culls' worth of juice money along. A model with a **single** variable
pile gets the direction of a packout change right and the magnitude wrong, which
is the more dangerous of the two errors: it looks like an answer.

The **cull credit is not decoration**. Thirty percent culls at juice price is not
thirty percent of the crop earning nothing, and a model that treated it that way
would make every light-packout scenario look worse than it is — the direction
that leaves fruit on the tree that should have been picked.

## The packout slider is an argument to `compute_breakeven`

```json
{"name": "Gala 2026 - ETC", "packout_pct": 62}
```

One call, one number. The analysis is recomputed at that packout and keeps it, so
the record always says which packout its stored results answer. `breakeven_packout_pct`
is the same question read backwards — *"we need 74% out of this block"* is a
target a packhouse can be given, and a breakeven revenue is not.

## Every line says who classified it

Three sources, best evidence first, stored on every line:

* **Account** — an operator classified the account itself with
  `breakeven_cost_behavior`, installed on `Account` as a Custom Field. Said once,
  true for every analysis afterwards.
* **Override** — this analysis was told to treat one account differently, once,
  and nothing was written to the Account.
* **Heuristic** — nobody has said, so the account's name and ERPNext type were
  read and a guess was made.

The heuristic is genuinely useful *and* genuinely a guess, and the design turns
on holding both at once. A first run on a real chart of accounts classifies most
of it correctly and hands back a number in a minute rather than an afternoon —
and reports how many lines it guessed at, in the result, in
`computation_warnings`, and again on every read. **A breakeven resting on forty
guessed classifications is a different object from one resting on none**, and the
person about to quote it to a lender is entitled to know which they have.

An override naming an account that is not an expense account of the company is
**refused**, not ignored: a dropped override leaves the account being guessed at
while the caller believes they classified it, and the result would look identical.

**Income tax is excluded by rule, not by heuristic.** At breakeven there is no
pre-tax income to tax, so a model carrying the tax line would demand the farm
cover a liability it does not have.

## What the reads refuse to do

**No breakeven quantity where the contribution margin is not positive.** There is
no such quantity — every additional box loses money — and the arithmetic limit is
an enormous number that reads as a hard target. `breakeven_units` comes back
`null` and the reason is stated. The breakeven **price** is still reported, and
there it is the number that matters: it says how far the price has to come up
before volume helps at all.

**No conversion between packages on the market overlay.** A breakeven per 40-lb
box compared against a USDA quotation per 20-lb carton is out by a factor of two
and looks entirely plausible. Both packages are reported next to the spread and
neither is converted, because pack style is a judgement this app has no basis to
make.

**No writing from a read.** `get_breakeven_sensitivity` answers whatever band it
is asked for and stores nothing; `compute_breakeven` stores a standard ±10/±20%
band across all five variables. What is in the register depends on who ran a
computation, not on who was browsing.

**An edited input goes stale rather than recomputing.** Change the price in the
Desk and the record flips to `Stale` with its old results intact. A breakeven
that changed underneath the person reading it, keeping the same `computed_on`, is
worse than one that says it is out of date.

## The market overlay reads a register, not the internet

USDA AMS Market News publishes shipping point prices — what a district was asking
f.o.b. — and every quotation this app sees is kept as a `USDA Price Quote`. The
overlay reads **the register**, which is what makes it work in a farm office
whose link is down and on a site that has never configured an API key at all. A
grower with a broker's bid in hand has a better number than any district average,
and `compute_breakeven(market_price=…)` stores it as its own labelled kind of
source.

Fetching is opt-in twice over: a MARS API key in settings, and either
`refresh_usda_prices` with a report slug or the nightly sweep, which **ships
off**. No report slugs are shipped — AMS identifies reports by slugs an operator
looks up for the districts they actually ship into, and a list invented here
would be right for one region and a nightly 404 everywhere else.

A quotation belongs to **no company**. A shipping point price is a fact about a
market, not about anybody's operation; the records that carry the operation are
the analyses that read it, and every one of those links to Company and is scoped
by Frappe exactly as before.

## Agricultural master data (v0.82.0)

Eleven tools, five doctypes and five child tables. The three registers everything
else in this app had been taking on trust from whoever was calling it: **what is
grown**, **where it is sold**, and **in what units**.

Before this release a spray check asking for a crop's pre-harvest interval, a
settlement asking what a bin weighed, and a breakeven asking what a market's
grades pay all got their answer from the caller. The site could not tell a
considered figure from a plausible one. Now each is a row somebody can read,
correct, and be held to.

### None of the three is company-scoped

Worth stating plainly, because `company` is accepted-and-reported-as-not-applied
on several tools in this app and here it is **not accepted at all**:

| Register | Why it belongs to no company |
| --- | --- |
| `Crop` | A species. A sweet cherry is a sweet cherry on both sides of a corporate boundary, and the days between bloom and harvest do not consult the deed |
| `Market` | A place in the world. Two growers shipping into the Pacific Northwest fresh cherry market are shipping into **one** market — per-company copies would give the site two answers to what a No. 1 is |
| `Agricultural UOM Context` / `Conversion` | A bin holds what it holds regardless of whose name is on it |

Per-company narrowing already exists on the layer where it belongs: a
**settlement** names both the market and the company.

### `Field.crop` stays free text

Nothing here turns it into a Link, and that is deliberate twice over. It has been
free text since it shipped on the stated grounds that "a block of table grapes is
not a schema change"; a Link beside it would give a site two answers to what
grows on a block with no rule for which wins. And a Link would make migrate
**order** load-bearing for every other feature that records a crop as a string.
The upgrade, if somebody wants it, is a patch that seeds `Crop` rows from the
distinct strings already on the site and only then changes the column — a release
of its own, not a field option.

### The crop tools

| Tool | What it does |
| --- | --- |
| `list_crops` | The register, varieties counted, plus the crops with no PHI, no harvest window, no varieties and no direct-marketed share |
| `get_crop` | One crop in full: varieties with rootstock and pollination group, water demand by growth stage, the markets that buy it, the conversions recorded for it |
| `create_crop` | Register one, with both child tables. **Mutating** |
| `update_crop` | Change one. Cannot re-key it. **Mutating** |
| `get_variety_care_recipe` | One variety's **resolved** water schedule — each Kc and weekly depth reconciled against the crop default per field and labelled with its source — plus its cultural practice protocol grouped by practice (v0.114.0) |

**A blank PHI is not a PHI of zero.** Zero means genuinely no interval; blank
means nobody has recorded one. They are reported apart at every level, because a
gate that conflates them clears fruit a label would hold. And every tool that
reports a PHI carries the caveat that the **binding** interval is the one printed
on the label of the material actually applied — on one crop that ranges from zero
days to thirty, and `default_phi_days` is only the floor for when nothing more
specific is known.

**Half a harvest window is refused; a wrapped one is not.** A start with no end
is a season nothing closes. But November to February is a real harvest, so the
obvious `start <= end` check is deliberately absent — that would be a rule about
integers wearing the costume of a rule about farming. `harvest_months` is
computed and wraps correctly, because a caller doing that subtraction itself gets
it wrong about half the time.

**A contradiction is refused; a judgement is reported.** `maturity_years` on a
variety of an *Annual* crop is refused — an annual has no non-bearing years to
capitalise, and both facts cannot be true. Every recorded variety sitting in one
pollination incompatibility group is only **reported**: they will not set fruit
for each other and the block finds out four years later, but the pollinizer may
be in a neighbouring block or simply unrecorded here.

### The variety overlays (v0.114.0)

Two child tables hang off `Crop` and carry the facts that are true of one
**variety** rather than of the species: `Crop Variety Water Requirement` and
`Crop Variety Protocol`.

Both hang off the crop and name their variety as a text column, because Frappe
has no nested child tables and `Crop Variety` is itself a child — a table on the
variety row is not a thing that can exist. `crop.py` checks that name against the
crop's own catalogue on save, which is the rule that matters: an override naming
a variety the catalogue does not list stores perfectly well, resolves to nothing,
and leaves a form showing what looks like a recorded decision. Invisible from
both ends, so it is refused.

**They are sparse overlays, and the fallback is per field.** A row exists only
where a variety departs from its crop; every stage nobody overrode falls back to
the crop's own figure. A row that overrides only the Kc leaves the crop's weekly
depth standing — resolving per *row* instead is the mistake `get_variety_care_recipe`
exists to stop a caller making, and it discards a real number every time.
Demanding all seven stages per variety would be asking a farm to restate figures
that were already right, and the restatements are what drift.

**Blank is not zero, here most of all.** An empty Kc on an override is a variety
with no opinion about Kc. `0.0` is a variety that genuinely takes no water at
that stage. An override carrying neither number is refused rather than stored,
because it changes nothing and nothing would ever show that.

**A protocol step is a plan, not a record, and not a label.** GA timings, PGR
programs, thinning and pruning — what the farm intends for the variety in a
normal year. What actually went onto a block is a `Spray Application`. One row is
one step, so a GA program of three applications is three rows and keeps its
schedule where something can read it; the uniqueness rule is deliberately *not*
on (variety, practice), which would refuse the commonest real recipe in the file.
Rates are text with their units, because ppm, pints per acre and quarts per
hundred gallons do not convert without knowing the dilution.

### The rootstock moved to the planting (v0.114.0)

`Crop Variety.rootstock` is one column on a catalogue with one row per variety,
so it can hold exactly one rootstock for `Bing` while the farm has Bing on
Mazzard in the old block and Bing on Gisela 6 in the 2019 planting. The rootstock
is half the tree — vigour, final size, density, how soon the block bears, how it
takes wet ground — so those are different trees, and a per-acre yield quoted
against the wrong one is not comparable to anything.

The block-level answer already existed: `Planting Season.rootstock` for a
block-year and `Field.rootstock` for the block. What was missing was anything
saying so. The catalogue column is now labelled a **catalogue default**, every
payload that reports it carries a caveat naming the two columns that bind, and
the `backfill_planting_rootstock` patch carries the catalogue value down onto
every planting that recorded none.

The patch **only ever fills a blank**. A planting that already names a rootstock
was typed against that block and is the better record by construction, so it is
never rewritten and never compared — no "which is right" question is raised. It
is a seed and not a sync: after it runs the two columns are free to disagree, and
they should.

### The market tools

| Tool | What it does |
| --- | --- |
| `list_markets` | The register, grades counted, plus the active markets with no grades and the ones with no USDA shipping point |
| `get_market` | One market, its grade ladder sorted by what each grade pays, and the premium spread across it |
| `create_market` | Register one with its grade standards. **Mutating** |
| `update_market` | Change or retire one. Cannot re-key it. **Mutating** |

**`active_without_grade_standards` is the list to read first.** A market with no
grades has no packout assumption behind it, so any breakeven quoting it is
quoting a number somebody typed rather than a standard the market enforces.

**`premium_spread_pct` is why a packout forecast is worth making.** It is the
distance between the best and worst grade a market pays. Where the spread is
narrow, an error in the projected split costs little; where it is wide, the split
is the largest single assumption in a breakeven.

**Sizes are millimetres, not row sizes.** Cherries trade by row and apples by
count per box, and both are *inverse* scales where the bigger fruit carries the
smaller number — a column holding those would sort backwards in every report.
Convert once; the sales desk keeps speaking rows.

**A negative premium is normal.** Juice against fresh, orchard run against fancy.
Bounded below at -100%, past which it is a sign error. A defect tolerance outside
0–100 is refused outright: stored, it would make every tolerance comparison pass.

**`shipping_point` is a join key, not a description.** USDA Market News publishes
daily terminal and shipping-point prices keyed on that exact string and on
nothing else.

### Units, and why ERPNext's own conversion table is not enough

| Tool | What it does |
| --- | --- |
| `list_ag_uom_contexts` | Which units are valid for which work, and the default for each |
| `get_uom_conversions` | How many of one unit are in another, for a given crop, and where the number came from |

ERPNext's `UOM Conversion Factor` holds **one global factor per unit pair**. A
bin of cherries is about 800 lb and a bin of apples about 900, so entering both
overwrites one with the other and entering either makes the site quietly wrong
about the other crop. Hence a register where **the crop is part of the key**.

```
                    resolution order
crop-specific row  ─┐
generic row        ─┼─►  factor, plus WHICH of these produced it
inverted row       ─┤
one-hop chain      ─┘
```

`get_uom_conversions` **resolves rather than looks up**. It will invert a row
recorded the other way round — "one bin is 800 lb" and "one lb is a 800th of a
bin" are the same fact, and requiring both to be entered would require an
operator to keep two rows in step by hand. It will chain through **one**
intermediate unit (bins → pounds → tons) and no more: past that it is multiplying
three nominal figures together and the compounding error exceeds the answer's
worth. A chain reports the **weaker** of its two bases, because an exact hop
composed with a nominal one is nominal.

**`basis` is not bookkeeping.** `Exact` is a definition — 128 fluid ounces to a
gallon, 43,560 square feet to an acre — and cannot carry a crop, because a
quantity that varies by fruit is not a definition. `Nominal` is the trade's rule
of thumb: right enough to plan with, not right enough to settle a dispute with.
`Operation Average` is the farm's own weighed figure, it must cite a source, and
it wins every lookup. A shrink dispute turns on whether the weight was defined,
assumed, or weighed.

**Three different refusals, because they need three different actions.** No row
at all; no *active* row (a superseded factor is kept switched off so last
season's settlements stay explicable, and is not consulted); or rows for other
crops and none for this one — which is correct rather than missing. Guessing
there is how a settlement goes wrong by a factor nobody traces.

**Harvest and Scale Ticket are two contexts, not one list.** A bin is a
*container* and a pound is a *weight*. A field crew hands in bins and the shed
reports pounds — two measurements of one delivery, and a single list accepting
either is a list that lets them be summed.

### What is seeded, and what that is worth

Install and every migrate lay down a starting book: three crops (Sweet Cherry,
Apple, Pear) with their varieties, rootstocks, pollination groups and water
demand by growth stage; three markets with grade ladders; four unit contexts; and
the conversions between them. It only ever creates what is not there, checked by
docname, so an operator's own figure is never overwritten and a deleted record is
never resurrected.

**The numbers are a starting book, not your farm's.** Every conversion but the
three definitions is `Nominal`, the yields are expectations, and the grade
premiums are illustrative *shapes* rather than this season's prices. An operation
that leaves them untouched and quotes them at a lender has misused them —
`list_markets` reports which markets still carry no reviewed grades precisely so
the ones nobody looked at stay visible.

On a Frappe bench with no ERPNext there is no `UOM` master, so the units and the
conversions that link to them are skipped **by name** while the crops and markets,
which link to neither, are seeded anyway.
---

# Breakeven calculator (v0.87.0)

*What price does this crop have to make?* Five tools over one record, and the
record is a **perspective on the ledger the farm already keeps**. Nothing here
posts, nothing is a journal entry, and no number it produces changes what the
financial statements say. What a Breakeven Analysis adds to the chart of accounts
is the one thing a chart of accounts cannot hold: which accounts stop mattering
when the crop gets bigger.

| Tool | What it does |
| --- | --- |
| `create_breakeven_analysis` | Register a crop, a volume and a price to model. Creates; does **not** compute |
| `compute_breakeven` | Read the expense accounts, classify them, answer the question — and store every intermediate |
| `get_breakeven_analysis` | One analysis in full, with every cost line and **who classified it** |
| `list_breakeven_analyses` | The register, naming the rows that are not answers |
| `get_breakeven_sensitivity` | What-if over one variable across a range. Stores nothing |

## A fruit farm has two volumes, and everything follows from that

A textbook breakeven has one. Picking, hauling and field bins are bought for
**everything that comes off the trees**; cartons, packing labour, freight and
commission are bought only for **what packs out**. So when packout falls from
85% to 60%, the second pile falls with it and the first does not — it spreads
over fewer sellable boxes. Every cost line therefore carries a `volume_basis`,
and the arithmetic is:

```
variable cost per sellable unit  =  vh / p  +  vs
cull credit per sellable unit    =  c · (1 − p) / p
contribution margin per unit     =  P + cull credit − variable cost
```

Each packed box carries `1/p` harvested units of picking with it and brings
`(1−p)/p` culls' worth of juice money along. A model with a **single** variable
pile gets the direction of a packout change right and the magnitude wrong, which
is the more dangerous of the two errors: it looks like an answer.

The **cull credit is not decoration**. Thirty percent culls at juice price is not
thirty percent of the crop earning nothing, and a model that treated it that way
would make every light-packout scenario look worse than it is — the direction
that leaves fruit on the tree that should have been picked.

## The packout slider is an argument to `compute_breakeven`

```json
{"name": "Gala 2026 - ETC", "packout_pct": 62}
```

One call, one number. The analysis is recomputed at that packout and keeps it, so
the record always says which packout its stored results answer. `breakeven_packout_pct`
is the same question read backwards — *"we need 74% out of this block"* is a
target a packhouse can be given, and a breakeven revenue is not.

## Every line says who classified it

Three sources, best evidence first, stored on every line:

* **Account** — an operator classified the account itself with
  `breakeven_cost_behavior`, installed on `Account` as a Custom Field. Said once,
  true for every analysis afterwards.
* **Override** — this analysis was told to treat one account differently, once,
  and nothing was written to the Account.
* **Heuristic** — nobody has said, so the account's name and ERPNext type were
  read and a guess was made.

The heuristic is genuinely useful *and* genuinely a guess, and the design turns
on holding both at once. A first run on a real chart of accounts classifies most
of it correctly and hands back a number in a minute rather than an afternoon —
and reports how many lines it guessed at, in the result, in
`computation_warnings`, and again on every read. **A breakeven resting on forty
guessed classifications is a different object from one resting on none**, and the
person about to quote it to a lender is entitled to know which they have.

An override naming an account that is not an expense account of the company is
**refused**, not ignored: a dropped override leaves the account being guessed at
while the caller believes they classified it, and the result would look identical.

**Income tax is excluded by rule, not by heuristic.** At breakeven there is no
pre-tax income to tax, so a model carrying the tax line would demand the farm
cover a liability it does not have.

## What the reads refuse to do

**No breakeven quantity where the contribution margin is not positive.** There is
no such quantity — every additional box loses money — and the arithmetic limit is
an enormous number that reads as a hard target. `breakeven_units` comes back
`null` and the reason is stated. The breakeven **price** is still reported, and
there it is the number that matters: it says how far the price has to come up
before volume helps at all.

**No conversion between packages on the market overlay.** A breakeven per 40-lb
box compared against a USDA quotation per 20-lb carton is out by a factor of two
and looks entirely plausible. Both packages are reported next to the spread and
neither is converted, because pack style is a judgement this app has no basis to
make.

**No writing from a read.** `get_breakeven_sensitivity` answers whatever band it
is asked for and stores nothing; `compute_breakeven` stores a standard ±10/±20%
band across all five variables. What is in the register depends on who ran a
computation, not on who was browsing.

**An edited input goes stale rather than recomputing.** Change the price in the
Desk and the record flips to `Stale` with its old results intact. A breakeven
that changed underneath the person reading it, keeping the same `computed_on`, is
worse than one that says it is out of date.

## The market overlay reads a register, not the internet

USDA AMS Market News publishes shipping point prices — what a district was asking
f.o.b. — and every quotation this app sees is kept as a `USDA Price Quote`. The
overlay reads **the register**, which is what makes it work in a farm office
whose link is down and on a site that has never configured an API key at all. A
grower with a broker's bid in hand has a better number than any district average,
and `compute_breakeven(market_price=…)` stores it as its own labelled kind of
source.

Fetching is opt-in twice over: a MARS API key in settings, and either
`refresh_usda_prices` with a report slug or the nightly sweep, which **ships
off**. No report slugs are shipped — AMS identifies reports by slugs an operator
looks up for the districts they actually ship into, and a list invented here
would be right for one region and a nightly 404 everywhere else.

A quotation belongs to **no company**. A shipping point price is a fact about a
market, not about anybody's operation; the records that carry the operation are
the analyses that read it, and every one of those links to Company and is scoped
by Frappe exactly as before.

## The spray program, crop protection and the block's own life (v0.88.0)

Three features that are one chain read end to end: a scout finds something, the
threshold engine decides whether it is worth answering, the answer that gets
chosen is often a spray, the spray shuts a block for a number of hours, and every
hour and every gallon of it lands against the ground that consumed it.

### The spray program

`Spray Tank Mix` is the **recipe** and `Spray Application` is the **event**.
Keeping them apart is what lets a farm approve one mix in March and file forty
applications against it without forty chances to retype a rate.

#### `create_spray_nozzle_config` / `list_spray_nozzle_configs`

One nozzle set as it is actually plumbed on a boom or an air-blast tower.

| Argument | Meaning |
| --- | --- |
| `nozzle_name` | What the crew calls it — the docname every application points at |
| `flow_rate_gpm` | Gallons per minute **per nozzle**, off the manufacturer's chart |
| `nozzle_type`, `pattern` | `Canopy Upper` / `Canopy Lower` are the halves of an air-blast tower |
| `droplet_class` | ASABE S572 — what a label's drift language names |
| `rated_pressure_psi`, `spacing_inches`, `nozzles_active`, `boom_width_ft` | |

**Flow is per nozzle and is never silently multiplied.** A record holding boom
flow is one nobody can check against the chart in their hand at the machine, and
the only way to find the error would be to spray at twice the rate.

#### `create_spray_tank_mix` — MUTATING, default off

Several products in one tank, **each at its own rate per acre**.

| Argument | Meaning |
| --- | --- |
| `mix_name` | The docname |
| `products` | `[{item_code, rate_per_acre, rate_uom, nozzle_set, target}]` |
| `dual_nozzle`, `nozzle_set_a`, `nozzle_set_b` | Two sets flipped mid-pass |
| `tank_size_gal`, `carrier_gpa` | Together they give acres per tank |
| `crop`, `target_pest`, `season_year`, `status`, `company`, `notes` | |

**Per-product rates, not one rate for the tank.** A cover spray is two or three
answers to two or three different problems, and only a per-product rate can be
checked against a label.

**The longest interval in the tank wins**, computed on save, for both REI and
PHI. Each product's label numbers are **copied off its Item at mix time** — a mix
filed in April and read in a hearing in November has to say what the label said
in April, and a live join says what it says now.

**A dual mix with nothing on one side is refused.** The flag's whole purpose is
that an application will ask which set was running where; a mix that puts every
product on `Both` cannot answer, and is a single-set mix ticked by accident.

#### `create_spray_application` — MUTATING, default off

What went out, over which blocks, when, by whom, **in what weather** — and the
restricted-entry windows it opens.

| Argument | Meaning |
| --- | --- |
| `blocks` | Names, or `[{block, acres, nozzle_set_used, completed_at}]` |
| `tank_mix` | The recipe. Its products are **copied onto** the record |
| `products` | Overrides the mix, for a tank mixed at the machine |
| `completed_at` | When the pass **finished** — every window runs from here |
| `status` | `Applied` (default), `Planned`, `Cancelled` |
| `applicator_license` | One of the first things a state inspection asks for |
| `ground_speed_mph` | With the nozzle's flow and spacing, gives gallons per acre |
| `flip_performed`, `flip_at` | Whether the flip actually happened |
| `wind_speed_mph`, `wind_direction`, `temperature_f`, `humidity_pct`, `weather_source`, `weather_recorded_at` | |
| `rei_hours` | Overrides every label. Omit to record a spray that restricts nobody |

**It does not refuse a tank with no label interval, and that is the one
deliberate difference from `record_spray_application`.** A tank of foliar
nitrogen restricts nobody and is still a real pass over real acres: it is
recorded, **zero** `Spray REI` records are created, and the response says so.
`record_spray_application` refuses in that case because its entire purpose *is*
the window — and a zero-hour window reads as *this block is clear*.

**One window per block, from that block's own completion time.** A block sprayed
at eight in the morning does not stay shut because the last block of the pass
finished at two. `get_active_rei` still answers every question about restricted
entry; nothing here re-answers it.

**Weather is recorded, never enforced.** Wind above the label window is drift;
wind **below** it is a temperature inversion, which is the half people forget.
Both earn an advisory written onto the record. A refusal would not prevent the
spray — it went out three hours ago — only the record of it.

**Calibration is arithmetic, not a typed number.** `gallons_per_acre` is
`(GPM × 5940) / (mph × spacing)`; 5940 is square inches per acre over inches
travelled per minute at 1 mph, spelled out in the constants so the one figure an
inspector recomputes by hand can be checked.

#### `list_spray_applications` / `get_spray_application`

`applications_without_wind_recorded` is the point of the list: wind at the time
of application is the most asked-for line on a state record and it cannot be
reconstructed afterwards. `get_spray_application` reports which blocks are
**still restricted right now**, read through the `Spray REI` register rather than
recomputed, so it stays correct after a window is cancelled and on a bench whose
scheduler has stopped.

#### `get_spray_application_report`

**Read-only.** Chemical usage over a period, grouped by **product** and then by
**block**: total quantity per product, which blocks received it, on what dates,
by which applicator, and the label intervals it carried. Takes `company`
(required), `date_from`/`date_to` (defaulting to the calendar year to date),
`block` and `product`.

**This is the pesticide use report.** Oregon's PARC reporting, California's
monthly PUR and Washington's WPS records all ask a version of the same question,
and all of them want it summed by product and located on the ground.
`list_spray_applications` answers *what did we spray on the 14th*; this answers
*how much captan went onto Block 7 this season*.

**The per-block quantity is rate × that block's acres**, never the tank total
spread evenly — the same doctrine `get_active_rei` applies to restrictions, for
the same reason. What a regulator asks about a block is what went onto that
ground, and an even split across blocks of unequal size is a number that was
never true of any of them.

**Quantities are summed per unit and never across units.** A product recorded at
lb/acre on one pass and qt/acre on another has two totals here, not one, and
`mixed_unit_products` names it. Adding pounds to quarts needs a density this app
does not have, and a wrong total on a use report is the kind of wrong an
inspector finds rather than an auditor.

**Applied only.** A planned application is a plan and a cancelled one did not
happen; both are counted in `excluded` so the report cannot be mistaken for the
whole register. An application naming no block is reported against
`(no block recorded)` using its own acres rather than dropped — the product total
stays right, and `applications_without_blocks` says the map is not.

### Crop protection: observation → pressure → IPM

Six threat categories, fixed because they are what a programme reports against:
**Insect, Disease, Weed, Vertebrate, Abiotic, Nutrient**. Abiotic covers frost,
hail and sunburn — damage with no organism behind it, which every pest register
leaves out and every grower has to record anyway.

#### `set_pest_action_threshold` / `list_pest_action_thresholds`

| Argument | Meaning |
| --- | --- |
| `crop`, `threat`, `threat_category` | Matched case-insensitively |
| `action_threshold` | Cross it and a recommendation is generated |
| `comparison` | `Greater Than` (default) … or `Less Than` for a **floor** |
| `warning_threshold` | The early number. Must arrive *first* |
| `crop_stage` | Blank means every stage, and is the fallback |
| `sample_unit` | An observation in a different unit is **not** evaluated |
| `beneficial_ratio_min` | Predators at or above this ⇒ no control recommended |
| `min_sample_size` | Below it, nothing is generated |
| `recommended_methods` | `Method: action`, one per line — what makes it actionable |

**The comparison is a column, not an assumption.** A Nutrient threshold fires
*below* its number — tissue nitrogen under 2.2 % is the finding. A hard-coded
greater-than would fire on every healthy block and never on a deficient one:
wrong in both directions at once, and silently.

**Revising retires the old row rather than editing it.** Every observation
already evaluated points at the row that evaluated it, and an edit in July would
rewrite what June's scouting was measured against.

#### `create_crop_observation` — MUTATING, default off

One call records the observation, moves the block's `Pest Pressure` for the
season, and generates an `IPM Recommendation` where the action threshold was
crossed. A programme that needs three records created in the right order gets one
created and abandoned.

**Beneficials override the threshold, and this is the point of the pipeline.** A
count over threshold with predators present at the threshold's ratio generates
*hold and re-scout* **instead of** a control — the block is already handling
itself, and the spray that fixes it kills the predators and guarantees a worse
flare in three weeks. The hold **replaces** the control options rather than
joining them: presenting both is presenting no recommendation at all.

**A sample too small to say generates nothing.** Two infested leaves out of five
is not twenty per cent infestation. The observation is recorded in full and the
pressure still moves; only the recommendation is withheld.

**No threshold on file is an answer, not an error.** It is the ordinary state of
a first season, and it is named as a gap — the observations already on file are
how you find out what the number should be.

#### `index_scouting_observations` — MUTATING, default off, idempotent (v0.115.0)

The second door onto the same register, and the one nobody types into. A scouting
task's completion **already is** an observation — a growth stage, a Brix reading,
a photograph and a coordinate, filed against a Farm Task Assignment. Until
v0.115.0 all of that stayed on the assignment and the Crop Observation register
had nothing in it, so the round was worked, evidenced and paid for and the map
had nothing to colour.

```json
{"date_from": "2026-08-17", "date_to": "2026-08-23"}
```

Over one window of **completion dates** every assignment closed in it whose task
produces a `Crop Observation` becomes one. It reads two places, because they
answer different questions:

| From the task's `creates_record_data` | From the assignment |
| --- | --- |
| `observation_type`, `growth_stage_code`, `brix_reading`, `brix_method` | the location fix (`farm_location_gps`) |
| `threat`, `threat_category`, `count_observed`, `sample_size` | the first photograph filed as evidence |
| `crop`, `crop_stage`, `sample_unit`, `severity`, `beneficials_observed` | the worker's own findings, and when it was finished |

The task's half is the template's defaults with the completion's own
`record_data` merged over the top, stamped at the moment of completion — so a
template edited next month cannot change what a round already walked said.
Reading only the task would file an observation with no photograph and no
coordinate; reading only the assignment would file one with no Brix.

**The threshold engine runs on a Pest Scout and only on a Pest Scout.** Where the
round named a threat, `evaluate_against_threshold` / `run_downstream` are the same
functions `create_crop_observation` calls, so an observation is evaluated
identically whichever door it came through. A Harvest Readiness round is a Brix
and a stage with no organism in it, and evaluating one would either find no
threshold (noise) or match a threshold for a pest nobody was looking for.

**It is a tool and not a `doc_events` hook,** for the reason `index_lot_events`
is: `hooks.py` promises this app installs none and `test_hooks.py` fails the
build over one. A hook here would also fire on a foreman correcting a findings
note a fortnight later, and would fire *inside the completion's own transaction*
— where a refusal from the observation's controller takes down a completion that
was otherwise fine, while the worker is stood in the block.

**Idempotent on `Crop Observation.source_task`, which is the register and not a
flag.** A second sweep over the same window writes nothing. Where an observation
exists but the task's `produced_record` is blank — cleared by hand, or a
half-finished write — the **flag is repaired** rather than a second observation
written. That is the failure a hook cannot see, and it would otherwise double a
block's pest pressure invisibly from both ends.

**One bad row never costs the window.** A completion whose measurements the
observation's controller refuses is counted and named in `refused` with its
reason, and the sweep carries on. The completion stands — it was worked, and its
evidence is on the assignment.

**Completions whose task names no location are skipped and counted.** An
observation *is* a block; the register is keyed on one.

#### The `Field Scouting` template, and what it asks for

The sixth seeded `Farm Task Template`, and the first whose completion produces an
agronomic record rather than a compliance one. Its evidence contract is
`{"photos": true, "findings_text": true, "gps": true}` — `gps` is new in v0.115.0
and is the one requirement **nobody is asked to type**, which is exactly why it
has to be in the contract: a handset takes the fix on its own, and a client that
never learned to send one closes the task perfectly happily and leaves a season of
observations that cannot be put on a map.

Its `creates_record_data` defaults `observation_type` to `General`, **not** to
`Pest Scout`. Pest Scout is the one type whose record is invalid without a threat
and a count, and a template that shipped a mandatory field it has no way to fill
would refuse every completion from a walk where nothing was found — which is most
of them. A scout who counted something sends `observation_type: "Pest Scout"`
with the threat and the count in `record_data` and gets the whole engine.

#### `get_pest_pressure` / `list_pest_pressures`

One row per block, threat and **season year**: a pest that was a problem last
year and is quiet this year has to be two stories, or the first flare of the new
season is read against last year's peak and looks like nothing.

**The peak is not the latest.** A pest answered in June and quiet in August still
peaked in June, and next year's programme is planned off the peak.

**`trend` means deteriorating, not numerically larger.** On a Nutrient threat a
*dropping* tissue reading is a **rising** pressure. `list_pest_pressures` orders
by the status ladder rather than by the raw numbers, which are in different units
across threats and cannot be ranked against one another.

#### `get_ipm_recommendation` / `compute_sustainability_score`

Options are ordered **least chemical first** — the first row is what a person
reads, and a list opening with the spray would make the ladder decorative.
Rejected options are kept: a farm that declined the biological option four times
running has a pattern worth seeing.

The scale is **this app's own 0–100**, and the response says so:

| Method | Weight | |
| --- | --- | --- |
| Cultural | 1.00 | prevention — the rung everything else falls back from |
| No Action | 1.00 | the threshold said act and the beneficials said wait |
| Biological | 0.90 | a control that persists after you stop paying for it |
| Mechanical | 0.75 | real intervention, no residue |
| Behavioral | 0.70 | mating disruption, traps |
| Unclassified | 0.30 | cannot be shown to have been anything other than a spray |
| Chemical | 0.20 | the last rung — deliberately **not** zero |

Chemical is not zero because a correctly timed threshold-driven spray *is*
integrated pest management, and scoring it zero would tell a farm its best
chemical decision was worth the same as its worst. Unclassified sits *below* it
so the scale never rewards leaving the field blank.

**Accepted actions displace proposed ones.** Before anything is accepted the
score describes the options offered; once anything is accepted it describes what
was **chosen**. Without that switch, a farm offered a release and a spray that
chose the spray would keep the credit for having been offered the release.

It is **not a certification** — not USDA organic, not Protected Harvest, not
LIVE, not IPM Institute. What it is good for is a season-over-season number on
one farm that moves when the programme actually changes.

### The value block lifecycle

#### `create_planting_season` / `list_planting_seasons` / `get_planting_season`

`Field` already carries a crop, a variety and a planting year — that is the
block's **current** state. `Planting Season` answers what a single set of columns
structurally cannot: *what was out there in 2023, and what did that year cost,
and what did it return.*

| Argument | Meaning |
| --- | --- |
| `field`, `block_name` | `block_name` is the sub-block where a field is worked as several plantings |
| `crop`, `variety`, `rootstock` | All `Data`, for the reason `Field.crop` is |
| `lifecycle` | `Perennial` (default) or `Annual` |
| `plant_year`, `season_year` | On an Annual they must be the same year |
| `status` | `Establishing` / `Productive` / `Declining` / `Removed` |
| `productive_from`, `productive_through` | Estimates, and load-bearing ones |
| `acres`, `trees_planted`, `spacing_in_row_ft`, `spacing_between_rows_ft` | |
| `cost_center` | Without one, no ledger cost reaches the block at all |

**An Annual whose two years differ is refused.** An annual *is* its planting.
Left as written, several years of establishment cost land against one year's
revenue and the block reads as ruinous in one season and free in the others.

**Status is the lifecycle, not the health**, and it is **not** derived from the
dates: those are estimates made at planting, and the transition is a judgement
somebody makes standing in the block. A fourth-leaf planting not yet carrying a
commercial crop is still Establishing whatever the plan said in 2021.

`leaf_year` is computed, never stored — a stored copy is wrong for eleven months
of every year.

#### `get_block_cost_summary`

**Cost reaches a block two ways and adding them is wrong.** The ledger, through
the cost centre, is authoritative. `Block Cost Entry` holds the three things the
ledger cannot: a shared cost split across blocks, a cost with no ledger entry at
all (owner labour, an in-kind trade), and the **capitalisation decision per
cost**.

The double count is **named rather than guessed at**. Rows swept from the ledger
are excluded, because the ledger is counted directly. Rows carrying a journal- or
GL-entry reference are *probably* already inside it — they are excluded **and
listed**, because including them inflates the block's costs and dropping them
silently understates, and a farm told which rows are in question settles it in a
minute from the voucher numbers.

#### `get_block_revenue_summary`

**This is an attribution of revenue, not a recognition of it.** The ledger
records that a settlement paid $84,000; nothing in it records that four blocks
grew the fruit. The two answer *what did the business earn* and *which ground
earned it*, and this figure must never be presented as the first.

The **basis** is on every row because it is a judgement: a packing house settles
on a pool, and splitting it back on delivered weight is right for a single
variety and wrong the moment two blocks of different grades were pooled. Scale
Tickets naming the block are listed, with unattributed ones flagged — deliveries
whose return has not been traced back to the ground that grew them. This tool
**reads** them and writes nothing; splitting a pool belongs to a person.

#### `get_block_profitability`

**An establishing block's negative margin is not a loss and this refuses to call
it one.** A fourth-leaf cherry block that spent $180,000 and returned $4,000 has
not lost $176,000 — it has invested it, and the investment is carried in the
block's basis. Every figure is still reported, because the numbers are real and
somebody needs them; what the app will not do is put the word *loss* next to
them. `is_meaningful_as_profit_and_loss` is the flag and `verdict` says why.

**`cumulative` is the point on a perennial.** A block that takes fifteen years to
pay back cannot be judged on one of them — and a general ledger can only ever
show you one of them, because the ledger's period is the fiscal year and the
block's period is fifteen of them.

---

## v0.91.0 — the payroll register and the pay stub

v0.30.0 built the payroll engine and every figure it computes has been stored on
a Farm Payroll Slip since. What was missing was the two ways a person reads them:
the **register** an operator reconciles a period against, and the **stub** a
worker is handed with their pay.

**Neither recomputes anything.** Both read stored slips, so neither can disagree
with the run it claims to be a view of — the same judgement `render_tax_form_pdf`
makes about `form_data_json`, and on wages a disagreement is a claim.

### `get_payroll_register`

**READ (default ON).** One row per employee for a period, a totals row, and the
employer taxes beside them.

| Parameter | Required | Description |
|---|---|---|
| `company` | yes | Company docname or abbreviation |
| `pay_period` | | A Farm Payroll Entry docname — its own period becomes the window (`payroll_entry` is an alias) |
| `date_from`, `date_to` | | `YYYY-MM-DD`. Required unless `pay_period` is given (`from_date`/`to_date` and `pay_period_start`/`pay_period_end` are aliases) |
| `include_drafts` | | Count Draft runs as well. Off by default |

Each employee row carries `employee_id`, `employee_name`, `gross_pay`,
`federal_tax`, `state_tax`, `ss_employee`, `medicare_employee`,
`other_deductions`, `total_deductions`, `net_pay`, `hours_worked`, `piece_units`
and `periods`. `totals` sums every one of them. `employer_costs` carries
`ss_employer`, `medicare_employer`, `futa`, `suta` and `state_employer_other`
with their own total.

```bash
curl -sS -X POST https://erp.example.com/api/method/erpnext_mcp.mcp.handle \
  -H 'Content-Type: application/json' -H "X-MCP-Token: $TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
       "name":"get_payroll_register",
       "arguments":{"company":"Example Trading Co",
                    "date_from":"2026-06-01","date_to":"2026-06-30"}}}'
```

**`other_deductions` is derived, not read** — `total_deductions` less the four
named taxes. Nothing on the slip names a garnishment, so a column that read a
*field* would report zero for one. Deriving it means a deduction column a later
release adds is counted the day it lands.

**The window is on `pay_period_end` and a run is counted whole.** A run that
ended outside the window is out even where some of its days fall inside;
splitting one would produce withholding totals that reconcile against no deposit
anybody ever made. `window_rule` says so in the result.

**Draft and Cancelled are out by default** and `statuses_counted` always says
which were counted. Naming one run in `pay_period` reads that run whatever its
status — the caller asked for that run, not for a window.

**A selection over 200 runs is refused, not truncated.** A register that quietly
stopped short would look like it had covered the period.

#### Two cost totals, and they are different numbers

`grand_total_labor_cost` is **net pay plus every employer tax**.
`total_cost_of_employment` is **gross pay plus every employer tax** — the money
that actually leaves the farm, because the withheld income tax and the employee's
FICA are the employer's to remit. They differ by exactly
`total_employee_withholding`, reported beside them so the arithmetic can be
checked on the face of the result.

### `render_pay_stub`

**MUTATING (default OFF).** Draws one employee's stub for one run and attaches
the PDF privately to the Farm Payroll Entry.

| Parameter | Required | Description |
|---|---|---|
| `payroll_entry` | yes | Farm Payroll Entry docname (`name` is an alias) |
| `employee` | yes | Employee docname, or the name as the slip prints it |
| `show_employer_contributions` | | Draw the employer FICA/FUTA/SUTA section. Off by default |
| `company_address` | | The employer address to print — ERPNext keeps none on Company |
| `overwrite` | | Render again though a stub for this person on this run is attached |

Returns `file_url` for the attached PDF, plus the period's gross, deductions,
net, the resolved `hourly_rate` and the `ytd` block.

**This is not a working copy.** Unlike the tax form pages, a stub is the record
it looks like: the itemised statement of earnings ORS 652.610 and RCW 49.46.020
require an employer to give a worker, drawn from the slip that was actually paid.
It carries its own header and footer and no draft stamp.

**The earnings itemise and then balance.** Units at the piece rate, or hours at
the rate and overtime at 1.5x — **never both on one stub.** A piece-rate worker's
hours *are* the hours they picked in, and a salary structure may carry an
`hourly_rate` beside a piece `base_rate` for the mixed worker whose split the
record cannot state; pricing both would bill the picking twice. A piece stub
shows its hours unpriced, as the count the minimum wage floor was tested against.
Whatever the gross that leaves unaccounted for — break pay, the hourly half, the
FLSA §778.111 half-time premium — is a named balancing line, so the priced lines
always add up to the gross beneath them. A negative balance is drawn
rather than clamped: it means the rate the page was given is not the rate the
slip was computed at.

**Year to date is the calendar year**, this period included, and the heading says
so — a withholding year is a calendar year and this site's fiscal year may not
be. Omitted entirely rather than drawn as zeros where nothing could be summed.

**No Social Security number**, not even the last four. Neither statute asks for
one and the employee ID identifies the row.

**Attached to the record, not to a field.** A run carries one stub per employee.
A second render of the same stub is refused unless `overwrite` is passed, and the
File that was there stays attached either way.

**`reportlab` draws the page**, imported defensively like the tax form
renderers': a bench without it loses this one tool by name, and every payroll
figure stays readable through `get_payroll_entry` and `get_payroll_register`.

### Both are on the mobile surface, and both are HR-only

`/farmops/api/mobile/get_payroll_register` and
`/farmops/api/mobile/render_pay_stub` are the only routes there that reach wages,
so both wrappers require an HR role in their own bodies rather than the field
roles the surface is built for — a Foreman is refused. The register is
company-scoped and declares no `employee` argument; the stub is also
employee-scoped, so a docname outside the caller's own crew reads as not found.
`show_employer_contributions` is not on the mobile signature: whether a farm
shows its own FICA on a worker's statement is one operator policy, not a checkbox
on the handset of whoever printed it.


## Direct deposit and ACH (v0.91.0)

Six tools: a register of where each worker's wages go, and the two generators
that turn a calculated payroll run into a file a bank will accept.

### The account number leaves this site in exactly one direction

`account_number` is a **Password** field, which Frappe stores in its encrypted
`__Auth` table rather than in a column. **No read tool returns it.**
`get_employee_bank_account` and `list_employee_bank_accounts` return
`account_number_last_four` and a masked `****1234`, and the mobile route returns
the same. The one place the full number is materialised is inside a generated
ACH file — and that file is written as a **private** attachment, the same rule a
1099 gets.

The field list those reads use is a module constant that does not contain
`account_number`, and a masking step strips it again on the way out. Two locks
on the one field that would matter.

### `create_employee_bank_account` / `update_employee_bank_account` — MUTATING, default off

`routing_number` is validated against the **ABA check digit** — a 3-7-1 weighted
sum over the nine digits — which catches every single-digit typo and every
transposition of an adjacent pair. Between them that is very nearly every way a
routing number gets copied wrong off a cheque, and the failure it prevents is
somebody's wages arriving at another bank.

**Changing the routing or account number clears `prenote_sent`.** The bank
confirmed the old number; a flag that survived the edit would let a
never-verified account past the warning in `generate_nacha_file` on the strength
of a test run against different digits.

**Split deposits are rows, not fields.** An employee paying $200 to savings and
the rest to checking has two rows: one `Fixed Amount` of 200 and one `Full`. At
most one `Full` per employee, and percentages across an employee's active rows
may not exceed 100 — both checked against the employee's whole set rather than
the row being written, because a row that is individually valid can still make
the set unpayable.

**A percentage is taken of the original net pay, not of the running remainder.**
"20% to savings" means a fifth of the cheque. The other reading makes the amount
depend on the order two sibling rows happened to be created in.

### `generate_nacha_file` — MUTATING, default off

Takes a **Calculated or Submitted** Farm Payroll Entry — never a draft, because
net pay is not final until the run is calculated — and attaches a NACHA file to
it privately. One batch, Standard Entry Class **PPD**, one Entry Detail record
per **deposit** rather than per employee.

**An employee with no active bank account is skipped and reported.** They are
paid by cheque, which is ordinary on a farm, and a file that refused to exist
because one picker has no bank account would be useless.

**An employee whose allocations do not add up refuses the entire file.** This is
the asymmetry worth understanding: a half-allocated cheque is not a smaller
payment, it is a wrong one, and it is wrong *silently* — the batch total is
computed from the entries actually written, so the file balances against itself
while shorting somebody. There is no partially-correct payroll file.

The result reports `employees_paid`, `employees_skipped` with a reason each, and
`warnings` for accounts that were never prenoted or are still inside the
three-banking-day return window. The prenote warning does **not** refuse: the
waiting period is a convention between a company and its bank, not something
this app should enforce against a payroll that has to go out on Friday.

### `generate_prenote_file` — MUTATING, default off

Zero-dollar entries carrying their own transaction codes — **23** checking,
**33** savings, against 22 and 32 for a live credit — which ask the receiving
banks to confirm the accounts exist. A $0.00 entry under an ordinary credit code
is not a prenote; it is a payment of nothing.

Defaults to every active account for the company that has never been prenoted,
and marks them sent. `resend=true` includes ones already done.

### ACH Originator Configuration

The company's own identity in the ACH network, one row per company: the
originating routing number, the ten-character company identification its bank
issued, and the names that appear in the file header. **None of it can be
inferred from anything else on the site** — it all comes from the origination
agreement — which is why the generators refuse, naming the company, rather than
guessing.

`settlement_days` is what the default effective entry date is computed from.

### The format is exact, and the tests are about that

Every NACHA record is **94 characters** — not 94 fields with a delimiter, 94
character positions each belonging to one field. A field written one character
short does not produce a short field; it shifts every field after it and the
file still looks like a file. The builder concatenates fixed-width pieces and
asserts the total is 94 before returning, and the test suite pins every field's
offset and width against the spec, both control records' totals, the entry hash,
and the ten-record blocking.

**The entry hash is not a checksum of the file.** It is the sum of the
*eight-digit* routing prefixes of every entry — the check digit deliberately
excluded — truncated to its rightmost ten digits.

## Garnishments and voluntary deductions

Every release before this one computed a slip whose only deductions were the ones
the government requires. That is the easy half of a payroll run and not the half
with a liability attached: a court serves a support order on the **employer**, and
an employer who pays the worker in full is answerable for the money it failed to
withhold and, in most states, for the arrears on top.

A **Farm Payroll Deduction** is one standing instruction — what to take, from
when, under which document. Each payroll run reads it and decides what it can
actually take out of that period's pay. The two are different numbers whenever a
ceiling binds, and the difference lands on the slip as a shortfall.

### The order is the law's own order

1. Gross pay, minimum wage makeup included.
2. **Pre-tax** voluntary deductions leave the wage base **before** withholding is
   computed. This is the step that makes a 401(k) a 401(k).
3. Taxes, on the reduced base.
4. **Disposable earnings** = gross − the taxes actually withheld. Note what is
   *not* subtracted: voluntary deductions, pre-tax ones included. 29 CFR 870.10
   says amounts required **by law**, and an elective deferral is not one —
   subtracting it would let an employee shrink the base a court order is measured
   against by raising their own contribution rate.
5. **Garnishments**, in legal priority order, against the CCPA ceilings.
6. **Post-tax** voluntary deductions, out of whatever cash is left.

Steps 5 and 6 are in that order because a garnishment outranks a union due. When
the money runs out, the voluntary deduction is what gets cut.

### "Pre-tax" is two different answers

Getting this wrong is the classic version of this bug, and it reconciles cleanly
all year before surfacing on a W-2:

| | Federal income tax | Social Security / Medicare / FUTA |
|---|---|---|
| Section 125 — health, dental/vision, HSA, FSA | exempt | **exempt** |
| Traditional 401(k) elective deferral | exempt | **not exempt** |

IRC §125(a) and §3121(a)(5)(G) for the first row; §402(e)(3) defers the income tax
on the second while §3121(v)(1)(A) keeps the deferral in the FICA wage base. So a
slip carries **two** reduced bases — `federal_taxable_gross` and
`fica_taxable_gross` — and on a slip with a 401(k) they differ by exactly the
deferral. Those two figures are what W-2 Box 1 and Box 3 are built from, and Box 1
and Box 3 are supposed to differ by the deferral rather than agree.

### Four ceilings, not one

| Kind | Ceiling | Authority |
|---|---|---|
| Ordinary garnishment (creditor, judgment) | lesser of **25%** of disposable earnings, or the amount over **30× federal minimum wage** | 15 U.S.C. §1673(a) |
| Child support | **50%** supporting another family, **60%** if not, **+5** for arrears past 12 weeks | §1673(b)(2) |
| Federal or state tax levy | **no CCPA limit**; bounded by the IRC §6334(d) exempt amount on the notice | 29 CFR 870.11(b)(2) |
| Student loan (AWG) | **15%** of disposable pay, inside the ordinary pool | 20 U.S.C. §1095a(a)(1) |

The floor is $217.50 a week at $7.25, and the regulation gives the longer periods
their own multipliers rather than asking anybody to annualise: 60× biweekly
($435), 65× semimonthly ($471.25), 130× monthly ($942.50).

**When several compete the pool is shared.** 29 CFR 870.11(b)(1): an ordinary
garnishment gets what is *left* of the 25% after support has taken its share, not
a fresh 25% of its own. Where support took a quarter or more, a creditor collects
nothing that period — that is the rule working, not a failure to collect, and the
slip says which rule refused it. Two orders of the same kind that will not both
fit are **prorated** by ordered amount, and each line says it was.

`state_cap_rate` is the hook for a state stricter than the federal 25%. Title III
is a floor under the worker's protection and never a ceiling on it (§1677), so
the tighter rule always wins and a looser state rate cannot loosen anything.

### What it will not do

**It never produces a negative net.** Where the elections and the orders together
exceed the pay, deductions are cut in reverse priority — voluntary first — and
every cut is reported with what was asked and what was taken.

**It does not carry arrears forward.** What a period could not take is reported
and not remembered; the next period computes from its own pay. `deduction_shortfalls`
on the slip and on the run summary is the only place it is ever said, and somebody
has to read it.

**It gives no legal advice and makes no determination.** The state rule that beats
the federal one is not shipped, and a few states bar creditor garnishment of wages
almost entirely.

### `list_payroll_deductions`

The register, filtered by employee, type, category, status or reference. Every row
carries the four **derived** facts that actually decide the money — effective
priority, pre-tax, FICA-exempt, and which ceiling governs it — because none of
them is necessarily stored on the row; they fall back to the category.
`in_force_on` asks what was in force on a date, which is the question an audit
asks, and it handles an open-ended order correctly where a plain date filter would
silently drop every one of them.

### `get_payroll_deduction`

One row in full, with the warnings worth reading about it: a garnishment with no
reference to defend it, a tax levy with no exempt amount, a percentage with no
per-period cap, a 401(k) wrongly marked FICA-exempt.

### `list_employee_deductions`

Everything standing against one worker's pay, **in the order it comes out**. Pass
`gross_pay` to turn it into a priced preview against the CCPA ceilings — and pass
`statutory_withholding` with it, because disposable earnings is gross less the
legally required withholding and this tool has no W-4, bracket table or state
configuration to compute it. Without it the preview treats gross as disposable,
which **overstates** what a garnishment may take; it says so on the answer rather
than quietly assuming zero tax.

### `create_payroll_deduction`, `update_payroll_deduction`

Both mutating, both default **off**.

The category sets the law and you rarely override it: processing order, ceiling,
pre-tax and FICA treatment all come from it. `deduction_type` is derived from the
category when you do not name one — a support order filed as a voluntary election
would sit behind the union dues and outside its own ceiling, so the doctype ships
with **no** default on that field rather than a default of Voluntary.

**Create refuses** a second active order of the same category with the same
reference against the same worker (filing it twice withholds it twice, which is
money actually taken from somebody), a garnishment marked pre-tax, a percentage
over 100, a window that ends before it starts, and an employee name matching more
than one person.

**Update cannot re-key it.** `employee` and `company` are refused by name: moving
a deduction to another worker would apply an order made against one person to
somebody else and leave an audit trail saying it had always been theirs.

**Nothing deletes.** A deduction is retired by status — `Completed` for an order
satisfied, `Suspended` for one a court has stayed. A garnishment removed from the
file cannot answer the court that asks why the withholding stopped, and a
satisfied order is the record proving it was paid off.

### On the slip, and on the mobile surface

`total_deductions` **includes** garnishments and voluntary deductions, so
`net_pay = gross_pay − total_deductions` remains the invariant it always was and
net pay stays what the worker is handed. The taxes on their own are
`statutory_deductions`; the itemised lines a stub prints are `deduction_lines`.

All five tools are on `/farmops/api/mobile/`, and like the payroll register and
the pay stub they require an HR role in their own bodies rather than the field
roles the surface is built for — what a person's wages are garnished for is among
the most sensitive facts this app holds, and a Foreman is refused. The writes are
published because withholding on a support order is required from the first pay
period after service, and the gap between an envelope opened in a yard and
somebody reaching a Desk is a gap with a liability in it. `employee` and `company`
are absent from `update_payroll_deduction`'s mobile signature entirely, so the
argument filter makes that refusal unreachable rather than merely enforced.

## Garnishment compliance — the order behind the withholding

Five tools over the `Farm Garnishment` DocType. `list_garnishments` and
`get_garnishment` are read-only and **on** by default; `create_garnishment`,
`update_garnishment` and `render_garnishment_response` are mutating and default
**off**.

### Why the order is a separate record from the deduction

A `Farm Payroll Deduction` is a standing *instruction* — take this much, from this
date, under this ceiling. That is everything a payroll run needs and nothing a
court needs. When a child support agency writes to ask why withholding stopped in
March, the answer is a **case number, a date of service, a balance and a letter**,
none of which belongs on a row whose job is to be read forty times a year by an
arithmetic engine.

So the order is one record and the instruction is another, and the order **owns**
the instruction: filing a garnishment creates the deduction, and satisfying or
terminating the garnishment stops it.

### Federal priority is derived, not entered

| Type | Priority | Ceiling on disposable earnings | Authority |
|---|---|---|---|
| Child Support | **1** | 50 / 55 / 60 / 65% by the two facts on the order | 15 U.S.C. §1673(b)(2) |
| Tax Levy | **2** | none — Title III does not apply | 29 CFR §870.11(b)(2) |
| Student Loan | **3** | 15%, inside the ordinary 25% pool | 20 U.S.C. §1095a(a)(1) |
| Creditor | **4** | lesser of 25% or the amount over 30× minimum wage | 15 U.S.C. §1673(a) |

The `priority` field is **read-only and recomputed from the type on every save**.
It is what the law says, not what an employer decides, and a field somebody could
type into would eventually hold an opinion.

**It is not the payroll engine's queue number.** The engine orders by the
*deduction's* category — 10, 20, 30, 40 — and `create_garnishment` deliberately
passes **no** priority across. Pushing 1–4 into that column would sort a creditor
order at 4 ahead of a support order at 10 and invert the exact precedence the
field exists to record. The two sequences agree about the order and disagree about
the integers.

### `create_garnishment`

Files the order **and** creates its deduction, through `create_payroll_deduction`
— the same writer, the same allowlist, the same duplicate refusal, the same
category-to-law mapping. A second deduction writer here would be a second place
for the CCPA ceilings to be got wrong.

**One switch governs both halves**, `allow_create_garnishment`. An operator who
enabled the first and got a record that withholds nothing would have a garnishment
on the file that the payroll run cannot see. If the deduction cannot be created
the whole call rolls back and no order is left behind.

**Refuses** a second active order on the same case number against the same worker
(filing it twice withholds it twice), a withholding amount of zero, a percentage
over 100, a stated ceiling over 100, and an employee name matching more than one
person. The same case number against a *different* worker is allowed — a
multi-defendant judgment is one number and two orders.

**Reports the competing orders** it now sits alongside. An employer served with a
creditor judgment against somebody who already has a support order does not get a
fresh 25% pool: 29 CFR §870.11(b)(1) gives the ordinary garnishment only what is
**left** after support, frequently nothing, and that is the correct answer rather
than a failure to collect.

### `update_garnishment`

Posting what payroll withheld is what moves the balance. Pass `add_withheld` with
what a run actually took — an **increment**, because a payroll run knows its
period and not the running total, and making every caller read the sum and write
it back is a lost update the moment two runs post together. The lost update is
money that was taken and is not on the balance.

**Reaching zero satisfies the order and stops the money.** When `total_withheld`
meets `total_owed` the status becomes `Satisfied`, `satisfied_on` is stamped, and
the linked deduction is retired to `Completed`. Withholding past a satisfied
judgment is money taken under an authority that has expired, and it is the
employer that took it.

**A `total_owed` of zero is an absence, not a paid-off debt.** Every Currency
field is 0 on a row nobody filled in, and every child support order is one — an
ongoing obligation with no principal to run down. The arithmetic runs only where a
balance exists; reading 0 as "satisfied" would mark a support order Satisfied on
the day it was filed and stop the withholding.

Changing the amount or type **carries onto the deduction**, reported as
`deduction_changes`: an order that says $200 and a run that takes $150 is not a
discrepancy anybody notices, because both figures look deliberate. Status is not
mirrored blindly — a deduction a court has *stayed* stays `Suspended`.

`employee` and `company` are refused by name, and nothing deletes. `Terminated` is
for an order the issuer released.

### `render_garnishment_response`

Draws the employer's acknowledgment back to the issuing court or agency — that the
order was received, that this employer employs the worker named, what will be
withheld and from which date, what else already stands against the same wages, and
what the employer undertakes to do next — and attaches the PDF **privately** to the
garnishment.

What an employer owes on being served is an **answer**, and failing to give one is
what gets an employer defaulted rather than merely audited: a federal Income
Withholding for Support order requires the employer to begin within a stated
number of days, a state writ normally requires a sworn answer within twenty or
thirty, and an administrative wage garnishment asks for an employer certification.

**It is the employer's own letter and the page says so.** Where the issuer
prescribes its own answer form this accompanies that form and does not replace it,
and it does not imitate one — drawing something that looked like a court's own form
would be the one page in this app that lies about what it is.

**No figure on it is a prediction.** Disposable earnings belong to a pay period
that has not happened, so the page prints what the order *directs* and which
ceiling governs it, never a computed dollar figure a later run could contradict.
The competing orders are printed with the shared-pool rule beside them for the same
reason. A second render refuses without `overwrite` — the likeliest thing already
in that field is the copy that was posted to the court.

## v0.92.0 — what is owed, and when it has to be there

`taxforms.py` generates RETURNS — a 941, an OQ, a WA-ESD — and records each as a
Tax Form kept verbatim, so that correcting payroll later cannot rewrite the W-2
already in an envelope. It says in its own docstring that computing a deposit
schedule is a thing it does not do. These five are that thing.

A return is filed once a quarter and says what was owed. Deposits are made every
payday, and a late one costs 2% to 15% of the deposit under IRC §6656 — on money
the employer withheld from somebody else's wages and is holding in trust. All
five are **read-only, default ON**, gated on `HR_ROLES` (System Manager, HR
Manager, HR User, Farm Manager), and store nothing: they recompute on every call,
because the question is what is owed *now*.

### Farmworkers are on Form 943, not Form 941

Form 943 is the annual return for agricultural employees; 941 is quarterly for
everybody else. They are not alternatives, the choice is made **per worker**, and
an employer with both farm and office staff files both. Nothing on a payroll slip
in this app marks a worker as agricultural labour, so `get_941_prefill` cannot
make the split — it totals every slip in the quarter and says so in
`warnings[0]`, first rather than last, because on a tree-fruit operation the
agricultural case is the normal one.

### FUTA may not apply to farm labour at all

Federal unemployment tax reaches agricultural wages only if the employer paid
**$20,000** in cash wages in some calendar quarter, or employed **10 or more**
farmworkers in each of **20 or more** weeks. Either is enough, and meeting one
makes the whole year liable from the first dollar. Meeting neither means no tax
and no Form 940 — not a reduced amount. Both tests are computed and **reported,
never enforced**: the cash-wage figure is exact, the weeks figure is derived from
pay periods, and farm wages paid outside this app count toward both and are
invisible here. Enforcing would mean silently zeroing a tax on an estimate, and a
liability that never reaches a report is one nobody audits.

### The $7,000 cap is consumed in date order

An employee earning $3,000 a quarter reaches the FUTA wage base in Q3. The Part 5
quarterly liabilities are **18 / 18 / 6 / 0** on a $42 annual tax — not $10.50
four times. Annualising and dividing produces a Part 5 that matches no real
quarter.

### The payday is not a recorded field

Every federal deposit rule keys on the date wages were **paid** (26 CFR
31.6302-1(c)). A Farm Payroll Entry has `pay_period_start` and `pay_period_end`
and no pay date; the slip child table has no date at all. So, in order: a run that
reached the ledger has a real `posting_date` on its GL postings and that is used;
everything else falls back to the period end plus `payday_offset_days`. A
fallback date is **early** by the farm's payment lag — the safe direction, since
an operator following it deposits too soon rather than too late — and every row
carries a `payday_basis` sentence saying which of the three it was.

### The lookback figure computed here is a floor

Monthly or semiweekly is decided by the four quarters ending 30 June of the
*prior* year, and by nothing about the current one. A total computed from this
site's own payroll can only see payroll this app ran: a quarter it did not run
reads as zero rather than as unknown, which can put a genuine semiweekly
depositor on a monthly schedule — the expensive direction. The result reports how
many of the four quarters had data and warns below four; pass `lookback_total`
off the filed 941s, or `schedule` directly.

### The quarter is taken as a word or as a number

All five reads accept `quarter` as `"Q3"` or as `3` — the string a model writes
and the integer a picker posts are normalised to the same value before the period
is computed. Anything outside 1–4 is still refused, and is quoted back as it was
sent rather than guessed at.

### `get_tax_remittance_summary`

**READ (default ON).** Everything owed to every authority for a period, broken
down by pay period. The federal figure is one EFTPS deposit's worth — income tax
withheld plus **both halves** of Social Security and Medicare. FUTA sits beside
it, not inside it, because it is deposited separately on its own quarterly rule.

The employer halves are read off the slip rather than doubled from the employee's:
Additional Medicare is a 0.9% employee-only surcharge with no employer match, so
doubling overstates the deposit for anybody over the threshold. Slips predating
those columns are mirrored per row, and `warnings` says how many.

| Parameter | Required | Description |
|---|---|---|
| `fiscal_year` | yes | Calendar year as `YYYY` (`year` is an alias) |
| `company` | | Required on a multi-company site |
| `quarter` | | `Q1`–`Q4`, or the number `1`–`4`; omit for the whole calendar year |

### `get_941_prefill`

**READ (default ON).** Lines 1 to 15 for one quarter, plus Part 2's monthly
liabilities — which must total line 12 **to the cent** or the return is rejected.
`reconciles` says whether they do, and the quarter-level residual (line 7's
fractions of cents, the adjustments, the credit) is applied to the last month that
actually held pay rather than to the last month of the quarter.

Recomputed on every call, so it will drift from a Tax Form generated earlier —
which is exactly why that one is stored. Read `warnings[0]`.

| Parameter | Required | Description |
|---|---|---|
| `fiscal_year` | yes | Calendar year as `YYYY` |
| `quarter` | yes | `Q1`–`Q4`, or the number `1`–`4` |
| `company` | | Required on a multi-company site |
| `deposits` | | Line 13 — federal deposits made for the quarter |
| `ytd_wages_by_employee` | | Prior-quarter wages, for the Social Security base from Q2 on |
| `sick_pay_adjustment` / `tips_and_group_term_life_adjustment` / `small_business_payroll_tax_credit` | | Lines 8, 9 and 11 |

### `get_state_tax_remittance`

**READ (default ON).** Oregon's two forms and Washington's one.

**An OQ is not a filing without its Form 132.** The OQ carries the employer's
totals across withholding, the Statewide Transit Tax, Paid Leave and
unemployment; Form 132 carries the per-employee detail Oregon assesses benefit
eligibility from — wages, UI subject wages after the cap, excess wages, and
**whole hours rounded down**, which is Oregon's instruction rather than a display
choice. Oregon reconciles the two against each other, and this reports when they
disagree. The UI wage base is consumed **from 1 January**, not from the start of
the quarter: applying it to the quarter alone overstates Q3 for a crew that
worked the spring.

Washington's ESD report carries hours per employee — a hard requirement there,
not an approximation — plus UI, Paid Family & Medical Leave and WA Cares.

| Parameter | Required | Description |
|---|---|---|
| `fiscal_year` | yes | Calendar year as `YYYY` |
| `quarter` | yes | `Q1`–`Q4`, or the number `1`–`4` |
| `company` | | Required on a multi-company site |
| `state` | | `OR` or `WA`; omit for both |
| `ui_rate` | | The state's assigned rate, as a percent |
| `or_ui_wage_base` / `wa_ui_wage_base` | | That state's UI taxable wage base for the year |
| `ytd_wages_by_employee` | | Prior-quarter wages, so the UI cap is consumed from January |
| `state_ids` | | `{"OR": "1234567-8"}`, overriding the State Tax Configuration |

### `get_tax_deposit_schedule`

**READ (default ON).** Every federal deposit deadline in the period, with the
rule that produced it, plus the state filing dates.

Semiweekly: a Wednesday/Thursday/Friday payday is due the following Wednesday, a
Saturday-to-Tuesday payday the following Friday. A federal holiday among the three
weekdays after the period closes buys **one more banking day** — the
three-banking-day rule, and the one most implementations skip. Monthly: the 15th
of the following month. A deposit reaching **$100,000** is due the next business
day whatever the schedule, and is flagged.

Holidays are computed as observed, including the case where 1 January falls on a
Saturday and the holiday lands on 31 December of the year before.

| Parameter | Required | Description |
|---|---|---|
| `fiscal_year` | yes | Calendar year as `YYYY` |
| `company` | | Required on a multi-company site |
| `quarter` | | `Q1`–`Q4`, or the number `1`–`4`; omit for the whole year |
| `lookback_total` | | Tax reported on the four returns in the lookback period |
| `schedule` | | `Monthly` or `Semiweekly`, overriding the lookback test |
| `payday_offset_days` | | 0–60; days between a period closing and the money moving |

### `get_futa_summary`

**READ (default ON).** Form 940 for a calendar year: lines 3 to 17, the Part 5
quarterly liabilities, the deposit plan and both agricultural coverage tests.

6.0% of the first $7,000 each employee earns, less a credit of up to 5.4% for
state unemployment tax paid on time — a net 0.6% in a state not under credit
reduction, which in 2025 neither Oregon nor Washington is. Under **$500**
accumulated at a quarter end nothing is deposited and the liability carries
forward, so a small employer can reach Q4 having deposited nothing and owe the lot
with the return.

What payroll recorded on the slips is compared against what the wage-base walk
produces, and a disagreement is reported rather than reconciled away — the walk is
the one that matches the form, because a single pay period computing its own FUTA
cannot see the year around it.

| Parameter | Required | Description |
|---|---|---|
| `fiscal_year` | yes | Calendar year as `YYYY` |
| `company` | | Required on a multi-company site |
| `deposits` | | Line 13 — FUTA already deposited |
| `exempt_payments` | | Line 4 |
| `credit_reduction` | | Line 11, for a credit-reduction state |
| `futa_rate` / `futa_wage_base` / `futa_state_credit_max` | | Override the FICA Configuration |

**A quarter is refused rather than ignored.** Form 940 is annual, and its
quarterly liabilities are computed from the whole year because the wage base is
consumed across it — a quarter cannot see the ones before it.

### On the mobile surface

All five are published on the FarmOps `/mobile` table, each carrying
`require_hr_role` in its own body and company-scoped through
`guard.require_company` — the same footing as `get_payroll_register`, and
deliberately not `DISPATCH_ROLES`, which would put the farm's tax position in
front of every foreman on the site. None writes anything: committing a figure as
the thing an agency was told is `generate_tax_form`, which stays off this surface.
The three correction arguments are on the deposit schedule's signature only; the
941's adjustment lines are not, so the route's argument filter keeps them off the
handset.

---

## The org structure an Employee links to (v0.68.1)

Five masters ship with Frappe HR, five of them are Link targets on `Employee`,
and until this release none of them could be created from here. `create_employee`
has always checked `designation`, `department`, `branch` and `employment_type`
against the site's own records and refused a value naming none — correctly, and
with the site's own choices listed in the refusal. What was missing was any way
to act on that answer.

Fifteen tools, three per master: `create_`, `list_` and `update_`.

| Master | Create | List | Update |
|---|---|---|---|
| Designation | `create_designation` | `list_designations` | `update_designation` |
| Department | `create_department` | `list_departments` | `update_department` |
| Branch | `create_branch` | `list_branches` | `update_branch` |
| Employment Type | `create_employment_type` | `list_employment_types` | `update_employment_type` |
| Employee Grade | `create_employee_grade` | `list_employee_grades` | `update_employee_grade` |

The checks in `create_employee` are **unchanged**. These tools create the records
those checks read; the check and the register are two halves of one thing, and
neither is loosened for the other.

### The docname is not the name you typed

Frappe names these five in three different ways, and a caller that assumed one
would be wrong on a live site:

* **Designation, Branch, Employment Type** — `field:` naming. The docname *is*
  the name somebody typed.
* **Employee Grade** — `Prompt`. The docname is exactly what you pass, with no
  column of its own behind it.
* **Department** — a controller that appends the company's abbreviation, so
  `Harvest` at Orchard Meadow, LLC is named `Harvest - OML`.

So every tool here resolves **both** spellings, always, on all five, and every
result reports the docname Frappe actually chose. Nothing in this app computes a
docname: that is a rule Frappe already owns, and a second implementation of it
would drift the first time a site customised `Department.autoname`.

```bash
# Created as "Harvest"; named "Harvest - OML".
{"name": "create_department",
 "arguments": {"department_name": "Harvest", "company": "Orchard Meadow, LLC"}}

# Both of these find it.
{"name": "update_department", "arguments": {"department": "Harvest", "disabled": true}}
{"name": "update_department", "arguments": {"department": "Harvest - OML", "disabled": true}}
```

### The reads carry a headcount

Every `list_` returns `active_employees` per row and an `unused` list of the rows
nobody holds, counted through the matching `Employee` column (`designation`,
`department`, `branch`, `employment_type`, `grade`). That is what makes the read
worth calling twice: a row in `unused` is safe to rename or retire, and one with
a crew on it is not.

`in_use_only: true` drops the empty rows. `list_departments` also takes
`company`.

### `new_name` renames, and carries the people with it

Branch, Employment Type and Employee Grade carry **nothing but their name** on a
stock site. An `update_` that only set fields would be a tool that can never do
anything, and the correction is the edit an operator actually needs — a camp
typed wrong at six in the morning is already on every person hired since.

`new_name` goes through Frappe's own `rename_doc`, so every Employee already
carrying the old value reads the new one without anybody editing them.

```bash
{"name": "update_branch",
 "arguments": {"branch": "Mill Creak", "new_name": "Mill Creek"}}
```

**It is refused where the target name already exists.** Frappe's `rename_doc`
takes a `merge` flag that folds one record into another; that is a decision about
which of two designations forty people actually hold, not a spelling fix, and
this tool must not make it by accident.

Anything that stored the old string *outside* a Link field — a saved report
filter, a saved view — still says the old one. That is reported in the result's
`note`.

### What the writes refuse

**Employee Grade's pay columns, by name.** `default_base_pay`,
`default_salary_structure` and `default_leave_policy` are refused on both the
create and the update, in the handler as well as in the schema. One value on a
grade sets what an entire *band* of people is paid — it reaches further than any
single Employee field this app writes — and it has a form, an approval and a
retention rule in the Desk that this app knows nothing about.

**A duplicate name.** Every `create_` is idempotent by NAME rather than by
docname, which is the only check that works on the two masters whose docname is
built rather than typed. Two rows with one name split the people who hold it
across both, and no report adds them back together.

**A company the caller cannot see**, on `create_department` — the one of the five
that belongs to a Company.

All ten writes require System Manager, HR Manager, HR User or Farm Manager. The
five reads are not role-gated: a hiring form has to be able to offer the list it
is about to refuse a value against.

### `reports_to` on the Employee

Added to `create_employee` and `update_employee` in the same release, because it
is the org-structure field that lives on the person rather than in a register.

Two of this app's own surfaces already read it and neither could get it filled
in: `escalate_farm_task` refuses with *"no reports_to on their Employee record, so
this app does not know who their supervisor is"*, and `get_shadow_log_entry` walks
the chain to build a review ladder. The only editor was the Desk.

It accepts the supervisor's docname, employee number, name **or** login — the same
four ways in every other Employee argument takes, so it can be filled from a badge
scan.

**A loop is refused, and the refusal prints the path.** This is the only
self-referential Link this app writes, so it is the only one that can close a
cycle; everybody in a reporting cycle is their own supervisor, an escalation walks
it forever, and a review ladder has no top. A cycle that was *already* in the data
before this tool existed does not block an unrelated correction — the walk stops
rather than making somebody else's mess the caller's problem.

---

## The pre-harvest interval, enforced at the pick (v0.68.1)

PHI days live on the pesticide label and reach the site through `Item.phi_days`.
Recording a spray stamps a **`phi_clears_on`** date on whatever recorded it, and
until this release that date was a compliance *alert* (`phi_harvest_window`) and
nothing more — the board said "no harvest until the 8th" and the tool that raises
a harvest task did not read it.

It reads it now, and **it refuses**.

### Why this refuses where the restricted-entry guard warns

These two look like the same check and are opposite decisions. Both are asserted
in `tests_standalone/test_phi_harvest.py`, in both directions, for that reason.

| | Restricted-entry interval | Pre-harvest interval |
|---|---|---|
| A condition on | **entry** | **the fruit** |
| Work inside it | lawful with the label's PPE on (40 CFR 170.607) | nothing makes it lawful |
| Discovered | at the block | at the packing house, days later, on a shipped load |
| This app's response | **warns**, and dispatches | **refuses** |

A server that refused work inside an REI would be inventing a rule stricter than
the regulation and training foremen to route around the app. A server that only
warned about a PHI would be watching somebody do the one thing the whole record
exists to prevent, and printing a sentence about it.

### Where the guard runs

Both moments harvest is *initiated* on a block:

* **`create_farm_task`** with `task_type: "Harvest"` and a `location` (or `asset`)
  naming the block — the moment the pick is planned. Refused on the arguments,
  before the document is inserted, so there is nothing to roll back.
* **`assign_farm_task`** — the moment a name goes onto the record. A picking plan
  made in advance and a cover spray landing on top of it is the ordinary order of
  events, and this is the guard that catches it.
* **`claim_farm_task`** — the worker's own door. Refused, and it names
  `assign_farm_task` so somebody standing on a block is told who *can* act.

Both mobile wrappers (`create_farm_task`, `assign_farm_task` on the FarmOps
`/mobile` route) inherit the guard and forward the override.

The refusal names the block, the first date it may be picked, the product and the
spray record:

```
Yellow Camp Block 3 - MC is inside a pre-harvest interval until 2026-08-08
(SURROUND-WP, SA-00001). A pick inside the interval is a residue violation on a
shipped load — it is found at the packing house, days later, and traced back to
this block and this date. …
```

### Two registers, and the longest window wins

This app records a spray in two places on purpose, and a guard that read one of
them would clear a block the other says is closed:

* **`Spray Application`** — the record of the pass, with the wind and the
  licence on it. Its blocks are a child table.
* **A completed `Farm Task` of type Spray** — `stock_bridge.spray_windows`
  stamps the window when the tank mix is drawn down, which is the path a spray
  dispatched from the board takes.

Both are read. Nothing is merged: each window keeps the register it came from,
because "which spray was this" is the first question asked when somebody disputes
a date.

### The day the block opens

`phi_clears_on` is the **last** day of the interval, not the first clear one. The
`phi_harvest_window` compliance rule raises while `phi_clears_on >= today` and
silences the day after, and this guard agrees with it to the day — a guard that
cleared a block *on* that date would open it a day before the alert about it went
out. Every message reports the day after.

### The override

`override_phi: true` with `phi_override_reason` (mandatory) raises or dispatches
the task anyway. It is for a **stamped date that is wrong**, not for an interval
that is inconvenient: a window opened by a tank that only covered part of the
block, or a label corrected by the registrant since.

* The reason is written into the task's **own `notes`**, not only into the action
  log. A load questioned at the packing house is traced to a block and a date, and
  the record somebody opens is the task.
* `override_phi` with no reason is refused. An override with no reason is
  indistinguishable afterwards from a guard that was never there.
* The spray records are untouched, so the compliance alert stands until the date
  passes.
* **`claim_farm_task` has no override at all.** "The picker decided the interval
  did not apply" is not a defence anybody can offer at the packing house.

---

## Mobile roles and farm job titles (v0.68.1)

"We need a Checker role." "We need a Tractor Driver role." "We need a Crew Leader
role." Two of those are not requests for a role and one of them is, and the test
that tells them apart is not *is it a distinct job* — it is **does it touch a
different set of records**.

* **A role** says what kind of record somebody may touch. It is what a Custom
  DocPerm hangs off, it is coarse on purpose, and adding one permanently widens
  this app's permission surface.
* **A job title** says what somebody does all day. It is `Employee.designation`,
  an operator adds one in ten seconds, and **this app already reads it** —
  `list_pending_threshold_acknowledgments` finds every checker on the site by
  filtering Active Employees on `designation == "Checker"`, and a Position Wage
  Default keys a rate on the same column.

### The mapping

`list_mobile_users` returns this as `job_titles`, so it is machine-readable
rather than a paragraph in a release note.

| Designation (`update_employee`) | Mobile role (`create_mobile_user`) | Why |
|---|---|---|
| Picker | Field Worker | The job the role was written for. |
| **Checker** | Field Worker | Same records as any Field Worker **plus the container fill threshold**, which v0.68.1 granted to the role. |
| **Tractor Driver** | Field Worker | Nothing about driving a tractor touches a register a picker does not. |
| **Crew Leader** | **Crew Leader** | Forming and closing a shift writes a register no Field Worker may. |
| Foreman | Foreman | The dispatch board as well as the shift. |

Two fields, two tools. `update_employee` sets the designation;
`create_mobile_user` sets the role. Seeding a designation grants nothing.

The designations are seeded by the installer, **create-only** — an operator who
renames one, or deletes a title they do not hire, keeps that decision through
every later migrate.

### Crew Leader: the seventh role

This app *already named* Crew Leader and did not have one.
`employee.SHIFT_ROLES` has been `HR_ROLES` plus Foreman and Crew Leader since
v0.19.3, and the iOS `ShiftToolsToolbar` offers Crew Clock to it — but `roles.py`
never created the role or granted it anything, so a site that wanted one had to
build it by hand and `create_mobile_user` refused to enrol one at all.

**It is not a second Foreman.** The crew lead runs the *shift* — who is clocked
on, the breaks called, the heat record — because OAR 437-004-1131 puts the water,
shade, rest-cycle and observation obligations on the supervisor who was standing
on the block, and `end_shift` writes one submitted Attendance per crew member, so
a crew lead who cannot close is a crew with no wage record for the day.

They do **not** run the *board*. Raising, assigning and cancelling work stay the
Foreman's — the same separation this app keeps between a Compliance Officer and
the dispatch register. On a site where the crew lead *is* the foreman, give them
Foreman; this role is the board-less half of it.

| | Field Worker | Crew Leader | Foreman |
|---|---|---|---|
| Farm Shift | read | **full** | full |
| Farm Task | read | read | full |
| Farm Task Assignment | full (their own) | read | full |
| Container Fill Threshold | read | read | read/write |
| Bucket Log Session / Entry | — | read | read |
| Compliance calendar, SOP library | — | — | read |
| Desk access | no | no | yes |

### The permission adjustments inside the existing roles

* **Field Worker gains read on `Container Fill Threshold` and
  `Fill Threshold Change Log`.** `update_fill_threshold` is Foreman-and-above,
  which is right — and nothing granted the person *applying* the band so much as
  a read, so a handset showed a number it had no permission to fetch and the
  acknowledgment loop asked people to confirm a number they could not see. They
  still may not move it: a checker who could change the number they are asked to
  trust is the exact shape that gate exists to prevent.
* **Field Worker does not gain the bucket log**, and that is deliberate in the
  direction that looks unhelpful. A Bucket Log Entry is a piece-rate count, which
  is somebody's pay — and a User Permission scopes by *company*, not by person,
  so a picker granted read there could read the whole crew's day. A worker's own
  count comes back through their own session on the mobile surface, where the
  caller is resolved to an Employee first.
* **Foreman and Crew Leader gain the bucket log, read-only.** The rows are
  written by `sync_bucket_entries` running as the MCP System User, so a write
  grant would describe an editing path nobody takes.
* **Farm Manager gains nothing here**, because all four of those doctypes already
  ship a standard DocPerm giving Farm Manager read/write/create. A grant in
  `roles.py` would have been a silent no-op *and* a lie — `describe_role` reads
  that tuple, so the catalogue would have advertised read-only on a register the
  role can already write.


## The SOP library, and the procedures that are not in it (v0.93.0)

### `get_policy_coverage`

Read-only, on by default. **The only read over this register that is about the
policies which do not exist.**

`list_compliance_policies` counts them, groups them by category and flags the
ones overdue for review. `get_compliance_policy` walks one version chain. Neither
can report an absence, for the ordinary reason that the absent records are not in
the register — and "zero compliance policies registered, audit packets have empty
policy sections" is a statement about absence, so nothing in the app could say
it.

**The expectation is read off the packet types rather than invented here.** Every
`AuditPacketType` already declares the `policy_categories` its packet pulls; that
declaration is what keeps a GLOBALG.A.P. procedure out of a DOL packet. This
reads the same declaration backwards: a category a regime pulls, for which this
company has no policy in force, is a section that packet will produce short.

| Field | What it says |
|---|---|
| `regimes[].expected_categories` | What that audit type's packet pulls |
| `regimes[].missing` | Expected, and no policy in force on `as_of` |
| `regimes[].covered_without_a_document` | Covered on paper only — see below |
| `regimes[].coverage_percent` | Covered over expected |
| `categories_with_no_policy` | The union across every scored regime |
| `work_list[]` | One row per missing category, with the `create_compliance_policy` call that fills it and which regimes are waiting on it |

**Coverage is active-and-effective, not merely present.** A Draft was never
adopted and a policy effective next month was not in force today, so neither
covers anything.

**A covered category whose policy has no document attached is reported
separately, never as a gap.** The procedure existing on the record and the
procedure existing are different problems that need different fixes, and folding
them together would send somebody off to write an SOP that is already written.

**A regime that names no categories reports as unscored.** `GlobalGAP` and
`Other` pull every policy of every kind, which makes it impossible to say what is
missing for them. Their rows say so. Answering "nothing missing" for a scheme
because nobody wrote its category list into this app would be the most flattering
possible lie, and the one this table would most reward.

### Registering an SOP is now one call

`create_compliance_policy` and `update_compliance_policy` take `file_content`
(base64) and `file_name`. The document is attached to the policy **and written
into `attached_document`**, which is the field the audit packet and the Desk form
both read.

```
create_compliance_policy(
    policy_name="Harvest Hygiene SOP",
    category="Harvest Hygiene",
    company="Orchard Meadow, LLC",
    version="v3",
    effective_date="2026-03-01",
    review_due_date="2027-03-01",
    policy_owner="…@…",
    file_content="<base64 of the PDF>",
    file_name="Harvest Hygiene SOP v3.pdf",
)
```

For a document too large for one JSON call, upload it with `stage_file_chunk` +
`commit_staged_file` and pass `attached_document` instead.

**Why this was worth changing.** It could always be done in two calls —
`create_compliance_policy` then `attach_file_to_document` — and that is how the
register filled up with policy records asserting procedures nobody had uploaded.
`_policy_notes` has said what such a record means since it was written ("this
record asserts that a procedure exists, which is not the same as a procedure
existing"); saying it is not the same as making the right thing the easy thing.

### A policy could have its document and be reported as having none

`attach_file_to_document` is the generic door and knows nothing about which of a
DocType's fields is meant to hold a document, so it created the File and left
`attached_document` empty. Everything downstream read that one field — so a
policy with the SOP genuinely attached came back `has_document: false`,
`without_a_document` listed it, and the audit packet printed a written procedure
as an unsupported claim.

`has_document` now falls back to the File table, batched in one query for the
whole register. The attach field stays authoritative when it is set;
`document_source` says which of the two answered.


## The mock recall, in both directions (v0.93.0)

### `trace_backward` · `trace_forward`

Read-only, both on by default. They write nothing operational.

Every critical tracking event the FSMA Food Traceability Rule asks for has been
recorded since v0.44.0. `Bucket Log Entry` carries `crew_id`, `block_id`,
`bin_id` and `shipment_id`, and `compliance_fields.py` states the intent of each
in the field definition itself — *"the block is where the lot came from, and it
is the join to the spray record, which is how a residue question becomes an
answerable question"*, and *"a buyer's mock recall is timed, and an operation
that cannot answer in four hours fails the audit."*

**The data threaded. Nothing walked it.** Answering "which blocks are in this
lot" meant filtering captures by hand, collecting the block ids, opening the
spray register, filtering that by block and by date, and writing the result on
paper — a four-hour answer to a four-hour question, done by the one person who
knows where everything is, on the day a buyer calls.

### They are not one question with a flag

| | `trace_backward` | `trace_forward` |
|---|---|---|
| Asked when | the PRODUCT is suspect | the SOURCE is suspect |
| Triggered by | a customer complaint, a residue detection, a positive test at the packing house | a spray at the wrong rate, a water test that came back positive, a flooded block |
| Start from | `shipment`, `bin`, `scale_ticket`, `settlement`, `bucket_entry` | `block`, `spray_application`, `water_test` |
| Ends at | blocks, crews, pickers, days — then the sprays and water those blocks were given | bins, shipments, settlements, invoices — and `customers_to_notify` |

The starting point is several arguments rather than a doctype-and-name pair
because the person asking is holding **one** thing and which thing depends
entirely on who telephoned them. Asking them to say `from_doctype="Trade
Shipment"` is asking them to know this app's register names during the one hour
when nobody has time to look them up.

### The date bound is the whole value of a forward trace

From a spray or a water test, `trace_forward` takes only what was picked **after**
that record. A recall naming three seasons of fruit because one tank went out in
April is a recall nobody can act on, and an operation that issues one is an
operation whose next recall is not believed.

From a bare block it takes everything **and says so** — unbounded is a legitimate
question and a different one, and the two must not be silently confused. Pass
`date_from` to bound it by hand.

`trace_backward` applies the mirror bound: applications are cut at the **last
capture**, because a pass made after the fruit came off did not reach it, and
naming it sends somebody to investigate a tank that was never on that crop.
Planned and Cancelled passes are excluded from both — neither put anything on the
ground.

### Every break is named, and that is the point

| Break | What it means |
|---|---|
| `unlinked_counts` | captures in this lot carrying no block / crew / bin / shipment id, **per column**. The number that turns "our traceability is fine" into a fact |
| `unresolved_block_ids` | a `block_id` matching no `Field`. The spray and water history covers only what resolved, so the answer is INCOMPLETE and says so |
| `unresolved_shipment_ids` | a `shipment_id` matching no `Trade Shipment`. Free text against a register with its own names — the fruit left and the register cannot say to whom |
| `breaks[].missing == "customers"` | **nobody to telephone.** No route reaches a customer, so the recall cannot be executed from this system at all |

The last one is a break rather than an empty list on purpose: an empty
`customers_to_notify` and a complete one look identical to anybody skimming.

### What it will not do

It invents no link the site did not record. `bin_id` is free text, two bins
called "17" in two seasons are two different bins, and this walks the ids that
were actually stored within the window it was asked about rather than guessing
which "17" was meant.


## The one screen an owner opens (v0.93.0)

### `get_owner_dashboard`

Read-only, on by default. **No `available` gate** — it composes seven sources and
reports the ones that could not answer, so gating the whole read on any single
register would contradict what it exists to do, and would take the dashboard away
from exactly the farm that most needs to see what is missing.

Every number on it already existed and each lived behind its own call — crews in
`list_shifts`, harvest in the bucket captures, compliance in
`get_audit_readiness`, the camp in `get_housing_capacity`, money in
`compute_all_kpis`, weather on the shift's own readings, work waiting in
`list_pending_approvals` and `list_dispatch_board`. Seven calls, seven shapes,
and no answer to the only question an owner asks at six in the morning: **is
anything wrong today.**

### `attention` is the product; everything else is its evidence

A ranked list of what is wrong right now, each row carrying `severity`,
`section`, `headline`, `detail`, `count` and **`read_it_with`** — the tool that
answers that row in full. Ranked by severity rather than by section, because an
open Critical compliance alert and a KPI two percent off target are not two items
on one list, and presenting them as though they were is how somebody learns to
stop reading a dashboard.

**Nothing invents a threshold.** Every severity is read off the record that
raised it — a Compliance Alert's own `severity`, a KPI definition's own bands.
This tool decides *order*, never *gravity*. The test for that changes an alert's
severity and watches the ranking follow.

### Sections

| Section | Source | What it raises |
|---|---|---|
| `crews` | `list_shifts` | a shift opened before today and still open — `end_shift` writes the Attendance, so an unclosed shift is a day of wages with no record behind it |
| `harvest` | `list_bucket_entries` | captures whose badge resolved to no employee — those buckets pay nobody |
| `compliance` | `get_audit_readiness` | open Critical and Warning alerts, at their own severity |
| `sop_coverage` | `get_policy_coverage` | SOP categories with no policy in force |
| `camp` | `get_housing_capacity` | the two backlogs **separately** — habitability walks and detector tests are different errands |
| `financial` | `compute_all_kpis` | KPIs past their own thresholds, carrying the definition's own message |
| `weather` | the open shifts' own readings | open shifts with **no** reading at all |
| `approvals` · `dispatch` | `list_pending_approvals`, `list_dispatch_board` | documents parked, open Criticals, and tasks sitting in the pool |

### An unavailable source is not a clean one

This is the failure this read must never produce: **a dashboard showing no
compliance alerts because the compliance source refused looks exactly like a farm
with no compliance alerts.** So `sections_reporting` and `sections_unavailable`
are both returned, `unavailable[]` carries the reason each source gave, and the
summary counts both.

A source that refuses is never fatal. The dashboard composes tools that each
enforce their own role, so a caller holding some of those roles gets the sections
they may see and a named refusal for the rest — rather than an error page, which
is a dashboard nobody opens twice.

### Weather comes off the shift, not off the internet

`fetch_weather_now` exists and is deliberately not called here. It writes a
reading, it refuses a closed shift, and a dashboard that reached Open-Meteo on
every render would put an outbound HTTP request on the path of a screen somebody
leaves open. The scheduled sweep already collects readings every fifteen minutes
onto the open shifts; this reports the most recent of them **with its own
timestamp and `source`**, so an hour-old reading cannot be read as a live one.


## Single-pass onboarding (v0.93.0)

### `onboard_employee` — three steps that were somebody's next four calls

Hire → assign department → assign housing → assign crew → do the W-4 was five
calls. It is one now.

| Argument | What it does | Delegates to |
|---|---|---|
| `w4` | files the withholding **elections** as a W-4 Form | `submit_w4` |
| `housing_unit` · `housing_assigned_date` | creates the assignment, not just the orientation task's target | `create_housing_assignment` |
| `shift` · `crew_role` · `crew_joined_at` | rosters them onto an open crew | `add_worker_to_shift` |

`department`, `designation`, `employment_type` and `branch` have been here since
v0.54.0 and go through `create_employee`'s allowlist.

**The scan is not the elections.** `documents["w4"]` attaches a signed page;
`w4` files the filing status, the dependent counts and the extra withholding
that the payroll engine actually reads. A farm that attached forty scans still
had forty people in `list_employees_missing_w4` and forty pickers withheld at the
default. Both are kept — the page is what an examiner asks to see, the record is
what the engine computes from — and omitting `w4` is **reported in `skipped`**
rather than passing quietly.

**Dates default backwards, not forwards.** The W-4's tax year comes off
`date_of_joining`; so does the housing assignment's start. Somebody hired on
Monday and onboarded on Wednesday slept somewhere on Monday night.

**Every new step delegates**, so the housing overlap refusal, Oregon's lawful
occupancy, and the refusal to roster somebody onto a second open shift are the
same code they are everywhere else. Each may fail without undoing the rest: the
reason lands in `skipped` and the step name in the new **`incomplete`** list,
beside the Employee that was still created.

**`next_step` appends the W-4 rather than replacing the enrolment step.** Two
different kinds of gap — one is a missing capability, the other is a wrong number
— and the field has meant "the next step towards a working phone" since the tool
shipped.


## Losing the phone (v0.93.0)

### `recover_mobile_access`

MUTATING (off). Every mechanical piece already existed — `revoke_api_token` says
in its own result that it is *"they lost their phone"* — and a manager holding a
lost-phone report still had to do three things in the right order, keyed on a
value they usually do not have.

1. **They do not know the login.** A foreman knows a face and a badge. Every
   other tool here takes `user`, which is an email on a system the worker has
   never signed into from a keyboard.
2. **The order matters and nothing enforced it.** The phone is in somebody
   else's pocket right now.
3. **Nothing asked who it was for.** `generate_api_token` mints a credential for
   whatever login it is given — right for a tool an administrator drives, wrong
   as the whole of an account-recovery path, because the request arrives as
   somebody at a farm office *saying* they are somebody.

### The badge is the identity proof

A badge is a physical card the worker still has when the phone is gone. `badge`
resolves through the same register a crew clock reads, so a retired card, an
unknown card and a card belonging to somebody who has left stay three different
refusals. Naming an `employee` or a `user` **as well** makes the two check each
other, and **a badge that resolves to somebody else stops the reset** — that is
either the wrong card or the wrong person, and neither ends in a working
credential.

**The no-badge path is not refused, it is recorded.** Somebody who lost the phone
*and* the card is an ordinary Tuesday, and a recovery tool that could not serve
it is one a farm routes around. `identity_verified_by` comes back as `badge` or
`manager assertion`, and the second is written onto the grant's notes and into
the audit row — a fact about how much this reset is worth, rather than something
to be inferred from an absent argument.

### It revokes before it mints

Minting first would leave the old credential live for as long as the second step
took, and forever if the second step never happened. A failure after the
revocation leaves the account with **no** credential, which is the safe side of
that trade — and `test_the_revocation_happens_before_the_mint` makes the mint
fail on purpose to prove it.

Arguments are validated **before** anything is destroyed, though: nothing about a
typo in `expiry_days` requires a working credential to have been killed first.

### The Employee record is never touched

Not re-created, not duplicated, not re-onboarded. Their badge, shifts, buckets,
housing, I-9 and W-4 hang off a docname that does not change here — the
difference between recovering an account and hiring somebody twice, and only one
of those puts a person on the dispatch board twice and in the payroll register
once.

Somebody with **no login at all** is refused and pointed at
`onboard_employee(employee=…)`, which reuses the same Employee record for exactly
this reason.

`reason` is required and has a length floor, because that row is the audit trail
for destroying somebody's credential and issuing another.

---

# v0.98.0 — Bin sealing, and the chain from a pack line back to a crew

## What this answers

A bin leaves the orchard on a trailer carrying a **tag** and nothing else. Every
question anybody asks about it afterwards is about the hour before it was closed:

| the question | what it needs |
|---|---|
| a residue detection at the packing house | which block, and was that block inside a re-entry interval |
| a piece-rate dispute | whose buckets, and how many each |
| a food-safety hold | which **other** bins the same crew filled that afternoon |
| a heat-exposure question | which shift, and therefore which weather timeline and which break log |

None of them is reconstructable once the crew has gone home. The buckets are
tipped and mixed, the badge scans exist only on the handset, and the tag points
at nothing. So the record is written at the moment of sealing, by the person who
closed the bin — which is also the only moment anybody knows the answer.

## `seal_bin`

**MUTATING (default OFF).** Also reachable from a handset at
`POST /farmops/api/mobile/seal_bin`. Required: `bin_tag`, `bucket_count`.

```json
{"bin_tag": "OML-4471", "bucket_count": 42,
 "contributors": [{"badge_id": "B-0117", "buckets_contributed": 18,
                   "first_scan_at": "2026-08-18 09:14:02", "last_scan_at": "2026-08-18 10:41:55"},
                  {"employee": "HR-EMP-00031", "buckets_contributed": 21}],
 "shift": "SHIFT-2026-0114", "field": "Mill Creek Block 4",
 "gps_lat": 45.9327, "gps_lon": -118.3877,
 "sealed_by": "HR-EMP-00008", "client_event_id": "8f2c…"}
```

```json
{"name": "BIN-2026-00017", "bin_tag": "OML-4471", "bucket_count": 42,
 "contributor_count": 2, "buckets_attributed": 39, "unattributed_buckets": 3,
 "h3_hex": "8a2a1072b59ffff", "already_sealed": false, "manual_tag": false}
```

**Idempotent on `client_event_id`,** which is the handset's own identifier for
one sealing action. A phone that sealed a bin and did not hear the answer sends
the same call again — over a funnel, in a canyon, on a dying battery this is the
ordinary case — and a second record of one bin is a doubled count at the pack
line and a doubled piece rate on somebody's cheque. A retry gets the same seal
back with `already_sealed: true`.

**Contributors arrive as Employee docnames, as badge strings, or as objects.**
All three land on one row shape. Duplicates are **merged**, not refused:
somebody who came back with a second bucket is one row with the buckets added up
and the scan window widened. A badge that resolves to nobody is **reported and
the bin is still sealed** — it comes back on `unresolved_badges`, because a
record with a gap in it is worth more than no record, and a bin refused over an
unregistered card is a bin nothing can trace at all.

**The two counts are allowed to disagree.** `bucket_count` is what the checker's
tally read; the contributors' `buckets_contributed` is what the badge scans
attributed. This app never reconciles them, because a bucket tipped by somebody
whose badge did not scan is *in the bin and not in the rows* — which is exactly
the fact a piece-rate dispute turns on. `unattributed_buckets` names the
difference instead.

## `get_bin_seal`

**READ (default ON).** One seal in full, contributors included, with
`unattributed_buckets` and the sentence explaining it where it is non-zero.

## `list_bin_seals`

**READ (default ON).** The register by `shift`, `field`, `block`, `bin_tag`,
`bucket_session`, `sealed_by` or a date range, newest first. Contributors are
deliberately **not** on this answer — one bin's crew is a child table, and forty
bins would be forty reads of it.

## `trace_bin`

**READ (default ON).** The read this whole feature exists for.

```json
{"bin_tag": "OML-4471"}
```

```json
{"bin_tag": "OML-4471", "matches": 1, "name": "BIN-2026-00017",
 "sealed_at": "2026-08-18 10:52:31", "sealed_by": "HR-EMP-00008",
 "sealed_by_name": "Ana Reyes", "field": "Mill Creek Block 4",
 "shift": "SHIFT-2026-0114", "bucket_count": 42,
 "contributors": [{"employee": "HR-EMP-00017", "employee_name": "…",
                   "buckets_contributed": 18, "first_scan_at": "…", "last_scan_at": "…"}],
 "unattributed_buckets": 3}
```

**It takes the tag, not a docname,** because nobody standing at a pack line has a
docname.

**It answers with every seal carrying the tag,** newest first, and `matches` says
how many. Bin tags are reused between seasons and between growers, so this app
does not make `bin_tag` unique: a uniqueness constraint would refuse the **second
true record** rather than the first false one. Where there is more than one,
`ambiguous` lists them all with their dates and blocks, and the sentence says so.

**A tag with no seal behind it is a refusal that says so plainly.** It is a break
in the chain rather than a lookup failure — nothing records which block that bin
came from and nothing can reconstruct it now. The refusal points at
`list_bin_seals(shift=…)`, which is usually how a mis-keyed tag is found.
## FSMA 204: lot codes and critical tracking events (v0.111.0)

Nine tools, and they sit **beside** the trace tools above rather than replacing
any of them. `trace_forward`, `trace_backward` and `trace_bin` keep their
arguments, their answers and their names exactly.

### Why a farm that already traces needs this

The three tools above walk a chain of **free-text ids** — `block_id`, `crew_id`,
`bin_id`, `shipment_id`, written on a bucket capture by whoever was holding the
phone. That chain is the honest record of what the site stored, and it has three
properties the Food Traceability Rule will not accept:

* **It is not an identifier.** Two bins called "17" in two seasons are two bins.
  A regulator asking "produce the records for lot X" is asking about *one* lot.
* **It does not survive a hand-off.** The buyer's portal, the packing house and
  the carrier do not have this site's bucket captures.
* **It does not survive a transformation.** Four field lots combined into one
  pallet destroy the join, and nothing in the free-text chain records which four.

So this release adds the thing the rule actually requires — a **traceability lot
code**, assigned once, unique on the site, printed on the fruit — and a
**Critical Tracking Event** register that *indexes* the records already here
under it. Nothing is copied: a CTE says "Spray Application SP-0041 is a Growing
event in lot YC3-BING-20260821-01", and the spray's own record remains the only
place its products, rates and weather live.

| | |
|---|---|
| `create_traceability_lot` | Assigns a lot code. Mutating, ships **off**. |
| `get_traceability_lot` | One lot with every event filed against it. Read. |
| `list_traceability_lots` | The register. Read. |
| `record_cte` | Files one critical tracking event. Mutating, ships **off**. |
| `trace_lot_forward` | Downstream lots and their destinations. Read. |
| `trace_lot_backward` | Upstream lots, blocks and the spray register. Read. |
| `recall_drill` | The twenty-four-hour answer, as a document. Read. |
| `get_lot_timeline` | The events in order, with the referenced records. Read. |
| `index_lot_events` | Attaches what the site already holds. Mutating, ships **off**. |
| `index_scouting_observations` | Completed scouting tasks become Crop Observations. Mutating, ships **off**, idempotent. |

### Three DocTypes, and one of them is an edge

**Traceability Lot Code.** `lot_code` is the docname — `autoname: field:lot_code`
— because a lot code is read off a bin, said down a telephone and typed into a
buyer's portal by somebody who has never seen this site, and a Link to a hash
would mean resolving a name nobody holds. It is **unique**, which is the opposite
of the decision `Bin Seal` takes about `bin_tag` and right for the opposite
reason: a bin tag is somebody else's sticker and is genuinely reused, and a lot
code is assigned here, once, by this app.

**Traceability Lot Source.** The child table on the lot naming the lots that went
*into* it. This is the transformation edge and the only reason a trace is ever
more than one hop — a pack line combining four field lots into a pallet has
destroyed the join unless somebody wrote down which four.

**Critical Tracking Event.** The rule's five event types — Growing, Receiving,
Transforming, Creating, Shipping — each carrying who, when, where, how much, and
where from and to. `reference_doctype` / `reference_name` are `Data` rather than
a Dynamic Link, deliberately: an event may name a register this site does not
have, and a Dynamic Link would either refuse the event or vanish it. An
unresolved reference is **reported as the data fault it is** rather than dropped.

## `create_traceability_lot`

**MUTATING (default OFF).** `{block}-{variety}-{YYYYMMDD}-{sequence}`.

```json
{"field": "Yellow Camp Block 3 - MC", "variety": "Bing", "harvest_date": "2026-08-21"}
```

```json
{"lot_code": "YC3-BING-20260821-01", "status": "Active", "already_existed": false,
 "opening_event": {"name": "CTE-2026-00001", "created": true, "event_type": "Creating"}}
```

**Idempotent on `(field, variety, harvest_date, company)`.** A retry gets the same
lot back with `already_existed` set. Two codes for one afternoon's fruit split a
recall in half and neither half names the other; pass `allow_duplicate` where the
block genuinely produced two lots that day.

**A lot with no `field` is accepted only with `source_lots`** — that is a
transformation lot, whose block is whatever its sources name. Asserting one on
the pallet would be a guess that a backward trace then reports as fact.

**The block segment comes from `Field.block_number`** where the register has one,
because that is what the crew and the packing house both say. A lot code built
out of a forty-character Field docname is one nobody reads aloud correctly.

## `get_traceability_lot`

**READ (default ON).** One lot, its source lots, and every event filed against
it. A lot with no events, or with no block *and* no source lots, comes back with
that named in `breaks` — the records may well exist and simply not be indexed,
and an empty list would read as a clean bill.

## `list_traceability_lots`

**READ (default ON).** By `field`, `variety`, `status`, `harvest_shift`,
`planting_season`, `company` or a `date_from`/`date_to` range. Newest harvest
first. Events and source lots are deliberately not on the answer: both are
per-lot reads, and forty lots would be eighty of them.

## `record_cte`

**MUTATING (default OFF).** One critical tracking event.

```json
{"lot_code": "YC3-BING-20260821-01", "event_type": "Shipping",
 "reference_doctype": "Trade Shipment", "reference_name": "TSHIP-2026-0009",
 "destination_location": "Hood River Cold Storage", "receiver": "Columbia Packing Co",
 "carrier": "Nordby Transport", "quantity": 42, "quantity_uom": "bin"}
```

**The event is a pointer, not a copy.** Copying the shipment's weight in here
would create a second version of a fact that can drift from the first, and the
drifted one is always the one somebody reads.

**It never changes the lot.** A Shipping event does not decrement `quantity` and
does not set `status` to Shipped — those are two measurements taken by two
people, and an event that silently rewrote the lot would make the lot disagree
with its own history with no way to tell afterwards which one somebody meant.

**Idempotent on `(lot, event_type, reference_doctype, reference_name)`.** An
event with *no* reference is never deduplicated: two hand-entered Shipping events
on one lot are two loads that left.

## `trace_lot_forward`

**READ (default ON).** The transformation graph walked downwards, then every
Shipping event in that closure.

```json
{"lot_code": "YC3-BING-20260821-01"}
```

```json
{"downstream_lots": [{"lot_code": "PACK-MIXED-20260823-01", "…": "…"}],
 "transformation_edges": [{"source_lot": "YC3-BING-20260821-01",
                           "lot_code": "PACK-MIXED-20260823-01", "depth": 1}],
 "destinations": [{"destination": "Hood River Cold Storage",
                   "receiver": "Columbia Packing Co", "carrier": "Nordby Transport",
                   "shipped_at": "2026-08-23 14:10:00"}],
 "counts": {"downstream_lots": 1, "shipping_events": 1, "destinations": 1,
            "unplaced_shipments": 0},
 "breaks": []}
```

**Not the same tool as `trace_forward`,** which takes a block, a spray or a water
test and walks the free-text bucket chain to the settlements and invoices. Both
are correct; they answer different questions from different evidence. Bolting a
`lot_code` argument onto the older tool would have made every existing caller's
description a lie about what it now does.

**It reports the fruit it cannot place.** A Shipping event naming neither a
destination nor a receiver is product that left and cannot be traced to anybody;
that count is `counts.unplaced_shipments`, and the honest scope of the recall is
wider than the destination list. A lot with **no** Shipping event at all is a
break, not an empty list — "it has not left" and "nobody recorded where it went"
are different answers and only one of them is good news.

## `trace_lot_backward`

**READ (default ON).** Source lots, their sources, the blocks at the roots of the
chain, and then the **spray register** — bounded at each root lot's own harvest
date, because a pass made after the fruit came off did not reach it. Planned and
Cancelled passes are excluded: neither put anything on the ground.

A chain that reaches no block is reported as a break. It ends at a lot code and
cannot reach the spray register at all, which is the one hop a residue question
is asked through.

## `recall_drill`

**READ (default ON).** `trace_lot_forward` written as the document somebody reads
at eleven at night.

```json
{"parties_to_notify": [{"party": "Columbia Packing Co",
                        "destination": "Hood River Cold Storage",
                        "carriers": ["Nordby Transport"],
                        "lot_codes": ["PACK-MIXED-20260823-01"],
                        "first_shipped_at": "2026-08-23 14:10:00",
                        "last_shipped_at": "2026-08-23 14:10:00"}],
 "lots_affected": ["YC3-BING-20260821-01", "PACK-MIXED-20260823-01"],
 "counts": {"lots_affected": 2, "parties_to_notify": 1, "unplaced_shipments": 0}}
```

**It writes nothing and sets nothing to Recalled.** A drill is run on fruit
nobody is worried about — that is what makes it a drill — and a read that changed
a status would make the rehearsal indistinguishable from the event.

**Readiness is a count, never a verdict.** This app computes no opinion about
whether an operation is compliant. Where no party can be named at all,
`scope_warning` says so in as many words: *do not read this as a clean bill.*

## `get_lot_timeline`

**READ (default ON).** The events in the order they **happened** — not the order
they were written, because a phone that posts an afternoon of events when the
signal comes back would otherwise produce a timeline reading in the order the
bars returned — each with `reference_detail`, the register row it points at, read
live.

A pointer that resolves to nothing is reported rather than dropped, and the two
ways it can fail are told apart: a register this site does not have is a
different problem from a record that was deleted, and only one of them means
evidence went missing.

## `index_lot_events`

**MUTATING (default OFF), idempotent.** How an operation gets from nothing to a
working lot register in one call, and how a season of history is indexed a week
at a time.

```json
{"date_from": "2026-08-17", "date_to": "2026-08-23"}
```

Over the window it does three things, in order:

1. **Bin Seals become lots and Receiving events.** A seal names a block, a crop
   and a day, which is exactly a lot; where none covers that combination one is
   created.
2. **Scale Tickets become Shipping events.** A ticket is a load weighed onto
   somebody else's scale — the grower's fruit arriving at the packer — so its
   `customer` is the receiver and its `destination` is where the fruit went.
3. **Spray Applications become Growing events,** on every lot whose block the
   pass reached on or before harvest.

**It is a tool and not a `doc_events` hook.** `hooks.py` promises this app
installs no document hooks and `test_hooks.py` fails the build over one;
`tools/itgc.py` settled the identical question the identical way. Running the
indexing here means an operator can see it, switch it off, and re-run it over
last season.

**It does not index Trade Shipments.** A Trade Shipment carries no lot column,
and this release adds no field to a doctype it would then have to keep. Guessing
a shipment's lots off a date would put fruit on a truck it was never on — use
`record_cte`, one call per shipment, naming the lots deliberately.

**What it skips is reported.** Bin seals with no `field` cannot be filed under a
lot, because a lot code *is* a block and a day; scale tickets matching no lot are
loads that left with nothing here to say which fruit they were. Both counts are
in `skipped`, with the sentence that says how to close each.

## The break horn reaches the crew (v0.99.0)

`BreakAlarm` on the handset plays a tone the instant a foreman calls a break, over
an `AVAudioSession` in `.playback` — so it **rings through the silent switch**,
deliberately, because a break horn that respects a muted phone does not work on
most of a crew's phones. That is exactly one phone: the one the break was called
on. Every other worker on the shift learned about the break when somebody
shouted.

Two mobile routes and two tools close that.

| | |
|---|---|
| `POST /farmops/api/mobile/register_push_token` | The handset enrols its APNs device token. Called on **every** login and launch. |
| `POST /farmops/api/mobile/unregister_push_token` | Logout retires it. A soft delete. |
| `list_push_tokens` | The register, with this bench's own APNs state beside it. Read. |
| `send_test_push` | One notification to one worker's handsets. Mutating, ships **off**. |

### The device is the identity; the token is not

Apple issues a new device token when the app is reinstalled, when a phone is
restored from a backup, when it is migrated to new hardware, and periodically for
no reason the client is told. A register keyed on the **token** accumulates one
live row and five dead ones per handset, and every crew push becomes five wasted
requests and one delivery — with no way to tell from the register which was
which.

So a **Mobile Push Token** is keyed on `platform::device_id`, unique, and the
token is a mutable field on the row that key finds. `register_push_token` is an
upsert: same device, same row, whatever token it is presenting today.

### Neither route takes a subject from the body

A phone enrols **itself**. `user` and `employee` are resolved from the caller's
own login and are not arguments, so `routes.bind` has nothing to drop and no body
can point a registration at a colleague — which would be a way to have another
worker's break horns, heat alerts and dispatch pings delivered to a handset of
your choosing. The same shape the three direct-deposit routes have, for the same
reason.

`unregister_push_token` does not take a `token` either, and that absence is
load-bearing: a logout that had to present the current token would fail exactly
when it matters most, and a phone whose token Apple rotated between login and
logout would go on receiving another shift's break horns forever.

### A crew break pushes; an individual break does not

`log_shift_break` and `end_shift_break` push to every worker still on the shift
when `applies_to` is `Crew`. A break covering one named worker is not news to the
other nineteen, and a tone that rings through a silent switch is not a thing to
send to somebody it is not about. A worker who has clocked out is excluded: their
phone is elsewhere.

The payload names the sound, and the two are deliberately unlike each other and
unlike any system sound — a rising double blast and a descending triple pip, both
already bundled in the app:

```json
{"aps": {"alert": {"title": "Break time", "body": "Paid Rest starting now — 10 minutes."},
         "sound": "break_start.caf",
         "interruption-level": "time-sensitive",
         "category": "FARM_BREAK"},
 "break_kind": "Paid Rest", "phase": "start", "shift": "SHIFT-2026-0001", "event": "…"}
```

`interruption-level: time-sensitive` is the point of the payload. A break horn is
worthless if Focus or a Scheduled Summary holds it until lunchtime.

### It degrades to nothing, and that is the design

The p8 signing key is an operator artefact that does not exist on this bench yet.
Until it does, **every break is logged exactly as before and no push is
attempted** — the break record is the compliance evidence under OAR 437-004-1131,
and a convenience on top of it must never be able to cost it. `log_shift_break`
answers with a `push` block saying which of those happened:

```json
{"shift": "SHIFT-2026-0001", "crew": 8, "tokens": 2, "sent": 0,
 "failed": 0, "skipped": 2, "reason": "not_configured"}
```

`crew` and `tokens` are separate numbers on purpose: eight workers with two
tokens is six people who never enrolled a phone, which is a different
conversation from a push that failed.

Configure it in the site's `site_config.json` — not on a Settings doctype, which
any Desk role can read and a dozen Frappe debug paths dump in full:

```json
{"apns_key": "/home/frappe/keys/AuthKey_ABC1234567.p8",
 "apns_key_id": "ABC1234567",
 "apns_team_id": "TEAM123456",
 "apns_topic": "farm.fafo.scanclock",
 "apns_environment": "production"}
```

All four are required together. A push missing any one of them is rejected by
Apple with a 403 that says nothing useful, which is a worse failure than not
sending. `apns_key` takes either the PEM text or a path to it.

### Apple's word retires a token, and nothing else does

`Unregistered` and `BadDeviceToken` deactivate the row on the spot — the app was
deleted, or the string is not a token for this topic and environment.
`TopicDisallowed`, `TooManyRequests` and the 5xxs deliberately **do not**: those
say something about this farm's configuration or about Apple, and treating them
alike would unsubscribe a whole crew over one wrong line in `site_config.json`.

Nothing is ever deleted. `is_active` retires a row, and `last_error` says why —
"this phone stopped receiving, on this date, for this reason" is the fact
somebody needs when a worker reports that the horn never reaches them.

`SERVER_CHANGES.md` item 16.

### A dispatch reaches the worker's phone, not just their task list (v0.107.0)

A task a foreman dispatched appeared in `list_my_tasks` and nowhere else, so the
foreman's half of the dispatch was instant and the worker's half was whenever
they next happened to open the app — which on a picking crew is at lunch.

`assign_farm_task` now pushes to the assignee's own handsets and answers with a
`push` block in the same shape the break horn uses:

```json
{"employees": 1, "tokens": 1, "sent": 1, "failed": 0,
 "skipped": 0, "deactivated": 0, "reason": "sent"}
```

```json
{"aps": {"alert": {"title": "New task", "body": "Habitability walk — MC-Cabin-01 at MC-Cabin-01 — Urgent"},
         "sound": "default",
         "interruption-level": "active",
         "category": "FARM_TASK"},
 "task": "TASK-2026-0007", "task_name": "…", "location": "MC-Cabin-01",
 "urgency": "Urgent", "phase": "assigned"}
```

- **`claim_farm_task` does not push.** That is somebody taking work off the board
  with the app already open in their hand; a notification for something they just
  did is noise.
- **It rings the assignee and nobody else.** A crew buzzed about every job
  somebody else was sent to stops reading any of them.
- **A reassignment says so** — `"title": "Task reassigned to you"` and
  `"reassigned": true` — because being sent to a job and having one taken off
  somebody and handed to you are the same row and different news.
- **It is sent last, after every write — but still inside the transaction.**
  `assign_farm_task` holds a `SELECT … FOR UPDATE` from its first line and Frappe
  keeps it until the request commits, so the send does happen inside the locked
  window. That gap is left open deliberately: closing it needs an after-commit
  hook this app uses nowhere (`log_shift_break` pushes inline too), and the race
  is the milliseconds to commit against push → APNs → handset → a person noticing
  → a tap. Ordering it last means no write can still fail after the send.
- **`apns-collapse-id` is the task docname**, so a job dispatched, taken off
  somebody and given back is one lock-screen row rather than three.
- **A worker with no handset is still dispatched.** `reason: "no_tokens"` rather
  than silence — "they never enrolled a phone" is fixable once somebody sees it.

### A Critical compliance alert reaches the people who can act on it (v0.107.0)

Alerts were raised by a sweep that runs while the farm is asleep and sat on a
calendar until somebody opened it. `refresh_compliance_alerts` now pushes each
newly **raised** alert to every supervisor, and reports how many phones rang:

```json
{"created": 3, "refreshed": 41, "reopened": 0, "auto_dismissed": 2,
 "pushed": 1, "pushed_alerts": 1, "push_suppressed": 0, "push_notes": ["no_tokens"]}
```

`pushed` (handsets rung), `pushed_alerts` (Criticals notified about) and
`push_suppressed` (Criticals past the cap) are three separate numbers because
none is derivable from the others: a bench with no p8 key raises every alert and
rings nothing, so `pushed` can be `0` while `pushed_alerts` is not.

- **Only `Critical`.** `Warning` and `Info` are the calendar working as designed —
  a certificate expiring in five weeks is exactly what a calendar is *for*. A
  full sweep on a real operation refreshes dozens of open items every night, and
  pushing all of them would train every supervisor to swipe erpnext_mcp
  notifications away without looking. The first thing lost when that habit sets
  in is the break horn.
- **On `created` and on `reopened`, never on `refreshed`.** Raising an alert is
  the event; noticing it is still true is not. A reopen means the condition
  resolved and has come back, which is news by construction and rare by
  construction — it takes an auto-dismissal and a recurrence. A **human**
  dismissal is never pushed over.
- **To supervisors, about a worker.** Recipients are everybody holding a role in
  `roles.DISPATCH_ROLES` — the same frozenset `guard.require_dispatch_role`
  refuses on, so the people told about an alert are exactly the people who may
  raise a task for it. The subject employee's *name* rides in the payload
  (`"title": "Critical: Ada Orchard"`) because an expiring I-9 is a fact about a
  worker and an obligation of the employer: the worker cannot act on it and the
  foreman can. Their handset is not addressed.
- **An alert with no company reaches every supervisor.** Frappe's own convention,
  the one `roles.companies_for` states in the same words: an empty scope is every
  company, not none. Most alerts are about the operation rather than a person.
- **At most `MAX_PUSHES_PER_SWEEP` (10) notifications per sweep.** The sibling of
  `RULE_CAP`, for the first sweep on an established farm: an operation installing
  this app in August with four years of camp records has a legitimately Critical
  finding in every cabin, and a foreman whose phone buzzes ninety times at once
  turns the category off — taking the break horn with it. Every alert is still
  **raised**; `push_suppressed` says how many did not ring, so the backlog is
  visible rather than inferred from silence. A Warning never spends one of the
  ten: the cap is checked outside the severity gate, not inside it.
- **The calendar is never at risk.** A sweep survives a push that explodes, a
  bench with no p8 key, and a farm with no enrolled foreman.

### Neither of the two new pushes pierces Do Not Disturb

`interruption-level: time-sensitive` stays the break horn's alone. A break earns
it: stopping work when relief is called is a safety obligation with a clock on
it. A task dispatched for tomorrow morning and an alert raised at two in the
morning do not. Both use `active`, which lights the screen and sounds when the
phone is not silenced and stays quiet in the list until morning when it is — and
a compliance alert uses `apns-priority: 5`, letting Apple pick a moment that
conserves the handset's battery.

A server that overrode a foreman's Focus nightly would be silenced within a
fortnight, and the break horn with it. Neither payload spends one of the two
`.caf` files either: those are learned sounds meaning *stop work* and *resume*,
and spending them on paperwork is how they stop meaning anything.

## The in-app feedback bubble drains (v0.105.0)

A bubble on every screen of the handset app, a form that captures the screen, the
person, the role, the time, the build and the model **for** the worker, dictation
in English and Spanish for a crew working in gloves, and an optional screenshot.
All of it shipped on the phone first. None of it had anywhere to go: there was no
`App Feedback` doctype and no route to file one, so every note the farm has
written since is sitting in a queue on somebody's handset.

| | |
|---|---|
| `POST /farmops/api/mobile/submit_app_feedback` | One note from the bubble. **Deduplicated on `entry_uuid`.** |

There is no MCP tool. The owner's half of the feature is a **list view** on the
doctype — sorted by when Send was pressed, filterable by screen and by role —
which is what `SERVER_CHANGES.md` item 24 asks for and what a Desk list already
does against the columns this writes.

### Publishing this route collects a backlog, not a note

A 404 on this client **parks** a note rather than failing it: it is retried every
six hours, forever, and the attempts are not counted against the give-up bound.
So the first call that answers 200 does not receive one note. It receives weeks
of them in one burst — and receives them **again from the start** if that burst
is interrupted before the phone records the acknowledgements.

That is why the route takes `UPLOAD_LIMIT` rather than `WRITE_LIMIT`, the same as
`sync_bucket_entries` and for the same reason, and why `entry_uuid` is `unique`
on the doctype. A resend is answered with **success and the record already
held** — never a refusal. The app treats any non-2xx as "not filed" and would
queue it again, so a 409 on a duplicate is indistinguishable, from the handset,
from never having built this at all. One worker's considered complaint filed
three times is how a feed becomes noise nobody reads.

### The login on a note is the one that was proved

A shared handset is normal on a farm, so the app sends its own idea of who is
holding the phone. `user` is not on the signature — `routes.bind` drops the body's
copy and `guard.endpoint` injects the authenticated caller — and `employee`,
`employee_name` and `user` on the record are all resolved from that caller.

What the handset claimed is kept in `claimed_employee` / `claimed_employee_name`,
and **only where it disagrees**: two identical columns on every row would hide the
disagreement, which is the only reason the claim is worth storing.

`role` and `designation` are stored as sent and checked against nothing. They are
half of what the owner filters by and the server cannot reconstruct them — one
login holds several roles and only the app knows which hat was on — and nothing
on this site authorises anything off those columns.

### A bad screenshot never costs the note

Not base64, not an image, over the 1 MB ceiling, or a `File` insert that threw:
all four record a reason in `screenshot_omitted` and file the note anyway. A 400
would be a note re-queued and re-sent forever by a handset that will never encode
that JPEG any smaller. The picture is context; the sentence somebody wrote is the
thing. A note too long for the column is shortened and marked, for the same
reason — the controller's cap is a real refusal only on the Desk path, where
whoever hit it can do something about it.

The app reasons the same way at its own end: it drops a capture over its inline
ceiling and sends `screenshot_omitted: "too_large"` instead of holding the note
back, and that reason is stored rather than discarded. "The worker took no
screenshot" and "the screenshot did not fit" are different facts and only the
second one is somebody's bug.

Captures are stored as **private** `File` rows. A screenshot of the app is a
screenshot of whatever roster, wage or task list was on the screen at the time.
The extension is read off the first bytes and the stored filename is composed
from the docname, so `screenshot_filename` and `screenshot_content_type` are not
on the signature — there is nowhere for a caller-supplied name to land.

### Both doors reach the same method

`farmops_api/app.py` now reads `multipart/form-data` as well as JSON. A file part
is base64'd in the **transport** and lands on the key its own part is named, so a
part called `screenshot` arrives as `screenshot` and no handler branches on how
the bytes got there. Nothing about the surface widens: `routes.bind` still reduces
whatever comes out of it to the keys the method declares.

`SERVER_CHANGES.md` item 24.

---

## v0.116.0 — the operational map overlays

Cycle 5, Precision Ag Map Phase 3. `/app/farm-overview` has drawn **shape** since
v0.110.0 — where the ground is and whose it is. Shape does not change between one
morning and the next, and every question a farm asks a map at six does:

> *Which blocks may a tractor go on today? Which are closed to entry, and for how
> long? Which are ready to pick? Where did the water run last night?*

Five registers already held all four answers and not one of them was reachable
from the map. Five tools, one new DocType and three new columns close that. **No
existing tool signature changed.**

### `get_map_overlays` — read-only, default ON

**Arguments:** `company`, `blocks` (array of Field docnames), `layers` (array),
`limit`.

```json
{"company": "Constancy Farms", "layers": ["spray_rei", "equipment_access"]}
```

**Returns** `blocks` and `zones`, each row carrying the layers that were asked
for, plus `layers`, `counts`, `role`, `withheld`, `refused_layers`, `warnings`
and `defaults`.

**The five layers.**

| Key | Subject | Read from | Answers |
| --- | --- | --- | --- |
| `irrigation` | zone | `Asset State Log`, through `Asset Register.irrigation_zone` | how long since the water came off, against this soil's own hours |
| `spray_rei` | block | the `Spray REI` register, via `get_active_rei`'s own reader | which blocks are closed to entry, and for how long |
| `spray_phi` | block | `Spray Application` **and** completed Spray tasks | which blocks may not be picked yet, and when each opens |
| `harvest` | block | the latest `Crop Observation` | growth stage and Brix against the variety's pick target |
| `equipment_access` | block | the two above it | may a tractor or sprayer go on this block |

**Nothing here is a second opinion.** Every layer reads its register through that
register's own owner — `spray_rei.active_for_blocks` runs the expiry sweep before
it answers, and `spray.phi_windows_for_blocks` already consults *both* places a
spray stamps a pre-harvest date and takes the later. A map that recomputed either
would drift from the federal one the first time either was corrected, and the
first anybody would know is a worker in a treated block or a load rejected at the
packhouse.

**Compaction and restricted entry are not the same question** and are never
merged into one traffic light. `irrigation` is about a **machine** on wet ground —
wheel ruts and a compacted pan that outlasts the planting. `spray_rei` is about a
**person** walking in — 40 CFR §170.407 and an employer's duty to keep workers
out. Different rules, different subjects, different consequences.

**`unknown` is a colour and it is never green.** A zone whose valves have never
been scanned has not been proven dry; a block with no scouting round on it is not
"not ready"; a crop with no Brix target does not make every block ripe. Each
comes back `unknown`, in grey, **with the reason** — and the two ways a zone can
have no answer get different sentences, because "no valve names this zone" is a
job in the asset register and "the valves are tagged and none has ever been
logged" is a job in the orchard.

**Every layer dict carries a `colour` in hex** and no client maps a status to
one. Two clients holding their own tables do not diverge loudly — they diverge on
one status on one client, which reads as a block that is simply a different
colour on the phone than on the office screen. Nobody files that; they stop
trusting the map.

**Equipment access is an order, not an average.** A live restriction blocks the
block full stop — driving a sprayer into a treated block is an entry and the
operator is a person, and an average would let a very dry block outvote a federal
restriction. Then wet ground is **caution and not a refusal**, because whether a
pass is worth a rut is the foreman's judgement; `driving_zone` names the zone
that made it their problem. An unmeasured block is caution too, never open.
**Soil moisture is named in `inputs_missing`** rather than weighted at zero —
this app has no soil moisture register yet, and an `open` verdict here honestly
means "nothing we can measure is against it".

**Harvest readiness needs both numbers.** `Crop Observation.brix_reading`'s own
description states the rule: Brix rises while the stage stands still in a hot
week, and the stage advances while Brix stalls in a wet one. So `pick_now`
requires a BBCH code in 87–89 **and** a reading at or above the target, and every
other combination is `near_ready` with `short_of` naming what is missing —
`brix` (the reading is under), `brix_reading` (nobody took one), `brix_target`
(no pick figure recorded for this crop) or `stage` (the sugar is there and the
fruit is not). A round older than seven days is still reported, flagged `stale`:
dropping it would draw an unscouted block over one scouted a fortnight ago.

**Which layers come back depends on the caller's roles.** Field Worker gets
restricted entry and nothing else — not because the rest is secret but because a
picking crew's phone showing five overlapping colour schemes is a phone nobody
reads the one that matters off. Crew Leader adds harvest readiness; Foreman and
Farm Manager get all five; Compliance Officer gets the two regulated windows.
**Every role gets restricted entry, always.** An account holding none of this
app's roles is not filtered at all — a picker *has* the Field Worker role, so a
role-less login is the MCP system user or a Desk session — and `unfiltered` says
which branch ran. It is a **display filter and not a gate**;
`frappe.has_permission` on each register is what decides what can be read.

### `list_soil_compaction_profiles` — read-only, default ON

**Arguments:** `enabled_only`, `limit`.

**Returns** `profiles` (each with `red_hours`, `yellow_hours`, `drainage_class`,
`source`, and the count and names of the blocks it covers), plus
`default_red_hours`, `default_yellow_hours` and **`blocks_without_profile`**.

That last number is the one worth reading. A block naming no profile is coloured
by the shipped 24/48-hour default, which is a **loam's** — so a farm on sand is
being told to keep machinery off ground that dried out yesterday, and a farm on
clay is being sent onto ground that has not. The eight profiles are seeded on
migrate and **nothing is wired up** until somebody says which soil each block is.

### `create_soil_compaction_profile` — MUTATING, default off

**Arguments:** `soil_type` (required, and the docname), `red_hours` (required),
`yellow_hours` (required), `drainage_class`, `source`, `notes`, `enabled`.

The eight seeded rows are USDA textural classes and are deliberately short — the
triangle has twelve, and four of them sit between neighbours already there. A
farm with a soil between two adds its own rather than picking the nearer of a
list nobody reads.

**Refuses a yellow figure at or below the red one**, in the controller, because
getting them the wrong way round is *silent*: it leaves no caution band at all,
so every wet block goes straight from red to green when the red hours pass and
the drying-out warning is never drawn. Nothing anywhere reports an error; the
first symptom is a rutted block. **Refuses either figure at zero** for the same
class of reason — a blank Float arrives as zero without anybody typing it, and
zero claims this soil is never too wet to drive on.

### `update_soil_compaction_profile` — MUTATING, default off, idempotent

**Arguments:** `soil_type` (required), `red_hours`, `yellow_hours`,
`drainage_class`, `source`, `notes`, `enabled`.

**An argument not passed is left alone and a zero is not a blank.** Omitting
`red_hours` means keep it; sending `0` means "never too wet", which the
controller refuses. Collapsing the two would take a working profile down on an
update that meant to change only the notes.

**Returns `blocks_recoloured`.** Editing a profile recolours every block pointing
at it on the next map read, and a typo discovered by a tractor is an expensive
way to learn how many that was.

Retiring a profile with `enabled: false` does not orphan the blocks naming it:
they fall back to the shipped default and the overlay **names the profile it
skipped**, so a colour that changed across a farm on the day somebody unticked a
row can be traced to that row.

### `assign_soil_profile` — MUTATING, default off, idempotent

**Arguments:** `field` (required), `soil_profile`, `clear`, `company`, `dry_run`.

Its own tool rather than an argument on `update_field` — the same call
`link_field_to_cost_center` makes about the identical shape: one Link column,
with a real consequence behind setting it wrong, and no change to the signature
of a tool other clients already call.

**Refuses a disabled profile.** A block pointed at a retired one is coloured by
the shipped default while its own form claims a measurement, which is the worst
of both. `clear: true` puts a block back on the default deliberately.

### New records and columns

| Where | What | Why |
| --- | --- | --- |
| `Soil Compaction Profile` (new) | `soil_type` (docname), `drainage_class`, `red_hours`, `yellow_hours`, `source`, `enabled`, `notes` | how long this soil stays too wet to drive on. A record and not a constant, for the reason `Pest Action Threshold` is: the number is local, and a threshold nobody can edit is one a crew stops believing |
| `Field.soil_profile` | Link | which profile this block's ground follows. On the block, because the soil belongs to the ground; a zone resolves through the block it waters |
| `Crop.target_brix` | Float | the crop-level pick figure the harvest layer argues from |
| `Crop Variety.target_brix` | Float | the sparse per-variety override, resolved the same way v0.114.0's water overlay is — blank is no opinion, and a zero is not a blank |

### The two other doors

`/app/farm-overview` grows an **operational layer** picker: one layer at a time
and **none by default**, because the layers cost register reads a boundary check
should not pay for, and because five colour schemes on one polygon is the same
unreadable screen the role filter exists to prevent. The register's own colour
stays on every shape the layer does not paint, so the map still reads as a map.

`/farmops/api/mobile/get_map_overlays` is the same answer on the handset, open on
enrolment alone — gating it on the dispatch role would have withheld a safety
warning from the only people it is about. `blocks` is how a scan becomes a map
answer: one docname is one register read rather than five hundred.

---

## v0.118.0 — Farm App Retirement, Cycle 1

The Flask sidecar is being retired into this app. Cycle 1 builds the five
registers it held that this one did not, ports the reference data and the
research prompts that could not be re-derived, and adds one new field that is
not a migration at all. **No existing tool signature changed**; `create_field`,
`update_field` and `list_fields` gained one optional argument each.

Thirty-three tools: eighteen read (**on** by default), fifteen mutating (**off**
by default, as every mutating tool ships).

### The block ticker — a new field, not a port

`block_ticker` on `Field`, and a read-only copy on `Planting Season`.

A ticker is the **buyer's** name for a block — `YC-3` for Yellow Camp Block 3.
It goes on the purchase order, comes back on the settlement, and is how a buyer
asks for the same fruit next season without knowing what the docname is.

It is deliberately **not** `block_number`. A block number is what the crew calls
it, is duplicated across parcels on purpose, and changes when somebody re-splits
a block. A ticker is a promise made to somebody outside the business, so it is
**unique across the company**, folded to upper case on save, and ten characters
at most — the width of a column on a printed settlement sheet.

| Tool | Change |
| --- | --- |
| `create_field` | accepts `block_ticker` |
| `update_field` | accepts `block_ticker`; an empty string clears it |
| `list_fields` | filters on `block_ticker`, case-insensitively |
| `get_field` | reports it (no argument change) |

**Empty is the normal state.** Most blocks are never sold by name, and `''` is
not treated as a value — otherwise the first untickered block would lock out
every other one.

**The Planting Season copy is a copy and not a `fetch_from`.** It is taken at
save time and kept. Re-tickering a block in 2027 must not relabel its 2024
season, because the settlements that quoted the old code would stop agreeing
with the season record they were settled against.

### IoT — `IoT Device`, `IoT Reading`

| Tool | Kind | Notes |
| --- | --- | --- |
| `create_iot_device` | write | mints the bearer token; shown once, never read back |
| `get_iot_device` | read | accepts a docname **or** the hardware id |
| `list_iot_devices` | read | filters include `online` |
| `update_iot_device` | write | `last_seen` and `auth_token` are refused |
| `create_iot_reading` | write | the only writer of `last_seen` |
| `list_iot_readings` | read | rows, not statistics; capped at 500 |
| `get_device_readings` | read | per reading type, per unit, never across them |

**`last_seen` means the device spoke.** It is written only when a reading
arrives, and `update_iot_device` refuses the argument — a `last_seen` somebody
typed is the one thing that would make a dead sensor look alive.

**Online is computed at read time and stored nowhere.** A stored flag is wrong
from the moment a device goes quiet, and that is the only moment it matters. A
silent probe is not reading zero moisture; it is not reading, and a block gets
irrigated or does not on the difference. `health_warnings` says which: never
reported, silent for so many hours, battery low, never calibrated.

**A reading's block is a copy of where its device sat when it was taken.** A
probe moved from Block 3 to Block 7 in July must not retroactively move June's
readings — the June irrigation decisions were justified from them.

**The timestamp is the device's and is required**, never defaulted to now. A
device posting a buffered backlog would otherwise have every reading stamped with
the moment its radio came back, which is the one time they did not happen — and
buffered readings are exactly the ones somebody wants during a frost event.
Duplicates on `(device, reading_type, timestamp)` are refused: devices retry,
gateways replay, and a batch stored twice doubles every average computed off it.

**Aggregates never cross a unit boundary.** Where one reading type arrives in two
units — a device reconfigured mid-season — `mixed_units` says so and the figures
carry a note, rather than a mean being taken through the change.

### Competitive intelligence — `Market Participant`, `Competitive Move`, `Acquisition Target`

Four tools each: `create_*`, `get_*`, `list_*`, `update_*`.

**Every scale figure is an estimate and every result says so.** A competitor's
revenue, acreage, headcount and share are reads of a private business. They are
worth acting on and are not worth combining with numbers from the ledger.

**The four fit scores fail separately, so they are stored separately.** A
distressed neighbour with perfect strategic fit and no cultural fit is a deal
that closes and then does not work — and a single attractiveness number hides
exactly that case. `accretive_score` is their mean, derived on save and **refused
as an argument**; `weakest_dimension` is reported beside it, because a deal fails
on its weakest score rather than its average. An **unscored** target scores
`null`, not zero: zero is an answer, and an unassessed target must not sort
beside one assessed as worthless.

**`live_count` is separate from the total.** A pipeline of forty targets of which
thirty-five are Closed or Passed is a pipeline of five, and the count that gets
quoted is the wrong one by default.

**The observation half and the response half of a move are kept apart**, so the
gap between what was recommended and what was actually done stays visible.
`list_competitive_moves` names `urgent_unanswered`; it is invisible move by move
and is the most instructive thing in the register. A future observation date is
refused — a move somebody expects is not an observation.

**No tool here decides.** There is no `assess`, no `rank`, no landscape verdict.
The scoring is a judgement a person makes and the arithmetic on it happens in one
place. The farm_app's landscape prompt is preserved as data (below).

### Strategy — `Strategic Plan`, `Strategic Objective`

Four tools each.

**Plans are superseded, never edited into the next one.** Naming a
`previous_version` versions the new plan and retires its predecessor in one call,
and the old wording is left exactly as written — the interesting question about a
strategy is almost always what it *used* to say. `version` is derived and refused
as an argument; a circular chain is refused, because a loop leaves "what did we
say before this" with no answer.

**An objective is its own record, not a row on the plan.** Recording this
quarter's actual should not be a write to the strategy document — and "show me
everything overdue across every plan" is then one query rather than a walk
through every parent.

**Target and actual are free text on purpose.** Half of these are numbers and
half are not: `14 tons/acre`, `two new buyers`, `crew housed on site`. A numeric
column would silently exclude the ones that matter most.

**An undated actual is refused**, and so is `Achieved` with an empty actual — the
most flattering row a plan can carry and the one nobody can check. `achieved_rate`
is computed over **settled** objectives only; counting ones still in progress
flatters every plan on the day it is written. `overdue` means past its date **and
still open**, so a `Failed` objective does not sit on the list for ever.

### Residue limits — `MRL Record`

| Tool | Kind |
| --- | --- |
| `create_mrl_record` | write |
| `get_mrl_record` | read |
| `list_mrl_records` | read |
| `update_mrl_record` | write |
| `get_mrl_for_chemical_crop_market` | read |
| `get_ipm_reference` | read |

**`source` is required and its absence is the failure this prevents.** A load is
refused at a border against a named regulation on a named date, and "we had 0.5
written down" is not a defence. A tier-4 inferred limit with an honest note is
worth keeping; a bare number with no provenance is worse than nothing, because it
looks identical to a checked one. `source_tier` records how far from the official
register the figure was found: 1 official database, 2 government gazette, 3
industry or academic cross-reference, 4 inferred.

**`chemical` is the active ingredient, not the trade name** — several products
share one active ingredient and the limit attaches to the ingredient.

**`get_mrl_for_chemical_crop_market` never guesses.** A miss returns
`found: false`. It will not fall back to another market's figure, will not
average across markets, and will not offer the nearest crop; every one of those
returns something that looks like an answer to a question about whether a load
can ship. What it returns instead is the neighbouring evidence — the same
ingredient's limits elsewhere, the same market's limits on other ingredients —
clearly labelled as research rather than as the answer.

**Zero is a real limit.** A non-detect requirement is the strictest limit there
is, and treating it as missing would convert it into no limit at all. **A ban is
not an MRL**: a banned substance still carries a default figure, and the load is
refused on the ban regardless of the residue found.

`expiry_date` is a **re-check** date rather than the regulation's own lapse.
`list_mrl_records` names `needs_recheck`, which is what the register exists for:
limits are revised and withdrawn constantly, and a stale one is more dangerous
than a missing one because nobody goes looking.

### `get_ipm_reference` — the book, not the farm

**Arguments:** `pest`, `product`, `beneficial`, `crop`, `table`.

Read-only in the strongest sense: it touches **no doctype and no site data**, so
it works on a bench with nothing installed, and every result carries
`is_site_data: false` with the works it was assembled from named. With no filter
it returns only the index.

| Table | Rows | What |
| --- | --- | --- |
| `pest_models` | 28 | the pest, and its degree-day emergence model where the literature has one |
| `beneficials` | 19 | the organism and what it needs to stay in the block |
| `pest_damage` | 8 | damage severity, the vulnerable BBCH window, economic impact per acre |
| `beneficial_activity` | 10 | when each beneficial is actually working |
| `pesticide_efficacy` | 24 | efficacy 0–1, IRAC/FRAC group, resistance risk, residual days |
| `beneficial_toxicity` | 80 | what each product does to each beneficial |
| `pesticide_products` | 46 | keyed by EPA registration number, with restricted-use and signal word |
| `pesticide_labels` | 190 | PHI, REI, label ceiling and the reduced IPM rate, per product per crop |

**The toxicity table is why this is worth carrying.** Every label states a PHI and
an REI because the law requires it; almost none states what the product does to
the predators already working the block. A farm that sprays a pyrethroid for one
aphid flush and loses its predatory mites has bought a spider mite outbreak in
August, and that consequence is written down almost nowhere else.

Passing `product` **and** `pest` returns `rotation_partners`: products effective
on the same pest from a **different** mode-of-action group. Same-group
alternatives are excluded rather than ranked lower — two products that work
equally well and share a group are one product as far as resistance is concerned.

**Matching is exact apart from case and spacing.** There is no fuzzy matching on
purpose: the near-misses in this vocabulary — `Cherry Slug` and `Pear Slug`,
`Spider Mites` — are precisely the ones that must not be bridged automatically.

**Nothing here is a label.** Intervals vary by state and formulation and change
between seasons; the label in the applicator's hand governs, and `caveat` says so
in every result.

### `erpnext_mcp/erpnext_mcp/prompt_templates.py` — no tool, and deliberately so

Thirteen research prompts carried out of the Flask app as **data**: two MRL
(single market and batch), six pest and IPM, two agronomy, three strategy. Each
carries `system`, `user`, `returns`, `source` (the farm_app file it came from) and
a derived placeholder list; `render()` fills one in and reports what was left
blank rather than raising.

These are the one part of that application that cannot be re-derived from the
schema. `mrl_research_single` names sixteen national regulators and a four-tier
fallback ladder because that is what it took to stop a model returning
`NOT_FOUND` at the first miss; the pest prompts open by pasting the exact names
already on file and demanding they be copied verbatim, because a model returning
"codling moths" for `Codling Moth` produces a row that attaches to nothing.

**No tool renders them into a writer.** The farm_app's `utils/ai_call.py` — the
provider dispatch to Ollama, xAI and Anthropic — is **not ported**: an MCP server
is already on the other end of a model. A generated strategic plan or an
unreviewed MRL landing in a register is a document the farm is measured against
that nobody chose, and the whole value of the `source` column is that a person
put something in it.

## v0.122.0 — Farm App Retirement, Cycle 3

The last of the Flask sidecar's own registers, and the audit of what is left.
Cycle 1 took the device network, the competitive picture, the strategy and the
residue limits; Cycle 2 took phenology, the satellite metrics and the barcodes.
What remained was the food-safety plan — the one part of that application an
inspector actually asks to see — and one gap in the task board that only showed
up when the routes were read end to end.

Deliberately **not** ported, and named here so the omission is a decision rather
than an oversight: the vault and its encryption, the Nostr identity, event and
relay code with its NIP-44 crypto, the Merkle proofs, the Tor backup sharding
and the Nostr-tied wallet pass. The vision labelling workflow is Volume Vision's
and stays there.

### HACCP and the FSMA preventive-controls framework — thirty tools

Eight DocTypes, and thirty-one tools over them: `list_`, `get_` and `create_`
for each of the eight, `update_` for the six whose records are revised rather
than appended to, and one dashboard.

| DocType | What it holds |
| --- | --- |
| Food Safety Plan | The master plan: facility, qualified individual, lifecycle |
| Hazard Analysis | Per-step hazard identification against a risk matrix |
| Preventive Control | CCP definitions, critical limits, monitoring specs |
| Monitoring Record | An actual measurement taken against a control |
| Corrective Action Record | A deviation and what was done about it |
| Verification Record | Calibration, log review, product testing |
| Recall Plan | FDA recall procedure, contacts and simulation dates |
| Supplier Verification | Supply-chain verification with certificate expiry |

**The plan is the root.** Every other record carries a `food_safety_plan`, and
most also carry the `preventive_control` inside it. The plan is the document an
auditor asks for; the seven registers under it are what make the answer
credible.

**These tools are CRUD, and that is the design.** The inspection records
elsewhere in this app branch on their findings and drive a state machine. HACCP
records do not: the compliance value is in the *existence* of the record and its
*completeness*, not in an automated transition. A qualified individual reviews a
food safety plan — a tool does not, and one that advanced a plan's status on its
own would be producing the very signature an auditor is trying to verify. Only
six DocTypes take an `update_` at all, because a Monitoring or Verification
record is an observation with a time on it: it is appended to, and a correction
to one is a Corrective Action Record, not an edit. A Hazard Analysis is the
exception among the read-mostly registers and does take an `update_`, because it
records a *judgement* rather than a measurement, and a plan review is precisely
the occasion for reaching a different one — `farm_app` let a hazard row be
edited, and refusing to here would have made a mistyped likelihood permanent.

### `get_food_safety_dashboard` — read-only, default ON

**Arguments:** `company`.

**Returns** `plans` — one row per Food Safety Plan with `qi_current`,
`review_overdue`, counts of hazards, controls, monitoring, verification and
supplier records, `open_corrective_actions`, `active_recall_plans`,
`last_recall_simulation` and `expired_supplier_certificates` — plus
`total_plans` and `total_open_corrective_actions`.

**It answers "are we audit-ready" in one call.** That question is otherwise
eight reads and a date comparison per plan, which is the kind of arithmetic that
gets done once and then not again. `qi_current` and `review_overdue` are
computed against today rather than stored, so neither can go stale in the
register; a plan with no expiry or no review date on file reports `false` for
both rather than guessing, because a missing date is not a passing one.

**A register that is not installed counts zero rather than refusing.** Each
child count is guarded on the DocType existing, so a site part-way through
`bench migrate` gets a dashboard with honest zeros instead of an error naming a
table the reader has never heard of.

### `list_tasks_by_location` — read-only, default ON

**Arguments:** `location_filter` (a Housing Unit, Field, Zone or Parcel
docname), `skill`, `task_type`, `urgency` (applied to the pool half only),
`company`, `user`, `limit`.

**Returns** `location_groups` — one entry per place, each with `location`,
`location_doctype`, `label` (e.g. `"MC-Cabin-01: 3 tasks, ~90 min"`),
`total_tasks`, `held_count`, `available_count`, `total_estimated_minutes`,
`tasks_missing_estimate` and the `tasks` themselves — plus `unlocated_tasks`
for hand-raised work naming no place, `skill_matching`, and `me`.

**It is a third reader of two calls that already existed**, not a new query:
`list_available_for_me`'s pool and `list_my_tasks`'s Claimed/In-Progress
holdings, combined and grouped by the Farm Task's own `location`. Every refusal
and scoping rule those two carry — entity access, the concurrent-claim count,
the honest skill-matching story `skill_matching` reports — travels with it
unchanged, because it is the same two calls underneath.

**Why a new tool and not a `group_by_location` flag** on `list_my_tasks` or
`list_available_for_me`: the same reason this module's other tools are separate
switches and not one with modes. An operator piloting the grouped view wants a
switch that reaches it alone, and a flag buried inside a tool that is already on
is not something a switch can reach.

**A task with no location is reported, not dropped.** `unlocated_tasks` names it
rather than folding it into a fake "Unlocated" group — there is no such place.
`total_estimated_minutes` sums only tasks that carry an estimate;
`tasks_missing_estimate` says how many in the group did not, so the minutes read
as a floor rather than a promise.
