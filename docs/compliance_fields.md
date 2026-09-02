<!-- SPDX-License-Identifier: MIT -->
# Compliance fields on operational DocTypes

**v0.15.0.** Every field this app adds to a DocType it did not create, which
framework wants it, and — the part that matters — what breaks in the day-to-day
work if it is missing.

---

## Why this file exists, and why the fields are where they are

erpnext_mcp promises that installing it adds no field to any DocType it did not
create. `hooks.py` says so, `before_uninstall` is built around it, and v0.7.0's
asset tooling keeps its cost split in an `Asset Cost Profile` beside ERPNext's
Asset rather than in custom fields grafted onto it — because a DocType of ours
goes with the app and a field on theirs does not.

**Sprint 7 breaks that promise, once, on purpose.**

Compliance is a lens on operational data rather than a duplicate set of records.
Every spray IS an EPA and Worker Protection Standard record. Every hire IS an
I-9 record. Every bucket IS an FSMA traceability record. The bolt-on version of
this feature is a "Spray Compliance Log" that somebody fills in *after* doing the
spraying, and it fails the only test that matters:

> Does removing the feature break **operations**, or only break **compliance
> reporting**?
>
> * breaks operations too → compliance is woven in correctly
> * only breaks reporting → it is a shadow layer; refactor

A shadow log drifts from reality the first busy week of harvest, and an auditor
who finds two records of one spray that disagree has found something far worse
than a missing field. So the applicator's name, the EPA registration number, the
restricted-entry interval and the pre-harvest interval go **on the spray
record** — where the person doing the spraying already is, and where leaving them
blank stops the spray being recorded at all.

The last column of every table below is that test, answered per field. A field
whose honest answer is "nothing breaks" is a shadow field and belongs in one of
the four external-evidence DocTypes instead. There is a test — `test_compliance_fields.py`
— that requires the sentence to exist, so a field cannot be added without
somebody confronting the question.

## What it costs, said plainly

Uninstalling erpnext_mcp from a site where these have been filled in **drops the
columns and everything typed into them.** The records themselves — the spray
logs, the employees, the bucket log entries — survive; the applicator names, EPA
registration numbers, REIs, PHIs, I-9 statuses and traceability links do not.
`before_uninstall` names every column by hand before it happens, with the
`bench backup --only-doctype` lines to run first.

That is a real cost and it is the right trade. An app that refuses to touch
anybody else's DocType cannot make compliance fundamental to operations; it can
only make it adjacent to them.

## How they are added

Every field is a **Custom Field**, which is Frappe's supported way for one app to
extend another's DocType. The target app's repository is untouched, its own
migrations keep working, and a later version of farm_precision_ag that ships
`epa_reg_number` itself finds this one already there rather than ending up with
two columns — the check is "is the field present at all", not "is there a Custom
Field row we wrote".

The installer runs on **every** `bench migrate` and is a no-op on the second run.
`install_compliance_fields` is the same installer on demand, with a `dry_run`
that reports what would happen without doing it.

It is behind `allow_install_compliance_fields`, which is the **only** mutating
switch in this app that ships ON — because a compliance field that arrives only
when an operator remembers to tick a box is a compliance field that is missing on
the sites that needed it most. Turn it off and no field is ever added, through
the hook or through the tool. `registry.DEFAULT_ON_MUTATING_TOOLS` names the
exception and argues for it, and a test asserts it is the only one.

## Graceful degradation

A DocType that is not on this site is **skipped by name**, with the app that
would bring it. A site without farm_precision_ag has no Spray Log and is told so;
it is not a failure and nothing else is disturbed. Install the owning app, run
the tool again, and only the newly-possible fields are added.

## The required fields, and the backlog they create

Seven of the twenty-four are required. Frappe enforces `reqd` **on save**, not
retroactively — so existing records stay readable and stop being re-saveable
until somebody fills the field in. That is the intended behaviour: a spray record
that never had an applicator was never compliant, and the field makes that
impossible to paper over.

It is also a surprise if nobody says it first, so the installer counts the rows.
`install_compliance_fields` reports `backlog` per field and `backlog_total`
across all of them. **That number is the operation's compliance debt, stated in
rows,** and it is the most useful thing either the hook or the tool produces on a
site with history.

---

### `Spray Log` — farm_precision_ag

Pesticide application records under FIFRA, the EPA Worker Protection Standard and Oregon's ORS 634. Every spray is a compliance event; these are the columns that make it one.

| Field | Type | Required | Framework | Why the regulator wants it | What breaks in the WORK without it |
| --- | --- | --- | --- | --- | --- |
| `applicator_name` | Data | **yes** | EPA WPS 40 CFR 170.309(f); ORS 634 / OAR 603-057 | Federal and Oregon pesticide records must name the person who made the application. Oregon additionally ties the record to a licensed applicator. | Nobody can be asked what the tank actually held, whether the nozzles were the high- or low-volume set, or why a block was skipped. The applicator is the only person who knows what happened in the field that day. |
| `epa_reg_number` | Data | **yes** | FIFRA; EPA WPS 40 CFR 170.309(f)(3) | The registration number identifies the product as registered for this crop and this use. It is the number a residue detection is traced back through. | The label is the law: without the registration number nothing downstream can check the product against the crop, the rate or the buyer's maximum residue limit, so a load can be rejected at the packing house with no way to find out which block it came from. |
| `rei_hours` | Int | **yes** | EPA WPS 40 CFR 170.407 — restricted-entry interval | The interval during which workers may not enter the treated area without PPE. Posting and notification obligations run off it. | THE crew-scheduling number. Without it nobody knows when the block can be picked, thinned or irrigated, and the crew boss guesses. This is the field that makes the compliance record and the work order the same record. |
| `phi_hours` | Int | **yes** | FIFRA label; FDA tolerances 40 CFR 180 | The pre-harvest interval: how long after application the fruit may not be picked. Violating it is a residue violation on a shipped load. | Harvest scheduling. A block sprayed inside its PHI cannot be picked, and the pick date is planned off this number weeks in advance. |
| `weather_temp_f` | Float | no | EPA WPS 40 CFR 170.309; label temperature restrictions | Many labels restrict application above a stated temperature, and an inversion is the usual cause of an off-target drift complaint. | Efficacy. Half the products in a tank behave differently at 90°F, and the reason a spray did not work is read out of this column the following week. |
| `weather_wind_mph` | Float | no | EPA label drift restrictions; ODA drift investigations | Nearly every label sets a maximum wind speed. It is the first thing an Oregon Department of Agriculture drift investigation asks for. | Whether to spray at all that morning, and the defence when a neighbour complains. Without it a drift complaint is unanswerable. |
| `wind_direction` | Data | no | EPA label drift restrictions; ODA drift investigations | Direction is what turns a wind speed into a statement about where the spray went, and about which neighbouring property was downwind. | Which end of the block to start at, and which rows to leave for a calmer day. A drift complaint from upwind answers itself. |
| `target_pest` | Data | no | FIFRA label use; IPM records for GAP / GlobalGAP | A product applied for a pest not on its label is an off-label application. Food safety audits ask for the IPM justification for every application. | The IPM loop. The threshold that triggered the spray and the assessment of whether it worked both key off the target pest; without it the next application is chosen blind. |

### `Employee` — farm_hr / hrms

Employment eligibility, tax withholding, the wage law that governs this person's pay, farm labor contractor licensing, and the language this person is trained and warned in. Every hire is a compliance event.

| Field | Type | Required | Framework | Why the regulator wants it | What breaks in the WORK without it |
| --- | --- | --- | --- | --- | --- |
| `preferred_language` | Select<br>`es` `en` | no | EEOC national-origin guidance (29 CFR 1606); OSHA 1910.1200(h) and the Worker Protection Standard 40 CFR 170.501, both of which require training and hazard communication 'in a manner the employee can understand' | Hazard communication, pesticide safety training and heat-illness training are all required to be delivered in a language the worker understands. An employer who trained a Spanish-speaking crew in English has not trained them, and the citation reads the same as if the training had not happened. This column is what lets the app prove which language each person was served in. | Which language every wizard, warning, task and REI notice this person sees comes back in. NEVER INFERRED FROM A DEVICE LOCALE: a phone set to English by whoever handed it over says nothing about who is holding it now, and getting this wrong silently is exactly the failure the column exists to prevent. Where it is empty the app serves English and says so rather than guessing. |
| `i9_status` | Select<br>`Verified` `Pending` `Expired` `N-A` | **yes** | IRCA 8 USC 1324a; Form I-9 | Employment eligibility must be verified within three business days of hire and re-verified when a document expires. ICE fines are per form. | Whether this person may be put on a crew at all. Expired means they cannot lawfully work tomorrow, which is a scheduling fact before it is a filing fact — and it is what the Sprint 7 alert engine blocks employment on. |
| `w4_status` | Select<br>`On-File` `Missing` `Requires-Update` | **yes** | IRC §3402; Form W-4 | Withholding must follow a signed W-4. Missing means the employer withholds at the default single rate and owes an explanation if asked. | Payroll cannot compute a net cheque without it. Missing is not a reporting gap, it is a cheque that comes out at the wrong number. |
| `jurisdiction` | Data | **yes** | FLSA; ORS 653 (Oregon); RCW 49.46 (Washington) | Wage law follows the location where the work is performed, not where the employer sits. Oregon and Washington differ on overtime for agricultural labour, on rest breaks and on minimum wage regions. | The minimum wage and the overtime rule used to compute this person's pay. A crew that crossed the river to a Washington block is paid under a different rule that day, and this is the field that says so. |
| `flc_license_status` | Data | no | MSPA 29 USC 1801; ORS 658.405 farm labor contractor licensing | Anyone recruiting, supervising or transporting agricultural workers for a fee needs a farm labor contractor licence, federally and in Oregon. Using an unlicensed contractor is the grower's violation as well as theirs. | Whether this person may lawfully run a crew or drive the bus. An expired licence takes a crew boss off the schedule that morning. |
| `flc_license_expiration` | Date | no | MSPA 29 USC 1801; ORS 658.405 | A licence is only a defence while it is current. The expiration date is the fact. | Feeds the renewal alert. A crew boss whose licence lapses mid-harvest is a crew with nobody who can lawfully supervise it. |

### `Bucket Log Entry` — erpnext_mcp

Harvest chain of custody: bucket → employee → crew → block → bin → shipment. The FSMA Food Traceability Rule's critical tracking events. `employee` is a declared field of the doctype itself (resolved from worker_badge by link_badge_to_employee); crew_id/block_id/bin_id/shipment_id, verified here, are the rest of the chain. Shipped as declared fields in v0.44.0.

**Verified, not added.** Through v0.43.0 this doctype belonged to a
hypothetical external "BucketLog bridge" app and these columns were grafted
on. v0.44.0 makes it erpnext_mcp's own — the sync endpoint (`sync_bucket_entries`),
the badge register and the doctype ship together — so these are declared
fields of a DocType this app ships, the same as Housing Unit's and Field's.

| Field | Type | Required | Framework | Why the regulator wants it | What breaks in the WORK without it |
| --- | --- | --- | --- | --- | --- |
| `crew_id` | Data | no | FSMA Subpart S; MSPA crew records | The crew is the unit a hygiene training record, a field sanitation inspection and a wage-law jurisdiction all attach to. | Who to pay, who to send where tomorrow, and which crew boss answers for the block. Harvest is organised by crew, not by picker. |
| `block_id` | Data | no | FSMA Subpart S critical tracking event; spray REI/PHI linkage | The block is where the lot came from, and it is the join to the spray record — which is how a residue question becomes an answerable question. | Yield by block, cost by block, and the REI check that says whether the block could lawfully be picked at all. |
| `bin_id` | Data | no | FSMA Subpart S — commingling / transformation event | A bin is where buckets from several pickers become one lot. It is the transformation event the rule asks to be recorded. | What actually goes on the truck. The bin is the physical unit the packing house receives and pays against. |
| `shipment_id` | Data | no | FSMA Subpart S — shipping event; buyer traceback exercises | The shipping event closes the chain. A buyer's mock recall is timed, and an operation that cannot answer in four hours fails the audit. | Getting paid. The shipment is what the invoice is raised against, and an unlinked bin is fruit that left the farm with no receivable behind it. |

### `Attendance` — hrms

The one-way bridge from a closed Farm Shift to the payroll register. A shift close writes one submitted Attendance per crew member for that person's own span, and this column is what says which shift it came from — so farm_hr has one canonical answer to 'when was Ana at work' and an investigator reading that day can get to the conditions she worked in.

**One column, and it is a bridge rather than a compliance fact in itself.** v0.19.3
makes the Farm Shift the anchor for exposure-based compliance. Without a column
pointing back at the shift, a shift-formed attendance day is indistinguishable
from a hand-keyed one — so nobody reading the register can reach the water
breaks, the weather and the supervisor's signature that describe it, and the
bridge cannot tell its own rows from somebody else's.

The bridge runs **one way only**. A shift is formed by a foreman naming a crew, a
location and a type; an attendance row carries none of those, so deriving shifts
from attendance would invent all three on a record an inspector reads.

| Field | Type | Required | Framework | Why the regulator wants it | What breaks in the WORK without it |
| --- | --- | --- | --- | --- | --- |
| `farm_shift` | Link | no | OAR 437-004-1131; FSMA 21 CFR 112.161(b); ORS 653 wage records | An attendance row says somebody was at work. The shift says what the conditions were, what breaks were called, who supervised and who signed. A heat-illness investigation and a wage claim both start from the day and need the second, and this link is the only way from one to the other. | Payroll reconciliation. A shift-formed day and a hand-keyed day look identical without it, so nobody can tell which rows a re-closed shift already wrote — and the bridge, unable to tell either, would pay somebody twice for one afternoon. |

### `Asset` — erpnext

The maintenance-versus-growth split every sustainable cash flow figure is read through, and the link that makes an asset on the books and a tag in the field one machine. Maintenance capex replaces what wore out and growth capex buys capacity that was never there; an operation that cannot tell them apart cannot say whether a good year was earned or borrowed from the orchard. And an operation whose fixed-asset register and whose scanned tags are two unconnected lists cannot say how many machines it owns.

**The first target in this file that is not about a regulator, and it belongs
here anyway.** Maintenance capex replaces productive capacity that wore out;
growth capex buys capacity that was never there. Sustainable CF/Acre is what is
left after the first is funded, and an operation that cannot tell them apart
reports expansion spending as if it were keeping the orchard whole.

**Why not a profile DocType beside the Asset.** v0.7.0 put the cost split in an
`Asset Cost Profile` precisely so this app would touch nobody else's schema, and
the same move fails here. The maintenance/growth call is made ONCE, by the person
raising the purchase, at the moment they know why they are buying the thing — the
old pump failed, or the new block needs a pump it never had. A profile row
written afterwards by somebody reconciling the quarter is a person reconstructing
an intention from an invoice, and they will get it wrong in the direction that
makes the quarter look better.

**`capex_type` is not `reqd`, deliberately.** Frappe enforces `reqd` on save
rather than retroactively, so marking it required would leave every existing
Asset readable and unsaveable — editing a location on a tractor bought in 2019
would demand a classification nobody present can make. The gate is in
`create_asset` instead, where the person raising the purchase is standing, and
`backfill_asset_capex_type` classifies the history in bulk.


**The tag on the machine and the asset on the books, made one thing (v0.148.0).**
An asset registered from a handset lands in `Asset Register`, which is where the
printed tag, the QR, the scan history, the service schedule and eight doctypes'
link fields all point. It does not land in ERPNext's `Asset`, which is where the
fixed-asset register, the insurance schedule and the depreciation run all look.
Without a link between them the same machine exists twice on one site and neither
copy knows about the other. `asset_mirror` writes the second record and
`asset_register` is the column that makes it the same machine.

**Three columns and not twenty-three.** `Asset Register` carries GPS, a serial
number, a model, a service schedule, an hour meter and the scan stamps, and the
obvious build copies all of them here so the Desk shows everything in one place.
That is the shadow layer this file argues against, aimed the other way: two
editable copies of one coordinate will disagree, and an insurance schedule
reading one while a dispatcher reads the other is worse than a single copy one
click away. Exactly one column below is not derivable from somewhere else — the
Link — and the other two exist to make it auditable.

**And one deliberate second copy: the coordinate (v0.149.0).** Everything else
on the tag stays on the tag. `gps_latitude` and `gps_longitude` do not, because
the unified map plots equipment out of the fixed-asset register alongside
blocks, zones and valves — and a map that had to join through `Asset Register`
to find a tractor could not plot one somebody created in the Desk. The drift a
second copy normally invites is answered by refreshing it on every sync and
stamping when that happened, not by hoping.

**And the footprint (v0.150.0).** A pin says where to walk; an outline says what is
there. `boundary_geojson` carries the shape a shed, a pump house, a cabin or a
cold store occupies, so the unified map can draw a building as a building.

**All six are read-only.** They are written by the mirror and by nothing else.
A denormalised copy a second person can type over is a copy that will one day
lie, and the whole value of a mirror is that you can tell when it has stopped
agreeing.

| Field | Type | Required | Framework | Why the regulator wants it | What breaks in the WORK without it |
| --- | --- | --- | --- | --- | --- |
| `capex_type` | Select | no | Managerial accounting — Sustainable CF/Acre (v0.19.5); lender maintenance-capex covenants | Maintenance capex replaces productive capacity that wore out; growth capex adds capacity that was never there. Sustainable cash flow is what is left after the first is funded, and an operation that cannot tell them apart reports growth spending as if it were keeping the orchard whole. | The replacement budget. 'What we spend to stay where we are' and 'what we spend to get bigger' are two different plans, and an operation that cannot separate them funds the second out of the first — which is deferred maintenance with a better name. |
| `maintenance_portion` | Currency | no | Managerial accounting — Sustainable CF/Acre (v0.19.5) | A single purchase is often both — a bigger tractor replacing a smaller one is the old machine's capacity as maintenance and the difference as growth. Recording only the total forces the whole amount into one bucket and the KPI reads whichever the person picked. | What a replacement reserve is sized against. The maintenance half of a mixed purchase is the recurring number; the growth half happens once. |
| `growth_portion` | Currency | no | Managerial accounting — Sustainable CF/Acre (v0.19.5) | The other half of the split, stored rather than derived. A portion computed as 'the total minus the other one' cannot disagree with the total, which sounds like a virtue and means a transposed figure is silently absorbed instead of refused. | What the expansion actually cost, separable from what keeping the existing ground going cost. It is the number a return-on-new-planting calculation starts from. |
| `capex_justification` | Small Text | no | Managerial accounting — Sustainable CF/Acre (v0.19.5) | Required for Growth and Mixed by `create_asset`: what capacity does this add? Classifying a purchase as growth takes it out of the maintenance figure, which raises sustainable cash flow — the one direction in which a misclassification flatters the operation, and therefore the one that needs a sentence behind it. | The reason the purchase was made, in the words of whoever made it, on the record it was made against. It is what next year's planning reads to find out whether the new capacity did what it was bought to do. |
| `asset_register` | Link | no | Fixed-asset register integrity — the unified asset register (v0.148.0) | Which printed tag this asset is. Without it the same machine exists twice on one site — once on the books and once on a sticker — and no query can tell that the tractor in the depreciation schedule and the tractor a worker scanned this morning are one tractor. | An adjuster holding a serial number, or an accountant holding a depreciation line, can reach the scan history, the service record and the photograph without knowing this app exists. Without the link, each has half a machine. |
| `farm_asset_type` | Data | no | Fixed-asset register integrity — the unified asset register (v0.148.0) | The farm's own vocabulary for what the thing is — valve, tractor, wind machine, cabin — which is finer than the Asset Category the accounts are kept by and is the word anybody on the ground would use to ask for it. | Filtering the Asset list to every wind machine, or every valve, without opening a record. A category built for depreciation accounts puts four unlike machines in one bucket. |
| `asset_register_synced_at` | Datetime | no | Fixed-asset register integrity — the unified asset register (v0.148.0) | When the mirror last agreed with the tag. A denormalised copy with no as-of stamp cannot be audited: nobody can tell a column that is current from one this app stopped being able to write months ago. | Whether the books are being kept up to date by the field at all. A stamp months behind the tag's own modified date is a sync that has been failing silently, and it is the only thing that would say so. |
| `gps_latitude` | Float | no | Fixed-asset register integrity — the unified asset register (v0.149.0) | Where the asset physically is, on the record an insurer, an assessor and a lender read. A schedule that lists a wind machine and cannot say which corner of which orchard it stands in describes a machine nobody can find. | Walking to it. 'The shop yard' is four acres and a pump, a bin trailer or a generator is findable by coordinate and by nothing else — and the dispatch map plots equipment from this column. |
| `gps_longitude` | Float | no | Fixed-asset register integrity — the unified asset register (v0.149.0) | The other half, and it is stored rather than derived for the reason every coordinate pair is: half a position is not a position. A record carrying one of the two is a point on the equator or the prime meridian. | The same walk. The mirror writes both columns or neither, so a machine on the map is a machine somebody actually took a fix on. |
| `boundary_geojson` | Long Text | no | Fixed-asset register integrity — the unified asset register (v0.150.0) | What a building occupies, as against where it is. An insurance schedule that lists a cold store and cannot say how big its footprint is, or which side of the yard it takes, describes a number rather than a structure. | Drawing the shed on the map as a shed instead of as a dot. Which way the shop faces, whether the cabin row sits inside the parcel, how much of the yard is already built on — none of which a pin can answer. |

### `Item` — erpnext

The pesticide label, as columns on the product it belongs to, and the two intervals every application of a chemical inherits from it. A Spray Log records what one application used; these record what the LABEL says — the restricted-entry and pre-harvest intervals, the EPA registration number, the crop the PHI applies to, the ingredient statement, the rate and the PPE — once per product. That is what lets a finished spray task say when the block reopens and when it may be picked without anybody reading a jug in the field, and what gives a scanned label's figures something to be checked against.

**Two Sprint 4 halves on one DocType, and the same argument as `Spray Log` with
the subject changed.** The REI and the PHI are on the spray record because that
is where the person doing the spraying is. They are *also* on the product,
because that is where the label says them — and the label is the law. A site
keeping them only on the spray record needs somebody to read a jug before every
application and type the number in correctly, which is a data-entry step
standing between a crew and a block they may not enter.

**What the first two columns buy, concretely.** `complete_farm_task` reads them
off the chemicals in the tank mix and stamps the *window* onto the task — an
expiry to the hour and a harvest date — which is what the
`rei_active_block_entry` and `phi_harvest_window` compliance rules raise from.
Without them the app can record that a spray happened and cannot say when the
block reopens, which is the one question the record exists to answer. A tank mix
takes the **longest** REI and the longest PHI of the products in it: a mix is
under the strictest thing in it, and a block does not become half-enterable at
hour twelve.

**What the other seven buy.** They are the rest of what a photographed label
says, so a scanned pesticide label has somewhere to land and the two numbers
above have something to be checked *against* rather than being the only copy of
what somebody typed. `label_scan_validation` links back to the
`Document Validation` they were read off — the photograph, the OCR text, every
check run against it, and whether a person has confirmed the reading.

**None is `reqd`,** in the way `Asset.capex_type` is not: most items in an
orchard's register are bins, twine and diesel, and a required REI would make
every one of them unsaveable until somebody typed a zero into a column that does
not apply to a pallet.

**`depends_on` decides what is SHOWN, and only for seven of the nine.** All nine
columns exist on every Item row; the expression in `CHEMICAL_ITEM_DEPENDS_ON`
decides whether a person editing a picking bag has to look at the seven
label-detail ones. It matches on the group's *name* — `chemical`, `pesticide`,
`spray`, `crop protection`, `fungicide`, `herbicide`, `insecticide` — rather than
on a hard-coded list of groups, because every site names its item groups
differently, and it shows them unconditionally on any Item already carrying an
`epa_registration_number` or a `signal_word`: a `depends_on` that hides data
somebody has already entered is not a display preference, it is a way to lose a
record.

**`rei_hours` and `phi_days` deliberately do not carry it.** The spray window
computes off them, their own guidance is "leave at zero for anything that is not
a restricted-entry product", and a display rule that hid them on a site whose
item group this file's expression does not anticipate would silently hand that
feature a zero it could not tell from a real one.

| Field | Type | Required | Framework | Why the regulator wants it | What breaks in the WORK without it |
| --- | --- | --- | --- | --- | --- |
| `rei_hours` | Int | no | EPA WPS 40 CFR 170.407 — restricted-entry interval; FIFRA label | The label's restricted-entry interval for this product, in hours. It is the number the re-entry prohibition after every application of it is computed from, and it belongs to the product rather than to any one spray. | Crew scheduling, from the item register outwards. Recorded here, finishing a spray task states the hour the block reopens by itself; recorded nowhere, somebody reads a jug in the field and the crew boss guesses. |
| `phi_days` | Int | no | FIFRA label; FDA tolerances 40 CFR 180 | The label's pre-harvest interval for this product, in days. Picking inside it is a residue violation on a shipped load, and the interval is a property of the product the same way the REI is. | Harvest scheduling weeks out. A block sprayed inside its PHI cannot be picked, and the pick date is planned against this number long before the sprayer is filled — so it has to be knowable from the product, not only from the last application record. |
| `epa_registration_number` | Data | no | FIFRA 7 USC 136; 40 CFR 152.132 registration numbering | The registration number identifies the product as registered for this crop and this use, and it is the number a residue detection is traced back through. On the Item rather than only on each Spray Log it is stated once, from the label, instead of typed from memory on every application. | Whether the jug in the shed may be used on the block at all. Without it nobody can check the product against the crop, the rate or the buyer's maximum residue limit before the tank is filled — which is a decision made at the shed, not at a desk afterwards. |
| `signal_word` | Select | no | FIFRA labeling — 40 CFR 156.64 signal words | The signal word is the label's own statement of acute toxicity, and it is what decides the personal protective equipment the applicator wears. 'None' is a real answer for a Category IV product and is in the list for that reason. | What the person mixing puts on before they open the jug. A blank here and a 'None' here mean different things to that person, and only one of them is safe to act on. |
| `phi_crop` | Data | no | FIFRA label; FDA tolerances 40 CFR 180 | One label carries a different pre-harvest interval for cherries, apples and pears. An interval with no crop beside it cannot be applied to a block, so the crop is stored with the number rather than assumed from the operation. | Which blocks the interval above actually governs. A grower running cherries and pears off one chemical shed has two answers for one jug, and a record holding only one of them is wrong half the time. |
| `active_ingredients` | JSON | no | FIFRA 40 CFR 156.10(g) ingredient statement; FRAC/IRAC resistance management | The ingredient statement is what ties a product to a resistance-management group, to the restricted-entry interval its class carries, and to every residue tolerance downstream. Stored as [{name, concentration, unit}] because a product is often several ingredients and the concentrations are what distinguish two formulations of the same active. | Rotation. Two products with different trade names and the same active ingredient are one spray as far as resistance is concerned, and a shed that cannot see that builds resistance while believing it is rotating. |
| `application_rate` | Data | no | FIFRA label use directions — 40 CFR 156.10(i) | Applying above the labeled rate is an off-label application and a residue risk; applying below it is a failed spray. The rate is on the label and belongs on the product record beside the interval it goes with. | What goes in the tank. The mix is calculated from this number and the acreage, at the shed, usually before anybody has opened a compliance record. |
| `ppe_requirements` | Small Text | no | EPA WPS 40 CFR 170.507 — handler PPE; label PPE statement | The label's PPE statement is what the handler and any early-entry worker must wear, and the Worker Protection Standard requires the employer to provide it. It is a property of the product, so it is recorded once per product. | What has to be in the shed before the spray can happen. A respirator nobody stocked is a spray that does not go out, and this is the field that says so a week early instead of on the morning. |
| `label_scan_validation` | Link | no | Internal provenance — v0.69.0 Document Intelligence | Where the eight fields above came from. A Document Validation holds the photograph, the OCR text, the extraction and every check run against it, so a number on this Item can be traced to the label it was read off rather than to whoever typed it. It also carries whether a person has confirmed the reading, which is the only thing on that record a machine did not produce. | Whether the numbers above can be trusted at the shed. An unvalidated REI and one read off a photograph a supervisor confirmed are the same integer on the screen and two very different things to bet a crew's re-entry on. |

### `Company` — erpnext

Whether this entity charges its labor camp occupants rent, answered once for the entity instead of guessed at on every bunk assignment. ORS 653 and OAR 839-015 require a housing deduction to be disclosed; the Housing Assignment row is that disclosure and still carries the answer. What this changes is who supplies it.

**A default, not a replacement.** `housing_deduction_from_wages` remains a
per-assignment Select on Housing Assignment, and an explicit answer on a single
assignment still wins — one arrangement can genuinely differ from the entity's
norm. What this field supplies is the answer where the caller sent none, so the
column stops reading `Unknown` on rows where a foreman was asked a wage question
he should never have been asked.

**It is written onto the row at creation, never resolved at read time.** The
audit packet and the camp register read the per-assignment column directly. A
default resolved when a report runs would leave every assignment created after
v0.94.0 reporting `Unknown` to an auditor while looking correct in the app —
which is the trap this note exists to keep shut.

**On Company rather than a single-doctype setting.** This app is multi-company,
and a `"issingle": 1` settings doctype holds one row for the whole site; it would
need a per-company child table to be correct. A field on Company is per-company
by construction. It is deliberately *not* `set_company_defaults`, which is the
accounting-defaults tool and keyed to its own supported list.

| Field | Type | Required | Framework | Why the regulator wants it | What breaks in the WORK without it |
| --- | --- | --- | --- | --- | --- |
| `default_housing_deduction_from_wages` | Select | no | ORS 653.035 and OAR 839-015-0100 (deductions from agricultural wages must be disclosed and authorised); 29 CFR 531 on lodging credited against the minimum wage | A housing deduction is a wage deduction, and a record that says 'Unknown' for every assignment is a disclosure nobody made. This is the entity's standing answer, so each Housing Assignment is written with a real one. | The foreman assigning a bunk stops being asked a wage question. The value is WRITTEN ONTO each Housing Assignment at creation, not resolved when a report reads it — `audit_packets` and the camp register read the per-assignment column, and a lazily-resolved default would leave them reporting 'Unknown' for every row created after this shipped. |

### `Housing Unit` — erpnext_mcp

FSMA Produce Safety Rule Subpart L worker facilities, and the habitability and detector-test dates Oregon's agricultural labor housing rules turn on. Shipped as declared fields in v0.12.0, verified here.

**Verified, not added.** These are declared fields of a DocType this app ships.
A missing one means the DocType did not migrate, and the installer reports it
rather than papering over it with a Custom Field — two columns and no error is
worse than the problem it would hide.

| Field | Type | Required | Framework | Why the regulator wants it | What breaks in the WORK without it |
| --- | --- | --- | --- | --- | --- |
| `fsma_worker_facility` | Check | no | FSMA Produce Safety Rule 21 CFR 112 Subpart L | Which of fifty buildings are subject to the worker facility sanitation requirements. Without the flag every building is either in scope or none is. | Which buildings get walked on the sanitation round, and which need supplies restocked before a crew arrives. |
| `last_habitability_inspection` | Date | no | OAR 437-004-1120 agricultural labor housing; 29 CFR 1910.142 | Annual habitability inspection is the cadence a camp is walked on. | Whether a cabin can be assigned. An uninspected unit is one nobody has confirmed has running water this season. |
| `smoke_detector_last_test` | Date | no | OAR 437-004-1120; ORS 479 smoke alarm requirements | A detector nobody has tested is a detector nobody knows works. | Somebody sleeps there tonight. |
| `co_detector_last_test` | Date | no | OAR 437-004-1120; ORS 690 carbon monoxide alarms | Required wherever there is a fuel-burning appliance, which on a camp cabin usually means a propane heater. | Somebody sleeps there tonight. |

### `Field` — erpnext_mcp

Food safety zoning, the agricultural water and spray dates the Produce Safety Rule turns on, the dates that say when this block was actually earning, and — from v0.97.0 — where the ground stands with the National Organic Program. Shipped as declared fields in v0.12.0, v0.19.5 and v0.97.0, verified here.

**Verified, not added.** These are declared fields of a DocType this app ships.
A missing one means the DocType did not migrate, and the installer reports it
rather than papering over it with a Custom Field — two columns and no error is
worse than the problem it would hide.

**`organic_certified` is derived and is not in this table.** The Field controller
rewrites it from `organic_status` on every save and the Desk shows it read-only,
so what is verified here is the column the answer actually comes from. A derived
flag a person can set independently is a flag that will disagree with the status
it came from, and the one that is wrong is always the one nobody edited.
`organic_cert_agency` and `transition_start_date` are declared beside it and are
supporting detail rather than the fact a regulator turns on.

**The three v0.19.5 dates are the denominator of every per-acre metric.** What is
PRODUCTIVE, not what is owned: fallow ground has acreage, a cost centre and a
water right and earns nothing, and a perennial in its pre-yield years is capital
under construction wearing the costume of an orchard. A block with no
`productive_from_date` is EXCLUDED from the denominator and reported, never
assumed productive — assuming it would put acres in the denominator that may be a
three-year-old planting, which makes the figure look conservative while quietly
turning a data gap into a number somebody acts on.

| Field | Type | Required | Framework | Why the regulator wants it | What breaks in the WORK without it |
| --- | --- | --- | --- | --- | --- |
| `productive_from_date` | Date | no | Managerial accounting — Sustainable CF/Acre (v0.19.5) | The denominator of every per-acre metric is what is PRODUCTIVE, not what is owned. Without this date a pre-yield block counts as earning ground and every per-acre figure is understated by however much of the farm is still coming into bearing. | When a block starts being budgeted as a crop rather than as capital under construction. It is what a picking plan, a bin forecast and a crew estimate all key off. |
| `productive_through_date` | Date | no | Managerial accounting — Sustainable CF/Acre (v0.19.5) | A block pulled in July earned for half the year. Null means still productive, which is the ordinary case; a date means the acreage stops counting from it, pro-rated. | Whether to send a crew there next season, and whether the water and spray programme still applies to it. |
| `pre_yield_end_date` | Date | no | Managerial accounting — Sustainable CF/Acre (v0.19.5) | Perennials spend their first years as capital rather than as crop — cherry is commonly three or four. Recorded separately from `productive_from_date` so a block still in its pre-yield years is COUNTED and reported rather than merely absent: those acres are next year's denominator, and a reader who cannot see them coming cannot read the trend. | When the block moves onto the picking plan, and when the establishment budget stops. Both are planned years ahead off this date. |
| `food_safety_zone` | Data | no | FSMA Produce Safety Rule 21 CFR 112; GAP / GlobalGAP zoning | Zoning is how a hazard assessment is expressed on the ground — which ground is adjacent to a dairy, a road, a wildlife corridor. | Which blocks get walked for animal intrusion before a pick, and which can be picked at all after a flood event. |
| `last_spray_date` | Date | no | EPA WPS 40 CFR 170.407 REI; FIFRA label PHI | The date the REI and PHI windows are counted from. | Whether a crew can enter this block today. It is read before every pick and every thinning pass. |
| `organic_status` | Select | no | National Organic Program 7 CFR 205 — §205.202 land requirements, §205.400 certification | Certification attaches to GROUND. The thirty-six months since the last prohibited application is a per-block fact, and a crop-level flag can represent neither it nor a farm running certified and conventional blocks of one variety. Certified acres are summed from this column. | Which materials may go on this block at all. A conventional product applied to a certified block does not produce a paperwork finding — it restarts the three-year clock on that ground, and the decision is made at the shed before the tank is filled. |

---

## Keeping this file true

The tables above are the contents of `compliance_fields.TARGETS`, and
`test_compliance_fields.py` asserts that every field in that table appears here
with its framework and its operational answer. A field added to the code and not
to this file fails the suite; a field described here and removed from the code
does too. Neither can drift from the other in a release.

## Related

* `erpnext_mcp/compliance_fields.py` — the table, the installer, and the argument
* `erpnext_mcp/install.py` — the `after_migrate` hook and the uninstall warning
* `docs/tool-catalog.md` — `install_compliance_fields` and `get_compliance_field_map`
* `tests_standalone/test_compliance_fields.py` — including `WovenNotShadow`
* `tests_standalone/test_housing.py` — the same `WovenNotShadow` argument, run
  against live DocTypes this app owns
