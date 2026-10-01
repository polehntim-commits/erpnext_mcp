#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The card print agent: ERPNext's print queue → CUPS → the Evolis Primacy 2.

docs/design/card_print_queue.md §6. One small service on the Mac that has the
printer's driver. It only makes OUTBOUND calls to ERPNext and local calls to
`lp` / `lpstat` / `lpoptions` / `ipptool`; it opens no port.

  every few seconds:  read the printer's state
                      → claim the oldest job for this station (also the heartbeat)
                      → write the PDF to a private temp file, check its hash
                      → lp it, watch it, read how it ended
                      → tell ERPNext: printed, or failed (and whether to retry)
                      → delete the temp file

One job at a time. A printer that is paused, in error or not accepting claims
NOTHING — the cards wait in the queue, in order. If ERPNext cannot be reached it
backs off to 30 seconds and keeps trying.

Standard library only, Python 3.9+: this runs under whatever python3 the Mac
has. Config in ~/.config/cardprint/config.toml; the API key and secret in the
macOS Keychain; the log in ~/Library/Logs/cardprint.log.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

VERSION = "1.0.0"
CONFIG_PATH = os.path.expanduser("~/.config/cardprint/config.toml")
LOG_PATH = os.path.expanduser("~/Library/Logs/cardprint.log")
API_PREFIX = "/api/method/erpnext_mcp.api.card_print."
DEFAULTS = {
	"erpnext_url": "http://umbrel.local:5300",
	"station": "primacy2-main",
	"queue": "Primacy_2",
	"media_option": "PageSize=Card",
	"single_option": "Duplex=NONE",
	"duplex_option": "Duplex=DuplexNoTumble",
	"extra_options": ["fit-to-page"],
	"poll_seconds": 5,
	"backoff_seconds": 30,
	"print_timeout_seconds": 180,
	"keychain_service": "cardprint",
	"keychain_account": "erpnext",
}
READY, PAUSED, ERROR = "Ready", "Paused", "Printer error"
#: `printer-state-reasons` values that are not a problem.
BENIGN_REASONS = ("none", "", "cups-waiting-for-job-completed", "com.apple.print.recoverable-warning")

log = logging.getLogger("cardprint")


# ── config ──────────────────────────────────────────────────────────────────
def parse_toml(text: str) -> dict:
	"""The flat subset this config uses: `key = "string" | number | true | ["a", "b"]`.

	Python 3.11's tomllib is used when present; this is for the 3.9 that ships
	with macOS."""
	try:
		import tomllib  # type: ignore

		return tomllib.loads(text)
	except ImportError:
		pass
	out: dict = {}
	for raw in text.splitlines():
		line = _strip_comment(raw).strip()
		if not line or "=" not in line or line.startswith("["):
			continue
		key, value = (part.strip() for part in line.split("=", 1))
		out[key] = _toml_value(value)
	return out


def _strip_comment(line: str) -> str:
	"""Everything before a `#` that is not inside a quoted string."""
	quote = ""
	for index, char in enumerate(line):
		if quote:
			if char == quote:
				quote = ""
		elif char in "\"'":
			quote = char
		elif char == "#":
			return line[:index]
	return line


def _toml_value(value: str):
	if value.startswith("[") and value.endswith("]"):
		return [_toml_value(item.strip()) for item in value[1:-1].split(",") if item.strip()]
	if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
		return value[1:-1]
	if value in ("true", "false"):
		return value == "true"
	try:
		return int(value)
	except ValueError:
		try:
			return float(value)
		except ValueError:
			return value


def load_config(path: str = CONFIG_PATH) -> dict:
	config = dict(DEFAULTS)
	if os.path.exists(path):
		with open(path, encoding="utf-8") as handle:
			config.update(parse_toml(handle.read()))
	if isinstance(config.get("extra_options"), str):
		config["extra_options"] = [config["extra_options"]]
	config["erpnext_url"] = str(config["erpnext_url"]).rstrip("/")
	return config


# ── a seam for tests: every outside call goes through one of these ──────────
class Shell:
	def run(self, args: list, timeout: int = 30) -> tuple:
		"""(returncode, stdout, stderr)."""
		try:
			done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
		except FileNotFoundError:
			return 127, "", f"{args[0]}: not found"
		except subprocess.TimeoutExpired:
			return 124, "", f"{args[0]}: timed out"
		return done.returncode, done.stdout, done.stderr


def keychain_secret(shell: Shell, service: str, account: str) -> str:
	"""`key:secret`, as stored by install.sh. Never logged."""
	code, out, _err = shell.run(["security", "find-generic-password", "-s", service, "-a", account, "-w"])
	return out.strip() if code == 0 else ""


class Server:
	"""ERPNext, over `Authorization: token key:secret`."""

	def __init__(self, base_url: str, token: str, timeout: int = 20):
		self.base_url = base_url
		self.token = token
		self.timeout = timeout

	def call(self, method: str, **params) -> dict:
		body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
		request = urllib.request.Request(
			self.base_url + API_PREFIX + method,
			data=body,
			method="POST",
			headers={
				"Authorization": f"token {self.token}",
				"Content-Type": "application/json",
				"Accept": "application/json",
			},
		)
		try:
			with urllib.request.urlopen(request, timeout=self.timeout) as response:
				payload = json.loads(response.read().decode() or "{}")
		except urllib.error.HTTPError as exc:
			detail = exc.read().decode(errors="replace")[:400]
			raise ServerRefused(exc.code, _server_message(detail)) from exc
		except (urllib.error.URLError, OSError, ValueError) as exc:
			raise ServerUnreachable(str(exc)) from exc
		return payload.get("message") or {}


class ServerUnreachable(Exception):
	pass


class ServerRefused(Exception):
	def __init__(self, status: int, message: str):
		super().__init__(f"HTTP {status}: {message}")
		self.status = status


def _server_message(body: str) -> str:
	try:
		data = json.loads(body)
		messages = json.loads(data.get("_server_messages") or "[]")
		if messages:
			return str(json.loads(messages[0]).get("message") or messages[0])[:300]
		return str(data.get("exception") or data.get("message") or body)[:300]
	except Exception:
		return body[:300]


# ── the printer ─────────────────────────────────────────────────────────────
def printer_state(shell: Shell, queue: str) -> tuple:
	"""(Ready | Paused | Printer error, a sentence). Reads only."""
	code, out, err = shell.run(["lpstat", "-p", queue])
	if code != 0:
		return ERROR, f"no CUPS queue called {queue} ({(err or out).strip()[:120]})"
	first = out.strip().splitlines()[0] if out.strip() else ""
	if "disabled" in first or "paused" in first.lower():
		detail = " ".join(line.strip() for line in out.strip().splitlines()[1:2])
		return PAUSED, f"the queue {queue} is paused" + (f": {detail}" if detail else "")
	code, out, _err = shell.run(["lpstat", "-a", queue])
	if code == 0 and "not accepting" in out:
		return PAUSED, f"the queue {queue} is not accepting jobs"
	code, out, _err = shell.run(["lpoptions", "-p", queue])
	match = re.search(r"printer-state-reasons=(\S+)", out or "")
	reasons = [r for r in (match.group(1).split(",") if match else []) if r not in BENIGN_REASONS]
	reasons = [r for r in reasons if not r.endswith("-report")]
	if reasons:
		return ERROR, "the printer reports " + ", ".join(reasons)
	return READY, ""


def lp_arguments(config: dict, copies: int, sides: str, path: str) -> list:
	args = ["lp", "-d", config["queue"], "-n", str(int(copies)), "-o", config["media_option"]]
	side = config["duplex_option"] if sides == "Dual" else config["single_option"]
	if side:
		args += ["-o", side]
	for option in config.get("extra_options") or []:
		args += ["-o", str(option)]
	return [*args, path]


def cups_job_id(lp_output: str) -> str:
	"""`request id is Primacy_2-42 (1 file(s))` → `Primacy_2-42`."""
	match = re.search(r"request id is (\S+)", lp_output or "")
	return match.group(1) if match else ""


def job_final_state(shell: Shell, cups_job: str) -> str:
	"""completed / aborted / canceled / stopped / unknown, via ipptool."""
	number = cups_job.rsplit("-", 1)[-1]
	_code, out, _err = shell.run(
		["ipptool", "-tv", f"ipp://localhost/jobs/{number}", "get-job-attributes.test"], timeout=20
	)
	match = re.search(r"job-state \(enum\) = (\S+)", out or "")
	return match.group(1) if match else "unknown"


def wait_for_job(shell: Shell, config: dict, cups_job: str, sleep=time.sleep, clock=time.monotonic) -> tuple:
	"""(success, error, retryable) once the job leaves the queue or the timeout passes."""
	deadline = clock() + int(config["print_timeout_seconds"])
	queue = config["queue"]
	while clock() < deadline:
		_code, out, _err = shell.run(["lpstat", "-W", "not-completed", "-o", queue])
		if cups_job not in (out or ""):
			final = job_final_state(shell, cups_job)
			if final in ("completed", "unknown"):
				state, message = printer_state(shell, queue)
				if final == "unknown" and state != READY:
					return False, f"the job left the queue and {message}", True
				return True, "", False
			return False, f"CUPS reports the job {final}", True
		state, message = printer_state(shell, queue)
		if state == ERROR:
			shell.run(["cancel", cups_job])
			return False, message, True
		sleep(2)
	shell.run(["cancel", cups_job])
	return False, f"the card had not printed after {config['print_timeout_seconds']} seconds", True


# ── one pass ────────────────────────────────────────────────────────────────
def print_job(shell: Shell, config: dict, job: dict, sleep=time.sleep) -> dict:
	"""Print one claimed job. Returns the arguments for complete_card_print_job."""
	pdf = base64.b64decode(job.get("artwork_base64") or "")
	if not pdf.startswith(b"%PDF"):
		return {"success": 0, "error": "the artwork is not a PDF", "retryable": 0}
	if job.get("artwork_sha256") and hashlib.sha256(pdf).hexdigest() != job["artwork_sha256"]:
		return {"success": 0, "error": "the artwork did not arrive intact (hash mismatch)", "retryable": 0}
	directory = tempfile.mkdtemp(prefix="cardprint-")
	path = os.path.join(directory, re.sub(r"[^A-Za-z0-9._-]", "_", job.get("file_name") or "card.pdf"))
	try:
		with open(path, "wb") as handle:
			handle.write(pdf)
		os.chmod(path, 0o600)
		args = lp_arguments(config, job.get("copies") or 1, job.get("sides") or "Single", path)
		log.info("printing %s: %s", job.get("name"), " ".join(args[:-1]))
		code, out, err = shell.run(args, timeout=60)
		cups_job = cups_job_id(out)
		if code != 0 or not cups_job:
			return {
				"success": 0,
				"error": f"lp refused the job: {(err or out).strip()[:300]}",
				"retryable": 0,
			}
		success, error, retryable = wait_for_job(shell, config, cups_job, sleep=sleep)
		return {
			"success": 1 if success else 0,
			"error": error or None,
			"retryable": 1 if retryable else 0,
			"cups_job": cups_job,
		}
	finally:
		shutil.rmtree(directory, ignore_errors=True)


def run_once(server: Server, shell: Shell, config: dict, sleep=time.sleep) -> str:
	"""One pass. Returns 'printed', 'failed', 'idle' or 'not-ready'."""
	state, message = printer_state(shell, config["queue"])
	answer = server.call(
		"claim_next_card_print_job",
		print_station=config["station"],
		printer_state=state,
		printer_message=message,
		agent_version=VERSION,
	)
	job = answer.get("job")
	if not job:
		if state != READY:
			log.info("not claiming: %s", message)
			return "not-ready"
		return "idle"
	log.info(
		"claimed %s (%s for %s, attempt %s)",
		job.get("name"),
		job.get("job_type"),
		job.get("reference_title"),
		job.get("attempts"),
	)
	result = print_job(shell, config, job, sleep=sleep)
	_report(server, job["name"], result, sleep)
	log.info("%s: %s", job.get("name"), "printed" if result["success"] else f"failed — {result.get('error')}")
	return "printed" if result["success"] else "failed"


def _report(server: Server, name: str, result: dict, sleep) -> None:
	"""The result must reach ERPNext; if it cannot, the stuck-job sweep requeues it."""
	for attempt in range(5):
		try:
			server.call("complete_card_print_job", name=name, **result)
			return
		except ServerRefused as exc:
			log.error("could not report %s: %s", name, exc)
			return
		except ServerUnreachable as exc:
			log.warning("could not report %s (try %s): %s", name, attempt + 1, exc)
			sleep(5)


def run_forever(server: Server, shell: Shell, config: dict) -> None:
	log.info(
		"card print agent %s — station %s, queue %s, %s",
		VERSION,
		config["station"],
		config["queue"],
		config["erpnext_url"],
	)
	while True:
		pause = int(config["poll_seconds"])
		try:
			outcome = run_once(server, shell, config)
			if outcome in ("printed", "failed"):
				pause = 1
		except ServerUnreachable as exc:
			log.warning("ERPNext is unreachable (%s); trying again in %ss", exc, config["backoff_seconds"])
			pause = int(config["backoff_seconds"])
		except ServerRefused as exc:
			log.error("ERPNext refused the call: %s", exc)
			pause = int(config["backoff_seconds"])
		except Exception:  # a bug must not stop the service; launchd would only restart it
			log.exception("unexpected error")
			pause = int(config["backoff_seconds"])
		time.sleep(pause)


# ── a test card (never printed without being asked) ─────────────────────────
def test_card_pdf() -> bytes:
	"""A one-page 85.6 × 54 mm PDF saying TEST CARD, written by hand (no libraries)."""
	width, height = 242.65, 153.07
	stream = (
		"BT /F1 18 Tf 20 96 Td (Farm Ops card print) Tj ET\n"
		"BT /F1 12 Tf 20 70 Td (TEST CARD) Tj ET\n"
		"BT /F1 8 Tf 20 20 Td (If this fills the card and reads left to right, the options are right.) Tj ET\n"
		f"0.5 w 6 6 {width - 12:.2f} {height - 12:.2f} re S\n"
	).encode()
	objects = [
		b"<< /Type /Catalog /Pages 2 0 R >>",
		b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
		(
			f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width:.2f} {height:.2f}] /Contents 4 0 R "
			"/Resources << /Font << /F1 5 0 R >> >> >>"
		).encode(),
		b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
		b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
	]
	out = b"%PDF-1.4\n"
	offsets = []
	for index, body in enumerate(objects, start=1):
		offsets.append(len(out))
		out += b"%d 0 obj\n" % index + body + b"\nendobj\n"
	xref = len(out)
	out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
	for offset in offsets:
		out += b"%010d 00000 n \n" % offset
	out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
	return out


def print_test_card(shell: Shell, config: dict) -> dict:
	job = {
		"name": "TEST",
		"copies": 1,
		"sides": "Single",
		"file_name": "cardprint-test.pdf",
		"artwork_base64": base64.b64encode(test_card_pdf()).decode(),
	}
	return print_job(shell, config, job)


# ── entry ───────────────────────────────────────────────────────────────────
def setup_logging(verbose: bool = False) -> None:
	log.setLevel(logging.INFO)
	formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
	try:
		os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
		handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=5)
		handler.setFormatter(formatter)
		log.addHandler(handler)
	except OSError:
		verbose = True
	if verbose:
		stream = logging.StreamHandler()
		stream.setFormatter(formatter)
		log.addHandler(stream)


def main(argv=None) -> int:
	parser = argparse.ArgumentParser(description="ERPNext card print agent")
	parser.add_argument("--config", default=CONFIG_PATH)
	parser.add_argument("--once", action="store_true", help="one pass, then exit")
	parser.add_argument(
		"--status", action="store_true", help="show the printer state and config; print nothing"
	)
	parser.add_argument(
		"--test-card", action="store_true", help="print one local test card (asks nothing of ERPNext)"
	)
	parser.add_argument("--verbose", action="store_true")
	options = parser.parse_args(argv)
	setup_logging(options.verbose or options.status or options.test_card or options.once)
	config = load_config(options.config)
	shell = Shell()

	if options.status:
		state, message = printer_state(shell, config["queue"])
		print(f"station {config['station']}  queue {config['queue']}  server {config['erpnext_url']}")
		print(f"printer: {state}" + (f" — {message}" if message else ""))
		print("lp would run: " + " ".join(lp_arguments(config, 1, "Single", "<file>")))
		has_token = bool(keychain_secret(shell, config["keychain_service"], config["keychain_account"]))
		print(
			"credentials: "
			+ ("found in the Keychain" if has_token else "NOT in the Keychain — run install.sh")
		)
		return 0
	if options.test_card:
		result = print_test_card(shell, config)
		print("test card: " + ("printed" if result["success"] else f"failed — {result.get('error')}"))
		return 0 if result["success"] else 1

	token = os.environ.get("CARDPRINT_TOKEN") or keychain_secret(
		shell, config["keychain_service"], config["keychain_account"]
	)
	if ":" not in token:
		log.error(
			"no ERPNext API key in the Keychain (service %s). Run install.sh.", config["keychain_service"]
		)
		return 2
	server = Server(config["erpnext_url"], token)
	if options.once:
		try:
			print(run_once(server, shell, config))
		except (ServerUnreachable, ServerRefused) as exc:
			print(f"error: {exc}")
			return 1
		return 0
	run_forever(server, shell, config)
	return 0


if __name__ == "__main__":
	sys.exit(main())
