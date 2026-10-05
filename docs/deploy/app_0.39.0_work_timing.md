**Deploy: FarmOps 0.39.0 (build 34) — Go / Hold, crop stage, suggested work, notices, SOP review (server v0.250.0)**

App release. Needs server **v0.250.0** (the mobile routes); deploy that first. Ship instead of 0.38.8 (it contains it).

**1. Build (Tim)**

Archive and ship FarmOps 0.39.0 (build 34) from fafo_ios main, scheme **FarmOps**. Verified here: `xcodebuild build
-scheme FarmOps` succeeds; FarmOpsKit tests pass on the simulator; the Spanish table lints (`plutil -lint`).

**2. Phone checks** (with a Work Timing rule approved on the server, v0.240.0)

1. A task on Hold shows a red **Hold** chip on its row; the task screen says why. "Go — check stage" shows in amber.
2. As a Foreman: **Let it start today** on a held task asks for a reason, then the row reads "Hold — overridden today".
3. Starting a task on a block whose stage is stale opens the **stage picker**: principal stage by name, then the
   second digit; "Same as last time" when the farm knows it. Save with no signal: it goes to Settings → pending and
   sends later. A stage earlier than the last says "check which is right".
4. Today: **Work notices** (notices sent to you, in your language), **No-work notices to decide** (supervisors:
   send / send with other work / no notice / resume), **Good to do today** (window-closing first) and **SOPs to
   review** (approvers: read it, approve, or request changes).
5. A task whose type an approved SOP covers shows that SOP in "How this job is done".
6. Spanish reads in tú.

**3. Not in this release**

The course player (videos with watched amount, the knowledge check and sign-off on the phone) and End-of-Day stage
prompts follow in 0.40.0.

**4. Rollback**

Ship 0.38.8. Stages already sent stay on the server.
