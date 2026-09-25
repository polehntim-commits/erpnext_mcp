# erpnext_mcp

[![tests](https://github.com/polehntim-commits/erpnext_mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/polehntim-commits/erpnext_mcp/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Frappe v15](https://img.shields.io/badge/Frappe-v14%20%7C%20v15%20%7C%20v16-0089ff.svg)](#compatibility)

A [Model Context Protocol](https://modelcontextprotocol.io) server that ships as
a Frappe app. `bench get-app`, `bench install-app`, tick a box, and an MCP client
— Claude Desktop, Claude Code, or anything else that speaks the protocol — can
read your ERPNext site and, only behind per-tool switches you turn on by hand,
write to it.

Nothing in it is specific to one install. Company names, account numbers, fiscal
years, report names and the Bank Transaction schema are all discovered from your
site at call time.

- **527 tools** — 252 read-only, 275 mutating.
- **Every mutating tool ships OFF, with one named exception.** A fresh install
  cannot change a document until you tick a box. The exception is
  `install_compliance_fields`, which adds columns rather than data and is argued
  for in [Compliance is not a module](#compliance-is-not-a-module); turn its
  switch off and it never runs.
- **Every call is audited**, reads included, in an append-only doctype.
- **LAN-only by default.** Token *and* a CIDR allowlist.
- **Its own doctypes, one endpoint, no `doc_events` and no overrides.**
  Installing it cannot change how anything already on your site behaves. The
  hooks it installs are additive and namespaced: two daily jobs that touch only
  this app's own tables (expired upload-staging rows, and the compliance alert
  sweep), one Jinja method (`erpnext_mcp_amount_in_words`) the check print
  format calls, and — from v0.32.0 — a Leaflet map on seven of its own forms
  (drawable on the three that carry a polygon, from v0.33.0), every one of which
  is a doctype this app created. Every one is resolved by a
  test — see
  `tests_standalone/test_hooks.py`, and v0.14.1 in the changelog for why that
  test exists.
- **One deliberate exception to "no field on anybody else's doctype", and it is
  v0.15.0's.** The compliance fields go ON Spray Log, Employee and the BucketLog
  bridge, because compliance woven into the operational record is defensible
  under audit and a shadow log beside it is not. It is behind a switch,
  `before_uninstall` names every column it would drop, and
  [docs/compliance_fields.md](docs/compliance_fields.md) makes the whole
  argument.
- **Six mobile roles and per-entity scoping, added in v0.17.0.** Field Worker,
  Foreman, Compliance Officer, Farm Manager, Family Member and Advisor, each
  installed idempotently on migrate. The role says what KIND of work somebody
  does; a Frappe User Permission on Company says WHOSE — see [Multi-entity
  scoping and the six mobile roles](#multi-entity-scoping-and-the-six-mobile-roles).
- **Compliance rules are data, added in v0.22.0.** A rule that watches for an
  expiring certificate or an uninspected cabin is a `Compliance Rule` record —
  its thresholds, scope, citations, regimes and message are fields somebody
  edits, so a renumbered regulation is an afternoon rather than a release. The
  runtime stays deterministic: no model runs in the trigger path, every rule
  needs a named human approver before it can fire, and edits supersede rather
  than overwrite so an alert from April is still readable against the definition
  that raised it. Since v0.22.1 **eleven of the thirteen shipped rules are fully
  declarative and none uses the sandboxed escape hatch** — the remaining two are
  argued as permanent rather than pending. See
  [docs/configurable_compliance_framework.md](docs/configurable_compliance_framework.md).
- **The shape of a recurring job is data too, added in v0.41.0.** A `Farm Task
  Template` says what one piece of work looks like — its type, its skill, its
  duration, whether it is dispatched or self-picked, what evidence closing it
  requires, what record completing it produces, and the items a worker ticks off
  — and a foreman or a compliance rule raises tasks from it. A task **snapshots**
  its template at creation, so editing one changes what future tasks look like
  and cannot reach a task already claimed or half-worked; a required checklist
  item left unticked refuses the completion by name, before any compliance record
  is written.
- MIT. Three runtime dependencies beyond Frappe/ERPNext (`shapely` and `h3` for
  field boundaries, `segno` for the mobile login QR), and the app still loads
  without any of them — each missing one costs its own tools BY NAME, with the
  pip command to fix it.

<!-- Screenshots: replace these placeholders with real captures. -->
| | |
| --- | --- |
| ![ERPNext MCP Settings](docs/img/settings-form.png) | ![MCP Action Log](docs/img/action-log.png) |
| The settings form: master switch, token, allowlist, one switch per tool. | Every call, reads included, in MCP Action Log. |

---

## Why would I install this?

Because your ERPNext site already knows the answer and you are the API.

You have a chart of accounts somebody thought about, thirty reports that took
years to get right, a purchase-order workflow with three approval states, and an
HR module that knows who was off last month. When somebody asks "which invoices
are past 90 days and who owns those customers", the answer exists — it is just
behind a login, four clicks and a filter panel, and if you want a model to help
you reason about it you end up pasting screenshots.

An LLM is perfectly capable of reasoning about a general ledger. It cannot see
one. This closes that gap without handing anything away:

- **It cannot write by default.** All but one of the 275 mutating tools are
  off on install (the exception is `install_compliance_fields`, argued below).
  Turning one on is a checkbox on a form only System Manager can open.
- **The dangerous verb gets its own switch.** `create_journal_entry` only ever
  produces a draft — `docstatus=0`, affecting no balance. Posting is
  `submit_journal_entry`: different tool, different switch, and it takes a name
  so it cannot create what it posts. Enable the first and not the second and you
  have given a model a scratchpad in the general ledger, which is usually what
  you actually wanted.
- **Nothing bypasses Frappe.** Writes go through
  `frappe.get_doc().insert()/.submit()/.cancel()`; workflow actions go through
  `apply_workflow`; reports go through `frappe.desk.query_report.run`. Doctype
  validation, fiscal-year checks, period-closing vouchers, frozen accounts,
  transition conditions and `on_submit` hooks all run exactly as they do for a
  human. There is no raw SQL in this app, and the test harness raises if anyone
  adds any.
- **It runs your reports rather than reimplementing them.** `run_report` invokes
  the Script and Query Reports your site already has. Reconstructing "Accounts
  Receivable Summary" out of primitive queries is how you get a number that is
  nearly right; running the report your accountant already trusts is how you get
  the one they would have got.
- **It is one whitelisted method.** No second listener, no sidecar, no new port,
  no process to supervise. Your existing nginx, TLS and access logs already
  cover it, and the server is up whenever the site is.
- **Uninstalling leaves no trace.** No `doc_events`, no overrides, no fixtures,
  and no field added to a doctype it did not create. Its own doctypes, an
  endpoint, six scheduled jobs that touch only its own tables, and form scripts
  on seven of its own forms — and then they are gone.

If you maintain a Frappe site for somebody else, the honest pitch is narrower:
this is a way to let them ask questions without giving them Desk access or
giving you a support ticket, and a way to let *you* debug their site from a
terminal you are already in.

---

## Install

```bash
cd ~/frappe-bench

# 1. Fetch the app. A local path works; so does a git URL.
bench get-app https://github.com/polehntim-commits/erpnext_mcp
# or: bench get-app /path/to/erpnext_mcp

# 2. Install it on your site.
bench --site yoursite.localhost install-app erpnext_mcp

# 3. Migrate and restart.
bench --site yoursite.localhost migrate
bench restart          # in development, `bench start` picks it up on its own
```

`bench get-app` installs the declared dependencies (`shapely`, `h3`, `segno` and
`reportlab`) into the bench's environment along with the app. If yours did not —
an offline install, a locked-down environment — the affected tools go quietly
unavailable and say so by name when a client asks for them; everything else
works. Without `shapely` and `h3` that is the six boundary tools; without
`reportlab` it is the two that draw a tax form onto a page, and the numbers stay
readable through `get_tax_form` either way. To add them afterwards:

```bash
./env/bin/pip install "shapely>=2.0" "h3>=4.0.0" "reportlab>=4.0" && bench restart
```

`install-app` syncs this app's doctypes and seeds their defaults, so after step 3
the read tools are already switched on — but the server itself is **off**,
because no token exists and `enabled` is unticked. It stays inert until you do
the next part.

### Turn it on

1. Open **ERPNext MCP Settings** (search it, or go to
   `/app/erpnext-mcp-settings`).
2. Click **Generate New Token**. Copy it from the dialog — it is stored
   encrypted and shown exactly once.
3. Check **Allowed CIDRs**. It defaults to loopback plus the RFC1918 private
   ranges; narrow it to the subnet your client is actually on.
4. Set an **MCP System User** (see [Attribution](#attribution)). This is not
   optional if you plan to enable reports, attachments or workflow actions —
   those honour the acting user's permissions.
5. Tick **Enabled** and save.
6. Click **Test Configuration**. It reports whether the server is ready, which
   tools are enabled, and which are unavailable on this site.
7. Use the **Connect to Claude Desktop** section that appears below the token to
   copy or download a ready-made client config — no hand-editing JSON. The panel
   shows which address it used and what else it considered; if that address is
   wrong, set **Public URL** and it always wins.

   Two cases need **Public URL** set explicitly. A site reached from outside on a
   different hostname (a Tailscale Funnel, a tunnel, a reverse proxy) has no way
   of knowing its own public name. And a published Docker port is invisible from
   inside the container — the panel recovers it from your browser's `Origin`
   header, which works when you are looking at the form, but not if the address
   you browse differs from the one clients use.

   If the generated URL is a bare IP, the panel warns you: Frappe routes requests
   to a site by Host header and an IP matches no site, so a client can get "site
   not found" while your browser works. Set `default_site` in
   `common_site_config.json`, or give the site a `host_name` that resolves for
   your clients, or put that name in **Public URL**.

Leave every switch in the write sections alone until you have a reason.

### Smoke test

```bash
TOKEN=paste-your-token-here
SITE=http://localhost:8000

curl -sS -X POST "$SITE/api/method/erpnext_mcp.mcp.handle" \
  -H 'Content-Type: application/json' \
  -H "X-MCP-Token: $TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | python3 -m json.tool
```

Then ask it to describe your site:

```bash
curl -sS -X POST "$SITE/api/method/erpnext_mcp.mcp.handle" \
  -H 'Content-Type: application/json' \
  -H "X-MCP-Token: $TOKEN" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"get_company_topology","arguments":{}}}' \
  | python3 -m json.tool
```

A `404` means `enabled` is off or no token is stored — those two are
deliberately indistinguishable from the app not being installed. A `401` means
the token is wrong; a `403` means your address is outside **Allowed CIDRs**. The
real reason for any rejection is in **MCP Action Log**, where you can read it and
a stranger cannot.

---

## Connect a client

The endpoint is MCP Streamable HTTP (JSON-RPC 2.0 over `POST`). It is POST-only:
this server never initiates a message, so there is no SSE stream to open, and a
`GET` returns a 405 saying so.

**Use the `X-MCP-Token` header, not `Authorization: Bearer`.** Frappe's own auth
layer inspects `Authorization` before any whitelisted method runs and routes a
`Bearer` value into its OAuth2 validator; an MCP token does not survive that trip
on every version. `Authorization: Bearer` is still accepted where it works, but
`X-MCP-Token` is the one that always arrives.

> **The settings form will do this for you.** Once the endpoint is enabled, the
> **Connect to Claude Desktop** section on ERPNext MCP Settings renders the exact
> JSON for your site — right URL, right token — with copy and download buttons and
> the config-file path for your platform. The rest of this section is what it
> generates, for anyone who would rather write it by hand.

### Claude Desktop

Claude Desktop launches MCP servers as local processes, so an HTTP server is
reached through the `mcp-remote` bridge. Edit
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "erpnext": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://erp.example.com/api/method/erpnext_mcp.mcp.handle",
        "--transport",
        "http-only",
        "--header",
        "X-MCP-Token: YOUR_TOKEN_HERE"
      ]
    }
  }
}
```

- **Plain HTTP on the LAN** — add `"--allow-http"` to `args`.
- **Keeping the token out of the config file** — pass it from the environment:
  ```json
  "args": ["-y", "mcp-remote", "https://erp.example.com/api/method/erpnext_mcp.mcp.handle",
           "--transport", "http-only",
           "--header", "X-MCP-Token: ${ERPNEXT_MCP_TOKEN}"],
  "env": { "ERPNEXT_MCP_TOKEN": "YOUR_TOKEN_HERE" }
  ```
  The whole header must stay one argument.

Restart Claude Desktop. The tools appear under the server name `erpnext`.

### Claude Code

No bridge needed:

```bash
claude mcp add --transport http erpnext \
  https://erp.example.com/api/method/erpnext_mcp.mcp.handle \
  --header "X-MCP-Token: YOUR_TOKEN_HERE"
```

### Anything else

`POST https://<your-site>/api/method/erpnext_mcp.mcp.handle` with
`X-MCP-Token: <token>`. The server negotiates protocol versions `2025-06-18`,
`2025-03-26` and `2024-11-05`, echoing back whichever the client asks for.

---

## The 395 tools

Full arguments, return shapes and worked examples:
**[docs/tool-catalog.md](docs/tool-catalog.md)**.

Tools whose site prerequisite is missing are not advertised at all — on a site
without Frappe HR, the three HR tools simply do not exist as far as a client is
concerned.

**A tool missing from your client's list is switched off, not absent.**
`tools/list` advertises only what is enabled AND runnable here, and every
mutating tool ships OFF — so a write tool you cannot see is one nobody has ticked
yet. Tick it in **ERPNext MCP Settings**; the refusal message names the exact
switch if you call the tool anyway.

### Finding a switch among seven hundred

Every tool has its own checkbox on **ERPNext MCP Settings**, which is the point
— and it means the page carries 759 of them. From v0.108.0 a console sits above
the first tool section:

* **A summary**, live as you tick: `412 of 757 tools enabled — no write tools
  enabled`. The write count is always its own clause.
* **Seven domain chips** — Farm Operations, HR & Payroll, Compliance & Safety,
  Accounting & Finance, Buying/Selling/Inventory, Assets/Property/Governance,
  Platform & Administration — each with its own `on/total`. Clicking one narrows
  the form to it.
* **Search**, over the tool name and the switch's label. Try `spray`, `i9`,
  `garnish`.
* **Eight presets** — Farm Manager, Foreman, Field Worker, Bookkeeper,
  Compliance Officer, Owner/Family, Read Only, Nothing Enabled — each of which
  writes every tool switch at once, shows you what it will change first, and
  saves as one Version row you can read afterwards.

A preset **disables as well as enables**, so applying one replaces the last
rather than accumulating; and it touches **only** `allow_<tool>` — never the
master switch, the token, the allowed CIDRs, the attribution user or the packet
types. Two of the eight enable write tools, and both name every one they turn on
before they run.

### Read-only — 104, all ON by default, each individually switchable

**Accounting** — the v0.1.0 surface

| Tool | What it answers |
| --- | --- |
| `get_company_topology` | What is this install? Companies, currencies, cost centers, fiscal years, chart roots, which optional doctypes exist. **Call this first.** |
| `get_account_balance` | Balance of one account as of a date, from GL Entry, in both raw and natural sign convention. |
| `get_journal_entries` | JE headers by date range, company, account-on-any-line, docstatus. |
| `get_journal_entry` | One JE in full, every line with party, cost center and reference. |
| `investigate_je_gl_link` | **The diagnostic.** Every line of one JE beside every GL Entry row it posted, with the join spelled out, every line whose party disagrees with the ledger flagged, and a plain-English `finding`. What you call when the voucher says one thing and the ageing report says another. |
| `list_bank_transactions` | Bank Transactions by account, date range and status, amounts normalised to one signed number. |
| `get_bank_statement` | One Bank Statement, on versions that have the doctype. |
| `list_fiscal_years` | Fiscal years and the companies they apply to. |
| `get_chart_of_accounts` | The chart as a nested tree, optionally one root type. |
| `list_unreconciled_bank_transactions` | The reconciliation worklist. |
| `search_accounts` | Turn "cash clearing" into a docname. Ranked best-first. |
| `propose_clean_chart` | A complete numbered chart of accounts for a company, from a static template, in the shape `import_chart_of_accounts` takes. Reports what it would collide with. Creates nothing. |
| `list_cost_centers` | The company's cost centers as a nested tree. Disabled ones excluded and counted unless asked for. |

**Workflow**

| Tool | What it answers |
| --- | --- |
| `list_workflows` | Every workflow: governed doctype, states, transitions, roles, which states are terminal. |
| `get_workflow_state` | Where one document sits, and every transition out of that state. |
| `list_pending_approvals` | The worklist — documents in a state something still has to be done to, optionally narrowed to one user's roles. |
| `list_available_actions` | What the acting MCP user can do to this document *right now*, conditions and self-approval evaluated. |

**Reports**

| Tool | What it answers |
| --- | --- |
| `list_reports` | Every saved report, its `ref_doctype` and its type. |
| `run_report` | Runs it. Query, Script and Report Builder all handled. Enforces the acting user's permissions. |

**Attachments**

| Tool | What it answers |
| --- | --- |
| `list_attachments` | Files on a document: name, size, private flag, who uploaded it. |
| `get_attachment_content` | One file's bytes, base64, size-capped. |
| `list_staged_uploads` | Every chunked upload in flight: pieces received out of pieces expected, **which indexes are missing** as compact ranges, bytes staged, and whether it is ready to commit. What you call when call 43 of 60 failed and you need to resume rather than re-send. |

(`attach_file_to_document` puts one *there* — it writes, so it lives with the
write tools below.)

**Comments and tasks**

| Tool | What it answers |
| --- | --- |
| `list_comments` | The comment and activity thread on a document. |
| `list_assigned_todos` | What is on somebody's list, with overdue flagged. |

**HR** — only where the `hrms` app is installed

| Tool | What it answers |
| --- | --- |
| `list_employees` | Employees with department, designation, status, joining date. |
| `get_attendance_summary` | Per-employee Present / Absent / Half Day / On Leave counts over a range. |
| `get_leave_balance` | Remaining leave per type, computed by HR's own balance function. |

**Sales and purchasing**

| Tool | What it answers |
| --- | --- |
| `list_sales_orders` | SO headers with totals and delivery progress. |
| `get_outstanding_invoices` | Who owes what, aged into buckets. |
| `list_purchase_orders` | PO headers with totals and receipt progress. |

**Site customisation**

| Tool | What it answers |
| --- | --- |
| `list_custom_fields` | Custom Fields in form order. The "why is my field not showing up" tool. |
| `list_client_scripts` | Client Scripts with a 500-character preview of each body. |

**Compliance packets**

| Tool | What it answers |
| --- | --- |
| `list_compliance_packets` | Which packet types this site can produce, and the filters each takes. |
| `generate_compliance_packet` | Builds one. See [Compliance packets](#compliance-packets) below. |

**Governance and assets**

| Tool | What it answers |
| --- | --- |
| `list_cap_table` | Who the members are: anonymous id, legal entity, admission, percentage. Retired members included. **This is the tool that de-anonymises the ledger** — see [Members are anonymous](#members-are-anonymous-on-purpose). |
| `list_member_events` | The equity trail: contributions, distributions, transfers, and the narrative for each. |
| `list_governance_documents` | What is in the archive, with the amendment chain and which entries are still in force. |
| `get_governance_document_content` | One archived document's metadata and its attachment, base64. |
| `depreciation_note_alignment_check` | For every financed asset, whether its remaining life and its note's remaining term still agree. |
| `list_notes_payable` | Every note or loan a company owes: lender, original and outstanding principal, rate, term, and when the next payment falls due. |

**Land, leases and related parties** — the v0.11.0 surface

| Tool | What it answers |
| --- | --- |
| `list_parcels` | The land register: acreage, county, assessor parcel id, use type, appraised value, and which parcels are on the balance sheet. Totals acreage and value, and reports the oldest appraisal — which is how you find out the valuation is four years stale. |
| `get_parcel` | One parcel in full, with every lease over it in either direction and the gap between what the Asset cost and what the appraisal says. |
| `list_leases` | Leases **both ways** with the rent roll: annual rent receivable, annual rent payable, the net, and what runs out inside 90 days. A crop share has no annual rate and is listed rather than counted as zero. |
| `get_lease` | One lease in full, with the parcel it covers, its attachments, and whether it is in force today by the dates on the record. |
| `list_related_parties` | Who is related to this company, in what capacity, since when, and which relationships have **no governing document behind them** — the first thing an examiner asks for. |
| `get_related_party` | One relationship with everything pointing at it: the person's other roles, their cap table entry, their Supplier, the parcels they hold title to. Never returns more than four digits of a taxpayer id. |

**Companies, ground and camps** — the v0.12.0 surface

| Tool | What it answers |
| --- | --- |
| `list_companies` | Every company with its abbreviation, currency, tax-id status, fiscal year period, cost center and account counts, and **the GL entry count with the first and last posting dates** — which is how you tell a live company from a shell, and what decides whether its currency can still be changed. Also reports whether the `Family` and `Contact` party types are registered. |
| `list_fields` | The block register: acreage totalled, plantings summarised, condition and food-safety facts per block — and **the varieties already planted on this site**, which is the autosuggest list worth having because a hardcoded one is wrong the first time somebody plants something new. |
| `get_field` | One block in full, with every irrigation zone over it, the water rights they run under, and how much of the block is not zoned at all. |
| `get_parcel_field_summary` | One parcel rolled up: blocks, planted acres against the parcel's own acreage **and the gap**, zones, flow, planting ages, counts by condition and variety, and the food-safety exceptions. |
| `list_irrigation_zones` | The zone register with area and flow totalled — plus the zones with **no agricultural water test** on record and the surface-water zones with **no water right named**. Those two lists are the report. |
| `get_irrigation_zone` | One zone in full, with the block it waters, this zone's share of it, and the compliance gaps in sentences rather than left to be inferred. |
| `list_housing_units` | The camp register: capacity, occupancy and open beds, plus the overdue habitability inspections, the uninhabitable units, the FSMA worker facilities, and the units filled past what their floor area lawfully allows. |
| `get_housing_unit` | One unit in full with its **whole assignment history**, and what each missing compliance date obliges. |
| `list_housing_assignments` | Who is housed where, currently or over a period, with the wage-deduction assignments named and the deposits still held. |
| `get_housing_capacity` | Beds, bodies and overdue inspections per parcel and in total, with a plain one-sentence-per-parcel readout. |
| `get_employee_housing_history` | Everywhere one person has been housed, in order, with deposits and wage deductions — the audit trail an IRS Section 119 exclusion is defended with. |
| `find_fields_containing_point` | **The geofence query.** Which blocks a GPS fix is inside — bounding box first, then exact point-in-polygon, with the boundary counting as inside. Reports how many blocks have no boundary at all, because an empty result on a half-mapped farm means "not inside any *mapped* block", not "not on the farm". |
| `find_fields_by_h3_cell` | Which blocks an H3 cell touches, at any resolution. The spatial-index join for anything else keyed on H3 — a bucket log, a crew track, a weather grid. |
| `list_family_members` | The family register: everybody a `Family`-party posting can name, with their relationship and whether a related-party record sits behind them. Names who has one and who does not — a gap for a member or a trustee, not for a relative who only receives transfers. |
| `get_family_member` | One person, their related-party detail, and **every posting that names them** — count, first and last date, net amount, companies. Read from the ledger rather than kept, so the count cannot drift from what happened. Never returns more than four digits of a taxpayer id. |

**Compliance** — the v0.15.0 surface

| Tool | What it answers |
| --- | --- |
| `get_compliance_field_map` | What compliance requires of an OPERATIONAL record on this site, field by field: which framework wants each one, why, and **what breaks in the day-to-day work without it**. Reports which are present here and which are not. |
| `list_compliance_policies` | The SOP library — what is written down, at what version, and what is stale. `without_a_document` is the list worth acting on first: a policy record with no attached procedure is a claim, and an auditor asks to read the procedure. |
| `get_compliance_policy` | One procedure with its **whole version chain**, walked in both directions, and every audit corrective action that cited it. An audit asks which procedure was in force on a date, and it is usually not the current one. |
| `list_certifications` | The certificate and licence register, **soonest expiry first** — the order somebody works them in. `expired` is read from the DATE, never from the status field, because nothing here rewrites a status when a date passes. |
| `get_certification` | One certificate with its full renewal history, **including every period it was allowed to lapse**. Resolves the holder against the Related Party, Family, Employee and Company registers; a name in none of them is a contractor on nobody's payroll, not a failure. |
| `list_regulatory_filings` | What was filed, to whom, when and under what docket number, with the ones still awaiting a response and the ones with no document attached. |
| `get_regulatory_filing` | One filing with its response, both attached documents, and what is missing from the proof it was made. |
| `list_audit_events` | Every audit and inspection, with open corrective actions counted and the **overdue** ones named. An operation is judged on closing findings, not on having none. |
| `get_audit_event` | One audit in full: scope, findings, and every corrective action with its severity, deadline, days overdue, what was done and what proves it. |
| `get_compliance_calendar` | **The main read.** What is due and what is late, worst first, grouped by category — with snoozed alerts hidden and counted, overdue ones never hidden by a forward window, and the rules that cannot run here named. |
| `list_compliance_rules` | Every alert rule with its `kairotic_gate` — the STATE that makes it fire rather than the date — and the framework it serves. |
| `get_audit_readiness` | Resolved over raised, as one comparable number, plus **how it was earned**: conditions that cleared themselves against dismissals somebody made by hand. |
| `list_audit_packet_types` | The eight audit regimes, what each pulls in, and which sections will be empty on this site because the DocType behind them is not installed. |
| `find_drifted_je_attributions` | **DIAGNOSTIC.** Every submitted JE whose voucher and general ledger disagree about who a line belongs to — the damage v0.13.0's broken party tool left behind. Three queries whatever the range. |

**Dispatch and the records that close the loop** — the v0.16.0 surface

| Tool | What it answers |
| --- | --- |
| `list_dispatch_board` | **The Kanban as JSON.** Every task grouped into its state column, worst urgency first, with the pool, the open Critical work, and **how much of the board came from a compliance alert** — which is the honest measure of whether the calendar is driving work or being read and ignored. |
| `list_available_tasks` | The pool: what a worker could pick up right now, filtered by location, skill or type, with their concurrent-claim count and whether they may take another. Dispatched work is **deliberately absent** — somebody has to be sent to that by name. |
| `list_dispatched_tasks` | What one worker is holding, and their history on request. |
| `get_farm_task` | One task in full: its evidence contract in sentences, every assignment and **every rejection with the reason given**, the compliance record its completion produced, and whether the alert it came from has since auto-dismissed. |
| `list_housing_inspections` / `get_housing_inspection` | Every habitability walk, with the ones that **found something and nobody closed** named separately. One walk in full with its photographs, the unit's whole history, and the later clean walk that superseded it. |
| `list_detector_tests` / `get_detector_test` | Every smoke and CO detector test, with the failures and the buildings that have **no detector at all** as open findings. |
| `list_water_tests` / `get_water_test` | Every agricultural water sample, with the contaminated and the **unreadable** ones named — a result nobody can interpret is not evidence that the water is safe. |

**Mobile accounts, the public endpoint, and the phone's own view** — the v0.17.0 surface

| Tool | What it answers |
| --- | --- |
| `list_mobile_users` | The roster **and everything wrong with it**: who has a phone, which role, which entities their User Permissions ACTUALLY allow — read live, so a scoping somebody changed in the Desk shows as drift — plus a `concerns` list per account. An account with no Company permission, a grant marked Revoked whose token still works, a credential past its review date. |
| `get_current_user_context` | Who is calling, their roles, their entities, which entity to open on, and plain `can` / `cannot` lists for an account screen. The identity comes from the request's own credential; a request that authenticated as one person and passes `user` naming another is **refused**. |
| `validate_public_endpoint` | Reaches this site **from outside** over HTTPS: certificate issuer and expiry, latency, what the MCP path answered, and a verdict with a next step. A 401 to the default unauthenticated probe is the best possible result. |
| `get_tailscale_funnel_config` | What this machine thinks it is serving: Funnel ports, URLs, the node's tailnet DNS name. Degrades honestly when Tailscale is on the host and invisible from inside the container, which is the expected state on an Umbrel. |
| `list_my_tasks` | What the authenticated caller is holding, with the tool each assignment is waiting for so a screen draws the right button. Worker resolved through their Employee record; a login with no Employee row is refused **by name** rather than answered with an empty list. |
| `list_available_for_me` | The pool the caller could take from, scoped to their entities. **Honest about skills**: nothing on a Frappe site records what skills a worker has, so an unfiltered pool comes back saying so rather than guessing from a job title. |
| `get_task_with_evidence_contract` | One task shaped for a phone: the evidence contract as a checklist with a `satisfied` flag and a capture hint per requirement. Same facts as `get_farm_task`; different reader. |
| `list_compliance_calendar_for_me` | The calendar narrowed to the caller's entities — one call per entity, merged, Critical first. An account with **no** Company User Permission is refused rather than shown the whole site. |


### Mutating — 113, all OFF by default, with one named exception

**Postings into the ledger**

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_journal_entry` | Creates a **draft** JE. Refuses unbalanced entries, negative amounts, group accounts, single-line entries. | Submit. There is no argument for it. |
| `submit_journal_entry` | Submits an existing draft, `0 → 1`. Writes GL Entries. **This moves balances.** | Create anything — it takes a name. |
| `bulk_submit_journal_entries` | Submits up to 500 drafts, each in its own transaction, and reports per document. One failure does not undo the rest. | Run at all unless `submit_journal_entry` is also switched on. |
| `cancel_journal_entry` | Cancels a submitted JE, `1 → 2`, writing reversing entries. `reason` mandatory. | Delete anything. |
| `update_journal_entry_party` | Sets or changes `party_type` and `party` on **one line** of a JE — including a submitted one — in the voucher **and** in its GL Entry rows, with a mandatory reason written to the entry's timeline. **Fixed in v0.14.0, and again in v0.15.0**: its idempotence check now reads BOTH tables, so a voucher that already agrees over a ledger that does not is a GL-only repair rather than a refusal. See [When the voucher and the ledger disagree](#when-the-voucher-and-the-ledger-disagree). | Move a balance. Account, debit, credit and date are not arguments, so the trial balance afterwards is arithmetically identical. Nor touch a cancelled entry, a rounding line, a bank or cash line, or a line whose GL rows cannot be identified with certainty. |
| `delete_draft_journal_entry` | Deletes a **draft** outright, recording what it was and why in the audit log. | Touch a submitted entry (cancel it) or a cancelled one (its reversing rows are the trail). |
| `delete_draft_purchase_invoice` | Deletes a **draft** Purchase Invoice outright, recording what it was and why, and releases the Expense Receipt it was billed from so it can be billed again. | Touch a submitted invoice (cancel it) or a cancelled one, or change the receipt's supplier link. |
| `create_bank_transaction` | Inserts a **draft** Bank Transaction. | Submit it. |
| `reconcile_bank_transaction` | Attaches payment vouchers, refusing to over-allocate. | Allocate past the remaining amount. |
| `set_opening_balance` | Books one historical event as a **draft** opening-balance JE, **computing** the offsetting line against Opening Balance Equity and flagging the entry as opening. | Submit it, or guess an equity account when two could match. |
| `post_opening_balance_journal_entry` | Books a whole opening balance sheet as one JE, every line given, the difference going to an `offset_account` you name. Posts it too, with `submit: true`. | Submit unless `submit_journal_entry` is also switched on — checked before anything is written. |
| `create_bank_account` | Creates the Bank Account a feed writes into, and the Bank behind it, in one transaction. | Point at anything but an Asset (bank) or a Liability (credit card) — or at an Asset whose `account_type` is not Bank or Cash. |

**Structural changes to the chart itself**

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_account` | One account under an existing group. Checks the parent, the root type, the number's uniqueness and the account type before writing. | Create a root — ERPNext treats roots as uneditable once made. |
| `update_account` | Rename, renumber, re-type, enable/disable. The docname moves with the fields, via ERPNext's own `update_account_number`. | Reparent. That is `move_account`, on purpose. |
| `move_account` | Reparent, and nothing else. Refuses a cross-root or cyclic move. | Rename anything. |
| `disable_account` | ERPNext's soft delete, `reason` mandatory. | Touch an account with GL entries in the current fiscal year, or delete anything ever. |
| `delete_account` | **Irreversible.** Hard-deletes an account with no history, freeing its number — which disabling does not. Four checks, all refusals, all reported at once. | Remove an account with GL entries, journal entry lines (drafts included), children, a company default or a Bank Account pointing at it. |
| `import_chart_of_accounts` | Builds a whole tree in one transaction, rolling back entirely on any failure. New top-level accounts included. **`dry_run` defaults to true.** | Silently reparent or rename an account that already exists. |
| `create_fiscal_year` | Creates a Fiscal Year, so ERPNext will accept postings dated inside it. Company-aware overlap check. | Overlap a year that shares a company, or take an end date that is not one year after the start unless `is_short_year`. |
| `update_fiscal_year` | Moves a year's dates, or enables/disables it. | Move dates in a way that leaves existing GL entries in no fiscal year, rename the year, or change which companies it applies to. |
| `advance_workflow` | Takes a workflow action via `apply_workflow`. **Can submit or cancel the document** if the target state says so. Supports `dry_run`. | Take an action the acting user is not allowed. |
| `create_todo` | Assigns a ToDo. | Touch any ledger. |

**How a posting is classified**

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_cost_center` | One cost center under an existing group. Checks the parent and the number's uniqueness before writing. | Add a second root — ERPNext gives every company exactly one, named after the company. |
| `update_cost_center` | Rename, renumber, disable/enable. The docname moves with the fields. | Reparent, or rename the company's root. |
| `create_accounting_dimension` | Creates the dimension, adds its Link field to the documents that carry it, and — only when asked — generates the custom DocType whose records are its values. | Point at a Single, a child table or a core doctype; reuse a fieldname another field already holds. |
| `create_dimension_value` | One record in the DocType a dimension points at. | Touch a ledger, or add a field. |
| `set_company_defaults` | Points the company's default account and cost center fields at real accounts, **type-checked**. Idempotent. | Write any of them if one value in the batch fails validation. |
| `bulk_wire_default_accounts` | The same fields, **found rather than given** — by account number for a numbered chart, then by account type, then by name, then by root type, every candidate passing the same type checks. The setup call to make after `create_company`. | Fill a field with something merely plausible. What nothing matched is reported with what was looked for, and the rest are still wired. |

**Who owns it, and what happened to their interest**

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_cap_table_entry` | Registers one member: anonymous id, legal entity, admission date, percentage. | Register a member the ledger cannot already refer to, or one already retired. |
| `update_cap_table_entry` | Changes a member's details. | Retire a member, or change the `member_id` every posting is tagged with. |
| `close_cap_table_entry` | Retires a member and writes the exit into the event trail. | Move any money — a final distribution is a separate call. |
| `record_member_event` | Records the event and, where it books money, a **draft** JE with the right accounts and the member tag on every line. | Post it. Guess an equity account when two could match. |
| `submit_member_event` | Posts the draft the event is waiting on. | Run at all unless `submit_journal_entry` is also switched on. |
| `attach_governance_document` | Files a governing document, attaches its content privately, and chains it onto what it supersedes. | File the same document twice, or make the amendment chain loop. |

**Evidence, onto the document it belongs to**

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `attach_file_to_document` | Attaches one file to **any** document — a statement onto the Journal Entry that books it, a receipt onto a Bank Transaction, a contract onto an Asset. Private by default, sha256 in the result and the audit row. | Attach to a document the acting user cannot write, to a cancelled one without being told to, or twice under the same filename. Move a balance, or change any existing row. |

**Files too big for one tool call** — see [Streaming uploads](#streaming-uploads-a-file-bigger-than-a-tool-call)

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `stage_file_chunk` | Puts one piece of a file into a staging **table**, so the upload survives a `bench restart`. Re-sending an index replaces that piece, because a caller whose call timed out has no other safe move. | Create a File. Staging alone lands nothing — `commit_staged_file` is the switch that decides whether anything does. |
| `commit_staged_file` | Reassembles the pieces, verifies them against the SHA-256 the caller computed **before** sending anything, and turns them into a File — attached to a document, filed as a new Governance Document, or standing alone. | Commit with a gap, a wrong hash or a wrong size. Delete a single staged piece until the File exists, so every refusal is fixed by changing the argument rather than re-sending the file. |
| `cancel_staged_upload` | Throws away a staged upload and its pieces. | Touch any File. What it destroys is half a file nobody has committed. |

**Assets that serve more than one segment**

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_asset` | Creates an ERPNext Asset plus the cost profile holding its usage split, its schedule and its note. **Switches ERPNext's own depreciation off on that asset.** | Accept a split that does not total 100, or a useful life that disagrees with the note's tenor. |
| `update_asset_allocation` | Replaces the split, from the next period onwards. | Rewrite periods already written — that is the history. |
| `link_asset_to_note` | Ties an asset to its financing and holds their terms together. | Link a diverging pair unless you pass `enforce_tenor=false`. |
| `run_depreciation_cycle` | Writes the depreciation due as **draft** JEs, split across each asset's cost centers. **`dry_run` defaults to true.** | Post a period twice, or post anything at all. |

**What the company owes**

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_note_payable` | Registers one note or loan: terms, provenance, the liability account it posts to, and the asset it financed. | Link an asset whose useful life disagrees with the note's term, unless you pass `enforce_asset_tenor=false`. |
| `record_loan_payment` | Splits a payment into principal and interest and drafts the JE that books it, decrementing the balance. | Book a split that does not add up, clear more principal than is owed, or post anything. |
| `close_note_payable` | Closes a note as Paid Off, Refinanced or Written Off, recording the disposition in its own history. | Write a journal entry — relieving the balance is a posting somebody should make on purpose, and the response says which. |

**Land and the agreements over it**

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_parcel` | Registers one parcel: county, assessor parcel id, acreage, use type, appraised value and the date it was appraised as of. | Register a second parcel with the same name, or the same assessor id, for one entity — that number is the county's key and two of them means a typo. |
| `update_parcel` | Changes a registered parcel, echoing every change as before → after. | Re-key it, move it between entities (a conveyance is not an edit), or set the asset link — that is its own tool. |
| `link_parcel_to_asset` | Points a parcel at the Fixed Asset carrying it and **reports the gap between cost and appraised value**. | Post anything. Land is not depreciated and this does not pretend otherwise. |
| `convey_parcel` | Moves a parcel onto **another entity's books**, carrying its attachments and every lease, block, irrigation zone, housing unit and housing assignment pointing at it. `reason` mandatory and written into the parcel's own conveyance history. | Post anything — basis transfer and gain recognition are entries somebody writes on purpose. Or convey out from under an **active lease**, or move a parcel with a Fixed Asset still linked. |
| `create_lease` | Records one lease in either direction, with the executed document attached. Reports whether the stated direction agrees with the party names. | Book anything. Recording the agreement and booking its consequences are separate acts. |
| `update_lease` | Changes status, term, rent, parties, parcel or counterparty. | Mark a lease Terminated without a termination date, or rename it — a renewal is a new lease with its own term. |

**Who is related to whom**

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_related_party` | Registers one relationship: who, what kind of entity, in what capacity, from when, under what document. | Store more than four digits of a taxpayer id. Nine is **refused**, naming the four to send — see [Four digits, never nine](#four-digits-never-nine). |
| `update_related_party` | Changes party type, dates, tax identity, address and the links to a cap table entry, a Supplier and a governing document. | Re-key it. A change of role is a new relationship; the old entry gets an end date rather than being deleted. |

**Companies, and the two party types a family operation actually pays**

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_company` | Stands up a Company plus the chart of accounts, cost centers and the fiscal year containing today — April for a farm year, January for a calendar one — and reports what ERPNext **actually** built, which is not always what was asked for. | Reuse a name or an abbreviation. Every account, cost center, parcel and lease docname ends in the abbreviation, and two companies sharing one makes those ambiguous. |
| `update_company` | Changes country, tax id and notes; changes the currency **only while nothing is posted**. Echoes every change as before → after, with the tax id redacted to its last four. | Change the abbreviation or the name (both are a migration, not an edit), the currency once anything is posted, or the fiscal year start month once any year exists — see [Two party types, and one exclusion](#two-party-types-and-one-exclusion). |
| `register_party_types` | Registers `Family` and `Contact` so a Journal Entry line can carry them. Idempotent, and already seeded on install and every `bench migrate`. | Reclassify anything. Existing rules and entries using Shareholder, Employee or Supplier are untouched. |

**The structure under a parcel**

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_field` | Registers one planted block: acreage, crop, variety, rootstock, planting year and density, condition — and the food-safety facts, which live on the block because they are the same facts the operation runs on. Docname is `<field name> - <parcel abbr>`. | Register blocks whose acreage sums to **more than the parcel they are on**, named with both figures and the excess. That is the failure a bad import produces every time. |
| `update_field` | Changes any of the above, echoing before → after. | Re-key it, move it to another parcel (ground does not move), or set the cost center — that is its own tool. |
| `link_field_to_cost_center` | Points a block at the Cost Center its costs book to, so per-acre and per-block costing has somewhere to land. | Use a cost center on another company's books (that is an intercompany transaction, not a dimension), a group cost center, or repoint a linked block without `replace=true`. |
| `create_irrigation_zone` | Registers one zone: source, Oregon water right, flow, sprinkler type, area — and the FSMA agricultural water facts. Docname is `<zone name> - <parcel abbr>`; acres are **computed** from square feet. | Reuse a zone number on one block — that number is what somebody types into the controller at two in the morning — or let zones sum past their block's acreage. |
| `update_irrigation_zone` | Changes any of the above and recomputes the acres. | Re-key it, move it to another block (pipe does not move), or set the acres directly. |
| `import_farm_app_fields` | Creates Fields from a batch of legacy Farm App records **carrying their ids**, so a later sync engine has something to match on. Dry run by default; a block already registered is skipped with the reason, so the same batch re-runs safely. | Update anything, delete anything, or half-import a farm — the whole batch is validated before the first insert. |
| `set_field_boundary` | Gives a block its shape as GeoJSON and derives centroid, bounding box, H3 coverage at resolutions 6–10 and the area the polygon encloses. | Accept invalid GeoJSON, a self-intersecting polygon, coordinates off Earth, or an area more than 25% from the recorded acreage — at that point one of the two figures is about a different piece of ground. |
| `set_zone_boundary` | The same for a zone, and reports whether it sits inside the block it waters. | **Enforce** that containment. A shared water line crosses a boundary, a pump house sits on the headland — refusing would make those unrecordable, so it reports and lets the operator decide. |
| `import_field_boundary_geojson` | Sets boundaries on **existing** blocks from a GeoJSON FeatureCollection — for migrating a farm's polygons in one go. Per-feature errors, so one bad feature in forty does not stop the other thirty-nine. | Create a Field. A feature naming an unregistered block is skipped with that said. |

**The camp**

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_housing_unit` | Registers one building with its capacity, condition and the compliance facts, computing the lawful occupancy from floor area at 50 sq ft a head unless you pass one. | Reuse a unit name on one parcel, or link an Asset on other books or already carrying a different unit. **Warns rather than refuses** a capacity over 20 outside a Multi-Unit Building — a twenty-person cabin is barracks by another name, and some really are. |
| `update_housing_unit` | Changes any of the above; recomputes the lawful occupancy when the square footage moves, **unless** the stored limit was one somebody typed. | Re-key it, or move a building between parcels — even a manufactured home that really was moved should be re-registered where it stands, so the assignment history stays attached to the ground it happened on. |
| `create_housing_assignment` | Puts one person in one unit from a date, with the deposit and the ORS 653 wage-deduction answer. Auto-named `HA-YYYY-MM-<seq>`. | Overlap an existing assignment on that unit unless `allow_multi_occupancy=true`; assign anybody to a shower block, a shop or an uninhabitable unit; or, where an HR app is installed, name somebody who is not on file. |
| `end_housing_assignment` | Writes the date somebody moved out and the deposit returned. | **Delete.** The record is what defends a Section 119 exclusion, answers a wage claim and tells an investigator who was in the camp that week. |

**The family register**

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_family_member` | Puts one person on the register so a journal entry line can carry `party_type='Family'` and name them. `relationship` takes Son and Daughter as well as Child; `related_to` says **whose** relative they are. Optional link to a Related Party where they also hold a role worth disclosing. | Hold a tax id — deliberately. A transfer below the IRS gift exclusion needs no W-9; somebody paid for work is a Contact or a Supplier and the posting should say so. Or record somebody as related to themselves. |
| `update_family_member` | Changes relationship, `related_to`, related party, active flag, notes. Retiring somebody is `active=false`, and the result says how many postings would have been orphaned by a delete. | **Rename them.** The name IS the docname and every journal entry that named them points at it. |

**Documents that get produced and filed**

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `generate_quarterly_investment_report` | Builds the quarter's report as a **PDF** and files it as a Prior Statement with the PDF attached: assets under management, activity, fee accrual, performance against a benchmark with a high-water mark, cash clearing, reconciliation state. | Run on a quarter that is not genuinely closed — see [A quarter closes when it closes](#a-quarter-closes-when-it-closes). Or invent a benchmark rate. |
| `generate_1099_prefill` | Aggregates a calendar year of supplier payments into an xlsx worksheet and a per-recipient 1099-NEC (Copies A, B and C), filed as a Tax Filing. | Finish the job: taxpayer ids print as the last four digits, and Copy A is stamped as an information copy rather than a filing. |
| `regenerate_governance_document_pdf` | Converts an archive entry's `.docx` attachment to PDF, attaches the PDF beside it, and repoints `attached_file` so a reader lands on something that opens. | Remove the `.docx` — it ADDS a fixed copy. Or guess between two `.docx` attachments, or replace an existing PDF without `overwrite=true` naming what it deleted. |
| `create_check_print_format` | Creates the Print Format that cuts a printed check out of a Payment Entry, laid out for US laser check stock — see [Cutting a check](#cutting-a-check). | Render MICR. That is printed in magnetic ink on the stock you buy, against your account. Or overwrite an app-shipped STANDARD format. |

#### A conveyance is not an edit

`update_parcel` refuses to move a parcel between entities, and that refusal is
right: ground changing hands has a date, an instrument behind it and
consequences for two sets of books, and a tool that let it happen by changing a
field would record none of them. `convey_parcel` is the door that refusal points
at.

**It deletes and recreates, which is the honest shape.** A Parcel's docname
encodes its entity — `Mill Creek - OML` on one set of books and
`Mill Creek - HLD` on the other, the same way every Account docname carries a
company abbreviation. There is no field to change that makes that true.

**The parcel's own short key is preserved, which is why the farm registers
survive.** Every Field, Irrigation Zone and Housing Unit is named
`<its name> - <PARCEL abbr>` — the parcel's key, not the company's — so all 29 of
a camp's cabins keep the docnames they have always had and only their `parcel`
link moves. A target entity already using that key is **refused** rather than
disambiguated, because a silently changed key would file the parcel's future
blocks under a different suffix from its existing ones.

**It writes no Journal Entry.** Recording that ground changed hands and booking
what that costs are separate acts — basis transfer and any gain or loss
recognised are entries with real tax consequences that somebody should write on
purpose, not produce as a side effect of filing a deed. The result names the
entries still owed. Same discipline as `close_note_payable`.

**Every refusal comes back at once.** A conveyance that failed on the lease, was
fixed, and then failed on the asset is two round trips to learn two things that
were both true from the start. `dry_run: true` returns the whole plan and the
whole refusal list without touching anything.

An appraisal report filed in the **old** entity's archive does not follow — a
Governance Document belongs to a company. That comes back as
`appraisal_document_status: "unlinked_needs_reattach"`, never as a silent null,
because "the appraisal needs re-filing" is real work and a quiet null is how it
gets forgotten.

#### "Son of whom?" — the family register answers it now

`Family.relationship` gained **Son** and **Daughter** in v0.13.0, beside `Child`
rather than instead of it: records already saying Child are still true, and a
register that forced a re-pick would be asking somebody to restate a fact that
has not changed.

The bigger gap was that "Alexander Polehn — Child" did not say **whose** child,
which is ambiguous the moment an entity has two members. `related_to` holds the
other person's name, and it is a **Data** field on purpose — a Frappe Link points
at exactly one doctype, and the answer here is a Family record, *or* a Related
Party record, *or* somebody in neither register. The tools resolve it on read and
report which register answered as `related_to_doctype`; `None` there means free
text, which is the designed fallback and not a failure.

`get_family_member` walks the chain and reports it as one sentence —
`Alex → Son of Tim → Manager of Orchard Meadow, LLC` — following `related_to`
upward through the family and then crossing **once**, at the top, through
`related_party` into the register that holds roles and entities. No single record
holds that, and the walk stops on a cycle, on a depth limit or on free text, and
says which.

**Nothing was backfilled, and nothing will be.** Which of two members somebody is
the child of is a fact only the family has, so records written before v0.13.0
arrive with `related_to` empty. `list_family_members` names them under
`without_related_to` and warns — that is the work list, not an error.

#### Streaming uploads: a file bigger than a tool call

`attach_file_to_document` accepts up to 8 MB of base64 in one call, and no caller
has ever reached that. The real ceiling is that an AI operator has to **compose**
the argument, and a base64 string lives inside the tool call it is writing —
which runs out around two hundred kilobytes. So every file-bearing operation
collapsed into the same four manual steps: write a Python script, `scp` it to the
box, `docker cp` it into the container, `docker exec` it. Per-parcel appraisal
PDFs. The master appraisal, three times, once per company after a conveyance.

Three tools replace that:

```
stage_file_chunk(session_id, chunk_index, total_chunks, chunk_base64)   × N
commit_staged_file(session_id, file_name, attach_to_doctype, attach_to_name)
```

**Cut the bytes, then encode.** Each `chunk_base64` is the base64 of **its own
slice of the file's bytes**. Do not base64 the whole file and then cut the
resulting string up — the middle pieces of that are not valid base64 on their
own, they cannot be checked when they arrive, and their per-piece hashes mean
nothing. The tool's refusal names this specifically, because a caller who has
done it will otherwise go looking for corruption in a file that is fine. Slices
need not be any particular size or divide evenly.

**The pieces are rows in a table, not entries in the cache.** That is the whole
design decision. A 5 MB upload is a hundred round trips over some minutes, and in
that window a `bench restart`, a worker recycle or a redis eviction under memory
pressure would throw the lot away — with the caller finding out at commit, having
spent the entire upload. Rows survive all of it. "Restart the bench halfway
through and finish the upload" is a test in the suite.

**Nothing is deleted until the File exists.** Every commit refusal — a missing
piece, a hash that does not match, a parent that is cancelled, a filename the
document already has — leaves the staged pieces exactly where they were. A
refusal is fixed by changing the argument, never by re-sending the file. The
target document is validated **before** a byte is reassembled, so a bad argument
costs nothing.

**`expected_sha256` is the point.** A hundred separate calls that each said
"fine" can still add up to a file that is not the one somebody meant to send. The
hash the caller computed before sending anything is the only thing that proves
otherwise, and it can be supplied on any call, including the last. Every piece
additionally records the hash of its own bytes, so a file that fails its
aggregate check is narrowed to the call that carried the bad piece rather than
reported as "the upload is wrong".

**A session belongs to whoever staged its first piece**, and only they may add to
it, commit it or cancel it. Not paranoia about other operators: two callers who
happened to pick the same session id would otherwise interleave their pieces into
one file, and the failure would present as corruption rather than as the
collision it is.

`commit_staged_file` has three destinations. Give `attach_to_doctype` and
`attach_to_name` to hang the file off a document; give `governance_document: true`
with a `title` and `category` to file a new archive entry with the file attached —
the same thing `attach_governance_document` does, for the documents too big to
send in one call; give neither and the File lands unattached.

**Staging cleans up after itself, twice.** A session is deleted on commit and on
cancel. Sessions idle for 24 hours are swept by a daily scheduler job **and** at
the top of every `stage_file_chunk` call. The second is the kairotic one — the
right moment to clear out abandoned uploads is when somebody is uploading, not at
three in the morning — and it is what keeps a bench with its scheduler switched
off from quietly accumulating ninety megabytes of a PDF nobody finished sending.

`list_staged_uploads` is the recovery tool: it reports which indexes are missing
as compact ranges (`3-6, 9`, not three hundred numbers) and which sessions are
ready to commit.

Ceilings: 200 KB of base64 per call, 600 pieces, 100 MB assembled. Past that, the
Desk's own upload control or a `file_url` this site can fetch is the better tool,
and the refusal says so.

#### Cutting a check

`create_check_print_format` writes the Print Format that turns a **Payment
Entry** — ERPNext's own check-cutting document — into a printed check. It is a
custom format, so `bench migrate` never overwrites it and a margin tuned for your
printer survives.

The layout is the standard US business one: **8.5 × 11, three 3.5-inch panels** —
check on top, remittance stub in the middle, remittance stub at the bottom. The
middle voucher tears off for the payee; the bottom one stays in the file. The
check panel carries the date, the payee, the amount in figures in a box, the
amount in words, the memo and a signature line. Both stubs carry the
invoice-by-invoice detail from the payment's references, which answers "what was
this for" without anybody having to ring up and ask.

**The amount in words follows US check convention** and not Frappe's
internationalised `money_in_words`:

```
1234.56  →  One Thousand Two Hundred Thirty-Four and 56/100
```

No currency word, because the stock already says **DOLLARS**. No "Only". A hyphen
inside the compound tens. The cents as a two-digit numerator over 100 — including
`00/100` on a whole-dollar amount, because a words line that stops at the dollars
is a line somebody can add to. The rest of the line is filled with asterisks for
the same reason.

##### The stock to buy

| | |
| --- | --- |
| **Format** | Laser check, **check on top**, two stubs. Sometimes sold as "check-voucher" or "voucher check". |
| **Deluxe** | Form **1000** / **9000** family (Business Voucher Checks). |
| **Costco Business** | Their laser voucher checks are the same layout at roughly half Deluxe's price. |
| **Intuit / QuickBooks** | Their "Voucher Checks" are the same geometry — the layout is an industry standard, not a vendor's. |
| **Paper** | 24 lb security paper with a chemical-wash stain, microprint border and a padlock icon. Do not print checks on plain paper. |
| **Envelope** | #10 double-window envelopes designed for voucher checks — the payee address block on the check panel lines up with the window when the sheet is folded in thirds. Confirm the window position against a sample before ordering a box of 500. |

**MICR is printed on the stock, by the people who sell it to you, against your
account.** Nothing here renders it and nothing should: it needs magnetic ink, the
right font at the right position, and a bank that has approved the layout. Order
the stock preprinted with your routing and account numbers and the check number
sequence. `reference_no` on the Payment Entry is what ERPNext records as the
check number — key it from the stock rather than expecting ERPNext to drive the
sequence.

**Ordering, in the order it matters:** confirm the bank's MICR specification (most
will send a spec sheet or approve a sample), order a small first run — 250, not
2000 — print one against blank paper, hold it over a real check at a window, and
adjust `margin_top` on the Print Format if the panels are off. Printers disagree
about the top margin by a few hundredths of an inch, and that is exactly what
that field is for. Only then order the box.

**Signatures.** `signature_image_url` prints a signature image above the line;
leave it empty and the line prints for a hand signature. A signature stamp or an
image on file is a control decision, not a technical one: anything that can print
a check can then print a signed check, so the usual arrangement is a
dual-signature threshold above some amount and a stamp kept where the person who
signs keeps it. The tool has no opinion; it prints what it is given.

#### When the voucher and the ledger disagree

Sprint 6 verification ran `update_journal_entry_party` against a $10 member
distribution and got `gl_entries_matched: 0`. The tool updated the voucher, left
the general ledger saying the old party, and returned a warning suggesting the
site was unusual. The site was not unusual.

**`GL Entry.voucher_detail_no` does not hold the account line's docname for a
Journal Entry.** That is the Sales Invoice Item convention. ERPNext's
`JournalEntry.get_gl_entries` fills that column from the line's
`reference_detail_no` — a pointer at a payment schedule row on an invoice being
settled — which is empty on every ordinary line. So a lookup keyed on it matched
nothing, on every submitted entry, for every account type. It looked like an
Equity quirk because that is where it was found. It was not.

v0.14.0 matches GL rows the way the ledger actually identifies a line — account
plus debit plus credit, preferring `voucher_detail_no` where a site does carry
one — and **refuses before writing anything** when the match is not certain:

- two lines of the same voucher with the same account and the same amounts are
  indistinguishable in the ledger, so choosing one would be a coin toss;
- ERPNext's `merge_similar_entries` collapses lines sharing an account, a party
  and a cost center into ONE summed row, so writing a party onto it would
  attribute somebody else's money to this party;
- a line that posted no GL row at all is a fact worth reporting rather than a
  count of zero to shrug at.

`allow_unmatched_gl: true` goes ahead anyway and the result leads with the
disagreement, because a refusal a caller cannot get past is how a safety gate
becomes the failure.

**`investigate_je_gl_link` is the tool that settles this kind of question.** One
call, read-only: every line of a Journal Entry beside every GL Entry row it
posted, with `account_type` and `root_type`, the party on each side, which lines
disagree with the ledger, which GL rows no single line explains, and a `finding`
that says in one paragraph what the counts mean. It works on drafts and on
cancelled entries and says which case it is looking at.

**If you ran v0.13.0's version of the party tool against a submitted entry, the
ledger still says what it said before.** `investigate_je_gl_link` shows which
entries are in that state — look for lines under
`lines_whose_party_disagrees_with_the_ledger`.

**v0.15.0 finds and repairs them wholesale.** `investigate_je_gl_link` answers
for one voucher; `find_drifted_je_attributions` scans a date range and reports
every drifted line with both sides, in three queries, matched by the same
function the repair writes through. Its `repair_input` is what
`repair_drifted_je_attributions` takes verbatim — dry run defaults TRUE — and the
repair moves no balance, because `party` is an attribution column and every debit,
credit, account and date is refused as an argument.

Lines whose GL rows cannot be identified with certainty are reported separately
as `ambiguous` and left out of `repair_input`: reporting a coin toss as a finding
would be worse than reporting nothing.

**And v0.15.0 fixed one more thing v0.14.0's fix left behind.** v0.14.0 corrected
the matcher but kept an idempotence check that read only the VOUCHER — so on a
line damaged by v0.13.0, where the voucher already says what you are asking for,
it refused with "nothing to change". That is exactly backwards: the voucher
agreeing is the *signature* of the damage. Nothing to change now means nothing to
change anywhere, a voucher that agrees over a ledger that does not is a GL-only
repair, and the result says `gl_only_update: true` so nobody mistakes it for a
fresh attribution.

#### Wiring a company's defaults

`set_company_defaults` only sets fields you already know the accounts for. Run
against four freshly-created companies it comes back "idempotent" for the four
`create_company` already did and says nothing about cash, bank, income or
expense — because nobody passed them. A company with no `default_income_account`
does not fail loudly; it fails weeks later, the first time somebody saves an
invoice line with no account on it.

`bulk_wire_default_accounts` **finds** them. In order: your `overrides`; then the
well-known account number for the chart template (1310 receivable, 2110 payable,
1140 cash, 1110 bank — descending into the sub-ledger when the number names a
group, as ERPNext's 1110 "Bank Accounts" does, 4100 income, 5100 expense, 5212
round off, 5218 write off); then an account whose `account_type` means the right
thing; then an account whose **name** says so (an account literally called "Write
Off" is evidence, not a guess); then, only where the field permits an untyped
account, the first leaf of the right root type.

**Every candidate has to pass the same type checks `set_company_defaults`
applies to a hand-written value.** The search proposes; those rules dispose. A
`1310` that exists and is a plain Asset rather than a Receivable is *not used* —
ERPNext keys party ledgers off `account_type`, so a `default_receivable_account`
pointed at the wrong kind of account posts fine and stops ageing correctly a
quarter later.

**It never fills a field with something merely plausible, and it never sulks.**
A field nothing matched is reported in `unresolved` with what was looked for and
how to fix it, and every other field is still wired — a company with nine of ten
defaults set is better off than one with none, and a chart with no Cost of Goods
Sold account is ordinary rather than broken. `strict: true` refuses the whole call
instead. An `overrides` value that cannot be resolved is always a hard refusal:
an explicit instruction that cannot be honoured is a different thing from a search
that came up empty.

Deterministic, so "idempotent" is true on the second run: where two accounts of
the same type exist, the lower account number wins, every time.

#### Four digits, never nine

`Related Party.tax_id_last4` takes exactly four digits and refuses nine — not
truncated, not masked, not accepted with a warning. The refusal names the four
digits to send instead, because a validator that says "invalid format" to
somebody who has just pasted a real SSN has told them nothing about why it
matters.

The controller enforces the same rule, because the Desk form is a second door
into the same field, and the field is declared four characters long as the belt
to that brace. `get_related_party` never returns more than four digits **even
from a linked Supplier** — `supplier_detail.tax_id` says only whether one is on
file.

#### Two party types, and one exclusion

ERPNext ships Customer, Supplier, Employee and Shareholder. A family operation
pays two kinds of people that fit none of them, and v0.12.0 registers both:

- **`Family`** — a relative receiving money that is neither payroll nor a
  purchase. `generate_1099_prefill` **excludes** those postings and reports the
  count, the total and the names, so "nobody looked" and "somebody looked and
  excluded them" are different-looking answers. A transfer below the IRS annual
  gift exclusion is not compensation for services: it needs no W-9 and produces
  no form. Without this party type those payments end up recorded as Supplier
  payments, which puts family money into vendor spend **and** onto a 1099 the
  recipient owes no tax on.
- **`Contact`** — the consultant who looks at the orchard twice a year, the
  neighbour who runs a tractor for a weekend. Not a formal Supplier, but paid
  for services. The pre-fill reads those postings and classifies them
  **borderline**, naming the W-9, rather than dropping them — which is the same
  mistake in the other direction.

Both are seeded on install and on every `bench migrate`, and neither touches
anything already recorded: an existing rule or Journal Entry using Shareholder,
Employee or Supplier keeps working exactly as it did.

**A party type's name has to be the name of a DocType**, and that is not a
convention — it is how ERPNext resolves a posting. `Party Type` names itself
`field:party_type`, and that field is a `Link` to `DocType`; a Journal Entry line
then carries `party`, a **`Dynamic Link`** resolved through `party_type`. So
`party_type = "Family"` requires a DocType called `Family`, and
`party = "Alex Bramwell"` requires Alex to be a record in it.

`Contact` already has one — Frappe ships it — which is why it registered
successfully in v0.12.0 while `Family` did not. **v0.12.1 ships a `Family`
DocType**: a small register of name, relationship, an optional link to the
related-party entry, and an active flag. It holds no tax id on purpose. A
relative who is genuinely paid for work is a Contact or a Supplier, and the
posting should be reclassified rather than the exclusion widened.

If a party type ever cannot be registered — its DocType is missing — the seeder
**skips it with the reason and carries on**. It does not raise: an exception
inside a patch aborts `bench migrate` for every app on the bench, not just this
one.

#### The polygon is the evidence, and the index has to be a superset

A boundary is not decoration on a Field. "Which block was sprayed" is a Worker
Protection Standard answer, "which zone got the water test" is an FSMA Subpart E
answer, and "was the worker in an authorised area" is both a payroll question and
a food-safety one. Every one of those resolves to a shape on the ground — without
it the record says a name, and a name is not something anybody can check against
a GPS fix.

**Everything derived is derived on save and cannot be set directly.** Centroid,
bounding box, H3 coverage and computed area are all functions of the polygon. A
figure a caller could edit independently is a figure that will disagree with the
shape, and the disagreement surfaces as a geofence saying no to somebody standing
in the right place.

**The H3 fill stores every cell the shape *touches*, not every cell whose centre
is inside it.** This is the one implementation detail worth knowing. H3's default
polygon fill is centre-based, and an orchard block is smaller than one H3 cell at
resolutions 6, 7 and 8 — so the default returns an **empty set** for a real
field, and an index built on it answers "in no field" for a point plainly in one.
A false negative that reads like a policy decision is exactly what a geofence
must not produce, so the fill uses `contain="overlap"`, which is a true superset.

For the same reason `find_fields_containing_point` narrows with the **bounding
box** rather than with the H3 cells: a bbox is a guaranteed superset of the shape
it bounds, so a candidate set built from it cannot miss the right answer. The
exact point-in-polygon test then settles every candidate, and the boundary counts
as inside — a pick recorded on the headland is in the block.

**Area is spherical, and said so.** `shapely` computes area in the units of its
coordinates, and these coordinates are degrees, so `.area` is degrees squared —
not an area of anything. The computed acreage uses the standard spherical-excess
integral, which differs from a WGS84 ellipsoidal area by well under 1% at these
latitudes. That is far inside the 5% and 25% thresholds it is compared against,
and a projection library to close a gap smaller than the disagreement between a
deed and a GIS trace would be precision theatre.

The satellite fields (`satellite_provider`, `imagery_asset_ref`,
`last_ndvi_pull_date`, `last_ndvi_mean`, `last_ndvi_stddev`) are schema only in
this release — nothing fetches imagery yet. NDVI is stored on its real range of
**-1 to 1**, not 0 to 1: water and bare soil read negative, and clamping the
floor to zero would make a flooded block indistinguishable from an unmeasured
one.

#### Compliance lives on the record it belongs to

The food-safety fields are on `Field`, the water-quality fields are on
`Irrigation Zone`, and the habitability and detector dates are on `Housing
Unit` — not in a compliance register beside them. The test is whether removing a
field breaks **operations** or only breaks **reporting**:

- Remove `last_spray_date` and the Worker Protection Standard report loses a
  line — *and* nobody can answer whether the re-entry interval on block 3 has
  run, which is a question a crew is waiting at the gate for.
- Remove `worker_hygiene_station_present` and an inspector loses a checkbox —
  *and* dispatch loses the fact that decides whether a crew may work that block
  at all.
- Remove a Housing Unit's condition and a habitability finding disappears —
  *and* `create_housing_assignment` stops refusing to put somebody in an
  uninhabitable cabin.
- Remove a Field's boundary and the spray record loses the thing an auditor
  checks a GPS fix against — *and* the geofence has no answer for a crew standing
  in the block.

Each of those has a test that asserts **both halves of the same removal**. A
separate "Field Compliance Log" that somebody fills in after the fact would fail
that test — nothing about picking would stop if it disappeared — which is why
this release does not have one.

The difference between a site holding four digits and a site holding nine is the
difference between an inconvenience and a notifiable breach. The full number
belongs on the signed W-9, on paper, in a drawer.

#### A quarter closes when it closes

`generate_quarterly_investment_report` refuses a quarter that is not ready, and
names everything that is missing in **one reply** rather than one per call. Four
things must be true:

1. the quarter has ended;
2. the custodian's statement is filed as a **Prior Statement** governance
   document with an effective date inside it;
3. no journal entry touching the investment accounts is still a draft;
4. no bank transaction in the period is unreconciled.

A report generated on a calendar date regardless of state is a report whose
numbers may be wrong, signed by somebody who assumed the schedule meant
something. `dry_run=true` runs every precondition and computes every figure
without writing.

It also invents nothing. Without `benchmark_rate_percent` the return over
benchmark and the performance fee are **not computed** and say so — they are not
zero and not estimated, because a performance fee against an assumed benchmark of
nothing overstates what the manager is owed. Holdings are the same: this app
reads one ERPNext site and the custodian's positions are not on it, so pass
`holdings` and the report reconciles the snapshot against the ledger, or omit it
and assets under management are the ledger balance, stated as such.

#### Where a generated document goes

Both generators file their output as a private File attached to a Governance
Document. That is the deliverable: permissioned, version-tracked, backed up with
the site, readable through `get_attachment_content`.

`output_path` is the second destination, for a batch of forms somebody has to
print. It is confined to the site's own `private/files` and `public/files` — a
relative path lands under the first — and everything else is refused, naming the
roots. The check is on the **resolved** real path, so a symlink cannot be used to
step outside, and an existing file is never overwritten unless `overwrite=true`.
A bad path refuses the whole run before the first write rather than leaving an
archive entry behind.

#### A fiscal year is a permission for a date

ERPNext refuses any posting whose date falls outside a Fiscal Year, and it
refuses it from *inside the document being saved* — so on a site whose only year
is 2026, booking a March 2025 equipment transfer fails with an error about a
date rather than about a missing year.

That makes `create_fiscal_year` the prerequisite for everything historical:
`set_opening_balance` and `create_journal_entry` cannot reach a period until the
year exists. Creating one books nothing and moves no balance; what changes is
which dates the ledger will accept.

A year with **no** `companies` applies to every company — that is how ERPNext
models a global fiscal year, and it is the default here. The overlap check
follows the same model: a global year collides with everything, two restricted
years collide only where they share a company. Disabling a year does not free its
range.

#### A note payable is not ERPNext's Loan

ERPNext's Loan module models the company as the **lender**: an application, a
disbursement, a repayment schedule, its own accounting. A holding company with
four notes outstanding is on the other side of all of it.

What a `Note Payable` record adds to the liability account that already exists is
three things the balance cannot tell you — the **terms** (rate, maturity,
frequency), the **provenance** (what was agreed, by whom, where the original is),
and **what it secures**. That last one is what lets
`depreciation_note_alignment_check` see whether a financed asset and its
financing still end in the same month.

`principal_outstanding` on a note is a **convenience figure** maintained by
`record_loan_payment`. The ledger's answer is the balance of the linked GL
account, and the two diverge by every payment recorded as a draft nobody has
posted — which, in an app where nothing submits, is the normal state. Every
response that reports the field says so.

#### Members are anonymous on purpose

Family names never go into the chart of accounts or the cost center tree. Those
are read by lenders, auditors and anyone handed an export, and a name that has
reached a statement cannot be taken out of it again.

So a posting is tagged with a **Member accounting dimension** value —
`Member-01` — and exactly one doctype, `Cap Table Entry`, says who that is. The
mapping can be granted to the people who need it without granting the ledger,
and the ledger can be read by everyone else without granting the mapping.

Cost centers stay what they are: segments of the *business*, not of the family.
A `Cap Table Entry` keeps an optional `member_cost_center` for sites whose
convention already gives each member one, but every tool files by the dimension.

```
Journal Entry line   →  account 3100 Member Capital, member = Member-01
Cap Table Entry      →  Member-01 = The Example Family Trust, admitted 2020-06-15, 60%
```

#### Depreciation is written here, not by ERPNext

An asset created by `create_asset` has ERPNext's `calculate_depreciation` set to
**0**, and that is deliberate. ERPNext runs a daily job that posts depreciation
for every asset with the flag set, through its own schedule and its own single
cost center. If it also ran, an asset would depreciate twice — silently, every
month. This app owns the schedule for the assets it creates, and
`run_depreciation_cycle` is the only thing that writes for them.

An asset you created in the Desk yourself is untouched by any of this: it has no
Asset Cost Profile, so these tools refuse it and ERPNext keeps depreciating it
exactly as before.

#### Building a chart from scratch

The four-step loop the chart tools are designed around, each step reviewable:

```
propose_clean_chart(company)                          → a proposal, nothing written
  ↓ delete what you do not want
import_chart_of_accounts(company, accounts)           → the full plan, nothing written
  ↓ read the plan
import_chart_of_accounts(company, accounts, dry_run=false)
  ↓
disable_account(...)                                  → retire the bundled defaults
```

Importing **adds** roots alongside whatever the company already has rather than
replacing them, because ERPNext will not let a root be edited or moved once it
exists. `propose_clean_chart` tells you which roots are already there and which
of the template's numbers are already taken, so you know before you run it.

Templates live in `erpnext_mcp/charts/` as plain Python literals with no
database dependency — which is what makes a proposal reviewable, diffable and
version-controllable before anything happens. The package auto-discovers them,
so adding `us_c_corp` is one file.

The one that ships is **`us_llc_farm`**: 81 accounts (17 groups, 64 ledgers) for
a US farming LLC that also runs an investment book. Deliberately compact — nine
flat operating-expense buckets rather than thirty-five, because a chart with a
line for every conceivable cost is one where nobody finds the right line. Three
things it does that a generic chart does not:

- **Crop labour is separate from administrative wages** — `5150 Direct Farm
  Labor` under COGS, `6100 Payroll & Benefits` under operating expenses, and
  `6150 Employer Payroll Tax Expense` split out again so wage cost and true cost
  of employment read apart.
- **The trading segment is its own range set** — assets `1800–1849`, income
  `4200–4249`, losses and costs `7300–7339`, unrealised movement `3500`. Filter
  a P&L to those and you have the investment book, running costs included;
  exclude them and you have the farm. No dashboard required, and a test fails if
  an account outside those ranges ever starts reading as trading.
- **`2120 Current Pay Period - Due to Employees` is a live balance**, not an
  accrual: updated continuously as work lands, flushed to zero at payroll. Its
  description says so, because the account only keeps that meaning if nobody
  books a month-end adjustment into it.

#### Adding an accounting dimension

An ERPNext accounting dimension does not hold its own values. It **points at a
DocType**, and every record of that DocType is a value — which is why setting one
up is three calls rather than one:

```
create_accounting_dimension(dimension_name="Member",
                            create_master_if_missing=true)   → the DocType, the
                                                               dimension, and a
                                                               Link field on each
                                                               target doctype
  ↓
create_dimension_value(dimension_name="Member",
                       value_name="Member-01")               → one value
  ↓
create_journal_entry(accounts=[
  {"account": "3100", "debit": 5000, "dimensions": {"member": "Member-01"}},
  ...
])
```

Two things worth knowing before the first call. **"Journal Entry" means the
line**: ERPNext carries dimensions on `Journal Entry Account`, never on the
header, because one entry books to several — the tool redirects and says so in
its response. And the dimension's **fieldname is the scrubbed label**, so
`Member` becomes `member` and `BBCH Stage` becomes `bbch_stage`; that is the key
you use in a line's `dimensions` object, and an unknown one is refused by name
rather than dropped.

**Compliance evidence, and the calendar over it**

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `install_compliance_fields` | **The one mutating tool that ships ON.** Adds the compliance columns to Spray Log, Employee and the BucketLog bridge as Custom Fields, and verifies the ones already on Housing Unit and Field. Idempotent; reports the backlog of rows a now-required field makes unsaveable. | Touch a single record. It writes columns, not data — and a `verify` target with a missing column is REPORTED rather than patched, because a Custom Field over an unfinished migration gives a site two columns and no error. |
| `create_compliance_policy` / `update_compliance_policy` | Registers a written procedure at one version, with the document attached. | Re-key it, set either end of the version chain, or take a review date that precedes the effective date. |
| `supersede_compliance_policy` | Replaces one procedure with another, writing **both ends of the chain in one act** with a mandatory reason on both timelines. | Let a policy supersede itself, give one two successors, or accept a successor whose effective date predates its predecessor. Nothing is deleted — a superseded procedure was correct on the dates it governed. |
| `create_certification` / `update_certification` | Registers a certificate or licence with the date it stops being a defence, and the issuing body's real lead time. | Take an expiration before the issue date, or **move an expiration forward** — that is a renewal, and editing it in place would produce a certificate that looks as though it never expired. |
| `renew_certification` | Moves the expiration out and records what was actually done to earn it, appending to a history. | Hide a lapse. A renewal recorded after the previous expiry reports the gap in days, because renewing late does not close one that already happened. |
| `create_regulatory_filing` / `update_regulatory_filing` | Records what went to an agency, when, under what docket number, and what came back. | Mark a filing Submitted with **no submission date**, accept a response dated before the filing, or take a submission date in the future. |
| `create_audit_event` / `update_audit_event` | Records an audit with its findings and one row per thing to fix; adds, amends and closes individual corrective actions. | Close an action without saying what actually changed — a tick in a box is what an auditor is trained to disbelieve. Or set the audit's closure date; that is `close_audit_event`. |
| `close_audit_event` | Declares an audit finished, with a mandatory note on how the closure was accepted. | **Close it while any corrective action is open** — refused in the tool AND in the controller, because that date is what `generate_audit_packet` reads as "finished". |
| `refresh_compliance_alerts` | Runs the whole rule set now instead of waiting for tonight. Creates, refreshes, reopens and auto-dismisses. | Touch any operational record — every rule is a read. Or duplicate an alert: the docname carries the rule and the source and nothing that changes daily. |
| `snooze_alert` | Hides one alert until a date. Not a dismissal: the condition is still true and it comes back on its own. | Take a date that is not in the future — an expired snooze is not a snooze. |
| `dismiss_alert` | Takes one alert off the calendar, with a **mandatory reason** — the only part of the record nobody can reconstruct, and the answer when the same finding turns up next year. | Delete the alert, or change anything underneath it. Dismissing one about an expired certificate does not renew the certificate. |
| `dismiss_compliance_alert` | The same dismissal, gated on the alert's own **May Be Dismissed From The Field** box — which defaults off. It is what the Farm Ops app calls, and what a model reading a calendar should call: whether an obligation may be closed without being met is a judgement somebody records in advance, per alert. | Close anything nobody marked dismissible, or take a word where a sentence belongs. |
| `dismiss_alert_bulk` | Dismisses every alert matching a filter, one reason for all of them. | Write on the first call — **dry run defaults TRUE**, because the whole calendar is one filter away. Or run with no filter at all. |
| `generate_audit_packet` | Assembles one audit type's evidence for one period into a PDF and files it as a Governance Document. Pulls from the operational records, not from a copy. | Produce a packet for a period that has not finished, or one whose corrective actions are still open — refused, with every open action named, because a warning on a printed document is not read by the person holding it. |
| `repair_drifted_je_attributions` | Brings drifted GL Entry rows back into step with their vouchers, in a batch, from `find_drifted_je_attributions`' own output. | Move a balance. `party` is an attribution column, so the trial balance after two hundred repairs is arithmetically identical. Nor abort on the first failure: each item is a different voucher. |

**Farm Task Dispatch, and the evidence a completion produces** — the v0.16.0 surface

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_farm_task` | Raises one piece of work with its type, location, skill, urgency, dispatch mode and the record completing it produces. | **Exist without an evidence contract.** `evidence_required` is mandatory, an empty one is refused, and so is a misspelt key — `{"photo": true}` asks for nothing while looking exactly like a photograph requirement. Nor promise a `creates_record` this site does not have, nor answer an alert that already has a task. |
| `assign_farm_task` | Sends a named person to a task — the foreman's half of the dual mode, for work where the named holder matters. | Take work off somebody who already holds it without `reassign=true` **and** a reason, which is written onto their assignment. Or reassign finished work: that rewrites history rather than dispatching anybody. |
| `claim_farm_task` | A worker takes one from the pool, and is told the evidence they will need. | Hold more than **three at once** — a hoarding limit, not a productivity one; completing one frees a slot in the same instant. Or claim Dispatched work: self-picking it would put the wrong person's name on a regulated record. |
| `start_farm_task` | Clocks in on **this task**, not on the shift. | Start twice. That would move the clock-in forward and shorten the hour actually spent. |
| `complete_farm_task` | Checks the evidence against the contract, files it, and **writes the compliance record the task promised** — the actual Housing Inspection, Detector Test or Water Test, with the photographs on it. | Accept a submission short of the contract, naming each requirement that is missing. Or accept a completion filed by anybody **other than the worker holding the task** — that is not a chain of custody, it is a rumour. |
| `reject_farm_task` | Hands one back with a **mandatory reason** and returns it to the pool. | Lose the rejection. The assignment stays: it is the proof somebody was sent, went, and could not do it, which answers an auditor in a way an absence never does. |
| `generate_tasks_from_compliance_alerts` | **The bridge.** Turns every open alert into a dispatchable task carrying the evidence its completion must produce, with the shape of the work inferred from the rule. | Duplicate. A task carries the alert that raised it, so a second run finds what the first did and skips it — two people are never sent to walk the same cabin. Nor invent a recipe for a rule it does not know: that is reported by name. |
| `create_housing_inspection` / `update_housing_inspection` | Records one habitability walk and moves the unit's inspection date forward, which is what dismisses `housing_inspection_overdue`. | Be argued into looking clean. The state is **computed from the findings** on every save, so somebody who typed "water stain, north wall" is not offered the option of marking it passed. Nor drag a register backwards with a back-dated walk. |
| `create_detector_test` / `update_detector_test` | Records a smoke and CO test, moves the unit's detector dates, and **raises a Farm Task** where a replacement is needed. | Write a date for a detector recorded **Not Present** — there was nothing to test, so nothing is known, and the calendar should go on saying so. A *failed* test does write the date: the ignorance is over. |
| `create_water_test` / `update_water_test` | Records one sample and moves **both** the zone's and the parent block's test date, because Subpart E is engaged by water contacting a crop and the crop is on the block. | Treat an unreadable result as a pass. Where neither the words nor a number can be read it routes to Corrective Action Required, because a clean record of nothing is worse than no record. |

**Mobile accounts, credentials, and the phone's own writes** — the v0.17.0 surface

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_mobile_user` | One call for four Desk forms: the User, one of the six roles, a Company User Permission per entity, the grant record, and the API credential — readable in the result **exactly once**. An update leaves an existing credential alone, because re-scoping somebody should not knock their phone offline. | **Make an account with no entities.** In Frappe that means it sees EVERY company, so it would be the least scoped account on the site. There is no flag to override it. Nor rewrite a live account's roles and scoping without `update_existing=true`. Nor grant a permission on a doctype another app owns — see [the Custom DocPerm trap](#multi-entity-scoping-and-the-six-mobile-roles). |
| `revoke_mobile_user` | Ends one account: disables the login, destroys the credential, and **records why**. | Accept a blank or throwaway reason. 'Left at the end of harvest' and 'dismissed for cause' are different answers to the same auditor question and the grant is the only place either survives. It also does **not** strip the roles — an account stripped bare is one nobody can be asked "what could they see". |
| `generate_api_token` | Mints a fresh Frappe API key/secret and returns the pair. Issuing a new one stops the previous one working, which is what makes this the answer to a lost phone. | **Expire.** `expiry_days` sets a REVIEW DATE and the result says so: Frappe API secrets do not expire on their own and this app installs no job that revokes one. `list_mobile_users` flags an overdue grant; `revoke_api_token` is what ends it. Nor mint for a disabled login. |
| `revoke_api_token` | Destroys the credential and leaves the account enabled — 'they lost their phone', where `revoke_mobile_user` is 'they no longer work here'. | Leave half of it behind. Both the key and the secret go: an api_key on the row reads like a live credential to anybody scanning the User list. |
| `generate_mobile_login_qr` | The enrolment card: a scannable PNG carrying the public URL, the user and the credential, base64 in the result and optionally archived as a **private** attachment for a camp office with no signal. | Point at a plaintext endpoint — encoding a live credential for `http://` puts it on the wire in the clear at every call, forever. Nor stay valid: 24 hours to enrol by default, and `rotate_token` (default TRUE) mints a fresh secret so an older photograph of an older card stops working. |
| `claim_task_via_mobile` / `start_task_via_mobile` / `complete_task_via_mobile` | Sprint 8's claim, start and complete with the worker resolved from the authenticated request instead of named in the body. | **Add a rule or weaken one.** The concurrent-claim limit, the refusal to self-pick Dispatched work, the evidence-contract check and the empty-string findings distinction all still come from `claim_farm_task` / `start_farm_task` / `complete_farm_task`, because they ARE those tools. |

**The person behind the login** — the v0.18.1 surface. Every mobile method scopes
work by **Employee**, not by User, so an account with no Employee behind it enrols
perfectly and then gets refused with "set `user_id` on their Employee record to
this email address". These are what fix that without leaving the conversation.

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_employee` | One Employee record — the identity every task board, evidence row and payroll register names. Nineteen fields; Links checked against this site's own Branch / Department / Designation / Employment Type / Gender records, Selects against this site's own options, and both refusals list what is actually available. The three compliance statuses **this app** installs as mandatory (`i9_status`, `w4_status`, `jurisdiction`) start at Pending, Missing and the hiring entity's own state — a hire is not refused for want of a field erpnext_mcp is the reason for, and every default is named in `defaults_applied`. | **Write a payroll, tax or banking field.** Salary structure, income tax slab, CTC, bank account and provident fund each have a form, an approval and a retention rule this app knows nothing about; they are refused by name. Nor silently drop a field this site's doctype lacks — it is reported. Nor make a second record for a name this company already has, without `allow_duplicate_name=true`. Nor default a field the **operator** made mandatory: nobody can invent a date of birth, so that refusal stands. |
| `update_employee` | The same nineteen fields on a record that exists, reporting **field by field what actually changed**, with the previous value. A value that already matched lands in `unchanged`. This is how a hire created at `i9_status` Pending becomes Verified, and how a crew that crossed the river gets the `jurisdiction` its wage law actually follows. | Re-point an Employee at a different login without `replace_user=true` — that moves the person's whole task history with it. Nor reach the payroll fields, for the same reason as above. |
| `link_employee_to_user` | Sets `Employee.user_id`, and reports **whether the phone will now work** — `farm_ops_ready` is true only when the account holds a Farm Ops role, its grant is Active and the Employee is Active, and the note names whichever of the three is missing. | Link a User that already belongs to another Employee, in either direction — one login resolving to two people gives `list_my_tasks` two answers where it needs one. Nor link an account with no Farm Ops role and no grant, without `allow_unenrolled_user=true`: that link changes nothing today and silently grants a task board on the day somebody grants the account a role for an unrelated reason. |

**The training register** — the v0.19.0 surface. Eleven compliance rules watched
certificates, policies, cabins, water, filings and audits, and none watched what
the crew was actually taught: WPS every twelve months, Oregon's heat rule
annually before the first 80 °F shift, FSMA Subpart C on hiring and periodically,
GAP annually with the signature attached. A single session can satisfy all of
them, so **one record carries a tag list** and every audit packet pulls the subset
that audit is entitled to see.

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `record_training` | One training event tagged with every regime it answers, with the §112.161 fields written at the time — trainee signature, farm name snapshotted onto the record, a date-and-time activity stamp. Retention follows the **longest** tag: five years where any is NOP, two for FSMA and WPS. | File a record with no regimes or no topics covered — an untagged record appears in no packet, and a regime tag without the topics is a claim an inspector will disallow. Nor accept a near-miss tag (`OSHA` for `OR-OSHA`), a training dated in the future, or an expiry before the completion. Nor **edit** last year's record on a renewal: it adds one and names what it supersedes. |
| `list_trainings` | The register filtered by regime, status, expiry window, person or period — which is how an audit packet is assembled. Reports `without_supervisor_review` (the FSMA §112.161(b) gap) and `without_trainee_signature` (§112.161(a)(4)). | Match a regime by substring. `GlobalGAP` contains `GAP`, and a `LIKE` filter would hand a USDA GAP auditor evidence from a different scheme — matching is by tag. Nor read `status` off the stored column: it is computed as of today, because a record last saved in March holds March's answer. |
| `get_training` | One record in full, that person's whole training history, the retention period **with its citation**, and the §112.161 elements this record lacks in the rule's own terms. | Fix those gaps. A signature added now is a signature dated now, and a record assembled before an inspection is what an inspector is trained to spot. |
| `sign_training_supervisor_review` | The FSMA §112.161(b) review — reviewed, dated and signed by a supervisor within a reasonable time after the record was made. **The requirement USDA GAP does not have and FDA cites most.** Reports the lag and flags it when long. | Accept a self-review, a supervisor from another entity, or a review dated before the training it reviews. Nor overwrite an existing signature without `replace_reviewer=true`. It is a **separate call** from `record_training` on purpose: simultaneous timestamps are the shape of a record an inspector reads as assembled rather than kept. |


**Crew shifts and heat exposure** — the v0.19.3 surface, and an architectural
move rather than another register. **Compliance anchors to a SHIFT, not to a
task.** A task completion carries a point-in-time reading; a shift carries a
timeline. Oregon OSHA does not ask what the temperature was when one job closed —
it asks whether the July 15 shift complied with OAR 437-004-1131 from start to
finish, and only a record spanning the exposure period can answer.

**The foreman is the sole actor, and that is a compliance decision.** There is no
clock-in tool here and there will not be one. -1131 puts the water, shade,
rest-cycle and observation obligations on a *named* responsible person, and FSMA
§112.161(b) asks that person to sign — so a crew of thirty each clocking
themselves in is a shift with thirty people responsible for the record, which is
a shift with nobody responsible for it. The observable failure is that nobody
logged the water break because everybody assumed somebody else had.

**Per-worker attendance survives inside the crew envelope.** Every crew row
carries its own `joined_at` and `left_at`, and closing the shift writes one
submitted `Attendance` per person for *their own* span — so farm_hr keeps one
canonical answer to "when was Ana at work" without the shift stopping being one
record.

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `start_shift` | Forms a crew at a place and starts the exposure period. Everybody rostered at the start joins at the **shift's** start, not at the moment the API call landed. `farm_location_gps` is the weather anchor v0.19.4 fetches against. | Roster a crew crossing entities — and it checks before writing anything, because a shift half-created and then refused leaves an open shift nobody meant to open. Nor take more than 60 names: past that it is a company roster, and every extra name is a wrong Attendance row when the shift closes. |
| `add_worker_to_shift` | A late arrival or a transfer. `joined_at` defaults to **now**, which is the opposite of `start_shift`'s default and right for the same reason. | Put the same person on a crew twice. Two rows look deliberate on the form and become two Attendance days for one person on close. Nor add to a closed shift, whose payroll rows are already written. |
| `remove_worker_from_shift` | Ends one worker's time on a shift that continues. **It sets `left_at`; it does not delete the row.** | Be called twice without an explicit time — a silent second call would move a departure that has already happened to now, and lengthen a day that has already ended. |
| `log_shift_event` | Water break, shade break, rest cycle, supervisor observation, cool-down — at the moment it happened, with an optional photo and a pointer back at whatever produced it. **The timeline is the evidence; the heat record is only the claim.** | Refuse an event timestamped slightly outside the shift. A clock five minutes out is not a false record, and refusing would mean the break goes unlogged rather than logged approximately — it is kept and reported. |
| `end_shift` | Closes it with the supervisor's signature and writes one Attendance per crew member for that person's own span, submitted. | Close without a signature. An unsigned close is an `UPDATE` setting a timestamp, and §112.161(b) asks for a review dated **and signed** — the shift stays open and nothing is written. Nor let the Attendance bridge block the close: a site with no Frappe HR or an archived employee is **reported**, because the signature is the compliance act and the payroll row is the convenience. |
| `create_heat_exposure_event` | The OAR 437-004-1131 record for one shift, signed and submitted — water, shade, rest **taken** rather than offered, observation, training, acclimatization plan by name. | File a second record for one shift: two records about one exposure period will disagree, and the one an inspector finds is whichever was filed second. Nor claim verified training the register contradicts **as of the day of the shift** — the same packet carries both, and a packet that contradicts itself is worse than one with a gap. Nor accept signs observed with no response and no explanation. |
| `list_shifts` / `get_shift` | The register, and one shift with its crew spans, its timeline, its weather and its heat record — **the evidence chain an inspector is handed.** | Read `status` off the stored column: it is computed from whether the shift has an end time, because an *open* shift is what the weather sweep walks. |
| `list_heat_exposure_events` / `get_heat_exposure_event` | The heat register, and one record with the obligations it does **not** claim were met, in the rule's own terms. | Fix those gaps. A tick added now is a tick added now, and a record completed before an inspection is what an inspector is trained to spot. |

The thirteenth compliance rule came with them. `supervisor_review_lapsed` watches
a signature that was never put on a record — Warning at 14 days, Critical past
30, auto-dismissed the moment somebody signs. Its clock runs from when the record
was **made**, not from the activity date, because §112.161(b)'s own phrase is
"after the records are made" and reading the activity date would raise a Critical
on every season somebody backfilled. It walks a *table* of doctypes carrying the
§112.161(b) columns, so the next one is a one-line addition rather than a rewrite.

`Attendance` gains a `farm_shift` Custom Field — the fourth column this app
grafts onto a doctype it did not create, declared beside the other three and
argued for in [Compliance is not a module](#compliance-is-not-a-module).
Without it a shift-formed day is indistinguishable from a hand-keyed one, so
nobody reading the register can reach the conditions the person worked in — and
the bridge, unable to tell its own rows from anybody else's, would pay somebody
twice for one afternoon.

**The weather timeline** — the v0.19.4 surface, and the half of a shift's
evidence nobody could produce before. A foreman's logged water break says what
was *done*; the timeline says what it was done *about*, and OAR 437-004-1131 is a
rule about conditions.

**The mechanism is mostly a schedule.** A `*/15 * * * *` cron walks every shift
with no `end_datetime` and a `farm_location_gps`, asks Open-Meteo what the
conditions are there, and appends a reading. Fifteen minutes rather than hourly
because nine readings on a nine-hour shift is a sketch and thirty-six is a
timeline. Open-Meteo needs no API key, which is a reason to be *more* careful
with it: the service caches by coordinate, skips a shift read within
`fetch_interval_minutes`, and treats a 429 as an instruction — exponential
backoff per coordinate, capped at an hour. Nothing raises. A failed fetch is a
missing reading, and a shift with a gap is an infinitely better outcome than a
scheduler that stopped.

**The heat index is computed, not read off the API.** Open-Meteo returns
`apparent_temperature`, which folds in wind and radiation and is a wind-chill
figure in winter. The NWS heat index is temperature and humidity, and it is what
the rule turns on: **88 °F at 70 % humidity is a 100 °F heat index**, and a shift
documented against the other number would look compliant while somebody was being
cooked. Both inputs are stored beside the result, so a disputed index can be
recomputed from the observation.

**A crossing logs an event. It never files a heat record.** A reading at or above
threshold writes one `Threshold Crossed` compliance event — once per shift, not
once per reading, or a hot afternoon buries the water breaks under thirty-six
identical rows. It does **not** create a Heat Exposure Event. That record says
which crew was exposed, what water was provided, whether the rest cycle was
*taken*, whether anybody showed signs and what was done — five judgements by the
person who was standing there, under their signature. **The sweep surfaces the
condition; the foreman decides whether it is a record.**

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `fetch_weather_now` | Appends one reading to an **open** shift immediately, bypassing the cache — for the foreman who wants the conditions on the record now rather than in eleven minutes. | Touch a closed shift. A `current` reading filed against a crew who went home is true about the place and false about the shift. Nor create a heat record, however hot the reading is. |
| `backfill_weather_for_shift` | Reconstructs a **closed** shift's timeline from the archive API, at that API's own hourly granularity, filtered to the shift's own period. Idempotent by the minute, and it never edits a reading — so a shift also swept live keeps its live rows and gets the gaps filled. | Write a compliance event. A `Threshold Crossed` row dated last July on a signed shift would be an observation nobody made, sitting beside water breaks somebody did — the crossings are **counted and reported** instead. Nor backfill an open shift: the archive is a reanalysis of hours that have already happened. |
| `list_shifts_missing_weather` | The worklist: closed shifts carrying fewer than one reading per hour of their own length. Shifts with **no coordinates** are listed separately, because no amount of backfilling documents one. | Pretend the heuristic is a proof. A shift swept for two hours and then missed has readings and is still missing most of its timeline. |
| `get_weather_timeline` | One shift's conditions with the extremes and `first_crossing` — the instant -1131's obligations start running from — without returning the whole shift. Calls out a **mixed** timeline by name. | Let a reconstruction pass for something contemporaneous. Every row says where it came from. |
| `get_weather_settings` | The kill switch, the cadence, the three thresholds and the **per-company overrides** — because "the threshold is 80" is false on a site where one entity set 75. Works even when weather is switched off, because it is the tool somebody calls to find out why nothing is being fetched. | Write any of it. There is deliberately no `update_weather_settings`: those are three outbound URLs and three numbers deciding whether a hot afternoon is logged at all, and a tool that could raise the heat threshold past anything Oregon produces would leave a site that behaves normally and never says anything is wrong. The Desk form is the write surface. |

Three things now fill themselves in, and **manual entry always wins** in all
three. A compliance event's weather snapshot comes from the reading current at
its own instant — the last one at or before it, within half an hour, because
*earlier beats later*: that is the conditions the foreman was standing in when
they called the break. A Heat Exposure Event's `max_temp_f`, `max_heat_index_f`
and `threshold_crossed_at` compute off the shift's timeline, with
`threshold_crossed_at` being the **earliest** crossing rather than the hottest
moment. An on-site reading beats a modelled figure for a grid square measured in
kilometres, so a computed value fills a blank and never corrects an answer.


---

**Sustainable CF/Acre** — the v0.19.5 surface, and the first tools in this run
that no regulator asked for.

$$\text{Sustainable CF/Acre} = \frac{\text{Normalized OCF} - \text{Maintenance Capex}}{\text{Productive Acres}}$$

**Headline operating cash flow lies in two directions at once.** It is *flattered*
by money that came in and will not come in again — an insurance recovery, a
settlement, a gain on a tractor sale that landed in the operating section — and it
is flattered *again* by maintenance that was not done. A farm running its
irrigation to failure to make a year look good is destroying the thing the year
was earned with, and the headline number goes **up** while it happens.

**The output is itemized, and that is the release.** A single number here is
worthless, because the number's whole claim is that it has been adjusted — and an
adjusted number nobody can inspect is indistinguishable from an arranged one.
Every buyer, lender and auditor who reads it will test it one add-back at a time,
so `get_sustainable_cf_per_acre` returns each approved adjustment with its
justification and the name that signed it, each maintenance-capex asset with its
purchase date and portion, and each productive block with the days it was in
service. The figure is the **last** key in the payload, not the only one.

**AI proposes, human approves**, and they are two tools with two switches.
Finding a non-recurring item scattered through a ledger nobody reads line by line
is worth a great deal and is something a model is good at; deciding that a
hailstorm in a region that hails every third year is non-recurring is a judgement
somebody defends across a table. `create_normalization_adjustment` makes a
**Draft** and cannot be argued into making anything else.

**Maintenance capex is actual spend, never a percentage of revenue.** The common
shortcut destroys the only interesting signal — an operation that spent nothing on
replacement did not have a cheap year, it borrowed the year from the orchard — so
`Asset` gains `capex_type` (Maintenance / Growth / Mixed) and `create_asset`
requires it, at the moment the person raising the purchase knows why they are
buying the thing. Assets with no classification are **excluded and counted**, in
neither direction.

**The denominator is what is productive, not what is owned.** `Field` gains
`productive_from_date`, `productive_through_date` and `pre_yield_end_date`. Fallow
ground and pre-yield plantings are out and counted separately — pre-yield acres
are next year's denominator — and a block that came into bearing in February is
weighted for the part of the period it was actually earning. A block with **no**
productive-from date is excluded and named, never assumed: assuming it puts acres
in the denominator that may be a three-year-old planting, which makes the figure
*look* conservative while turning a data gap into a number somebody acts on.

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_normalization_adjustment` | Proposes one add-back or subtraction with the sentence saying why it will not recur. `amount` is always positive; the sign lives in `direction`. | Make it count. It creates a **Draft**, always. Nor accept a justification under forty characters — the floor is not a quality bar, it is a floor under "one-time". |
| `approve_normalization_adjustment` | The human half: status to Approved, signature attached, `approved_on` **written rather than taken as input**. | Approve unsigned — there is no such path. Nor approve a second adjustment for the same company, period and category: two approved rows are two answers to one question. A correction **supersedes**. |
| `reject_normalization_adjustment` | Refuses one on the record with the reason attached, and the rejection is **kept** — a refusal with a reason teaches the next proposal. | Reject something already approved. It has been counted, and rewriting a decision is not recording one. |
| `backfill_asset_capex_type` | Classifies unclassified Assets in bulk on one sentence of heuristic: everything bought before the operation started tracking is generally maintenance. **Dry-run by default**, idempotent. | Overwrite a classification somebody made, or take `Mixed` as a bulk default — a split is a judgement about one invoice, and applying one to a hundred assets would be inventing a hundred splits. |
| `list_normalization_adjustments` | The register, and `counted_in_the_kpi` says which rows actually move the number. `awaiting_a_decision` is the one worth reading at quarter end. | Hide what did not count. Drafts, rejections and superseded rows are all there. |
| `get_sustainable_cf_per_acre` | The figure with every ingredient itemized, plus `computation_warnings` — undated blocks, unclassified assets, a period with no approved adjustments at all. | Return zero where there are no productive acres. It returns **null**: a division nobody performed is not an answer, and a zero would be read as one. |

Raw OCF is computed from `GL Entry` **by the direct method** rather than read off
ERPNext's Cash Flow report — cash and bank movement per submitted voucher,
apportioned by the accounts on the other side, with a mixed voucher split
proportionally. A report's output cannot be traced back to rows, and the whole
argument here is that it has to be. Full notes:
**[docs/kpi_sustainable_cf_per_acre.md](docs/kpi_sustainable_cf_per_acre.md)**.

---

**The window standard** — v0.19.6, and the reason the figure above can be read
without knowing what season it is.

**Every financial report defaults to a trailing twelve months.** Not a feature on
one metric: the shape every financial figure in this app takes. Agricultural
revenue is aggressively seasonal, so Q3 is harvest and Q1 is pruning — set two
single periods against each other and the answer is that the operation collapsed
in January and recovered in September, **every year, on every farm**, whether or
not anything happened. A rolling twelve months contains the whole cycle exactly
once however it is read, so consecutive points differ only by the month that
entered and the month that left: the figure moves when the business moves.

**Three blocks, and each corrects the other two.** `point_in_time` is the period
just finished. `window` (also `ttm`) is the rolling figure, with the summed
adjustments, the aggregated maintenance capex and the time-weighted acres each
still inspectable. `historical_averages` is what that window has been worth for
this operation before — the only thing that says whether the current number is
good. A TTM figure means one thing above its five-year mean and the opposite
below it.

**The window ends at the last completed step**, never a part-finished one — three
days of August against twelve months of everything else is a figure that falls
every first of the month and recovers by the thirty-first. **Partial history is
labelled, never annualized.** **Quarterly and Yearly steps follow the company's
own fiscal year**, and the payload says which month it opens in.

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `get_windowed_report` | Any registered report over a window — `sustainable_cf_per_acre`, `ocf`, `revenue` — TTM by default, with its own history beside it. Registering a computer is the whole of adding a fourth. | Touch your ledger. It warms the `Financial KPI History` cache and writes nothing else: no Account, no GL Entry, no Asset, no Field, no adjustment. A test asserts exactly that. |
| `list_financial_kpi_history` | The cache as a plain series, for drawing or exporting a line. | Hide a gap. A missing window is reported as missing, because plotting it as continuous draws a trend that did not happen. |
| `recompute_kpi_history` | Rebuilds the cache for one KPI — the answer to a retroactively approved adjustment when the pack goes out this afternoon. `force=true` clears and rebuilds. | Change anything not derivable. Every row it writes is what the live computation would produce and every row it deletes comes back; the worst outcome of running it wrongly is time spent. |

The history is cached in `Financial KPI History` and filled by an overnight sweep
at 02:00 — a five-year Monthly history is sixty full computations over twelve
months of GL each. Approving a normalization adjustment for a period the cache
already covers **deletes** the snapshots whose window contained it: a stale
components list is worse than a missing one, because it is a set of ingredients
that does not produce the number printed above it. Full notes:
**[docs/reporting_ttm_standard.md](docs/reporting_ttm_standard.md)**.


## The Financial KPI Framework

**v0.19.6 made the window standard generalize across three *shipped* reports.
v0.39.0 makes the KPI itself a record.** Every operation has two or three ratios
that are genuinely its own — a cost per bin that only means anything against its
own bin count, a debt service coverage on the covenant's arithmetic rather than
anybody else's — and none of them belong in a shipped app. A `Financial KPI
Definition` is what an operation adds without a release, a deploy or an engineer.

**The engine does not move.** `formula_type` has exactly two values and both are
deterministic: `Built-in` delegates to a computer that ships with this app, and
`Expression` evaluates arithmetic over named inputs in a sandbox that parses to an
AST and refuses every node that is not arithmetic — no imports, no attribute
access, no subscripts, no comprehensions, no calls except `min`, `max`, `abs`,
`round`. **Nothing on the record holds Python**, which is the deliberate
difference from `Compliance Rule.custom_python`: a compliance rule can need to
express a shape no set of fields captures, and a financial KPI is a number divided
by another number.

| Tool | What it does | What it cannot do |
| --- | --- | --- |
| `create_financial_kpi_definition` | Define a KPI as a record: how it is computed, over what window, and what values are worth an alert. | Run code. The expression is checked against an arithmetic allowlist before it is stored, and refused at SAVE time rather than discovered as a quiet null at three in the morning. |
| `update_financial_kpi_definition` | Edit one in place, returning the diff with the previous values. | Rename a `kpi_id`. It is the cache key on every history row, and renaming orphans the whole series. |
| `list_financial_kpi_definitions` / `get_financial_kpi_definition` | The register, and one definition with its `problems` and its cached row count. | Report a broken definition as healthy. A Built-in naming a missing computer is named as such. |
| `compute_kpi` / `compute_all_kpis` | One KPI, or the whole dashboard, each with its history and its threshold verdict. | Touch your ledger. They warm the cache and write nothing else. |
| `refresh_kpi_cache` | Fill or, with `force`, rebuild the cached history. | Change anything not derivable, exactly as `recompute_kpi_history` cannot. |

**A definition holds the question and never an answer.** Every figure is computed
from the ledger when asked for and cached with the components that produced it, so
an Expression KPI reports not just that `current_assets` was 412,000 but how many
accounts it matched, how many GL entries, which way round they were read, and
whether it was a balance at the window's end or a movement across it — a
distinction a current ratio lives or dies on.

**Thresholds land on the one compliance calendar.** A KPI past its critical line
raises a `Compliance Alert` under a new `Finance` category, with the same
dismissal, snooze and auto-clear as an expiring certificate. Nobody closes it,
because there was never a task — there was a number. The scan **reads the cache
and never computes**, so an alert and a dashboard can never disagree about the
value. An omitted threshold is not a threshold of zero, and a KPI with no
thresholds reports `No thresholds` rather than `OK`. Full notes:
**[docs/financial_kpi_framework.md](docs/financial_kpi_framework.md)**.


## Compliance packets

A packet is an artefact rather than an answer: a structured JSON document for
somebody who has to sign something off. `generate_compliance_packet` returns one
inline — nothing is stored, emailed or filed.

```json
{"name": "generate_compliance_packet",
 "arguments": {"packet_type": "reconciliation_packet",
               "filters": {"account": "1100", "period_start": "2026-07-01",
                           "period_end": "2026-07-31"}}}
```

Three things make it more than a query:

- **It says how it was made.** `generated_at`, `generated_by`, `site`,
  `generator_version`, and `mcp_action_log_id` — the audit row for the very call
  that produced it. A figure can be traced back to a request, an IP and a user
  six months later.
- **It never truncates quietly.** Any collection that hits the cap raises a WARN
  naming how many rows were omitted. A packet that silently drops entries is a
  lie with provenance attached.
- **It reports what is wrong with itself.** `flags` carries INFO / WARN / ERROR
  findings, and `flag_summary.signable` is false whenever an ERROR is present.
  ERROR means the numbers do not internally agree — not merely that something is
  unusual.

| Packet type | Filters | What it contains |
| --- | --- | --- |
| `reconciliation_packet` | `account`, `period_start`, `period_end`, `company?` | Opening/closing balances, movement summary, every JE that touched the account, unposted drafts, cancelled entries, and a check that `opening + net == closing`. |
| `fiscal_year_audit_packet` | `company`, `fiscal_year` | Trial balance (each row stating its basis), income statement, balance sheet, top 20 entries, intercompany activity, document counts, and a check that `Assets - (Liabilities + Equity) = Income - Expense`. |

Each type has its own `allow_<packet_type>` switch, separate from the two tool
switches — so you can expose a reconciliation packet without exposing a payroll
one when that arrives.

**Adding a type is a single file drop** in `erpnext_mcp/packets/`. The package
auto-discovers every module that registers a `PacketSpec`; there is no list to
update and no handler to touch. Roadmap: payroll, organic-transition
attestations, tax-year summaries, SOX controls.

---

## Compliance is not a module

**v0.15.0.** Sprint 7 adds a compliance framework, and the first decision it made
was not to build one as a module.

> **Compliance is a lens on operational data, not a duplicate set of records.**

Every spray IS an EPA and Worker Protection Standard record. Every hire IS an
I-9 record. Every bucket IS an FSMA traceability record. The alternative — a
"Spray Compliance Log" somebody fills in *after* doing the spraying — drifts from
reality the first busy week of harvest, and an auditor who finds two records of
one spray that disagree has found something far worse than a missing field.

The test used throughout:

> Does removing a feature break **operations**, or only break **compliance
> reporting**? Breaks operations too → woven in correctly. Only breaks reporting
> → it is a shadow layer; refactor.

### The compliance columns go on the operational record

`install_compliance_fields` adds twenty-four fields — as `Custom Field` rows — to
Spray Log, Employee and the BucketLog bridge, and verifies (without touching) the
ones this app already ships on Housing Unit and Field. Seven are required.

This is **the one place erpnext_mcp extends a doctype it did not create**, and it
is deliberate. `docs/compliance_fields.md` has every field with the framework
that wants it and — the column that matters — what breaks in the day-to-day WORK
without it:

| Field | What breaks in the work |
| --- | --- |
| `rei_hours` | THE crew-scheduling number. Without it nobody knows when the block can be picked, and the crew boss guesses. |
| `epa_reg_number` | A load can be rejected at the packing house with no way to find out which block it came from. |
| `i9_status` | Whether this person may be put on a crew tomorrow. A rostering fact before it is a filing fact. |
| `picker_id` | Piecework pay. Every bucket is somebody's money. |
| `co_detector_last_test` | Somebody sleeps there tonight. |

A test requires that last sentence to exist for every field, so a shadow field
cannot be added without somebody confronting the question.

**What it costs, said plainly.** Uninstalling this app drops those columns and
everything typed into them. `before_uninstall` names every one before it happens,
with the `bench backup --only-doctype` lines to run first. That is a real cost
and it is the right trade: an app that refuses to touch anybody else's doctype
cannot make compliance fundamental to operations, only adjacent to them.

The switch is `allow_install_compliance_fields`, the only mutating switch that
ships ON — a compliance field that arrives when an operator remembers to tick a
box is missing on the sites that needed it most. Turn it off and nothing is added,
through the tool or through `after_migrate`.

### Four doctypes for evidence that comes from outside

Nobody writes a harvest hygiene SOP by harvesting. Four kinds of evidence arrive
from outside the operation and have no operational act to hang off:

| DocType | What it holds | The refusal that defines it |
| --- | --- | --- |
| **Compliance Policy** | SOPs, versioned, with the document attached | `supersede_compliance_policy` writes **both ends of the chain in one act** — "which procedure was in force" is asked from whichever end the auditor starts |
| **Certification** | GAP, GlobalGAP, organic, applicator and FLC licences | Editing the expiration forward is refused; that is a **renewal**, and a renewal records the lapse instead of hiding it |
| **Regulatory Filing** | What went to an agency, and what came back | A filing marked Submitted with **no submission date** is refused — the agency's position is that they have no record |
| **Audit Event** | Audits, findings, and every corrective action | `close_audit_event` **refuses while any action is open**, in the tool *and* in the controller |

### The Kairotic Compliance Calendar

**Chronos serves Kairos.** The clock runs a nightly sweep; the sweep decides
nothing. Nine rules ask whether a condition is true *right now*:

> "It is the first of the month, so remind somebody about water testing" — fires
> on fallow ground, on ground tested last week, and is ignored by March.
>
> "This block was sprayed eleven days ago and its agricultural water has not been
> tested in 118 days" — fires on exactly the blocks where FSMA Subpart E is
> engaged, on exactly the days it is engaged.

`water_test_stale` is the clearest case: the gate is **the spray, not the date.**
An untested block nobody is spraying is dormant rather than unsafe, and becomes
Critical the day it re-enters rotation.

**Alerts auto-dismiss when the condition resolves.** The water test was done, the
licence was renewed, the cabin was inspected — the alert goes away on the next
sweep without anybody switching it off. If the condition comes back, the same
alert reopens; a dismissal a *person* made never does, because they looked and
decided.

Three different ways off the calendar, kept distinct: auto-dismissal (the work
got done), **snooze** (a date — the condition is still true and it comes back on
its own), and **dismissal** (a person decided, and the reason is mandatory
because it is the only part of the record nobody can reconstruct).

`get_compliance_calendar` is read-only and on by default: a compliance calendar
behind a write switch is a calendar nobody consults.

### Audit packets, and the gate on producing one

`generate_audit_packet` assembles eight regimes — FSMA, GAP, GlobalGAP, OSHA,
DOL, EPA, USDA_NIFA and an unscoped Other — into a PDF, filed as a Governance
Document in the company's archive.

It pulls from the operational records, not from a copy. The spray records ARE the
spray logs; the worker facility records ARE the housing register. Nothing in a
packet can have drifted from what was actually done.

**The gate is a refusal, not a warning.** A packet asserts a compliant period, so
it is refused on a period that has not finished and on one whose corrective
actions are still open — naming every one. A warning at the top of a printed
document is not read by the person the document is handed to.
`allow_open_actions=true` produces it anyway with the open items in a section at
the **front**, which is the honest way to hand over an unfinished period.

**Empty sections say why they are empty.** A packet on a site with no BucketLog
bridge says the bridge is not installed and the traceability has to be supplied
separately — a silently omitted section reads as an operation with nothing to
declare.

### The Compliance Command Center

A Frappe Dashboard at `/app/compliance-command-center`: alert counts by severity,
overdue corrective actions, expiring certificates, open audits, and charts for
alerts by category, alerts raised over time, the certificate expiration timeline
and filings by agency.

Built by an idempotent installer, **not** shipped as `fixtures` — which
`test_hooks.py` forbids by name. A fixture cannot look at what is already there,
so an operator who reordered their cards or deleted a chart would get it silently
put back on every migrate.

`get_audit_readiness` computes the one number somebody acts on — resolved over
raised — and reports **how it was earned**. An operation at 95% entirely through
human dismissals is a different operation from one at 95% because the work got
done, and a score that could not tell them apart would be worth gaming.

---

## Farm Task Dispatch

v0.15.0 could tell an operation that fifty-four things were wrong. Nothing in it
could send anybody to fix one.

That is not a missing feature, it is a missing half. A compliance calendar whose
alerts have no actionable path is a list somebody reads on a Tuesday and
transcribes onto a whiteboard, and by August the whiteboard and the calendar
disagree. v0.16.0 closes the loop:

```
Compliance Alert  →  Farm Task        →  worker claims, starts, completes
   (a condition)      (with an evidence   (photographs, signature, findings)
                       contract)
                            ↓
                    Housing Inspection / Detector Test / Water Test
                            ↓
                    the unit's last_habitability_inspection moves forward
                            ↓
                    tonight's sweep finds the condition no longer true
                            ↓
                    the alert auto-dismisses. Nobody touched it.
```

**Nothing in this release dismisses an alert**, and that prohibition is the
design. The only honest way an alert goes away is to change the world and let the
sweep notice — anything else is a system where the calendar and the camp disagree
and the calendar is the one that looks clean.

### Evidence is required before the work is, not after

`evidence_required` is a mandatory field on `Farm Task`. A task cannot be created
without stating what closing it obliges somebody to produce:

```json
{"photos": true, "signature": true, "findings_text": true, "witness": false}
```

`complete_farm_task` then refuses a submission that does not meet it, naming each
requirement that is short. **There is no path to a task somebody can close by
saying they did it.** An empty contract is refused; so is one whose every
requirement is false; so is a misspelt key, because `{"photo": true}` asks for
nothing, refuses nothing, and looks exactly like a photograph requirement right
up until the audit.

One subtlety worth knowing before you meet it: a `findings_text` requirement is
satisfied by passing an **empty string**. A clean inspection is a positive
statement and that is how it is made; leaving the argument out entirely records
that nobody was asked.

### Dual mode, because one mode is wrong for half the work

| | For | Why |
| --- | --- | --- |
| **Self-pick** | habitability walks, detector tests, camp maintenance, water sampling | Fifty-four walks a foreman has to assign by hand are fifty-four walks that do not happen. |
| **Dispatched** | applicator-licensed spraying, electrical work, I-9 re-verification, licence renewals | Somebody is SENT, by name, and the record says who sent them. `claim_farm_task` refuses these outright: self-picking one puts the wrong person's name on a regulated record. |
| **Either** | most general work | Both doors open. |

**Three concurrent claims per worker.** A hoarding limit, not a productivity one:
completing or rejecting a task frees a slot in the same instant, so it never
stands between somebody and their next job — only between them and their fourth
simultaneous one. Without it, one worker empties the pool onto their own name and
the board looks worked.

### Rejection is an answer

"Nobody got to it and dispatch never followed up" is the sentence nobody can
defend in front of an auditor. `reject_farm_task` requires a reason, returns the
task to the pool, and **keeps the rejected assignment** — the proof that somebody
was sent, went, and could not do it. "The ladder is broken and I could not reach
the detector" is a fact somebody can act on; an absence is not.

### The board

`/app/farm-task/view/kanban/Farm Task Dispatch` — a Frappe **Kanban Board** with
one column per state, Rejected included. A foreman drags a card and Frappe writes
the field, on desktop and on a phone, with the site's own permissions and theme.
**There is no custom UI here and none is needed.** `list_dispatch_board` returns
the same columns as JSON for a caller that cannot see a screen.

The landing page at **`/app/farm-task-dispatch`** carries a quick-add shortcut,
the board, five Number Cards (in the pool, open Critical, awaiting review, and
raised-from-alerts against raised-by-hand), charts by type and urgency, and link
cards for the compliance records a completion writes.

The raised-from-alerts pair is two counts rather than one percentage on purpose:
a Number Card counts one collection and cannot divide two, and a card that
displayed a ratio would have to invent it.

Built by the same idempotent installer as the Command Center, for the same
reason: an existing board is left exactly as somebody has since arranged it. The
one exception, added in v0.16.1, is a workspace that exists and is **empty** —
that is what the bad release wrote, not an arrangement anybody chose, so it is
filled in.

### The records a completion produces

Three DocTypes, and one rule shared by all of them:

```
findings blank    →  Recorded
findings present  →  Corrective Action Required
```

**The state is computed from what was found, never chosen.** Somebody who has
typed "water stain, north wall, spreading" is not offered the option of marking
the walk as passed, because the state is recomputed from the text on every save.
`workflow_state` is the framework's own field name, so a site that wants Frappe's
native Workflow layered on top attaches one and `advance_workflow` drives it —
but the branch ships working, because a branch that needs configuring first is a
branch that is off on every site nobody configured.

Three judgements inside them that are easy to get backwards:

- **A failed detector test still writes the date.** The stale-detector alert asks
  whether anybody *knows* the detector works. A Fail answers it. The answer is
  bad, so the record raises a Critical alert of its own — but the ignorance is
  over, and leaving the date blank would have the calendar saying "nobody has
  tested this" about a building somebody tested this morning.
- **"Not Present" writes no date**, for the mirror reason, and is a finding in
  its own right.
- **An unreadable laboratory result is not a clean result.** Results are read by
  words first and numbers second, with generic E. coli compared against the FSMA
  112.44(b) criterion of 126 CFU/100 mL. Where neither reading works, somebody
  has to go and look at the report. Treating an uninterpretable result as a pass
  is how a compliance file becomes a clean record of nothing.

And a write-back rule that only ever moves forward: March's walk entered in July
is filed as evidence and does **not** drag a register that already knows about
June — that would re-raise an alert about work which has since been done.

---

## Multi-entity scoping and the six mobile roles

Several entities run on one site. An operating company that farms the ground, a
land-holding company that owns it, a family office, trusts. A field worker at
the operator must not see the holding company's parcels; an advisor to the
family office must not see the operator's task board.

**The role says what KIND of work somebody does. A Frappe User Permission on
Company says WHOSE.** That split is the whole design, and it is why not one
company name appears in any role definition in `erpnext_mcp/roles.py`.

Bolting entities into the roles would have produced "Field Worker — OpCo",
"Field Worker — Holdings", and a new role every time a family adds an LLC. It
would also have made this app specific to one install, which is the promise its
`hooks.py` opens with.

### The six

Installed idempotently on every `bench migrate`, by the same hook that builds
the Compliance Command Center. A role that exists is left alone; a permission an
operator has since edited is left alone too.

| Role | What it is for | What it cannot do |
| --- | --- | --- |
| **Field Worker** | The phone in the orchard. Reads the pool and the job, writes their own assignment and the evidence on it. No desk access. | Read the SOP library or the compliance calendar. Raise, assign or cancel work. **Rewrite the job** — Farm Task is read-only for this role; only the assignment is writable, because a worker moves their own record through its states and does not change the task's urgency or its evidence contract. |
| **Foreman** | The dispatch board for one operating company: raise work, send people to it, read the calendar that generated it. | Touch accounting — no accounting doctype is named in any role in this app. Edit the certificate or SOP registers. See the cap table or the governance archive. |
| **Compliance Officer** | The registers end to end: policies, certificates, filings, audits, the alert calendar. Can build the packet an audit asks for. | **Dispatch anybody.** Farm Task is read-only, deliberately: the person who decides a walk is required and the person who decides who walks it must not be one account, or that account could raise a task, assign it to itself and close it. |
| **Farm Manager** | Operations and the ground under them: dispatch, compliance, parcels, fields, zones, housing, leases — and **Section 2 of the I-9**, which is the employer's half of the form and needs an employer who can write it. | See the cap table or member events. Edit the governance archive. Touch notes payable or asset cost profiles. **Raise or destroy an I-9** — the form begins with the worker's Section 1 and its life is the retention schedule's. |
| **Family Member** | The holding-company view: cap table, member events, governance, related parties, land, debt. | See the operating company's task board — the day-to-day of whoever farms the ground is not the holding company's business. Edit the cap table or post a member event. |
| **Advisor** | The narrowest role in the app: governance documents, related parties and regulatory filings, read-only, for the one entity they advise. | **Write anything, anywhere.** See the cap table, the ledger, the task board or the calendar. |

The two headline separations are asserted in both directions in
`tests_standalone/test_roles.py`: a Field Worker cannot read a Compliance Policy
and a Compliance Officer can; a Compliance Officer cannot dispatch and a Foreman
can. Asserting only one half of a separation proves nothing — a role that could
read *nothing* would pass the first test.

### The role badge

`get_current_user_context` — the call the app makes at login and on every manual
refresh — carries a `role_indicator` block from v0.108.0, and so does every row
`search_employees` returns:

```json
"role_indicator": {
  "key": "foreman", "label": "Foreman", "short_label": "FRM", "precedence": 4,
  "has_login": true, "is_administrator": false, "can_dispatch": true
}
```

It exists so a client stops deriving the badge itself from the raw `roles` array
against a list of role names compiled into it. The precedence is stated in
`roles.ROLE_INDICATORS` and answers one question — *of everything this person
holds, which one word describes them on a screen this size?* — so a Foreman who
is also a Field Worker reads as a Foreman, and a System Manager on a handset
reads as an Administrator rather than as whatever farm role they also carry.

`has_login` is false for anyone with no `user_id`, which is most of a picking
crew: "nobody has given this person an account" and "this person's account holds
no role" send a foreman to two different places. And the badge is a **display
fact, not a boundary** — `guard.require_dispatch_role` still runs on every
dispatching call, and `can_dispatch` here is computed off the same frozenset it
refuses on, so a picker can grey a row out instead of letting somebody discover
the refusal after they have chosen.

### Creating an account

```
create_mobile_user(
  email          = "ana@constancyfarms.example",
  full_name      = "Ana Ramos",
  role           = "Field Worker",
  entity_access  = ["Constancy Farms LLC"],       # REQUIRED. At least one.
  preferred_company = "Constancy Farms LLC",      # which entity the app opens on
)
```

That writes the User, assigns `Field Worker` (plus the site's own `Employee`
role where it has one), writes one `User Permission` row per entity with
`apply_to_all_doctypes`, files a Mobile Access Grant, and returns the API
credential once.

**`entity_access` is mandatory and there is no override.** In Frappe, a user
with NO User Permission on Company sees **every** company on the site. So the
account you would get by leaving it out is the *least* scoped account here, not
the most — which in a release about scoping is the one mistake that has to be
impossible.

One `User Permission` row with `apply_to_all_doctypes` scopes every document
that links to a Company, for that user, across every doctype at once — including
doctypes this app has not written yet. That is why this uses Frappe's mechanism
rather than filtering inside each tool.

### The Custom DocPerm trap

Worth knowing about even if you never touch `roles.py`, because it is a live
site's permissions if it goes wrong.

Frappe resolves a role's permissions roughly like this:

```
perms                      = every DocPerm for the role
custom_perms               = every Custom DocPerm for the role
doctypes_with_custom_perms = distinct parent from `tabCustom DocPerm`
for p in perms:
    if p.parent not in doctypes_with_custom_perms:
        custom_perms.append(p)
```

Read the third line. **The moment ANY Custom DocPerm row exists for a doctype,
every standard DocPerm on that doctype is ignored — for every role on the site,
not just the one the row was written for.** One row granting Field Worker read
on `Employee` would silently revoke HR Manager, HR User and System Manager from
the Employee register, during `bench migrate`, with nothing printed.

The installer does two things about it, and both are tested:

1. **It mirrors the standard permissions into custom ones first**, per doctype,
   before the first new row lands — which is exactly what Frappe's own Role
   Permission Manager does under the name `setup_custom_perms`.
2. **It refuses outright to write a permission onto a doctype this app does not
   own.** Not because the write would fail — it would succeed, which is the
   problem. A refused target lands in the migration's printed output rather than
   disappearing.

That second rule has a consequence stated plainly rather than hidden: a Field
Worker who needs to read their own Employee record needs a role from the app
that owns `Employee`. `create_mobile_user` assigns the site's own `Employee`
role alongside, and tells you when the site has not got one.

### The credential, and what it does not promise

The mobile app carries Frappe's own API key/secret pair in the Keychain and
sends it as `Authorization: token <api_key>:<api_secret>` **alongside**
`X-MCP-Token`. The two headers do different jobs:

- `X-MCP-Token` is **entry**. It is the same shared bearer token everything else
  uses, and it is still gated by the CIDR allowlist.
- `Authorization: token …` is **identity**. Frappe authenticates it before this
  app's endpoint runs, so `list_my_tasks` and friends know which of forty
  workers is holding the phone. It buys no additional access.

**API secrets do not expire, and this app does not pretend otherwise.** Frappe
has no expiry on one, and this app installs no scheduled job to add one — such a
job would rewrite another app's User records on a timer with nobody watching,
and `hooks.py` declares exactly five scheduled jobs and argues for every one.
`token_expires_on` is a **review date**: `list_mobile_users` flags an overdue
grant loudly, `get_current_user_context` reports it to the phone, and
`revoke_api_token` is what actually ends access. Calling a reminder an expiry
would be a false assurance about a credential, which is worse than none.

### Enrolment by QR

`generate_mobile_login_qr` draws a card carrying `{url, user, token,
expires_at}`. The app scans it, stores the credential, and every call after that
carries the header. The alternative is somebody typing a 15-character secret
into a phone keyboard in a farm office, which is how the secret ends up on a
whiteboard.

**The image is a live credential.** Anybody who photographs it over somebody's
shoulder has that account until the token is revoked. That is inherent to
enrolment by QR; the mitigations are all time-shaped:

- it stops being valid to enrol with after 24 hours by default (1–168 allowed);
- `rotate_token` defaults to **true**, so re-minting a card invalidates every
  older copy of it — and every phone already enrolled on that account, which
  must re-scan;
- it refuses a non-HTTPS endpoint outright;
- `archive=true` files it as a **private** attachment on a Governance Document
  for offline distribution, and the result tells you to delete that document
  once the phone is enrolled. The durable record is the Mobile Access Grant,
  which holds no secret.

### Mobile Access Grant

One row per person, named by their email, tracked for changes. Frappe already
knows who has a login, which roles they hold and which companies they may see.
It knows none of the things an audit asks: who decided this person should have a
phone, what it was for, when the credential was issued, and — the one that
matters most — **why it was taken away**.

`before_uninstall` names it among the records that go, and separately warns
about what uninstalling does **not** remove: the six roles are rows in Frappe's
own `Role` table, the User Permissions are rows in `User Permission`, and the
API credentials are on the User records. Removing this app takes the MCP
endpoint away from those accounts and leaves everything else. To actually end
mobile access, run `revoke_mobile_user` for each account first.

---

## Exposing ERPNext MCP publicly via Tailscale Funnel

Everything up to here assumed the endpoint is on your LAN. A phone in an orchard
is not on your LAN. [Tailscale Funnel](https://tailscale.com/kb/1223/funnel)
publishes one port of one tailnet node to the public internet at
`https://<host>.<tailnet>.ts.net`, with TLS terminated by a certificate
Tailscale obtains itself.

> **READ THIS BEFORE YOU RUN ANY OF IT.**
>
> **Everything the API exposes becomes PUBLIC.** The hostname is in Tailscale's
> public DNS and appears in certificate transparency logs, so it is
> **discoverable** — not secret, not obscure, findable by anybody who looks.
> **The auth token is the only thing between the internet and your ledger.**
>
> Three settings that were belt-and-braces on a LAN become load-bearing:
> `auth_token` (now the whole boundary — long, unique, rotated when a phone is
> lost), `allowed_cidrs` (see step 4, and getting it wrong either locks out the
> app or opens the gate), and `enabled` (the one tick that takes the endpoint off
> the internet with no restart and no deploy).
>
> **Nothing in this app can turn Funnel on or off, and nothing will.** Changing
> what is reachable from the entire internet is an operator decision made
> deliberately — and `tailscale funnel` needs a local socket and privileges a
> containerised Frappe worker does not have, so a tool that shipped would fail
> on the deployment it was written for while looking like it might work.

### 1. Enable Funnel on the tailnet

Funnel is off for a tailnet until an admin allows it. In the Tailscale admin
console, under **Access controls**, the policy needs a `nodeAttrs` entry granting
`funnel` to the node (or to a tag it carries):

```jsonc
"nodeAttrs": [
  { "target": ["tag:server"], "attr": ["funnel"] },
]
```

### 2. Point Funnel at the port nginx already serves

On the **host** — not inside the Frappe container:

```bash
# What is serving now, and on which ports.
tailscale serve status
tailscale funnel status

# Publish 443 to the internet, proxying to the port your bench's nginx listens on.
# 8080 here; use whatever your deployment actually binds.
sudo tailscale funnel --bg --https=443 http://127.0.0.1:8080
```

Funnel forwards to a port that is **already working**. If
`curl -H 'X-MCP-Token: …' http://127.0.0.1:8080/api/method/erpnext_mcp.mcp.handle`
does not answer locally, Funnel will not make it answer publicly.

Your public name comes back from:

```bash
tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))'
```

### 3. Let nginx answer for the Funnel hostname

Frappe routes by `Host`, so a request arriving as `umbrel.tail1234.ts.net`
reaches a site of that name — which does not exist. Two ways out; pick one.

**Either** add the Funnel hostname to the site's `host_name`, so Frappe knows the
site by it:

```bash
bench --site <site> set-config host_name https://umbrel.tail1234.ts.net
```

**Or** make nginx serve your site for that hostname, by adding it to the
`server_name` line of the site's block in
`sites/../config/nginx.conf` and reloading:

```nginx
server_name  yoursite.local  umbrel.tail1234.ts.net;
```

Either way, set **`public_url`** on **ERPNext MCP Settings** to
`https://umbrel.tail1234.ts.net`. That is what `generate_mobile_login_qr` puts on
the card, and it is the only URL `validate_public_endpoint` will send your token
to.

### 4. Fix the allowlist, which is the step people get wrong

A Funnel request does not arrive from the phone's IP. Tailscale forwards it to
your local port, so `remote_addr` is loopback and the `X-Forwarded-For` chain is
whatever nginx appended. This app gates on the **rightmost** hop (see
`docs/security.md` on why), which under Funnel is normally `127.0.0.1`.

So the loopback entries in the default `allowed_cidrs` are what let the mobile
app through — leave them in, and **do not** be tempted to add `0.0.0.0/0`
because something did not work. Confirm what the endpoint actually sees by
making one call and reading the `caller_ip` column of **MCP Action Log**; that
column and the gate decision can never disagree, which is what makes it worth
reading.

### 5. Confirm it, from outside, with the tool

```
validate_public_endpoint()
```

It opens a TLS connection to the public name, reads the certificate, and POSTs a
real MCP `tools/list` to
`https://<host>.<tailnet>.ts.net/api/method/erpnext_mcp.mcp.handle`.

**A 401 is the best possible result.** It proves three things at once: the path
is reachable, the certificate is valid, and the token gate is holding. The probe
is unauthenticated by default precisely so that answer is available.

To prove the whole round trip, `authenticate=true` sends this site's own token —
and refuses any URL that is not your configured `public_url`, because a tool that
will POST your bearer token to a hostname in its arguments is a tool that
exfiltrates it. For the same reason the probe only ever reaches `public_url` or a
host under `.ts.net`, over HTTPS, base URL only, and does not follow redirects.

A 200 with tools advertised means it is working — and means everything your
enabled tools expose is now reachable from the internet by anybody holding the
token. That is the moment to re-read which mutating tools you have ticked.

`get_tailscale_funnel_config` asks the same question from the inside. On an
Umbrel it will usually report that it can see neither the `tailscale` binary nor
the host's socket, because Frappe is in a container and Tailscale is not. **That
is the expected state and not a fault** — Funnel needs no cooperation from the
Frappe process at all — and the tool says so rather than reporting a problem you
would go and try to fix.

### 6. The mobile app uses a DIFFERENT service, with different gates (v0.18.0)

Everything above describes one endpoint,
`/api/method/erpnext_mcp.mcp.handle`, which is what an AI client calls. **The
Farm Ops iOS app does not use it, and since v0.18.0 it does not use Frappe's
request handler at all.** It calls a closed list of routes on a separate process
— eleven at v0.18.0, forty-one as of v0.48.3, sixty-six as of v0.62.0, and
`routes.py` is the list:

```
POST /farmops/api/mobile/<one of sixty-four>
POST /farmops/api/files/<one of two>
X-FarmOps-Token: <api_key>:<api_secret>
```

served by `erpnext_mcp/farmops_api/` — a Werkzeug WSGI service on port 5250
inside the same container, published to the host's loopback only.

**NOTHING IN THE APP MAY POST TO `/api/method/…`, INCLUDING FRAPPE'S OWN
ENDPOINTS.** v0.48.3 is what that rule costs when it is broken: the onboarding
wizard uploaded its I-9 and W-4 photographs to Frappe's `/api/method/upload_file`
because there was no route to attach a staged file with. `fallback_auth`'s path
check covers `/api/method/erpnext_mcp.api.` and nothing else, so the token was
never read there, the request arrived as Guest, and Frappe answered 200 with the
login page — the exact failure the paragraph below describes, reproduced on the
one call that had been left behind. Every photograph and signature was lost
silently for as long as the wizard existed. `attach_onboarding_document` is the
route that was missing; see `RELEASES/v0.48.3.md`.

**Why it is not just another Frappe endpoint.** v0.17.1 put these eleven methods
at `/api/method/erpnext_mcp.api.mobile.*` and every call from a phone came back
as an HTTP 200 carrying the Desk's HTML login page. v0.17.2 established that the
Tailscale `serve`/`funnel` proxy removes `Authorization` — proven from inside the
tailnet as well as from the public funnel — and carried the credential four more
ways, resolving all of them in an `auth_hooks` entry so identity was settled in
the same window Frappe settles it. **Every one of the five still returned the
login page through the funnel, and every one of them worked against `localhost`
inside the container.** Five independent carriers do not fail by coincidence; the
remaining common factor was `/api/method/*` itself. Bank Bridge, a plain WSGI
service, works perfectly through the same funnel. So the methods moved to that
shape.

**The gates did not move, because it is the same code.** `farmops_api/routes.py`
is eleven entries and each names the same `@guard.endpoint`-wrapped function the
whitelisted path calls, so all seven checks below run here as they run there. The
test suite asserts the two transports' responses are byte-identical for the same
input, which is what makes that a checkable property rather than a claim.

**The old path is still live** at `/api/method/erpnext_mcp.api.mobile.*`, with
v0.17.2's three credential carriers intact. It works on the LAN and from inside
the container, and it is the fallback if the sidecar is ever the thing that is
down.

**Step 4 above does not apply to them and cannot.** A whitelisted method reached
directly never runs `security.authorize()`, so these paths have no
`X-MCP-Token`, no CIDR allowlist and no per-tool switch — a phone on LTE has
none of the three to offer. They carry their own seven gates instead, in
`erpnext_mcp/api/guard.py`: a global kill switch, a role gate, an **Active
Mobile Access Grant**, per-user rate limits, entity scoping on every argument and
every returned row, an audit row for every call including refused ones, and
secret stripping on the way out. `docs/security.md` §*The second transport*
covers each in full, and it is the section to read before you widen anything.

Two consequences for this chapter:

- **A whole-port Funnel does NOT publish them any more, and this changed in
  v0.18.0.** A blanket `/` handler forwards to ERPNext's nginx; farmops-api is a
  different port. Whichever shape your Funnel is, the eleven `/farmops/api/…`
  paths have to be mounted explicitly — the exact commands are in
  [`RELEASES/v0.18.0.md`](RELEASES/v0.18.0.md). Never funnel `/api/method/` as a
  whole prefix to save typing; that publishes every whitelisted method of every
  installed app.
- **An enrolled account holding no Company User Permission is REFUSED here**,
  not shown everything. That inverts Frappe's own rule on purpose; on a path
  reachable from the internet, the framework's default is exactly backwards.

To stop just the phones, leaving the AI endpoint up:

```bash
bench --site <site> set-config farm_ops_mobile_enabled 0
```

or untick **Farm Ops Mobile API Enabled** on ERPNext MCP Settings. Every phone
gets a 503 — never a 401, which the app reads as "credential dead, sign out" and
which would destroy every completion queued on every device.

### Turning it off

```bash
sudo tailscale funnel --https=443 off
```

Or, faster and without a shell on the host: untick **enabled** on ERPNext MCP
Settings. The endpoint then returns 404 to everybody, indistinguishable from an
app that was never installed, and takes effect on the very next request.

**That switch does not stop the mobile API** — see step 6. There are two
surfaces and two switches, because stopping the AI and stopping forty phones are
different decisions, and one control for both would guarantee that doing either
did both.

---

## Before you enable `advance_workflow`

It is the one write tool whose blast radius is decided by your site rather than
by the tool. A transition into a state with `doc_status: 1` **submits the
document** — on a Journal Entry that writes GL Entries and moves balances.

Run `list_workflows` first and check which target states carry `doc_status: 1`.
Then use the dry run:

```json
{"name": "advance_workflow",
 "arguments": {"doctype": "Purchase Order", "name": "PUR-ORD-2026-00184",
               "action": "Approve", "dry_run": true}}
```

```json
{"dry_run": true, "executed": false, "would_succeed": true,
 "current_state": "Pending Approval", "would_move_to": "Approved",
 "would_set_docstatus": 1, "would_submit": true,
 "effects": ["workflow_state: 'Pending Approval' → 'Approved'",
             "SUBMITS the document (target state has doc_status 1) — for a Journal Entry this writes GL Entries and moves balances"],
 "next_step": "Call again with dry_run=false to execute."}
```

The intended pattern is dry-run, show the human, execute. A dry run never raises
for an unavailable action — it reports `would_succeed: false` with the reason,
because "it would be refused" is the answer to the question.

One limit worth knowing: a dry run resolves the *transition*. It does not run the
document's own validation, so it cannot predict a submit that fails on a
mandatory field or a doctype hook.

---

## Security posture

Full threat model: **[docs/security.md](docs/security.md)**. The short version:

- **Three gates, all mandatory.** Master switch (off ⇒ 404), token
  (constant-time compare; missing or wrong ⇒ 401), CIDR allowlist (outside ⇒
  403). An empty allowlist denies everyone rather than allowing everyone.
- **Rejections are opaque.** Every refusal says the same thing. Which gate failed
  goes to the audit log, not to the caller.
- **The token is a Password field.** Encrypted at rest, never logged, never
  returned by any tool, shown to the operator exactly once.
- **LAN-facing by default, and it takes a deliberate act to change that.** The
  shipped allowlist is loopback plus the three RFC1918 blocks. **If you publish
  the endpoint** — see [Exposing ERPNext MCP publicly via Tailscale
  Funnel](#exposing-erpnext-mcp-publicly-via-tailscale-funnel) — everything the
  enabled tools expose becomes public and discoverable, and the auth token
  becomes the whole boundary. `validate_public_endpoint` will tell you, from
  outside, exactly what is answering.
- **Ledger reads are authorized by the token, not by roles.** The accounting read
  tools use `frappe.db.get_all`, which does not consult Frappe permissions. Three
  categories are different and *do* enforce them: **reports** (they run through
  the Desk APIs), **attachments** and **comments** (they check `read` on the
  parent document). Mutations always run the acting user's permission checks.
  The reasoning is in docs/security.md — it is a real trade-off, not an
  oversight.
- **Per-user identity, from v0.17.0, and it is identity and NOT entry.** A
  mobile client sends `Authorization: token <api_key>:<api_secret>` alongside
  `X-MCP-Token`; Frappe authenticates it before this app's endpoint runs, and
  `security.capture_calling_user` saves who it was in the one-line window before
  the MCP System User is assumed. That is what lets `list_my_tasks` answer for
  the right worker. It grants no access the shared token did not already grant,
  and a request that authenticated as one person and asks to act as another is
  refused.
- **To stop everything right now:** untick **Enabled** and save. Next request is
  a 404. No restart, no token rotation. This is also how you take a public
  endpoint off the internet without a shell on the host.

### Attribution

With **Attribute Actions To An MCP System User** on (the default), set an **MCP
System User** and every document a tool creates carries that `owner`, and every
permission-checked tool is bounded by that user's roles. Create a dedicated user
— `mcp@yourcompany.example` — with only what the tools you enabled need.
**Accounts User** covers the ledger tools; reports, attachments and workflow
actions need whatever their own doctypes require.

Leave it blank and mutations run as `Administrator`, which works and makes
MCP-authored documents indistinguishable from an admin's own.

---

## Audit log

Every call gets a row in **MCP Action Log** (`/app/mcp-action-log`) — reads
included, refusals included. The interesting question after the fact is rarely
"what did it change", it is "what did it see".

| Field | Example |
| --- | --- |
| `timestamp` | `2026-07-25 14:31:07.482119` |
| `tool_name` | `advance_workflow` |
| `caller_ip` | `10.0.4.22` |
| `arguments_json` | `{"action": "Approve", "doctype": "Purchase Order", "name": "PUR-ORD-2026-00184"}` |
| `result_status` | `Success` \| `Error` \| `Blocked` \| `Unauthorized` |
| `result_summary` | `Approve on Purchase Order PUR-ORD-2026-00184: 'Pending Approval' → 'Approved' as mcp@example.com` |
| `docstatus_delta` | `0 → 1 (submitted)` |

Rows are **append-only**: the doctype grants System Manager read and delete but
not write, and the controller refuses an update even from a script. Delete is
allowed so a busy site can be pruned — Frappe records every deletion in its own
Deleted Document doctype.

A row written by a *failed* mutation is committed on its own, after the failed
work is rolled back, so the record of the attempt survives even though the
attempt did not.

**Uninstalling drops this table.** Export it first if you need it; the
`before_uninstall` hook will remind you.

---

## Compatibility

| | Supported | Notes |
| --- | --- | --- |
| **Frappe** | v14, **v15**, v16 (`develop`) | Developed and run in production against **v15.115.0**. v15.100+ is the tested floor. The in-bench workflow suite builds a real Workflow on a synthetic DocType, so it verifies the framework contract on whichever version you run it against. |
| **ERPNext** | v14, v15, v16 | Required — `hooks.py` declares it, so `install-app` refuses on a Frappe-only site. |
| **Frappe HR (`hrms`)** | optional | Present → the three HR tools appear. Absent → they are not advertised at all. |
| **LibreOffice headless** | optional | Present → `regenerate_governance_document_pdf` can convert a `.docx`. Absent → it refuses cleanly and names the package. Nothing is installed at runtime. |
| **`segno` or `qrcode`** | optional | Either one → `generate_mobile_login_qr` appears. Neither → that one tool is not advertised, and everything else in the mobile login flow still works: `generate_api_token` returns the same credential as text. `segno` is the declared dependency because it is pure Python; `qrcode` is accepted where a bench already has it. |
| **`tailscale` CLI** | optional | Present → `get_tailscale_funnel_config` reads the live serve config. Absent → it says which of two situations it is in and points at `validate_public_endpoint`, which asks from outside and needs none of it. A container with no Tailscale is the expected state, not a fault. |
| **Python** | 3.10+ | CI runs 3.10 and 3.11. |
| **Database** | MariaDB, Postgres | No raw SQL anywhere, so whatever your bench runs. |

This app asks the site what it has rather than pinning a version — see
`erpnext_mcp/compat.py`. Concretely:

- **Bank Transaction amounts** — `deposit`/`withdrawal` on newer ERPNext, a
  single signed `amount` on older. Tools report one normalised `amount_signed`
  and say which layout they found.
- **Unallocated amounts** — the column where it exists, computed
  `gross - allocated` where it does not, and the response says which.
- **Company default cost center** — `cost_center` or `default_cost_center`, or
  `null` on a Company created before its chart of accounts.
- **Bank Statement** — absent on older ERPNext; the tool is not advertised there.
- **Client Script** — was `Custom Script` before v13; both are handled.
- **ToDo assignee** — `allocated_to`, or `owner` on versions before it existed.
- **Leave balance API** — `hrms.hr...` since v14, `erpnext.hr...` before the
  split.
- **Fields that do not exist are never selected.** Selecting a missing column is
  a hard SQL error, which is exactly the failure mode a reusable app must not
  have.

---

## Seeding the related-party register

`Related Party` is the one doctype in this app whose useful content is a list of
people's names, so nothing is seeded for you and no names ship in this
repository. `scripts/seed_related_parties.py` reads a JSON file you keep outside
it:

```bash
python3 scripts/seed_related_parties.py --input parties.json --site yoursite.localhost
# read the plan, then:
python3 scripts/seed_related_parties.py --input parties.json --site yoursite.localhost --apply
```

It runs **outside** `bench execute`, so it configures Frappe itself: it finds the
bench's `sites` directory by looking for `common_site_config.json`, takes the
site from `--site` or `currentsite.txt`, and **creates the log directories before
`frappe.connect()`** rather than assuming they exist. Without `--apply` it writes
nothing. The whole plan is validated before the first insert — including the
four-digits-never-nine rule, refused before Frappe is even started — so a plan of
forty records is refused whole rather than half-applied.

Each record takes the same fields `create_related_party` does:

```json
[
  {"party_name": "A Person", "party_type": "Individual",
   "company": "Your Company", "relationship_to_company": "Manager",
   "effective_date": "2020-06-15", "tax_id_type": "SSN", "tax_id_last4": "6789"}
]
```

### Into a container

Both the script and the plan have to be copied in, in this order:

```bash
docker cp parties.json <container>:/home/frappe/frappe-bench/parties.json
docker cp scripts/seed_related_parties.py <container>:/home/frappe/frappe-bench/

docker exec -w /home/frappe/frappe-bench <container> \
    python3 seed_related_parties.py --input parties.json --site <site>

# read the plan, then re-run with --apply
docker exec -w /home/frappe/frappe-bench <container> \
    python3 seed_related_parties.py --input parties.json --site <site> --apply

# and take the names back out again
docker exec <container> rm -f /home/frappe/frappe-bench/parties.json
```

---

## Tests

```bash
# Logic: refusals, arithmetic, switch handling, protocol shape.
# No bench, no database, no third-party packages. ~0.3s.
python3 -m unittest discover -s tests_standalone -t .

# Framework contract: migration, real encryption, real ERPNext validation,
# real permission enforcement, the real workflow and report APIs.
bench --site yoursite.localhost run-tests --app erpnext_mcp
```

3104 standalone tests and 284 in-bench tests. The standalone suite installs an
in-memory `frappe` double so the refusal tests get run every time rather than
only when a bench is handy; the in-bench suite covers what only a real site can
prove and skips rather than fails when the site lacks the setup a case needs.
Details in **[docs/development.md](docs/development.md)**.

The document writers are tested against their own bytes rather than against a
mock of a library: the PDF tests open the file and check that every offset in the
cross-reference table points at the object it claims, which is the one failure
that would otherwise produce a report that is generated, attached, archived and
unopenable. The v0.17.0 login QR is held to the same standard — its PNG is
decoded back to a module matrix and compared with an independent encoding of the
payload the tool says it wrote, so "the card carries this JSON" is a checked
claim rather than a hopeful one.

The double reproduces Frappe's own `Authorization: token <key>:<secret>`
validation, which is what makes the credential round trip real: generate a
token, make a request the server identifies as that person, revoke it, make the
same request and watch it stop being them. A fixture that simply asserted who
the caller was would have passed for the wrong reason.

---

## Roadmap

Candidates for the next release, roughly in order of how often they come up:

- **More chart templates.** `us_c_corp`, `us_s_corp` and `us_partnership`, each
  a file drop against the framework `us_llc_farm` established. They differ from
  it almost entirely in the 3000s — which is exactly why entity type is in the
  template key rather than a flag on one chart.
- **Chart diffing.** "Show me how this company's chart differs from the
  template" is the question `propose_clean_chart` almost answers; the missing
  half is matching existing accounts to template ones by name and reporting the
  moves and renames that would reconcile them.

- **Auth.** Per-token scopes, so one client can be given the reconciliation
  tools and another the reporting tools without two sites. Token expiry and
  rotation without a manual button press. Optional mTLS for the LAN case.
- **More workflow depth.** Bulk actions over a filtered set, and workflow history
  (who moved what, when) from the Version table. The dry-run preview landed in
  v0.3.0.
- **More packet types.** `payroll_packet` (needs Frappe HR),
  `organic_transition_packet`, `tax_year_packet`, `sox_control_packet` — each a
  single file drop against the existing framework.
- **Packets that reach outside ERPNext.** `external_sources` is already in the
  payload shape, empty; Bank Bridge variance and its anchor chain go there.
- **Packet persistence.** v0.3.0 returns packets inline only. Storing one as a
  File against a document, with a hash, is what makes it evidence rather than a
  transcript.
- **Custom report authoring.** A mutating, default-off tool to create a Query
  Report from SQL an operator reviews first — the "I ask for a figure often
  enough that it should be a report" case.
- **Write coverage for the categories that only read today.** Nothing in HR or
  sales writes, on purpose; the useful additions are probably `create_todo`'s
  siblings (assign, close) rather than posting invoices.
- **Prepared-report support** in `run_report`, for reports too slow to run
  inline.
- **A Workspace for the whole app.** v0.16.0 builds one for Farm Task Dispatch;
  the rest of this app's doctypes still appear only via search.

- **Deeper 1099 filing.** State 1099 filings and electronic filing to the IRS.
  What ships today stops at a pre-fill on purpose — the taxpayer id has to come
  off a signed W-9, and Copy A has to be the official scannable form.
- **Satellite imagery, on state rather than on a schedule.** The schema is here:
  provider, asset reference, last pull date, NDVI mean and standard deviation.
  The pull itself is not, and when it lands it should fire when a boundary
  exists AND the last pull is stale AND the block is in an active crop cycle —
  not on a calendar tick, which would burn imagery credits on a fallow block in
  January.
- **Geofence enforcement.** `find_fields_containing_point` answers the question;
  nothing yet *acts* on the answer. Wiring it into a bucket log ("is this pick
  inside an assigned block?") or a time clock ("is this worker on ground they
  are rostered to?") is the other half.
- **Parcel boundaries.** Fields and zones have polygons; parcels do not, so
  nothing checks that a block sits inside the ground it is recorded against.
  `set_field_boundary` says so in a warning rather than leaving it a silent gap.
- **The Farm App sync engine.** v0.12.0 ships `import_farm_app_fields`, which
  aligns the schemas by carrying each legacy record's id onto the ERPNext Field
  it creates. The engine that keeps the two in step afterwards — reading
  structure out, pushing per-zone cost and revenue events back — is the next
  half, and it needs the ids this release establishes.
- **Mobile capture for dispatch — the iOS half.** As of v0.17.0 the server side
  is complete: six roles, per-entity scoping, an API credential, QR enrolment, a
  public HTTPS transport, and seven tools shaped for a screen rather than for a
  report. What is left is the app itself — a camera roll, a signature pad and an
  offline queue, uploading through `stage_file_chunk`. Every call it needs
  already exists.
- **A skill register.** `list_available_for_me` is honest about not having one:
  nothing on a Frappe site records what skills a worker HAS, so the pool comes
  back unfiltered and says so. A register somebody actually populates — probably
  a child table on Employee owned by an HR app, not by this one — is what turns
  `skill_required` from a filter into routing.
- **OAuth / Sign in with Apple** for the mobile app, replacing the API token +
  QR pair. Deferred deliberately: token and QR is enough for a pilot, and an
  OAuth flow needs a redirect URI the phone can reach, which is a decision to
  make after the Funnel address settles.
- **Expiry that is an expiry.** Frappe API secrets do not expire and this app
  will not pretend they do with a scheduled job that rewrites User records at
  three in the morning. Doing it honestly means a token store this app owns and
  an auth hook it currently refuses to install — a real design, not a to-do.
- **Skill and location routing.** `skill_required` and `location` are recorded
  and filtered on; nothing yet *suggests* the nearest available worker who holds
  the right skill. That is the useful next step, and it should fire on state —
  somebody is on that parcel now — rather than on a roster.
- **Access control for the camp.** `Housing Unit.access_card_zone` is recorded
  now and read by nothing. Wiring it to a real card system is what turns the
  register into a door that opens.
- **Real estate for the operating company.** The land register is per-entity by
  construction; standing up a second company's parcels is data entry, not code —
  and since v0.12.0, `create_company` can stand up the company too.

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Uninstall

```bash
bench --site yoursite.localhost uninstall-app erpnext_mcp
```

Drops this app's own doctypes and everything in them: **ERPNext MCP Settings**,
**MCP Action Log** (the audit history), **Cap Table Entry**, **Member Event**,
**Governance Document**, **Asset Cost Profile**, **Note Payable**, **Parcel**,
**Lease**, **Related Party**, **Family**, **Field**, **Irrigation Zone**,
**Housing Unit**, **Housing Assignment**, **Compliance Policy**,
**Certification**, **Regulatory Filing**, **Audit Event**, **Farm Task**, **Farm
Task Assignment**, **Housing Inspection**, **Detector Test** and **Water Test**.
Four of those are worth a pause. A camp roster is the only record of who slept
where, and it is what an IRS Section 119 exclusion and an ORS 653 wage claim are
both answered with. An **Audit Event** holds whether each corrective action was
ever closed — an open finding nobody can produce a closure for is how last year's
finding becomes this year's penalty. A **Housing Inspection** and a **Water
Test** are the only records that anybody ever went and looked. And a **Farm Task
Assignment** holds, for every job somebody could *not* do, the reason they gave —
which exists nowhere else on the site and is the answer to "why was this never
done".

**It also drops the v0.15.0 compliance COLUMNS from doctypes belonging to other
apps** — the applicator names, EPA registration numbers, REIs, PHIs, I-9
statuses and traceability links on Spray Log, Employee and the BucketLog bridge.
The records themselves survive; what was typed into those columns does not.
`before_uninstall` names every one, with the export commands, before it happens.

Nothing else on the site is touched — the
accounts, cost centers, dimensions, journal entries, bank accounts and Assets
these tools created are ordinary ERPNext records and stay exactly as they are.

`before_uninstall` prints a row count and an export command for each doctype
that has anything in it, because most of them are the **only** copy: the cap
table is the only mapping from a member id to a legal name, the event trail the
only record of why an equity entry exists, a governance document may hold the
only digital copy of an agreement, a note payable is the only place a debt's
terms and provenance are written down, the parcel register holds appraised
values and the dates they were appraised as of, a lease holds rent terms that
exist in no other digital form, and the related-party register is the source for
a related-party disclosure on a return. The ledger has the balances and nothing
else. Export before you uninstall.

---

## License

MIT. See [LICENSE](LICENSE).
