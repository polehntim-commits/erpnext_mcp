# Farm Ops over `/farmops/api` only — contract

Server `erpnext_mcp` **v0.216.0**, app **0.27.0 (9)**. Frozen before code. Closes security
review finding C1 (2026-10-02): the public `/erpnext` Funnel mount can be removed once this is
deployed and the phones have moved.

## 0. Audit result this contract rests on

- The app makes **no** direct ERPNext call. Every request is `<base>/farmops/api/...`
  (`mobile/*`, `files/*`, `tiles/*`, `mobile/enroll_device`). Files travel as bytes inside those
  answers. The only other host is `api.open-meteo.com`.
- `<base>` is the `url` of the login card, which is the `public_url` setting. On OML that is
  `https://<host>/erpnext`, so the phone calls `https://<host>/erpnext/farmops/api/...` — through
  the `/erpnext` Funnel mount and ERPNext's own nginx, which forwards `/farmops/` to the sidecar.
- On OML `https://<host>/farmops/api/health` is **not** published today (Tailscale 404).
- Asset tags encode `<public_url>/scan/<asset>`. No such page exists; the tag works because the
  in-app scanner sends the text to `universal_scan`, which unwraps it.

So no new data route is needed. What changes is the base URL, and how to move it safely.

## 1. Settings (ERPNext MCP Settings)

| field | type | default | meaning |
|---|---|---|---|
| `farmops_public_url` | Data | empty | The address phones use: `https://<host>` with **no path**. The sidecar is reached at `<this>/farmops/api`. |
| `legacy_erpnext_paths` (label "Allow legacy /erpnext paths for phones"; reported as `allow_legacy_erpnext_paths`) | Check | **1** | On: with `farmops_public_url` empty, cards and tags are still built on `public_url` as before. Off: nothing new is issued on a base that has a path; the tools refuse and say to fill in `farmops_public_url`. |

`farmops_public_url` with a path, a query or a non-https scheme is refused on save.

## 2. The phone's base URL — one rule on the server

`mobile base` = explicit `url` argument → `farmops_public_url` → `public_url` (only while
`allow_legacy_erpnext_paths` is on) → the site URL.

Used by `generate_mobile_login_qr`, `open_device_enrollment`, `create_mobile_user`,
`recover_mobile_access` (the `url` in the card / enrolment QR, `mobile_endpoint`, the grant's
`endpoint_url`). The MCP endpoint these tools also print stays on `public_url`.

**The QR payloads do not change shape** (`v` stays 1): only the value of `url`. A card issued
after `farmops_public_url` is set works on every app build ever shipped.

## 3. The phone's base URL — one rule in the app (0.27.0)

Given the credential's `url` **U**:

1. **root** = U with its path removed. If U has no path, root = U and there is nothing to decide.
2. `GET <root>/farmops/api/health` (5 s). An answer with `service == "farmops-api"` → the app
   uses **root**. That is the version check: a server that answers there can be used there.
3. Otherwise the app uses **U** exactly as today (legacy).
4. The choice is remembered per credential and used at launch without asking. While on legacy,
   root is tried again at most every 6 hours. While on root it is re-checked in the background at
   launch and on return to the foreground (at most every 15 minutes), and the app falls back to U
   only if root does not answer **and** U's health does — no signal never moves a phone.
5. The credential itself is never rewritten; signing out and scanning a new card starts clean.

`api_base_mode` is `"farmops"` when the base in use has no path, `"legacy"` otherwise.

## 4. Who has moved — `report_device_capabilities`, readiness

`report_device_capabilities` takes one more optional argument, **`api_base_mode`**, stored on the
caller's Mobile Device Enrollment row (new field `api_base_mode`). The app sends it after
sign-in, at launch, and when the mode changes.

**`get_server_status`** gains `erpnext_funnel`:

```json
{
  "farmops_public_url": "https://host",
  "public_url": "https://host/erpnext",
  "allow_legacy_erpnext_paths": true,
  "ready_to_close_erpnext_funnel": false,
  "reasons": ["Tim's Login card: app 0.26.0 has not reported using /farmops/api"],
  "devices": [{"user": "...", "device": "Login card", "app_version": "0.26.0",
               "api_base_mode": null, "last_seen_on": "...", "ready": false}],
  "ignored_idle_devices": 0
}
```

`ready_to_close_erpnext_funnel` is true when `farmops_public_url` is set **and** every Enrolled
device seen in the last 30 days reports `api_base_mode == "farmops"`. A device not seen for 30
days is counted in `ignored_idle_devices`, not as a blocker. No devices → true.

Nothing is refused on this basis. The flag informs; closing the Funnel is the operator's act.

## 5. `validate_public_endpoint`

With `probe_routes: true`:

- the routes are probed at `farmops_public_url` when it is set and no `url` was passed
  (before: `public_url`); 401 JSON is still the pass, and health and the scan page are probed too;
- a new `erpnext` block reports, for the same host, unauthenticated GETs of `/erpnext/login`,
  `/erpnext/api/method/ping` and `/erpnext/farmops/api/health` with their status, and
  `erpnext_public` (any of them answered other than the proxy's 404);
- `farmops_only`: `{routes_ok, erpnext_closed, ready_to_close_erpnext_funnel, verdict}` where
  `verdict` is one sentence: what to do next, or that the cutover is complete.

`public_url` carrying a path (`…/erpnext`) is accepted as the configured URL; the path rule still
applies to a `url` argument.

## 6. Route gates — `list_sidecar_routes`

Each row gains **`gate`**: the `require_*` checks the route's handler runs (in the wrapper or in
the module helper it calls), e.g. `["require_scope", "require_supervisor"]`, and **`gate_note`**
where a route is deliberately caller-scoped with no role check (the two chunked-upload routes:
enrolled caller, own upload session). The summary counts `mutating_ungated`.

A test fails if a mutating route has no gate and is not on that short list with a reason, or if
the list names a route that no longer needs it.

## 7. Asset tag links

- `GET /farmops/api/scan/<name>` — public, static. One small HTML page: "This is a Farm Ops tag.
  Open the Farm Ops app and scan it." It looks nothing up and shows nothing but the escaped code.
- New and re-saved assets get `qr_url = <farmops_public_url>/farmops/api/scan/<name>` when
  `farmops_public_url` is set; otherwise unchanged.
- **Printed tags keep working**: `universal_scan` unwraps anything containing `/scan/`, old or
  new, and never needed the page.
- App: `ScanLink` turns an opened URL of either shape into the scan code and runs the ordinary
  scan. Registering a URL scheme / Associated Domain is **not** in this release (see decisions).

## 8. Review fixes

- `generate_access_control_report`: no crash on a user with an empty role / doctype / name row.
- `record_backup_test.test_restore_by`: a user id, an email, or a full name ("Tim Polehn") is
  resolved to a User; anything else is kept in the test notes and the caller is recorded.
- `create_backup_record.location`: over 140 characters is shortened in the field and the full
  text is kept in the notes.

## Surfaces

No new tools (983) and no new mobile routes (167 named). One argument on
`report_device_capabilities`; one GET page; two settings; one device field.

## Decisions left for Tim

1. **No universal link in this release.** It needs `/.well-known/apple-app-site-association`
   published at the host root (one more public path) and the farm's hostname compiled into the
   app's entitlements. Tags work in-app without it.
2. **The readiness flag never blocks a phone.** Turning `allow_legacy_erpnext_paths` off only
   stops new cards/tags being issued on `/erpnext`.
3. **Idle devices (30 days) do not hold up readiness.**
