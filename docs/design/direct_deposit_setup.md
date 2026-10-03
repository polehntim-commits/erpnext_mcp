# Secure direct-deposit setup — design and contract

**Status: FROZEN (2026-10-03).** Tim approved. Server **v0.225.0**, app **0.32.0**. Everything OFF:
setting **`direct_deposit_self_service`** (off) hides the phone flow and refuses its routes;
**`direct_deposit_plaid_enabled`** (off) hides Plaid.

## 0. Rules

- **Bank details arrive in the app only.** Never by email: office@ triage already flags bank-change emails
  as phishing (v0.221.0, `payment_change` → Needs person); its standing note now adds "direct-deposit
  changes are made only in the Farm Ops app". No route, tool or form accepts bank details for somebody
  else from a phone, and no MCP tool writes them.
- **A new account is not paid until it is verified, approved and its hold has passed.** It is created
  as an Employee Bank Account with status **Pending**, which `generate_nacha_file` never pays (it pays
  `Active` only). The account it replaces keeps paying until then.
- **Masked everywhere.** Routing and account numbers appear as their last 4 in every answer, list, email,
  push and log. The full account number lives only in the existing encrypted Password field
  (`account_number`), read only by `generate_nacha_file` / `generate_prenote_file`.

## 1. The worker's flow (phone, own account only)

1. **My records → Direct deposit**: current account(s) masked, and any change in progress.
2. **Change account**: routing number (ABA checksum checked on the phone and on the server; the bank's name
   filled from a small table that includes the fintech sponsor banks — Cash App: Sutton Bank 041215663 and
   Lincoln Savings Bank 073923033 — or typed), account number entered twice, Checking/Savings.
3. **Sign**: with device keys on, Face ID — the unlock key signs
   `farmops-direct-deposit|<employee>|<routing>|<sha256(account number)>|<account type>`; otherwise the
   authenticated device credential, recorded as such. A **Signing Evidence** row records signer, method,
   device, address, time and the document hash.
4. Server: creates the **Pending** Employee Bank Account (`verification_state: Submitted`,
   `replaces`: the current Full account), and sends the **notice** (§3).
5. **Verify** — one of:
   - **Plaid** (when enabled and the bank is supported): the phone opens Plaid **Hosted Link** in
     `ASWebAuthenticationSession` (no Plaid SDK — the app stays Apple-only). The server then exchanges the
     token, calls **Auth** (routing + account) and **Identity** (owner names), and requires: routing
     equal, account equal, and the owner name matching the Employee (normalised tokens; first + last both
     present). It then removes the Item (`/item/remove`) — nothing of Plaid's is kept beyond the result.
   - **Bank form** (fallback, and the only way for banks Plaid does not support, e.g. Cash App): the
     worker picks the bank's direct-deposit form (PDF or photo). The phone extracts routing and account on
     the device (PDFKit text, else Vision OCR) and must match what was entered before it uploads. The
     server re-extracts from a text PDF (pypdf) and requires the same match; where it cannot read the file
     (a photo) the state is `Needs review` and the manager compares by eye. The file is stored PRIVATE on
     the Employee Bank Account, with its SHA-256.
6. **Manager approval** (Desk, HR Manager / System Manager): the Employee Bank Account form shows the
   verification, evidence and notice, with **Approve** / **Reject**. Approval needs `Verified` or a
   reviewed `Needs review`. No MCP tool approves.
7. **Prenote**: an approved Pending account is included in the next `generate_prenote_file` (zero-dollar
   entry). `activates_on` = prenote date + **`direct_deposit_hold_days`** (default **14**, one biweekly pay
   cycle; NACHA allows 3 banking days for a return).
8. **Activation**: a daily job makes the account Active on `activates_on` and the account it replaces
   Inactive — unless a prenote return was recorded (`prenote_returned`, set by a person), which makes it
   **Rejected**. The worker gets a push; the notice channel gets a second notice.

## 2. Data — one doctype extended, nothing new

**Employee Bank Account** gains: status options **Pending**, **Rejected**; `verification_state`
(Submitted / Verified / Needs review / Failed), `verification_method` (Plaid / Bank form),
`verification_detail` (what matched — never a number), `name_match` (Check), `submitted_via` (phone /
desk), `submitted_at`, `signing_evidence` (Link), `evidence_file` (Attach, private), `evidence_sha256`,
`replaces` (Link Employee Bank Account), `approved_by`, `approved_at`, `prenote_returned` (Check),
`activates_on` (Date), `notice_sent_to`, `plaid_request` (Data, the Hosted Link token id; never a token).

## 3. Change controls

- **Notice to the previous channel**: on submit and again on activation — email to the Employee's
  personal / company email on file *before* the change, and a push to every other enrolled device:
  "A change to your direct deposit was requested on <date> from <device>. If this was not you, tell the
  office now." No numbers, no link.
- **Manager approval** as above; the approver cannot be the employee.
- **Hold**: `direct_deposit_hold_days` (default 14) after the prenote.
- **Rate**: one open change per employee; a second submission replaces the first and is noticed again.
- **Audit**: MCP Action Log row on submit, verify, approve, reject, activate; Signing Evidence for the
  worker's signature.

## 4. Plaid configuration (decision 1)

Settings on ERPNext MCP Settings: `plaid_client_id`, `plaid_secret` (Password), `plaid_env`
(sandbox / production), `direct_deposit_plaid_enabled`. The calls are made from the ERPNext server with the
standard library (no Plaid SDK): `/link/token/create` (products **auth**, **identity**; `hosted_link`),
`/link/token/get`, `/item/public_token/exchange`, `/auth/get`, `/identity/get`, `/item/remove`.

**Recommended: a separate Plaid use case on the same Plaid team, not Bank Bridge's.** Bank Bridge's Plaid
review is transactions-only for the farm's own accounts; verifying workers' personal accounts (consumers)
with Auth + Identity is a different use case Plaid approves separately, and Bank Bridge's API is not built
to broker it. The same team keys can be typed in here (that is the "reuse"); erpnext_mcp never calls Bank
Bridge.

## 5. Surfaces

Mobile routes (+5): `get_my_direct_deposit`, `submit_direct_deposit_change`, `start_bank_verification`,
`finish_bank_verification`, `upload_bank_form`. MCP tool +1: `list_direct_deposit_changes` (read, masked).
Desk: Employee Bank Account form buttons Approve / Reject / Prenote returned (`api.direct_deposit`).
Daily job `direct_deposit.activate_due`. Settings: `direct_deposit_self_service` (0),
`direct_deposit_plaid_enabled` (0), `plaid_client_id`, `plaid_secret`, `plaid_env`,
`direct_deposit_hold_days` (14). Prenote: `generate_prenote_file` also takes approved Pending accounts.

## 6. Decisions for Tim

1. Plaid: separate use case on the same team (recommended) vs. Bank Bridge's.
2. Hold: 14 days, or "until the next payroll after the prenote".
3. Name-match strictness: first + last name tokens (recommended) vs. last name only.
4. A photo of the bank form (no text) is `Needs review` — the manager compares; acceptable?
