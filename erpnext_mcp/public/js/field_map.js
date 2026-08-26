// SPDX-License-Identifier: MIT
/**
 * The block's own boundary, drawn inside its parcel's.
 *
 * v0.32.0. Both shapes on one map because `set_field_boundary` REPORTS whether
 * the block sits inside its parcel and never enforces it — a planting that
 * predates a deed split really does straddle the line — and a warning string
 * about an overhang is something nobody pictures. Drawn, the difference between
 * "that is the corner we always farmed across" and "two vertices are in the
 * wrong order" takes a second to see.
 *
 * v0.33.0 MAKES IT DRAWABLE, and the parcel underneath is what makes drawing it
 * possible at all: a block corner on a satellite image is a change in canopy, and
 * the deed line it should stop at is not visible from the air. Both on one map,
 * one of them editable, is the only arrangement in which somebody can trace a
 * block and see where it lands.
 *
 * NO COUNTY IMPORT HERE. The county knows tax lots, not blocks — see
 * `parcel_map.js`.
 *
 * v0.139.0 ADDS THE FSA IMPORT, WHICH IS ON THIS FORM AND ON NO OTHER, and it is
 * the exact mirror of the line above rather than an exception to it. The two
 * governments publish different things:
 *
 *   * A COUNTY publishes TAX LOTS. A tax lot is what a deed, a tax bill and an
 *     assessor all agree on, which is what a Parcel is. It has never heard of an
 *     orchard block, so a county button here would offer to set a block's
 *     boundary to the whole lot it happens to sit in.
 *   * THE FARM SERVICE AGENCY publishes COMMON LAND UNITS. A CLU *is* a field —
 *     drawn at the resolution the ground is actually farmed at, split where the
 *     farming splits — and it is the shape the farm's own acreage report, its
 *     crop insurance and every ARC/PLC and CRP payment are already measured
 *     against. On a Parcel form it would be the wrong unit in the other
 *     direction: a tract holds several CLUs and crosses tax lots.
 *
 * AND FSA'S POLYGON IS BETTER THAN A TRACED ONE for this shape too, for a
 * different reason than the county's is. A block boundary really is a farming
 * decision that only the farm knows — but if the farm has ever filed an acreage
 * report, that decision has ALREADY BEEN DRAWN, by them, in an office, on a
 * shape they signed for. Tracing it again by eye produces a second version of
 * the farm's own boundaries that disagrees with the first by a few percent, and
 * only one of the two is the one FSA pays on.
 */

erpnext_mcp.geo_map.attach_editable_boundary("Field", {
	title: __("Block Boundary"),
	container: { doctype: "Parcel", field: "parcel" },
	fsa: true,
});
