# Spec: `get_backup_status` (read-only MCP tool) + backup ingest

For the erpnext_mcp code session. Current version is v0.204.0. Status: design only; nothing implemented.

## 1. Goal

One read-only call answers: "Is this site's data safe right now?"

It should report:
- the last backup (time, size, encrypted y/n)
- whether it reached the peer
- whether the peer restored it last night, and how the checks went
- any age or failure alerts

## 2. Where the data comes from

### Host side (already built, erp-backup kit 2026-10-01.2)

Each box's scripts write a status file after every stage: backup, push, check, standby, archive-test, install, promote, fence.

- Host copy: `/home/umbrel/umbrel/erp-backup/status.json`
- Copy inside the ERPNext container, readable by Frappe: `<bench>/sites/erp_backup_status/<box>.json`, where `<box>` is `oml` or `umbrellocal`
- Schema: `"schema": "erp-backup-status/1"`. No secrets: the archive key shows as a 12-character prefix only, and there are no passwords or tokens.

The scripts never call the ERPNext API. Leaving no API token on the host was a deliberate choice. So ERPNext records are created **inside the app**, by ingest (section 4).

### Status file shape (abridged)

```json
{
 "schema": "erp-backup-status/1", "generated_at": "2026-10-02T07:30:04-07:00",
 "box": "oml", "host": "orchardmeadow-umbrel", "peer": "umbrellocal", "kit_version": "2026-10-01.2",
 "site": "frontend", "standby_site": "standby-umbrellocal",
 "roles": {"send": true, "receive": true, "standby": false},
 "own_backup": {"result": "OK", "at": "...", "started": "...", "set": "2026-10-02_0230", "size_mb": "412",
                "encrypted": "yes", "recipient": "age1abcdefgh", "origin": "oml",
                "counts": "10 5 3 2 1 4 2", "location": "orchardmeadow-umbrel:/home/umbrel/umbrel/erp-backup/own/daily/2026-10-02_0230",
                "age_hours": 5.0, "last_ok_age_hours": 5.0},
 "push": {"result": "OK", "at": "...", "set": "2026-10-02_0230", "peer": "umbrellocal", "standby_set_sent": "yes"},
 "remote_restore_of_my_data": {"restored_by": "umbrellocal", "set": "2026-10-02_0230", "site": "standby-oml",
                "at": "...", "duration_s": 312, "method": "partial-restore", "result": "Pass",
                "counts_ok": "yes", "files": "812/812,1204/1204", "secrets": "14", "decrypt_fail": "0", "age_hours": 3.0},
 "peer_copies_check": {"result": "OK", "newest_set": "...", "age_hours": "4", "daily": "14", "weekly": "8"},
 "standby_of_peer": { ...same keys as a standby run... },
 "last_archive_test": {"result": "OK", "at": "...", "no_key_test": "Pass", "check": "Pass", "duration_s": "540"},
 "promoted": null, "fenced": false,
 "alerts": [{"level": "warning", "code": "REMOTE_RESTORE_STALE", "message": "..."}],
 "overall": "ok | warning | critical"
}
```

On failure, a stage block carries `result: "FAIL"`, `msg`, and `last_ok_at`. The last good details are kept.

## 3. Tool: `get_backup_status`

- **Read-only.** On by default, like the other `get_*`/`list_*` tools.
- **Inputs (all optional):**
  - `box`: default is every file present
  - `stale_hours`: default 30
  - `include_raw`: default false; true returns the status JSON verbatim
- **Sources, in order:**
  1. `sites/erp_backup_status/*.json`
  2. the latest `Backup Record` rows and their test results (same data as `list_backup_records`)
- **Output:**

```json
{
 "site": "frontend", "checked_at": "...", "overall": "ok|warning|critical",
 "boxes": [{
   "box": "oml", "status_file_age_hours": 0.4,
   "last_backup": {"at": "...", "set": "...", "size_mb": 412, "encrypted": true, "origin": "oml", "result": "OK"},
   "offsite_copy": {"peer": "umbrellocal", "last_push_at": "...", "result": "OK"},
   "last_standby_restore": {"by": "umbrellocal", "at": "...", "set": "...", "result": "Pass",
                            "counts_ok": true, "files_ok": true, "decrypt_failures": 0, "duration_minutes": 6},
   "last_archive_test": {"at": "...", "no_key_test": "Pass", "result": "Pass"},
   "standby_held_here": {"of": "umbrellocal", "at": "...", "result": "Pass"},
   "promoted": null, "fenced": false,
   "alerts": [ ... ]
 }],
 "erpnext_records": {"latest_backup_record": "BKP-....", "last_passing_test_on": "2026-10-02", "verification_window_days": 30, "within_window": true},
 "alerts": [ ...all boxes, deduplicated... ]
}
```

- **Alert codes:** the kit computes these. The tool recomputes them from the timestamps so a stale file can't hide a problem.
  - `BACKUP_FAILED`, `BACKUP_STALE` (last good backup > `stale_hours`), `BACKUP_UNENCRYPTED`
  - `PUSH_STALE_OR_FAILED`
  - `NO_REMOTE_RESTORE`, `REMOTE_RESTORE_STALE`, `REMOTE_RESTORE_NOT_PASS`
  - `PEER_COPIES_BAD`, `STANDBY_FAILED`, `STANDBY_STALE`, `STANDBY_CHECK_NOT_PASS`
  - `PROMOTED` (critical), `FENCED`
  - Added by the tool: `NO_STATUS_FILE` (critical) and `REPORTER_DEAD` (status file > 26 h old, critical). The scripts run at least daily, so a silent file means the timers are gone, often after an umbrelOS update.
  - Added by the tool: `NO_PASSING_TEST_IN_WINDOW`, from Backup Record tests over the verification window.
- **Never** return file paths outside `sites/erp_backup_status/`, never read other host files, never shell out.

## 4. Ingest: status file → ERPNext records

Add a scheduler job (hourly) plus a whitelisted, System-Manager-only method `erpnext_mcp.backup_status.ingest()`. It reads `sites/erp_backup_status/*.json` and upserts records idempotently. These are the same operations the existing tools do.

| Status file field | ERPNext action |
|---|---|
| `own_backup` with `result=OK`, new `set` | `create_backup_record`: `backup_type=Full`, `started_at=started`, `completed_at=at`, `status=Success`, `location=location`, `size_mb`, `retention_days=KEEP_DAILY` (14), `offsite=false`, `rpo_hours=24`, `rto_hours` from the last passing test, notes = `set`, origin, encrypted=age, recipient prefix, kit version |
| `push` OK for the same `set` | second `create_backup_record`: `backup_type=Offsite Replica`, `offsite=true` only if Tim confirms umbrel.local is at a different site (config flag in the app; default false → `Full`); `location=<peer>:/home/umbrel/umbrel/erp-backup-from-<box>/daily/<set>` |
| `own_backup` with `result=FAIL` | `create_backup_record` with `status=Failed`, notes = `msg`. One per failure `at`. |
| `remote_restore_of_my_data` with a new `at` | `record_backup_test` on the replica record for that `set`: Pass→`Pass`, Partial→`Partial`, anything else→`Fail`; `restore_duration_minutes=ceil(duration_s/60)`; `test_restore_by="erp-backup@<restored_by>"`; notes = method, site, counts_ok, files, secrets, decrypt_fail |
| `last_archive_test` (on the peer's file; it tests *our* archive when `source=peer`) | `record_backup_test` on the replica record, notes "full restore from encrypted archive + no-key test=<no_key_test>" |

- **Idempotency key:** `(box, set, kind)`, stored in the record notes or a hidden custom field. Re-ingesting never duplicates.
- **Cross-box:** OML's file carries the receipt about OML's data (`remote_restore_of_my_data`), because umbrel.local pushes receipts back. So OML's site can record its own restore tests without reading umbrel.local's file.
- **Until this ships:** Claude creates the same records by hand from `status.sh` output using the existing tools.

## 5. Tests for the code session

- Fixture files: all-OK; backup FAIL with `last_ok_at`; stale file (generated_at − 40 h); missing receipt; `promoted` set; Partial standby.
- Run ingest twice → no duplicates.
- The tool on a site with no status dir → `NO_STATUS_FILE`, no exception.
- Timezone: timestamps are ISO-8601 with offset. Compare as aware datetimes.

---

## 6. Implementation contract (v0.215.0) — decisions on top of the spec

Frozen before code. Where this section and the spec above differ, this section is what ships.

1. **Where the files are read.** `<sites path>/erp_backup_status/*.json` and nothing else. A file is
   read only if its name is `[a-z0-9_-]{1,40}.json`, it resolves inside that directory (no symlink
   out), it is under 256 KB, and it declares `schema: erp-backup-status/1`. Anything else is skipped
   and named in `skipped`. No shell, no other path, no path in any answer beyond the file's name.
2. **Which box is this site.** One file present → that box. Several → the Farm Feature Flag
   `backup_box` names it; with no flag, ingest does nothing and says so (the read tool still reports
   every file). A second box's file is used for one thing: its `last_archive_test` with
   `source: "peer"` and `peer` = this box is a test of **our** archive.
3. **Company.** Backup Record needs one. Flag `backup_record_company`, else the site's default
   company. With neither, ingest reports it and writes nothing.
4. **Idempotency is a column, not a note.** Backup Record gains `ingest_key` (hidden, read-only):
   `<box>|<set>|own`, `<box>|<set>|replica`, `<box>|<failed at>|failed`. Tests are keyed in
   `test_ingest_key`: `<restored_by>|<at>|restore`, `<tester>|<at>|archive`. Re-ingesting writes nothing.
5. **`test_restore_by` is a Link to User**, so `erp-backup@<box>` cannot be stored in it. It is left
   empty and the first line of `test_restore_notes` is `By erp-backup@<restored_by>`.
6. **Offsite.** Flag `backup_peer_is_offsite` (default false). False → the replica row is
   `backup_type: Full`, `offsite: 0`; true → `Offsite Replica`, `offsite: 1`.
7. **A newer test replaces an older one on the same record** only when its `at` is later.
8. **Ingest** is `erpnext_mcp.backup_status.ingest` — whitelisted, System Manager only — and an hourly
   scheduler entry that never raises. It writes Backup Records only; it never edits or deletes one
   it did not create.
9. **The tool never writes.** `get_backup_status` is read-only and on by default; it does not ingest.
10. **Alert levels.** critical: `NO_STATUS_FILE`, `REPORTER_DEAD`, `BACKUP_FAILED`, `BACKUP_STALE`,
    `REMOTE_RESTORE_NOT_PASS`, `PROMOTED`. warning: everything else. `overall` is the worst level
    present. The kit's own alerts are kept; a code the tool recomputed replaces the kit's.

Tools: 982 → **983** (478 read, 505 write). No mobile route. No iOS change.
