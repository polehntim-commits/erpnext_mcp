# SPDX-License-Identifier: MIT
"""Did the deploy land? Read-only, no shell. v0.318.0."""

import json
import os
import tempfile
from unittest import mock

from erpnext_mcp import __version__, backup_status, deploy_status

from .fixtures import V12TestCase


class TheCommit(V12TestCase):
	def test_head_from_a_ref_file_and_from_packed_refs(self):
		with tempfile.TemporaryDirectory() as root:
			git = os.path.join(root, ".git")
			os.makedirs(os.path.join(git, "refs", "heads"))
			with open(os.path.join(git, "HEAD"), "w") as f:
				f.write("ref: refs/heads/main\n")
			with open(os.path.join(git, "refs", "heads", "main"), "w") as f:
				f.write("a" * 40 + "\n")
			self.assertEqual(deploy_status.git_head(root), {"branch": "main", "commit": "a" * 40})
			os.remove(os.path.join(git, "refs", "heads", "main"))
			with open(os.path.join(git, "packed-refs"), "w") as f:
				f.write("# pack-refs\n" + "b" * 40 + " refs/heads/main\n")
			self.assertEqual(deploy_status.git_head(root)["commit"], "b" * 40)

	def test_the_image_build_record(self):
		with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
			json.dump({"sha": "c" * 40, "commit": {"message": "x"}}, f)
		try:
			self.assertEqual(deploy_status.image_head(f.name), {"commit": "c" * 40})
		finally:
			os.remove(f.name)
		self.assertEqual(deploy_status.image_head("/nonexistent/head.json"), {})


class TheStatus(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_get_deploy_status=1, allow_run_post_deploy_check=1)

	def test_version_patches_and_health(self):
		answer = {"checked": True, "ok": True, "answer": {"ok": True, "service": "farmops-api"}}
		with mock.patch.object(deploy_status, "health", return_value=answer):
			got = self.tool_data("get_deploy_status")
		self.assertEqual(got["version"], __version__)
		self.assertIsInstance(got["pending_patches"], list)
		self.assertTrue(got["health"]["ok"])
		self.assertIn("migrate_needed", got)

	def test_the_post_deploy_check_flags_a_wrong_version(self):
		with mock.patch.object(deploy_status, "health", return_value={"checked": True, "ok": True, "answer": "ok"}):
			got = self.tool_data("run_post_deploy_check", {"expected_version": "0.1.0"})
		version = next(c for c in got["checks"] if c["check"] == "4a version")
		self.assertEqual(version["status"], "fail")
		self.assertEqual(got["overall"], "fail")
		self.assertIn("4e phone checks (on a device)", got["not_checked_here"])

	def test_an_unset_public_url_is_not_checked_rather_than_failed(self):
		got = deploy_status.health()
		self.assertFalse(got["checked"])


class TheKit(V12TestCase):
	def test_an_older_kit_is_flagged(self):
		body = {"schema": backup_status.SCHEMA, "box": "oml", "kit_version": "2026-09-01.1",
		        "generated_at": "2026-10-11T08:00:00Z"}
		got = backup_status.describe_box(body, backup_status.STALE_HOURS)
		self.assertEqual((got["kit_expected"], got["kit_outdated"]), (backup_status.KIT_EXPECTED, True))
