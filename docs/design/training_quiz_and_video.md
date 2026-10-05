# Training knowledge checks and in-app course video (D-6C Dozer Operator first)

**Status: APPROVED (decisions 42–46) — server side shipped: course videos and watched amount in v0.246.0, knowledge checks and sign-off in v0.247.0.** The phone's player, quiz screens and sign-off follow in an app release.
earlier note's v0.230.0 / 0.37.0 went to durable task photos). Quiz content comes from Tim's separate document.
Updated for the shared config lifecycle and the CCF core.

**At a glance**

- **Effort:** M — one server release + one app release.
- **Risks:** % watched is approximate (the player can't tell if anyone was looking) — labelled as such; YouTube can't be cached offline; an over-strict video minimum could block training — off by default.
- **Open questions for Tim:** One Training Evidence doctype? Shuffle answers? Require videos before the quiz (recommend no)? Sign-off on phone, Desk or both? Spanish video URL per entry?.

## 1. What the app does today

`get_training_curriculum` returns `video_url`; the app only shows a **"Has a film"** label and counts the course
as having content. No button opens it; the only web view is the PDF preview. A trainee cannot reach the film
from Farm Ops.

## 2. Quiz

- **Definition**: a **`quiz`** kind in the shared config lifecycle (Farm Config Version), keyed per Training Type
  (e.g. `quiz:d6c_dozer_operator@3`). Each attempt records the version it was taken on.
- **Content**: multiple choice, true/false, short answer; EN/ES; optional explanation per question; per course:
  pass mark (default 80%), shuffle (default yes except true/false), retakes (default yes).
- **Evidence**: one doctype, **Training Evidence**, rows of kind *Quiz attempt* or *Video view*.
  - Attempt: employee, Training Type, quiz version, answers (JSON), auto-graded score, short answers awaiting
    marking, pass/fail, phone start/finish (official), server receive time, request id, device, video-minimum
    result.
  - View: see §3.
- **Phone**: course screen lists videos and PDFs, then **Take knowledge check**; one question per screen; score
  then a review of missed questions with explanations; short answers "your trainer will mark this"; retake.
  Offline like Add Asset: downloaded by Prepare for offline and on opening the course, saved as you go, queued
  with a request id; pass/fail shown from the cached key; the server re-grades and its answer is recorded.
- **Sign-off**: passing does not complete training. A pass (short answers marked) goes to **Ready for sign-off**
  (trainer tile + Desk list). Tim reviews attempt, misses and the videos summary and signs (Signing Evidence,
  Face ID where enabled). Then the Employee Training Record is created/completed (`supervisor_reviewed_by/on/
  signature`, certificate PDF with questions, answers, score, attempts and sign-off; evidence linked).
- **MCP**: authoring through the generic config tools (`draft_config`, `preview_config`, `publish_config` with
  kind `quiz`); plus `get_quiz_results` (read) and `mark_short_answers` (write, off).
- **Off by default**: `knowledge_checks_enabled`; nothing shows until a quiz is published for a course.

## 3. Course videos

- **Data**: a **Course Video** table on Training Type — title EN/ES, URL, kind (YouTube / uploaded file),
  required/optional, section ("Core", "Vintage films"), order, optional length, optional Spanish URL. A one-time
  migration moves each `video_url` in as a required Core video; `get_training_curriculum` returns `videos` by
  section and keeps `video_url` for older builds.
- **MCP**: `add_course_video`, `update_course_video`, `remove_course_video` (writes off); YouTube links validated
  and embeddability recorded.
- **Playback**: YouTube in the official IFrame player inside a web view; if it can't embed, the app says so and
  opens it outside (YouTube app or Safari), logged "opened outside Farm Ops — not measured". Uploaded MP4s in the
  system player. YouTube is never downloaded; offline shows "Videos need a connection — watch before heading
  out". MP4s can be saved for offline.

## 4. Watched amount

- While **playing**, read `getCurrentTime()` about once a second (`getDuration()` and speed once). Each normal
  step (≈ 1 s × speed) adds a stretch; a bigger jump is a **seek** (not counted, counting restarts). Overlaps
  merge; paused, buffering, backgrounded or locked adds nothing. At 1.5×/2× the covered part counts; time spent
  is separate. Same counting for MP4.
- Each view stores merged stretches, **% watched** (unique seconds ÷ length), total play seconds, seeks,
  furthest point, length, start/end, device, `measured` (*approximate* / *not measured*).
- Totals combine one person's views of a video across phones.
- Trainer sees "Watched 87% (11:20 of 13:02) · 2 views · 3 skips · reached 12:58" and the footer *"Approximate:
  counted by the app's player while the video was playing on screen. It can't tell whether anyone was looking."*
- **Optional gate**: per-course minimum % for required videos (off by default; per-video override, e.g. 90%).
  When on, the quiz unlocks once met; the phone checks its own counts offline, the server re-checks across
  devices and records the result; an attempt below the minimum is kept and marked *video minimum not met*. The
  trainer can override at sign-off with a logged reason. Expressed as a CCF `person` condition
  (`video.watched_pct`), so it can also gate tasks.

## 5. Open decisions (from the earlier notes)

1. One Training Evidence doctype (recommended) or two tables. 2. Shuffle choices (yes except true/false).
3. Require videos opened before the quiz (recommend no; optional gate above). 4. Sign-off on phone, Desk or
both (recommend both). 6. Optional Spanish video URL per entry (recommend yes).

## 6. Effort

One server release + one app release.
