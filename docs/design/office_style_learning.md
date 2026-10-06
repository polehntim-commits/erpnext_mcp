# office@ replies — style learning (Phase 4): design for Tim's approval

**Status: DRAFT, 2026-10-05. Nothing is built.** `office_reply_drafts.md` names Phase 4 as "style learning beyond
examples + notes" and §12 decision 2 keeps drafting in mode A (the Claude client drafts; no server-side provider or
API key on the box). This note proposes how drafts learn the office's voice **inside that decision** — from what
people already do when they correct a draft — and lists what only Tim can decide (§5).

## 1. What the system already has

Every sent reply stores `draft_text`, `sent_text`, a unified diff and `edit_ratio` (v0.222.0). The next draft for the
same company and class gets the five most recent sent replies as examples (edited ones first) and the company's
`mail_style_notes`. What it does not do: notice that people make **the same correction** again and again, choose
examples for **relevance** rather than recency, or show whether drafts are **getting better**.

## 2. Proposal (no new provider, no new table)

1. **Recurring corrections → suggested style notes.** A nightly job reads the diffs of the last 60 days per company
   and class and finds edits people repeat: a sign-off replaced ("Best regards" → "Thanks, the office"), a phrase
   deleted every time ("I hope this email finds you well"), greetings, length (drafts cut by a third), language
   (Spanish requested when they wrote in Spanish). Each pattern seen in at least 3 replies by at least 2 days becomes
   a **suggested** line for `mail_style_notes`, with its evidence (which replies, before/after).
2. **A person accepts or rejects each suggestion** — in the Desk (and on the phone's Replies to review screen as a
   small "Suggested style" card for Accounts / Farm Manager). Accepted lines are added to the company's style notes
   through the existing config lifecycle (draft → publish by a person). Nothing changes the notes by itself.
3. **Better examples.** The drafter's examples prefer, in order: replies to the same sender, to the same linked
   record type, then the same class — still edited-first, still never another company's.
4. **"Are drafts getting better?"** `get_mail_draft_quality`: per class and month, the median `edit_ratio`, how many
   were sent unchanged, discarded, or rewritten (> 60% changed). A Desk report; no phone tile.

## 3. What it never does

Send anything; change the style notes without a person; read mail of another company for examples; send mail text
to any service (the drafting client is the only reader, as today); learn from a discarded draft's text (only that it
was discarded).

## 4. Effort

One release server-side (the job, `suggest_style_notes` / `get_mail_draft_quality` reads, accept/reject writes off by
default) and a small app card. Tests pin: no send path, nothing applied without a person, company isolation.

## 5. Decisions for Tim

1. Build §2 (recommended), or keep Phase 4 at examples + notes only.
2. Thresholds: a pattern counts after **3 replies on 2+ days**?
3. Who accepts a suggested style line: Farm Manager and Accounts Manager (recommended), or Tim only?
4. The phone card (§2.2) — yes, or Desk only?
5. Server-side drafting (provider B) stays **out**, as decision 2 has it — confirm.
