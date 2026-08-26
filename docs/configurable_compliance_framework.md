# The Configurable Compliance Framework

**Compliance rules are data. The engine that runs them is code. Nothing
probabilistic runs at sweep time.**

Until v0.22.0 a compliance rule in this app was a Python function. Moving a
threshold, correcting a citation, narrowing a rule to one company or switching
one off for a season was a code change, a release and a deploy.

Regulations do not move on a release cadence. Oregon OSHA renumbered heat
illness from OAR 437-004-1130 to -1131. Oregon Tilth added a Fraud Prevention
Plan requirement. The FDA re-phased FSMA Produce Safety. Every one of those is a
data change that used to wear a code change's clothes.

Since v0.22.0 a rule is a **Compliance Rule** record. Its thresholds, its scope,
its citations, its regimes, its message and its switch are fields somebody edits.
What did not move is the sweep: `alerts/base.py` still walks a rule set, still
keys each alert on the rule and the record and nothing that moves daily, still
auto-dismisses what it did not observe. Only *where the rule set comes from*
changed.

**v0.22.1 added the four primitives v0.22.0's §5 named**, and five of the seven
rules that still carried a shipped scanner became data. The two that did not move
are argued in §5 as permanent rather than as backlog.

**v0.22.5 is the first release where the vocabulary was used to write a rule
nobody had written in Python first.** `shift_heat_threshold_crossed` fires on a
weather reading rather than on a date, and the three fields it needed —
`latest_child_field_threshold_json`, `date_field_role: State` with
`default_severity`, and `producer_assigned_to_expression` — are all additive. The
split is now **12 declarative / 2 built-in-permanent / 0 `custom_python`**.

---

## 1. The non-negotiable: the runtime is deterministic

**There is no model in the trigger path.** No classifier, no embedding, no
natural-language interpretation of anything at sweep time. A rule fires because a
date crossed a threshold or a column matched a filter, and the report can name
which.

That is not squeamishness about AI; it is what makes an alert *defensible*. For
every alert an auditor questions, the answer traces to:

- a **Compliance Rule** row,
- its `regulation_citations`,
- its `human_approved_by` and `human_approved_on`,
- and the specific field on the specific record that crossed a threshold.

Not to a model output nobody can explain.

**AI's role is confined to authoring.** An AI-proposed rule is *text* until a
human reads the citation against the regulation and approves it. Once approved it
executes the identical deterministic path as every other rule. `authored_by` is
provenance, not behaviour.

`propose_compliance_rule` was declared and refused in v0.22.0. **v0.37.0 wires
it**, on exactly the terms that paragraph promised: drafts land with
`enabled = 0`, an existing rule is never edited or disabled — a proposal against a
live `rule_id` is written at version+1 and touches nothing, and the supersession
happens at approval, by the person approving — and any proposal carrying
`custom_python` is flagged on the record, with `approve_compliance_rule` refusing
it until the approver passes `accept_ai_authored_code` and reads the program the
refusal prints back at them.

**It calls no model.** The AI doing the proposing is the MCP client; the tool is a
validator and a gate. The four rails it enforces, and the reasoning behind each,
are in `erpnext_mcp/proposals.py`. `propose_inspection_template_from_regulation`
and `approve_inspection_template` are the same pattern one layer up, for the forms
a worker fills in.

---

## 2. The three shapes of a rule

| Shape | What is on the record | What is code | Shipped rules |
| --- | --- | --- | --- |
| **declarative** | everything | nothing rule-specific | **12** |
| **builtin_scanner** | every tunable — thresholds, scope, citations, regimes, the switch | the *shape of the join* | **2** |
| **custom_python** | everything, including a restricted program | the interpreter that runs it | **0** |

That `0` is the important number, and it means more at 12/2/0 than it did at
6/7/0. `custom_python` is an escape hatch for rules an operator or a proposer
writes that the primitives do not reach — and a framework that needed a program
for twelve of its own fourteen rules would be a framework whose vocabulary does
not reach its own problem domain.

**The twelfth was never Python at all.** v0.22.5's `shift_heat_threshold_crossed`
was authored as a record, in this vocabulary, with nothing to fall back to — which
is the first evidence that the framework can absorb a NEW obligation rather than
only the thirteen it was reverse-engineered from.

**The right response to reaching for `custom_python` is to say what shape of
question the rule asks, and turn that shape into a field.** §5 is the record of
doing exactly that: v0.22.0 named four primitives its built-ins were waiting for,
v0.22.1 built them, and five rules moved.

The two that remain built-in are **permanent**, not pending. Both ask a different
*shape* of question — an aggregation, and a walk over a table of doctypes with
its clock on `creation` — and §5 says why turning either into a field would make
the vocabulary worse rather than wider.

---

## 3. Authoring a declarative rule

A declarative rule is evaluated like this, per row of `target_doctype`:

```
apply scope_filters (ALL must hold)
anchor          = row[date_field]                      (empty date_field → no clock)
if anchor is missing → missing_date_behaviour: Skip, or Raise at severity_expired
due             = anchor + cadence_days                (cadence 0 → the anchor IS the deadline)
days_remaining  = due - today
severity        = default_severity   if date_field_role is State (no clock at all)
                  severity_expired   if days_remaining < 0 or there is no clock
                  severity_critical  if days_remaining <= threshold_critical_days
                  severity_warning   if days_remaining <= threshold_warning_days (or window_field)
                  otherwise: say nothing
message         = render(message_template, row + computed context)
```

### The fields, and what each one is for

| Field | Meaning |
| --- | --- |
| `rule_id` | The stable key alerts are filed under. **First segment of every alert docname** — never change it on a live rule. |
| `target_doctype` | The DocType whose rows the rule walks. |
| `date_field` | The cadence anchor. **Leave empty** for a rule with no clock; every matching row then raises at `severity_expired`. |
| `date_field_role` | `Clock` (default), `Timestamp`, or `State` (v0.22.5 — the rule fires on a data state and the date is read for the message only). |
| `default_severity` | What a `State` rule raises at. Ignored on the other two roles. |
| `producer_assigned_to_expression` | v0.22.5. Sends the producer task to one named person rather than into a skill pool. Exclusive with `producer_skill_required`. |
| `cadence_days` | How often the activity must recur. 365 on a last-inspection date is the annual walk; 0 means the date field *is* the deadline. |
| `threshold_critical_days` / `threshold_warning_days` | Fire at this many days remaining or fewer. **Negative means the band never fires.** The warning threshold is also the outer window: outside it the rule says nothing. |
| `severity_critical` / `severity_warning` / `severity_expired` | The severity of each band. Separate fields because `filing_response_due` escalates Info → Warning as the deadline passes, and a fixed ladder could not express it. |
| `missing_date_behaviour` | `Skip` for an expiry (a training with no expiry does not lapse). `Raise` for a cadence (a cabin nobody has ever inspected is the most overdue cabin there is). |
| `due_date_mode` | `From Anchor`, `Today`, or `None`. The calendar sorts on it. |
| `window_field` | A field on the row carrying its **own** lead time, used instead of `threshold_warning_days` — `renewal_window_days` on a certificate, because the turnaround an issuing body takes is a property of the certificate. |
| `scope_filters_json` | ANDed filters. See below. |
| `message_template` | Jinja, rendered in a sandbox with **no framework in it**. |
| `regimes` / `regimes_from_field` | The audits the alert answers to, from the rule or copied off the row. |
| `requires_doctypes` / `requires_fields` | What must exist for the rule to run at all. |

### v0.22.1's primitives

Four field groups, plus two small helpers each of them turned out to need. Every
one is **nullable and additive**: a rule that uses none of them is exactly the
rule v0.22.0 could already express, and `get_compliance_rule` reports empties for
the ones a rule does not use rather than nulls — an operator reading two rules
side by side should not have to know that a blank column and an empty list are
one thing.

**The order the gates run in is part of the contract**, and it is worth stating
because these primitives are the first ones that compose:

```
1. scope_filters          define the population — cheapest, purely local
2. gate_date_field        is this row's condition RIPE at all?
3. superseded_by_later_clean   is this finding still the LATEST word?
4. the clock              how far past due, and therefore which severity
```

A different order would not merely be slower. Running supersession before the
scope filters would mean a rule narrowed to one company reading another's records
to decide what it can see, which is the kind of thing nobody notices until an
auditor asks why an alert went quiet.

#### 1. `superseded_by_later_clean_json` — the gate about *other rows*

A finding stops being true when a **later clean record for the same subject**
supersedes it. A cabin re-inspected in September with nothing found says more
about July's water stain than a checkbox does, and it requires nobody to remember
a field. No filter on the finding's own row can answer a question about other
rows.

```json
{
  "subject_field": "unit",
  "clean_state_field": "workflow_state",
  "clean_state_values": ["Recorded"],
  "unreadable_counts_as_dirty": true
}
```

`doctype` and `date_field` default to the target's, which is what lets one rule
walking two doctypes supersede each on its own date column. Dates are compared as
text — correct for the ISO strings every date column here holds.

`unreadable_counts_as_dirty` defaults to **true** and should stay there: a record
whose state is empty or unreadable does **not** supersede. A result nobody can
interpret is not evidence that the water is safe, and treating it as clean is how
a compliance file becomes a clean record of nothing.

The index is built **once per sweep** and folded to a per-subject list, not
queried per candidate. A camp with fifty cabins and four years of history is two
queries, not four hundred — and that is a reason this is a field rather than a
`custom_python` program, where the obvious way to write it is the per-row query.

#### 2. `regime_heuristics_json` — an ordered lookup on a *name*

`regimes_from_field` copies tags off a column. This is for the case where there
is no column, only a **name to read**: a certificate's audits come from its TYPE
through an ordered table.

```json
[
  {"if_field_contains": {"field": ["cert_type", "cert_name"], "value": ["wps", "worker protection"]},
   "then_regimes": ["WPS"]},
  {"if_field_contains": {"field": ["cert_type", "cert_name"], "value": ["globalgap", "global gap"]},
   "then_regimes": ["GlobalGAP"]},
  {"if_field_contains": {"field": ["cert_type", "cert_name"], "value": ["gap"]},
   "then_regimes": ["GAP"]},
  {"default_regimes": ["Internal"]}
]
```

**First match wins, and the order is the whole content.** `globalgap` is checked
before `gap` because "GlobalGAP" contains "GAP", and a USDA GAP packet must not
be handed another scheme's certificate.

**Where entries name several fields, the field order is the OUTER loop**: the
whole table is tried against `cert_type` before any of it is tried against
`cert_name`. That is not an implementation detail. A certificate typed "Food
Safety Training" and named "WPS refresher" is a food-safety certificate, and a
table that scanned entry-first would retag it from a word somebody typed on the
day.

Matchers are `if_field_contains` (substring) and `if_field_in` (exact membership
of a closed list), each taking `case_insensitive` — true by default, and worth
turning off when the column is a Select rather than free text.

The heuristics say what an **alert** is. The rule's own `regimes` stays the
**union** of what the table can emit, because that is what
`refresh_compliance_alerts(regime=…)` matches on to decide whether the rule has
to run at all. Two different questions, both answered.

`category_heuristics_json` is the same shape producing the alert's **category**,
for the same reason: one rule fires on eleven kinds of certificate, an applicator
licence is a Workforce item and a GlobalGAP certificate is a Certifications one,
and a constant on the rule would file most of them under the wrong heading.

#### 3. `gate_date_field` + `gate_within_days` — a second date, used only as a gate

The declarative engine has **one** cadence anchor. Some rules are a *conjunction*
over two independent dates: a block raises a water-test alert when it was sprayed
inside the season **and** its water was tested outside the cadence, and neither
half fires alone. Ground nobody is spraying raises nothing however stale its
water.

```jsonc
"gate_date_field": "last_spray_date",
"gate_within_days": 120,
"gate_scope": "Direct"
```

**A row whose gate date is empty is gated OUT**, and that asymmetry with
`missing_date_behaviour` is deliberate: no inspection ever recorded is the most
overdue cabin there is; no spray ever recorded is the *least* urgent block there
is. The gate is a claim that the condition matters now, and no date is no claim.

`gate_scope: "Latest Related"` reads the newest date off **another doctype**
pointing back at the row, for a site keeping a spray *log* rather than a spray
*column*:

```json
{
  "doctype": "Farm Task",
  "subject_field": "field",
  "date_field": "completed_on",
  "subject_key": "name",
  "scope_filters": [{"field": "task_type", "op": "eq", "value": "Spray"}]
}
```

Read once per sweep and folded to a per-subject maximum, same as the supersession
index.

#### 4. `date_fields_json` — several anchors of the same kind

A cabin has a smoke detector and a CO detector, tested independently. **Either**
being stale fires, and the message must name which.

```json
[
  {"field": "smoke_detector_last_test", "label": "smoke"},
  {"field": "co_detector_last_test", "label": "CO"}
]
```

Each is measured against the same `cadence_days`; the severity folds to the worst
of them; the template is handed `stale_dates` — only the fields that actually
reached a band, each with its `label`, `date`, `days_since` and `days_remaining`
— plus `first_stale_label`.

"The CO detector was last tested 400 days ago" is a different errand from "no
smoke detector test has ever been recorded", and an alert saying only "a detector
is overdue" sends somebody to test the wrong one.

The labels are **fragments**, not sentences — "smoke", "CO" — because the
template around them supplies "detector". A label that reads as a sentence
produces a message that reads as a list.

#### The two helpers the four needed

**`date_field_role`** (`Clock`, the default, or `Timestamp`). A finding's date is
*when the thing was found*, not a deadline. Left as a clock, a corrective action
recorded today has zero days remaining, reaches no band on a rule whose
thresholds are negative, and silently stops firing on exactly the day somebody
needs to see it. `Timestamp` reads the date for the message and for the
supersession test, bands nothing, and raises every matching row at
`severity_expired` — including one with no date at all, because a finding nobody
dated is still a finding.

**`target_doctypes_json`**. `target_doctype` is singular by design and stays the
default. One shipped rule is genuinely about two record types, because a cabin
with a water stain and a cabin with a dead CO detector are the same conversation
with the same person on the same walk round the camp:

```json
[
  {"doctype": "Housing Inspection", "date_field": "inspection_date", "label": "the habitability inspection"},
  {"doctype": "Detector Test", "date_field": "test_date", "label": "the detector test"}
]
```

`label` is the fragment the message says instead of the doctype name — "the
detector test" reads like the errand it is and "Detector Test" reads like a
table. A doctype in the list that a site has not got is skipped, not fatal.

### v0.22.5's primitive: firing on a data state

Every primitive above still ends at a **clock**. The gate decides whether a row
is worth asking about; the clock decides what it raises. v0.22.5 is the release
where a rule can have no clock at all.

#### `latest_child_field_threshold_json` — the newest child row, and a number on it

A sibling of `gate_related_table_json` rather than an extension of it, and **the
difference is the fold**. `gate_related_table_json` folds a related doctype to one
*value* per subject — the maximum date — and asks how old it is. This one folds to
one *row* per subject, the latest, and then asks about its other columns.

A maximum over dates cannot answer "and what was the temperature on that row",
because the answer is not a maximum of anything: the 85 °F reading at noon says
nothing about compliance at four o'clock if a 72 °F reading was written at half
past three. Extending the existing primitive would have meant one field whose fold
changed depending on which of its keys were set, which is the kind of thing that
reads fine and is impossible to reason about six months later.

```json
{
  "child_doctype": "Farm Shift Weather Reading",
  "parent_field": "parent",
  "parentfield": "weather_timeline",
  "subject_key": "name",
  "order_by": "reading_datetime",
  "context_key": "latest_weather",
  "match": "any",
  "conditions": [
    {"field": "temp_f", "op": "gte", "threshold": 80,
     "threshold_source": "weather.heat_threshold_temp_f"},
    {"field": "heat_index_f", "op": "gte", "threshold": 80,
     "threshold_source": "weather.heat_threshold_heat_index_f"}
  ]
}
```

| Key | Meaning |
| --- | --- |
| `child_doctype` | Where the rows live — a child table's own doctype |
| `parent_field` | The column on the child naming the scanned row. `parent` by default |
| `parentfield` | Which child table of the parent, where the doctype hangs off more than one. Without it a rule about a weather timeline can start answering questions about a crew list |
| `subject_key` | The column on the **scanned** row the children name. `name` |
| `order_by` | The column "latest" is measured on. **Required** — without it the rule reads whichever row the database handed back first, which is an answer that changes when somebody adds an index |
| `context_key` | What the message template calls the row |
| `match` | `any` (an OR over the conditions, the default) or `all` |
| `conditions` | `{field, op, threshold, threshold_source}`. Ops are `gte`, `gt`, `lte`, `lt`, `eq`, `ne` |
| `scope_filters` | Which child rows count at all, in the rule's own filter vocabulary |

**`threshold_source` is the interesting key, and it is a closed registry.** It
names one of a handful of settings this app already resolves per company:

| Source | Number |
| --- | --- |
| `weather.heat_threshold_temp_f` | The ambient heat threshold on Weather Settings, per company (default 80 °F) |
| `weather.heat_threshold_heat_index_f` | The heat-index threshold, per company (default 80 °F) |
| `weather.wind_threshold_mph_spray_block` | The spray-block wind threshold, per company |

It is closed because "read a number from somewhere" is one sentence away from
"read anything from anywhere". What it buys is that the alert layer and the
**v0.19.4 shift sweep read the same number**: an entity that decided its own heat
threshold is 75 sets it once, and a literal on the rule would make the two layers
disagree about the same afternoon on the same shift, invisibly, until somebody
compared two records. The literal `threshold` stays on the condition as the floor
the setting falls back to — a site whose Weather Settings have not migrated gets
the regulation's number rather than nothing.

Three behaviours worth stating because they are all the *safe* direction and none
of them is obvious:

- **A subject with no child row is gated out.** A shift whose weather timeline is
  empty is not a cool shift; it is a shift nobody has a reading for, and raising
  off no reading would be this app asserting a fact it does not have.
- **A child row whose field is empty does not satisfy its condition**, so
  `match: "all"` fails on it. A reading with no temperature is not a cool reading.
- **The comparison is numeric only**, and this is the one place `_passes` is
  *not* reused. `_passes` falls back to a lexical comparison when a side is not a
  number, which is right for a scope filter full of ISO dates and exactly wrong on
  a thermometer: a reading somebody typed as `"warm"` sorts after `"80"`.

The index is built **once per sweep**, capped at `SCAN_CAP`, and folded in Python
— the same shape as the supersession index and for the same reason. Twelve open
shifts each carrying a reading every fifteen minutes is one query, not twelve.

#### `date_field_role: "State"` and `default_severity` — a rule with no clock

`default_severity` alone is not enough, and the reason is a database fact rather
than a design preference. `threshold_critical_days` and `threshold_warning_days`
are **Int columns**, so "no threshold" and "a threshold of zero" are one value —
and zero is a real setting meaning "fire on the due date itself". A shift that
started this morning is zero days from its own start, so a rule read as a clock
says *Critical* about a crew who are merely at work. Nothing the engine can read
off the numbers distinguishes the two cases. The rule has to say which it is.

So `State` is a third `date_field_role`:

| Role | The date is | The severity is |
| --- | --- | --- |
| `Clock` | a deadline; its distance picks the band | `severity_critical` / `severity_warning` / `severity_expired` |
| `Timestamp` | when the thing was found; bands nothing | `severity_expired`, on every matching row |
| `State` | read for the message only | `default_severity`, on every row the gates let through |

v0.22.1 refused a third role value deliberately, and **this is not the one it
refused**. What it refused was a role that inverted the *sign* of every threshold
beside it — a number meaning days elapsed where the same number on twelve other
rules means days remaining, which is a number somebody will eventually misread.
`State` reads no thresholds at all, so there is no number left to misread. The
band it reports is the word `state` rather than a reused `expired`, because an
alert saying "expired" about a condition with no expiry is a word an auditor
would be right to query.

`State` also outranks the per-row window, which is the one thing that outranks
everything on a clock. A shift is not less hot for having a short lead time.

#### `producer_assigned_to_expression` — a person, not a pool

Until v0.22.5 a producer task routed one of two ways: through an Inspection
Template (claimed out of the pool by whoever holds the template's skill) or as a
plain Farm Task with a `skill_required` and a `dispatch_mode`. Both are **pools**.

Some obligations are not a pool's. OAR 437-004-1131 asks what the supervisor did
about the heat, and the only person who can answer is the one who was standing
there. So the rule may carry an expression over the alert's source row:

```json
"producer_assigned_to_expression": "row.foreman"
```

It is evaluated in the same sandbox as `custom_python` — same grammar, same
refusals, same budget — and vetted on save rather than on the afternoon somebody
needed the task. Where it resolves, the task is inserted `Claimed`, with
`dispatch_mode` = `Dispatched`, an open Farm Task Assignment, and **no skill**.

**The two routings are exclusive and both doors refuse the combination.** A skill
is a pool and an assignee is a person; a task carrying both is a task whose holder
depends on which one the dispatcher read first, and the failure is silent in the
worst direction — the foreman who was standing in the heat never sees it, and
somebody with the skill closes it from a desk.

An expression that names nobody, or names somebody payroll has never heard of,
puts the task back on skill routing and says so in `routing_notes`. It is never
left as `Dispatched` with nobody on it: that is a task sitting in Available which
no worker is allowed to claim — visible, urgent and unreachable.

The same release also made the producer path read the **record** where
`ALERT_TASK_MAP` has nothing to say. Since v0.22.0 every rule had carried
`producer_farm_task_type`, `producer_skill_required` and `evidence_contract` —
seeded *from* that table, so the two could not disagree — and nothing read them
back. A rule authored after the framework shipped therefore had a producer recipe
on its record and landed in `skipped_unmapped` anyway. The table is still
consulted before the record's inline fields, which is what keeps the thirteen
shipped rules producing exactly the tasks they always did.

#### v0.41.0: the producer recipe became a record of its own

The three inline fields work, and what they cannot do is be **shared**. Two rules
asking for the same job state it twice, in full, and drift the first time one of
them is edited.

`producer_task_template` now names a **Farm Task Template** — a record holding
the shape of one job: its type, its skill, its duration, its dispatch mode, its
evidence contract, the compliance record its completion produces, the
instructions a worker reads and the items they tick off. Where a rule names one,
**the template is the whole recipe** and the three inline fields are not read at
all. They stay as the fallback for a rule with no template, which is most of
them.

So the producer path resolves in three steps, in this order:

1. the rule's `producer_task_template`;
2. `ALERT_TASK_MAP`;
3. the rule's own inline `producer_*` fields.

Step 1 costs **one query for the whole sweep**, not four per alert; on a site
where no rule names a template it is an empty dict and every alert takes exactly
the path it took before.

The alert still supplies what only it knows — the severity that becomes urgency,
the place the work happens, and its own message, which goes on the task *after*
the template's standing instructions, because a worker needs "how this job is
done" before "what is wrong with this particular cabin".

**The field used to point at an Inspection Template and nothing ever read it.**
Multi-section visits are raised by `_bundle_into_sessions`, which matches a
template's sections against the records a place's pending alerts are asking for,
and never consulted this column. Repointing it gave the field behaviour for the
first time; the `repoint_producer_task_template` patch clears any value still
naming an Inspection Template and prints each one by name, because a Link whose
target moved refuses the next save of any row holding an old value. Bundling is
unchanged.

**Seeding wires nothing.** `bench migrate` seeds five Farm Task Templates whose
type, skill, duration, dispatch mode and evidence contract match `ALERT_TASK_MAP`
to the letter — asserted by a test — and leaves `producer_task_template` exactly
as it was on every rule. Pointing a rule at a template is a deliberate act, by
somebody who has read what the template asks for.

#### One more operator, and the trap it exists for

`istrue` / `isfalse` joined the scope-filter vocabulary in v0.22.1, and they are
the **only** correct way to filter on a Check box. A Check read back before it has
been through the database layer carries the *string* `"0"`, which `isnotnull` —
and every other truthiness test — calls true. On the rule that needed it, the
wrong answer is "this shed is a worker facility", which puts a building nobody
sleeps in on the camp's inspection list.

### Scope filters, and why `default` is load-bearing

```json
[
  {"field": "status", "op": "eq", "value": "Active", "default": "Active"},
  {"field": "unit_type", "op": "nin", "value": ["Toilet-Shower", "Kitchen"], "default": ""}
]
```

Operators: `eq`, `ne`, `gt`, `lt`, `gte`, `lte`, `in`, `nin`, `isnull`,
`isnotnull`, `istrue`, `isfalse`, `contains`, `ncontains`, and `any`.

**`any` is the one disjunction, and it arrived in v0.138.0 for one rule.** The
list above is ANDed, which is right and covers every shipped rule but
`i9_section_1_unsigned`: an I-9 section is attested *either* by a signature image
captured at a pad the server was holding *or* by the sealed page arriving with
both signing moments recorded beside it, so "not attested" is an AND of an OR and
no arrangement of ANDed filters says it.

```json
[
  {"field": "section_1_signature", "op": "isnull"},
  {"op": "any", "value": [
    {"field": "signed_pdf", "op": "isnull"},
    {"field": "section_1_signed_at", "op": "isnull"},
    {"field": "section_2_signed_at", "op": "isnull"}
  ]}
]
```

A group names no `field` of its own and passes when at least one filter nested in
its `value` passes. It goes **one level deep** — `any` inside `any` is refused at
authoring time, because a boolean expression language belongs in `custom_python`,
which this rule already has, and a nested-group vocabulary stopping short of that
would be a worse version of both. An empty group is refused for the reason every
other check in `parse_filters` exists: it passes every row, so the rule would look
scoped and be unscoped. A group none of whose columns this site has **passes**,
which is the same fail-safe direction every absent column takes here — a group can
only ever exclude rows, so passing it widens the scan and says so in a warning
rather than going quiet on a site that has not run `install_compliance_fields`.

**Filters are evaluated in Python, not pushed into SQL, and `default` is why.**
In SQL, `status != 'Active'` excludes every row whose status was never set —
which on a new camp is most of them. Three of the shipped rules read a column
whose empty value means something specific:

- a Compliance Policy with no status **is in force**,
- a Regulatory Filing with no status is **neither Draft nor Withdrawn**,
- a Housing Unit with no condition is **not Uninhabitable**.

`default` says out loud what the legacy `str(row.get("status") or "Active")` said
in an idiom. Omitting it where it matters is how a rule goes quiet on exactly the
records nobody has touched.

A filter naming a field this site has not got is **skipped and reported** in
`computation_warnings`, not treated as a failed row — half this app's compliance
columns are installed on demand, and a rule that refused every row on a site that
had not run `install_compliance_fields` would look exactly like a clean
operation.

#### Dynamic values: the four template variables

A filter's `value` may be a template instead of a literal, resolved **at sweep
time** rather than when the rule was saved:

| Template | Resolves to | Added |
| --- | --- | --- |
| `{{current_year}}` | the calendar year, as a number | v0.68.0 |
| `{{current_month}}` | the calendar month, as a number | v0.68.0 |
| `{{current_date}}` | today, as `YYYY-MM-DD` | v0.68.0 |
| `{{current_datetime}}` | now, as `YYYY-MM-DD HH:MM:SS` | v0.69.0 |

"Current year" is not a fact a rule authored today can hardcode, because the rule
is still meant to be true next January — `w4_tax_year_outdated` is the rule that
was missing this. **Matched whole-string only:** `"{{current_year}}"` resolves
and `"before {{current_year}}"` is refused at authoring time, because a filter
value is compared against a column as a number or as exact text, never as a
blended string. Lists resolve element-wise, for `in` / `nin`. The registry is
closed, for the same reason `custom_python` runs in an interpreter this app
wrote: "resolve a template" is one sentence away from "evaluate an expression".

**`{{current_datetime}}` is what gives a rule an hour hand.** Every threshold in
this vocabulary counts *days*, which is right for a certificate and useless for a
restricted-entry interval: a four-hour REI on a block sprayed at two in the
afternoon expires at six, and a rule that could only compare dates would either
hold the alert until midnight or drop it at breakfast. Against a Datetime column
it compares as ISO **text**, which sorts correctly to the second — so
`{"field": "rei_expires_at", "op": "gte", "value": "{{current_datetime}}"}` is
true exactly while the block is shut. **That comparison is the auto-dismiss:**
the sweep stops observing the row and `alerts/base.py` dismisses what it stops
observing. Nothing in the engine had to learn about hours.

### Message templates

Jinja, rendered by `jinja2.sandbox.SandboxedEnvironment` with **no `frappe` in
the globals** — deliberately not `frappe.render_template`, whose environment
carries the framework and would be a second, undocumented escape hatch beside the
one this release spent a module sandboxing.

Available: every field on the row by name, plus `row`, `days_remaining`,
`days_overdue`, `days_since_anchor`, `anchor`, `due_date`, `today`, `severity`,
`regimes`, `subject`, `cadence_days`, `threshold_critical_days`,
`threshold_warning_days`, `rule_title`, `regulation_citations`.

A template that fails to render produces a plain, honest fallback and a warning
rather than killing the rule. **An ugly alert is a problem somebody fixes; a
missing one is not.**

Write the message somebody will actually act on. `"WPS handler training expires
in 12 days and he cannot lawfully spray after that"` is a different decision from
`"expires in 12 days"`.

---

## 4. `custom_python`: what it is, and when not to use it

A short restricted program, evaluated by `alerts/sandbox.py` — an **AST
interpreter**, never `exec`, never `eval`.

### What is in scope

`frappe` (a read-only facade: `get_all`, `get_value`, `get_doc`, `exists`,
`count`, and a `utils` namespace), `today`, `company`, `target_doctype`,
`doctype_meta`, `rule`, `regimes`, the rule's thresholds, `observation(...)`,
`warn(...)`, `days_until`, `days_since`, `datetime`, `timedelta`, the severity
constants, and the safe half of the builtins. **There is nothing else** — a name
the caller did not provide is a refusal listing what is in scope.

`frappe.get_doc` returns a **plain dict**, not a Document. A Document has
`.save()` on it, and a read-only sandbox that hands back a live document is
read-only in the same sense a locked door with the key in it is locked.

### What is refused, and why

| Refused | Why |
| --- | --- |
| `import`, `from … import` | One import is `os`, and `os` is the filesystem. |
| `exec`, `eval`, `compile`, `open`, `globals`, `locals`, `getattr`, `setattr`, `type`, `object`, `super` | Each is a way back to the interpreter the allowlist just removed. |
| **every underscore-prefixed attribute** | `x.__class__.__bases__[0].__subclasses__()` is the standard escape from every sandbox that forgot this, and it needs no imports at all. |
| `while` | Unbounded by construction. `for` over a sequence is bounded by the sequence. |
| `def`, `class`, `lambda`, `yield` | A rule that needs to define a function has outgrown this field. |
| `try` / `except` | A rule that swallows its own errors is a rule that goes quiet, which is the failure this whole app is written against. |
| `with`, `del`, `global`, `nonlocal`, `assert`, `raise`, `await`, `:=` | No use case, and each is a surface. |

Bounded at **200,000 node visits** and **5 seconds** of wall clock. A program
that exceeds either is reported against that one rule; the other rules still run,
because the sweep has never let one rule take the night down.

A refused or failed program does **not** silently observe nothing. It raises a
Warning against the rule itself saying the condition is now **UNWATCHED** — a
compliance rule that quietly stops watching is worse than one that visibly
breaks.

### Why RestrictedPython or asteval are not used

Both are good libraries, and neither is on this bench. `pyproject.toml` has three
runtime dependencies, each argued for and each imported defensively so a bench
missing one loses a named feature rather than the app. Adding a fourth for a
field that ships **used by zero of the shipped rules** would be the tail wagging
the dog. The subset actually needed here is small and closed — read some rows,
compare some dates, build some observations — and an interpreter for that subset
has no supply chain and refuses by construction rather than by configuration.

### When to reach for `custom_python`

**You probably don't.** That was already the intention in v0.22.0, when the
answer rested on six declarative rules and a promise about four primitives. It
now rests on eleven and the primitives themselves, so it is worth saying plainly:

Before this field, check whether the question is one of these:

| The question your rule asks | The field that already asks it |
| --- | --- |
| is this date past due, and by how much? | `date_field` + `cadence_days` + the thresholds |
| …for **several** dates at once, naming which? | `date_fields_json` |
| only for rows matching some columns? | `scope_filters_json` |
| …including a **check box**? | `istrue` / `isfalse` — never `isnotnull` |
| only when a **second** date is recent? | `gate_date_field` + `gate_within_days` |
| …read off **another doctype**? | `gate_scope: Latest Related` |
| is this finding still the latest word on its subject? | `superseded_by_later_clean_json` |
| which audit / which category is **this row**? | `regime_heuristics_json`, `category_heuristics_json`, `regimes_from_field` |
| is the date a deadline, or a timestamp? | `date_field_role` |
| does one rule cover two kinds of record? | `target_doctypes_json` |
| what does the alert actually *say*? | `message_template` |

**The remaining honest uses are narrow**, and every one of them is a request for
the next primitive rather than a home for a program:

- an **aggregation** — group rows, fold to the worst, raise one alert per group.
  That is `audit_action_overdue`, and §5 argues it should stay code rather than
  become either a field or a program.
- arithmetic across **three or more** columns that no threshold expresses.
- a lookup against something structural this app models in Python and not in a
  column.

If you are reaching for it for anything else, say in one sentence what *shape* of
question your rule asks. If that sentence fits in the table above, the answer is
a field. If it does not, **file it** — the table above is exactly the list of
sentences somebody filed against earlier versions of this document.

### The rule of thumb

> If you can say in one sentence what *shape* of question your rule asks, that
> shape probably wants to be a declarative field rather than a program.

---

## 5. The migration of the five, and the two that stay

This section was v0.22.0's backlog. It is now v0.22.1's changelog, and it ends
with a shorter list than it started with.

### What moved, and what each rule cost

| Rule | Primitive it was waiting for | Also needed |
| --- | --- | --- |
| `housing_corrective_action_open` | `superseded_by_later_clean_json` | `target_doctypes_json`, `date_field_role` |
| `water_test_contamination` | `superseded_by_later_clean_json` | `date_field_role` |
| `certification_expiring` | `regime_heuristics_json` | `category_heuristics_json`, a band-order fix |
| `water_test_stale` | `gate_date_field` + `gate_within_days` | — |
| `housing_detector_test_stale` | `date_fields_json` | the `istrue` filter operator |

**The hardest was `housing_corrective_action_open`**, and not because of
supersession — that was the primitive the backlog ranked first and it landed
cleanly enough to take `water_test_contamination` with it for free, which is the
two-for-one v0.22.0 predicted. It was hard because of the two things nobody had
written down:

- it walks **two doctypes** under one `rule_id`, and `target_doctype` is singular
  by design. Splitting it into two rules would have split one walk round the camp
  across two alert types and changed every alert docname on every site;
- `inspection_date` is a **timestamp, not a deadline**. As a clock, a finding
  recorded today has zero days remaining, reaches no band with this rule's
  negative thresholds, and stops firing on exactly the day somebody needs to see
  it. That bug does not show up on a fixture dated last month, which is what
  makes it worth naming here.

The band-order fix `certification_expiring` needed is the same kind of thing.
`_band` used to check the critical threshold before the outer window, which is
indistinguishable from the shipped scanner until the **per-row** window is
narrower than the rule's critical threshold — a certificate whose issuing body
turns renewals round in ten days. The window is the claim about when the work can
usefully start, and nothing inside the rule outranks it. The window is now checked
first, which is what the Python always did.

### `audit_action_overdue` stays built-in. Permanently.

It walks an Audit Event's corrective-action **child rows**, keeps the overdue
ones, picks the worst, takes its severity from that finding's own severity, and
raises **one alert per audit** rather than one per action — five open items on one
PrimusGFS audit are one conversation with one auditor, and five rows would look
like five problems.

Every part of that is an aggregation, and an aggregation is not a filter. The
primitive would be a second engine: group-by, fold, pick. That is a fair
description of "write it in Python", and building it as a field group would give
the vocabulary a shape nothing else in it has, for one rule.

This is not a gap in the vocabulary. It is a different kind of question, and the
honest answer is a reviewed, tested, shipped scanner with every tunable still on
the record.

### `supervisor_review_lapsed` stays built-in. Permanently.

Three reasons, any one of which is enough:

1. **It walks a table of doctypes, not one.** `REVIEW_TARGETS` is the list of
   records carrying the §112.161(b) review columns, written to grow — Housing
   Inspection, Water Test, Heat Exposure Event and Farm Task Assignment are each
   one row away. v0.22.1's `target_doctypes_json` reaches *two* related camp
   records with one shared shape; this is a registry of unrelated ones with
   different columns, and stretching the field to cover it would make the field
   worse for the rule it was built for.
2. **The condition is an `OR` of two nulls.** A record is unreviewed when the
   reviewer is missing *or* the date is missing — a date with nobody attached is
   what an auditor is trained to disbelieve. Scope filters are ANDed
   deliberately; an OR-of-filters is a query language, and a query language in a
   text field is what `custom_python` already is.
3. **The clock runs on `creation`, not on the activity date.** §112.161(b)'s own
   words are "after the records are made", and reading the activity date would
   raise a Critical on every record of a season somebody backfilled.

And the strongest argument is the one about the numbers: this rule's thresholds
mean days **elapsed**, not days **remaining**. The thing measured is an absence
getting older rather than a deadline approaching. A number on a record that means
the opposite of what the same number means on the other twelve is a number
somebody will eventually misread — and `date_field_role` deliberately did not
grow a third value to paper over it. Two readings of a date field is a choice; a
third that inverts the sign of every threshold beside it is a trap.

### What "permanent" means here

It means the answer is not "later". Both rules keep every tunable on their record
— thresholds, scope filters, citations, regimes, packet list, switch — and only
the shape of the join is code. `list_compliance_rules` reports them as
`builtin_scanner`, and `get_compliance_rule` says on the row itself that the
shape is a scanner rather than a gap.

Two out of thirteen is the honest measure of a vocabulary: wide enough that the
exceptions can be named, narrow enough that naming them is worth doing.

## 6. Provenance, approval and audit

### The gate

`enabled` cannot be set without `human_approved_by` **and**
`human_approved_on` — the DocType refuses it. Both, because a review date with
nobody attached is what an auditor is trained to disbelieve.

There is no path by which a rule starts firing without a person having put their
name to it. That matters most for the case that does not exist yet: **"a model
wrote a rule and it went live" must never be a true sentence about this app.**

`create_compliance_rule` always writes a Draft, whatever the caller asked for.
Only `approve_compliance_rule` enables one.

### Versioning by copy

`update_compliance_rule` writes a **new row at version+1** and points the old
row's `superseded_by` at it. The old row is disabled, never edited, never
deleted.

Two consequences, and both are the point:

- a sweep that started against v1 **finishes against v1** — there is no window in
  which a running evaluation's definition changes underneath it;
- an alert raised last April can still be read against the definition that raised
  it, thresholds and citation as they were.

The new version **inherits** the old one's approval. A threshold moved is not a
new rule, and forcing re-approval on every tuning edit trains people to click
through approvals, which is worse than not having the gate. A rule that was off
stays off.

### One live row per `rule_id`

Enforced in the controller rather than by a unique index, because the constraint
is not on the column: v1 and v2 share a `rule_id` by construction. What may not
exist twice is a row that is **enabled and unsuperseded** — two definitions of
one rule, where the one tonight's sweep ran would be whichever sorted first.
`active_row_flag` materialises that condition as an indexed column.

### Off is not deleted

`deactivate_compliance_rule` requires a reason of at least a sentence and appends
it to the rule's purpose. The rule raises nothing **and dismisses nothing** — the
alerts it already owns stay exactly as they were, which is the same reading a
rule skipped for a missing DocType gets, and for the same reason: **switching a
rule off is not evidence that anybody did the work.**

There is deliberately no delete.

### The audit trail

Every call writes an MCP Action Log row through `registry.dispatch` — arguments,
caller, timestamp, result. `update_compliance_rule` additionally returns a
field-by-field `changes` diff, so the log row records what the rule said
**before** and not merely what was asked for. Combined with the DocType's own
`track_changes` history and the superseded rows themselves, "who changed this
rule and when" is answerable without leaving the app.

---

## 7. Migration and idempotency

The fourteen shipped rules are seeded into records by `install._compliance_rules()`
on install and after **every** migrate.

**It is a seeder, not a Frappe `fixtures` entry**, and `test_hooks.py` forbids
that word by name. A fixture is imported by `bench migrate` with no ability to
skip what a site already has, so an operator who raised a threshold would have it
corrected back on the next upgrade. The seeder checks for the `rule_id` **across
every row, not only live ones**, before it writes:

- a rule somebody edited keeps the edit;
- a rule somebody switched off stays off;
- a rule somebody superseded with their own v2 does not get v1 seeded back beside
  it — which would give the `rule_id` two live rows and make the sweep's answer
  depend on sort order.

**Migrated rules arrive `enabled = 1`**, against this app's usual instinct that
everything mutating ships off. They were *already running* — as Python — the
night before, and seeding them disabled would silently switch the whole
compliance calendar off during an upgrade.

**Until the migrate runs, the sweep falls back to the shipped definitions and
says so** in `engine_notes` on its report. A compliance calendar that quietly
emptied itself for the length of an upgrade would be the single worst failure
this app could have.

### Backward compatibility, asserted rather than asserted-to

`test_compliance_rule_engine.TheMigrationChangesNothing` builds one fixed
database, runs the sweep with the shipped Python rules, snapshots every alert
row, deletes the alerts, seeds the thirteen records, runs the sweep again through
the record-driven engine, and compares the two snapshots **field by field** —
docname, severity, category, company, source, message, due date, first seen.

Not counts. Not "an alert of this type exists". The rows.

`test_ccf_primitives.TheMigrationOfEachRuleChangesNothing` does the same thing
for v0.22.1, once per migrated rule, on a fixture built to make that rule speak
in as many of its shapes as it has — and it uses the **shipped scanner itself**
as the oracle rather than a pasted expectation, by pointing the seeded rule back
at its `builtin_scanner`, snapshotting, then pointing it at its declarative
definition and snapshotting again. `test_e2e_workflow` then does it once more over
a whole operation with all thirteen rules running together, because the
interesting failures of a migration like this are not inside one rule.

### The upgrade a v0.22.0 site actually gets

**The seeder cannot perform v0.22.1's migration, and that is the same property
that makes it safe.** It leaves alone anything already on the site, so a site
that installed v0.22.0 has a `certification_expiring` row naming a built-in
scanner that the seeder will never look at again.

`patches/migrate_declarative_rules.py` does it deliberately, and answers the two
questions the seeder never faces:

- **an operator's edits survive.** Everything both shapes share is carried across
  from the row that is on the site — thresholds, severities, cadence, citations,
  regimes, retention, packets, producer recipe, approval, switch. A site that
  contracted its annual detector cycle at ten months still has ten months
  afterwards. Only the fields describing the *shape* of the scan come from the
  shipped definition. `scope_filters` is **concatenated**, not replaced: in
  v0.22.0 a built-in seeded with an empty filter list, so anything in that column
  was added by an operator on top of the scanner's own scoping, and dropping it
  would silently widen a rule somebody narrowed. `extra_parameters.spray_season_days`
  is read across into `gate_within_days`, so a tuned spray season is not quietly
  ignored by the new gate.
- **the old row is superseded, not edited** — same as `update_compliance_rule`,
  same reasoning. An alert raised last April can still be read against the
  definition that raised it, and a sweep already running against v1 finishes
  against v1.

It is listed in `patches.txt` **and** called from `after_migrate`, so it runs at
least twice on any real bench and is a no-op the second time: the check is "does
this row still name a scanner", which is false the moment the first run
succeeded. A rule it could not migrate keeps its built-in scanner — which still
ships, still runs and still raises exactly the same alerts — and is named on the
console.

---

## 8. Worked example

An operation decides to watch for something no shipped rule covers: a cabin's
occupancy limit has to be posted in it (OAR 437-004-1120).

```jsonc
// 1. Author it. It arrives as a DRAFT and fires nothing.
create_compliance_rule({
  "rule_id": "cabin_capacity_unposted",
  "title": "A cabin in use has no posted occupancy limit",
  "category": "Housing",
  "target_doctype": "Housing Unit",
  "date_field": "",                    // no clock: the condition is true or it is not
  "severity_expired": "Warning",
  "threshold_critical_days": -1,
  "threshold_warning_days": -1,
  "due_date_mode": "Today",
  "scope_filters": [
    {"field": "unit_type", "op": "eq", "value": "Cabin"},
    {"field": "capacity", "op": "gt", "value": 0},
    {"field": "condition", "op": "ne", "value": "Uninhabitable", "default": ""}
  ],
  "message_template":
    "{{ name }} sleeps up to {{ capacity }} and the occupancy limit is not recorded as posted "
    "in the unit. OAR 437-004-1120 expects it where the people it protects can read it.",
  "regimes": ["OR-OSHA"],
  "regulation_citations": "OAR 437-004-1120(3)(b)",
  "kairotic_gate_description":
    "Fires on a cabin that can actually be slept in — a shower block raises nothing, and a "
    "unit marked Uninhabitable raises nothing because there is nobody in it to protect. "
    "It goes quiet when the limit is posted.",
  "audit_packet_types": ["OSHA"]
})

// 2. See what it WOULD do. Writes nothing.
test_compliance_rule({"name": "cabin_capacity_unposted"})

// 3. A human approves it. There is no other way to turn it on.
approve_compliance_rule({"name": "cabin_capacity_unposted"})

// 4. It is watching tonight, and its alerts are in the OSHA packet.
refresh_compliance_alerts({"company": "Highland LLC"})
```

No release. No deploy. No engineer.

A year later the citation is renumbered:

```jsonc
update_compliance_rule({
  "name": "cabin_capacity_unposted",
  "regulation_citations": "OAR 437-004-1121(3)(b)",
  "reason": "OR-OSHA renumbered the housing rule in the March 2027 revision"
})
```

That writes v2, disables v1, and leaves v1 fully readable — so an alert raised
under the old citation still shows the citation it was raised under.

### A second worked example: the rule that fires on the weather

`shift_heat_threshold_crossed` ships seeded and enabled in v0.22.5, and it is the
first rule this app has ever shipped that was **authored as a record**. There is
no Python behind it and there never was, which is why it is worth reading in
full: it is what the vocabulary looks like when nothing is being migrated into it.

```jsonc
create_compliance_rule({
  "rule_id": "shift_heat_threshold_crossed",
  "title": "An open shift's latest weather reading has crossed OR-OSHA's heat threshold",
  "category": "Workforce",
  "target_doctype": "Farm Shift",
  "requires_doctypes": "Farm Shift, Farm Shift Weather Reading",

  // NO CLOCK. The shift's start is read for the message and bands nothing.
  "date_field": "start_datetime",
  "date_field_role": "State",
  "default_severity": "Warning",
  "due_date_mode": "None",
  "threshold_critical_days": -1,       // belt and braces: never bands, even if
  "threshold_warning_days": -1,        // somebody edits the role back to Clock

  // ONLY OPEN SHIFTS. `end_datetime` is the fact and `status` is the summary of
  // it, which is why the v0.19.4 sweep filters on the first — the `default` on
  // the second keeps an imported row that never set the column.
  "scope_filters": [
    {"field": "end_datetime", "op": "isnull"},
    {"field": "status", "op": "eq", "value": "Active", "default": "Active"}
  ],

  "latest_child_field_threshold": {
    "child_doctype": "Farm Shift Weather Reading",
    "parent_field": "parent",
    "parentfield": "weather_timeline",
    "order_by": "reading_datetime",
    "context_key": "latest_weather",
    "match": "any",
    "conditions": [
      {"field": "temp_f", "op": "gte", "threshold": 80,
       "threshold_source": "weather.heat_threshold_temp_f"},
      {"field": "heat_index_f", "op": "gte", "threshold": 80,
       "threshold_source": "weather.heat_threshold_heat_index_f"}
    ]
  },

  "message_template":
    "Heat threshold crossed on {{ row.name }} at {{ row.location }} — latest reading "
    "{{ latest_weather.temp_f }}°F ({{ latest_weather.heat_index_f }}°F heat index) at "
    "{{ latest_weather.reading_datetime }}. Document water/shade/rest breaks per "
    "OAR 437-004-1131.",

  // THE FOREMAN, BY NAME. Not a skill pool — the record is a judgement by the
  // person who was standing there, and it carries their signature.
  "producer_farm_task_type": "Compliance-Audit",
  "producer_assigned_to_expression": "row.foreman",
  "evidence_contract": {"findings_text": true, "signature": true},
  "extra_parameters": {
    "producer_task_what": "Document the water, shade and rest cycle the crew took"
  },

  "regimes": ["OR-OSHA"],
  "regulation_citations": "OAR 437-004-1131 heat illness prevention",
  "retention_years": 3,
  "audit_packet_types": ["OSHA"],
  "kairotic_gate_description":
    "Fires on a WEATHER FACT, not a date. When the latest reading on an open shift is at "
    "or above the OR-OSHA heat threshold — currently 80°F on the ambient thermometer or "
    "heat index — the foreman gets a task to document the water, shade and rest cycle "
    "their crew took. Not a compliance decision by the app; the record is the foreman's, "
    "signed by the foreman, and this alert is what makes sure it exists. Silences by "
    "itself when the shift closes."
})
```

**Two systems observing the same fact at different cadences.** The v0.19.4
weather sweep runs every fifteen minutes, appends a reading and logs a *Threshold
Crossed* compliance event **on the shift** — the operational log, unchanged. The
rule sweep runs on its own schedule, reads the same open shift and the same
latest reading, raises an **alert**, and turns the alert into a task. The first
captures the fact; the second translates the fact into a required response.

They cannot collide, and not because either defers to the other: the shift's
event dedupes on the shift, the alert dedupes on `rule_id` plus the shift's
docname, and the task dedupes on the alert it answers. Run them in either order,
or twice, and there is one of each.

**And it goes quiet through no new mechanism at all.** The temperature drops and
the gate stops matching; the shift closes and the scope filters stop matching. In
both cases the rule observes nothing, and the sweep auto-dismisses what it did not
observe — exactly what happens when a certificate is renewed. The task the
foreman was given stays: a shift that closed is not evidence that anybody wrote
down the water and the shade.

---

## 9. What is deliberately NOT operator-editable

- **The sweep engine** — `alerts/base.py`. Reconciliation, idempotent docnames,
  auto-dismissal, the regime filter's refusal to dismiss what it did not run.
- **The Observation, Alert and Farm Task schemas.**
- **Security-critical logic** — role guards, kill switches, the audit log,
  transport authorisation.
- **MCP tool contracts** — these are API surface.
- **The sandbox itself**, and the allowlist it refuses by.

The rule *definitions* are data. The rule *engine* stays code.

---

## 10. Roadmap

| Version | What |
| --- | --- |
| v0.22.0 | Compliance Rule doctype, declarative engine, sandbox, migration of the thirteen, seven tools. **6 / 7 / 0.** |
| v0.22.1 | The four primitives §5 named, plus `date_field_role`, `target_doctypes_json`, `category_heuristics_json` and the `istrue` filter operator. Five rules migrated; the remaining two argued as permanent. **11 / 2 / 0.** No new tools — the surface stays at 256. |
| **v0.22.5** (this) | `latest_child_field_threshold_json`, `date_field_role: State` + `default_severity`, and `producer_assigned_to_expression`. One new rule — `shift_heat_threshold_crossed`, the first this app ships that is only data — and the producer path now reads the record where `ALERT_TASK_MAP` has nothing to say. **12 / 2 / 0.** No new tools — the surface stays at 256. |
| v0.23.5 | `propose_compliance_rule` wired: AI reads a regulation, drafts a rule with `authored_by = AI-proposed`, `enabled = 0` and an `ai_source_citation`; a review queue in Desk. |
| v0.24.5 | Regulation Feed doctype + scheduled re-evaluation: registered sources are re-read, and regulations that moved produce change proposals. |

The auditor test for all of it: *an auditor asks about a rule that changed last
month, and the record shows `human_approved_on` two weeks ago with an
`ai_source_citation` pointing at the Federal Register notice.* Proof the
operation tracked, evaluated and adopted the change — on the record itself.
