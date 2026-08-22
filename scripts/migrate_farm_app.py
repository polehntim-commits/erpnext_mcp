#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Carry the farm_app's MRL reference data and satellite history into ERPNext.

WHAT THIS MOVES, AND WHY IT IS NOT EVERYTHING. `erpnext_mcp.farm_app_migration`
is where the transfer lives and where every decision it makes is argued. The
short version: the sidecar's contents are test data except for two things, and
those two are what this carries.

    maximum_residue           → MRL Record                 the residue limits themselves
    mrl_research_session      → MRL Record                 the research that produced some of them
    field_satellite_metric    → Satellite Metric           the index series the imagery left behind
    satellite_backfill_cursor → Satellite Backfill Cursor  how far back imagery was already paid for

Cached raster FILES are reported by `--rasters` and never moved — see below.

RUNNING IT. Like `seed_related_parties.py`, this runs OUTSIDE `bench execute`
and configures Frappe itself:

    python3 migrate_farm_app.py --database farm_app.db --site erp.local
    # ...read the plan, then:
    python3 migrate_farm_app.py --database farm_app.db --site erp.local --apply

Without `--apply` NOTHING IS WRITTEN.

FLAGS, WHICH MATCH THIS DOCSTRING EXACTLY (a previous release shipped a script
whose docstring and `argparse` disagreed, and it cost twenty minutes of somebody
else's evening):

    --database PATH     the farm_app SQLite file. Required. Opened read-only.
    --site SITE         the Frappe site. Defaults to `currentsite.txt`.
    --sites-path PATH   the bench's `sites` directory. Auto-detected.
    --company NAME      the company every migrated record belongs to.
    --table NAME        migrate only this table. Repeatable. Default: all four.
    --limit N           most rows to take from any one table. Default 100000.
    --rasters PATH      also report cached NDVI rasters, checked under this root.
    --report PATH       write the full JSON report here as well as printing it.
    --apply             actually write. Without it, nothing is created.
    --verbose           print every refusal and warning, not just the counts.

THE NAME JOIN IS THE PART TO READ THE REPORT FOR. MRL rows point at a crop and a
country by SQLite id, and nothing on this site's `Crop` or `Market` carries those
ids — so the join is on the NAME, done once, exactly, with no fuzzy matching.
Every unmatched name is listed under `unmatched`. A limit whose crop or market
does not resolve is REFUSED rather than filed against nothing: both are required
on `MRL Record`, and one filed anyway looks like an answer. Create or rename what
is missing and re-run — the migration is idempotent, so the second pass picks up
only what was refused.

THE RASTERS ARE REPORTED, NOT MOVED. `Field.ndvi_path` in the sidecar points at
a file inside the farm_app container. Copying megabytes across a container
boundary is a `docker cp` an operator does with their own hands and their own
disk, at a moment they choose — so `--rasters <root>` says what exists, how big
it is, and whether it is readable from here, and stops there. The numbers this
app actually reads are the Satellite Metric rows, which do migrate.

EXIT CODES. 0 when the run did what it was asked (including a dry run that found
problems it reported), 1 for a bad plan or a database this cannot read, 2 for a
Frappe or site problem. A failure part-way rolls the transaction back — nothing
is half-written.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
	sys.path.insert(0, REPO_ROOT)

from erpnext_mcp import farm_app_migration as migration  # noqa: E402


class PlanError(Exception):
	"""A problem with the input. Reported before Frappe is started."""


# ── the parts that need no bench ────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
	"""The parser, separately from parsing, so a test can read what it registers."""
	parser = argparse.ArgumentParser(
		prog="migrate_farm_app.py",
		description="Migrate the farm_app SQLite database into ERPNext. Dry-run by default.",
	)
	parser.add_argument("--database", required=True, help="the farm_app SQLite file, opened read-only")
	parser.add_argument("--site", default="", help="the Frappe site; defaults to currentsite.txt")
	parser.add_argument("--sites-path", default="", help="the bench's sites directory")
	parser.add_argument("--company", default="", help="the company every migrated record belongs to")
	parser.add_argument("--table", action="append", default=[], help="migrate only this table; repeatable")
	parser.add_argument(
		"--limit", type=int, default=migration.DEFAULT_LIMIT, help="most rows from any one table"
	)
	parser.add_argument("--rasters", default="", help="also report cached NDVI rasters under this root")
	parser.add_argument("--report", default="", help="write the full JSON report here as well")
	parser.add_argument("--apply", action="store_true", help="actually write; without it nothing is created")
	parser.add_argument("--verbose", action="store_true", help="print every refusal and warning")
	return parser


def registered_flags() -> set:
	"""Every long option `build_parser` registers, `--help` excluded."""
	out = set()
	for action in build_parser()._actions:
		out.update(option for option in action.option_strings if option.startswith("--"))
	return out - {"--help"}


def parse_args(argv=None) -> argparse.Namespace:
	return build_parser().parse_args(argv)


def check_tables(names) -> tuple:
	"""The `--table` list, refused by name if any of them is not a spec."""
	wanted = tuple(str(name).strip() for name in names or () if str(name).strip())
	unknown = [name for name in wanted if name not in migration.SPEC_BY_TABLE]
	if unknown:
		raise PlanError(
			f"unknown table(s): {', '.join(unknown)}. Known: {', '.join(migration.SPEC_BY_TABLE)}"
		)
	return wanted


def find_sites_path(explicit: str = "") -> str:
	"""The bench's `sites` directory, from the flag or by looking for one."""
	if explicit:
		path = os.path.abspath(explicit)
		if not os.path.isdir(path):
			raise PlanError(f"--sites-path {explicit!r} is not a directory")
		return path
	for seed in (os.path.dirname(os.path.abspath(__file__)), os.getcwd()):
		current = seed
		while True:
			for candidate in (os.path.join(current, "sites"), current):
				if _is_sites_dir(candidate):
					return candidate
			parent = os.path.dirname(current)
			if parent == current:
				break
			current = parent
	raise PlanError(
		"could not find the bench's sites directory. Pass --sites-path, or run this from inside "
		"the bench (the directory holding `sites/common_site_config.json`)."
	)


def _is_sites_dir(path: str) -> bool:
	return os.path.isfile(os.path.join(path, "common_site_config.json")) or os.path.isfile(
		os.path.join(path, "apps.txt")
	)


def find_site(sites_path: str, explicit: str = "") -> str:
	if explicit:
		return explicit
	current = os.path.join(sites_path, "currentsite.txt")
	if os.path.isfile(current):
		with open(current) as handle:
			name = handle.read().strip()
		if name:
			return name
	raise PlanError(f"no --site given and {current} is missing or empty. Pass --site <site>.")


def format_report(report: dict, verbose: bool = False) -> str:
	"""The report as the lines a person reads. Counts always, detail on request."""
	out = []
	for entry in report["tables"]:
		head = (
			f"  {entry['table']:<24} → {entry['doctype']:<20} "
			f"read {entry['read']:>6}   create {entry['created']:>6}   "
			f"present {entry['already_present']:>6}   refuse {entry['refused']:>5}"
		)
		if entry["updated"]:
			head += f"   update {entry['updated']}"
		out.append(head)
		if entry["note"]:
			out.append(f"      note: {entry['note']}")
		if entry["truncated"]:
			out.append("      TRUNCATED — the row limit bit; this table is not fully migrated")
		shown = entry["refusals"] if verbose else entry["refusals"][:3]
		for refusal in shown:
			out.append(f"      refused row {refusal['id']}: {refusal['why']}")
		if not verbose and len(entry["refusals"]) > 3:
			out.append(f"      ...and {len(entry['refusals']) - 3} more refusals (--verbose to see them)")
		warnings = entry["warnings"] if verbose else entry["warnings"][:3]
		for warning in warnings:
			where = f"row {warning['id']}: " if warning["id"] is not None else ""
			out.append(f"      warning {where}{warning['warning']}")
		if not verbose and len(entry["warnings"]) > 3:
			out.append(f"      ...and {len(entry['warnings']) - 3} more warnings (--verbose to see them)")
	out.append("")
	out.append(
		f"  {report['created']} to create, {report['already_present']} already present, "
		f"{report['updated']} updated, {report['refused']} refused, {report['warnings']} warning(s)"
	)
	return "\n".join(out)


# ── the parts that need a bench ─────────────────────────────────────────────
def connect(site: str, sites_path: str):
	"""Start Frappe against this site."""
	for path in (
		os.path.join(os.path.dirname(sites_path.rstrip(os.sep)), "logs"),
		os.path.join(sites_path, site, "logs"),
		os.path.join(os.path.expanduser("~"), "logs"),
	):
		try:
			os.makedirs(path, exist_ok=True)
		except OSError:
			pass

	import frappe

	frappe.init(site=site, sites_path=sites_path)
	frappe.connect()
	return frappe


def main(argv=None) -> int:
	args = parse_args(argv)
	try:
		tables = check_tables(args.table)
		if not os.path.isfile(args.database):
			raise PlanError(f"no such database file: {args.database}")
		sites_path = find_sites_path(args.sites_path)
		site = find_site(sites_path, args.site)
	except PlanError as error:
		print(f"error: {error}", file=sys.stderr)
		return 1

	print(f"site:       {site}")
	print(f"sites path: {sites_path}")
	print(f"database:   {args.database}")
	print(f"tables:     {', '.join(tables) if tables else 'all, in dependency order'}")
	print(f"mode:       {'APPLY — records will be created' if args.apply else 'DRY RUN — nothing written'}")

	try:
		connection = migration.open_database(args.database)
	except migration.MigrationError as error:
		print(f"error: {error}", file=sys.stderr)
		return 1

	frappe = None
	try:
		frappe = connect(site, sites_path)
	# Any exception here is a site problem, and the useful answer is the type
	# and the message rather than a traceback.
	except Exception as error:
		print(f"error: could not connect to {site}: {type(error).__name__}: {error}", file=sys.stderr)
		return 2

	try:
		seeds = migration.seed_links_from_site(frappe)
		by_name = migration.seed_links_by_name(connection, migration.frappe_lookup(frappe))
		unmatched = by_name.pop("_unmatched", {})
		links = migration.Links({**seeds, **by_name})
		print(f"blocks already carrying a Farm App id: {links.known('field')}")
		print(f"crops matched by name:   {links.known('commodity')}")
		print(f"markets matched by name: {links.known('country')}")
		for table, names in sorted(unmatched.items()):
			target = {"commodity": "Crop", "country": "Market"}[table]
			print(f"  NO {target} on this site for: {', '.join(sorted(names))}")
		loader = migration.FrappeLoader(frappe) if args.apply else migration.DryRunLoader()
		report = migration.migrate(
			connection,
			loader,
			links,
			{"company": args.company},
			only=tables,
			limit=args.limit,
		)
		if args.apply:
			frappe.db.commit()
	# Report and roll back. A migration that half-applied is worse than one that
	# did nothing, so the transaction goes back either way.
	except Exception as error:
		try:
			frappe.db.rollback()
		except Exception:  # pragma: no cover - teardown must not mask a real error
			pass
		print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
		print("nothing was written — the transaction was rolled back.", file=sys.stderr)
		return 1
	finally:
		try:
			frappe.destroy()
		except Exception:  # pragma: no cover
			pass
		connection.close()

	report["unmatched"] = unmatched
	if args.rasters:
		report["rasters"] = migration.raster_manifest(connection, args.rasters)

	print()
	print(format_report(report, args.verbose))
	if report.get("rasters"):
		manifest = report["rasters"]
		print(
			f"\n  cached rasters: {len(manifest['rasters'])} referenced, {manifest['missing']} not "
			f"readable under {manifest['checked_against']}, "
			f"{manifest['total_bytes'] / 1e6:.1f} MB found"
		)
		print("  these are NOT moved by this script — copy them out yourself if you want them kept.")
	if args.report:
		with open(args.report, "w") as handle:
			json.dump(report, handle, indent=2, sort_keys=True, default=str)
		print(f"\nfull report written to {args.report}")
	if not args.apply and report["created"]:
		print("\nNothing was written. Re-run with --apply to create the records above.")
	return 0


if __name__ == "__main__":  # pragma: no cover
	sys.exit(main())
