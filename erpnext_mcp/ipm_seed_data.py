# SPDX-License-Identifier: MIT
"""The IPM graph's literature seed: what `ipm_reference` does not already say. v0.262.0.

`ipm_reference` is the reference shipped in code — pests, beneficials, damage windows, efficacy,
toxicity and labels. `ipm_graph.seed()` turns those tables into IPM Organism nodes and IPM
Relationship edges. This module adds the three things they do not carry:

1. VERTEBRATES as first-class pests and beneficials, with their legal status. The protection is
   stated per species, because it differs: European starlings are not covered by the Migratory
   Bird Treaty Act; American robins and cedar waxwings are, and lethal take needs a USFWS
   depredation permit; American crows are covered but fall under a federal depredation order with
   conditions. Owls, kestrels and hawks are protected. Deer are a state game species.
2. WHO EATS WHOM — the predator and parasitoid links the beneficial descriptions state in prose.
3. STARTER ACTION THRESHOLDS for Mid-Columbia sweet cherry, every one seeded DISABLED and
   Proposed, with recommended methods, for Tim to approve. They are starting points to verify
   against the PNW Pest Management Handbook and WSU Tree Fruit guidance, not settled values.

Everything here is provenance Literature and keyed by `seed_key`; a re-seed updates only rows that
are still Literature, and never touches a User Entered, Farm Observed, Imported or AI Proposed row.
"""

CHERRY = "Cherries"

#: Vertebrates the reference tables lack (Vole, Pocket Gopher, Bat, Barn Owl, Great Horned Owl,
#: Red Fox and Kestrel come from `ipm_reference`; their legal status is set in PROTECTION below).
VERTEBRATES = [
	{"name": "European Starling", "scientific_name": "Sturnus vulgaris", "kind": "Vertebrate Pest",
	 "protected_status": "", "protected_note": "Non-native; not protected by the Migratory Bird Treaty Act. "
	 "State and local rules on shooting and trapping still apply.",
	 "bbch_from": 81, "bbch_to": 89, "description": "Flocks strip ripening cherries from straw colour to harvest."},
	{"name": "American Robin", "scientific_name": "Turdus migratorius", "kind": "Vertebrate Pest",
	 "protected_status": "MBTA", "protected_note": "Protected by the Migratory Bird Treaty Act: lethal control "
	 "needs a USFWS depredation permit. Lead with netting, scare devices and habitat.",
	 "bbch_from": 81, "bbch_to": 89, "description": "Pecks and removes ripening fruit; a major cherry bird pest."},
	{"name": "Cedar Waxwing", "scientific_name": "Bombycilla cedrorum", "kind": "Vertebrate Pest",
	 "protected_status": "MBTA", "protected_note": "Protected by the Migratory Bird Treaty Act: lethal control "
	 "needs a USFWS depredation permit. Lead with netting and scare devices.",
	 "bbch_from": 81, "bbch_to": 89, "description": "Flocks swallow whole cherries; damage concentrates late in ripening."},
	{"name": "American Crow", "scientific_name": "Corvus brachyrhynchos", "kind": "Vertebrate Pest",
	 "protected_status": "MBTA Depredation Order", "protected_note": "Protected by the Migratory Bird Treaty Act; a "
	 "federal depredation order (50 CFR part 21) allows control when crows are damaging crops, with conditions "
	 "and reporting. Check the order and state rules first; non-lethal methods lead.",
	 "bbch_from": 81, "bbch_to": 89, "description": "Takes fruit and damages irrigation lines."},
	{"name": "House Finch", "scientific_name": "Haemorhous mexicanus", "kind": "Vertebrate Pest",
	 "protected_status": "MBTA", "protected_note": "Protected by the Migratory Bird Treaty Act.",
	 "bbch_from": 81, "bbch_to": 89, "description": "Pecks ripening fruit and buds."},
	{"name": "Mule Deer", "scientific_name": "Odocoileus hemionus", "kind": "Vertebrate Pest",
	 "protected_status": "State Protected", "protected_note": "A state game species: removal needs a WDFW/ODFW "
	 "damage permit or tag. Fencing and repellents first.",
	 "description": "Browses shoots and young trees; rubs bark."},
	{"name": "Cottontail Rabbit", "scientific_name": "Sylvilagus nuttallii", "kind": "Vertebrate Pest",
	 "protected_status": "", "protected_note": "State hunting rules may apply.",
	 "description": "Girdles young trunks in winter."},
	{"name": "Red-tailed Hawk", "scientific_name": "Buteo jamaicensis", "kind": "Beneficial Vertebrate",
	 "protected_status": "MBTA", "protected_note": "Protected. Perches help; never a target.",
	 "description": "Hunts voles, gophers and rabbits from perches over the orchard floor."},
]

#: Legal status for vertebrates that come from `ipm_reference`.
PROTECTION = {
	"Barn Owl": ("MBTA", "Protected. Nest boxes help; never a target."),
	"Great Horned Owl": ("MBTA", "Protected. Never a target."),
	"Kestrel": ("MBTA", "Protected (American kestrel). Nest boxes help."),
	"Bat": ("", "Several Northwest bat species are state-listed; do not disturb roosts."),
	"Red Fox": ("State Protected", "A state furbearer."),
	"Pocket Gopher": ("", "Northern pocket gopher is unprotected; the Mazama pocket gopher (western WA) is "
	                      "ESA-listed — not a Mid-Columbia species."),
}

#: subject (beneficial) → relation → object (pest), effectiveness 0–1. From the beneficial
#: descriptions in `ipm_reference.BENEFICIALS` and the PNW handbooks.
PREDATION = [
	("Ladybug", "preys_on", "Black Cherry Aphid", 0.8),
	("Ladybug", "preys_on", "Spider Mites", 0.4),
	("Ladybug", "preys_on", "San Jose Scale", 0.3),
	("Green Lacewing", "preys_on", "Black Cherry Aphid", 0.7),
	("Green Lacewing", "preys_on", "Spider Mites", 0.4),
	("Green Lacewing", "preys_on", "Obliquebanded Leafroller", 0.3),
	("Tachinid Fly", "parasitizes", "Obliquebanded Leafroller", 0.5),
	("Tachinid Fly", "parasitizes", "Codling Moth", 0.3),
	("Predatory Mite", "preys_on", "Spider Mites", 0.9),
	("Trichogramma platneri", "parasitizes", "Codling Moth", 0.5),
	("Trichogramma platneri", "parasitizes", "Obliquebanded Leafroller", 0.5),
	("Ganaspis brasiliensis", "parasitizes", "Spotted Wing Drosophila", 0.5),
	("Aganaspis pelleranoi", "parasitizes", "Western Cherry Fruit Fly", 0.3),
	("Aganaspis pelleranoi", "parasitizes", "Spotted Wing Drosophila", 0.3),
	("Bacillus subtilis", "controls", "Brown Rot", 0.5),
	("Bacillus subtilis", "controls", "Botrytis Blossom Blight", 0.5),
	("Ampelomyces quisqualis", "parasitizes", "Powdery Mildew", 0.5),
	("Cydia pomonella Granulovirus", "controls", "Codling Moth", 0.7),
	("Obliquebanded Leafroller Nucleopolyhedrovirus", "controls", "Obliquebanded Leafroller", 0.5),
	("Bat", "preys_on", "Codling Moth", 0.4),
	("Bat", "preys_on", "Obliquebanded Leafroller", 0.3),
	("Barn Owl", "preys_on", "Vole", 0.8),
	("Barn Owl", "preys_on", "Pocket Gopher", 0.6),
	("Great Horned Owl", "preys_on", "Pocket Gopher", 0.6),
	("Great Horned Owl", "preys_on", "Cottontail Rabbit", 0.5),
	("Great Horned Owl", "preys_on", "Vole", 0.5),
	("Red Fox", "preys_on", "Vole", 0.6),
	("Red Fox", "preys_on", "Pocket Gopher", 0.4),
	("Red Fox", "preys_on", "Cottontail Rabbit", 0.5),
	("Kestrel", "preys_on", "Vole", 0.6),
	("Red-tailed Hawk", "preys_on", "Vole", 0.5),
	("Red-tailed Hawk", "preys_on", "Pocket Gopher", 0.5),
	("Red-tailed Hawk", "preys_on", "Cottontail Rabbit", 0.4),
	("Ground Beetle", "preys_on", "Cherry Slug", 0.3),
	("Honeybee", "pollinates", CHERRY, 0.9),
	("Mason Bee", "pollinates", CHERRY, 0.8),
]

#: Vertebrate pests attack the crop (the reference's PEST_DAMAGE covers the insects and diseases).
VERTEBRATE_DAMAGE = [
	("European Starling", 0.6, 81, 89), ("American Robin", 0.6, 81, 89), ("Cedar Waxwing", 0.5, 81, 89),
	("American Crow", 0.3, 81, 89), ("House Finch", 0.3, 81, 89), ("Vole", 0.5, None, None),
	("Pocket Gopher", 0.5, None, None), ("Mule Deer", 0.4, None, None), ("Cottontail Rabbit", 0.3, None, None),
]

#: Non-lethal options a threshold status offers for a vertebrate, cheapest-impact first.
NON_LETHAL = {
	"bird": [("exclusion", "Netting over high-value blocks from straw colour"),
	         ("cultural", "Scare devices — propane cannons, distress calls, reflective tape — rotated so birds do not habituate"),
	         ("cultural", "Prompt, clean harvest; remove dropped and cull fruit"),
	         ("biological", "Kestrel and owl nest boxes, raptor perches")],
	"rodent": [("biological", "Barn owl nest boxes and raptor perches"),
	           ("cultural", "Mow and keep a vegetation-free strip at the trunk; remove trunk guards' debris"),
	           ("exclusion", "Trunk guards / hardware cloth on young trees"),
	           ("cultural", "Trapping (gophers: box or pincer traps in active runs)")],
	"deer": [("exclusion", "Deer fence (8 ft)"), ("cultural", "Repellents rotated"), ("cultural", "Scare devices")],
	"rabbit": [("exclusion", "Trunk guards on young trees"), ("cultural", "Remove brush cover near the block")],
}

#: Which vertebrate group each species is in for NON_LETHAL, and which carry a rodenticide gate.
VERTEBRATE_GROUP = {
	"European Starling": "bird", "American Robin": "bird", "Cedar Waxwing": "bird", "American Crow": "bird",
	"House Finch": "bird", "Vole": "rodent", "Pocket Gopher": "rodent", "Mule Deer": "deer",
	"Cottontail Rabbit": "rabbit",
}
RODENTICIDE_GATE = "rodent_bait"

#: Starter action thresholds, Mid-Columbia sweet cherry. ALL seeded disabled and Proposed.
STARTER_SOURCE = ("Starter value for review (v0.262.0) — verify against the PNW Pest Management Handbook and "
                  "WSU Tree Fruit guidance before approving.")
THRESHOLDS = [
	{"threat": "Western Cherry Fruit Fly", "threat_category": "Insect", "crop_stage": "BBCH 75–89",
	 "sample_unit": "Per Trap", "comparison": "Greater Than Or Equal", "action_threshold": 1,
	 "recommended_methods": "Ammonium-baited yellow sticky traps hung before emergence (about 900 °F degree-days, "
	 "base 41 °F); treat at first catch and keep fruit covered at the label interval through harvest. Zero "
	 "tolerance for larvae in fruit. Rotate IRAC groups."},
	{"threat": "Spotted Wing Drosophila", "threat_category": "Insect", "crop_stage": "BBCH 81–89 (straw colour on)",
	 "sample_unit": "Per Trap Per Week", "comparison": "Greater Than Or Equal", "action_threshold": 1,
	 "recommended_methods": "Yeast/vinegar bait traps from straw colour; treat at first catch once fruit is "
	 "susceptible and keep covered to harvest; harvest promptly and remove culls; rotate IRAC 5 / 28 / 3A."},
	{"threat": "Black Cherry Aphid", "threat_category": "Insect", "crop_stage": "BBCH 54–75",
	 "sample_unit": "Percent Infested", "comparison": "Greater Than Or Equal", "action_threshold": 10,
	 "warning_threshold": 5,
	 "recommended_methods": "Check 50 terminals per block; conserve lady beetles and lacewings; a delayed-dormant "
	 "oil for overwintering eggs; a selective aphicide only above threshold and before leaves curl tight."},
	{"threat": "Obliquebanded Leafroller", "threat_category": "Insect", "crop_stage": "BBCH 57–75",
	 "sample_unit": "Percent Infested", "comparison": "Greater Than Or Equal", "action_threshold": 2,
	 "recommended_methods": "Shoot sampling pre-bloom and post-bloom; pheromone traps to time the summer "
	 "generation; Bt or a selective IRAC 5 / 28 product at egg hatch; conserve Trichogramma and tachinids."},
	{"threat": "Spider Mites", "threat_category": "Insect", "crop_stage": "BBCH 71–89",
	 "sample_unit": "Per Leaf", "comparison": "Greater Than Or Equal", "action_threshold": 5, "warning_threshold": 2,
	 "beneficial_ratio_min": 0.1,
	 "recommended_methods": "Brush or count 25 leaves per block; hold off where predatory mites are at least one per "
	 "ten spider mites; avoid pyrethroids that flare mites; a selective miticide only above threshold."},
	{"threat": "San Jose Scale", "threat_category": "Insect", "crop_stage": "Dormant; crawlers BBCH 69–79",
	 "sample_unit": "Percent Infested", "comparison": "Greater Than", "action_threshold": 0,
	 "recommended_methods": "Scale on last year's fruit or wood means treat: dormant/delayed-dormant oil; time any "
	 "crawler spray by pheromone-trap biofix and degree-days; conserve lady beetles."},
	{"threat": "Powdery Mildew", "threat_category": "Disease", "crop_stage": "BBCH 60–89",
	 "sample_unit": "Percent Infested", "comparison": "Greater Than", "action_threshold": 0,
	 "recommended_methods": "Model-driven (Gubler-Thomas index) protectant program from shuck fall; act at first "
	 "colonies on leaves or fruit; rotate FRAC groups; sulfur where temperatures allow."},
	{"threat": "Bacterial Canker", "threat_category": "Disease", "crop_stage": "Dormant to bloom; after frost",
	 "sample_unit": "Percent Infested", "comparison": "Greater Than", "action_threshold": 0,
	 "recommended_methods": "Prune only in dry weather; protect before frost and wet periods (copper where the label "
	 "allows); remove cankers; avoid nitrogen late in the season."},
	{"threat": "European Starling", "threat_category": "Vertebrate", "crop_stage": "BBCH 81–89",
	 "sample_unit": "Percent Infested", "comparison": "Greater Than", "action_threshold": 0,
	 "recommended_methods": "Netting, rotated scare devices, prompt harvest. Not MBTA-protected; any lethal control "
	 "follows state and local rules."},
	{"threat": "American Robin", "threat_category": "Vertebrate", "crop_stage": "BBCH 81–89",
	 "sample_unit": "Percent Infested", "comparison": "Greater Than", "action_threshold": 0,
	 "recommended_methods": "MBTA-protected: netting, rotated scare devices, raptor perches and kestrel boxes, "
	 "prompt harvest. Lethal control only under a USFWS depredation permit."},
	{"threat": "Vole", "threat_category": "Vertebrate", "crop_stage": "Fall through early spring",
	 "sample_unit": "Count", "comparison": "Greater Than Or Equal", "action_threshold": 1,
	 "recommended_methods": "Apple-slice test of runways; barn owl boxes and perches; clean tree rows and trunk "
	 "guards. Any rodenticide goes through the rodent-bait task rules (applicator, notice, label)."},
	{"threat": "Pocket Gopher", "threat_category": "Vertebrate", "crop_stage": "Year round",
	 "sample_unit": "Count", "comparison": "Greater Than Or Equal", "action_threshold": 1,
	 "recommended_methods": "Fresh mounds mean an active gopher: trap in the run; raptor perches. Any toxic bait "
	 "goes through the rodent-bait task rules."},
]
