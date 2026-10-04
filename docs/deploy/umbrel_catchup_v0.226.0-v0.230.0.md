**Deploy: umbrel.local catch-up to server v0.230.0 and app 0.37.0 (build 20)**

One pass that covers v0.226.0, v0.227.0, v0.228.0, v0.229.0, v0.229.1 and v0.230.0 (Mill Creek offline 1–8,
durable task photos, placeholder-phone repair). The per-release files in this folder have the detail; this
is the grouped run for a box that is behind. **umbrel.local only. Not OML, no Funnel changes.**

**1. Build (Tim)**

The image bakes erpnext_mcp from GitHub `main`, so push local main first (code is `2b2c099`,
`__version__ = "0.230.0"`; the later commit on main is design docs only), then build the fafo-erpnext image.
Archive and ship FarmOps 0.37.0 (build 20) from fafo_ios main `4aa1709`.

**2. Server, over SSH to umbrel.local**

```zsh
sudo docker exec -u frappe -w /home/frappe/frappe-bench fafo-erpnext_server_1 bench --site frontend migrate
sudo docker restart fafo-erpnext_server_1
sudo docker exec fafo-erpnext_server_1 grep __version__ /home/frappe/frappe-bench/apps/erpnext_mcp/erpnext_mcp/__init__.py
```

Expect `__version__ = "0.230.0"` from the last line. One migrate covers every release in this batch (all columns are additive).

**3. Check the migrate in the Desk**

- Asset Register and Housing Unit: collapsed **Made on a phone** section. Card Print Job: **Tag format**, **Location**.
- Farm Shift Crew Member and Training Session Attendee: **Phone and server times**. ERPNext MCP Settings:
  **Offline window (hours)** = 12.
- Housing Inspection and Detector Test: **Filed from a phone**.
- Manager Today screen: **Punch times to review** tile (empty until something is flagged).

**4. Settings and switches (defaults; change nothing unless testing)**

- `offline_punch_window_hours` = 12.
- On (read): `allow_list_tag_print_queue`, `allow_list_punch_reviews`.
- Off (write): `allow_print_tags_for_location`, `allow_resolve_punch_review`, `allow_repair_placeholder_phones`.

**5. Phone test (airplane mode where it says so)**

1. Settings → **Prepare for offline**: types, register, parcels, housing, employees, tags for scanning, badges,
   each with "Last synced".
2. Airplane mode → Add asset with photos → Add another → force-quit and reopen → back online → Sync now: records
   show Created offline with the phone time; the on-screen QR opens the record.
3. Airplane mode → Clock in crew → back online: joins at the tap time; past the window it is flagged on the tile.
4. Airplane mode → Housing walk with photos → File this inspection; Detector test → back online: both arrive,
   dated by the phone.
5. Airplane mode → scan an asset tag and a cabin QR: card opens "No signal — showing what this phone saved".
6. Airplane mode → End a shift (clock-out, sign); run a class (badges, signatures, Complete); Report a problem
   with a photo → back online: each sends once, in order; a resend answers `replayed`.
7. Airplane mode → complete a task with before/after photos: **Completed — photos pending**, each photo **Saved
   on phone** → back online: Uploading → Uploaded → Completed; the Desk has every photo once.
8. Settings → Waiting to sync: nothing stuck at Sending; Retry on any row not being sent.

**6. Optional: placeholder-phone repair (v0.229.1) on umbrel.local**

Tick **Repair Placeholder Phones**, call `repair_placeholder_phones` (dry run), review, call again with
`apply: true` only if the list is right, untick.

**7. Rollback**

All columns are additive; an older image ignores them. Phones on 0.37.0 against an older server keep their
writes waiting in Settings with Retry.
