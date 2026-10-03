# office@ reply drafts — design and contract

**Status: FROZEN FOR REVIEW (2026-10-02).** Nothing is built. Queued after the `/farmops`-only
cutover and the "Device & client enrollment" batch. Building starts after Tim has answered §12.
Server **v0.220.0 →**, app **0.30.0** (versions are placeholders until the batches ahead ship).

Ground rules: **nothing is ever sent without a named person approving it**; inbound mail is
untrusted input; the phone talks only to `/farmops/api`; mutating MCP tools ship OFF; avoid table
sprawl; reuse the document-intake agent and the triage machinery.

---

## 0. Incoming mail on OML today (read-only diagnosis, 2026-10-02)

| Fact | Source |
|---|---|
| Account **Office**: `imappro.zoho.com:993`, SSL, IMAP, sync rule **UNSEEN**, initial sync 250, login `office@orchardmeadow.net`, password auth (not OAuth) | Error Log traceback variables |
| Only **7** received emails in ERPNext, ever; the newest on **2026-09-25** (one week ago); 6 of 7 unlinked ("Open"), 1 linked | Communication |
| 2026-09-21 03:00 `AttributeError: 'EmailServer' object has no attribute 'imap'` | Error Log |
| 2026-09-29 12:50 `ValidationError: socket error: EOF` while connecting | Error Log |

Account settings themselves were not read: the MCP refuses Email Account outright because it holds
the mailbox password (by design).

**Likely causes, most likely first:**

1. **Sync rule UNSEEN.** Frappe imports only messages still *unread* on the server. Anything opened
   in Zoho webmail or Zoho Mail on a phone before the next pull (every ~5 min) is **never imported**.
   With a person also reading office@, this alone explains a near-empty inbox.
2. **Repeated connect failures may have disabled incoming.** Frappe counts failed connects on the
   account (`no_failed`) and, past its threshold, can switch incoming off. Two errors are logged;
   whether the account is still enabled is the first thing §0.1 checks (to be confirmed against
   Frappe 15's code on umbrel.local).
3. **Zoho-side.** IMAP access toggled off, an app-specific password rotated, or Zoho dropping idle
   TLS sessions (the EOF).
4. **Scheduler.** Mail is pulled by the scheduler. If it is paused or a worker is stuck, nothing is
   pulled and nothing errors.

The 09-21 `AttributeError` is a follow-on: Frappe tries to select INBOX after the connection itself
failed. It is noise, not a separate fault.

### 0.1 Phase 0 — make incoming reliable (first, before any triage)

- **`get_mail_status`** (read; also folded into `get_server_status.email.incoming`): per incoming
  account — enabled, server, port, SSL, sync rule, `uidvalidity`/`uidnext`, failed-attempt count,
  last received Communication and its age, errors in the last 7 days, scheduler state. No password,
  ever.
- **Recommended setting change** (a tool, default OFF, shown to Tim first): sync rule **ALL**
  (Frappe then tracks IMAP UIDs, so a message read in Zoho is still imported once, and never twice —
  behaviour to be proved on umbrel.local before OML)
  and re-enable incoming if it was disabled. ALL with a high initial sync count would import old
  mail on the first pull: the tool sets `uidnext` to the current mailbox position first, so only
  new mail arrives.
- **Watchdog** (scheduled, writes nothing but alerts): no received mail for 24 h while the account is
  enabled, or 3+ connect errors in a day → alert to **Security Alert Recipients** (v0.216.1) and the
  `farmops_auth_alert`-style hook. A silent inbox becomes a message within a day.
- **Verify on umbrel.local first**, then Tim applies on OML.

---

## 1. Triage (item 1)

A scheduled job (every 5 minutes; also callable) picks up each new **Received** Communication on an
account listed in `office_mail_accounts` (a setting; default: the Office account). It does not use
`doc_events`: this app installs no document hooks (hooks.py), and a poll is enough.

**Classes:** `customer`, `supplier`, `invoice_receipt`, `compliance_regulatory`, `training`,
`personal`, `spam` — plus the orthogonal flag **`suspicious`** (§5).

**Step 1 — deterministic, server-side, no model.** Same shape as `triage.py`: reads words and
records, never meaning.

| Signal | Leads to |
|---|---|
| Sender address = a Contact linked to a Customer / Supplier | `customer` / `supplier` + link |
| Sender = Employee (company or personal email, user id) | `personal` (or `training` by keywords) + link Employee |
| Supplier's invoice number / our PO / Purchase Invoice `bill_no` in subject or body | `invoice_receipt` + link that Purchase Invoice / PO |
| Attachment the document-intel reader classifies as receipt/invoice | `invoice_receipt` (existing `document_intel`) |
| Sender domain on the regulator list (`*.wa.gov`, `epa.gov`, `usda.gov`, `osha.gov`, `dol.gov`, … — a config list) or a Regulation Feed's source | `compliance_regulatory` |
| Training Session name/date, certificate keywords | `training` + link Training Session |
| Zoho's spam header if present; bulk/list headers; no-reply senders | `spam` (never drafted) |

**Step 2 — the model refines** (§6): it may change the class, propose a different link and say why,
with a confidence. A person can override either in one tap.

**Linking reuses document intake.** `route_incoming_document` / Frappe's Relink (`reference_doctype`,
`reference_name`, status Linked, no save, an audit comment). Attachments are routed onto the record
by `file_url` as today. Nothing new moves bytes.

## 2. Draft generation (item 2)

For classes other than `spam` and `personal` (configurable), the server builds a **context pack**
from ERPNext — **facts only, no prose**:

| Class | Context |
|---|---|
| customer | open Sales Invoices (status, due, outstanding), last payments, open Sales Orders, primary contact |
| supplier | open Purchase Invoices / POs for that supplier, last payment, our contact |
| invoice_receipt | the matched Purchase Invoice (status, due, paid/outstanding), the Expense Receipt match |
| compliance_regulatory | matching Compliance Rule / alert / filing status and due dates |
| training | the person's upcoming Training Sessions, certifications and expiry |

The model writes a reply **draft** from the message, the pack, the company's style notes and recent
examples (§4). The draft is stored, **never sent**, with the exact pack it used (so the reviewer sees
which facts went in).

**Drafts are never written** for: `spam`; `suspicious` mail; any message asking to change payment or
bank details (§5); mail from a sender the farm has never corresponded with *and* that asks for money
or documents. Those are flagged for a person instead.

## 3. Review and send (item 3)

**One state machine:** `Triaged → Drafted → (Edited) → Approved → Sent`, or `Discarded`, or
`Needs person` (no draft by rule).

| Surface | What |
|---|---|
| MCP | `list_mail_drafts` (read), `get_mail_draft` (read: message, flags, links, pack, draft), `update_mail_draft` (edit text / class / link), `approve_mail_draft` (sends), `discard_mail_draft`, `redraft_mail` — all mutating tools default OFF |
| Desk | a list view of Office Mail with filters by state/class and an "Approve and send" button on the form |
| Phone | a server-driven tile **"Replies to review"** (new tile report kind `mail_drafts`, badge = count) → list → detail: original message, flags, linked record, editable draft, attachments as explicit checkboxes, **Approve and send** / Discard. Routes under `/farmops/api/mobile/…`. Face ID on approve once device enrollment has shipped. |

**Sending** goes through Frappe's own `communication.make` — the path the Desk's Reply uses — from the
Office account, as Office (`always_use_account_email_id_as_sender` is already on for Zoho, v0.216.1),
`in_reply_to` the original, referenced to the linked record, with the company footer (v0.216.1). The
approver must:

1. hold a role in `mail_approver_roles` for that class (setting; default: Accounts Manager/User for
   customer, supplier and invoice_receipt; HR Manager for training and personal; Compliance Officer /
   Farm Manager for compliance; System Manager for all); **and**
2. pass Frappe's own `email` permission on the linked document (the same check the Desk's email
   dialog applies); **and**
3. be a person: the MCP path takes the calling human from `security.capture_calling_user`; the MCP
   System User alone can never approve.

**Audit:** who approved, when, from which surface (Desk / phone device / MCP client), the exact text
sent, the attachments chosen, and the resulting outgoing Communication — on the Office Mail row and
as an MCP Action Log row.

## 4. Writing style (item 4)

- On send, the row keeps `draft_text`, `sent_text`, a unified diff and an edit-distance ratio.
- **Examples:** the next draft for the same company and class receives the N most recent sent replies
  (default 5), preferring ones that were edited (they carry the corrections), as few-shot examples.
- **Style notes per company:** a Farm Feature Flag `mail_style_notes` (company-scoped; flags already
  support that) — e.g. "Sign as Orchard Meadow office; short; no exclamation marks; Spanish if they
  wrote in Spanish."
- No examples leave the company they were written for.

## 5. Safety (item 5)

- **Never auto-send.** No code path sends except `approve_mail_draft` with a human approver (§3). A
  test pins that the drafting job, the triage job and every model path cannot reach a send.
- **Attachments and financial details need explicit approval.** The draft may *propose*
  attachments; each must be ticked by the approver. A draft that contains an amount, an account or
  routing number, a tax ID or a bank name is marked `contains_financial_details`, and approve asks
  for a second explicit confirmation on every surface.
- **Suspicious / phishing flags** (shown in red, never drafted):
  - display name names a known supplier/customer/bank but the address domain is not theirs;
  - a look-alike of a known domain (edit distance ≤ 2, swapped TLD, homoglyphs);
  - **payment-change requests** ("new bank details", "updated ACH", "change of remittance", "wire
    to"), however polite;
  - urgency + payment ("today", "overdue, pay now") from a first-time sender;
  - links whose text and target differ; attachments of executable or macro types.
- **Bank changes are never confirmed by a draft.** Such mail gets no draft; it gets a `Needs
  person` state with the standing instruction: verify by phone using the number already on file,
  never the one in the email. Optionally a Farm Task to the accounts person.
- **Inbound mail is untrusted.** The model that drafts has **no tools**: it receives text and returns
  text. Instructions inside an email ("ignore previous…", "send the W-9 to…") cannot reach anything
  but the draft, and the draft is read by a person before it can go anywhere.
- **SPF/DKIM/DMARC.** Frappe does not keep the raw headers of an imported message. Phase 0 checks
  whether Zoho's `Authentication-Results` can be captured on import (a small addition to the pull,
  if Frappe allows it without a core patch); until then the flags above are content- and
  sender-based, and Zoho's own spam filtering is the first line.

## 6. Who drafts (item 6) — options

| | A. MCP client pulls | B. Server-side provider | C. Hybrid (recommended) |
|---|---|---|---|
| How | Claude (Claude Code / Desktop, or a scheduled Claude task) calls `list_mail_needing_drafts`, `get_mail_context`, `save_mail_draft` | a scheduled job calls a configured model API (e.g. Claude via the Anthropic API) with the pack | triage + context always server-side; drafting by A by default, B behind a setting |
| Key on the box | none | an API key, encrypted in settings | only if B is turned on |
| Latency | when someone (or a schedule) runs Claude | minutes | A: on schedule; B: minutes |
| Cost | Tim's existing Claude use | per email | either |
| Prompt-injection blast radius | the client has many tools — the drafting tools must be the only ones the session is given (a "mail-drafter" scope; ties into the OAuth scopes of the enrollment batch) | none: no tools | as A/B |
| Fits this app | the document-intake agent already works this way | new outbound dependency | — |

**Recommendation: C, starting with A.** The server does everything deterministic (triage, linking,
flags, context packs, the queue). Drafting is done by a Claude client limited to the drafting
scope. B is an optional setting for later, if waiting on a client turns out to be the bottleneck.

## 7. Data model — one new doctype

**Office Mail** (one row per received Communication on an office account):
communication (Link, unique), company, account, received_at, sender, class, class_source (rule /
model / person), confidence, suspicious (Check) + flags (JSON), linked doctype/name, state,
context_pack (JSON), draft_text, draft_model, proposed_attachments (JSON), contains_financial_details,
approved_by, approved_at, approved_via, sent_text, sent_attachments (JSON), sent_communication
(Link), diff, edit_ratio, discarded_by/at/reason.

Nothing is added to Communication (a doctype this app does not own). Style notes reuse Farm Feature
Flags; examples are Office Mail rows. Settings: `office_mail_accounts`, `mail_approver_roles` (JSON),
`mail_drafting_mode` (`mcp` / `provider` / `off`), provider fields only if B ships.

## 8. Phases

| Phase | Server | App | Content |
|---|---|---|---|
| 0 | v0.220.0 | — | incoming status, watchdog, sync-rule fix tool; verified on umbrel.local, applied on OML by Tim |
| 1 | v0.221.0 | — | Office Mail doctype, triage job, linking, flags, Desk list, MCP read tools |
| 2 | v0.222.0 | — | context packs, drafting via MCP (A), review/approve/send via MCP and Desk, audit |
| 3 | v0.223.0 | 0.30.0 | phone tile "Replies to review", approve from phone (Face ID after enrollment) |
| 4 | v0.224.0 | — | style learning (diffs, examples, notes); optional server-side provider (B) |

## 9. Surfaces (estimate)

MCP tools: `get_mail_status`, `list_mail_drafts`, `get_mail_draft`, `list_mail_needing_drafts`,
`get_mail_context` (read, 5); `fix_incoming_mail_sync`, `triage_mail`, `save_mail_draft`,
`update_mail_draft`, `approve_mail_draft`, `discard_mail_draft`, `redraft_mail` (write, 7, all OFF).
Mobile routes: `list_mail_drafts`, `get_mail_draft`, `update_mail_draft`, `approve_mail_draft`,
`discard_mail_draft` (+5). Tile report kind `mail_drafts`. Scheduled jobs: triage (5 min), incoming
watchdog (hourly).

## 10. Tests that pin the promises

- No send without an approval row naming a human; the MCP System User cannot approve.
- Payment-change mail never gets a draft; suspicious mail never gets a draft.
- An attachment is sent only if the approver ticked it.
- The drafter receives no tool definitions.
- Approve refuses a user without the class role or without `email` permission on the linked doc.
- Triage is idempotent: a Communication is triaged once.

## 11. Not in scope

Sending from any account other than the office ones; bulk mail; auto-replies of any kind; reading
other people's mailboxes.

## 12. Decisions for Tim

1. **Sync rule ALL** for the Office account (Phase 0) — recommended; it is the likely reason mail is
   missing.
2. **Drafting mode C/A** (Claude client pulls) first; server-side provider later, or never.
3. **Approver roles per class** (§3) — the defaults above, or a simpler "Office Approver" role.
4. **Which classes get drafts** — default all except spam and personal.
5. **Financial-detail second confirmation** on every surface (recommended) vs. Desk only.
6. **A Farm Task for payment-change requests** to the accounts person — on or off.
7. **Header capture for SPF/DKIM** — worth a small pull-time addition if Frappe allows it without a
   core patch; otherwise content-based flags only.
