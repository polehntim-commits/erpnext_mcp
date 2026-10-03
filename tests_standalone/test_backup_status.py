# SPDX-License-Identifier: MIT
"""get_backup_status and the backup ingest (v0.215.0). docs/design/backup_status.md."""

import datetime
import json
import os
import shutil
import tempfile
from unittest import mock

import frappe

from erpnext_mcp import backup_status, hooks, registry

from .fixtures import MAIN, SeededTestCase
from .harness import STORE, set_roles

BACKUP = "Backup Record"
ZONE = datetime.timezone(datetime.timedelta(hours=-7))


def ago(hours: float) -> str:
	"""An ISO-8601 stamp with an offset, `hours` before now — as the kit writes them."""
	return (datetime.datetime.now(ZONE) - datetime.timedelta(hours=hours)).isoformat(timespec="seconds")


def all_ok(box="oml", peer="umbrellocal", set_name="2026-10-02_0230") -> dict:
	return {
		"schema": "erp-backup-status/1",
		"generated_at": ago(0.4),
		"box": box,
		"host": "orchardmeadow-umbrel",
		"peer": peer,
		"kit_version": "2026-10-01.2",
		"site": "frontend",
		"roles": {"send": True, "receive": True, "standby": False},
		"own_backup": {
			"result": "OK",
			"at": ago(5),
			"started": ago(5.1),
			"set": set_name,
			"size_mb": "412",
			"encrypted": "yes",
			"recipient": "age1abcdefgh",
			"origin": box,
			"location": f"orchardmeadow-umbrel:/home/umbrel/umbrel/erp-backup/own/daily/{set_name}",
		},
		"push": {"result": "OK", "at": ago(4.8), "set": set_name, "peer": peer},
		"remote_restore_of_my_data": {
			"restored_by": peer,
			"set": set_name,
			"site": f"standby-{box}",
			"at": ago(3),
			"duration_s": 312,
			"method": "partial-restore",
			"result": "Pass",
			"counts_ok": "yes",
			"files": "812/812,1204/1204",
			"secrets": "14",
			"decrypt_fail": "0",
		},
		"peer_copies_check": {"result": "OK", "age_hours": "4", "daily": "14", "weekly": "8"},
		"last_archive_test": {
			"result": "OK",
			"at": ago(20),
			"no_key_test": "Pass",
			"check": "Pass",
			"duration_s": "540",
		},
		"promoted": None,
		"fenced": False,
		"alerts": [],
		"overall": "ok",
	}


class BackupStatusCase(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.dir = tempfile.mkdtemp(prefix="erp_backup_status_")
		self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
		self.flags = {backup_status.FLAG_COMPANY: MAIN}
		for target, replacement in (
			("status_dir", lambda: self.dir),
			("_flag", lambda key, default=None: self.flags.get(key, default)),
		):
			patcher = mock.patch.object(backup_status, target, replacement)
			patcher.start()
			self.addCleanup(patcher.stop)

	def write(self, body: dict, name: str | None = None) -> None:
		with open(os.path.join(self.dir, name or f"{body['box']}.json"), "w", encoding="utf-8") as handle:
			json.dump(body, handle)

	def codes(self, answer) -> set:
		return {alert["code"] for alert in answer["alerts"]}

	def records(self) -> list:
		return STORE.rows(BACKUP)


class TheTool(BackupStatusCase):
	def test_all_ok_after_ingest_is_ok(self):
		self.write(all_ok())
		backup_status.run_ingest()
		answer = self.tool_data("get_backup_status", {})
		self.assertEqual((answer["overall"], answer["alerts"]), ("ok", []))
		box = answer["boxes"][0]
		self.assertEqual(
			(box["box"], box["last_backup"]["size_mb"], box["last_backup"]["encrypted"]), ("oml", 412.0, True)
		)
		self.assertEqual((box["offsite_copy"]["peer"], box["offsite_copy"]["result"]), ("umbrellocal", "OK"))
		restore = box["last_standby_restore"]
		self.assertEqual(
			(
				restore["result"],
				restore["counts_ok"],
				restore["files_ok"],
				restore["decrypt_failures"],
				restore["duration_minutes"],
			),
			("Pass", True, True, 0, 6),
		)
		self.assertEqual(box["last_archive_test"]["no_key_test"], "Pass")
		self.assertLess(box["status_file_age_hours"], 1)
		self.assertTrue(answer["erpnext_records"]["within_window"])
		self.assertNotIn("raw", answer)
		self.assertNotIn(self.dir, json.dumps(answer), "no path beyond the file's own name")
		self.assertEqual(
			self.tool_data("get_backup_status", {"include_raw": True})["raw"]["oml"]["kit_version"],
			"2026-10-01.2",
		)

	def test_no_status_directory_is_an_alert_and_not_an_exception(self):
		shutil.rmtree(self.dir)
		answer = self.tool_data("get_backup_status", {})
		self.assertEqual(answer["overall"], "critical")
		self.assertIn("NO_STATUS_FILE", self.codes(answer))
		self.assertEqual(answer["boxes"], [])
		self.assertIn("NO_STATUS_FILE", self.codes(self.tool_data("get_backup_status", {"box": "oml"})))

	def test_a_failed_backup_keeps_its_last_good_details(self):
		body = all_ok()
		body["own_backup"].update(result="FAIL", msg="disk full", last_ok_at=ago(29))
		body["overall"] = "ok"  # the file's own verdict is not believed
		self.write(body)
		answer = self.tool_data("get_backup_status", {})
		self.assertEqual(answer["overall"], "critical")
		self.assertIn("BACKUP_FAILED", self.codes(answer))
		self.assertNotIn("BACKUP_STALE", self.codes(answer), "the last good one is 29 h old, inside 30")
		self.assertIn("BACKUP_STALE", self.codes(self.tool_data("get_backup_status", {"stale_hours": 24})))
		self.assertEqual(answer["boxes"][0]["last_backup"]["message"], "disk full")

	def test_a_stale_file_cannot_hide_behind_its_own_ok(self):
		body = all_ok()
		body["generated_at"] = ago(40)
		for key in ("own_backup", "push", "remote_restore_of_my_data"):
			body[key]["at"] = ago(45)
		self.write(body)
		codes = self.codes(self.tool_data("get_backup_status", {}))
		self.assertTrue(
			{"REPORTER_DEAD", "BACKUP_STALE", "PUSH_STALE_OR_FAILED", "REMOTE_RESTORE_STALE"} <= codes
		)

	def test_a_missing_receipt_a_partial_restore_and_an_unencrypted_backup(self):
		body = all_ok()
		del body["remote_restore_of_my_data"]
		body["own_backup"]["encrypted"] = "no"
		self.write(body)
		codes = self.codes(self.tool_data("get_backup_status", {}))
		self.assertTrue({"NO_REMOTE_RESTORE", "BACKUP_UNENCRYPTED"} <= codes)
		body = all_ok()
		body["remote_restore_of_my_data"]["result"] = "Partial"
		self.write(body)
		answer = self.tool_data("get_backup_status", {})
		self.assertIn("REMOTE_RESTORE_NOT_PASS", self.codes(answer))
		self.assertEqual(answer["overall"], "critical")

	def test_promoted_and_fenced_and_the_kits_own_alerts(self):
		body = all_ok()
		body["promoted"] = {"at": ago(1), "by": "tim"}
		body["fenced"] = True
		body["alerts"] = [
			{"level": "warning", "code": "DISK_LOW", "message": "12% free"},
			{"level": "warning", "code": "PROMOTED", "message": "dup"},
		]
		self.write(body)
		answer = self.tool_data("get_backup_status", {})
		by_code = {a["code"]: a for a in answer["alerts"]}
		self.assertEqual(by_code["PROMOTED"]["level"], "critical")
		self.assertEqual(by_code["FENCED"]["level"], "warning")
		self.assertEqual(by_code["DISK_LOW"]["message"], "12% free")
		self.assertEqual([a["code"] for a in answer["alerts"]].count("PROMOTED"), 1)

	def test_a_standby_box_reports_what_it_holds(self):
		body = all_ok(box="umbrellocal", peer="oml")
		body["roles"] = {"send": False, "receive": True, "standby": True}
		body["standby_of_peer"] = {"result": "Partial", "at": ago(2), "set": "2026-10-02_0230"}
		body["peer_copies_check"]["result"] = "FAIL"
		self.write(body)
		answer = self.tool_data("get_backup_status", {})
		codes = self.codes(answer)
		self.assertTrue({"STANDBY_CHECK_NOT_PASS", "PEER_COPIES_BAD"} <= codes)
		self.assertNotIn("BACKUP_STALE", codes, "a box that does not send is not judged on its own backup")
		self.assertEqual(
			answer["boxes"][0]["standby_held_here"],
			{"of": "oml", "at": body["standby_of_peer"]["at"], "result": "Partial"},
		)

	def test_a_box_without_the_standby_role_is_not_judged_on_one(self):
		"""v0.222.1. OML: STANDBY_ENABLED=0, roles.standby false, an empty standby block."""
		body = all_ok()
		body["standby_of_peer"] = {"result": "", "at": None}
		body["alerts"] = [{"level": "warning", "code": "STANDBY_CHECK_NOT_PASS", "message": "kit says so"}]
		self.write(body)
		backup_status.run_ingest()
		answer = self.tool_data("get_backup_status", {})
		self.assertEqual((answer["overall"], answer["alerts"]), ("ok", []))
		self.assertIsNone(answer["boxes"][0]["standby_held_here"])
		self.assertFalse(answer["boxes"][0]["holds_standby"])

	def test_the_peer_is_named_as_not_reported_here(self):
		self.write(all_ok())
		answer = self.tool_data("get_backup_status", {})
		self.assertEqual([p["box"] for p in answer["peers_not_reported"]], ["umbrellocal"])
		peer = all_ok(box="umbrellocal", peer="oml")
		peer["roles"] = {"send": True, "receive": True, "standby": True}
		peer["standby_of_peer"] = {"result": "Pass", "at": ago(2), "set": "2026-10-02_0230"}
		self.write(peer)
		self.flags[backup_status.FLAG_BOX] = "oml"
		answer = self.tool_data("get_backup_status", {})
		self.assertEqual(answer["peers_not_reported"], [])
		self.assertEqual(len(answer["boxes"]), 2)

	def test_only_status_files_in_the_one_directory_are_read(self):
		self.write(all_ok())
		self.write({"schema": "something-else/1", "box": "evil"}, "evil.json")
		with open(os.path.join(self.dir, "notes.txt"), "w") as handle:
			handle.write("not json")
		with open(os.path.join(self.dir, "broken.json"), "w") as handle:
			handle.write("{")
		outside = tempfile.mkdtemp()
		self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
		with open(os.path.join(outside, "secret.json"), "w") as handle:
			json.dump(all_ok(box="stolen"), handle)
		os.symlink(os.path.join(outside, "secret.json"), os.path.join(self.dir, "link.json"))
		answer = self.tool_data("get_backup_status", {"include_raw": True})
		self.assertEqual([b["box"] for b in answer["boxes"]], ["oml"])
		self.assertEqual({s["file"] for s in answer["skipped"]}, {"evil.json", "broken.json", "link.json"})

	def test_timestamps_are_compared_as_aware_datetimes(self):
		utc = backup_status.parse("2026-10-02T14:30:04+00:00")
		local = backup_status.parse("2026-10-02T07:30:04-07:00")
		self.assertEqual(utc, local)
		self.assertEqual(
			backup_status.hours_since("2026-10-02T07:30:04-07:00", utc + datetime.timedelta(hours=3)), 3.0
		)
		self.assertIsNone(backup_status.parse("yesterday"))

	def test_it_is_a_read_tool_and_on_by_default(self):
		self.assertIn("get_backup_status", registry.READ_TOOLS)
		self.write(all_ok())
		before = len(self.records())
		self.tool_data("get_backup_status", {})
		self.assertEqual(len(self.records()), before, "the tool never ingests")
		self.assertIn("stale_hours", self.tool_error("get_backup_status", {"stale_hours": "soon"}))


class Ingest(BackupStatusCase):
	def test_an_ok_file_becomes_two_records_and_a_passing_test_once(self):
		self.write(all_ok())
		report = backup_status.run_ingest()
		self.assertEqual(
			(report["box"], report["company"], len(report["created"]), len(report["tests"])),
			("oml", MAIN, 2, 1),
		)
		rows = {r["ingest_key"]: r for r in self.records()}
		own = rows["oml|2026-10-02_0230|own"]
		self.assertEqual(
			(own["backup_type"], own["status"], own["size_mb"], own["retention_days"], own["rpo_hours"]),
			("Full", "Success", 412.0, 14, 24),
		)
		self.assertIn("age1abcdefgh", own["notes"])
		self.assertTrue(own["started_at"] <= own["completed_at"])
		replica = rows["oml|2026-10-02_0230|replica"]
		self.assertEqual((replica["backup_type"], int(replica.get("offsite") or 0)), ("Full", 0))
		self.assertTrue(
			replica["location"].startswith("umbrellocal:/home/umbrel/umbrel/erp-backup-from-oml/daily/")
		)
		self.assertEqual((replica["test_restore_result"], replica["restore_duration_minutes"]), ("Pass", 6))
		self.assertTrue(replica["test_restore_notes"].startswith("By erp-backup@umbrellocal"))
		self.assertFalse(replica.get("test_restore_by"))

		again = backup_status.run_ingest()
		self.assertEqual((again["created"], again["tests"]), ([], []))
		self.assertEqual(len(self.records()), 2)

	def test_a_new_set_and_a_new_restore_add_and_the_rto_follows_the_last_pass(self):
		self.write(all_ok())
		backup_status.run_ingest()
		self.write(all_ok(set_name="2026-10-03_0230"))
		report = backup_status.run_ingest()
		self.assertEqual((len(report["created"]), len(report["tests"])), (2, 1))
		self.assertEqual(len(self.records()), 4)
		newest = next(r for r in self.records() if r["ingest_key"] == "oml|2026-10-03_0230|own")
		self.assertEqual(newest["rto_hours"], 1, "six minutes, rounded up to the hour")

	def test_a_failed_backup_is_one_failed_record_per_failure(self):
		body = all_ok()
		failed_at = ago(1)
		body["own_backup"].update(result="FAIL", msg="disk full", at=failed_at, last_ok_at=ago(26))
		self.write(body)
		backup_status.run_ingest()
		backup_status.run_ingest()
		failed = [r for r in self.records() if r["status"] == "Failed"]
		self.assertEqual(len(failed), 1)
		self.assertIn("disk full", failed[0]["notes"])

	def test_a_partial_restore_is_recorded_as_partial_and_anything_else_as_fail(self):
		body = all_ok()
		body["remote_restore_of_my_data"]["result"] = "Partial"
		self.write(body)
		backup_status.run_ingest()
		replica = next(r for r in self.records() if r["ingest_key"].endswith("|replica"))
		self.assertEqual(replica["test_restore_result"], "Partial")
		self.assertEqual(backup_status._result("exploded"), "Fail")

	def test_the_offsite_flag_makes_the_copy_an_offsite_replica(self):
		self.flags[backup_status.FLAG_OFFSITE] = True
		self.write(all_ok())
		backup_status.run_ingest()
		replica = next(r for r in self.records() if r["ingest_key"].endswith("|replica"))
		self.assertEqual((replica["backup_type"], int(replica["offsite"])), ("Offsite Replica", 1))

	def test_two_files_need_the_box_flag_and_the_peers_archive_test_is_ours(self):
		self.write(all_ok())
		peer = all_ok(box="umbrellocal", peer="oml")
		peer["last_archive_test"] = {
			"source": "peer",
			"at": ago(1),
			"check": "Pass",
			"no_key_test": "Pass",
			"duration_s": "540",
			"set": "2026-10-02_0230",
		}
		self.write(peer)
		report = backup_status.run_ingest()
		self.assertEqual(report["created"], [])
		self.assertIn("backup_box", report["notes"][0])
		self.flags[backup_status.FLAG_BOX] = "oml"
		report = backup_status.run_ingest()
		self.assertEqual([t["kind"] for t in report["tests"]], ["restore", "archive"])
		replica = next(r for r in self.records() if r["ingest_key"] == "oml|2026-10-02_0230|replica")
		self.assertIn("no-key test=Pass", replica["test_restore_notes"])
		self.assertEqual(replica["restore_duration_minutes"], 9)
		self.assertEqual(len([r for r in self.records() if r["ingest_key"].startswith("umbrellocal|")]), 0)
		self.assertEqual(backup_status.run_ingest()["tests"], [])

	def test_nothing_to_read_and_no_company_are_said_not_raised(self):
		self.assertEqual(backup_status.run_ingest()["notes"], ["no status file to ingest."])
		self.write(all_ok())
		self.flags.pop(backup_status.FLAG_COMPANY)
		with mock.patch.object(backup_status, "_company", lambda: ""):
			report = backup_status.run_ingest()
		self.assertIn("no company", report["notes"][0])
		self.assertEqual(self.records(), [])
		backup_status.ingest_scheduled()

	def test_the_whitelisted_ingest_is_a_system_managers_and_the_job_is_hourly(self):
		self.write(all_ok())
		set_roles("clerk@example.test", ["Accounts User"])
		frappe.set_user("clerk@example.test")
		with self.assertRaises(frappe.PermissionError):
			backup_status.ingest()
		frappe.set_user("Administrator")
		self.assertEqual(len(backup_status.ingest()["created"]), 2)
		self.assertIn("erpnext_mcp.backup_status.ingest_scheduled", hooks.scheduler_events["hourly"])
