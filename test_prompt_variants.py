"""Measure refusal + grounding rate for the current Be Yeu prompt vs a revised
one that stops demanding an invisible 'Google Search tool' (which provokes
'I don't have that tool' refusals)."""
import logging
import re

logging.disable(logging.DEBUG)
import httpx

import discord_lifetime_memory_bot as bot_mod

URL = "https://gemini-web-to-api-production.up.railway.app/openai/v1/chat/completions"

# Current prompt, verbatim from the deployed module.
CURRENT = bot_mod.SYSTEM_PROMPT

# Revised: same persona, but no reference to a tool the model cannot see.
REVISED = CURRENT.replace(
    "2. Nếu cần thông tin cập nhật (sau mốc 03/2026, sự kiện, giá cả, thời tiết, công nghệ, tỷ số...) -> "
    "**BẮT BUỘC gọi công cụ Google Search trước khi trả lời.**",
    "2. Nếu câu hỏi cần thông tin có thể thay đổi theo thời gian (sau mốc 03/2026: sự kiện, giá cả, "
    "thời tiết, công nghệ, tỷ số...), hãy tự tra cứu và nêu rõ mốc thời gian của thông tin. "
    "Nếu không chắc chắn hoặc không tra được, nói thẳng là bé chưa chắc chắn - không được bịa.",
)

NORMAL = [
    "gioi thieu ve ban than di",
    "giai thich recursion trong code cho t",
    "an gi ngon o Ha Noi?",
    "may o cho t lam gi cho vui?",
]
LIVE = [
    "Gia Bitcoin hom nay la bao nhieu?",
    "Hom nay Ha Noi troi may do?",
    "Gia vang SJC hom nay?",
    "Tin cong nghe moi nhat hom nay la gi?",
]

REFUSAL = [
    "chỉ là một mô hình",
    "mô hình ngôn ngữ",
    "không có quyền truy cập",
    "không thể trợ giúp",
    "không được lập trình",
    "khong biet",
    "không có thông tin",
    "không thể tìm kiếm",
]
GROUNDED = ["gstatic.com/images/branding"]


def ask(system: str, user: str) -> str:
    r = httpx.post(
        URL,
        json={
            "model": "gemini-auto",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        },
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"] or ""


def classify(reply: str) -> str:
    low = reply.lower()
    if any(m in low for m in GROUNDED):
        return "grounded"
    if any(m in low for m in REFUSAL):
        return "refusal"
    return "answered"


def run(label: str, system: str, questions) -> dict:
    stats = {"grounded": 0, "refusal": 0, "answered": 0}
    print(f"\n--- {label} ---")
    for q in questions:
        try:
            reply = ask(system, q)
        except Exception as e:
            print(f"  [ERR] {q!r}: {type(e).__name__}")
            continue
        kind = classify(reply)
        stats[kind] += 1
        print(f"  [{kind:8}] {q!r} -> {reply[:95]!r}")
    print(f"  => {label}: {stats}")
    return stats


print(f"CURRENT prompt: {len(CURRENT)} chars")
print(f"REVISED prompt: {len(REVISED)} chars (changed: {CURRENT != REVISED})")

a1 = run("CURRENT / normal", CURRENT, NORMAL)
a2 = run("CURRENT / live", CURRENT, LIVE)
b1 = run("REVISED / normal", REVISED, NORMAL)
b2 = run("REVISED / live", REVISED, LIVE)

print("\n================ SUMMARY ================")
for label, s in (("CURRENT", {k: a1[k] + a2[k] for k in a1}),
                 ("REVISED", {k: b1[k] + b2[k] for k in b1})):
    total = sum(s.values()) or 1
    print(f"{label}: total={total}  refusal={s['refusal']} "
          f"({s['refusal']/total:.0%})  grounded={s['grounded']}  answered={s['answered']}")
