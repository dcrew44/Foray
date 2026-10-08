"""Does the Python accumulator reproduce (a) the TS server's persisted onEnd message and
(b) the message the real Chat client built, for the same streams?"""
import json, copy
from pathlib import Path
from uistream import UIMessageAccumulator, parse_sse
import server_stdlib

OUT = Path(__file__).resolve().parent.parent / "out"

def approve(msg, approved=True):
    m = copy.deepcopy(msg)
    for p in m["parts"]:
        if p.get("state") == "approval-requested":
            p["state"] = "approval-responded"; p["approval"]["approved"] = approved
    return m

def fold(sse, last=None, mid=""):
    acc = UIMessageAccumulator(mid, last)
    for c in parse_sse(sse):
        acc.apply(c)
    return acc.message

# (a) vs the TS server's own processUIMessageStream (onEnd responseMessage)
t1 = fold((OUT / "ts-turn1.sse").read_text())
print("TS turn1 persisted == accumulator:", t1 == json.loads((OUT / "ts-turn1.persisted.json").read_text()))
t2 = fold((OUT / "ts-turn2.sse").read_text(), last=approve(t1))
print("TS turn2 persisted == accumulator:", t2 == json.loads((OUT / "ts-turn2.persisted.json").read_text()))

# (b) vs the real Chat client fed by our canned emitter (main:approve)
user = {"id": "u", "role": "user", "parts": [{"type": "text", "text": "x"}]}
s1 = "".join(server_stdlib.scenario_main({"message": user}))
m1 = fold(s1)
s2 = "".join(server_stdlib.scenario_main({"message": approve(m1)}))
m2 = fold(s2, last=approve(m1))
txt = (OUT / "client-main-approve.json").read_text()
client = json.loads(txt[txt.index("\n{") + 1:])["messages"][-1]
print("client final (canned main:approve) == accumulator:", m2 == client)
if m2 != client:
    for a, b in zip(m2["parts"], client["parts"]):
        if a != b: print("  diff", a, "\n   vs ", b)
