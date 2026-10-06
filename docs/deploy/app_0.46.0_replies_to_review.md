**Deploy: FarmOps 0.46.0 (build 41) — office@ replies reviewed on the phone (server v0.258.0)**

App release. Ship instead of 0.45.0 (it contains it). The tile appears with server **v0.258.0** and office@ on. fafo_ios
main, scheme **FarmOps**.

**1. Build (Tim)**

Archive and ship FarmOps 0.46.0 (build 41). Verified here: `xcodebuild build -scheme FarmOps` succeeds; FarmOpsKit
tests pass on the simulator (2,939), including the Face ID message pinned to the server's bytes.

**2. Phone checks**

1. Today → **Replies to review** → a reply: their message, the draft (editable), files to attach, Approve and send /
   Discard.
2. With device keys on: Approve and send asks for Face ID; cancelling Face ID sends nothing.
3. Spanish: the screen reads in tú ("Tu respuesta", "Aprobar y enviar").

**3. Rollback**

Ship 0.45.0.
