"""Verify the memory fix: forwarder returns the full answer, and the bot
persists BOTH sides of the exchange (previously only error paths saved)."""
import asyncio
import inspect
import logging
import os

os.environ.setdefault("TRIGGER_KEYWORDS", "be yeu")
logging.disable(logging.DEBUG)

import httpx

import discord_lifetime_memory_bot as bot_mod

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        failures.append(name)


# ---------- 1. forwarder returns the full answer ----------
fwd_path = os.path.join(os.path.dirname(__file__), "..", "gemini-discord-forwarder", "discord_forwarder.py")
fwd_src = open(fwd_path, encoding="utf-8").read()
check('forwarder returns "answer"', '"answer": answer' in fwd_src)
check('forwarder keeps answer_preview', '"answer_preview": answer[:100]' in fwd_src)

# Live call proves the field is really there (also posts one test message to Discord).
resp = httpx.post(
    "https://gemini-discord-forwarder-production.up.railway.app/webhook/gemini-to-discord",
    json={
        "messages": [{"role": "user", "content": "Reply with exactly: MEM-OK"}],
        "system": "Always reply with exactly the requested token.",
        "username": "memory-test",
    },
    timeout=120,
)
resp.raise_for_status()
data = resp.json()
check("forwarder 200", data.get("status") == "accepted", str(data.get("status")))
check("forwarder response has answer", "answer" in data, f"keys={list(data)}")
answer = data.get("answer") or ""
check("answer non-empty", len(answer) > 0, f"{len(answer)} chars")
check("answer_preview still short", len(data.get("answer_preview", "")) <= 100)

# ---------- 2. bot persists both sides ----------
saved: dict = {}


async def fake_save(channel_id, messages):
    saved[channel_id] = list(messages)


bot_mod.save_history = fake_save  # persist_turn resolves this at call time

# 2a. successful turn -> user + assistant both stored
hist = [{"role": "user", "content": "[tester]: hi"}]
asyncio.run(bot_mod.persist_turn(111, hist, "Xin chào mọi người!"))
stored = saved.get(111, [])
check("success persists user turn", stored[0]["role"] == "user" if stored else False, str(stored[:1]))
check("success persists assistant turn",
      len(stored) == 2 and stored[1]["role"] == "assistant", str([m["role"] for m in stored]))
check("assistant content kept", stored[1]["content"] == "Xin chào mọi người!" if len(stored) == 2 else False)

# 2b. long answer is capped at STORE_MAX
saved.clear()
long_answer = "á" * (bot_mod.STORE_MAX + 500)
asyncio.run(bot_mod.persist_turn(222, [], long_answer))
got = saved[222][-1]["content"]
check(f"answer capped at STORE_MAX={bot_mod.STORE_MAX}", len(got) == bot_mod.STORE_MAX, f"got={len(got)}")

# 2c. empty answer -> still saves (so the user turn is not lost), no empty assistant row
saved.clear()
asyncio.run(bot_mod.persist_turn(333, [{"role": "user", "content": "q"}], "   "))
check("empty answer still saves user turn", saved.get(333) == [{"role": "user", "content": "q"}],
      str(saved.get(333)))

# 2d. on_message / ask must actually call persist_turn on success
bot_src = open(os.path.join(os.path.dirname(__file__), "discord_lifetime_memory_bot.py"), encoding="utf-8").read()
check("on_message calls persist_turn", "await persist_turn(channel_id, messages," in bot_src)
check("ask calls persist_turn", "await persist_turn(ctx.channel.id, history," in bot_src)
# save_history now runs only from persist_turn and /ask's error path: a failed
# call stores nothing, because there is nothing to undo.
check("no bare save on success path",
      bot_src.count("await save_history(") == 2,
      f"count={bot_src.count('await save_history(')}")

# 2e. history round-trips through JSON (what actually lands in Postgres JSONB)
import json
blob = json.dumps(stored)
check("history is JSON-serialisable", json.loads(blob) == stored, f"{len(blob)} bytes")

print()
if failures:
    print(f"FAILED: {failures}")
    raise SystemExit(1)
print("ALL MEMORY CHECKS PASSED")
