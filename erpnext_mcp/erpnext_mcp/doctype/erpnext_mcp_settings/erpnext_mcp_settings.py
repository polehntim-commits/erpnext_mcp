# SPDX-License-Identifier: MIT
"""Controller for ERPNext MCP Settings.

Validation here is deliberately opinionated, because every field on this form is
a security control and the failure modes are all silent. A typo'd CIDR that is
skipped at request time, a master switch on with no token, an allowlist someone
emptied while tidying — each of those either locks out a working client or opens
one up, and neither announces itself. So the form refuses to save them.

The token is generated server-side and returned to the operator's browser
exactly once. It cannot be read back afterwards: the field is a Password, so
Frappe stores it in the encrypted auth table and the form only ever shows a
placeholder. That is the correct behaviour for a credential, and it is why the
button says "Generate New Token" rather than "Show Token".
"""

import ipaddress

import frappe
from frappe import _
from frappe.model.document import Document

from erpnext_mcp import registry, settings

#: 48 hex characters, ~192 bits from `frappe.generate_hash` (which is
#: `secrets.token_hex` underneath). Long enough that the constant-time compare
#: in security.py is the least interesting part of the attack.
TOKEN_LENGTH = 48


class ERPNextMCPSettings(Document):
	def validate(self):
		self._validate_cidrs()
		self._validate_token_present()
		self._validate_system_user()
		self._validate_farmops_url()

	def _validate_farmops_url(self) -> None:
		"""`https://<host>`, nothing after it. The phone appends /farmops/api itself."""
		from urllib.parse import urlsplit

		raw = str(self.get("farmops_public_url") or "").strip().rstrip("/")
		self.farmops_public_url = raw
		if not raw:
			return
		parsed = urlsplit(raw)
		if parsed.scheme != "https" or not parsed.hostname:
			frappe.throw(
				f"Farm Ops Public URL must be an https:// address, e.g. https://erp.tail1234.ts.net — got {raw!r}."
			)
		if parsed.path.strip("/") or parsed.query or parsed.fragment:
			frappe.throw(
				"Farm Ops Public URL must not carry a path — the app adds /farmops/api itself. "
				f"Use {parsed.scheme}://{parsed.netloc}."
			)

	def on_update(self):
		self._warn_about_enabled_mutations()

	def _validate_cidrs(self) -> None:
		"""Every entry must parse, and the list must not be empty while enabled.

		`security.py` skips an unparseable entry at request time so one typo
		cannot take the whole gate down — but skipping silently is only
		acceptable because this check exists to stop the typo being saved in the
		first place.
		"""
		# None means the field has never been set, so the declared default is
		# what will apply. An empty string means somebody deliberately cleared
		# it, which is a different thing and is caught below.
		raw = settings.DEFAULT_CIDRS if self.allowed_cidrs is None else self.allowed_cidrs
		entries = _split(raw)
		bad = []
		for entry in entries:
			try:
				ipaddress.ip_network(entry, strict=False)
			except ValueError:
				bad.append(entry)
		if bad:
			frappe.throw(
				_("Not valid CIDR notation: {0}. Use forms like 192.168.1.0/24 or 10.0.0.5/32.").format(
					", ".join(bad)
				),
				title=_("Invalid Allowed CIDRs"),
			)
		if settings.as_bool(self.enabled) and not entries:
			frappe.throw(
				_(
					"Allowed CIDRs is empty, which denies every caller. Enter at "
					"least one block (127.0.0.1/32 for same-host clients), or "
					"untick Enabled."
				),
				title=_("No Callers Allowed"),
			)

	def _validate_token_present(self) -> None:
		if not settings.as_bool(self.enabled):
			return
		# A brand-new value is still in the field; a saved one has to be fetched
		# out of the auth table.
		token = self.auth_token or settings.auth_token()
		if not token:
			frappe.throw(
				_("Generate an auth token before enabling the MCP endpoint."),
				title=_("No Token"),
			)

	def _validate_system_user(self) -> None:
		if not (settings.as_bool(self.require_user_context) and self.mcp_system_user):
			return
		enabled = frappe.db.get_value("User", self.mcp_system_user, "enabled")
		if not enabled:
			frappe.throw(
				_("MCP System User {0} is disabled. Mutating tools would fail on every call.").format(
					self.mcp_system_user
				),
				title=_("Disabled User"),
			)

	def _warn_about_enabled_mutations(self) -> None:
		"""Say out loud which write tools are live.

		Not a confirmation dialog — the operator ticked the box on purpose. But
		"submit_journal_entry is on" is worth reading once in plain language,
		because it is the switch that lets an AI client move a balance.

		THE DEFAULT-ON INSTALLERS ARE SKIPPED. `registry.DEFAULT_ON_MUTATING_TOOLS`
		names them and argues for each. Including them would fire this warning on
		every save of a stock configuration, and a warning that fires every time is
		a warning nobody reads — which would cost exactly the thing it exists for.
		"""
		live = [
			name
			for name in registry.MUTATING_TOOLS
			if name not in registry.DEFAULT_ON_MUTATING_TOOLS and settings.as_bool(self.get(f"allow_{name}"))
		]
		if not (settings.as_bool(self.enabled) and live):
			return
		frappe.msgprint(
			_("Mutating MCP tools are live: {0}. Every call is recorded in MCP Action Log.").format(
				", ".join(f"<b>{name}</b>" for name in live)
			),
			title=_("AI Write Access Enabled"),
			indicator="orange",
		)

	@frappe.whitelist()
	def generate_token(self) -> dict:
		"""Mint a new bearer token, save it, and return it once.

		Rotating is the same operation as creating: the old token stops working
		the moment this saves, which is the behaviour you want when the reason
		you pressed the button is that the old one leaked.
		"""
		frappe.only_for("System Manager")
		token = frappe.generate_hash(length=TOKEN_LENGTH)
		self.auth_token = token
		self.token_generated_on = frappe.utils.now()
		self.save()
		return {
			"token": token,
			"generated_on": self.token_generated_on,
			"endpoint": "/api/method/erpnext_mcp.mcp.handle",
			"note": (
				"Copy this now — it is stored encrypted and cannot be shown again. "
				"Any client using the previous token has just stopped working."
			),
		}


def _split(raw: str) -> list[str]:
	out = []
	for chunk in str(raw or "").replace("\n", ",").split(","):
		entry = chunk.split("#", 1)[0].strip()
		if entry:
			out.append(entry)
	return out
