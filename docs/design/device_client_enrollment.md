# Device & client enrollment — design and contract

**Status: FROZEN (2026-10-03).** Tim, overnight: "get it done … with code as well". Built in phases
on top of v0.217.0, **everything OFF by default**: deploying changes nothing until Tim turns it on,
and the legacy login cards, device api_key/api_secret pairs and the static X-MCP-Token keep working
until he turns them off after the cutover. The §14 decisions are taken at their recommended defaults
and can be changed before enabling.

**Switches that turn it on** (ERPNext MCP Settings; none named `allow_*`):

| Setting | Default | Turns on |
|---|---|---|
| `device_keys_enabled` | off | §3–§5, §7 on the phone side: pickup links, key-bound auth, the silent upgrade |
| `phone_approval_enabled` | off | §6.2: a new phone asks, a manager approves on their phone |
| `mcp_oauth_enabled` | off | §6.3: OAuth 2.1 for MCP clients |
| `legacy_device_secrets` | **on** | the old login cards and device secrets keep working |
| `legacy_static_mcp_token` | **on** | the static X-MCP-Token keeps working |

With `device_keys_enabled` off the new sidecar paths answer exactly as paths that do not exist.

Releases: server **v0.218.0 → v0.220.0**, app **0.28.0 → 0.29.0** (phases in §12). Renumbered on
2026-10-03: v0.217.0 became the security-status release.

Ground rules carried over: every phone is a crew phone with no Tailscale; the phone talks only to
`/farmops/api`; nothing under `/farmops` answers HTML; the app uses Apple's stack only (CryptoKit,
Security, LocalAuthentication); mutating MCP tools ship OFF; "avoid table sprawl".

---

## 0. Where we start (v0.216.0)

| Today | Weakness this batch removes |
|---|---|
| Office QR = `{type: farm_ops_enroll, url, token}`; 256-bit token, **SHA-256 at rest**, single use, **24 h** window (max 7 days). Phone POSTs it to `/mobile/enroll_device` and gets `api_key` + `api_secret`. | The phone then holds a **long-lived bearer secret** for months. Anything that copies the Keychain item or the header is that worker until somebody revokes it. |
| Login cards (`generate_mobile_login_qr`) print `api_key` + `api_secret` into the QR. | **A photo of the card is the account.** |
| Every request sends `X-FarmOps-Token: key:secret`. Revocation is checked on every call. | Replay of a captured request works for as long as the secret lives. |
| MCP: one static `X-MCP-Token` per site; runs as `mcp_system_user`. | One shared, non-expiring secret for every AI client; no per-client scope; no per-client revoke (review C2). |
| No biometric gate. | A borrowed unlocked phone is the worker. |

## 1. Goals and non-goals

**Goals.** No long-lived bearer secret on any phone or in any QR. Every credential is bound to a
key that never leaves the device's secure hardware. Face ID / Touch ID stands between a picked-up
phone and the account. New phones and AI clients are approved by a manager **on their phone**. One
inventory, one-tap revoke, a lost-device flow. Every failure audited and alertable.

**Non-goals (this batch).** Android implementation (the protocol is specified so it can follow
unchanged — §11). Passkeys for the Desk are evaluated, not built (§10). No change to what a role
may do — only to how a caller proves who it is.

---

## 2. Vocabulary

- **Pickup link** — a single-use URL `https://<host>/farmops/api/enroll/<nonce>`; what the QR encodes.
- **Unlock key** — P-256 key in the Secure Enclave, usable only after Face ID / Touch ID. Signs
  token requests. Never signs ordinary API calls.
- **Proof key** — a second P-256 key in the Secure Enclave, usable whenever the phone is unlocked,
  no biometric prompt. Signs every API request (a DPoP proof), so a stolen access token is useless
  without the phone.
- **Access token** — opaque, random, short-lived (default = the re-auth interval), bound to the
  proof key's thumbprint. Stored only as a hash on the server.
- **Access request** — a pending "please let this device / client in" shown as a short code + QR.
- **Approver** — a person allowed to approve that kind of request (§6).

---

## 3. One-time pickup links (item 1)

### 3.1 Issue

`issue_enrollment_link(user, device_name?, minutes?)` — MCP tool (mutating, default OFF) and mobile
route `/mobile/issue_enrollment_link` (HR Manager / HR User / Farm Manager / System Manager; same gate
as today's enrolment). Also the Desk button on the grant.

- Creates a **Pending** `Mobile Device Enrollment` row (the existing table) holding the SHA-256 of a
  fresh 256-bit nonce and `enrollment_expires_at = now + minutes`.
- `minutes`: default **`enrollment_link_minutes`** setting = **10**; allowed 2–60.
- A newer link for the same account supersedes an older Pending one (existing rule).
- Answers `{link, qr_png_base64, expires_at, device}`. **The nonce exists in plaintext only in this
  answer and in the QR.** It is never logged by this app (the audit row carries the device row name).
- The QR encodes the **URL only** — no JSON, no user, no key.

### 3.2 Pick up

- `GET /farmops/api/enroll/<anything>` — the same `text/plain` page for every value, valid or not:
  "Open the Farm Ops app and scan this code." **A GET never consumes and never reveals validity**
  (a phone's Camera app or a link preview must not burn the link).
- `POST /farmops/api/enroll/<nonce>` — the app, body:
  `{device_name, platform, os_version, app_version, unlock_public_key, proof_public_key,
  key_protection, unlock_signature, attestation?}` plus a DPoP proof (§4.3) signed by the proof key.
  `unlock_signature` = ES256 by the unlock key over `"farmops-enroll|" + nonce + "|" + proof_jkt`
  (so pickup requires Face ID and proves both keys are on the same device).
- Server: look up `sha256(nonce)`; row must be Pending and unexpired; **consume atomically**
  (row lock; status Pending→Enrolled in the same transaction that stores the keys). Verify both
  signatures. Store the two public keys (JWK), `key_thumbprint` (RFC 7638 of the proof key),
  `key_protection`, platform, versions, `approval_method = "pickup_link"`.
- Answers `{device, user, base_url, access_token, expires_in, server_time, reauth_seconds}`.
  **No secret is ever returned.**
- **Every failure** — unknown, expired, used, superseded, bad signature, malformed — is the same
  `404` with the same body. Counted against the address in the v0.216 per-address failure meter
  (H3/H4); the alert fires at 10/min.
- Audited: `mobile:enroll_pickup` with device row, IP, outcome (never the nonce).

### 3.3 What happens to the old ways

`generate_mobile_login_qr` (secret in the QR) and `open_device_enrollment` (JSON token QR) keep
working while the setting **`legacy_device_secrets`** is on (default **on** until Phase 4), so the
phones in the field today are not stranded. With it off, both tools refuse and name
`issue_enrollment_link`, and `/mobile/enroll_device` answers the uniform 404.

---

## 4. Device-bound keys and request signing (item 2)

### 4.1 Keys on the iPhone

Both keys: `SecureEnclave.P256.Signing.PrivateKey` (CryptoKit), stored as the Secure Enclave
key's `dataRepresentation` in the Keychain, `kSecAttrAccessibleWhenUnlockedThisDeviceOnly`
(never in a backup, never on another device).

| Key | Access control | Used for |
|---|---|---|
| Unlock key | `[.privateKeyUsage, .biometryCurrentSet]` | pickup; minting access tokens; approving (§6) |
| Proof key | `[.privateKeyUsage]` | a DPoP proof on every request |

`.biometryCurrentSet`: adding or removing a face/finger **invalidates the unlock key**. The phone
then needs a new pickup link or an approval (§6.2). This is the point of the flag — see decision 2.

### 4.2 Minting an access token

1. `POST /farmops/api/auth/challenge` `{device}` + DPoP proof → `{challenge, server_time,
   expires_in: 60}`. Challenge: 256-bit, single use, hashed in cache, 60 s.
2. App prompts Face ID (the unlock key's own prompt — "Unlock Farm Ops"), signs
   `"farmops-token|" + challenge + "|" + device + "|" + proof_jkt` with the unlock key.
3. `POST /farmops/api/auth/token` `{device, challenge, signature}` + DPoP proof →
   `{access_token, expires_in, server_time}`.

Token lifetime = **`device_reauth_minutes`** setting, default **480** (a shift), allowed 5–1440.
That is the configurable re-auth interval: Face ID once per token. (iOS caps Keychain biometric
reuse at 5 minutes, so the interval is enforced by the token, not by `LAContext` reuse.)

### 4.3 Every request

Headers:

```
Authorization: FarmOps <access_token>
DPoP: <compact JWS, ES256, header {typ:"dpop+jwt", alg:"ES256", jwk:<proof public key>}>
```

DPoP claims (RFC 9449, plus one): `htm` (method), `htu` (URL without query), `iat`, `jti`
(128-bit random), `ath` (base64url SHA-256 of the access token), **`bh`** (base64url SHA-256 of
the body; `""` for an empty body).

Server, in this order, every call, no cache of the outcome:

1. token hash found, not expired, not revoked;
2. JWS verifies with the `jwk` in the header, and `thumbprint(jwk) == token.jkt` (binding);
3. `htm`/`htu`/`ath`/`bh` match the request;
4. `|iat − server_now| ≤ 120 s` (skew, §8.3);
5. `jti` not seen in the last 300 s (replay cache — redis, per-process fallback);
6. device row Enrolled, grant Active, user enabled (the v0.216 revocation chain).

Any failure → the uniform 401. One exception, below.

**Revoked-device wipe.** If 2–5 pass (the caller provably holds the device's proof key) but the
device row is Revoked, the 401 carries `{"device_revoked": true}`. Only the real device can earn
that answer; the app then deletes its keys, token and local cache.

### 4.4 Background work

Uploads and the offline queue run on the access token + proof key — no Face ID needed in the
background. When the token has expired, queued work waits until the worker opens the app and
passes Face ID. Nothing is lost; the queue already survives this.

### 4.5 Existing phones (no re-scan)

App 0.28 on a phone holding a legacy `api_key`/`api_secret`: on first launch it creates both keys,
prompts Face ID once, and calls `POST /mobile/upgrade_device_key` (authenticated the old way) with
the two public keys and signatures by both keys over `farmops-upgrade|<the api_key it holds>|<proof thumbprint>`. The server binds the keys to **that same device row**,
**destroys the stored secret**, sets `approval_method = "upgraded"`, and answers `{upgraded, device,
next}` — no token: the mobile transport strips every token-shaped key from its answers, on purpose.
The phone then signs in through `/auth/challenge` and `/auth/token` inside the same Face ID
context, and deletes the secret from its Keychain. The worker sees one Face ID prompt.

---

## 5. Biometric unlock (item 3)

- Face ID / Touch ID is asked: at pickup, when an access token is minted (launch after expiry,
  re-auth interval), and before any approval or revoke (§6, §7).
- `NSFaceIDUsageDescription` (EN/ES): "Farm Ops uses Face ID to confirm it is you before it signs in
  or approves a device."
- No passcode fallback on the unlock key (Tim's `biometryCurrentSet`). A worker whose Face ID fails
  is re-enrolled by a manager (§6.2). See decision 2.
- A phone with no biometrics enrolled cannot enrol; the app says so and how to fix it.

---

## 6. Approve-from-phone (item 4)

### 6.1 Who may approve what

| Request kind | Approver roles (any one) |
|---|---|
| A new phone for a worker | HR Manager, HR User, Farm Manager, System Manager |
| A new phone for an HR / Farm Manager / System Manager account | System Manager, or another holder of the same role |
| An MCP client | System Manager (and Farm Manager for read-only scopes) |

Checked in the tool, so the phone, the Desk and MCP share the gate. An approver cannot approve a
request for their own account.

### 6.2 New phone, no office QR

1. New phone: "I'm new / I have a new phone". App creates both keys, calls
   `POST /farmops/api/access/request` `{kind:"device", device_name, platform, app_version,
   unlock_public_key, proof_public_key, unlock_signature}` + DPoP proof. Open route; rate limit
   **5 an hour per address**; at most **50 open** site-wide; each expires in **15 min**.
2. Answer: `{request, code: "7KQ4-M2XD", expires_at}`. The code is 8 characters from a 31-character
   alphabet (no 0/O/1/I/L), shown big and as a QR `farmops-approve:<code>`.
3. Manager's Farm Ops: **Approve a device** → scan or type the code → sees device name, platform,
   app version, when, and the address it came from → **chooses the person** (employee picker) →
   Face ID → `POST /mobile/approve_access_request {code, user, decision}`, signed with the
   manager's **unlock key** over `"farmops-approve|" + request + "|" + user + "|" + decision`.
   A wrong code is counted (5 a minute per approver).
4. New phone polls `POST /farmops/api/access/status {request}` with a DPoP proof from the
   **requesting** proof key (so only that phone can collect). Approved → `{device, user,
   access_token, …}` exactly as pickup. Denied/expired → uniform 404.

The approval is recorded on the device row: `approved_by`, `approved_at`,
`approval_method = "phone_approval"`, the approver's device.

### 6.3 MCP clients — OAuth 2.1

Per the MCP authorization spec (2025-11-25) and what Claude clients support (Claude Code and
claude.ai connectors: authorization code + PKCE, discovered via RFC 9728; client registration by
**Client ID Metadata Document** or **Dynamic Client Registration**).

| Endpoint (on the ERPNext site, i.e. tailnet after cutover) | Purpose |
|---|---|
| `/.well-known/oauth-protected-resource` | RFC 9728: resource = the MCP endpoint; names the AS |
| `/.well-known/oauth-authorization-server` | RFC 8414 metadata: `S256` only, `authorization_code` + `refresh_token`, CIMD and DCR advertised |
| `/api/method/erpnext_mcp.oauth.register` | RFC 7591 DCR — registers as **Pending**; nothing works until approved |
| `/api/method/erpnext_mcp.oauth.authorize` | the consent page (below) |
| `/api/method/erpnext_mcp.oauth.token` | code→tokens; refresh with rotation |
| `/api/method/erpnext_mcp.oauth.revoke` | RFC 7009 |

**Consent without a password in the browser.** The authorize page (HTML — it is on the ERPNext
site, not `/farmops`) shows the client's name, the scopes it asked for, and a short code + QR. A
manager approves on their phone exactly as in §6.2 (kind `mcp_client`), choosing the scope profile
(§6.4) — or a System Manager logged into the Desk approves there. The page polls, then redirects
with the code. Auth codes: single use, 60 s, PKCE `S256` required, `redirect_uri` exact match,
`resource` (RFC 8707) must be this MCP endpoint.

**Tokens.** Access token **60 min** (`mcp_access_token_minutes`). Refresh token **30 days idle,
90 days absolute** (`mcp_refresh_days`), **rotated on every use**; presenting a used refresh token
**revokes the whole family** and raises a security alert (theft signal). All tokens opaque,
hashed at rest.

**Who the client acts as (review C2).** Every approved client runs as a dedicated
**`mcp-agent`** service user (role profile "MCP Agent": no Administrator, no System Manager), with
`sub` = the approving manager recorded on every audit row. The effective tool set =
**granted scopes ∩ the site's `allow_<tool>` switches** — the switches remain the outer limit.

**Legacy.** The static `X-MCP-Token` keeps working while **`legacy_static_mcp_token`** is on
(default on until Phase 4). `get_server_status` reports `ready_to_disable_static_mcp_token`
(no static-token call in 14 days).

Device authorization grant (RFC 8628) is **not** built: no Claude client uses it. It can be added
later for a headless client without changing anything here.

### 6.4 Scopes

| Scope | Grants |
|---|---|
| `mcp:read` | every read tool the site has switched on |
| `mcp:write:<domain>` | mutating tools in one console domain (`tool_groups.DOMAINS`: farm, workforce, compliance, accounting, commerce, holding, platform) |
| `mcp:tool:<name>` | one named tool |

Profiles offered to the approver: **Read only**, **Read + farm**, **Custom**. A client
can never receive a scope it did not ask for.

---

## 7. Inventory, revoke, lost device (item 5)

- **`list_access_inventory`** (read tool; mobile route for the approver roles; Desk report):
  every phone and MCP client — who, kind, name, platform, app version, key protection, approved by
  / method, enrolled/approved at, last seen + last address, scopes (clients), status, and
  `legacy_secret: true` for phones not yet upgraded.
- **Revoke**: `revoke_mobile_device` (exists) and new `revoke_mcp_client` — tools, mobile routes
  (one tap + Face ID) and Desk buttons. Takes effect on the next call (revocation is read every call).
  Revoking a client revokes all its tokens; revoking a phone revokes its tokens and keys.
- **Lost device** (`report_lost_device(user, device?)`): revokes the device (or all of a user's
  devices), records "lost" as the reason, raises the alert, and lists what that device could reach.
  The worker gets a new phone in through §3 or §6.2. If the lost phone ever calls again with its
  real key it is told `device_revoked` and wipes itself (§4.3).

---

## 8. Security details (item 6)

### 8.1 Secrets at rest and in URLs

| Thing | At rest | In a URL |
|---|---|---|
| Pickup nonce | SHA-256 | **yes, the only one** — single use, ≤ 60 min, never logged by this app |
| Challenge, access token, refresh token, auth code, approval code | SHA-256 | never |
| Device keys | public half only | never |
| OAuth client secret | none — all clients are public (PKCE) | — |

Gunicorn's access log records request paths, including a pickup nonce. Acceptable because the
nonce is dead on first use or after ten minutes; Phase 1 also changes the sidecar access-log format
to drop the last path segment under `/enroll/`.

### 8.2 Replay

DPoP `jti` cache (300 s); challenges and auth codes single use; refresh-token rotation with family
revocation; pickup atomic consume.

### 8.3 Clock skew

The server's time is in every token/challenge answer (`server_time`) and in the `Date` header. The
app keeps `offset = server_time − local_time` and stamps `iat` with corrected time. A proof outside
±120 s gets the uniform 401 **with `FarmOps-Server-Time`** (the only extra the anonymous answer
carries — the time is not a secret); the app corrects and retries once.

### 8.4 Alerts

All go to the **Security Alert Recipients** setting (`security_alert_email`; empty → enabled
System Managers) and the `farmops_auth_alert` hook, and are one audit row each:

| Event | Severity |
|---|---|
| Refresh-token reuse (family revoked) | critical |
| Device reported lost; any revoke | warning |
| New phone enrolled / approved; MCP client approved | info (digest) |
| 10 failed sign-ins / pickups / proofs from one address in a minute | warning |
| 5 wrong approval codes from one approver in a minute | warning |

`security_alert_email` ships in Phase 0 (v0.216.x) if it has not shipped before this batch — it
also fixes the v0.216 sign-in alert, which emails nobody when the drift field is empty.

### 8.5 What is still uniform

Pickup failures: one 404. Signature/token failures: one 401. Access-request failures: one 404.
The only differences are `device_revoked` (earned by holding the key) and `FarmOps-Server-Time`.

---

## 9. Data model — two new doctypes, one extended

- **Mobile Device Enrollment** (existing child of Mobile Access Grant) + `unlock_public_key`,
  `proof_public_key` (JWK, Small Text), `key_thumbprint` (Data, indexed), `key_protection`
  (secure_enclave / strongbox / tee / software), `platform`, `os_version`, `approval_method`,
  `approved_by`, `approved_at`, `last_ip`, `revocation_kind` (revoked / lost). The existing
  `enrollment_token` (hash) and `enrollment_expires_at` carry the pickup nonce.
- **Access Request** (new): kind (device / mcp_client), status (Pending / Approved / Denied /
  Expired), code hash, display name, platform/app version, public keys (device), client (link),
  requested scopes, granted scopes, subject user, approved by/at, address, expires_at.
- **Access Token** (new): token hash (unique), kind (access / refresh / auth_code / challenge),
  holder (device row or MCP client), jkt, scopes, family, issued/expires/used/revoked at.
- **MCP OAuth clients** are rows of **Access Request** with kind `mcp_client` once approved, or —
  if Tim prefers — Frappe's own **OAuth Client** doctype (decision 5).

Settings (ERPNext MCP Settings; no `allow_` prefix): `enrollment_link_minutes` (10),
`device_reauth_minutes` (480), `legacy_device_secrets` (on), `legacy_static_mcp_token` (on),
`mcp_access_token_minutes` (60), `mcp_refresh_days` (30), `security_alert_email`.

## 10. Passkeys for the Desk — evaluation only

Frappe v15 core does not ship passkeys; a WebAuthn PR is open upstream. Third-party apps exist
(e.g. `frappe-passkeys`, which states support for Frappe ≥ 15.108 — OML runs **15.113.4**).
Passkeys created on an iPhone or Mac are stored in Apple Passwords / iCloud Keychain.
Deliverable in Phase 4: a one-page memo (adopt the app, wait for upstream, or none), with a test on
umbrel.local. Nothing is installed without Tim's approval. The Desk is tailnet-only after cutover,
which lowers the urgency.

## 11. Android parity (specified, not built)

Same wire protocol. Keys: Android Keystore, StrongBox when present (`setIsStrongBoxBacked`), else
TEE; unlock key `setUserAuthenticationRequired(true)`, `setUserAuthenticationParameters(0,
AUTH_BIOMETRIC_STRONG)`, `setInvalidatedByBiometricEnrollment(true)` (= `biometryCurrentSet`);
proof key no user authentication. `key_protection` reports `strongbox` / `tee`. Optional key
attestation chain in `attestation`.

## 12. Phases

| Phase | Server | App | Content |
|---|---|---|---|
| 0 | v0.216.x | — | `security_alert_email` (if not already shipped) |
| 1 | **v0.218.0** | **0.28.0** | pickup links; Secure Enclave keys; DPoP; access tokens; Face ID; silent upgrade of existing phones; inventory; revoke; lost device; `device_revoked` wipe |
| 2 | **v0.219.0** | **0.29.0** | approve-from-phone for new phones (§6.2) |
| 3 | **v0.220.0** | 0.29.x | MCP OAuth 2.1 + phone/Desk consent; `mcp-agent` user; scopes; refresh rotation; static token behind setting |
| 4 | settings only | — | turn off `legacy_device_secrets` when inventory shows no `legacy_secret` phone; turn off `legacy_static_mcp_token` when the flag says ready; passkey memo |

Each phase: contract section frozen (this doc), tests on both sides, deploy file, verification on
cellular with Tailscale off for every phone step.

## 13. Surfaces (estimate)

MCP tools: `issue_enrollment_link`, `list_access_inventory`, `approve_access_request`,
`deny_access_request`, `revoke_mcp_client`, `report_lost_device` (+6; read: 1, write: 5, all write
default OFF). Mobile routes: `issue_enrollment_link`, `upgrade_device_key`,
`list_pending_access_requests`, `approve_access_request`, `deny_access_request`,
`list_access_inventory`, `revoke_access`, `report_lost_device` (+8). Open sidecar paths:
`/enroll/<nonce>`, `/auth/challenge`, `/auth/token`, `/access/request`, `/access/status` (+5, all
uniform-failure, metered).

## 14. Decisions for Tim

1. **Re-auth interval default: 8 hours** (Face ID once a shift). Shorter is safer and nags more.
2. **`biometryCurrentSet` with no passcode fallback.** A worker who adds a new face/finger, or
   whose Face ID stops working (gloves, mask), must be re-enrolled by a manager. Alternative:
   `biometryCurrentSet` **or** device passcode.
3. **Two keys per phone** (unlock + proof) so background sync works without Face ID. One key would
   mean a Face ID prompt for every request or no background sync.
4. **Who approves whom** — table §6.1, in particular that managers' phones need a System Manager.
5. **Own small OAuth server inside erpnext_mcp vs Frappe's built-in OAuth.** Recommended: our own —
   consent must be approve-on-phone, scopes map to tool switches, and tokens must be hashed at
   rest. Before Phase 3 I will check on umbrel.local whether Frappe 15.113's OAuth (DCR, metadata,
   PKCE) stores tokens hashed; if it does, reusing its OAuth Client table saves one piece.
6. **`.well-known` on the tailnet origin.** The MCP endpoint's discovery documents must be served
   from the origin root (`https://<host>:8443/.well-known/...`). That is a `tailscale serve` path on
   the office port (tailnet only, never Funnel) plus a Frappe route — to be proved on umbrel.local.
7. **App Attest** (Apple's proof the request comes from the genuine Farm Ops app). Optional; adds
   protection against a re-built client. Recommended for Phase 1 as "recorded, not required".
8. **Phones with a legacy secret after Phase 1**: upgraded silently on first launch of 0.28. Phase
   4 turns the old path off; a phone never opened by then needs a new pickup link.
9. **Passkeys for the Desk** — memo only in this batch.
