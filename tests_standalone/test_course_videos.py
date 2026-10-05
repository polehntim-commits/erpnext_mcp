"""Course videos and watched amount. v0.246.0 (approved queue item 7; decisions 42, 44, 46)."""

from erpnext_mcp import training_videos

from .harness import STORE
from .test_dispatch import ALL_ON, DispatchTestCase

ON = {**ALL_ON, **{f"allow_{t}": 1 for t in ("add_course_video", "update_course_video", "remove_course_video",
                                              "record_video_view", "get_video_progress", "get_training_curriculum")}}
COURSE = "D-6C Dozer Operator"
YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class VideoTestCase(DispatchTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		STORE.seed("Training Type", [{"name": COURSE, "training_type_name": COURSE, "active": 1, "video_url": YT}])
		STORE.seed("Employee", [{"name": "EMP-001", "employee_name": "Ana"}])

	def a_video(self, **kw):
		args = {"training_type": COURSE, "title": "Walk-around", "url": YT, "length_seconds": 600, **kw}
		return self.tool_data("add_course_video", args)["video"]


class TheCourse(VideoTestCase):
	def test_the_old_video_url_moves_in_as_a_required_core_video_once(self):
		self.assertEqual(training_videos.migrate_video_urls(), [COURSE])
		rows = training_videos.videos(COURSE)
		self.assertEqual([(r["kind"], r["required"], r["section"]) for r in rows], [("YouTube", True, "Core")])
		self.assertEqual(training_videos.migrate_video_urls(), [], "create-only")

	def test_videos_by_section_on_the_curriculum(self):
		self.a_video()
		self.a_video(title="1950s D6 film", section="Vintage films", required=False, url="/files/d6-1956.mp4",
		             title_es="Película D6 de 1956")
		data = self.tool_data("get_training_curriculum", {"training_type": COURSE})
		self.assertEqual([s["section"] for s in data["videos"]], ["Core", "Vintage films"])
		vintage = data["videos"][1]["videos"][0]
		self.assertEqual((vintage["kind"], vintage["required"], vintage["title_es"]), ("File", False, "Película D6 de 1956"))
		self.assertEqual(data["video_url"], YT, "older phones still get it")

	def test_a_link_that_is_not_a_video_is_refused(self):
		self.assertIn("YouTube link or an uploaded video",
		              self.tool_error("add_course_video", {"training_type": COURSE, "title": "x", "url": "https://example.com/page"}))

	def test_update_and_remove_keep_the_views(self):
		video = self.a_video()["video"]
		self.tool_data("update_course_video", {"training_type": COURSE, "video": video, "min_pct": 90})
		self.assertEqual(training_videos.videos(COURSE)[0]["min_pct"], 90)
		self.tool_data("record_video_view", {"employee": "EMP-001", "training_type": COURSE, "video": video,
		                                     "stretches": [[0, 60]]})
		gone = self.tool_data("remove_course_video", {"training_type": COURSE, "video": video})
		self.assertEqual(gone["views_kept"], 1)


class WatchedAmount(VideoTestCase):
	def test_stretches_merge_and_skips_do_not_count(self):
		self.assertEqual(training_videos.merge([[100, 200], [0, 50], [180, 260], [50.3, 70], [5, 1]]),
		                 [[0, 70], [100, 260]])

	def test_views_combine_across_phones_into_one_line_and_say_they_are_approximate(self):
		video = self.a_video()["video"]
		first = self.tool_data("record_video_view", {"employee": "EMP-001", "training_type": COURSE, "video": video,
		                                             "stretches": [[0, 300]], "seeks": 1, "furthest_seconds": 300,
		                                             "client_request_id": "a"})
		self.assertEqual(first["watched_pct"], 50.0)
		self.assertIn("Approximate", first["note"])
		again = self.tool_data("record_video_view", {"employee": "EMP-001", "training_type": COURSE, "video": video,
		                                             "stretches": [[0, 300]], "client_request_id": "a"})
		self.assertTrue(again["duplicate"], "a retry is not a second view")
		self.tool_data("record_video_view", {"employee": "EMP-001", "training_type": COURSE, "video": video,
		                                     "stretches": [[250, 522]], "seeks": 2, "furthest_seconds": 522, "device": "B"})
		progress = self.tool_data("get_video_progress", {"employee": "EMP-001", "training_type": COURSE})
		row = progress["videos"][0]
		self.assertEqual((row["views"], row["watched_pct"], row["seeks"]), (2, 87.0, 3))
		self.assertEqual(row["line"], "Watched 87% (8:42 of 10:00) · 2 view(s) · 3 skip(s) · reached 8:42")
		self.assertIn("can't tell whether", progress["footer"]["en"])

	def test_opened_outside_is_not_measured(self):
		video = self.a_video()["video"]
		data = self.tool_data("record_video_view", {"employee": "EMP-001", "training_type": COURSE, "video": video,
		                                            "measured": False})
		self.assertIsNone(data["watched_pct"])
		self.assertEqual(data["measured"], "Not measured")

	def test_the_minimum_is_off_until_set_then_judged_per_required_video(self):
		video = self.a_video()["video"]
		self.tool_data("record_video_view", {"employee": "EMP-001", "training_type": COURSE, "video": video,
		                                     "stretches": [[0, 480]]})
		self.assertTrue(training_videos.progress("EMP-001", COURSE)["minimum_met"])
		STORE.get_raw("Training Type", COURSE)["video_min_pct"] = 90
		progress = training_videos.progress("EMP-001", COURSE)
		self.assertFalse(progress["minimum_met"])
		self.assertEqual(progress["videos"][0]["minimum_pct"], 90)
