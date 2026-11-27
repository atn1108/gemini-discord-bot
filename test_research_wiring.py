"""Pre-deploy checks for the bot: import, helpers, payload shape."""
import asyncio
import logging
import os

# Match Railway production config before the module reads its env.
os.environ["TRIGGER_KEYWORDS"] = "bé yêu"

logging.disable(logging.DEBUG)

import discord_lifetime_memory_bot as bot_mod

failures = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        failures.append(name)


# 1. Import already succeeded if we got here; slash commands must be registered.
# Before login py-cord keeps them in pending_application_commands.
pending = bot_mod.bot.pending_application_commands
cmds = {c.name for c in pending}
check("import + decorators", True, f"pending={sorted(cmds)}")
check("/research registered", "research" in cmds)
check("/ask registered", "ask" in cmds)
check("/forget registered", "forget" in cmds)
check("/memory registered", "memory" in cmds)
check("KEYWORDS from env", bot_mod.KEYWORDS == ["bé yêu"], str(bot_mod.KEYWORDS))

# 2. System prompt wired in
check("SYSTEM_PROMPT non-empty", len(bot_mod.SYSTEM_PROMPT) > 200, f"{len(bot_mod.SYSTEM_PROMPT)} chars")
check("SYSTEM_PROMPT is persona", "Bé Yêu" in bot_mod.SYSTEM_PROMPT)

# 3. research_query()
cases = [
    ("nghiên cứu thị trường crypto", "thị trường crypto"),
    ("research quantum computing", "quantum computing"),
    ("deep research pin lithium", "pin lithium"),
    ("nghien cuu gia vang hom nay", "gia vang hom nay"),
    ("bé yêu nghiên cứu đối thủ tiktok", "đối thủ tiktok"),   # leading keyword stripped
    ("bé yêu research rust vs go", "rust vs go"),
]
for raw, want in cases:
    got = bot_mod.research_query(raw)
    check(f"research_query({raw!r})", got == want, f"got={got!r} want={want!r}")

# Keyword stripping must not loop forever or eat the topic
multi = [
    ("bé yêu deep research tokamak", "tokamak"),
    ("BÉ YÊU NGHIÊN CỨU pin lithium", "pin lithium"),
    ("bé yêu   research   spaced topic  ", "spaced topic"),
]
for raw, want in multi:
    got = bot_mod.research_query(raw)
    check(f"stripping({raw!r})", got == want, f"got={got!r} want={want!r}")

# Must NOT trigger normal chat
for raw in ("xin chào", "bot hỏi đáp đi", "nghiên cứu", "ai là ai"):
    got = bot_mod.research_query(raw)
    check(f"no-trigger({raw!r})", got is None, f"got={got!r}")

# 4. chunk_text()
long = ("đoạn " * 5000).strip()
chunks = bot_mod.chunk_text(long, 1900)
check("chunk_text joins back", "".join(chunks) == long, f"{len(chunks)} chunks")
check("chunk_text under limit", all(len(c) <= 1900 for c in chunks),
      f"max={max(len(c) for c in chunks)}")
check("chunk_text short passthrough", bot_mod.chunk_text("ngắn", 1900) == ["ngắn"])

# 5. format_research() — completed
done = {
    "status": "completed",
    "query": "test",
    "summary": "# Tóm tắt\n\nNội dung báo cáo " + "rất dài. " * 800,
    "sources": [
        {"title": "Nguồn A", "url": "https://a.example"},
        {"title": "Nguồn B", "url": "https://b.example"},
    ],
    "duration_ms": 88013,
}
text, md = bot_mod.format_research(done, "test")
check("format completed -> message", "🔎" in text and "88s" in text and "2 nguồn" in text, text[:80].replace("\n", " "))
check("format completed -> message <=2000", len(text) <= 2000, f"{len(text)} chars")
check("format completed -> md has sources", "## Sources" in md and "https://a.example" in md, f"{len(md)} chars")
check("format completed -> md full length", len(md) > len(text), f"md={len(md)} vs msg={len(text)}")

# 6. format_research() — failure
fail_text, fail_md = bot_mod.format_research({"status": "failed", "error": "quota exceeded"}, "x")
check("format failed -> error msg", "quota exceeded" in fail_text and fail_md == "")

# 7. Webhook payload carries the system prompt (persona + remembered facts)
import inspect
src = inspect.getsource(bot_mod.call_webhook)
check("call_webhook sends system", "system_prompt = await build_system_prompt()" in src and '"system": system_prompt' in src)
check("call_webhook builds prompt", "build_system_prompt" in src)

# 8. Timeout guards
check("RESEARCH_TIMEOUT bounded", 0 < bot_mod.RESEARCH_TIMEOUT <= 900, f"{bot_mod.RESEARCH_TIMEOUT}s")
check("httpx timeout in call_webhook", "timeout=120" in inspect.getsource(bot_mod.call_webhook))

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
