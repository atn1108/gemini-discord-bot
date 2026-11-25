import discord
import httpx
import os
import logging
import asyncpg
import json
import io
import re
import asyncio
from collections import defaultdict, deque
from typing import List, Dict, Optional
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== CONFIG (Railway Variables) =====
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://gemini-discord-forwarder-production.up.railway.app/webhook/gemini-to-discord")
KEYWORDS = [k.strip().lower() for k in os.getenv("TRIGGER_KEYWORDS", "gemini,ai,ask,hey bot").split(",")]
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "50"))
# Cap a single stored turn so a long answer cannot blow up the JSONB row
# or the context window we resend on every call.
STORE_MAX = int(os.getenv("STORE_MAX", "4000"))
# Plain chatter kept between answered turns, so tagging the bot mid-argument
# lets it see the conversation instead of asking for context again.
PENDING_MAX = int(os.getenv("PENDING_MAX", "40"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "Gemini Bot")
BOT_AVATAR = os.getenv("BOT_AVATAR", "")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# ===== DEEP RESEARCH =====
DEEP_RESEARCH_URL = os.getenv(
    "DEEP_RESEARCH_URL",
    "https://gemini-web-to-api-production.up.railway.app/gemini/v1beta/deepresearch",
)
RESEARCH_TIMEOUT = int(os.getenv("RESEARCH_TIMEOUT", "300"))  # seconds, never hang
RESEARCH_DEFAULT_SOURCES = int(os.getenv("RESEARCH_DEFAULT_SOURCES", "10"))
# A message counts as a research request when it starts with one of these
# (optionally after a trigger keyword, e.g. "bot nghiên cứu thị trường crypto").
RESEARCH_PREFIXES = ("deep research ", "deepresearch ", "nghiên cứu ", "nghien cuu ", "research ")
# Longest first — see _match_prefix.
REMEMBER_PREFIXES = ("remember that ", "remember to ", "remember:",
                     "nhớ rằng ", "nhớ là ", "nhớ giúp ", "nhớ cho ", "nhớ ")
FORGET_PREFIXES = ("forget ", "quên ")

# ===== SYSTEM PROMPT ("Bé Yêu") =====
# Override with the SYSTEM_PROMPT env var if you want to change persona without a redeploy.
DEFAULT_SYSTEM_PROMPT = """# Identity
Bạn là "Bé Yêu", trợ lý AI riêng của một nhóm bạn thân trên Discord.
Xưng "em" hoặc "bé", gọi các thành viên theo tên hiển thị (nếu có) hoặc "mọi người". Giọng điệu tự nhiên, lém lỉnh, nhiệt tình như thành viên trong nhóm chat, không sến súa hay giáo điều.

# Operating Loop (Luồng xử lý bắt buộc)
1. Nhận yêu cầu -> Xác định loại tác vụ (Tin tức/Thời sự | Hỏi đáp nhanh | Code/Học tập | Tán gẫu).
2. Nếu cần thông tin cập nhật (sau mốc 03/2026, sự kiện, giá cả, thời tiết, công nghệ, tỷ số...) -> **BẮT BUỘC gọi công cụ Google Search trước khi trả lời.**
3. Trả lời thẳng vào trọng tâm ngay từ câu đầu tiên. Cắt bỏ mọi câu rào đón ("Dưới đây là...", "Chào bạn...", "Theo thông tin tìm được...").

# Anti-patterns (Tuyệt đối tránh)
- Không bịa đặt hoặc đoán mò khi không có dữ liệu. Nếu tìm không thấy, báo thẳng "bé chưa tìm ra thông tin chính xác".
- Không dùng bảng (Markdown Table) vì vỡ khung hiển thị trên Discord điện thoại; thay bằng danh sách gạch đầu dòng phân cấp.
- Không trả lời dài dòng cho các câu hỏi đơn giản.

# Format Rules (Chuẩn Discord)
- Dùng **in đậm** cho ý chính, gạch đầu dòng (`-`) cho các mục.
- Code đặt trong block ```ngôn_ngữ ... ```.
- Tối ưu độ dài: câu hỏi thường trả lời trong 1–4 câu; hướng dẫn kỹ thuật hoặc tổng hợp tin tức thì chia ý rõ ràng, đủ bước."""
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)

# ===== DATABASE =====
pool: asyncpg.Pool = None

# Chatter seen since the last answered turn: kept in memory and merged into the
# next prompt, so tagging the bot mid-conversation carries the surrounding
# context instead of making everyone re-explain it.
pending: Dict[int, deque] = defaultdict(lambda: deque(maxlen=PENDING_MAX))
# Serialises answered turns per channel: two people tagging at once must not
# read the same history and overwrite each other's stored answer.
channel_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                channel_id BIGINT PRIMARY KEY,
                messages JSONB NOT NULL DEFAULT '[]',
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_updated 
            ON conversations(updated_at DESC);
        """)
        # Facts the group explicitly asked the bot to remember (names, nicknames).
        # Global on purpose: a name is valid in every channel, and /forget only
        # clears conversation history.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id SERIAL PRIMARY KEY,
                fact TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
    logger.info("Database initialized")

def decode_messages(raw) -> List[Dict]:
    """Parse the JSONB column into a list of turns.

    asyncpg's default json/jsonb codec decodes to str, not to a parsed object,
    so a stored row arrives as text (and a double-encoded row as text twice).
    Tolerate every shape instead of blowing up on history.append.
    """
    for _ in range(2):
        if not isinstance(raw, str):
            break
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    return [m for m in raw if isinstance(m, dict) and "role" in m]

async def get_history(channel_id: int) -> List[Dict]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT messages FROM conversations WHERE channel_id = $1", channel_id
        )
        return decode_messages(row["messages"]) if row else []

async def save_history(channel_id: int, messages: List[Dict]):
    trimmed = messages[-(MAX_HISTORY * 2):]
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO conversations (channel_id, messages, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (channel_id) DO UPDATE SET
                messages = EXCLUDED.messages,
                updated_at = EXCLUDED.updated_at
        """, channel_id, json.dumps(trimmed))

async def clear_history(channel_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM conversations WHERE channel_id = $1", channel_id)

# ===== REMEMBERED FACTS =====
FACT_MAX = 500        # one fact, capped so a pasted wall of text cannot bloat the prompt
FACT_LIMIT = 50       # how many facts ride along with every request

async def add_fact(fact: str, author: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO facts (fact, author) VALUES ($1, $2)",
            fact[:FACT_MAX], author[:100],
        )

async def get_facts() -> List[Dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT fact, author FROM facts ORDER BY created_at DESC LIMIT $1",
            FACT_LIMIT,
        )
    # Oldest first, so the prompt reads in the order things were said.
    return [{"fact": r["fact"], "author": r["author"]} for r in reversed(rows)]

async def delete_facts(pattern: str) -> int:
    """Delete facts containing `pattern`; an empty pattern deletes every fact.

    Returns how many rows went away.
    """
    async with pool.acquire() as conn:
        if pattern:
            status = await conn.execute(
                "DELETE FROM facts WHERE fact ILIKE $1", f"%{pattern}%")
        else:
            status = await conn.execute("DELETE FROM facts")
    # asyncpg returns e.g. "DELETE 3".
    parts = status.split()
    return int(parts[-1]) if len(parts) == 2 and parts[-1].isdigit() else 0

async def build_system_prompt() -> str:
    """The persona prompt, with the remembered facts appended when there are any."""
    facts = await get_facts()
    if not facts:
        return SYSTEM_PROMPT
    lines = "\n".join(
        f"- {f['fact']} (do {f['author']} nhờ nhớ)" if f["author"] else f"- {f['fact']}"
        for f in facts
    )
    return (
        SYSTEM_PROMPT
        + "\n\n# Facts the group asked you to remember\n"
        + "Use them when relevant; do not invent any others.\n"
        + lines
    )

async def persist_turn(channel_id: int, history: List[Dict], answer: str) -> None:
    """Append the assistant reply and save, so the exchange actually persists.

    Without this the DB only ever held user turns (and only on errors), which
    made conversation memory silently stop growing.
    """
    answer = (answer or "").strip()
    if answer:
        history.append({"role": "assistant", "content": answer[:STORE_MAX]})
    await save_history(channel_id, history)

# ===== DEEP RESEARCH =====
def chunk_text(text: str, limit: int = 1900) -> List[str]:
    """Split text into Discord-safe chunks (hard limit is 2000)."""
    if len(text) <= limit:
        return [text]
    chunks, rest = [], text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return chunks

def strip_keywords(text: str) -> str:
    """Peel off leading trigger keywords ("bé yêu ", "bot ", ...) until none match."""
    text = text.strip()
    while True:
        for kw in KEYWORDS:
            m = re.match(re.escape(kw) + r"\s+", text, flags=re.IGNORECASE)
            if m:
                text = text[m.end():].strip()
                break
        else:
            break  # no keyword matched on this pass -> done
    return text

def _match_prefix(text: str, prefixes: tuple) -> Optional[str]:
    """Return what follows the first matching prefix, or None if none matches.

    Longest prefix first: re's alternation prefers the leftmost alternative, so
    "nhớ rằng " has to be tried before "nhớ " or the filler word leaks into the
    captured text. Slicing at the match end keeps the offset aligned with
    `text` even when casefolding changes lengths.
    """
    m = re.match(
        "|".join(re.escape(p) for p in prefixes),
        text,
        flags=re.IGNORECASE,
    )
    return text[m.end():].strip() if m else None

def research_query(content: str) -> Optional[str]:
    """Return the research topic if this message is a research request, else None.

    Accepts "nghiên cứu X", "research X", and messages with trigger keywords
    stripped first, e.g. "bot nghiên cứu X" or "ê gemini research X".
    """
    return _match_prefix(strip_keywords(content), RESEARCH_PREFIXES) or None

def remember_query(content: str) -> Optional[str]:
    """Return the fact to store when someone asks the bot to remember, else None.

    "bé yêu nhớ t là Đạo" / "@bot remember that Max likes pho" both work. A
    one-word capture ("nhớ rồi") is not a fact, so it falls through to chat.
    """
    fact = _match_prefix(strip_keywords(content), REMEMBER_PREFIXES)
    if not fact or len(fact.split()) < 2:
        return None
    return fact

def forget_query(content: str) -> Optional[str]:
    """Return the delete pattern, "" for "forget everything", or None if this
    message is not a forget request.

    None vs "" matters: an empty capture ("quên" with nothing after it) must
    not wipe every fact, so it stays None and the message is treated as chat.
    """
    pattern = _match_prefix(strip_keywords(content), FORGET_PREFIXES)
    if pattern is None:
        return None
    if pattern.lower() in ("hết", "all", "het", "xóa hết", "xoa het"):
        return ""
    return pattern or None

async def run_deep_research(query: str, max_sources: int = RESEARCH_DEFAULT_SOURCES) -> Dict:
    """Call the Railway deep-research endpoint. Synchronous, bounded by RESEARCH_TIMEOUT."""
    payload = {"query": query, "language": "vi", "max_sources": max_sources}
    async with httpx.AsyncClient(timeout=RESEARCH_TIMEOUT) as client:
        resp = await client.post(DEEP_RESEARCH_URL, json=payload)
        resp.raise_for_status()
        return resp.json()

def format_research(data: Dict, query: str) -> tuple[str, str]:
    """Return (discord_message, full_markdown_report)."""
    status = data.get("status", "unknown")
    if status != "completed":
        err = data.get("error") or f"status={status}"
        return f"❌ Deep Research failed: {err}", ""

    summary = data.get("summary") or ""
    sources = data.get("sources") or []
    duration = (data.get("duration_ms") or 0) // 1000
    title = data.get("query") or query

    # Chat message: header + opening of the report (full text lives in the attachment)
    head = (
        f"🔎 **Deep Research** — {title}\n"
        f"⏱ {duration}s · 📄 {len(sources)} nguồn\n\n"
    )
    # First chunk only, broken at a line boundary so markdown stays intact.
    body = chunk_text(summary, 1600)[0]
    if len(summary) > 1600:
        body += "\n\n*(báo cáo đầy đủ xem file đính kèm)*"
    message = head + body

    # Full report as a .md attachment (Discord caps a message at 2000 chars)
    md = (
        f"# {title}\n\n"
        f"> Nguồn: Deep Research (gemini-web-to-api)\n"
        f"> Thời lượng: {duration}s\n"
        f"> Số nguồn: {len(sources)}\n\n"
        f"{summary}\n\n## Sources\n"
    )
    for i, s in enumerate(sources, 1):
        md += f"{i}. [{s.get('title', '')}]({s.get('url', '')})\n"
    return message, md

async def deliver_research(send, query: str, max_sources: int = RESEARCH_DEFAULT_SOURCES):
    """Run a research request and post the result (message + .md attachment) to Discord.

    `send` is an async callable taking text and returning the sent message,
    e.g. `channel.send` or a `ctx.followup.send(..., wait=True)` wrapper.
    """
    placeholder = await send(
        f"🔎 **Đang nghiên cứu:** {query}\n"
        f"_Tối đa {RESEARCH_TIMEOUT // 60} phút, đợi bé tí nhé..._"
    )
    try:
        data = await run_deep_research(query, max_sources)
    except httpx.HTTPStatusError as e:
        await placeholder.edit(content=f"❌ Deep Research lỗi HTTP {e.response.status_code}")
        return
    except Exception as e:
        await placeholder.edit(content=f"❌ Deep Research lỗi: {e}")
        return

    text, md = format_research(data, query)
    if not md:
        await placeholder.edit(content=text)
        return
    # Fall back to text-only if the channel forbids attachments.
    try:
        file = discord.File(io.BytesIO(md.encode("utf-8")), filename="deep-research.md")
        await placeholder.edit(content=text, file=file)
    except Exception as e:
        logger.warning(f"attachment failed, sending text only: {e}")
        await placeholder.edit(content=text)

# ===== DISCORD BOT =====
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Bot(intents=intents)

async def call_webhook(history: List[Dict], channel_id: int) -> tuple[bool, dict]:
    payload = {
        "messages": history,
        "system": await build_system_prompt(),
        "username": BOT_USERNAME,
        "avatar_url": BOT_AVATAR
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(WEBHOOK_URL, json=payload)
            return True, resp.json()
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return False, {"error": str(e)}

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info(f"Keywords: {KEYWORDS}")
    logger.info(f"Max history: {MAX_HISTORY} exchanges (persistent)")

def strip_mentions(text: str, mentioned_users, bot_id) -> tuple[str, bool]:
    """Drop the bot's own mention token and render every other one as @name.

    Returns (text, mentioned); mentioned means the bot itself was tagged, and
    tagging the bot counts as a trigger even without a keyword.
    """
    mentioned = any(u.id == bot_id for u in mentioned_users)
    for user in mentioned_users:
        replacement = "" if user.id == bot_id else f"@{user.display_name}"
        text = text.replace(f"<@{user.id}>", replacement).replace(f"<@!{user.id}>", replacement)
    return text.strip(), mentioned

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    content = (message.content or "").strip()
    if not content:
        return

    content, mentioned = strip_mentions(content, message.mentions, bot.user.id if bot.user else 0)
    if not content and mentioned:
        content = "alo"

    # Deep Research — "nghiên cứu X", "research X", "bot nghiên cứu X"
    topic = research_query(content)
    if topic:
        logger.info(f"research from {message.author.display_name}: {topic!r}")
        await deliver_research(message.channel.send, topic)
        return

    # Prefix the sender so the model knows who is talking (see system prompt persona)
    turn = {"role": "user", "content": f"[{message.author.display_name}]: {content}"[:STORE_MAX]}

    if not mentioned and not any(kw in content.lower() for kw in KEYWORDS):
        # Plain chatter: keep it as context for the next tag, do not answer.
        pending[message.channel.id].append(turn)
        logger.info(f"ctx #{message.channel.id} from {message.author.display_name} "
                    f"({len(pending[message.channel.id])} buffered): {content[:60]!r}")
        return

    # Remember / forget — gated on the trigger so a random "quên ... " in chat
    # never deletes stored facts.
    fact = remember_query(content)
    if fact is not None:
        await add_fact(fact, message.author.display_name)
        logger.info(f"remember from {message.author.display_name}: {fact!r}")
        await message.reply(f"🧠 Đã nhớ: **{fact}**")
        return

    pattern = forget_query(content)
    if pattern is not None:
        deleted = await delete_facts(pattern)
        logger.info(f"forget from {message.author.display_name}: {pattern!r} -> {deleted}")
        if pattern == "":
            await message.reply(f"🗑️ Đã quên hết {deleted} điều.")
        else:
            await message.reply(f"🗑️ Đã quên {deleted} điều khớp **{pattern}**.")
        return

    logger.info(f"ask from {message.author.display_name} (mention={mentioned}): {content[:80]!r}")
    channel_id = message.channel.id
    async with channel_locks[channel_id]:
        # Stored transcript + chatter since the last answer + this message, so
        # the model already knows what the group was talking about.
        history = await get_history(channel_id)
        messages = history + list(pending.get(channel_id, ())) + [turn]

        async with message.channel.typing():
            success, result = await call_webhook(messages, channel_id)

        if success:
            # The buffered chatter is stored now, so it stops being pending.
            pending.pop(channel_id, None)
            await persist_turn(channel_id, messages, result.get("answer", ""))
        else:
            # Nothing was stored: keep the buffer so the context is not lost.
            await message.reply(f"❌ Error: {result.get('error', 'Unknown error')}")

    if success:
        await message.add_reaction("✅")

# ===== COMMANDS =====
@bot.slash_command(name="forget", description="Clear conversation memory in this channel")
async def forget(ctx):
    await clear_history(ctx.channel.id)
    await ctx.respond("🧠 Conversation memory cleared for this channel (permanent).\n"
                      "Facts I remember: `/facts`. To forget them: `quên hết`.")

@bot.slash_command(name="facts", description="List facts the group asked me to remember")
async def facts(ctx):
    rows = await get_facts()
    if not rows:
        await ctx.respond("🧠 Nothing remembered yet. Try `nhớ t là Đạo`.")
        return
    lines = [f"{i}. {f['fact']}" + (f" — *{f['author']}*" if f["author"] else "")
             for i, f in enumerate(rows, 1)]
    # Discord caps a message at 2000 chars; facts are global so keep the list intact-ish.
    out = "**Facts I remember:**\n" + "\n".join(lines)
    if len(out) > 1900:
        out = out[:1900] + "\n… (dùng `quên <từ>` để xóa)"
    await ctx.respond(out)

@bot.slash_command(name="memory", description="Show conversation history")
async def memory(ctx):
    history = await get_history(ctx.channel.id)
    if not history:
        await ctx.respond("📝 No conversation history yet.")
        return
    
    lines = []
    for msg in history[-20:]:
        role = "👤 You" if msg["role"] == "user" else "🤖 Gemini"
        preview = msg["content"][:150] + ("..." if len(msg["content"]) > 150 else "")
        lines.append(f"**{role}:** {preview}")
    
    await ctx.respond(f"**Conversation History ({len(history)} messages, persistent):**\n" + "\n".join(lines))

@bot.slash_command(name="ask", description="Ask Gemini directly")
async def ask(ctx, question: str):
    await ctx.defer()
    history = await get_history(ctx.channel.id)
    history.append({"role": "user", "content": question})
    
    success, result = await call_webhook(history, ctx.channel.id)
    if success:
        await persist_turn(ctx.channel.id, history, result.get("answer", ""))
        await ctx.followup.send("✅ Sent! Response will appear shortly.")
    else:
        await ctx.followup.send(f"❌ Error: {result.get('error', 'Unknown error')}")
        history.pop()
        await save_history(ctx.channel.id, history)

@bot.slash_command(
    name="research",
    description="Deep Research: báo cáo tổng hợp có nguồn (mất ~2-5 phút)",
)
async def research(
    ctx,
    query: str,
    max_sources: int = RESEARCH_DEFAULT_SOURCES,
):
    # Discord gives a slash command 3s to respond, and research takes minutes -> defer first.
    await ctx.defer()

    async def _send(text: str):
        return await ctx.followup.send(text, wait=True)

    await deliver_research(_send, query, max_sources)

# ===== STARTUP =====
@bot.listen()
async def on_connect():
    await init_db()

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not set")
        exit(1)
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set")
        exit(1)
    bot.run(DISCORD_TOKEN)