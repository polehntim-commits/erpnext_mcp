# Starting work, hours and evidence — contract (frozen for v0.198.0)

Source: OML App Feedback **AFB-2026-00022**, *"For some reason initiating a task requires a
photo"*, plus what FT-2026-09-00004 (40-WM-SE, "Take pictures of the wind machine and document
hours") showed when it was read back:

- The task read back as `origin: compliance_rule` with no reporter. **The stored row is right**:
  `origin = field_reported`, `reported_by = HR-EMP-00001`, `reported_at` is set. The read path
  never selected those columns, so `_describe_task` printed its fallback instead.
- No hour-meter reading was recorded, so `40-WM-SE.current_hours` is still 0.
- Six photos came through with `captured_on` and `phase` empty, although their filenames say
  `before` and `after`, and `farm_location_gps` was null.
- App Feedback `submitted_at` is about 7 hours after `received_at`. The phone's UTC `…Z` stamp is
  stored as naive UTC, while `received_at` is site-local, so `queued_days` comes out negative.

The phone (fafo_ios) builds against this document. Every change is additive: a shipped
build keeps working, and nothing it sends today is refused.

## 1. Starting work never needs a photo

| call | before | now |
|---|---|---|
| `report_field_task` (MCP and `/mobile`) | `photo_file_token` required | **optional**. When sent, it must be a real File and goes to `Farm Task.report_photo` as before |
| `report_asset_issue` (MCP and `/mobile`) | `photo_file_token` required | **optional** |
| `create_farm_task`, `claim`, `start` | never required | unchanged |

A photo is asked for only by the **completion** evidence contract. A field report still raises
its task with `{"photos": true, "findings_text": true}`, plus `hours` (§3), so the photograph is
taken when the work is done.

## 2. A task reads back as it was stored

Every task payload (MCP `get_farm_task`, the task lists, and the `/mobile` task shape) carries:

```json
{"origin": "field_reported", "reported_by": "HR-EMP-00001", "reported_by_name": "Tim Polehn",
 "reported_at": "2026-09-27 16:54:14", "report_photo": "b12f99c64b"}
```

`reported_*` and `report_photo` are `null` when absent. `origin` is the stored value, and falls
back to `compliance_rule` only for a row that genuinely has none.

The asset-screen actions (`record_asset_stock_movement`, `attach_asset_document`) now store
`origin = field_reported`, `reported_by = the caller` and `reported_at = now`. They used to be
stored as `compliance_rule`.

## 3. Hour meters

**Which assets have one:** `engine_hours.hour_meter_types()` is the checked-out types (Tractor,
Vehicle) plus **Wind Machine, Pump, Irrigation Pump, Well Pump, Generator**.

**The evidence contract gains `hours`** (bool). `create_farm_task` accepts it.
`report_field_task` on an asset with an hour meter sets it:
`{"photos": true, "findings_text": true, "hours": true}`. At completion, a contract with `hours:
true`, on a task whose `asset` has an hour meter, is unmet without `hours_reading`. On an asset
with no meter the key is ignored.

**Task payload** gains `asset_hours`. It is `null` when the task has no asset.

```json
"asset_hours": {"asset": "40-WM-SE", "asset_type": "Wind Machine", "has_hour_meter": true,
                "current_hours": 0.0, "hours_updated_at": null}
```

**Completion** (`complete_farm_task`, `/mobile/complete_task_via_mobile`) gains
`hours_reading` (number) and `allow_meter_reset` (bool). The reading is filed through the
existing engine-hours path:
- an **Asset State Log** row with `action = "log_hours"`, from/to the asset's current state,
  `engine_hours`, `performed_by`, `performed_at`, the completion's GPS, and notes naming the
  task;
- then `current_hours` and `hours_updated_at` through `engine_hours.cache_reading`.

A non-number or a negative reading **refuses** the completion; nothing is written. A reading
below the last one on record, without `allow_meter_reset`, **does not** refuse. A queued
completion must never be stuck on a typo. The completion is filed and the reading is not.
The answer says so:

```json
"hours_reading": {"recorded": true, "asset": "40-WM-SE", "engine_hours": 1234.5,
                  "previous_reading": null, "state_log": "ASL-…", "reason": null}
```

`hours_reading` is `null` when none was sent.

## 4. Evidence metadata

Each `evidence_files` entry may also carry the following. Every key is optional, and an old
build sends none of them.

| key | stored as | notes |
|---|---|---|
| `phase` | `phase` | `before` / `after`. **Fallback:** the `_before_` / `_after_` token in the file name, which is how every shipped build already names its photos |
| `captured_at` | `captured_on` | ISO 8601. An offset or `Z` is converted to the **site's** time zone; a naive value is taken as site-local |
| `latitude`, `longitude` | new `Farm Task Evidence.gps_latitude` / `gps_longitude` (Float) | where that photo was taken |

`farm_location_gps` on the assignment is still the explicit value, else `latitude`/`longitude`.
**It now falls back** to the first photo carrying coordinates, preferring `after` photos, so a
completion with a fix on any photo has a location.

## 5. App Feedback time stamps

- `submitted_at` / `timestamp` is converted into the **site's** time zone, like `received_at`
  (`datetimes.as_site_datetime`). The phone keeps sending `…Z` and needs no change.
- Patch `erpnext_mcp.patches.normalize_app_feedback_timestamps`. A stored `timestamp` more than
  10 minutes *after* its own `received_at` cannot be true, because nothing arrives before it is
  sent. Such a row was stored in UTC and is converted to site-local. The patch is idempotent,
  and rows it cannot place are left alone.
- `queued_days` is then non-negative, apart from handset clock skew.

## 6. Surface

No new routes, tools or switches. Additive arguments: `hours_reading` and `allow_meter_reset` on
`complete_farm_task` / `/mobile/complete_task_via_mobile`, and `phase` / `captured_at` /
`latitude` / `longitude` per evidence entry. New columns: `Farm Task Evidence.gps_latitude` and
`gps_longitude`. Evidence contract key: `hours`. fafo_ios SERVER_CHANGES §46.
