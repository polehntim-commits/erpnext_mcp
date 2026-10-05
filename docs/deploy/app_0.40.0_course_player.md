**Deploy: FarmOps 0.40.0 (build 35) — the course player: videos, knowledge check, sign-off (server v0.250.0 or later)**

App release. Needs server **v0.250.0** (routes) — with v0.246.0 / v0.247.0 for course videos and quizzes. Ship instead
of 0.39.0 (it contains it). fafo_ios main **`2569e93`**, scheme **FarmOps**.

**1. Build (Tim)**

Archive and ship FarmOps 0.40.0 (build 35). Verified here: `xcodebuild build -scheme FarmOps` succeeds; FarmOpsKit
tests pass on the simulator (2,929); the Spanish table lints.

**2. Phone checks** (a course with videos, and a published quiz with Knowledge Checks on)

1. Training → Courses → the D-6C course: a Videos section by section ("Core", "Vintage films"), each with "Watched …"
   once played, and the "approximate" footer.
2. Play a YouTube video for a minute, skip ahead, close: the line updates (minutes covered, 1 skip). A video that
   cannot embed offers "Open it outside Farm Ops" and is recorded "not measured".
3. **Take the knowledge check**: one question per screen; choices shuffle (true / false never); a short answer says
   the trainer will mark it. The score and the missed questions with explanations show at the end — also with no
   signal (the attempt queues and sends later).
4. As HR / Farm Manager: Training → **Training sign-off** lists passed attempts; **Sign off** files the training record
   (asks for a reason when the video minimum was not met).
5. Spanish reads in tú.

**3. Rollback**

Ship 0.39.0. Views and attempts already sent stay on the server.
