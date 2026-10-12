# SPDX-License-Identifier: MIT
"""v0.321.0 — the lens and zoom a photo was taken with (FarmOps 0.64.0: zoom out to 0.5× instead of walking back).

Farm Task Evidence gains `capture_lens` and `zoom_factor`; the phone sends them per file beside captured_at and the
fix, and an odd value is dropped rather than refused — the photograph matters more than its label.
"""

from erpnext_mcp.api import mobile as mobile_api

from . import test_task_workflow as tw
from .harness import STORE


class TheLensIsKept(tw.WorkflowTestCase):
	def test_lens_and_zoom_land_on_the_evidence_row(self):
		task = self.claimed_report()
		wide = tw.a_photo_file("5b0c1e7d2a", f"{task}_photo_after_UW.jpg")
		odd = tw.a_photo_file("9e8d7c6b5a", f"{task}_photo_after_X.jpg")
		answer = self.finish(task, hours_reading=1, evidence_files=[
			{"file_token": wide, "kind": "photo", "capture_lens": "ultra_wide", "zoom_factor": 0.5},
			{"file_token": odd, "kind": "photo", "capture_lens": "fisheye", "zoom_factor": "lots"},
		])
		files = {row["file"]: row for row in STORE.get_raw("Farm Task Assignment",
		                                                   answer["assignment"]["name"])["evidence_files"]}
		self.assertEqual(files[wide]["capture_lens"], "ultra_wide")
		self.assertAlmostEqual(files[wide]["zoom_factor"], 0.5)
		self.assertNotIn("capture_lens", {k for k, v in files[odd].items() if v})
		self.assertNotIn("zoom_factor", {k for k, v in files[odd].items() if v})


class ThePhoneForwardsThem(tw.WorkflowTestCase):
	def test_the_mobile_normaliser_passes_lens_and_zoom(self):
		rows = mobile_api._evidence([{"file_token": "x", "kind": "photo", "capture_lens": "wide", "zoom_factor": 1}])
		self.assertEqual(rows[0]["capture_lens"], "wide")
		self.assertEqual(rows[0]["zoom_factor"], 1)
