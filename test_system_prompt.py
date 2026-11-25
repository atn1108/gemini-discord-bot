"""Send the real Be Yeu system prompt to web-to-api and confirm the persona lands."""
import json
import logging
import os

logging.disable(logging.DEBUG)
os.environ.setdefault("TRIGGER_KEYWORDS", "be yeu")

import httpx

import discord_lifetime_memory_bot as bot_mod

URL = "https://gemini-web-to-api-production.up.railway.app/openai/v1/chat/completions"

CASES = [
    ("alo", ["em", "bé", "mọi người"]),
    ("mấy giờ rồi", ["em", "bé", "mọi người"]),
    ("hôm nay trời như nào", ["em", "bé", "mọi người"]),
]

failures = []
for user_msg, markers in CASES:
    payload = {
        "model": "gemini-auto",
        "messages": [
            {"role": "system", "content": bot_mod.SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
    }
    try:
        r = httpx.post(URL, json=payload, timeout=90)
        r.raise_for_status()
        reply = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[FAIL] {user_msg!r} -> {type(e).__name__}: {e}")
        failures.append(user_msg)
        continue

    hit = any(m in reply.lower() for m in markers)
    print(f"[{'PASS' if hit else 'FAIL'}] {user_msg!r} -> {reply[:160]!r}")
    if not hit:
        failures.append(user_msg)

print()
if failures:
    print(f"persona not detected in: {failures}")
    raise SystemExit(1)
print("PERSONA CONFIRMED (system prompt reaches the model)")
