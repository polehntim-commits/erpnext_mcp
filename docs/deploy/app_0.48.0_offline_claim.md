**Deploy: FarmOps 0.48.0 (build 43) — claim a task with no signal (server v0.261.0)**

App release. It replaces 0.47.0 and includes it. fafo_ios main, scheme **FarmOps**. Deploy server v0.261.0 first
(`v0.261.0_offline_claim.md`, which also has the two-phone check).

**1. Build (Tim)**

Archive and ship FarmOps 0.48.0 (build 43). Verified here: `xcodebuild build -scheme FarmOps` succeeds, and the
FarmOpsKit tests pass on the simulator.

**2. What changed on the phone**

- **Claim works with no signal.** Tapping Claim on a task from the saved pool queues the claim with the tap time.
  The task moves to My Tasks, and its screen says "Claimed on phone — will confirm when synced". Start, pause,
  resume and complete then work offline as before.
- **The claim is sent first.** The queue sends it ahead of that task's start, and the completion waits for it.
- **Losing the race is explained.** If someone else's claim reached the farm first, the task screen says so and
  names who holds the task: "Your time and photos are kept — you're on this task as a second worker." Tap OK to
  dismiss it.
- **Spanish (tú)** for all of the above.
- **Sign-out** clears these notices along with the queue, so nothing of one worker's stays on a shared phone.

**3. Rollback**

FarmOps 0.47.0. Let the phones sync before downgrading, so no queued claim is waiting.
