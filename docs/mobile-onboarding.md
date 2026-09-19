# Mobile onboarding — `/app/mobile-onboarding`

Enrolling a worker's phone, from a page somebody can stand at with them.

Everything this page does, `erpnext_mcp/tools/mobile.py` has done since v0.17.0.
`create_mobile_user` makes the account, assigns the role, writes a User
Permission per entity and mints the credential; `generate_mobile_login_qr` draws
the card the phone scans; `list_mobile_users` reports who is enrolled and what
is wrong with any of it. What was missing was **a place for a person to do it
from** — the only two surfaces were an MCP client and `bench console`, and
enrolment happens on the morning of somebody's first day with the worker
standing at the desk holding their phone.

The page is the four whitelisted methods in `erpnext_mcp/mobile_onboarding.py`
and a Frappe Page in `erpnext_mcp/erpnext_mcp/page/mobile_onboarding/`. It adds
no rule of its own to the two tools it wraps, with one exception, described
under *Permissions* below.

---

## Getting there

`/app/mobile-onboarding`, or type **Mobile Onboarding** in the awesomebar.

The Page record ships with the app and appears after `bench migrate`. It is
**not** added to any Workspace or sidebar group: this app installs no Workspace
at all, and adding one would rearrange a Desk the operator already had — the
same promise `hooks.py` opens with. Pin the route as a bookmark, or add it to
your own Workspace, and it stays yours.

---

## Before it will enrol anybody

Two things have to be true of the **site**, and the page checks both at load
time rather than at submit time — the banner across the top says which one is
missing and what to do about it, and the form is disabled behind it.

| what | why | where to fix it |
|---|---|---|
| a QR encoder on the bench | there is nothing to draw the card with | `./env/bin/pip install segno` and restart |
| **Public URL** is set and is `https://` | the card points the phone at this address, and it carries a live credential | ERPNext MCP Settings → Public URL |

**Why before and not after.** Run in the obvious order — make the account, then
draw the card — either failure leaves a worker **half enrolled**: the login
exists, the credential exists, and the person at the desk has nothing to scan.
Recovering from that means finding a different screen, which is the thing this
page exists to spare somebody. So both questions are asked first and the refusal
says *No account was created.*

`get_tailscale_funnel_config` reports what the machine is actually serving, if
Public URL and reality have drifted apart.

---

## Enrolling somebody

1. **Full name** — as it should read on a dispatch board and on an evidence
   record.
2. **Email** — this *is* the account name.
3. **Role** — one of this app's seven mobile roles. The note under the picker is
   the role's own summary and its `cannot` list, straight from `roles.py`. A job
   title (Checker, Tractor Driver) is a **Designation** on the Employee and not
   a role; `list_mobile_users` returns the mapping between the two.
4. **Entities** — tick at least one. This is the point of the whole form. In
   Frappe a user with **no** User Permission on Company sees **every** company on
   the site, so an unscoped account would be the least scoped one here, not the
   most. There is no override; the tool refuses.
5. **Card valid for (hours)** — how long the printed card may be used to *enrol*
   with. Default 24, ceiling 168. It is not how long the credential lasts: once
   the phone has scanned it, the token works until it is revoked.
6. **Create & Generate QR.**

The card appears beside the form. **Print card** opens it in a tab sized for one
sheet; hand it over, watch them scan it, then destroy it.

### The card is a live credential

Anybody who photographs it has that account until the token is replaced. That is
inherent to enrolment-by-QR and the mitigation is time — the card stops being
valid to enrol with at the printed expiry, and issuing a new one replaces the
secret so an old photograph stops working. The card says all of this in print,
on the card, where the person handling it will read it.

The **secret is never in the page's JSON**. The response carries a PNG; the
credential is inside the symbol, and the only thing that reads it is the phone's
camera.

---

## v0.175.0 — the QR that carries no credential

The Employee form now has **Actions › Onboard Worker**. It shows a QR holding
the server address and a **single-use enrolment token** — no key, no secret —
with a countdown to the end of the window (24 hours by default). The worker's
phone posts the token to `POST /farmops/api/mobile/enroll_device` and receives
its own credential, once. The dialog notices the phone arrive and takes the QR
off the screen; nothing is saved as a file.

Every phone now has its own row (**Mobile Device Enrollment**) under the
worker's Mobile Access Grant, so a lost phone is revoked on its own —
`revoke_mobile_device`, or the idle sweep — and the worker's other phone keeps
working. Phones enrolled before this release were moved onto rows by a patch and
need no re-scan.

This page still prints the older **login card**, which does carry a credential,
because Farm Ops builds without the enrolment exchange can only read that card.
Once the phones in the field understand `farm_ops_enroll`, Onboard Worker is the
only enrolment surface anybody needs.

## Re-enrolment

**Regenerate QR** on any row of the roster. The ordinary reasons are a replaced
phone, a lost phone, a token the idle sweep took away, and a card that expired
on a desk before anybody scanned it.

It **rotates the credential**, which is what the confirmation warns about: a
phone already enrolled on that account stops working until it scans the new
card. That is the right default — somebody asking for a second card usually
cannot account for the first one — and `generate_mobile_login_qr` still takes
`rotate_token=false` for the case where you only want to re-print a card for a
phone that is still working.

**A new account's first card does not rotate anything.** `create_mobile_user`
mints the credential; the card prints *that* one. Minting a second a millisecond
later would hand over a secret that was already dead on paper.

---

## The roster

Every mobile account, with the drift checks `list_mobile_users` performs. A row
with a ⚠ under it is one of:

- **no User Permission on Company** — that account currently sees every entity.
- **the grant and the live scoping disagree** — somebody changed one without the
  other.
- **revoked, but the token still answers**, or **revoked, but the login is still
  enabled**.
- **the token's review date has passed** and it is still live. Frappe API secrets
  do not expire on their own; this is a reminder, not an enforcement.
- **the grant names a role the account does not hold.**

**Last activity** is `last_seen_on`, stamped by `api/guard` at most once a day.
It is the column `sweep_idle_grants` acts on, so it is also the answer to "why
did that phone stop working".

---

## Permissions

The page holds **no role list of its own**, and that is deliberate. The gate is
Frappe's own permission tables, so an operator can see it and change it without
a release:

| action | needs |
|---|---|
| read the roster | read on `Mobile Access Grant` |
| enrol / regenerate | create on `Mobile Access Grant` **and** create on `User` |

`Mobile Access Grant` ships **System Manager only**. To let another role enrol —
an office manager, say — add it to that doctype's permission table in the Desk
and give it User creation as well.

**The `User` half is what makes widening the other half safe.** However far the
grant's permission table is opened, this page can still only produce an account
the caller could have produced by hand on the User form. It is not a route
around Frappe's own user administration.

**The Page record names no role.** A standard Page is rewritten from the app's
JSON at every `bench migrate`, so a role list stored there is a decision an
operator makes and then silently loses. Somebody without permission opens the
page and reads a sentence saying which permission is missing; the methods refuse
either way. Both phone-only roles ship with Desk access off, so they never reach
`/app` at all.

---

## What it is not

- **Not a second implementation.** Every refusal on this page is one of
  `tools/mobile.py`'s, unchanged: the role catalogue, the mandatory entity
  scoping, the HTTPS-only endpoint, the one-week ceiling on a card, the "this
  user already exists, say so on purpose" rule. The only check this module adds
  is the session gate above, because an MCP tool has no session to ask about.
- **Not a way to revoke somebody.** `revoke_mobile_user` ends an account and
  records who did it and why. Ending somebody's access is not a button beside a
  list.
- **Not a hire.** `onboard_worker` is the hire — the Employee, the I-9, the W-4,
  the badge. This is the phone.
