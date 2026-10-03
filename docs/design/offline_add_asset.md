# Offline Add Asset (Mill Creek) — audit, design and contract

**Status: FROZEN (2026-10-03).** Tim's priority. Server **v0.226.0**, app **0.33.0**. Builds on what
exists; nothing new runs on the server until a phone sends it.

## 0. Audit of today's build (fafo_ios 0.32.0, read-only)

**Can it add assets offline at all? Partly — and it loses data.** `AssetRegistrationModel.submit`
(`fafo_ios/FarmOps/Features/Assets/AssetRegistrationModel.swift:420`) queues the registration when the
phone has no path, in `SyncManager`'s SwiftData `PendingOperation` queue (on disk; survives a kill),
with an optimistic `CachedAsset` row. But:

| # | Gap | Effect |
|---|---|---|
| 1 | The photos are not queued (`queueRegistration`, :492, sends no files) | Offline photos are never uploaded; their files are unreferenced and the launch-time orphan sweep deletes them |
| 2 | The form is in memory until Save | App killed mid-form = everything typed and the GPS fix lost |
| 3 | No QR before sync — the QR comes from `generate_asset_qr` and the queued confirmation hides it | Nothing can be tagged until the phone is back in range |
| 4 | The tag is the typed docname | A name already taken is refused at sync, and the tag would have to change |
| 5 | `client_id` is sent but the sidecar drops it (`register_asset` declares no such argument) | A resend after a lost reply is refused as "duplicate tag" and the queue marks a created asset as failed |
| 6 | The optimistic `CachedAsset` is keyed by the form's UUID but `markCachedAssetSynced` looks it up by the queue row's id | A synced asset stays "unsynced" in the cache forever |
| 7 | The parent check is a live call | Entering a parent offline blocks Save ("That parent tag isn't in the register") |
| 8 | Houses and cabins with bed capacity go through `CreateLocationSheet` (online only, no capacity) | Not possible offline |
| 9 | Uploads restart from chunk 0 on every attempt (`ChunkUploader.stage` mints a new `upload_id`) | A 12 MP photo on one bar never finishes |
| 10 | No list of what is waiting, no per-item status, no "Add another", no "Prepare for offline" | Batch entry is blind |

**Can it add several before syncing?** Mechanically yes (each Save queues one operation, sent oldest
first), but each one loses its photos (1), there is no list of them (10), and one refusal leaves a
failed row with no way to fix and resend it.

## 1. What changes

### 1.1 The tag is a UUID minted on the phone (requirement 3)

Each new asset gets `tag_uuid` (a v4 UUID) the moment the form opens. The QR encodes the scan URL with
that UUID — `https://<farm ops host>/farmops/api/scan/<uuid>` — drawn on the phone (CoreImage), so a tag
can be shown, shared, printed or scanned before sync. The server stores `tag_uuid` (unique) and sets
`qr_url` to `/scan/<uuid>` for offline-made assets; `/scan/<tag>`, `universal_scan` and `scan_asset`
resolve a UUID as well as a docname. The docname stays the human name ("Cabin 3"); if it has to change
at sync (taken), the tag does not.

### 1.2 Drafts and photos are on disk at once (requirement 2)

A **draft** (SwiftData `AssetDraft`) is written on every field change: type, name, description,
serial, model, values, parent/location (picked or typed), category, beds, occupied flags, GPS fix and
accuracy, photo file names, `tag_uuid`, `request_id`, device time. Photos are already files
(`EvidenceStore`); the draft references them, and the orphan sweep counts draft references as claims.
Reopening the screen after a kill restores the draft.

### 1.3 One queue operation per asset, then one per photo (requirement 5)

Save turns the draft into `PendingOperation`s: **create** (`register_asset` or `create_housing_unit`)
carrying `tag_uuid`, `request_id` (a UUID minted with the draft — the same on every resend),
`device_created_at` and `offline: 1`; then **one attach per photo**, each depending on its create.
Sync runs automatically on reconnect and from a **Sync now** button, oldest first; a failure marks that
asset and its photos and moves on to the next asset. Photo uploads use a **stable `upload_id`**
(`tag_uuid` + photo hash) and ask the server which chunks it already has (`get_staged_upload`), so an
interrupted upload resumes.

### 1.4 The server makes a resend safe (requirement 5)

`register_asset` / `create_housing_unit` (route and tool) accept `tag_uuid`, `request_id`,
`device_created_at`, `offline`, `review_note`, `confirm_new`, `link_to_existing`.

- **Same request, same result**: a `request_id` already recorded returns the record it created, with
  `replayed: true` — no second record, no refusal.
- A `tag_uuid` already on another record with a different `request_id` is refused (`tag_in_use`).
- **Logged**: `created_offline`, `device_created_at`, `created_device` on the record, one comment
  "Created offline on <device> at <device time>", one MCP Action Log row.

### 1.5 Status on the phone (requirement 6)

Each saved asset shows **Saved on phone**, **Syncing**, **Synced**, or **Needs attention** with the
reason. Needs attention: a refusal the server meant (`name_taken`, `unknown_type`, a validation), a
possible duplicate (§1.6), or a photo that would not upload after the retries. The asset opens back into
its form, can be fixed, and **Resend** sends it again with the same `request_id` and `tag_uuid`.

### 1.6 Duplicates go to a person (requirement 7)

Before creating, the server looks for an asset with the same **serial number**, or the same **name and
parent/location**. If it finds one and the request does not say `confirm_new`, it creates nothing and
answers `possible_duplicate` with the candidates. The phone shows them: **Keep as new** (resend with
`confirm_new: 1`) or **It's the same one** (`link_to_existing: <asset>` — the server adds this
`tag_uuid` to that asset's `tag_aliases`, so the printed tag scans to it, and the photos attach to it).
Never merged automatically.

### 1.7 Pickers offline (requirement 4)

Cached at the last sync: asset types, categories, parent assets and locations (the asset register,
parcels, fields, housing units), employees, templates. Each picker shows "Last synced <time>". A value
not in the cache can be **typed**; it is sent with `review_note` ("type typed offline: …", "parent
typed offline: …"). The server files a typed parent it cannot find as no parent and sets
`needs_review` + the note; an unknown type is `unknown_type` (Needs attention: pick a type).

### 1.8 Houses and cabins (requirement 1)

Type **House** or **Cabin** creates a **Housing Unit** (`unit_type`, `capacity` = beds, `parcel`, GPS,
occupied, people-work-here) through the same queue, with the same `tag_uuid` / `request_id` rules.

### 1.9 Batch entry (Tim's addition)

After Save: **Add another** keeps the location, parent, category and type and numbers the name
("Cabin 1" → "Cabin 2"); **Copy from previous** fills every field but the serial number, photos and
GPS. **On this phone** lists every saved asset with its status and "N waiting to sync".

### 1.10 Tag printing — optional, off by default

A **Print a tag** switch on the form, **off by default**. When on, a Card Print Job (job type Asset Tag)
is queued for the asset — through the phone queue when offline — with its **tag format**: Card (Evolis),
Outdoor label, or Sheet. Without a printer: the QR is shown on screen and can be shared as an image,
offline. Server: Card Print Job gains `tag_format` and `location_label`; the queue is listed by location
on the phone and the Desk; **Print all for this location** sends the Card jobs to the card printer queue
and turns the Sheet / Outdoor label jobs into one `generate_asset_qr_sheet` PDF; a printed tag is marked
printed and a second print is a logged reprint (`is_reprint`, reason).

### 1.11 Prepare for offline (requirement 9)

One button: refreshes every cache in §1.7, the tag-format settings and the scan host, and shows what was
downloaded and when.

## 2. Surfaces

Server: Asset Register + Housing Unit fields `tag_uuid` (unique), `tag_aliases`, `request_id`,
`created_offline`, `device_created_at`, `created_device`, `needs_review`, `review_note`; Card Print Job
`tag_format`, `location_label`. Routes: `register_asset` and `create_housing_unit` take the new
arguments; new `get_staged_upload` (which chunks arrived); `list_tag_print_queue`,
`print_tags_for_location`. Scan resolution by UUID and alias.

App: `AssetDraft` (SwiftData), dependent queue operations, resumable uploads, status badges, the
"On this phone" list, Add another / Copy from previous, offline QR, print switch, Prepare for offline,
Sync now.

## 3. Tests

Create offline → kill (a new container on the same store) → reopen → sync; the same request sent twice
(one record, `replayed`); a refusal fixed and resent; a photo upload interrupted and resumed; a
possible duplicate kept and linked; a UUID tag scanned before and after sync.
