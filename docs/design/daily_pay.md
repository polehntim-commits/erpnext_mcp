# Daily pay with a weekly overtime true-up — design for Tim's approval

**Status: DRAFT, 2026-10-05. Nothing is built.** Queued as "daily pay: design only" (2026-10-03). This note
proposes the shape, names what it reuses, and lists the decisions only Tim can make (§9). Payroll stays
human: every run, file and payment is started and approved by a person, exactly as today.

## 1. What it is

A worker who opts in is paid **each working day** for that day — hours × rate and piece units × piece rate,
minimum-wage makeup, taxes withheld — and, once the **workweek** closes, a **settlement run** pays what only a
week can decide: the overtime premium, the week's deductions and garnishments, and any correction. Workers
who do not opt in stay on the weekly (or current) schedule; nothing changes for them.

## 2. Two ways to do it, and the recommendation

| | A. Daily pay periods (recommended) | B. Weekly payroll with daily advances |
|---|---|---|
| Payroll period of record | each day (IRS "daily or miscellaneous", 260/yr) | the week |
| Taxes | withheld on each daily payment (Pub 15-T daily table; Oregon daily) | withheld on the weekly run; the daily draw is an **advance of net wages** |
| Legal shape | ordinary wages, paid daily | earned-wage-access; Oregon treats wage advances as loans/assignments unless structured carefully |
| Overtime | weekly premium paid in the settlement run (§4) | computed in the weekly run as today |
| Effort here | moderate: a period type, a run per day, the settlement run | larger: an advance ledger, recovery against the weekly net, negative-net handling |

**Recommendation: A.** The engine already withholds for a `Daily` period (`withholding.PERIODS_PER_YEAR`), the
payroll run, pay stubs, GL posting and NACHA file all work per period, and a daily wage payment is plain wages —
no advance to recover, no loan rules.

## 3. The daily run

- **What it pays**: for each opted-in worker with **reviewed** punches that day (v0.251.0 punch review — an
  unreviewed punch waits and is paid on the next daily run after review): straight-time hours × rate, piece units
  × piece rate (bucket / bin entries, as today), break pay at the average piece hourly, and the **daily**
  minimum-wage makeup (§6). No overtime premium (§4). No deductions other than taxes (§5).
- **When**: a person starts it from the Desk or the phone ("Run today's pay", HR / Farm Manager), after the day's
  punches are reviewed. It is a `run_payroll_for_period` with `pay_frequency: Daily`, the day as start and end,
  and only the opted-in employees. Same calculate → review → submit → post to GL → NACHA path as every run.
- **Paying it**: the NACHA file for the day's net, uploaded by a person to the bank (same-day ACH where the bank
  offers it, else next-day). Workers without verified direct deposit (v0.225.0) get a check or stay weekly.

## 4. The weekly overtime true-up (the settlement run)

- The workweek is fixed (e.g. Sunday–Saturday, a setting). When it closes, the settlement run computes, per
  worker, total hours and total straight-time earnings for the week (hourly + piece + break pay + makeup).
- **Overtime hours** = hours over `overtime_rule.weekly_threshold_hours` (the v0.235.0 payroll setting).
- **Regular rate** (FLSA, 29 CFR 778.111 for piece workers) = the week's straight-time earnings ÷ total hours.
- **Premium owed** = overtime hours × regular rate × (multiplier − 1). The daily runs already paid straight time
  for every hour, so only the half-time premium is owed — the same arithmetic `payroll_calc` uses today for
  piece workers.
- Paid on the settlement run's payday — the regular payday for the period in which the workweek ends (FLSA
  allows the premium to follow "as soon as practicable" once hours are known). Withholding on the premium uses
  the **weekly** period for that run (it is a separate payment of a weekly figure).

## 5. Deductions, garnishments, benefits

Taken in the **settlement run**, against the week's disposable earnings: child support and other garnishments
(the CCPA protected floor is defined per week — `payroll_deductions.CCPA_PERIOD_MULTIPLIERS` has no daily
period, and computing it per day would protect less than the law intends), housing, advances, union/benefit
deductions. Daily runs withhold taxes only. A deduction larger than the settlement net carries to the next
week; it never makes a daily payment negative.

## 6. Minimum wage

Oregon measures minimum wage per pay period. With daily periods, the piece-rate makeup is computed **per day**
(units × rate vs. hours × minimum, with the overtime-aware floor already in `payroll_calc`). A slow piece day
can no longer be averaged against a good one — daily pay costs the farm more makeup than weekly pay when piece
rates are tight. The weekly settlement does **not** claw makeup back.

## 7. Data — no new tables

- Employee: `daily_pay` (Check) and `daily_pay_since` (Date) — the opt-in and when it started.
- Payroll Entry: `pay_frequency` gains **Daily**; `run_kind` (Daily / Settlement / Regular) so reports and the
  payroll calendar can tell them apart.
- Settings: `daily_pay_enabled` (off), `workweek_start` (Sunday), `daily_pay_cutoff` (the hour after which
  today's punches are paid tomorrow).
- Everything else (slips, stubs, GL, NACHA, tax forms) is the existing per-period path.

## 8. Phone and Desk

- Worker: "Pay me daily" in My records (with the plain-language terms, signed like the direct-deposit change),
  today's pay on the pay-stub list, and the week's overtime on the settlement stub.
- Supervisor / HR: a **Today's pay** tile — who is due, whose punches still need review, the total — and the
  "Run today's pay" action; the settlement run appears on the payroll calendar on the workweek's payday.
- Windows (v0.224.0): the payroll window already opens the run tools; nothing new is switched on by default.

## 9. Decisions for Tim

1. **A or B** (§2). Recommendation A.
2. **Who may opt in**: everyone, or only seasonal / piece-rate workers? Can a worker switch back mid-season
   (recommend: at a workweek boundary only)?
3. **Workweek**: Sunday–Saturday?
4. **Cutoff**: what hour closes "today" for pay (recommend 18:00; later punches go on tomorrow's run)?
5. **Bank**: does the farm's bank offer same-day ACH, and its cutoff? Otherwise daily pay lands next business day.
6. **Overtime threshold**: the payroll setting is 40 h/week at 1.5×. Oregon's statutory agricultural threshold
   for 2025–26 is 48 h (40 h from 2027 — Oregon HB 4002, 2022). 40 h is lawful (more generous);
   confirm it is intended, or set 48 until 2027 — it is a payroll setting, previewed before it changes.
7. **Workers without direct deposit**: weekly only, or a daily check?
8. **Cost note**: daily minimum-wage makeup (§6) and per-day ACH fees — acceptable?

## 10. Build plan once approved (small releases, each with a deploy file)

1. Settings, Employee opt-in fields, `Daily` frequency and `run_kind` on Payroll Entry; daily withholding tested
   against Pub 15-T and Oregon tables.
2. The daily run (reviewed punches only) and its pay stub; NACHA for the day.
3. The settlement run: weekly premium on the regular rate, deductions and garnishments, carry-forward.
4. Phone: opt-in, today's pay, the tile and the run action; payroll calendar entries.
5. A parallel-run proof before switching it on: a past week computed both ways, with the per-worker difference
   explained to the cent (the same discipline as the v0.235.0 overtime-setting move).
