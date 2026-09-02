# Security

What this app assumes, what it defends, and what it does not.

## The one-line version

If you need to stop everything right now: open **ERPNext MCP Settings**, untick
**Enabled** AND **Farm Ops Mobile API Enabled**, save. The next request is a 404
and a 503 respectively. No restart, no token rotation, no client reconfiguration.

**There are two switches because there are two surfaces**, and since v0.17.1 that
is the first thing to know about this document: `Enabled` stops the MCP endpoint
the AI uses, and `Farm Ops Mobile API Enabled` stops the eleven whitelisted
methods the phones use. Neither stops the other. Everything from "Threat model"
down to "Per-tool switches" describes the first surface only — see
[The second transport](#the-second-transport-the-farm-ops-mobile-api-v0171).

---

## Threat model

**What is being protected.** Read access to a general ledger — and, as of v0.2.0,
to a site's saved reports, its document attachments, its comment threads, its
purchase-approval queue and (where Frappe HR is installed) its employee,
attendance and leave records. Where an operator has enabled it, also the ability
to create, post and cancel Journal Entries, take workflow actions, and assign
work.

Read access alone is serious, and got more so in v0.2.0. A chart of accounts plus
balances is a fair description of a business's finances; add the attachments and
you have whatever anybody uploaded against those documents, and add the HR tools
and you have personal data about named people.

**Who this defends against.**

- Anyone on the network who has not been given the auth token.
- Anyone with the token who is calling from outside the configured CIDR range.
- A token holder attempting an operation the operator has not enabled.
- A model that misunderstands what it was asked to do and tries to post an
  unbalanced, out-of-period or duplicate entry.
- Someone with database or script access trying to quietly rewrite the audit log.

**Who this does not defend against.**

- **A token holder inside the allowlist.** They can call every enabled tool. That
  is what the token is *for*; the controls on offer are which tools exist and what
  the audit log records, not a per-caller identity model.
- **A System Manager on the site.** They can change every setting here, generate a
  new token and enable every write tool. This app adds no privilege that a System
  Manager did not already have — a System Manager can post a Journal Entry
  directly.
- **A compromised Frappe site.** If an attacker can run code on the site, this
  app's gates are the least of the problem.
- **Traffic interception.** This app does not terminate TLS. It rides on your
  site's existing reverse proxy; if that serves plain HTTP, the auth token
  crosses the network in the clear.

---

## The three gates

Every request runs all three, in this order, and all three must pass.
`erpnext_mcp/security.py` is the whole of it.

### 1. Master switch → 404

`enabled` off, or no token stored, returns **404**, not 403. A disabled endpoint
should be indistinguishable from an app that was never installed, so a scanner
learns nothing from probing the path. There is no configuration in which this
endpoint answers without a secret.

The settings form refuses to save `enabled` without a token, so the
"enabled-but-tokenless" state only arises from a direct database edit or a
half-finished restore — exactly when you least want the endpoint answering.

### 2. Auth token → 401

`X-MCP-Token: <token>`, compared with `hmac.compare_digest` so the comparison
takes the same time whatever the guess. 48 hex characters (~192 bits) from
`frappe.generate_hash`.

Stored in a **Password** field, which means Frappe keeps it in the encrypted auth
table rather than in the document row. Consequences worth knowing:

- Nothing reads it back out — not a tool, not the settings form, not the
  `selftest` diagnostic, which reports only *whether* a token exists.
- It is shown to the operator exactly once, in a dialog, at generation. That is
  why the button says **Generate New Token** and not **Show Token**.
- A site restored without its `encryption_key` cannot decrypt it. That fails
  closed: `auth_token()` returns `""`, and the endpoint answers 404.
- Generating is the same operation as rotating. The old token stops working the
  moment the new one saves, which is the behaviour you want when the reason you
  pressed the button is that the old one leaked.

**Why not `Authorization: Bearer`.** That is the MCP norm, and it is deliberately
*not* the primary header here. Frappe's own auth layer inspects `Authorization`
before any whitelisted method runs and routes a `Bearer` value into its OAuth2
validator; a token that is not an OAuth Bearer Token does not survive that trip
intact on every version. This was found the hard way on a live v15 site: a
correctly configured client arrived at the endpoint with nothing to present.

`X-MCP-Token` is a header Frappe has no opinion about, so it reaches this app
exactly as the client sent it. `Authorization: Bearer` is still accepted, second,
because it costs nothing and it is what a client will try by default — where
Frappe leaves it alone, it works. When both are sent, `X-MCP-Token` wins, so a
stale `Authorization` header left over from another integration cannot override
the one you configured.

### 3. Network allowlist → 403

The caller's address must fall inside one of the CIDR blocks in **Allowed CIDRs**.

- Default: `127.0.0.1/32,::1/128,10.0.0.0/8,192.168.0.0/16,172.16.0.0/12` —
  loopback and the RFC1918 private ranges. `::1/128` is in there because on a
  modern host `localhost` resolves to IPv6 first, so a first-run
  `curl http://localhost/...` arrives from `::1`.
- **An empty list denies everyone.** A blank field is far more likely a mistake
  than an intent to publish an accounting API, so it fails closed. The settings
  form refuses to save an empty list while `enabled` is on.
- A malformed entry is skipped at request time rather than taking the whole gate
  down — but it is *also* refused at save time, which is what makes skipping
  acceptable rather than dangerous.
- IPv4 and IPv6 are matched separately: `0.0.0.0/0` does not admit an IPv6 caller.
  Add `::/0` if that is genuinely what you want.
- Widening this to `0.0.0.0/0` is something you have to type yourself.

**How the caller's address is determined.** Behind a reverse proxy `remote_addr`
is the proxy, so the real caller is only in the `X-Forwarded-For` chain. That
header is client-supplied, and bench's stock nginx *appends* to it rather than
replacing it (`$proxy_add_x_forwarded_for`), so its **leftmost** entry is whatever
the client felt like claiming. This app therefore gates on the **rightmost** entry
— the one the nearest proxy appended itself, which a client cannot forge — falling
back to `remote_addr` when the header is absent.

> **If you run more than one proxy layer**, the rightmost entry is an inner proxy
> rather than the client, and the CIDR gate stops being meaningful. On that
> topology, rely on the auth token plus a firewall rule (`iptables`, a security
> group, a `deny` in nginx) and treat the allowlist as decoration.

Note that `frappe.local.request_ip`, which the rest of Frappe uses, is the
*leftmost* entry. This app deliberately does not use it for the gate. The IP
recorded in the audit log is the same one the gate evaluated, so a log row and a
gate decision can never disagree about who called.

### The same-origin exception

The CIDR gate is bypassed when **both** of these hold:

1. The request carries an `Origin` header whose host equals this site's host.
2. The session is a signed-in user with the **System Manager** role.

Each half closes the other's hole. `Origin` alone is worthless — any non-browser
client can send any value. The session alone would let a logged-in user's browser
be driven from a third-party page. Together they describe exactly one situation: a
page served by this site, fetched by a signed-in System Manager. That is the
operator using a console on their own Desk, and it is not something the allowlist
should be able to lock them out of.

### Rejections are opaque

Every refusal returns the same body: `unauthorized`, or `not found` for the 404
cases. The *reason* — "auth token missing or incorrect", "caller ip 203.0.113.7
is outside allowed_cidrs" — goes to **MCP Action Log**, where the operator can read
it and the caller cannot. Telling an unauthenticated caller "your IP is fine, your
token is wrong" hands them a free oracle for exactly the two facts worth probing
for.

---

## Authorization inside the gates

### Ledger reads: the token is the authorization

The accounting, workflow, trade and metadata read tools use `frappe.db.get_all`
and `frappe.get_doc`, **neither of which consults Frappe role permissions.** A
token holder can read everything those enabled tools cover, regardless of what
roles the MCP System User has.

This is deliberate, and you should decide whether you agree with it:

- **For it:** a token holder could read the same data through Frappe's own
  `/api/resource` endpoints anyway, given a session. Role-filtered reads on an
  accounting API also mean *silently* hidden rows — a balance that is wrong with no
  indication that it is wrong, which for a ledger is worse than a refusal.
- **Against it:** it means you cannot hand out a token scoped to one company or one
  account tree.

The granularity actually on offer is the per-tool switches. Turning
`get_chart_of_accounts` and `search_accounts` off leaves a client able to answer
questions about accounts it was told about and unable to go looking for others.
If you need per-company scoping, run a second site.

### Three read categories that DO enforce permissions

The line is drawn at content that is not ledger data:

- **Reports** (`run_report`) go through `frappe.desk.query_report.run` and
  `frappe.desk.reportview.get`, which check the acting user's permission on the
  report's `ref_doctype`. This is not a decision this app makes — it is what
  those APIs do — but it has a real consequence: **a report can reach data the
  individual read tools do not expose**, and the only thing bounding that is the
  MCP System User's roles. If you enable `run_report`, the roles you give that
  user are the security boundary. Give it the narrowest set that runs the reports
  you care about.
- **Attachments** (`list_attachments`, `get_attachment_content`) check `read` on
  the parent document, and treat an unattached private file as its owner's. A
  File is whatever somebody uploaded — a signed contract, a passport scan, a
  payroll export — and `is_private` is a promise the framework makes. Handing
  that to anyone holding an API token would be a different product.
- **Comments** (`list_comments`) check `read` on the document, for the same
  reason: a comment thread is what people said to each other about a document,
  not a field of it.
- **The mobile transport is the one exception, and it is narrower rather than
  wider.** `api/mobile.attach_file_to_document` calls
  `files.attach_file_to_authorized_parent`, which skips
  `frappe.has_permission(parent, "write")` and no other check. It may, because on
  that transport the caller has already been proved enrolled and scoped, the
  doctype is on a closed allowlist, and `require_scoped_doc` has confirmed the
  record is inside the caller's own entities — a company scope Frappe's model
  cannot express without a User Permission per row. The DocPerm being stood in
  for is the Desk's, and on nine of the eleven allowlisted registers it grants
  `write` to no role a field worker holds, which made the route refuse most of
  what it advertises (v0.152.0). The same brokering on the read side is
  `mobile.BROKERED_PARENTS`. The MCP tool itself is unchanged and still asks.

Yes, this is an inconsistency. It is a deliberate one, and the shape of it is:
*numbers in the ledger are gated by the token; things a person wrote or uploaded
are gated by Frappe.*

### Mutating tools: Frappe permissions apply in full

Every write goes through `frappe.get_doc(...).insert()` / `.submit()` /
`.cancel()`, which run the acting user's permission checks along with doctype
validation, the fiscal-year check, period-closing vouchers, account freezing,
mandatory dimensions and every `on_submit` hook. There is no raw SQL anywhere in
this app, and there should never be: the day an MCP tool writes a GL Entry
directly is the day it can corrupt a ledger.

So the MCP System User's roles **do** bound what mutations can do. Give it the
narrowest set that works — **Accounts User** is normally enough.

---

## Per-tool switches

Thirty-five tools, thirty-five switches, on a form only System Manager can open.
Five of the seven write switches sit in **Accounting Write Tools**; the other two
— `advance_workflow` and `create_todo` — sit in their own category sections. The
red banner at the top of the settings form always lists every write tool that is
currently live, wherever its switch happens to be, and it reads that list from
the server rather than from a copy in JavaScript.

**All seven mutating tools ship off.** A fresh install cannot change a single
document. A call to a disabled tool is refused *by name*, before its arguments are
looked at, so nothing about the arguments — valid or not — can leak back. A
disabled tool does not appear in `tools/list` either: a model cannot be tempted by
a tool it cannot see.

**The split that matters.** `create_journal_entry` only ever produces a draft
(`docstatus=0`), and there is no argument that makes it submit. Posting is
`submit_journal_entry`, a separate tool with a separate switch that takes a name
and nothing else — it cannot create the entry it submits. So:

| Enabled | What an AI client can do |
| --- | --- |
| neither | Nothing. Reads only. |
| `create_journal_entry` only | Propose entries all day. Not one touches a balance. A human reviews and submits. |
| both | Post to the general ledger unattended. |

The middle row is the one most operators want, and it is the reason the two are
not one tool.

**`advance_workflow` deserves its own paragraph.** It is the only write tool whose
blast radius depends on site configuration rather than on the tool: a transition
into a state with `doc_status: 1` **submits the document**, so approving a
Purchase Order through it does everything submitting that Purchase Order does.
Read `list_workflows` before enabling it and check which states carry
`doc_status: 1`. The upside is that it cannot invent a transition — it can only
take one an operator already designed, as a user the operator chose, subject to
the conditions the operator wrote.

**Read tools are switchable too**, for surface control rather than security. An
operator running this for bank reconciliation can turn the chart-of-accounts tools
off and stop a client wandering through the whole ledger for context it does not
need.

### Availability is not the same as enabled

Separately from the switches, a tool can declare a site prerequisite: the HR
tools need the `hrms` app, the sales tools need ERPNext, `get_bank_statement`
needs a doctype older versions do not ship, `list_client_scripts` needs Client
Script (or its pre-v13 name). A tool whose prerequisite is unmet is not
advertised in `tools/list` and cannot be called, **whatever the switches say**.

The distinction is not cosmetic. A tool that is listed and always fails is a trap
for a model, which decides what is possible from the catalogue and will keep
trying. And the two refusals need different words: *"your operator turned this
off"* sends somebody to go and ask, while *"this site does not have Frappe HR"*
tells them to stop. Availability is checked before the switch, so nobody is sent
to have a pointless conversation with an operator who could not help anyway.

A predicate that raises is treated as unavailable. An availability check that
errored is not evidence the tool would have worked.

### The tools that make an outbound request

Almost everything here reads and writes the local site. A few tools open a
connection *out* from the site's own network, which is the shape of every
server-side request forgery there has ever been, so each states its own rule:

- **`validate_public_endpoint`** probes this site from outside. It will only
  reach the operator's configured `public_url` or a host under `.ts.net`, over
  HTTPS, on the default port, and it refuses to send the bearer token anywhere
  but the configured URL. See `tools/funnel.py`.
- **`check_regulation_feed`** / **`check_all_regulation_feeds`** fetch the
  regulation page named on a Regulation Feed record and hash it. They detect
  change and never act on what they read.
- **`pull_model_from_vv`** (v0.59.0) fetches a trained model from Volume Vision.
  Its target is an operator's own training box on their own LAN —
  `http://umbrel.local:5101` — so a public-suffix allowlist would be exactly
  wrong and a hardcoded host would make this a script for one site. What is
  enforced instead, in `services/volume_vision.py`: **http/https only**, **no
  credentials in the URL** (a `user:pass@host` target is refused, not
  forwarded), **no redirects followed** (a 3xx is reported with its `Location`
  and nothing is fetched — following one is how an allowed host becomes an
  unallowed one between the check and the request), and a **512 MB ceiling**
  checked against `Content-Length` before the body is read and against the body
  after, so a server that lies about the first cannot exhaust the worker on the
  second. The URL comes from the ML Model record's own `source_server` unless a
  caller passes one, and the switch — `allow_pull_model_from_vv` — is off by
  default like every other mutating tool.

The fetched bytes are then held to their own contract: a zip is read as a model
bundle and refused if it does not open, carries no `manifest.json`, or names a
different `source_uuid` than the record it is being attached to.

---

## The audit log

Every call gets a row in **MCP Action Log** — reads, writes, refusals and calls to
tools that do not exist.

**Why reads.** A read tool cannot corrupt the ledger, but the interesting question
after the fact is rarely "what did it change" — it is "what did it see". A log that
only records mutations cannot tell you whether a client enumerated every account
before it was switched off.

**Append-only, and meant.** The doctype grants System Manager read and delete but
not write, and the controller refuses an update even from a script or a console —
a UI-only restriction is not an audit trail. Delete is allowed on purpose so a busy
site can be pruned; Frappe records every deletion in its own Deleted Document
doctype, so a pruned row still leaves a trace.

**It survives a rollback.** A Frappe request is one transaction. If a mutating tool
half-wrote a document and then failed, that write must be rolled back — and a naive
audit row would go with it, losing exactly the record you most want. So the order
is: roll back first, then insert the failure row into a clean transaction and
commit it on its own.

**Redaction.** Arguments are logged verbatim except for keys naming a secret
(`token`, `password`, `secret`, `api_key`, `credential`), which are masked. No
current tool takes one; a future one might, and a log that is read-only forever is
the wrong place to discover that.

**Uninstalling drops it.** `bench uninstall-app` removes the doctype and its table.
Export first if you need the history; `before_uninstall` will remind you.

---

## The second transport: the Farm Ops mobile API (v0.17.1)

**Everything above this heading describes ONE endpoint,
`/api/method/erpnext_mcp.mcp.handle`. Since v0.17.1 there is a second surface,
and none of the three gates applies to it.**

That is not an oversight, it is arithmetic. `security.authorize()` is called *by*
`mcp.handle`; eleven whitelisted methods under `erpnext_mcp.api.mobile.*` and
`erpnext_mcp.api.files.*` are reached directly by an iOS client and never pass
through it. Each of the three gates is also individually inapplicable:

- **The shared token.** A phone has never had one, and distributing one secret to
  forty devices is a secret in name only.
- **The CIDR allowlist.** The entire point is a worker on LTE in an orchard,
  outside every RFC1918 range the default list contains.
- **The per-tool switches.** Those govern what the *AI* may do. A field worker is
  a different principal and gets a different gate; coupling them would mean you
  could not stop the AI completing tasks without taking forty phones down with it.

### What stands in their place

Rebuilt in `erpnext_mcp/api/guard.py`, run on **every** call, in this order:

| # | Gate | Refusal |
|---|---|---|
| 1 | `farm_ops_mobile_enabled` (Settings **or** `site_config.json`; either off means off) | **503** |
| 2 | Not Guest | 403 |
| 3 | One of `Field Worker`, `Farm Worker`, `Foreman`, `Farm Manager` | 403 |
| 4 | An **Active Mobile Access Grant** | 403 |
| 5 | Rate limit per user per method (read 60/min, write 10/min, completion 20/min, chunk 120/min) | **429** |
| 6 | Entity scoping on every company argument and every returned row | 403 / 404 |
| 7 | MCP Action Log row + secret strip — on success *and* on every refusal | — |

Gates 2–4 all answer with the **same message**, so a caller cannot learn which
one it failed. The reason is written to the audit log, where the operator can
read it and the caller cannot — the same reasoning as "why the client never
learns which gate it failed", above.

### Gate 4 is the one that surprises people

**Holding a field role is not being enrolled.** `Administrator` holds every role
on the site, so the role gate alone would let the operator's own login drive the
field API by accident — and an admin account is the one credential that could
reach every entity at once. The grant is a deliberate act with a doctype and an
owner; `revoke_mobile_user` ends it, and the door closes on the very next call
rather than whenever the token is next rotated.

### Entity scoping deliberately inverts Frappe's default

In Frappe a user with **no** User Permission on Company is **unrestricted**. That
is the framework's rule and every Desk surface honours it. On an endpoint
reachable from the open internet it means the single worst-configured account on
the site is also the least scoped one, so the mobile surface **refuses** a caller
with no entities instead of showing them everything. `create_mobile_user` already
refuses to create such an account; the two together mean there is no path to an
unscoped phone.

Scoping is applied twice on purpose — once as a query filter, once as
`guard.scoped()` on everything leaving the building — because the tools read
through `frappe.db.get_all`, which does **not** consult User Permissions. A
wrapper that trusted the framework here would hand the holding company's task
board to an operating company's picker.

### The surface is a closed list and cannot grow by accident

There is no dispatcher, no `call(tool_name, args)`, no registry lookup. A method
exists as a function or its path 404s, so the reachable surface is eleven
`@frappe.whitelist()` lines you can audit by reading them. The other ~195 tools
are not reachable from a phone at any path.

Arguments that would be dangerous are **absent from the signatures** rather than
filtered out — Frappe drops body keys a whitelisted method does not declare, so an
argument that is not in the signature is one no client can send: `cancel` (a
rejection could otherwise delete the work), `record_data`, `worker_id`,
`attach_to_doctype`/`attach_to_name`, `governance_document`, `is_private`.

Uploads carry an extension allowlist (`.jpg .jpeg .png .heic .heif .webp .pdf` —
`.html` and `.svg` both execute script when served and are not on it) and reduce
filenames to a basename. Every committed evidence file is private and attached to
nothing; it reaches its compliance record through `complete_task_via_mobile`,
which checks the task belongs to the caller.

### How the phone proves who it is — three carriers, one credential (v0.17.2)

**The Tailscale `serve`/`funnel` proxy removes the `Authorization` header.**
Proven three ways on 2026-08-01: the call works against `localhost` inside the
container, and returns the Desk's `/me` page through the tunnel — including from
a machine *on the tailnet*, which rules out the public funnel edge and leaves the
proxy step. Frappe authenticated nobody, so `is_whitelisted` refused a Guest
before the method ran, and the phone got HTML with a 200 on it.

So the app sends the **same** `<api_key>:<api_secret>` pair three ways and the
server takes the first that resolves:

| | Carrier | Who reads it |
|---|---|---|
| a | `Authorization: token <key>:<secret>` | Frappe's own auth layer. Nothing in this app runs. |
| b | `X-FarmOps-Token: <key>:<secret>` | `erpnext_mcp/api/fallback_auth.py`, via an `auth_hooks` entry |
| c | `"_auth": {"api_key": …, "api_secret": …}` in the POST body | the same, when neither header survives |

**(b) and (c) are doors, not bypasses.** They answer exactly one question —
*which Frappe user is this* — and answer it with Frappe's own scheme: look the
`api_key` up on User, compare the stored secret with `hmac.compare_digest`,
refuse a disabled account. **All seven gates above then run on the user they
establish**, unchanged. A wrong secret is Guest, not an error, and produces the
same opaque refusal as everything else.

Three properties worth knowing:

- **A credential Frappe already validated is never overridden.** (a) beats (b)
  beats (c), always.
- **Failed verifications are metered per key, successes are not.** Ten wrong
  answers for one `api_key` in a minute close the fallback path *for that key*.
  The counter is deliberately **not** keyed on the caller's address: every phone
  on the farm arrives from the funnel's single address, and one stale credential
  would take the rest off the air.
- **The audit row says which door.** `mobile:<method>` rows carry
  `(fallback_auth: header)` or `(fallback_auth: body)`; a row with no such tag is
  a request whose `Authorization` header survived the proxy. That is how you tell
  whether your tunnel is still eating headers.

The `auth_hooks` entry is this app's only request-lifecycle hook. It acts on
`/api/method/erpnext_mcp.api.*` and nothing else, it grants no permission, and it
cannot raise.

### Statuses are part of the contract

`FarmOpsKit` reads **401** as *"credential dead, sign out and re-scan"* and
anything else as *"offline, keep working into the queue"*. So the kill switch
answers 503 and the rate limit answers 429: a refusal that answered 401 would sign
forty phones out and destroy every queued completion sitting on them. Do not
"tidy" these into 401.

### The perimeter is now your tunnel

With the CIDR gate gone from this path, what stands between the internet and these
eleven methods is Frappe's own token authentication plus the seven checks above.
Two consequences worth acting on:

- **Audit what your Tailscale Funnel actually publishes** (`tailscale serve
  status --json`). A whole-port funnel publishes `/app` and every whitelisted
  method of every installed app, not just these eleven.
- **Never funnel `/api/method/` as a prefix.** Expose
  `/api/method/erpnext_mcp.api.mobile.` and `/api/method/erpnext_mcp.api.files.`
  specifically if your funnel is path-scoped.

### The third transport: farmops-api (v0.18.0)

**The eleven methods above are now ALSO served by a separate process, and that
is the one a phone actually calls.** Everything in this section still applies to
it, verbatim, because it is the same code — but the perimeter facts changed and
an auditor needs both.

| | The whitelisted path (v0.17.1–0.17.2) | farmops-api (v0.18.0) |
|---|---|---|
| Address | `/api/method/erpnext_mcp.api.{mobile,files}.*` | `/farmops/api/{mobile,files}/<method>` |
| Served by | Frappe's own WSGI handler, port 8000 → nginx 8080 | `erpnext_mcp/farmops_api/`, gunicorn, port 5250 |
| Identity | Frappe auth + `fallback_auth` `auth_hooks` | `X-FarmOps-Token`, verified by **the same** `fallback_auth` verifier |
| The seven gates | `api/guard.py` | `api/guard.py` — literally the same decorated functions |
| Reachable from | whatever the funnel publishes | only what is explicitly mounted at `/farmops/api/…` |
| Status | **still live**, LAN and in-container | what the phones use |

**Why a second process at all.** Through the Tailscale funnel, all five of
v0.17.2's credential carriers returned the Desk's HTML login page, and all five
worked against `localhost` inside the container. The remaining common factor was
Frappe's `/api/method` handler on that path behind that proxy. This routes around
it; it does not explain it. See `RELEASES/v0.18.0.md`.

**What an auditor should check, specifically:**

- **The route table is closed.** `farmops_api/routes.py` is eleven entries with
  no dispatcher and no method-name argument, asserted against `api/mobile.py` and
  `api/files.py` in both directions. `create_journal_entry`, `convey_parcel` and
  `import_chart_of_accounts` are not reachable at any path.
- **The gates are not copied.** Every route names a `@guard.endpoint`-wrapped
  function. `ByteIdentical` in `tests_standalone/test_farmops_api.py` compares
  both transports' serialised responses over every read, which is what would
  catch a copy that had started to drift.
- **The argument filter is reproduced.** Frappe's handler binds a body to a
  signature by dropping unknown keys, and the eleven wrappers were written
  against that — it is what makes `record_data`, `worker_id`, `cancel` and `user`
  unreachable from a phone. `routes.bind` does the same by `inspect`, and the
  four refused arguments are asserted absent from every route's accepted set.
- **The bind address.** gunicorn listens on `0.0.0.0:5250` *inside the
  container*; the compose publishes it as `127.0.0.1:5250` on the host. The
  prefix is what keeps it off the farm LAN, and it is one edit away from not
  being there. Check `docker compose config` on the running app, not the repo.
- **The failure counter is shared.** `fallback_auth.verify_credential` meters
  wrong answers per api_key, and both transports call it — so a guesser does not
  get a fresh budget per door.
- **Nothing answers HTML.** Every status the service can produce is JSON,
  including 404, 405 and an unhandled exception in its own error handler.

The **kill switch stops both**, because it is gate 1 inside the shared
`guard.endpoint`. To stop only the new transport:

```sh
docker exec <container> supervisorctl stop farmops-api
```

### Stopping it

ERPNext MCP Settings → untick **Farm Ops Mobile API Enabled**, or:

```sh
bench --site <site> set-config farm_ops_mobile_enabled 0
```

Next request, every phone gets a 503 and keeps its queued work. The MCP endpoint
is untouched — the two switches are separate on purpose.

---

## The third transport: the bank push endpoints (v0.73.0, v0.74.0)

Three whitelisted methods a bank pipe calls with its own ERPNext credential:

```
POST /api/method/erpnext_mcp.bank.push_statement_anchor
POST /api/method/erpnext_mcp.bank.push_account_pairing
POST /api/method/erpnext_mcp.bank.push_account_metadata   (v0.74.0)
```

The third is the second with the pairing taken out, and it exists so a nightly
metadata refresh does not need a credential path that can also repoint which two
accounts are companions. It refuses `paired_bank_account` and `pairing_type` by
name — least authority applied to a payload rather than to a role.

**They do not use the MCP gates and they do not use the mobile gates.** Neither
is right for this caller, and the reasons are different.

The MCP transport's three gates are a bearer token, a CIDR allowlist and a master
switch — a *surface* control over what an AI client may reach. A bank pipe is not
an AI client: it does not choose what to call, it has a handful of things to say,
and giving it the MCP token would give it the whole tool catalogue.

The mobile surface's seven gates include an **Active Mobile Access Grant**, which
exists to make "enrolled" a fact about a handset somebody deliberately issued.
There is no handset here and no enrolment to speak of, and a server-to-server
credential that had to be registered as a phone would be a lie in the register a
`revoke_mobile_user` reads.

So these use **Frappe's own permission system**, the same choice `api/gis.py`
makes for the Desk map:

1. **A named user.** Guest is refused before anything is read. A pipe gets its own
   ERPNext user with an API key.
2. **`frappe.has_permission(doctype, "write", throw=True)`** on the doctype being
   written — `Statement Anchor` or `Bank Account`. This is what makes a **User
   Permission scoping the credential to one company** apply here as it applies in
   the Desk, and it is the reason to give a pipe a real user rather than reusing
   an administrator's key.
3. **An audit row on every call, including the refusals**, in MCP Action Log,
   prefixed `push:`. "Which credential pushed the October anchor, and when" is the
   first question anybody asks when two systems disagree about a number, and an
   endpoint that answered it with silence would put the operator back to reading
   server logs. Refusal rows are committed on their own transaction so they
   outlive the rollback.

**What a compromised pipe credential can do**, which is the question worth asking:
write statement anchors and Plaid metadata for accounts that already exist. It
cannot create a Bank Account, cannot post to the ledger, cannot reach any MCP
tool, and cannot assert a variance — every derived number on an anchor is
recomputed from the pushed inputs, so the worst available lie is a wrong opening
or closing balance, which shows up immediately as a variance against the
transactions already on file.

**Turning them off.** There is no dedicated switch and deliberately so: the
control is the credential. Disable the pipe's User, or remove its write permission
on Statement Anchor and Bank Account, and all three methods refuse on the next
call.

---

## What the endpoint is not

*(This section is about the MCP endpoint. The mobile surface described above
answers several of these differently — where it does, it says so.)*

- **Not a public API.** It is one whitelisted Frappe method, intended to be reached
  over a LAN or a private tunnel. Do not add it to a public reverse-proxy path.
  **The mobile surface is the exception and is public by design**, which is why it
  carries its own seven gates instead of these three.
- **Not a second listener.** No new port, no sidecar, no process to supervise. It
  inherits your site's TLS, nginx rate limits and access logs, and it is up
  whenever the site is.
- **Not an SSE stream.** POST-only. This server never initiates a message, so
  there is nothing for a stream to carry, and a `GET` returns a 405 saying so
  rather than an idle connection that looks like it is working.
- **Not a per-caller identity model.** One token, one configured acting user. The
  mobile surface *is* per-caller: it runs as the authenticated worker throughout,
  which is what makes its entity scoping and its staging-session ownership real.
  Two clients that should see different things need two sites, or a v0.3 that
  has per-token scopes — see the README roadmap.
- **Not rate limited by this app.** The MOBILE surface is — per user, per method,
  per minute, in `api/guard.py` — because it is reachable from the internet and an
  MCP session is not. For the MCP endpoint itself, if you want that, Frappe ships a decorator —
  add `@rate_limit(key="mcp", limit=120, seconds=60)` from `frappe.rate_limiter`
  above `handle()` in `erpnext_mcp/mcp.py`. It is left off by default because an
  MCP session legitimately makes many calls in quick succession, and a limit tuned
  for a login form would break normal use.

---

## Hardening checklist

- [ ] Narrow **Allowed CIDRs** to the subnet your client is actually on, not the
      whole of `10.0.0.0/8`. (This does **not** cover the mobile surface — it has
      no CIDR gate and cannot have one. See the two items below.)
- [ ] Check what your tunnel actually publishes: `tailscale serve status --json`.
      A whole-port funnel exposes `/app` and every whitelisted method of every
      installed app, not only the eleven mobile ones.
- [ ] Confirm every **Mobile Access Grant** is one you meant to issue, and that
      each has entity access naming at least one Company — `list_mobile_users`
      flags an account with none, and in Frappe none means *every*.
- [ ] Create a dedicated **MCP System User** with **Accounts User** and nothing
      more. Do not leave mutations running as `Administrator`.
- [ ] Leave every mutating switch off until you have a specific need, then enable
      the narrowest one. If `create_journal_entry` is enough, do not enable
      `submit_journal_entry`.
- [ ] Turn off any read tool the client does not need.
- [ ] Serve the site over HTTPS. The auth token is only as private as the
      transport.
- [ ] Firewall the port as well. The CIDR gate is defence in depth, not a firewall.
- [ ] Watch **MCP Action Log** for `Unauthorized` rows — those are someone probing
      a live endpoint.
- [ ] Rotate the token when a client machine is decommissioned. One button.
- [ ] If you enable `run_report`, remember the MCP System User's roles are the
      only bound on what reports can reach. Audit that user's Role Profile.
- [ ] Decide deliberately about `get_attachment_content`. Files on an ERPNext
      site are frequently the most sensitive thing on it.
- [ ] Before enabling `advance_workflow`, run `list_workflows` and check which
      target states carry `doc_status: 1` — those transitions submit documents.
- [ ] Export the audit log before uninstalling.

## Reporting a vulnerability

Open a GitHub issue for anything already public. For something not yet public,
email the address in `pyproject.toml` rather than filing publicly.
