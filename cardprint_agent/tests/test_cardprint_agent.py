# SPDX-License-Identifier: MIT
"""The card print agent against a fake printer and a fake ERPNext. Standard library only.

Run: python3 -m unittest discover -s cardprint_agent/tests -t cardprint_agent
"""

import base64
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cardprint_agent as agent

PDF = agent.test_card_pdf()


class FakeShell(agent.Shell):
	"""lp / lpstat / lpoptions / ipptool, scripted."""

	def __init__(self):
		self.calls = []
		self.enabled = True
		self.accepting = True
		self.reasons = "none"
		self.lp_fails = False
		self.queue_rounds = 1  # how many lpstat polls the job stays in the queue
		self.final = "completed"
		self.printed_files = []

	def run(self, args, timeout=30):
		self.calls.append(list(args))
		tool = args[0]
		if tool == "lpstat" and "-p" in args:
			word = "is idle.  enabled" if self.enabled else "disabled"
			return 0, f"printer Primacy_2 {word} since Thu Oct  1 10:27:07 2026\n\tOut of ribbon\n", ""
		if tool == "lpstat" and "-a" in args:
			return (
				0,
				"Primacy_2 " + ("accepting requests" if self.accepting else "not accepting requests"),
				"",
			)
		if tool == "lpoptions":
			return (
				0,
				f"device-uri=socket://192.168.1.196/ printer-state=3 printer-state-reasons={self.reasons}",
				"",
			)
		if tool == "lp":
			if self.lp_fails:
				return 1, "", "lp: Unsupported document-format"
			with open(args[-1], "rb") as handle:
				self.printed_files.append((args[-1], handle.read()))
			return 0, "request id is Primacy_2-42 (1 file(s))\n", ""
		if tool == "lpstat" and "not-completed" in args:
			self.queue_rounds -= 1
			return 0, ("Primacy_2-42 tim 1024 Thu\n" if self.queue_rounds >= 0 else ""), ""
		if tool == "ipptool":
			return 0, f"        job-state (enum) = {self.final}\n", ""
		return 0, "", ""


class FakeServer:
	def __init__(self, jobs=None, unreachable=False):
		self.jobs = list(jobs or [])
		self.calls = []
		self.unreachable = unreachable

	def call(self, method, **params):
		if self.unreachable:
			raise agent.ServerUnreachable("connection refused")
		self.calls.append((method, params))
		if method == "claim_next_card_print_job":
			if params.get("printer_state") != agent.READY or not self.jobs:
				return {"job": None}
			return {"job": self.jobs.pop(0)}
		return {"status": "ok"}


def job(name="CPJ-2026-00001", copies=1, sides="Single", pdf=PDF):
	return {
		"name": name,
		"job_type": "Employee ID",
		"copies": copies,
		"sides": sides,
		"reference_title": "Ana Ramos",
		"attempts": 1,
		"artwork_base64": base64.b64encode(pdf).decode(),
		"artwork_sha256": hashlib.sha256(pdf).hexdigest(),
		"file_name": f"{name}.pdf",
	}


CONFIG = dict(agent.DEFAULTS, print_timeout_seconds=6)
NO_SLEEP = lambda seconds: None  # noqa: E731


class TheLoop(unittest.TestCase):
	def test_a_job_is_printed_reported_and_its_file_deleted(self):
		shell, server = FakeShell(), FakeServer([job(copies=2)])
		self.assertEqual(agent.run_once(server, shell, CONFIG, sleep=NO_SLEEP), "printed")
		lp = next(call for call in shell.calls if call[0] == "lp")
		self.assertEqual(
			lp[:-1],
			[
				"lp",
				"-d",
				"Primacy_2",
				"-n",
				"2",
				"-o",
				"PageSize=Card",
				"-o",
				"Duplex=NONE",
				"-o",
				"fit-to-page",
			],
		)
		path, content = shell.printed_files[0]
		self.assertEqual(content, PDF)
		self.assertFalse(os.path.exists(path), "the temp PDF is deleted after the job")
		method, params = server.calls[-1]
		self.assertEqual(method, "complete_card_print_job")
		self.assertEqual(
			(params["name"], params["success"], params["cups_job"]), ("CPJ-2026-00001", 1, "Primacy_2-42")
		)

	def test_dual_uses_the_drivers_duplex_option(self):
		args = agent.lp_arguments(CONFIG, 1, "Dual", "/tmp/x.pdf")
		self.assertIn("Duplex=DuplexNoTumble", args)
		self.assertNotIn("Duplex=NONE", args)

	def test_a_paused_queue_claims_nothing_and_says_why(self):
		shell, server = FakeShell(), FakeServer([job()])
		shell.enabled = False
		self.assertEqual(agent.run_once(server, shell, CONFIG, sleep=NO_SLEEP), "not-ready")
		method, params = server.calls[0]
		self.assertEqual((method, params["printer_state"]), ("claim_next_card_print_job", "Paused"))
		self.assertEqual(len(server.jobs), 1, "the job stays queued")
		self.assertFalse([c for c in shell.calls if c[0] == "lp"])

	def test_a_printer_error_is_reported_as_one(self):
		shell = FakeShell()
		shell.reasons = "media-empty-error"
		self.assertEqual(
			agent.printer_state(shell, "Primacy_2"),
			("Printer error", "the printer reports media-empty-error"),
		)
		shell.reasons = "none"
		shell.accepting = False
		self.assertEqual(agent.printer_state(shell, "Primacy_2")[0], "Paused")

	def test_lp_refusing_the_file_is_not_retried(self):
		shell, server = FakeShell(), FakeServer([job()])
		shell.lp_fails = True
		self.assertEqual(agent.run_once(server, shell, CONFIG, sleep=NO_SLEEP), "failed")
		params = server.calls[-1][1]
		self.assertEqual((params["success"], params["retryable"]), (0, 0))
		self.assertIn("lp refused", params["error"])

	def test_an_aborted_job_is_retryable(self):
		shell, server = FakeShell(), FakeServer([job()])
		shell.final = "aborted"
		agent.run_once(server, shell, CONFIG, sleep=NO_SLEEP)
		params = server.calls[-1][1]
		self.assertEqual((params["success"], params["retryable"]), (0, 1))
		self.assertIn("aborted", params["error"])

	def test_a_job_that_never_finishes_times_out_and_is_cancelled(self):
		shell, server = FakeShell(), FakeServer([job()])
		shell.queue_rounds = 10_000
		ticks = iter(range(0, 1000, 3))
		result = agent.wait_for_job(shell, CONFIG, "Primacy_2-42", sleep=NO_SLEEP, clock=lambda: next(ticks))
		self.assertEqual(result[0], False)
		self.assertTrue(result[2])
		self.assertIn(["cancel", "Primacy_2-42"], shell.calls)
		self.assertTrue(server.jobs)

	def test_a_damaged_download_is_refused_before_printing(self):
		bad = job()
		bad["artwork_sha256"] = "0" * 64
		shell = FakeShell()
		result = agent.print_job(shell, CONFIG, bad, sleep=NO_SLEEP)
		self.assertEqual((result["success"], result["retryable"]), (0, 0))
		self.assertFalse([c for c in shell.calls if c[0] == "lp"])

	def test_an_unreachable_server_raises_for_the_backoff(self):
		with self.assertRaises(agent.ServerUnreachable):
			agent.run_once(FakeServer(unreachable=True), FakeShell(), CONFIG, sleep=NO_SLEEP)

	def test_nothing_queued_is_idle(self):
		self.assertEqual(agent.run_once(FakeServer(), FakeShell(), CONFIG, sleep=NO_SLEEP), "idle")


class ConfigAndPieces(unittest.TestCase):
	def test_the_example_config_parses_on_any_python(self):
		here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
		config = agent.load_config(os.path.join(here, "config.example.toml"))
		self.assertEqual(config["queue"], "Primacy_2")
		self.assertEqual(config["media_option"], "PageSize=Card")
		self.assertEqual(config["extra_options"], ["fit-to-page"])
		self.assertEqual(config["poll_seconds"], 5)

	def test_the_fallback_toml_reader(self):
		text = 'a = "x"  # note\nb = 5\nc = ["p", "q"]\nd = true\n# comment\n'
		real = sys.modules.pop("tomllib", None)
		sys.modules["tomllib"] = None  # force the fallback
		try:
			self.assertEqual(agent.parse_toml(text), {"a": "x", "b": 5, "c": ["p", "q"], "d": True})
		finally:
			sys.modules.pop("tomllib", None)
			if real is not None:
				sys.modules["tomllib"] = real

	def test_the_cups_job_id_is_read_from_lp(self):
		self.assertEqual(agent.cups_job_id("request id is Primacy_2-42 (1 file(s))"), "Primacy_2-42")
		self.assertEqual(agent.cups_job_id("lp: error"), "")

	def test_the_test_card_is_a_card_sized_pdf(self):
		self.assertTrue(PDF.startswith(b"%PDF-1.4"))
		self.assertIn(b"/MediaBox [0 0 242.65 153.07]", PDF)

	def test_the_secret_is_never_in_the_config(self):
		self.assertNotIn(
			"secret", " ".join(k for k in agent.DEFAULTS if k != "keychain_service").replace("keychain", "")
		)


if __name__ == "__main__":
	unittest.main()
