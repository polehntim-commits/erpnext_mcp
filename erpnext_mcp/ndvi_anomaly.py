# SPDX-License-Identifier: MIT
"""Week-over-week NDVI drops: deciding whether one is worth sending somebody to look.

WHAT THE DECISION ACTUALLY IS. A satellite pass gives a block one number a week.
A block whose greenness falls 20% between two passes has something happening to
it — a broken valve, a mite flare, a wind event, a herbicide drift from next
door — and the only way to find out which is to walk it. So the output of this
module is not a diagnosis. It is a decision about whether to spend a scout's
morning, and everything below is calibrated around what a false alarm costs
(that morning) against what a miss costs (a block that keeps declining for
another week).

WHY A RELATIVE DROP AND NOT AN ABSOLUTE THRESHOLD. NDVI is not comparable
between crops, between rootstocks, or between May and September. A cherry block
at 0.82 and a young planting at 0.41 are both healthy; a fixed floor would alarm
on the young block every week and never on the mature one. The block is compared
against ITSELF a week ago, which is the only baseline that needs no per-crop
calibration.

THE INDEX SCALE IS THE TRAP, AND IT IS WHY `to_index` EXISTS. Raw NDVI runs -1
to 1 and this app stores it that way on `Field.last_ndvi_mean`; the farm_app
this logic came from stored a 0-100 indexed value. A relative drop computed on
raw NDVI is unsound near zero and meaningless across it: 0.05 → 0.01 is an 80%
"drop" on bare ground where nothing happened, and 0.10 → -0.10 is a 200% one.
Every comparison here is therefore made on the 0-100 index, and a caller handing
in raw values gets them converted rather than refused — the two scales are
mechanically distinguishable (raw NDVI is never above 1.0) and refusing one
would mean every caller carrying a units flag.

THE THREE GUARDS, EACH OF WHICH SUPPRESSED A REAL FALSE ALARM.

*A long gap is not a week.* Cloud cover routinely costs two or three passes, and
a 25-day-old baseline compared against today is a seasonal change being read as
an event. Gaps beyond `MAX_GAP_DAYS` are dropped rather than scaled, because the
scouting task says "since last week" and a task that lies about its own window
wastes the walk.

*A block already flagged is not flagged again.* The decline that triggered
Monday's alert is still there on Thursday. Without `DEDUPE_DAYS` a single event
produces an alert per pass until the block recovers, and a queue of duplicates
is a queue somebody stops reading.

*A baseline at or below zero has nothing to be a percentage of.* Division by a
baseline of 0.0 is not a 100% drop, it is a block with no vegetation index to
compare against — bare ground, a flooded row, or a masked pass.

WHAT THIS MODULE WILL NOT DO IS TOUCH THE DATABASE. It takes two readings and
answers whether to act, so it can be unit tested without a bench — the same
split the farm_app used, and the reason its version of this logic was the one
piece of the satellite subsystem that never needed a fix.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: The relative fall that counts as an anomaly. Fifteen percent of a block's own
#: greenness in a week is outside normal phenological change everywhere except
#: senescence, which is what `SENESCENCE_PRINCIPAL_STAGES` below is for.
DROP_PCT = 0.15

#: Above this, the alert is `high` rather than `medium`. Thirty percent in a week
#: is not a stress signal, it is something that has already happened.
HIGH_SEVERITY_PCT = 0.30

#: The longest gap between two passes that still counts as a week-over-week
#: comparison. See the module docstring — a gap this wide is a season, not an
#: event.
MAX_GAP_DAYS = 10

#: How long an alert suppresses the next one for the same block.
DEDUPE_DAYS = 7

#: The lowest baseline index worth computing a percentage against. A block at 5
#: on a 0-100 index has no canopy; the percentages it produces are noise with a
#: decimal point.
MIN_BASELINE_INDEX = 5.0

#: BBCH principal stages where a fall in greenness is the crop doing what it is
#: supposed to. 8 is ripening and 9 is senescence: leaves yellow, NDVI falls, and
#: an alert raised on it sends somebody to look at a block that is on schedule.
#: Callers that know the block's stage pass it; callers that do not get the
#: alert, because a missed real drop costs more than a walked healthy block.
SENESCENCE_PRINCIPAL_STAGES = (8, 9)


def to_index(value):
	"""A 0-100 index from either scale, or `None` if the value is not a reading.

	Raw NDVI (-1 to 1) is rescaled; a value already above 1.0 is taken as an
	index and passed through. The boundary case `1.0` is treated as RAW, which
	makes it the index 100 either way — the two readings that could be meant are
	the same answer, so the ambiguity costs nothing.
	"""
	if value is None or isinstance(value, bool):
		return None
	try:
		number = float(value)
	except (TypeError, ValueError):
		return None
	if number != number or number in (float("inf"), float("-inf")):  # NaN and infinities
		return None
	if -1.0 <= number <= 1.0:
		return (number + 1.0) * 50.0
	if 0.0 <= number <= 100.0:
		return number
	return None


def severity_for_drop(drop_pct: float) -> str:
	"""`"high"` or `"medium"` for a relative drop. Nothing here returns `"low"` —
	a drop below `DROP_PCT` produces no alert at all rather than a quiet one."""
	return "high" if drop_pct >= HIGH_SEVERITY_PCT else "medium"


def evaluate(
	previous_value,
	previous_ts,
	latest_value,
	latest_ts,
	last_alert_ts=None,
	now=None,
	growth_stage=None,
) -> dict | None:
	"""`None`, or `{drop_pct, severity, gap_days, previous_index, latest_index}`.

	`None` means no alert, and the caller cannot tell WHY from the return value —
	deliberately. Every reason is a reason not to act, they are not ranked, and a
	caller that started branching on them would be re-implementing this decision
	one guard at a time. `explain()` answers the same question in words for a
	human reading a report.

	`growth_stage` is the block's BBCH code if the caller knows it. A drop during
	ripening or senescence is suppressed — see `SENESCENCE_PRINCIPAL_STAGES`.
	"""
	assessment = explain(
		previous_value, previous_ts, latest_value, latest_ts, last_alert_ts, now, growth_stage
	)
	if not assessment["alert"]:
		return None
	return {
		"drop_pct": assessment["drop_pct"],
		"severity": assessment["severity"],
		"gap_days": assessment["gap_days"],
		"previous_index": assessment["previous_index"],
		"latest_index": assessment["latest_index"],
	}


def explain(
	previous_value,
	previous_ts,
	latest_value,
	latest_ts,
	last_alert_ts=None,
	now=None,
	growth_stage=None,
) -> dict:
	"""The same decision as `evaluate`, with the reason attached either way.

	`{alert, reason, drop_pct, severity, gap_days, previous_index, latest_index}`.
	Written for the report that has to say why a block with a visible decline was
	not flagged — "the baseline was 23 days old" is an answer somebody can act on
	and "no alert" is not.
	"""
	blank = {
		"alert": False,
		"reason": "",
		"drop_pct": None,
		"severity": None,
		"gap_days": None,
		"previous_index": None,
		"latest_index": None,
	}

	previous_index = to_index(previous_value)
	latest_index = to_index(latest_value)
	if previous_index is None or latest_index is None:
		return {**blank, "reason": "one of the two readings is missing or is not a number"}

	previous_at = _moment(previous_ts)
	latest_at = _moment(latest_ts)
	if previous_at is None or latest_at is None:
		return {**blank, "reason": "one of the two readings has no usable timestamp"}

	measured = {"previous_index": round(previous_index, 3), "latest_index": round(latest_index, 3)}
	if previous_index < MIN_BASELINE_INDEX:
		return {
			**blank,
			**measured,
			"reason": f"the baseline index is {previous_index:.1f}, below the {MIN_BASELINE_INDEX:.0f} "
			"a percentage can be computed against — bare ground, water, or a masked pass",
		}

	gap_days = (latest_at - previous_at).days
	if gap_days <= 0:
		return {**blank, **measured, "reason": "the newer reading is not newer than the baseline"}
	if gap_days > MAX_GAP_DAYS:
		return {
			**blank,
			**measured,
			"gap_days": gap_days,
			"reason": f"the readings are {gap_days} days apart, past the {MAX_GAP_DAYS}-day limit "
			"for a week-over-week comparison",
		}

	drop_pct = (previous_index - latest_index) / previous_index
	measured = {**measured, "gap_days": gap_days, "drop_pct": round(drop_pct, 4)}
	if drop_pct < DROP_PCT:
		return {
			**blank,
			**measured,
			"reason": f"the change is {drop_pct * 100:.1f}%, under the {DROP_PCT * 100:.0f}% threshold",
		}

	stage = _principal(growth_stage)
	if stage in SENESCENCE_PRINCIPAL_STAGES:
		return {
			**blank,
			**measured,
			"reason": f"the block is at BBCH stage {stage}x, where greenness is expected to fall",
		}

	moment = _moment(now) or datetime.now(timezone.utc)
	alerted_at = _moment(last_alert_ts)
	if alerted_at is not None and (moment - alerted_at) < timedelta(days=DEDUPE_DAYS):
		days = (moment - alerted_at).days
		return {
			**blank,
			**measured,
			"reason": f"this block was already flagged {days} day(s) ago, inside the "
			f"{DEDUPE_DAYS}-day quiet period",
		}

	return {
		**blank,
		**measured,
		"alert": True,
		"severity": severity_for_drop(drop_pct),
		"reason": f"greenness fell {drop_pct * 100:.0f}% in {gap_days} day(s)",
	}


def message(field_name, previous_value, previous_ts, latest_value, latest_ts, drop_pct) -> str:
	"""The scouting task's own words.

	Says the two figures, the two dates and the percentage, because the scout
	standing in the block needs to know how big a change to look for and when it
	started. The figures are quoted on the 0-100 index whatever scale came in,
	so two tasks raised from two sources are comparable to the person reading
	them.
	"""
	previous_index = to_index(previous_value)
	latest_index = to_index(latest_value)
	previous_at = _moment(previous_ts)
	latest_at = _moment(latest_ts)
	span = ""
	if previous_at and latest_at:
		span = f" between {previous_at.date().isoformat()} and {latest_at.date().isoformat()}"
	figures = ""
	if previous_index is not None and latest_index is not None:
		figures = f" NDVI index fell from {previous_index:.1f} to {latest_index:.1f}"
	return (
		f"NDVI anomaly detected on {field_name or 'an unnamed block'} — investigate."
		f"{figures} ({(drop_pct or 0) * 100:.0f}%){span}."
	).replace("  ", " ")


# ── the parts nobody outside calls ──────────────────────────────────────────
def _moment(value):
	"""A timezone-aware datetime from what a Frappe row or a caller supplies.

	Frappe hands back naive datetimes in the site's timezone and a caller may
	hand in an aware one. Comparing the two raises, so a naive value is read as
	UTC — which is right for the satellite timestamps this compares (the provider
	publishes acquisition times in UTC) and harmless for the rest, since every
	comparison here is a difference in days.
	"""
	if value is None or isinstance(value, bool):
		return None
	if isinstance(value, datetime):
		return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
	text = str(value).strip()
	if not text:
		return None
	text = text.replace("Z", "+00:00")
	for pattern in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
		try:
			parsed = datetime.fromisoformat(text) if pattern is None else datetime.strptime(text, pattern)
		except ValueError:
			continue
		return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
	return None


def _principal(growth_stage):
	"""The BBCH principal stage a code names, or `None`.

	Delegates to `bbch.principal` rather than re-deriving it, because the stage
	column holds `"BBCH 87"` as often as `"87"` and one module already knows
	that.
	"""
	from . import bbch

	return bbch.principal(growth_stage)
