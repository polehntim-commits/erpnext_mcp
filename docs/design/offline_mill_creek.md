# Mill Creek offline — the remaining workflows (contract)

**Status: FROZEN (2026-10-03). Tim approved items 1–8 in the ranked order.** Follows
`offline_add_asset.md` (v0.226.0 / app 0.33.0). Three releases; each ships with tests, a deploy file and
a status. Everything new is off or passive until a phone sends it.

| Release | Server | App | Items |
|---|---|---|---|
| A | v0.227.0 | 0.34.0 | 1 stuck uploads · 4 punch times · 7 queue drain + store fallback (+ the launch sweep race) |
| B | v0.228.0 | 0.35.0 | 2 housing walk + detector test, filed and queued · 3 scan answers offline |
| C | v0.229.0 | 0.36.0 | 5 end shift + training session queued · 6 report a problem queued · 8 Prepare for offline covers everything |

## Release A

### 1. Nothing sticks at "Sending"
A `PendingOperation` left `syncing` by a killed app is put back to `pending` when the queue opens (it is
resent with its own `client_request_id` / `client_reference`, which the server already treats as the same
request). Settings shows Retry on any row that is not actually being sent.

### 4. Punch times: the phone's tap is official, the server's receipt is kept beside it
- `clock_in_crew` and `check_in_training_day` take `tapped_at` (ISO 8601, the phone's clock at the tap).
  The crew clock's `add_worker_to_shift` / `clock_out_worker` already send `joined_at` / `left_at`.
- **The official time is the phone's**: `joined_at` / `left_at` on the crew row, `scanned_at` on the
  attendee row. The training check-in window is judged at the tapped time.
- **Stored beside it, never overwritten**: `joined_device_at` / `joined_received_at`,
  `left_device_at` / `left_received_at` (Farm Shift Crew Member); `device_scanned_at` / `received_at`
  (Training Session Attendee).
- **Flagged for a manager** (`punch_review`, with `punch_review_reason`) when the receipt is more than
  `offline_punch_window_hours` (setting, default 12) after the tap, or the tap is more than 5 minutes
  after the receipt (a phone clock ahead).
- **Resolving is explicit**: `resolve_punch_review` (Farm Manager / HR / System Manager; MCP tool off by
  default) records `punch_resolution` — *Phone time stands*, *Server time used*, or *Corrected* with a
  time — plus who and a note. Only *Server time used* or *Corrected* changes the official time, and the
  device and received times stay as they were. `list_punch_reviews` (phone, MCP, a manager tile) lists
  what is waiting.
- A punch with no `tapped_at` (an older app) is stamped as before (server time), with `received_at` set
  and no flag.

### 7. The second queue drains like the first
`SyncManager` drains on foreground and on a 60-second timer as well as on reconnect, launch and sign-in.
A store that will not open is no longer silent: the app says the queue is held in memory only and asks
for it to be reported. The launch-time evidence sweep no longer runs before the second queue has said
what it holds (its default answer is "unknown", which skips the sweep).

## Release B
### 2. Housing walk and detector test
`file_housing_inspection` and `file_detector_test` routes (the app has called both and got 404),
idempotent on `client_request_id`, with device time. The walk is a draft on disk (like `AssetDraft`);
photos are files; Submit queues it with its photos (staged at drain, resumable). A photo that will not
upload marks the item Needs attention instead of being dropped.
### 3. Scan offline
A scanned tag resolves from the phone's cache: assets, housing units, and per-record history and due tasks
saved by Prepare for offline and every online scan. Offline answers say "as of <time>".

## Release C
### 5. End shift and run a training session offline
End shift (with its signature file) and the class runner (attendees by badge, signatures, complete) are
queued with device times and request IDs; signatures are files, never only in memory.
### 6. Report a problem
`report_field_task` takes `client_request_id` (same request, same task); the photo is saved to disk at
capture and the report is queued.
### 8. Prepare for offline, complete
Adds employees (badge directory), task templates, inspection templates, the task list and the housing
list, each with "last synced".
