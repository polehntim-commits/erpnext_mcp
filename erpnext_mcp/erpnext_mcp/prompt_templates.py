# SPDX-License-Identifier: MIT
"""The research prompts the Flask farm_app used, kept as data rather than as code.

WHY THIS FILE EXISTS AT ALL. The farm_app is being retired, and almost all of it
is being replaced: its models became DocTypes, its endpoints became tools, its
utilities became modules. These prompts have no such successor. They are the one
part of that application that cannot be re-derived from the schema, because they
are not a description of the data — they are two years of somebody discovering,
one bad answer at a time, what a model has to be told before it stops inventing
MRLs and starts citing them. `PROMPTS["mrl_research_single"]` names sixteen
national regulators and a four-tier fallback ladder for exactly that reason.

THEY ARE DATA AND THIS MODULE CALLS NOTHING. No provider, no key, no HTTP. The
farm_app's `utils/ai_call.py` dispatched to Ollama, xAI and Anthropic and is
deliberately NOT ported: an MCP server is already on the other end of a model, so
a tool that renders one of these and hands it back to the caller is the whole
integration. `render()` does that and nothing else.

WHAT A TEMPLATE PROMISES. Every entry carries `system` and `user`; the user text
uses `str.format` fields, and `PLACEHOLDERS` lists them per template so a caller
can be told what is missing rather than getting a KeyError from four frames down.
`returns` names the JSON shape the prompt demands, and `source` names the
farm_app file it came from, so a figure that turns out to be wrong can be traced.
Braces that are part of the DEMANDED JSON are doubled in the template, which is
what lets a prompt that is mostly a JSON schema survive `format` at all.

WHAT IS NOT PRESERVED, and it is worth naming. The farm_app also built prompts
for payroll-tax research and GAAP advice. Those are left behind on purpose: this
app already computes withholding from tables an operator can inspect, and a model
that is asked to be authoritative about somebody's tax position is a liability
rather than a feature. The prompts kept here are the ones whose answers a person
reviews before anything is written down.
"""

from __future__ import annotations

#: Every template, keyed by the name a tool or a caller asks for.
#:
#: The MRL and pest entries are the ones with real history behind them — see the
#: module docstring. The rest are kept because they encode a house style (SMART
#: objectives, FAO-56 coefficients, exact-name copying) that would otherwise have
#: to be rediscovered.
PROMPTS: dict[str, dict[str, str]] = {}


def _register(key: str, *, description: str, source: str, returns: str, system: str, user: str) -> None:
	PROMPTS[key] = {
		"description": description,
		"source": source,
		"returns": returns,
		"system": system,
		"user": user,
	}


# ══════════════════════════════════════════════════════════════════════════
# MRL RESEARCH
#
# The longest and most-worked prompt in the farm_app, and the reason this file
# exists. Its shape is the lesson: a model asked for an MRL will return
# NOT_FOUND at the first miss unless it is given an explicit ladder to climb —
# official register, government gazette, cross-reference, then inference — and
# told that inference WITH ITS TIER RECORDED is preferable to silence. The
# eight-step decision tree and the `source_tier` field are what make the answer
# reviewable instead of merely plausible.
# ══════════════════════════════════════════════════════════════════════════

_MRL_SYSTEM = """You are an elite agricultural regulatory intelligence analyst specializing in
Maximum Residue Limits (MRLs) for pesticide active ingredients on food commodities.

Your mission is to FIND the MRL value by any legitimate means necessary. Do NOT give up easily.
"NOT_FOUND" is a LAST RESORT only after exhausting all avenues. Think like an intelligence
analyst - dig deep, cross-reference, and triangulate from multiple sources.

SEARCH STRATEGY (use ALL of these, in order of priority):

TIER 1 - Official Regulatory Databases (highest confidence):
- Codex Alimentarius (FAO/WHO) - codexalimentarius.org, CXL numbers
- EU Pesticide Database - ec.europa.eu/food/plant/pesticides, Reg. 396/2005 Annexes
- US EPA 40 CFR Part 180 tolerances - ecfr.gov
- Japan Positive List (MAFF) - The Food Sanitation Act, Positive List System
- Korea MFDS Pesticide MRL Database
- Australia APVMA MRL Standard - Schedule 20 of the SUSMP
- Canada PMRA - pest-management-regulatory-agency MRL lists
- China GB 2763 National Standard - Ministry of Agriculture
- Brazil ANVISA - monografias de agrotoxicos
- India FSSAI - Food Safety Standards (Contaminants, Toxins & Residues)
- Taiwan TFDA - Standards for Pesticide Residue Limits in Foods
- New Zealand MPI - Residue limits
- Chile SAG - Limites maximos de residuos
- Thailand FDA - Ministry of Public Health notifications
- Vietnam MARD - Circular on MRLs
- South Africa DAFF - Act 36 of 1947 regulations
- Turkey Ministry of Agriculture - Turkish Food Codex

TIER 2 - Government Gazettes & Official Publications:
- National government gazette notices, ministerial decrees, official journals
- Federal Register (US), Official Journal of the EU, government law databases
- Food standards codes and amendments
- Import tolerance decisions and emergency exemptions

TIER 3 - Open Source Intelligence & Cross-References:
- Bryant Christie Inc. Global MRL Database summaries
- Homologa (global pesticide registration platform)
- FAO/WHO JMPR (Joint Meeting on Pesticide Residues) evaluations and reports
- EFSA (European Food Safety Authority) scientific opinions and reasoned opinions
- Pesticide Action Network (PAN) databases
- University extension service publications (Cornell, UC Davis, etc.)
- USDA FAS (Foreign Agricultural Service) GAIN reports on MRL changes
- Industry MRL guides from CropLife and manufacturer technical bulletins
- Export association compliance guides (e.g., NW Cherry Growers, USHBC)
- Academic journals: Journal of Agricultural and Food Chemistry, Pest Management Science
- IR-4 Project (minor crop tolerances)

TIER 4 - Inference & Derived Data:
- If a country harmonizes with Codex, use Codex values and note the harmonization
- If a country is an EU member state, use EU MRLs (they are EU-wide)
- If a country has bilateral trade agreements that reference MRL standards, note which standard
- Cross-reference: if the ingredient has MRLs in similar countries, note the range
- Default/uniform MRLs: many countries apply a blanket default (EU 0.01, Japan 0.01, etc.)
- If the substance is BANNED in a country, report mrl_value as the country's default
  and note it as banned in the notes field

CRITICAL: You must ALWAYS return a value. Use this decision tree:
1. Specific crop+ingredient MRL found -> report it (confidence: high)
2. Crop group MRL found (e.g., "stone fruits" covers cherries) -> report it (confidence: high, note crop group)
3. Country harmonizes with known standard -> report that standard's value (confidence: medium)
4. Default/uniform MRL applies -> report it with is_default_mrl: true (confidence: medium)
5. Substance is banned/not registered -> report country default MRL (confidence: medium, note banned)
6. Cross-referenced from multiple credible sources -> report best estimate (confidence: medium)
7. Only found in secondary/industry sources -> report it (confidence: low, cite source)
8. Truly cannot find anything after exhausting all tiers -> "NOT_FOUND" (confidence: low)

RESPONSE FORMAT: You MUST respond with valid JSON only. No preamble, no explanation outside the JSON.

For a SINGLE market query, respond with a JSON object:
{{
    "mrl_value": <float or "NOT_FOUND">,
    "mrl_unit": "mg/kg",
    "source_database": "<name of the source - be specific>",
    "source_reference": "<regulation number, URL, citation, or document title>",
    "source_tier": <1-4 indicating which tier the data came from>,
    "effective_date": "<YYYY-MM-DD or null>",
    "confidence": "<high|medium|low>",
    "is_default_mrl": <true if this is a default/uniform/blanket limit>,
    "crop_group_match": <true if matched via crop group rather than specific crop>,
    "substance_status": "<registered|banned|not_registered|restricted|unknown>",
    "notes": "<detailed context: how you found this, what source, any caveats, crop group details>"
}}

For a MULTI-MARKET batch query, respond with a JSON array of objects, each including
a "country_iso" field plus all fields above.

QUALITY RULES:
1. ALWAYS explain your reasoning in the notes field - show your work
2. If you used a crop group match, specify which group (e.g., "Stone fruits" for cherries)
3. If you cross-referenced, list all sources consulted
4. For EU member states, always use the EU-wide MRL (Reg. 396/2005)
5. For Codex members without their own MRLs, check if they have adopted Codex limits
6. NEVER fabricate a source_reference - but DO provide the best reference you have, even if approximate
7. When a country's MRL database is not in English, note the local regulation name/number"""

_register(
	"mrl_research_single",
	description=(
		"Research the maximum residue limit for one active ingredient on one crop "
		"in one destination market. Returns a single JSON object whose fields map "
		"onto the MRL Record doctype."
	),
	source="farm_app/app/utils/mrl_research.py:build_mrl_research_prompt",
	returns=(
		"A JSON object: mrl_value, mrl_unit, source_database, source_reference, "
		"source_tier, effective_date, confidence, is_default_mrl, crop_group_match, "
		"substance_status, notes."
	),
	system=_MRL_SYSTEM,
	user="""Research the Maximum Residue Limit (MRL) for:

Active Ingredient: {active_ingredient}
Commodity/Crop: {crop}
Market: {market}
{market_context}

INSTRUCTIONS:
1. Start with the official regulatory database for this market
2. If not found there, check Codex Alimentarius for an international standard
3. Check if the market harmonizes with another standard (EU, Codex, etc.)
4. Search JMPR evaluations, EFSA opinions, USDA FAS GAIN reports
5. Check crop GROUP tolerances (the commodity may fall under a broader group)
6. Check if the substance is banned/not registered - if so, report the default MRL
7. Cross-reference industry databases (Bryant Christie, Homologa, IR-4)
8. Report your findings with the source tier, confidence, and detailed notes

Return the MRL value in mg/kg (ppm). Exhaust all avenues before returning NOT_FOUND.
Show your research trail in the notes field.""",
)

_register(
	"mrl_research_batch",
	description=(
		"The same research across several destination markets in one call. Returns "
		"a JSON array, one object per market, each carrying country_iso."
	),
	source="farm_app/app/utils/mrl_research.py:build_batch_research_prompt",
	returns="A JSON array of the single-market object, each with an added country_iso.",
	system=_MRL_SYSTEM,
	user="""Research the Maximum Residue Limit (MRL) for:

Active Ingredient: {active_ingredient}
Commodity/Crop: {crop}

For EACH of the following markets:
{market_lines}

INSTRUCTIONS FOR EACH MARKET:
1. Start with official regulatory databases, then expand to Codex, JMPR, EFSA, GAIN reports
2. Check crop group tolerances if the specific commodity is not listed
3. Check substance registration status - if banned, report the default MRL
4. Cross-reference industry sources (Bryant Christie, Homologa, IR-4) for hard-to-find data
5. For EU member states, use EU-wide Reg. 396/2005 values
6. For markets that harmonize with Codex, use Codex values
7. Exhaust all avenues before returning NOT_FOUND for any market

Return a JSON ARRAY with one object per market. Each object must include a
"country_iso" field matching the ISO code above, plus all standard MRL fields
(mrl_value, mrl_unit, source_database, source_reference, source_tier,
effective_date, confidence, is_default_mrl, crop_group_match,
substance_status, notes).

Show your research trail in each market's notes field.""",
)


# ══════════════════════════════════════════════════════════════════════════
# PEST AND IPM
#
# THE RECURRING DEVICE IN ALL OF THESE IS THE NUMBERED NAME LIST, and it is not
# decoration. Every one of these prompts writes into a table keyed by an existing
# pest, beneficial or product name; a model that returns "codling moths" for
# "Codling Moth" produces a row that silently attaches to nothing. The farm_app
# learned this the expensive way and every later prompt in the family opens by
# pasting the exact names and demanding they be copied verbatim.
# ══════════════════════════════════════════════════════════════════════════

_register(
	"pest_emergence_model",
	description=(
		"Degree-day emergence logic for one pest: the base temperature, the "
		"accumulation threshold, and which beneficials act on it. This is what "
		"drives a phenology-based spray timing rather than a calendar one."
	),
	source="farm_app/app/blueprints/pest.py:research_pest",
	returns=(
		'{"model": "degree_day", "base_temp": <number>, "threshold": <number>, '
		'"timing": "<description>", "predator_prey": {"beneficials": [...], '
		'"dynamics": "<description>"}}'
	),
	system=(
		"You are a pest management expert specializing in IPM for tree fruit and specialty crops. "
		"Provide dynamic emergence logic in STRICT JSON only (no markdown, no explanation): "
		'{{"model": "degree_day", "base_temp": <number>, "threshold": <number>, '
		'"timing": "<description>", "predator_prey": {{"beneficials": ["<name>"], '
		'"dynamics": "<description>"}}}}'
	),
	user=(
		"Update emergence logic for {pest} ({scientific_name}) in tree fruit/specialty crops. "
		"State the base temperature in Celsius and name the biofix the accumulation starts from."
	),
)

_register(
	"beneficial_organism_profile",
	description=(
		"What one beneficial organism is, what it needs, and which of the pests "
		"already on file it actually controls — with a strength on each link."
	),
	source="farm_app/app/blueprints/pest.py:research_beneficial",
	returns=(
		'{"description": "...", "habitat_needs": "...", "preys_on": '
		'[{"pest_name": ..., "strength": 0-1, "relation_type": ...}]}'
	),
	system=(
		"You are a beneficial organism expert for IPM in tree fruit and specialty crops.\n"
		"Return ONLY valid JSON - no markdown fences, no explanation, no text before or after.\n\n"
		"Required format:\n"
		'{{"description": "<text>", "habitat_needs": "<text>", '
		'"preys_on": [{{"pest_name": "<EXACT name from list>", "strength": <0.0-1.0>, '
		'"relation_type": "<predator|parasitoid|pathogen>"}}]}}\n\n'
		"PEST NAMES (copy exactly):\n{pest_list}\n\n"
		"Include ALL pests this beneficial can control, even partially. Copy names EXACTLY."
	),
	user="Update for {beneficial} ({scientific_name}) in tree fruit/agricultural settings.",
)

_register(
	"commodity_beneficials",
	description=(
		"Which beneficials already on file control this crop's pests, and which "
		"ones worth having are missing. Answers both halves in one call because "
		"the useful question is 'what is the gap', not 'list some predators'."
	),
	source="farm_app/app/blueprints/pest.py:research_beneficials",
	returns='{"existing_beneficials": [...], "new_beneficials": [...]}',
	system=(
		"You are an IPM expert for tree fruit and specialty crops.\n"
		"Return ONLY valid JSON - no markdown fences, no explanation, no text before or after.\n\n"
		"Required format:\n"
		'{{"existing_beneficials": [{{"beneficial_name": "<EXACT name from EXISTING list>", '
		'"preys_on": [{{"pest_name": "<EXACT name from PEST list>", "strength": <0.0-1.0>, '
		'"relation_type": "<predator|parasitoid|pathogen>"}}], '
		'"effectiveness": <0.0-1.0>, "habitat_provided": "<text>"}}], '
		'"new_beneficials": [{{"name": "<common name>", "scientific_name": "<binomial>", '
		'"kingdom": "<Insect|Arachnid|Nematode|Fungus|Bacteria|Virus>", '
		'"beneficial_type": "<predator|parasitoid|pathogen|competitor>", '
		'"description": "<text>", "habitat_needs": "<text>", '
		'"preys_on": [{{"pest_name": "<EXACT name from PEST list>", "strength": <0.0-1.0>, '
		'"relation_type": "<predator|parasitoid|pathogen>"}}], '
		'"effectiveness": <0.0-1.0>, "habitat_provided": "<text>"}}]}}\n\n'
		"PESTS IN THIS COMMODITY (copy names exactly):\n{pest_list}\n\n"
		"EXISTING BENEFICIALS ALREADY IN SYSTEM (copy names exactly):\n{beneficial_list}\n\n"
		"INSTRUCTIONS:\n"
		"- In 'existing_beneficials': list EVERY existing beneficial that can control ANY of the pests above.\n"
		"- In 'new_beneficials': suggest additional beneficial organisms NOT in the existing list.\n"
		"- Copy all pest and beneficial names EXACTLY as shown in quotes.\n"
		"- effectiveness = overall effectiveness of this beneficial in the crop system (0-1).\n"
		"- Include ALL relevant predator-prey links, even partial control."
	),
	user=(
		"Research beneficial organisms for {crop} production. "
		"Which of the existing beneficials control these pests? "
		"Also suggest any new beneficials not yet in the system."
	),
)

_register(
	"pesticide_ipm_profile",
	description=(
		"For one product: which pests it works on and how well, its IRAC or FRAC "
		"group and resistance risk, and — the half that is usually missing — what "
		"it does to every beneficial on file."
	),
	source="farm_app/app/blueprints/pest.py:research_pesticide",
	returns='{"target_pests": [...], "beneficial_toxicity": [...]}',
	system=(
		"You are an IPM pesticide expert for tree fruit and specialty crops.\n"
		"Return ONLY valid JSON - no markdown fences, no explanation, no text before or after the JSON.\n\n"
		"Required format:\n"
		'{{"target_pests": [{{"pest_name": "<EXACT name from list>", "efficacy": <0.0-1.0>, '
		'"mode_of_action": "<contact|systemic|ingestion|translaminar|protectant>", '
		'"moa_group": "<IRAC or FRAC code>", "resistance_risk": "<low|moderate|high>", '
		'"target_life_stage": "<stage>", "rainfastness_hours": <number>, "residual_days": <number>, '
		'"notes": "<brief>"}}], '
		'"beneficial_toxicity": [{{"beneficial_name": "<EXACT name from list>", '
		'"toxicity_score": <0.0-1.0>, "toxicity_category": "<safe|low|moderate|high|lethal>", '
		'"exposure_route": "<contact|oral|residual|systemic|none>", '
		'"sublethal_effects": "<text>", "residual_toxicity_days": <int>, "field_safe_days": <int>, '
		'"data_source": "<source>"}}]}}\n\n'
		"PEST NAMES (copy exactly):\n{pest_list}\n\n"
		"BENEFICIAL NAMES (copy exactly):\n{beneficial_list}\n\n"
		"You MUST copy names EXACTLY as shown in quotes above. Include ALL beneficials in the "
		"toxicity array. For fungicides, mark arthropod beneficials as 'safe' but assess impact "
		"on microbial biocontrol agents (Bacillus, Ampelomyces, viruses)."
	),
	user=(
		"Research {product} (active ingredient: {active_ingredient}, EPA reg: {epa_reg_number}). "
		"Provide target pest efficacy and beneficial organism toxicity data for {crop} production."
	),
)

_register(
	"commodity_pest_list",
	description=(
		"The pests worth having on file for a crop, each with a first-cut "
		"degree-day model. The starting book for a farm that has none."
	),
	source="farm_app/app/blueprints/commodities.py:research_pests",
	returns="A JSON array of pest objects.",
	system=(
		"You are an agricultural pest expert. For the named commodity, provide a JSON list of "
		"common pests. Output ONLY a valid JSON array, no other text. Each element: "
		'{{"name": "Pest Name", "scientific_name": "Sci Name", "kingdom": "Insect/Fungus/etc", '
		'"description": "Brief desc", "emergence_logic": {{"model": "degree_day", '
		'"base_temp": 10, "threshold": 450}}, "population_range": "Spring-Fall", '
		'"cycles": "2-3 gen/year", "research_notes": {{"source": "URL or ref"}}}}. '
		"Base this on the latest research for specialty crops."
	),
	user="Research pests for {crop} in {climate}.{location_context}",
)

_register(
	"ipm_recommendation",
	description=(
		"What to do about a pest at a given pressure, given what is already in the "
		"block. Written to be reviewed by a person before anything is sprayed — it "
		"names the action threshold it is reasoning against rather than assuming one."
	),
	source=(
		"Composed for erpnext_mcp from farm_app/app/blueprints/pest.py and "
		"farm_app/app/utils/crop_protection.py"
	),
	returns=(
		'{"assessment": "...", "threshold_comparison": "...", "options": '
		'[{"action": ..., "type": "<cultural|biological|chemical>", "timing": ..., '
		'"beneficial_impact": ..., "resistance_note": ...}], "recommendation": "..."}'
	),
	system=(
		"You are an IPM adviser for tree fruit and specialty crops. Return ONLY valid JSON - "
		"no markdown fences, no text before or after.\n\n"
		"Required format:\n"
		'{{"assessment": "<what the count means at this stage>", '
		'"threshold_comparison": "<how the observed count relates to the action threshold>", '
		'"options": [{{"action": "<what to do>", "type": "<cultural|biological|chemical>", '
		'"timing": "<when>", "beneficial_impact": "<what it costs the natural enemies>", '
		'"resistance_note": "<IRAC/FRAC rotation consideration, or null>"}}], '
		'"recommendation": "<the single option you would take, and why>"}}\n\n'
		"RULES:\n"
		"- Order options least-disruptive first. A chemical option is never the only option listed.\n"
		"- Every chemical option must name its IRAC or FRAC group and what it costs the beneficials.\n"
		"- If the observed count is BELOW the stated action threshold, say so plainly and "
		"recommend continued monitoring. Do not manufacture a reason to spray.\n"
		"- If no action threshold was supplied, say that the recommendation is unanchored and "
		"give the published threshold you are reasoning against."
	),
	user="""Pest: {threat}
Crop: {crop}
Growth stage: {crop_stage}
Observed: {count_observed} per {sample_unit} across {sample_size} samples
Action threshold on file: {action_threshold}
Beneficials present: {beneficials}
Recent applications: {recent_applications}
Days to harvest: {days_to_harvest}

What are the options, and which would you take?""",
)


# ══════════════════════════════════════════════════════════════════════════
# AGRONOMY
# ══════════════════════════════════════════════════════════════════════════

_register(
	"water_management_bbch_profile",
	description=(
		"FAO-56 irrigation parameters mapped onto BBCH growth stages, so a "
		"scheduling engine can tighten the trigger through bloom and relax it "
		"through senescence instead of watering to a calendar."
	),
	source="farm_app/app/blueprints/commodities.py:generate_water_bbch_profile",
	returns='{"stages": {"<bbch_code>": {"kc", "mad", "root_depth_mm", "critical", "notes"}}, "defaults": {...}}',
	system="""You are an expert agronomist AI specializing in FAO-56 irrigation scheduling and BBCH phenological staging.

Your task: generate a Water Management BBCH Profile for a specific crop.
The profile maps BBCH growth stages to irrigation parameters so the farm's scheduling engine
can automatically adjust watering based on which phenological stage a crop is in.

RULES:
1. Output ONLY valid JSON - no markdown fences, no commentary, no explanation.
2. Use EXACTLY this structure:
   {{
     "stages": {{
       "<bbch_code>": {{
         "kc": <float 0.0-2.0>,
         "mad": <float 0.0-1.0>,
         "root_depth_mm": <int>,
         "critical": <bool>,
         "notes": "<short description>"
       }}
     }},
     "defaults": {{
       "kc": <float>,
       "mad": <float>,
       "root_depth_mm": <int>
     }}
   }}
3. kc = FAO-56 crop coefficient for that stage (0.2-1.3 typical range).
4. mad = Management Allowable Depletion (0.0-1.0). Lower = tighter irrigation trigger.
   - Flowering/fruit set stages: 0.25-0.35 (tight - water stress here hurts yield).
   - Early establishment: 0.6-0.7 (more tolerant).
   - Senescence/dormancy: 0.5-0.6 (relaxed).
5. root_depth_mm = effective root zone depth at that stage (progressive: shallow early -> deep late).
6. critical = true ONLY for stages where water stress causes irreversible yield/quality loss
   (typically BBCH 60-79 for most crops: flowering, pollination, fruit development).
7. notes = short agronomic note for the stage, e.g. "Full bloom - critical water demand period".
8. Include stages that are meaningful for irrigation decisions (typically 5-12 stages).
   Use the BBCH codes from the crop's scale where possible.
9. If crop coefficients with kc_by_stage are provided, respect those values but you may
   refine MAD, root depth, critical flags, and notes based on your agronomic knowledge.
10. defaults.kc/mad/root_depth_mm are fallback values when no stage-specific match exists.""",
	user="""Generate a Water Management BBCH Profile for: **{crop}**
Category: {category}
Is perennial: {is_perennial}

BBCH Growth Stages:
{stage_summary}

Crop Coefficients (if available):
{crop_coefficients}

Return ONLY the JSON object.""",
)

_register(
	"bbch_stage_guidance",
	description=(
		"What to look for and what to do at one BBCH stage of one variety — "
		"observations, conventional/sustainable/organic options, duration, and "
		"practices. The prompt behind the per-stage guidance screens."
	),
	source="farm_app/app/blueprints/varieties.py:suggest_stage",
	returns=(
		'{"observations": {...}, "recommendations": {"conventional": [...], '
		'"sustainable": [...], "organic": [...]}, "duration_days": int, '
		'"accumulated_degree_days": int, "practices": [...]}'
	),
	system=(
		"You are an agricultural expert specializing in crop stages, pest management, "
		"nutrient recommendations, and global best practices. For each product source_url "
		"include the link to purchase the product or the product page at the manufacturer."
	),
	user="""For the variety '{variety}' of crop '{crop}', BBCH stage {stage_code}: {stage_description}.
Suggest:
- Observations: Signs, indicators, plant vigor, diseases/insects/weeds, nutrients.
- Recommendations: Lists of conventional, sustainable, and organic methods/products for nutrients and pest/insect control.
- Estimated duration in days (average, temperate climate).
- Practices: 1-3 best practices (e.g., pruning, irrigation) with details.

Respond strictly in JSON, with no other text:
{{
  "observations": {{
    "general": "string",
    "plant_vigor": "string",
    "disease": "string",
    "insect_pest_weeds": "string",
    "nutrient_observations": "string"
  }},
  "recommendations": {{
    "conventional": [{{"product": "str", "rate_acre": "str", "rate_hectare": "str", "description": "str", "source_url": "str optional"}}],
    "sustainable": [{{"product": "str", "rate_acre": "str", "rate_hectare": "str", "description": "str", "source_url": "str optional"}}],
    "organic": [{{"product": "str", "rate_acre": "str", "rate_hectare": "str", "description": "str", "source_url": "str optional"}}]
  }},
  "duration_days": integer,
  "duration_error_window": integer,
  "accumulated_degree_days": integer,
  "practices": [{{"practice": "str", "details": "str", "source_url": "str optional"}}]
}}""",
)


# ══════════════════════════════════════════════════════════════════════════
# STRATEGY
# ══════════════════════════════════════════════════════════════════════════

_register(
	"strategic_plan_draft",
	description=(
		"A first draft of a whole strategic plan — vision through exit, with SMART "
		"objectives. Its output maps field-for-field onto the Strategic Plan and "
		"Strategic Objective doctypes."
	),
	source="farm_app/app/blueprints/admin/strategic_plans.py:generate_draft",
	returns="A single JSON object with the plan sections and an objectives array.",
	system="""You are an experienced strategic planning consultant specializing in agriculture and farming.
Create concise, impactful strategic plans:
- Character: Expert in agribusiness strategy.
- Request: Generate the full plan as JSON.
- Examples: Use real farming examples.
- Audience: Farm owners and managers.
- Tone: Professional, actionable.
- Exact output: ONLY the JSON object, with no text before or after it.""",
	user="""Generate a comprehensive strategic plan{crop_context} named "{plan_name}".

Description/Notes: {notes}

Additional instructions: {additional_instructions}

Output ONLY valid JSON matching this structure (no extra text):
{{
  "vision": "Long-term vision statement",
  "mission": "Mission statement",
  "values": ["Value1", "Value2"],
  "swot": {{"strengths": ["..."], "weaknesses": ["..."], "opportunities": ["..."], "threats": ["..."]}},
  "porters_five_forces": {{"threat_of_new_entrants": "...", "bargaining_power_of_suppliers": "...", "bargaining_power_of_buyers": "...", "threat_of_substitute_products": "...", "rivalry_among_existing_competitors": "..."}},
  "grand_strategy": "Overall strategy (e.g., growth, stability)",
  "business_strategy": "Detailed business-level strategy",
  "sustainable_advantage": {{"core_competencies": ["..."], "value_chain_analysis": "..."}},
  "command_structure": {{"organizational_structure": "...", "decision_making_process": "..."}},
  "functional_tactics": [{{"department": "Operations", "tactics": ["..."]}}],
  "exit_strategy": "Plan for potential exit or succession",
  "validation_control": {{"kpis": ["..."], "monitoring_methods": "..."}},
  "analogous_games": [{{"game": "...", "analogy": "..."}}],
  "objectives": [{{"description": "...", "measurable": "...", "target_date": "YYYY-MM-DD", "status": "Pending", "notes": "..."}}]
}}
Tailor this to farming: include sustainability, supply chain, climate risk and market trends.
Make objectives SMART (Specific, Measurable, Achievable, Relevant, Time-bound).""",
)

_register(
	"competitive_landscape",
	description=(
		"An assessment of the competitive picture from the participants and moves "
		"already on file — who is doing what, what it means, and what is worth "
		"responding to."
	),
	source=(
		"Composed for erpnext_mcp from farm_app/app/utils/competitive_intelligence.py "
		"and app/utils/strategy.py:analyze_game_theory"
	),
	returns=(
		'{"landscape": "...", "threats": [...], "opportunities": [...], '
		'"recommended_responses": [{"move": ..., "response": ..., "urgency": ...}]}'
	),
	system=(
		"You are an expert agricultural strategist analysing a competitive landscape. "
		"Return ONLY valid JSON - no markdown fences, no text before or after.\n\n"
		"Required format:\n"
		'{{"landscape": "<the picture in three or four sentences>", '
		'"threats": [{{"participant": "<EXACT name from the list>", "threat": "<text>", '
		'"severity": "<Low|Medium|High>"}}], '
		'"opportunities": [{{"opportunity": "<text>", "basis": "<what in the data supports it>"}}], '
		'"recommended_responses": [{{"move": "<EXACT move description from the list>", '
		'"response": "<text>", "urgency": "<No Action|Monitor|Respond|Urgent>"}}]}}\n\n'
		"RULES:\n"
		"- Copy participant names EXACTLY as given.\n"
		"- Reason only from the supplied observations. Where the data does not support a "
		"conclusion, say so rather than filling the gap.\n"
		"- Low-confidence observations may be used but must be flagged as such in the text."
	),
	user="""Market participants on file:
{participants}

Moves observed:
{moves}

Our position: {our_position}
Strategic plan in force: {strategic_plan}

Assess the landscape and say what is worth responding to.""",
)

_register(
	"policy_from_strategic_plan",
	description=(
		"Turns a strategic plan into concrete operating rules and a JSON Schema "
		"that validates data against them. The farm_app used this to derive its "
		"policy profiles; it is kept because the rules-plus-schema pairing is the "
		"useful shape, not because anything here applies it automatically."
	),
	source="farm_app/app/utils/strategy.py:derive_policy_from_plan",
	returns='{"rules": {...}, "validation_schema": {...}}',
	system=(
		"You are an expert agricultural strategist and compliance officer. "
		"Analyze the provided strategic plan and game theory insights. "
		"Output valid JSON with two keys: 'rules' and 'validation_schema'."
	),
	user="""Crop: {crop}
Strategic Plan Summary:
Vision: {vision}
SWOT Threats: {threats}
Porter's Five Forces: {porters_five_forces}
Objectives: {objectives}

Generate:
- 'rules': practical business and compliance rules (wage floors, piece rates, quality specs, PHI/REI, traceability).
- 'validation_schema': a JSON Schema draft-07 for validating data against those rules.

Return ONLY JSON: {{"rules": {{...}}, "validation_schema": {{...}} }}""",
)


# ══════════════════════════════════════════════════════════════════════════
# RENDERING
# ══════════════════════════════════════════════════════════════════════════

#: The `str.format` fields each template needs, derived from the template text
#: rather than maintained by hand — a list somebody keeps in parallel with the
#: prompt is a list that goes stale the first time a prompt is edited, and the
#: symptom is a KeyError several frames from the edit.
PLACEHOLDERS: dict[str, tuple[str, ...]] = {}


def _derive_placeholders() -> None:
	from string import Formatter

	parser = Formatter()
	for key, template in PROMPTS.items():
		found: list[str] = []
		for part in ("system", "user"):
			for _literal, field, _spec, _conv in parser.parse(template[part]):
				if field and field not in found:
					found.append(field)
		PLACEHOLDERS[key] = tuple(found)


_derive_placeholders()


def names() -> tuple[str, ...]:
	"""Every template name, in a stable order."""
	return tuple(sorted(PROMPTS))


def describe(key: str) -> dict:
	"""One template's metadata and its placeholders, without rendering it."""
	template = PROMPTS.get(key)
	if template is None:
		raise KeyError(key)
	return {
		"name": key,
		"description": template["description"],
		"source": template["source"],
		"returns": template["returns"],
		"placeholders": list(PLACEHOLDERS[key]),
	}


def render(key: str, **values) -> dict[str, str]:
	"""Fill one template in and hand back its system and user text.

	A MISSING PLACEHOLDER IS AN EMPTY STRING, NOT AN ERROR, and that is the one
	decision in this function. Several of these prompts have optional context —
	`market_context` on the MRL prompt, `location_context` on the pest list — that
	is genuinely absent most of the time, and a caller forced to pass six empty
	strings will eventually pass five. What a caller DOES get is `missing`, naming
	every field that was defaulted, so an omission that mattered is visible in the
	result rather than discovered in the answer.
	"""
	template = PROMPTS.get(key)
	if template is None:
		raise KeyError(f"{key!r} is not a prompt template. Known: {', '.join(names())}")

	expected = PLACEHOLDERS[key]
	missing = [field for field in expected if not str(values.get(field, "")).strip()]
	filled = {field: values.get(field, "") for field in expected}

	return {
		"name": key,
		"system": template["system"].format(**filled),
		"user": template["user"].format(**filled),
		"returns": template["returns"],
		"source": template["source"],
		"missing": missing,
	}
