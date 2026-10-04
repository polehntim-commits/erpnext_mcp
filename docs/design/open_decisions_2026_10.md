# Open decisions for Tim (October 2026 design notes)

One list for every proposed design. My recommendation is first in each item; a one-word answer per line is
enough. Nothing here is built until approved.

## Queue and engine — `ccf_core_work_timing.md`, `config_lifecycle_and_tool_consolidation.md`

1. Build order: CCF core + shared lifecycle first, then dates → checklists → Library → triggers phase 1 (live by
   late Dec) → SOP → quiz/video → upload links → degree days. **OK?**
2. Existing 14 CCF rules stay on their current fields (optional migration later) — or move all to the tree now?
3. Replaying past weather defaults to "as decided" (saved snapshots), archive as an option?
4. Rule staging defaults to shadow mode (log only) — or real actions on staged blocks/crews?
5. Publishing AI-proposed versions only in the Desk/phone for **all** kinds (today MCP can publish wizards,
   tiles, labels)? (Payroll kinds and Work Timing rules are human-only regardless.)
6. Approve the 8 generic tools (incl. `draft_config`; retire folded into rollback)?
7. Remove deprecated aliases after 14 days with zero calls + Tim's OK — or a fixed date?
8. Where does the "3:30 AM compliance brief" run? (Not found on this Mac, in claude.ai routines, crontab or
   launchd.)
9. Payroll: include `overtime_rule` (move 40 h / 1.5× into data, values unchanged)?
10. Payroll preview flags employees whose net changes by more than **$1** — or a percentage?

## Go/Hold, weather and stage

11. "Harvest complete" for a block: a manual **Harvest done** tap per block — or last harvest task / season end?
12. Missing stage data: always Hold (strict) — or a template may allow "Go, verify stage in field"?
13. Stage names and degree-day thresholds: I draft from the OSU/WSU papers, marked unverified for Tim — or Tim
    supplies?
14. Triggered templates raise one task per block automatically — or managers create, trigger only times them?
15. Farm-wide default "dry day": rain probability < 30% and < 1 mm, overridable per rule?
16. Hold check at the start of **each work day** — or only the task's first start?
17. Override: reason + supervisor's name — or the supervisor confirms on their phone?
18. Overhead water from tasks flagged `overhead_water` + a Start of Day question — or build a real irrigation
    schedule later?
19. Rain check: ≥ 0.01 in **or** ≥ 40% on every forecast day — or amount only beyond day 3?
20. Spray presets point at Weather Settings wind (15 mph on OML), a rule adds stricter/minimum only when needed?
21. Archive check a week after wounding work ("did it actually rain?"): now or later?
22. H3 resolution 7 (~5 km²) for sharing forecasts — finer for Mill Creek terrain?
23. Burning: worker confirmation now, NWS red-flag feed later — or red-flag in phase 1?
24. Preset "verify" values (spray wind/temperature, 4 h rain-fast fallback, harvest heat 85°F, Wasco fire season):
    Tim supplies, or I draft from the Library?
25. BBCH: stage required on the phone, flagged-not-required over MCP?
26. Sweet-cherry scale: I draft codes/names/Spanish from the published stone-fruit scale for Tim to check; our
    own block photos?
27. Task stage prompts at start and in the End of Day check (not a separate prompt)?
28. A backwards stage is kept and flagged "check" — or blocked?

## Dates, checklists, notices

29. Reminder recipient "supervisor" = the assignee's reports-to, falling back to the dispatcher?
30. A Cancelled blocker unblocks its dependants, with a note?
31. "Morning brief": nothing by that name exists — overdue in the owner dashboard + a tile, or build a daily
    brief push/email?
32. Reminders at 06:00 site time, 2 days before?
33. Daily checks are Farm Tasks (no Daily Check doctype)?
34. No-work notice when the supervisor doesn't answer by the cutoff: auto-send — or wait?
35. "Expected workers" = crew task members + assignee + tomorrow's dispatched — or also anyone on the block in the
    last 2 days?
36. Crew messaging settings on the crew task — or a real Crew record?
37. Default start time in "work resumes" messages: 07:00 farm-wide, crews override?
38. Rule approvers for Work Timing = the SOP approvers list (Tim only for now)?
39. Several rules on one task: all must pass; Advisory rules warn only?

## SOP, Library, training, uploads

40. SOP approval rule when more approvers are added: all required — or any one?
41. Library: approved (Mac Vision OCR, editable tags, before SOP). No open items.
42. Training evidence: one Training Evidence doctype — or two tables?
43. Shuffle quiz choices (except true/false)?
44. Require videos before the quiz: no by default, optional per-course minimum %?
45. Sign-off on phone and Desk (both)?
46. Optional Spanish video URL per entry?
47. Upload links: video cap 2 GB?
48. Upload links: private-only by default; public needs an extra admin switch?
49. Test multi-GB uploads over Funnel on umbrel.local first?
