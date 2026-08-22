# farm_app retirement — cutover checklist

**What this is.** The runbook for the day `fafo-farm-app` is switched off. Work
top to bottom; every step says how to tell whether it worked and what to do when
it did not.

**Read `farm_app_route_mapping.md` first.** It carries the evidence behind the
single most important claim here: **iOS needs no URL change.** Its mobile API
already points at ERPNext.

> **None of this was executed from the development machine, and it could not
> be.** This repo has no bench and cannot bind a port; the ERPNext site is the
> deployed Umbrel container, reachable as an MCP endpoint but not over SSH from
> here. Every command below is written to be run **on the Umbrel host**, and the
> verification steps are written so the person running them can tell success
> from failure without trusting this document.

---

## 0. Before the day

- [ ] **Pick a low-traffic window.** Nothing here is instant: the ERPNext
      healthcheck has a 180 s `start_period`, so allow an hour.
- [ ] **Name a rollback decider.** One person decides whether §4 is triggered.
- [ ] **Confirm the bake period** — 30 days is the assumption in §5. The
      `fafo-farm-app` data volume is not touched until that ends.
- [ ] **Settle the two open decisions** flagged in the mapping document:
      - the **H-2A hiring-cohort** gap (`/hiring-cohorts/*`) — no Frappe HR
        analogue exists; deferred, not covered.
      - the **FSMA plan PDF** (`/food-safety/<id>/export-pdf`) — no tool.
      Neither blocks cutover; both become unavailable when Flask stops.
- [ ] **Tell the crew what goes dark.** Two iOS features stop working, both
      Tor-only and both excluded from migration by decision:
      the **Apple Wallet badge pass** and the **server-fetched company logo**
      (the cached logo persists). Neither errors — both return early with no
      onion address. Nobody should first learn this from a phone.

---

## 1. Pre-flight — prove ERPNext is ready

### 1.1 DocTypes are migrated

The Cycle 3 tools refuse with "run `bench migrate`" when their DocType is
absent, so an un-migrated site fails loudly rather than silently.

- [ ] On the Umbrel host:
      ```
      docker exec -it fafo-erpnext_server_1 \
        bench --site <site> migrate
      ```
- [ ] Verify the eight HACCP DocTypes exist. Via MCP, `get_food_safety_dashboard`
      is the cheapest single call — it touches all eight and answers with honest
      zeros on an empty site. A refusal naming a DocType means migrate did not
      finish.
- [ ] Confirm the Cycle 1 and Cycle 2 registers are present too — IoT Device,
      MRL Record, Satellite Metric, Satellite Backfill Cursor. `list_iot_devices`,
      `list_mrl_records` and the satellite tools each answer or refuse by name.

### 1.2 Tool switches

Reads ship **on**; **all** mutating tools ship **off** by design.

- [ ] At `/app/erpnext-mcp-settings`, enable the HACCP write switches the farm
      will actually use. They are off after migrate even though the JSON default
      says otherwise for reads — **editing a JSON default never changes a
      deployed site**, only the settings form does.
- [ ] Spot-check one write end to end (`create_food_safety_plan`) before relying
      on it.

### 1.3 Migrate the real data — only if production has any

The sidecar's contents were established in v0.121.0 to be **test data except for
two things**: the MRL reference data and the satellite history. That finding was
about the *local copy*. **Production is a different database and must be checked
on its own terms.**

- [ ] Copy the production SQLite file off the `fafo-farm-app` data volume
      (`${APP_DATA_DIR}/data` on the host).
- [ ] **Dry run first — without `--apply` nothing is written:**
      ```
      python3 scripts/migrate_farm_app.py \
        --database farm_app.db --site <site> \
        --company "Orchard Meadow, LLC" \
        --rasters /path/to/raster/cache \
        --report /tmp/migration-plan.json
      ```
- [ ] **Read the report before applying.** The part that matters is the name
      join: MRL rows point at a crop and a country by SQLite id, nothing on this
      site carries those ids, so the join is on the name, exactly, with no fuzzy
      matching. Every unmatched name is listed under `unmatched`, and a limit
      whose crop or market does not resolve is **refused rather than filed
      against nothing**. Unmatched names here mean missing `Crop`/`Market`
      records — create them, then re-run the dry run.
- [ ] If the report shows only empty tables and duplicate test rows, **skip the
      migration**; there is nothing real to carry.
- [ ] Apply, then re-run the same command without `--apply` and confirm it now
      reports nothing left to do:
      ```
      python3 scripts/migrate_farm_app.py \
        --database farm_app.db --site <site> --apply ...
      ```
- [ ] **Cached NDVI rasters are reported and never moved.** If they matter,
      copy them by hand — `--rasters` only tells you what is there.

### 1.4 Take a backup you have actually tested

- [ ] ERPNext: `bench --site <site> backup --with-files`, copied **off** the host.
- [ ] farm_app: snapshot `${APP_DATA_DIR}/data` for `fafo-farm-app` — this is the
      migration source and the rollback state. Do not skip it because §5 says the
      volume is kept.

---

## 2. iOS — confirm, do not change

- [ ] **No base-URL edit is required.** iOS reaches ERPNext through one constant,
      `MobileAPI.namespace = "farmops/api/mobile"`
      (`FarmOpsKit/Sources/FarmOpsKit/Networking/MobileAPI.swift`), and that is
      already served by `erpnext_mcp/farmops_api/routes.py`. Changing it would
      break a working app.
- [ ] Confirm the handset's configured server points at the **ERPNext** host, not
      at port 5001. A device onboarded long ago may carry an explicit
      `server_url` (`OnboardingPayload.server_url`) — the field whose example
      value is `http://192.168.1.50:5000`. Re-onboard any handset that does.
- [ ] Smoke-test on a real device against ERPNext **before** stopping Flask:
      log in, `get_current_user_context`, `list_my_tasks`, start and end a shift,
      upload one photo (`stage_file_chunk`).
- [ ] Expect these to stay broken — they 404 today and are unrelated to
      retirement: `trace_forward`, `trace_backward`,
      `create_heat_exposure_event`, `list_heat_exposure_events`,
      `create_detector_test`, `create_housing_inspection`,
      `get_inspection_template`, `list_inspection_templates`, `get_active_rei`,
      `generate_mobile_login_qr`. iOS shows these as *unavailable*, not as errors.

---

## 3. Umbrel — stop farm_app

- [ ] **Stop, do not uninstall.** Uninstalling can remove the data volume; the
      whole rollback plan depends on it surviving.
      ```
      sudo ~/umbrel/scripts/app stop fafo-farm-app
      ```
- [ ] Confirm it is down:
      ```
      docker ps --filter name=fafo-farm-app        # expect no rows
      curl -sS -m 5 http://localhost:5001/api/startup-status   # expect refused
      ```
- [ ] Confirm nothing else was collateral. `fafo-spray-app` deliberately uses
      host port 5056 to avoid colliding with farm_app's 5001 — both are separate
      apps and stopping one must not stop the other:
      ```
      docker ps --filter name=fafo-erpnext
      docker ps --filter name=fafo-spray-app
      ```

### Verify ERPNext still works — from outside the host

- [ ] Desk loads and a human can log in.
- [ ] The MCP endpoint answers a real `tools/list`.
- [ ] `get_company_topology` returns the company.
- [ ] `get_food_safety_dashboard` answers.
- [ ] **From a handset on the real network**, repeat the §2 smoke test. A shift
      that starts and ends after Flask is down is the actual pass condition.
- [ ] Check the Tailscale funnel still serves `/farmops` — that mount points at
      port 5250 (`erpnext_mcp`), never at 5001, so retirement should not touch
      it. Confirm rather than assume, and make sure no stale per-route mount
      shadows the prefix.

---

## 4. Rollback — how to put it back

Trigger if the §3 handset test fails, or if anything a crew depends on is
unreachable and not on the known-dark list.

- [ ] Start it again:
      ```
      sudo ~/umbrel/scripts/app start fafo-farm-app
      ```
- [ ] Wait for health. The container's own healthcheck polls
      `http://localhost:5000/api/startup-status` every 30 s with a 180 s
      `start_period` — **give it three minutes before concluding it failed.**
      ```
      docker ps --filter name=fafo-farm-app     # expect healthy
      curl -sS http://localhost:5001/api/startup-status
      ```
- [ ] **The vault needs attention on restart.** `VAULT_KEY` and
      `FARM_APP_VAULT_SALT` are auto-generated and persisted to the data volume
      on first boot; the owner visits `/unlock` to activate vault encryption. If
      the data volume survived — and §3 is written so it does — the existing key
      is still there and this is just the unlock step. **If the volume was
      destroyed, encrypted PII is not recoverable by restarting.** That is the
      reason §3 says stop and not uninstall.
- [ ] **ERPNext keeps whatever was migrated.** Rollback restores Flask; it does
      not un-migrate MRL or satellite rows. Re-running the migration later is
      safe to *plan* against, but re-check the dry-run report rather than
      assuming it will no-op.
- [ ] Write down what failed before trying again. A second attempt without a
      diagnosis is the same attempt.

---

## 5. Post-cutover

**Bake period: 30 days.** Nothing in this section happens before it ends.

During the bake:

- [ ] Leave `fafo-farm-app` **installed and stopped**, data volume intact.
- [ ] Watch for the two known-dark features being reported as bugs — Wallet pass
      and company logo. That is expected behaviour, not a regression.
- [ ] Watch ERPNext logs for 404s on `/farmops/api/mobile/*`: a method the phone
      calls that the server never published looks exactly like a cutover failure
      and is usually one of the ten pre-existing gaps in §2.

After the bake, if nothing has needed rollback:

- [ ] Take a final archival copy of the farm_app data volume and store it where
      backups are kept, not on the host.
- [ ] Uninstall the app:
      ```
      sudo ~/umbrel/scripts/app uninstall fafo-farm-app
      ```
- [ ] Remove `fafo-farm-app` from the community store repo
      (`fafo-umbrel-store/fafo-farm-app/`) and from `umbrel-app-store.yml`, so
      nobody reinstalls `polehntim/farm-app:v0.1.2-alpha` by accident.
- [ ] Close out the deferred items or move them onto a roadmap: the H-2A hiring
      cohorts, the FSMA plan PDF, and the ten unpublished mobile routes.

---

## Quick reference

| Thing | Value |
| --- | --- |
| Umbrel app id | `fafo-farm-app` |
| Container | `fafo-farm-app_server_1` |
| Image | `polehntim/farm-app:v0.1.2-alpha` |
| Host port | `5001` → container `5000` |
| Healthcheck | `GET /api/startup-status` (30 s interval, 180 s start period) |
| Data volume | `${APP_DATA_DIR}/data` → `/data` |
| ERPNext app id | `fafo-erpnext` |
| iOS mobile namespace | `farmops/api/mobile` (already ERPNext) |
| Migration script | `scripts/migrate_farm_app.py` (`--apply` to write) |
| Company | `Orchard Meadow, LLC` |
