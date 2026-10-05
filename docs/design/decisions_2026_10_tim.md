# Tim's decisions on `open_decisions_2026_10.md` (2026-10-04)

**Everything in the October design notes is APPROVED except where an answer below changes it.** Recorded from Tim's
answers as relayed, with the relayer's reading of the ambiguous ones. Numbers follow `open_decisions_2026_10.md`.

## Queue (the order of work)

1. CCF core + shared config lifecycle → 2. start/due dates → 3. daily checklists → 4. Reference Library →
5. triggers phase 1 (live by late December for dormant pruning), with suggested tasks, probabilistic dry-day,
auto-clearing Holds and no-work escalation → 6. SOP approval → 7. training quiz + video → 8. upload links (no video)
→ 9. irrigation schedule → 10. degree days → ~~11. walkie-talkie design note~~ (dropped 2026-10-04 by Tim).

## Queue and engine

1. **Build order:** yes, as proposed.
2. **The 14 existing CCF rules** stay on their current fields for now.
3. **Weather replay:** saved snapshots by default; the archive is optional.
4. **Staging:** a staged rule takes REAL actions (on its staged audience) — not shadow mode.
5. **AI-proposed versions:** publishing happens only in the Desk or on the phone, for every kind.
6. **Generic tools:** the eight are approved.
7. **Deprecated aliases:** no automatic removal and no date. Kept until Tim explicitly approves removal.
8. **3:30 AM compliance brief:** scheduled as a local Cowork task by the relayer. Nothing to build.
9. **Payroll settings** (overtime_rule and the rest) move into the shared lifecycle — only if payroll runs
   uninterrupted, and only after a preview diff proves identical output.
10. **Payroll preview flag:** a percentage threshold, configurable, default 2% (not $1).

## Go/Hold, weather, stage

11. **Harvest complete:** a combination — manual "Harvest done" per block, the last harvest task, season end — and it
    records which varieties were picked.
12. **Missing stage data:** allow "Go, verify stage in field", or estimate the stage from calendar + degree days.
13. **Stage names:** drafted from OSU/WSU and unified with BBCH, one vocabulary.
14. **Triggered tasks:** managers create them. ALSO build **suggested tasks** — work the system suggests given current
    conditions (weather, stage, holds lifted).
15. **Dry day:** probabilistic — risk computed from forecast probabilities. When the forecast that caused a Hold
    changes, the Hold clears by itself and the task may go.
16. **Hold check timing:** advisory check at the start of each work day. A Hold never blocks or delays pay for time
    already worked.
17. **Override:** the supervisor confirms on their phone, with a reason recorded.
18. **Overhead water:** OML has none — drop the flag. BUILD a real irrigation schedule, reusing zones, valves,
    runtime and the iOS scheduling.
19. **Rain threshold:** > 0.05 in, adjustable per rule.
20. **Spray wind:** default preset maximum 10 mph; the product label overrides; stricter or minimum rules only when
    needed.
21. **Archive check:** continuous, at least weekly, comparing what actually happened.
22. **H3 resolution:** 7.
23. **Burning:** check the forecast now and ahead ("will the fire be out" before conditions turn); red-flag awareness
    in phase 1 (winter is low risk).
24. **Preset values:** drafted from the Library.
25. **BBCH:** required on the phone; flagged but not required over MCP.
26. **Sweet-cherry scale:** draft from the published stone-fruit scale plus our own block photos.
27. **Stage prompts:** at task start, at End of Day, and whenever an issue is reported.
28. **Backwards stage:** not blocked — the user can correct; the change is recorded and flagged.

## Dates, checklists, notices

29. **Supervisor:** reports-to, falling back to the dispatcher.
30. **Cancelled blocker:** unblocks its dependants, with a note.
31. **Morning brief:** from the local Cowork task — no server brief. Overdue items still show on the owner dashboard
    and a tile.
32. **Reminders:** 06:00 site time, 2 days before.
33. **Daily checks** are Farm Tasks; equipment in use is checked by its operator per its SOP.
34. **No-work notice:** if the supervisor has not answered by the cutoff, ESCALATE to management. Never auto-send.
35. **Expected workers:** as proposed.
36. **Crew communication:** the crew must be able to talk to each other (a Crew record if needed). NEW: a proximity
    push-to-talk ("walkie-talkie") in Farm Ops for flaggers and operators — design note only (Apple PushToTalk,
    Multipeer/Bluetooth range, offline, safety). Do not build yet.
37. **Default start time:** 07:00; crews may override.
38. **Approvers:** Tim for now; room for more approvers by topic.
39. **Several rules on one task:** all must pass. Most rules Advisory for now.

## SOP, Library, training, uploads

40. **SOP approvers:** by the task or position the SOP covers.
41. **Library:** as designed.
42. **Training evidence:** one Training Evidence doctype.
43. **Shuffle answers:** yes.
44. **Videos before quiz:** no by default; optional per-course minimum %.
45. **Sign-off:** phone and Desk.
46. **Spanish video URL:** optional.
47. **Upload links:** NO video uploads for now (the Umbrel may be too slow).
48. **Upload visibility:** private by default; public needs an admin switch.
49. **Large uploads:** not tested now; the design keeps large files possible later.

## Business cards

50. A scanned card always files a business Contact.
51. An unmatched company: offer to create a Supplier or Customer.
52. Where met: GPS-suggested, editable.
53. Who can scan: Foreman or higher can capture and browse; crew leaders cannot.
