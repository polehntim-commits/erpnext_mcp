# SPDX-License-Identifier: MIT
"""What this deployment is running and whether the deploy landed — read-only, no shell. v0.318.0.

`get_deploy_status` answers the questions the deploy file used to need SSH for: the app version and the git commit the
image was built from (read from files: the app's `.git`, the image's build record), patches listed in `patches.txt`
that the Patch Log has not applied, app doctypes whose JSON is newer than the database (a migrate is due), when this
worker started, the scheduler's last job, and the public phone endpoint's `/farmops/api/health`.

`post_deploy_check` runs the deploy file's checklist as one call — each check pass / warn / fail with its detail. It
never writes, and it never calls anything but its own site's database, files and the one health URL.
"""

from __future__ import annotations

import datetime
import json
import os
import urllib.request

import frappe

from . import __version__, compat

#: The image's build step saves the commit it cloned (fafo-umbrel-store/fafo-erpnext/build/Dockerfile).
IMAGE_HEAD = "/tmp/.erpnext_mcp_head.json"
HEALTH_PATH = "/farmops/api/health"
APP = "erpnext_mcp"


def _repo_root() -> str:
	try:
		return os.path.dirname(frappe.get_app_path(APP))
	except Exception:
		return ""


def git_head(root: str = "") -> dict:
	"""{branch, commit} from `.git` — HEAD, the ref file, else packed-refs. Never runs git."""
	root = root or _repo_root()
	git = os.path.join(root, ".git") if root else ""
	try:
		if os.path.isfile(git):  # a worktree: "gitdir: <path>"
			with open(git, encoding="utf-8") as handle:
				git = handle.read().split(":", 1)[1].strip()
		with open(os.path.join(git, "HEAD"), encoding="utf-8") as handle:
			head = handle.read().strip()
	except Exception:
		return {}
	if not head.startswith("ref:"):
		return {"branch": None, "commit": head[:40] or None}
	ref = head.split(" ", 1)[1].strip()
	for base in (git, os.path.dirname(os.path.dirname(git)) if "/worktrees/" in git else ""):
		if not base:
			continue
		try:
			with open(os.path.join(base, ref), encoding="utf-8") as handle:
				return {"branch": ref.rsplit("/", 1)[-1], "commit": handle.read().strip()[:40]}
		except Exception:
			pass
		try:
			with open(os.path.join(base, "packed-refs"), encoding="utf-8") as handle:
				for line in handle:
					if line.strip().endswith(" " + ref):
						return {"branch": ref.rsplit("/", 1)[-1], "commit": line.split(" ", 1)[0][:40]}
		except Exception:
			pass
	return {"branch": ref.rsplit("/", 1)[-1], "commit": None}


def image_head(path: str = IMAGE_HEAD) -> dict:
	"""The commit the image was built from, where the build saved it; {} otherwise."""
	try:
		with open(path, encoding="utf-8") as handle:
			raw = json.load(handle)
	except Exception:
		return {}
	sha = raw.get("sha") or (raw.get("commit") or {}).get("sha") if isinstance(raw, dict) else None
	return {"commit": str(sha)[:40]} if sha else {}


def pending_patches() -> list:
	"""Patches in `patches.txt` the Patch Log has not recorded."""
	try:
		with open(os.path.join(frappe.get_app_path(APP), "patches.txt"), encoding="utf-8") as handle:
			listed = [line.strip() for line in handle if line.strip() and not line.strip().startswith(("#", "["))]
	except Exception:
		return []
	if not compat.doctype_exists("Patch Log"):
		return listed
	done = set(frappe.db.get_all("Patch Log", pluck="patch", limit=100000) or [])
	return [p for p in listed if p not in done]


def doctypes_needing_migrate(limit: int = 50) -> list:
	"""App doctypes whose JSON `modified` is newer than the database's — the model sync has not run since."""
	out = []
	try:
		root = os.path.join(frappe.get_app_path(APP), APP, "doctype")
		names = sorted(os.listdir(root))
	except Exception:
		return out
	for folder in names:
		path = os.path.join(root, folder, f"{folder}.json")
		if not os.path.isfile(path):
			continue
		try:
			with open(path, encoding="utf-8") as handle:
				body = json.load(handle)
		except Exception:
			continue
		name, modified = body.get("name"), str(body.get("modified") or "")[:19]
		if not name or not modified:
			continue
		in_db = frappe.db.get_value("DocType", name, "modified") if frappe.db.exists("DocType", name) else None
		if in_db is None or str(in_db)[:19] < modified:
			out.append({"doctype": name, "json_modified": modified, "db_modified": str(in_db)[:19] if in_db else None})
		if len(out) >= limit:
			break
	return out


def scheduler() -> dict:
	out = {"last_job_at": None}
	try:
		from frappe.utils.scheduler import is_scheduler_disabled

		out["enabled"] = not is_scheduler_disabled()
	except Exception:
		out["enabled"] = None
	if compat.doctype_exists("Scheduled Job Log"):
		rows = frappe.db.get_all("Scheduled Job Log", fields=["creation"], order_by="creation desc", limit=1) or []
		if rows:
			out["last_job_at"] = str(rows[0]["creation"])[:19]
	return out


def health(timeout: float = 5.0) -> dict:
	"""GET the phones' public `/farmops/api/health` — the Funnel, farmops-fwd and the sidecar in one probe."""
	from . import settings

	base = settings.farmops_public_url()
	if not base:
		return {"checked": False, "note": "FarmOps Public URL is not set in ERPNext MCP Settings."}
	url = base + HEALTH_PATH
	try:
		with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - our own configured URL
			body = response.read(2000).decode("utf-8", "replace")
			try:
				parsed = json.loads(body)
			except ValueError:
				parsed = None
			return {"checked": True, "url": url, "status": response.status,
			        "ok": response.status == 200 and bool((parsed or {}).get("ok")), "answer": parsed or body[:200]}
	except Exception as exc:
		return {"checked": True, "url": url, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


def status(check_health: bool = True) -> dict:
	from .tools import diagnostics

	head = git_head()
	built = image_head()
	data = {
		"site": getattr(frappe.local, "site", None),
		"version": __version__,
		"commit": built.get("commit") or head.get("commit"),
		"commit_source": "image build record" if built.get("commit") else (".git" if head.get("commit") else None),
		"branch": head.get("branch"),
		"pending_patches": pending_patches(),
		"doctypes_needing_migrate": doctypes_needing_migrate(),
		"last_patch_applied": diagnostics._last_patch(),
		"worker_started_at": diagnostics._STARTED_AT or None,
		"scheduler": scheduler(),
		"health": health() if check_health else {"checked": False},
		"note": "This site only — call it on each server (umbrel.local and OML).",
	}
	data["migrate_needed"] = bool(data["pending_patches"] or data["doctypes_needing_migrate"])
	return data


def _check(name: str, ok, detail, warn: bool = False) -> dict:
	return {"check": name, "status": "pass" if ok else ("warn" if warn else "fail"), "detail": detail}


def post_deploy_check(expected_version: str = "", expected_commit: str = "") -> dict:
	"""The deploy file's checklist (4a–4f) as one call. Read-only."""
	from . import backup_status, payroll_settings

	st = status()
	checks = []
	if expected_version:
		checks.append(_check("4a version", st["version"] == expected_version.lstrip("v"),
		                     f"running {st['version']}, expected {expected_version}"))
	if expected_commit:
		known = st.get("commit") or ""
		checks.append(_check("4a commit", bool(known) and known.startswith(expected_commit[:7]),
		                     f"running {known[:12] or 'unknown'}, expected {expected_commit[:12]}", warn=not known))
	checks.append(_check("4a migrate", not st["migrate_needed"],
	                     f"{len(st['pending_patches'])} pending patch(es), "
	                     f"{len(st['doctypes_needing_migrate'])} doctype(s) newer than the database"))
	health_ok = (st["health"] or {}).get("ok")
	checks.append(_check("4a health (Funnel → farmops-fwd → sidecar)", bool(health_ok),
	                     st["health"].get("answer") or st["health"].get("error") or st["health"].get("note"),
	                     warn=not st["health"].get("checked")))
	sched = st["scheduler"]
	checks.append(_check("scheduler", sched.get("enabled") is not False,
	                     f"enabled={sched.get('enabled')}, last job {sched.get('last_job_at')}", warn=True))
	versions = {}
	for key in payroll_settings.KEYS:
		try:
			versions[key] = payroll_settings._body(key, "")[1]
		except Exception:
			versions[key] = None
	checks.append(_check("4b pay settings in force", all(versions.values()), versions, warn=True))
	try:
		from .tools import security_status

		switches = security_status.list_mcp_switches({}).data
		dangerous = switches.get("dangerous_enabled") or []
		checks.append(_check("4d write switches", not dangerous, f"{len(dangerous)} dangerous switch(es) on: "
		                     + ", ".join(str(d.get("tool") if isinstance(d, dict) else d) for d in dangerous[:10]),
		                     warn=True))
	except Exception as exc:  # pragma: no cover
		checks.append(_check("4d write switches", False, f"unreadable: {exc}", warn=True))
	try:
		from .tools import pdf_check

		pdf = pdf_check.test_pdf_rendering({}).data
		checks.append(_check("4f PDF rendering", bool(pdf.get("ok")), pdf.get("error") or "rendered", warn=True))
	except Exception as exc:
		checks.append(_check("4f PDF rendering", False, f"{type(exc).__name__}: {exc}"[:200], warn=True))
	try:
		backups = backup_status.status()
		checks.append(_check("backups", backups["overall"] in ("OK", "ok", "green"), backups["overall"], warn=True))
	except Exception as exc:  # pragma: no cover
		checks.append(_check("backups", False, str(exc)[:200], warn=True))
	order = {"pass": 0, "warn": 1, "fail": 2}
	overall = max((c["status"] for c in checks), key=order.get, default="pass")
	return {"overall": overall, "checks": checks, "deploy_status": st,
	        "not_checked_here": ["4e phone checks (on a device)", "the other server — run this there too"],
	        "at": str(datetime.datetime.now())[:19]}
