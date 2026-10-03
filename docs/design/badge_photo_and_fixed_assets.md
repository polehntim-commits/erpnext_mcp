# Badge photo as a task; fixed assets do not move by accident — contract

Server `erpnext_mcp` **v0.214.0**, app **0.26.0**. Frozen before code. Two additions to the
October batch.

---

# Part A — Badge photo capture

## A1. The template (data)

A seeded Farm Task Template **"Badge photo"** (`title_es` "Foto para el gafete"), task type
`Other`, evidence `{photos: true}`, dispatch `Either`. Its form:

| key | type | notes |
|---|---|---|
| `employee` | `link` → Employee | required. Pre-filled by whoever raises the task. |
| `photo` | `photo` | required, `min_count` 1, `max_count` 1, **`guide: "portrait_4x5"`**, **`camera: "front"`**, help: plain background, face the camera, no hat or sunglasses (EN/ES). |
| `consent` | `attestation` | required: "This is me / the person named, and the photo may be used on the farm ID card." |
| `request_card_print` | `check` | optional: "Ask for a new ID card with this photo." |

Like every seeded template it is created once and an edited one is never overwritten.

**`guide` and `camera` are two new optional field attributes on `photo`**, not a new kind:
`guide` ∈ {`portrait_4x5`}, `camera` ∈ {`front`, `back`}. A phone that does not know them draws
its ordinary photo control — that is the capability fallback, with nothing to negotiate — and
the server crops whatever arrives (A3). Any other value is a validation error on the template.

## A2. Raising it

**`request_badge_photo(employee?, assign_to?)`** — MCP tool (mutating, default OFF) and mobile
route. Raises one "Badge photo" task about the Employee (`subject_doctype` Employee), assigned to
that employee unless `assign_to` names somebody else, with `employee` pre-answered.

- One open badge-photo task per employee: a second request answers the open one with
  `already: true`.
- **Who:** anybody for themselves (the phone sends no `employee`); for somebody else, HR
  Manager / HR User / Farm Manager / Foreman / System Manager.
- **Also raised automatically** — never failing the thing that triggered it — when
  - a card print is requested for someone with no photo: the print answer carries
    `badge_photo_task` beside the existing "has no photo" warning (the card still queues);
  - `onboard_employee` finishes for someone with no photo.
  Both are governed by the Farm Feature Flag `badge_photo_auto_request` (default **on**).
- From the Employee record in Desk: a **Badge › Request badge photo** button (a Client Script
  row, seeded once).

## A3. Completing it — the handler

Completion handlers are a closed registry keyed by template name (`completion_handlers`);
"Badge photo" is the first entry. After the ordinary completion has filed its evidence:

1. the photo is read from the `photo` answer (a File), **cropped to 4:5 about its centre,
   resized to 600 × 750, re-encoded as JPEG with every EXIF tag — GPS included — dropped**;
2. saved as a **private** File attached to the Employee (`badge-photo-<employee>-<stamp>.jpg`);
3. `Employee.image` is set to it. The card renderer reads a private File by its row, so the
   next card carries it;
4. **the previous photo is kept**: its File is not deleted, and a comment on the Employee names
   the old and the new file and who changed it;
5. if `request_card_print` was ticked and the person completing may ask for cards, an ID card
   is requested (idempotent on the task's name). Otherwise the answer says why not.

The completion answer carries `badge_photo: {employee, image, previous_image, width, height,
card_print?}`. The handler never raises: a photo that cannot be processed is reported and the
task still closes, with `badge_photo.error`.

**Who may complete:** the employee the task is about, or one of the A2 roles. Anybody else is
refused before anything is written.

**`set_employee_photo(employee, file_url | file)`** — MCP tool (mutating, default OFF) for Desk
and MCP: the same five steps on a file already on the site. A2 roles.

## A4. Phone

The generic form renderer. A `photo` field with `guide: "portrait_4x5"` opens a camera with a
4:5 frame and a face oval, starting on the front camera, and crops to the frame on the phone.
**My Records → Badge photo** raises the caller's own task and opens it.

---

# Part B — Fixed assets

## B0. What happened

40-WM-SE (a Wind Machine) moved 180 m. `scan_asset` has always written the handset's GPS fix
onto the asset on every scan, and nothing recorded that it did. A wind machine does not move;
the phone that scanned its tag does.

## B1. `fixed_location`

`Farm Asset Type.fixed_location` (Check). Seeded **fixed**: Irrigation Valve, Irrigation Zone,
Water Source, Wind Machine, Fuel Tank, Gas Tank, Storage, Cold Storage, Block, Housing Unit,
Cabin, House (and the flag-only building types). **Mobile**: Tractor, Implement, Sprayer,
Vehicle, General. A patch sets the flag on the types a site already has; after that it is the
site's to change (`update_asset_type fixed_location: …`, or the checkbox). A type this app has
never heard of is mobile.

## B2. Scanning

- **Fixed asset: a scan never changes its position.** Not the first time either — an asset
  with no position is placed with Move (B3).
- **Mobile asset:** a scan still updates the position (that is how a tractor is found), and a
  change of more than 25 m now writes a history row.
- **Every scan** records where it was made: `Asset Register.last_scan_latitude` /
  `last_scan_longitude` beside `last_scan_at` / `last_scan_by`.
- The scan answer carries `fixed_location`, `position_updated` and `last_move`.

## B3. Moving — `move_asset`, `undo_asset_move`

**`move_asset(asset_name, gps_latitude, gps_longitude, reason, accuracy_m?, client_request_id?)`**

- **Foreman, Farm Manager, System Manager** — checked in the tool, so MCP and the phone share
  the gate. Entity scope applies.
- `reason` is required when the asset already has a position (always, fixed or mobile). A first
  placement needs none.
- Writes the position and one **Asset State Log** row: action `Moved`, `from_latitude`,
  `from_longitude`, `gps_latitude`, `gps_longitude` (the new position), `distance_m`, the reason
  in `notes`, who and when. State is unchanged (`from_state` = `to_state`).
- Answers `{asset, moved, distance_m, from, to, log, undo_until}`. The same coordinates again is
  `moved: false` — safe to send twice; `client_request_id` is kept on the row.

**`undo_asset_move(asset_name)`** — same roles. Puts the asset back where its **latest** move
took it from, if that move is under **24 hours** old and has not itself been undone; writes a
`Move undone` row. Otherwise refuses and says why.

`last_move` (on scan and detail answers): `{log, moved_at, moved_by, from, to, distance_m,
reason, can_undo, undo_until}` or null.

## B4. The server refuses everything else

The Asset Register controller refuses a change to `gps_latitude` / `gps_longitude` on a
**fixed** asset that already has a position unless it came through `move_asset`,
`undo_asset_move`, or `update_registered_asset` **with `reason`** — which then writes the same
`Moved` row. That covers the Desk form, every tool, and any future code path. Registering an
asset, and giving a position to one that has none, are not moves.

`list_asset_state_history` returns the new columns, so every position change — a move, an undo,
a mobile asset re-sighted by a scan, an MCP correction — is in the history. **Before this
release no position change was recorded at all.**

## B5. Phone

- "Update location from where I am" is replaced by **Move asset**, shown to Foreman / Farm
  Manager only. On a fixed asset it sits behind a lock: **hold to unlock**.
- The sheet takes the phone's averaged fix **or** a pin placed on the map, shows
  "Move 180 m?", requires a reason, and confirms.
- **Undo move** is offered for 24 hours on the asset's screen.
- No pin for a fixed asset is draggable anywhere; the only place a position is chosen is inside
  the unlocked Move sheet.
- An older server (no `move_asset`): the phone offers nothing that moves a fixed asset.

## Surfaces

MCP tools: `request_badge_photo`, `set_employee_photo`, `move_asset`, `undo_asset_move` (all
mutating, default OFF). 978 → **982** (477 read, 505 write). Mobile routes: `request_badge_photo`,
`move_asset`, `undo_asset_move`. 164 → **167**.

## Decisions left for Tim

1. **A fixed asset with no position is not placed by a scan** either — only by Move.
2. **Gas Tank and Fuel Tank are fixed** (bulk yard tanks). A portable one wants its own type.
3. **The badge photo is cropped about its centre** when the phone did not frame it (an older
   build, or a photo set from Desk). There is no face detection on the server.
4. **A photo is auto-requested** when a card is asked for with none on file. The flag turns it off.

---

## Addendum v0.216.1 — FT-2026-10-00001 (badge photo not set)

**What happened.** A foreman raised "Badge photo" straight from the template on the Work screen
(`origin: foreman_dispatch`): no subject, no pre-answered employee. App 0.26.0 completed it through
its generic screen — a "before" frame at pickup, an "after" frame and a signature — and sent **no
form answers** (`form_answers` empty on both the task and the assignment). The server accepted that,
because a form is only checked when answers arrive (older apps send none). The handler then had no
employee and no `photo` answer; its error went into a key on the completion answer that nothing
shows. Employee.image stayed empty.

**Now:**

- **Whose photo:** the task's subject, else the form's `employee`, else **the employee the task is
  assigned to**. Raising "Badge photo" from the template with an assignee now sets the subject and
  names the task "Badge photo — <name>".
- **Which photo:** the form's portrait field, else **the last photo filed** with the completion
  that is not a "before" frame. A signature is never a portrait.
- **No photograph at all** → the completion is refused before anything is written.
- **A failure is visible:** a comment on the task and on the Employee saying the photo was not set
  and why.
- **App 0.27.1:** when a task's form takes the photo (a `photo` field) and the contract does not
  also demand a signature, the generic before frame, the after photos and the signature pad are not
  shown — one portrait and the consent. If the form could not be read they stay, as the fallback.
- The contract stays `{"photos": true}`: Farm Task refuses an empty contract on purpose, and a photo
  taken on the form counts toward it.
- **Desk:** the Employee form's Connections gain **Farm Task** (by `assigned_to`, group "Farm Ops"),
  seeded once at migrate.
