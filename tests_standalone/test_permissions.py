# SPDX-License-Identifier: MIT
"""Entity scoping for the two doctypes Frappe's own mechanism never reached.

THE BUG THESE TESTS PIN DOWN. `create_mobile_user` promises that one User
Permission on Company "restricts EVERY document that links to a Company, across
every doctype". True — and the load-bearing clause is *"that links to a
Company"*. Of the thirty-odd doctypes this app ships, exactly two do not:

  * `Housing Assignment`, which `roles.py` READ-grants to Field Worker, Foreman,
    Compliance Officer and Family Member and FULL-grants to Farm Manager. Before
    v0.17.1 a field worker scoped to one entity could list every camp bed
    assignment on the site — names, cabins, wage-deduction status.
  * `Family`, the family-office member register, READ-granted to Family Member.

FOUR CLAIMS.

1. `TheHoleIsClosed` — a scoped user's query is restricted, and the restriction
   names the right companies.
2. `TheRuleIsNarrowerThanTheBanItReplaced` — these hooks may only ever name
   doctypes THIS APP CREATED. That is the invariant the old blanket ban on
   `permission_query_conditions` was standing in for, and it is asserted
   directly now rather than by proxy.
3. `NobodyIsLockedOut` — an unrestricted user (an operator, Administrator, a
   migration) gets no condition at all, and every failure path returns the
   unrestricted answer. A query-conditions hook that raises does not fail one
   query; it fails every list view of that doctype for everybody, forever.
4. `TheDocumentCheckAgrees` — the single-document read is scoped too. A doctype
   filtered out of a list but readable at its own URL is not scoped, it is tidy.
"""

import frappe

from erpnext_mcp import permissions, roles

from .fixtures import MAIN, OTHER, V12TestCase
from .harness import STORE

SCOPED_USER = "ana@example.test"
UNSCOPED_USER = "operator@example.test"


class PermissionsTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		roles.install_roles()
		STORE.seed(
			"User",
			[
				{"name": SCOPED_USER, "enabled": 1, "full_name": "Ana Ramos"},
				{"name": UNSCOPED_USER, "enabled": 1, "full_name": "Operator"},
			],
		)
		# Ana sees ONE entity. The operator has no User Permission at all, which
		# in Frappe means every entity — the case that must keep working.
		STORE.seed(
			"User Permission",
			[
				{
					"name": "UP-ANA-MAIN",
					"user": SCOPED_USER,
					"allow": "Company",
					"for_value": MAIN,
					"apply_to_all_doctypes": 1,
					"is_default": 1,
				}
			],
		)
		STORE.seed(
			"Housing Unit",
			[
				{"name": "MC-Cabin-01", "unit_name": "MC-Cabin-01", "owning_entity": MAIN},
				{"name": "HL-Cabin-09", "unit_name": "HL-Cabin-09", "owning_entity": OTHER},
			],
		)
		STORE.seed(
			"Housing Assignment",
			[
				{"name": "HA-MINE", "unit": "MC-Cabin-01", "employee": "EMP-ANA", "status": "Current"},
				{"name": "HA-THEIRS", "unit": "HL-Cabin-09", "employee": "EMP-BEN", "status": "Current"},
				{"name": "HA-ORPHAN", "unit": "", "employee": "EMP-NOBODY", "status": "Current"},
			],
		)


# ── 1 ───────────────────────────────────────────────────────────────────────
class TheHoleIsClosed(PermissionsTestCase):
	def test_a_scoped_user_gets_a_condition_at_all(self):
		"""Before v0.17.1 this returned nothing, for everybody, always."""
		condition = permissions.housing_assignment_query(SCOPED_USER)
		self.assertTrue(condition, "a scoped user got no restriction on Housing Assignment")

	def test_the_condition_names_the_users_entity_and_not_the_other_one(self):
		condition = permissions.housing_assignment_query(SCOPED_USER)
		self.assertIn(MAIN, condition)
		self.assertNotIn(OTHER, condition)

	def test_it_restricts_through_the_unit_because_the_row_has_no_company_of_its_own(self):
		"""The whole reason the framework could not do this."""
		condition = permissions.housing_assignment_query(SCOPED_USER)
		self.assertIn("tabHousing Unit", condition)
		self.assertIn("owning_entity", condition)
		self.assertIn("`tabHousing Assignment`.`unit`", condition)

	def test_the_family_register_is_scoped_through_its_related_party(self):
		condition = permissions.family_query(SCOPED_USER)
		self.assertIn("tabRelated Party", condition)
		self.assertIn(MAIN, condition)

	def test_a_mismatched_pair_does_not_widen_access_to_both_entities(self):
		"""`parcel` is consulted only where `unit` is blank. An assignment whose
		unit is one entity's and whose parcel is another's is a data error, and
		the reading that shows it to both is the wrong one."""
		condition = permissions.housing_assignment_query(SCOPED_USER)
		parcel_clause = condition.split("`tabHousing Assignment`.`parcel`")[0]
		self.assertIn("`tabHousing Assignment`.`unit` IS NULL", parcel_clause)

	def test_a_row_with_no_unit_at_all_stays_visible(self):
		"""Same call as `api/guard.scoped`: a row with no entity is a data problem,
		not another entity's secret, and hiding it makes it invisible not fixed."""
		condition = permissions.housing_assignment_query(SCOPED_USER)
		self.assertIn("IS NULL OR", condition)


# ── 2 ───────────────────────────────────────────────────────────────────────
class TheRuleIsNarrowerThanTheBanItReplaced(PermissionsTestCase):
	def test_these_hooks_only_ever_name_doctypes_this_app_created(self):
		"""THE INVARIANT THE OLD BLANKET BAN WAS A PROXY FOR.

		`hooks.py` promises that installing this app cannot change how anything
		already on the site behaves. Restricting a doctype that would not exist
		without this app cannot break that promise; restricting somebody else's
		would, silently, on every site that installed us.
		"""
		from erpnext_mcp import hooks

		ours = set(_app_doctypes())
		self.assertTrue(ours, "could not enumerate this app's doctypes")
		for hook in (hooks.permission_query_conditions, hooks.has_permission):
			for doctype in hook:
				self.assertIn(
					doctype,
					ours,
					f"{doctype} is not a doctype this app created — scoping it would change the "
					"behaviour of a site that installed us, which hooks.py promises never happens",
				)

	def test_both_hooks_cover_exactly_the_same_doctypes(self):
		"""A list filtered but a document readable is not scoping, it is tidiness."""
		from erpnext_mcp import hooks

		self.assertEqual(
			set(hooks.permission_query_conditions),
			set(hooks.has_permission),
		)
		self.assertEqual(set(hooks.permission_query_conditions), set(permissions.scoped_doctypes()))

	def test_every_other_doctype_is_left_to_frappe_because_it_links_to_company(self):
		"""The two scoped here are the ONLY two without a Link to Company. If a
		later release adds one that should have been scoped, this fails and
		somebody has to decide."""
		unscoped = []
		for doctype in _app_doctypes():
			meta = _meta(doctype)
			if meta is None or meta.get("istable") or meta.get("issingle"):
				continue
			links = [
				field["fieldname"]
				for field in meta["fields"]
				if field.get("fieldtype") == "Link" and field.get("options") == "Company"
			]
			if not links and doctype not in permissions.scoped_doctypes():
				unscoped.append(doctype)
		# These carry no entity because they legitimately have none: a system log,
		# two upload staging tables, and — from v0.19.2 — two site-wide VOCABULARY
		# masters. `Mobile Access Grant` is NOT among them: it carries
		# `preferred_company`, a Link to Company, so Frappe scopes it like
		# everything else. That is worth knowing, because the audit that found this
		# hole originally grepped for a field NAMED "company" and would have
		# wrongly flagged the grant too; what matters is the field's TYPE, not its
		# name.
		#
		# THE TWO v0.19.2 ADDITIONS ARE DELIBERATE AND ARE NOT A HOLE. A Compliance
		# Regime is the word "WPS" and a Training Type is the course "OSHA 30";
		# neither belongs to an entity, both are seeded identically on every site,
		# and neither carries one fact about anybody's operation. Scoping them per
		# company would mean a user of entity A being unable to READ THE NAME of
		# the regime that entity B's records are tagged with — which would break
		# every packet and every filter without protecting anything, since there is
		# nothing on these rows to protect. The records that DO carry the operating
		# facts — Employee Training Record, Compliance Alert — link to Company and
		# are scoped by Frappe exactly as before.
		#
		# THE v0.21.0 ADDITION IS THE SAME ARGUMENT A THIRD TIME. An Inspection
		# Template is the SHAPE of a visit — "a close-down is these six sections" —
		# and carries no fact about anybody's operation: not a cabin, not a
		# worker, not a date. The record that carries those is the Inspection
		# SESSION, which links to Company and is scoped by Frappe exactly as
		# everything else. Scoping the template would mean a user of entity A
		# being unable to read the definition of the form that entity B's
		# sessions were worked from — which breaks every packet that prints a
		# session's provenance while protecting nothing, because there is nothing
		# on the row to protect.
		#
		# THE v0.22.0 ADDITION IS THE STRONGEST CASE OF THE FOUR. A Compliance
		# Rule is the DEFINITION of a condition — "a certificate inside its
		# renewal window is worth knowing about" — and it is a statement about a
		# regulation rather than about an operation. It names no cabin, no
		# worker, no certificate and no date. The rows that carry those are the
		# Compliance ALERTS the rule raises, and every one of them links to
		# Company and is scoped by Frappe exactly as before.
		#
		# Scoping the rule itself would also be actively wrong rather than merely
		# unnecessary: one rule raises alerts across every entity on the site, so
		# a per-company rule row would mean either duplicating all thirteen per
		# entity — thirteen places to update when OR-OSHA renumbers a citation —
		# or having a rule owned by one entity silently governing another's
		# records. Per-company narrowing is available and belongs where it can be
		# read: a `scope_filters` entry on the rule, which says so on the record
		# an auditor is looking at.
		#
		# THE v0.75.0 ADDITION IS THE SAME ARGUMENT ABOUT A VENDOR. A Merchant
		# Alias says that a till which prints "SIATAPING" belongs to Sawyer's Ace
		# Hardware. That is a fact about a shop's receipt printer, not about
		# anybody's operation: it names no amount, no date, no employee and no
		# purchase. The rows that carry those are the Expense RECEIPTS the alias
		# resolves, every one of which links to Company and is scoped by Frappe
		# exactly as before.
		#
		# Scoping it would also be wrong rather than merely unnecessary, for the
		# reason the Compliance Rule paragraph gives: a Supplier is already
		# site-wide in ERPNext, so a per-company alias would mean either the same
		# spelling taught once per entity — two rows that will eventually
		# disagree about one vendor — or an alias owned by one entity quietly
		# resolving another's receipts. And the whole design of the register
		# rests on one alias key resolving to exactly one Supplier, which a
		# company column would break by making the key non-unique.
		# THE v0.79.0 ADDITION IS THE SAME ARGUMENT ABOUT A FORM. A Wizard
		# Definition is the SHAPE of a question — "ask when it happened, then who
		# was hurt, and show the injury step only when it was not a near miss" —
		# and a shape belongs to no entity. It names no incident, no employee and
		# no company; the records it produces carry all three and are scoped by
		# Frappe exactly as before.
		#
		# Scoping it would be actively wrong for the reason the Compliance Rule
		# paragraph gives, plus one of its own: the docname IS the wizard key a
		# handset asks for, so a per-company definition would mean either the
		# same key existing several times — which the unique constraint refuses —
		# or a key prefixed per entity, which every client would have to learn.
		# Per-company narrowing, when somebody wants it, is `required_role` and a
		# second definition under its own key.
		#
		# THE v0.80.0 ADDITION IS THE SAME ARGUMENT ABOUT A KIND OF PAPER. A Trade
		# Document Template is the SHAPE of a document — "a phytosanitary
		# certificate carries a botanical name, a treatment schedule and an
		# additional declaration" — and a shape belongs to no entity. It names no
		# shipment, no customer and no container. The records that carry those are
		# the Trade SHIPMENTS and the Trade DOCUMENTS themselves, both of which
		# link to Company and are scoped by Frappe exactly as before.
		#
		# Scoping it would be wrong for the docname reason as well as the shape
		# one: the docname IS the template name a Destination Document Requirement
		# points at, so a per-company template would mean either the same name
		# existing several times — which the unique constraint refuses — or a name
		# prefixed per entity, which every rule would have to spell. Per-company
		# narrowing exists and is on the layer where it belongs: `Destination
		# Document Requirement.company`, which is a Link to Company and IS scoped
		# by Frappe, so one entity really can be made to ask for a document
		# another does not — without two spellings of the certificate itself.
		#
		# THE v0.85.0 ADDITION IS `Farm Translation`, and it is the same argument
		# about a WORD. The Spanish for "bucket" belongs to no company; two
		# entities on one site read the same language, and a per-company string
		# register would mean maintaining two translations of one sentence and
		# discovering the divergence when a worker moved between crews. The
		# docname reason applies too: the docname IS `key::language`, so a
		# per-company row would need either duplicates the unique constraint
		# refuses or a prefixed key every caller would have to spell.
		#
		# `Shadow Log Entry` is deliberately NOT on this list. A supervisory copy
		# names a company and IS scoped by Frappe, because it carries a person's
		# work in a snapshot — a manager at one entity has no business reading
		# another's crew, which is precisely the distinction this list draws.
		# THE v0.87.0 ADDITION IS THE SAME ARGUMENT ABOUT A MARKET. A USDA Price
		# Quote is what a shipping point district was ASKING for a commodity on a
		# day — a published fact about a market, not about anybody's operation. It
		# names no block, no cost, no customer and no crop anybody owns. The
		# records that carry those are the Breakeven ANALYSES that read it, every
		# one of which links to Company and is scoped by Frappe exactly as before.
		#
		# Scoping it would be wrong rather than merely unnecessary, for the reason
		# the Merchant Alias paragraph gives: the quotation is public and
		# site-wide, so a per-company copy would mean the same published number
		# stored once per entity — two copies of one fact that will eventually
		# disagree — or a quotation owned by one entity quietly overlaying
		# another's analysis. And the register's whole design rests on commodity,
		# variety, market, package and date being the identity, which a company
		# column would break by making that key non-unique.
		# THE v0.82.0 ADDITIONS ARE THE FOUR AGRICULTURAL MASTERS, and each is a
		# slightly different version of the same argument:
		#
		#   * `Crop` is a SPECIES. A sweet cherry is a sweet cherry on both sides
		#     of a corporate boundary, and the days between bloom and harvest do
		#     not consult the deed. A per-company copy would mean two rows that
		#     must agree about botany and no mechanism making them.
		#   * `Market` is a PLACE IN THE WORLD. Two growers shipping into the
		#     Pacific Northwest fresh cherry market are shipping into ONE market —
		#     scoping it would give the site two answers to what a No. 1 is, which
		#     is the exact failure the doctype exists to prevent. Per-company
		#     narrowing already exists on the layer where it belongs: the
		#     SETTLEMENT names both the market and the company.
		#   * `Agricultural UOM Context` and `Agricultural UOM Conversion` are
		#     about UNITS. A bin holds what it holds regardless of whose name is
		#     on the bin, and an operation that weighs its own bins should improve
		#     the site's figure rather than fork it.
		#
		# The docname reason applies to all four as it does to Trade Document
		# Template: the docname IS the key other records spell — a Market's
		# `primary_commodity` names a Crop by name, a conversion's docname carries
		# its crop — so a per-company row would need either duplicates the unique
		# constraint refuses or a prefixed name every caller would have to spell.
		# THE v0.87.0 ADDITION IS THE SAME ARGUMENT ABOUT A MARKET. A USDA Price
		# Quote is what a shipping point district was ASKING for a commodity on a
		# day — a published fact about a market, not about anybody's operation. It
		# names no block, no cost, no customer and no crop anybody owns. The
		# records that carry those are the Breakeven ANALYSES that read it, every
		# one of which links to Company and is scoped by Frappe exactly as before.
		#
		# Scoping it would be wrong rather than merely unnecessary, for the reason
		# the Merchant Alias paragraph gives: the quotation is public and
		# site-wide, so a per-company copy would mean the same published number
		# stored once per entity — two copies of one fact that will eventually
		# disagree — or a quotation owned by one entity quietly overlaying
		# another's analysis. And the register's whole design rests on commodity,
		# variety, market, package and date being the identity, which a company
		# column would break by making that key non-unique.
		# THE v0.99.0 ADDITION IS A HANDSET, WHICH IS NOT AN ENTITY'S. A Mobile
		# Push Token is where a notification is DELIVERED — one phone, one APNs
		# device token, keyed on `platform::device_id` and unique on it. A worker
		# on this site may be employed by two entities and carries one phone
		# through both, so a company column would mean two rows for one handset:
		# either the unique constraint refuses the second, or the register grows
		# exactly the duplicate-per-phone problem the whole doctype is shaped to
		# prevent, and every crew push becomes one delivery and one wasted
		# request.
		#
		# The entity narrowing already exists on the layer where it belongs, and
		# it is the Market argument again: nothing addresses a push to a token
		# directly. `send_push_to_shift_crew` starts from a FARM SHIFT, which
		# links to Company and is scoped by Frappe exactly as before, and reaches
		# only the Employees rostered on it. `list_push_tokens` is Foreman-and-
		# above in its own body. A token is a routing address and holds no wage,
		# no hour, no location and no fact about the business.
		# THE v0.116.0 ADDITION IS A SOIL, WHICH IS A PROPERTY OF GROUND AND NOT
		# OF A COMPANY. A Soil Compaction Profile says how long a silt loam stays
		# too wet to drive on; a silt loam does not consult the deed either, and
		# two entities farming the same bench are farming the same soil. It is
		# the Crop argument about a species, applied one layer down.
		#
		# The docname reason applies as it does to the four above: `soil_type` IS
		# the docname and `Field.soil_profile` spells it, so a per-company row
		# would need either duplicates the unique constraint refuses or a
		# prefixed name every block would have to carry. The entity narrowing
		# already exists where it belongs — the FIELD names both the profile and
		# its owning entity, and Frappe scopes the Field exactly as before.
		self.assertEqual(
			sorted(unscoped),
			[
				"Agricultural UOM Context",
				"Agricultural UOM Conversion",
				"Asset State Log",
				"Compliance Regime",
				"Compliance Rule",
				"Crop",
				"Farm Translation",
				"Federal Tax Table",
				"I-9 Audit Log",
				"I-9 Document Type",
				"Inspection Template",
				"Labor Break Policy",
				"MCP Action Log",
				"Market",
				"Merchant Alias",
				"Mobile Push Token",
				"Soil Compaction Profile",
				"Staged File Chunk",
				"Staged File Upload Session",
				"State Tax Table",
				"Trade Document Template",
				"Training Type",
				"USDA Price Quote",
				"Wizard Definition",
			],
		)


# ── 3 ───────────────────────────────────────────────────────────────────────
class NobodyIsLockedOut(PermissionsTestCase):
	def test_a_user_with_no_company_permission_gets_no_condition(self):
		"""Frappe's own rule, and what makes an operator's login work. Departing
		from it HERE would make one doctype invisible to an admin for reasons
		nothing else on the page shares."""
		self.assertEqual(permissions.housing_assignment_query(UNSCOPED_USER), "")
		self.assertEqual(permissions.family_query(UNSCOPED_USER), "")

	def test_administrator_is_unrestricted(self):
		self.assertEqual(permissions.housing_assignment_query("Administrator"), "")

	def test_a_failure_returns_the_unrestricted_answer_rather_than_locking_the_doctype(self):
		"""A hook that raises fails EVERY list view of the doctype for everybody,
		including the Desk page somebody would open to fix it."""
		broken = dict(permissions.ROUTES)
		try:
			permissions.ROUTES[permissions.HOUSING_ASSIGNMENT] = (("unit", "No Such Doctype", "x"),)
			self.assertEqual(permissions.housing_assignment_query(SCOPED_USER), "")
		finally:
			permissions.ROUTES.clear()
			permissions.ROUTES.update(broken)

	def test_the_company_names_are_escaped_into_the_sql(self):
		"""They are docnames an operator typed, and they end up inside a string
		literal in a WHERE clause."""
		STORE.seed(
			"Company",
			[{"name": "O'Brien Orchards LLC", "abbr": "OBO", "default_currency": "USD"}],
		)
		STORE.seed(
			"User Permission",
			[
				{
					"name": "UP-ANA-QUOTE",
					"user": SCOPED_USER,
					"allow": "Company",
					"for_value": "O'Brien Orchards LLC",
					"apply_to_all_doctypes": 1,
				}
			],
		)
		condition = permissions.housing_assignment_query(SCOPED_USER)
		self.assertNotIn("'O'Brien Orchards LLC'", condition)
		self.assertIn("O", condition)


# ── 4 ───────────────────────────────────────────────────────────────────────
class TheDocumentCheckAgrees(PermissionsTestCase):
	def test_a_scoped_user_may_read_their_own_entitys_assignment(self):
		row = frappe.get_doc("Housing Assignment", "HA-MINE")
		self.assertTrue(permissions.housing_assignment_has_permission(row, "read", SCOPED_USER))

	def test_and_may_not_read_another_entitys(self):
		row = frappe.get_doc("Housing Assignment", "HA-THEIRS")
		self.assertFalse(permissions.housing_assignment_has_permission(row, "read", SCOPED_USER))

	def test_an_unscoped_user_may_read_both(self):
		for name in ("HA-MINE", "HA-THEIRS"):
			row = frappe.get_doc("Housing Assignment", name)
			self.assertTrue(permissions.housing_assignment_has_permission(row, "read", UNSCOPED_USER))

	def test_a_row_with_no_unit_is_readable_rather_than_hidden(self):
		row = frappe.get_doc("Housing Assignment", "HA-ORPHAN")
		self.assertTrue(permissions.housing_assignment_has_permission(row, "read", SCOPED_USER))

	def test_it_accepts_a_plain_dict_as_well_as_a_document(self):
		"""Frappe hands this hook either, depending on the call path."""
		self.assertTrue(
			permissions.housing_assignment_has_permission({"unit": "MC-Cabin-01"}, "read", SCOPED_USER)
		)
		self.assertFalse(
			permissions.housing_assignment_has_permission({"unit": "HL-Cabin-09"}, "read", SCOPED_USER)
		)

	def test_the_company_a_document_belongs_to_is_followed_through_its_link(self):
		self.assertEqual(permissions.document_company("Housing Assignment", {"unit": "MC-Cabin-01"}), MAIN)
		self.assertEqual(permissions.document_company("Housing Assignment", {"unit": ""}), "")


# ── helpers ─────────────────────────────────────────────────────────────────
def _app_doctypes() -> list:
	import json
	import pathlib

	root = pathlib.Path(__file__).resolve().parents[1] / "erpnext_mcp" / "erpnext_mcp" / "doctype"
	names = []
	for folder in sorted(root.iterdir()):
		definition = folder / f"{folder.name}.json"
		if definition.is_file():
			names.append(json.loads(definition.read_text()).get("name") or folder.name)
	return names


def _meta(doctype: str):
	import json
	import pathlib

	slug = doctype.lower().replace(" ", "_").replace("-", "_")
	path = (
		pathlib.Path(__file__).resolve().parents[1]
		/ "erpnext_mcp"
		/ "erpnext_mcp"
		/ "doctype"
		/ slug
		/ f"{slug}.json"
	)
	return json.loads(path.read_text()) if path.is_file() else None
