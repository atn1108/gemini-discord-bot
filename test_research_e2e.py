"""E2E: call the real deep-research endpoint exactly as the bot does."""
import asyncio
import logging
import os
import time

os.environ["TRIGGER_KEYWORDS"] = "con vợ siêu múp, bot, ê gemini, ai"
logging.disable(logging.DEBUG)

import discord_lifetime_memory_bot as bot_mod


async def main():
    query = "pin lithium-ion ưu nhược điểm"
    print(f"URL  : {bot_mod.DEEP_RESEARCH_URL}")
    print(f"limit: {bot_mod.RESEARCH_TIMEOUT}s, default_sources={bot_mod.RESEARCH_DEFAULT_SOURCES}")

    t0 = time.time()
    try:
        data = await bot_mod.run_deep_research(query, max_sources=3)
    except Exception as e:
        print(f"FAIL run_deep_research: {type(e).__name__}: {e}")
        return 1
    elapsed = time.time() - t0

    print(f"elapsed: {elapsed:.1f}s")
    print(f"status : {data.get('status')}")
    print(f"model  : {data.get('model')}")
    print(f"sources: {len(data.get('sources') or [])}")

    text, md = bot_mod.format_research(data, query)
    print(f"msg    : {len(text)} chars")
    print(f"md     : {len(md)} chars")
    print(f"msg<=2000: {len(text) <= 2000}")
    print(f"md has Sources: {'## Sources' in md}")

    if data.get("status") != "completed":
        print(f"FAIL status={data.get('status')} error={data.get('error')}")
        return 1
    if len(text) > 2000:
        print("FAIL message exceeds Discord limit")
        return 1
    if "## Sources" not in md:
        print("FAIL report missing sources section")
        return 1

    print("\n--- first 500 chars of Discord message ---")
    print(text[:500])
    print("\nE2E PASS")
    return 0


raise SystemExit(asyncio.run(main()))
