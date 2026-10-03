# SPDX-License-Identifier: MIT
"""Plaid Hosted Link for a worker's own bank account: Auth + Identity, then forget. v0.225.0.

docs/design/direct_deposit_setup.md §1.5, §4. Standard library only — no Plaid
SDK on the server, and none in the app: the phone opens the Hosted Link URL in
`ASWebAuthenticationSession`. The server creates the link token, later reads the
session's public token, exchanges it, reads Auth (routing + account) and Identity
(owner names), compares, and ALWAYS removes the Item. Nothing of Plaid's is kept
except the answer: matched or not.

Credentials: `plaid_client_id`, `plaid_secret` (Password) and `plaid_env` on
ERPNext MCP Settings — a separate Plaid use case from Bank Bridge's (§4).
"""

from __future__ import annotations

import json
import urllib.request

import frappe

from . import settings

HOSTS = {"sandbox": "https://sandbox.plaid.com", "production": "https://production.plaid.com"}
CALLBACK_SCHEME = "farmops"
COMPLETION_URI = f"{CALLBACK_SCHEME}://plaid-complete"
TIMEOUT = 20
TOKEN_TTL = 4 * 3600
#: Tests replace this: (url, body dict) → response dict.
TRANSPORT = None
_LOCAL_TOKENS: dict = {}


class PlaidError(Exception):
	"""Plaid answered with an error, or could not be reached. The message names the code only."""


def _credentials() -> tuple:
	client_id = str(settings._value("plaid_client_id") or "").strip()
	try:
		secret = settings.get_settings().get_password("plaid_secret", raise_exception=False) or ""
	except Exception:
		secret = ""
	env = str(settings._value("plaid_env") or "sandbox").strip().lower()
	if not client_id or not secret:
		raise PlaidError("Plaid is not configured (client id and secret on ERPNext MCP Settings).")
	return client_id, secret.strip(), HOSTS.get(env, HOSTS["sandbox"])


def _post(path: str, body: dict) -> dict:
	client_id, secret, host = _credentials()
	payload = {"client_id": client_id, "secret": secret, **body}
	url = host + path
	if TRANSPORT is not None:
		answer = TRANSPORT(url, payload)
	else:  # pragma: no cover - the network
		request = urllib.request.Request(
			url,
			data=json.dumps(payload).encode(),
			headers={"Content-Type": "application/json"},
			method="POST",
		)
		try:
			with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
				answer = json.loads(response.read().decode() or "{}")
		except urllib.error.HTTPError as exc:
			try:
				answer = json.loads(exc.read().decode() or "{}")
			except Exception:
				answer = {"error_code": f"HTTP {exc.code}"}
		except Exception as exc:
			raise PlaidError(f"Plaid could not be reached ({type(exc).__name__})") from None
	if answer.get("error_code"):
		raise PlaidError(f"Plaid refused: {answer.get('error_code')}")
	return answer


def _token_key(employee: str) -> str:
	return f"erpnext_mcp:plaid_link:{employee}"


def _remember(employee: str, token: str) -> None:
	try:
		frappe.cache().set_value(_token_key(employee), token, expires_in_sec=TOKEN_TTL)
	except Exception:
		_LOCAL_TOKENS[_token_key(employee)] = token


def _recall(employee: str) -> str:
	try:
		value = frappe.cache().get_value(_token_key(employee))
	except Exception:
		value = None
	return str(value or _LOCAL_TOKENS.get(_token_key(employee)) or "")


def _forget(employee: str) -> None:
	try:
		frappe.cache().delete_value(_token_key(employee))
	except Exception:
		pass
	_LOCAL_TOKENS.pop(_token_key(employee), None)


def start(employee: str) -> dict:
	answer = _post(
		"/link/token/create",
		{
			"client_name": "Farm Ops",
			"language": "en",
			"country_codes": ["US"],
			"user": {"client_user_id": employee},
			"products": ["auth", "identity"],
			"hosted_link": {"completion_redirect_uri": COMPLETION_URI, "is_mobile_app": True},
		},
	)
	if not answer.get("hosted_link_url") or not answer.get("link_token"):
		raise PlaidError("Plaid did not return a Hosted Link URL")
	_remember(employee, answer["link_token"])
	return {
		"hosted_link_url": answer["hosted_link_url"],
		"request_id": str(answer.get("request_id") or "")[:140],
	}


def _public_token(link_token: str) -> str:
	answer = _post("/link/token/get", {"link_token": link_token})
	for session in answer.get("link_sessions") or []:
		results = session.get("results") or {}
		for item in results.get("item_add_results") or []:
			if item.get("public_token"):
				return item["public_token"]
		success = session.get("on_success") or {}
		if success.get("public_token"):
			return success["public_token"]
	return ""


def finish(employee: str) -> dict:
	"""`{"state": "pending"}`, or `{"ach": [{routing, account}], "owner_names": [...]}` — then forgets."""
	link_token = _recall(employee)
	if not link_token:
		raise PlaidError("no Plaid session is open for you; start the verification again")
	public = _public_token(link_token)
	if not public:
		return {"state": "pending"}
	access = _post("/item/public_token/exchange", {"public_token": public}).get("access_token") or ""
	try:
		auth = _post("/auth/get", {"access_token": access})
		identity = _post("/identity/get", {"access_token": access})
	finally:
		try:
			_post("/item/remove", {"access_token": access})
		except Exception:  # pragma: no cover - best effort; the token is not stored anywhere
			pass
		_forget(employee)
	ach = [
		{"routing": row.get("routing"), "account": row.get("account")}
		for row in ((auth.get("numbers") or {}).get("ach") or [])
	]
	names = [
		name
		for account in identity.get("accounts") or []
		for owner in account.get("owners") or []
		for name in owner.get("names") or []
	]
	return {"state": "done", "ach": ach, "owner_names": names}
