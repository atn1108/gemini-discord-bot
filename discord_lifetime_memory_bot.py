import discord
import httpx
import os
import logging
import asyncpg
import json
import io
import re
import asyncio
import datetime
import base64
import html
import ipaddress
import socket
import urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from typing import List, Dict, Optional, Any
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

# ===== PRIVILEGE / TOOL USE =====
# Discord user IDs allowed to run server-management tools (kick/ban/timeout/
# channels/roles/nicknames). Guild owners and members with the Administrator
# permission are always allowed, so an empty list is still safe.
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().lstrip("-").isdigit()}
# Escape hatch: turn the whole tool layer off without a code change if the tool
# bridge starts degrading answers.
ENABLE_TOOLS = os.getenv("ENABLE_TOOLS", "true").strip().lower() in ("1", "true", "yes", "on")
# Hard cap on tool round-trips per question so a confused model cannot spin.
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "4"))

# ===== DEEP RESEARCH & MEDIA / FILES =====
DEEP_RESEARCH_URL = os.getenv(
    "DEEP_RESEARCH_URL",
    "https://gemini-web-to-api-production.up.railway.app/gemini/v1beta/deepresearch",
)
IMAGE_GEN_URL = os.getenv(
    "IMAGE_GEN_URL",
    "https://gemini-web-to-api-production.up.railway.app/openai/v1/images/generations",
)
IMAGE_GEN_TIMEOUT = int(os.getenv("IMAGE_GEN_TIMEOUT", "120"))
GEMINI_COOKIES = os.getenv("GEMINI_COOKIES", "")
MAX_ATTACHMENT_BYTES = int(os.getenv("MAX_ATTACHMENT_BYTES", str(5 * 1024 * 1024)))
MAX_FILE_TEXT_CHARS = int(os.getenv("MAX_FILE_TEXT_CHARS", "12000"))
MUSIC_QUEUE_MAX = int(os.getenv("MUSIC_QUEUE_MAX", "50"))
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
1. Nhận yêu cầu -> Xác định loại tác vụ (Tin tức/Thời sự | Hỏi đáp nhanh | Code/Học tập | Nhạc/Ảnh/Tệp | Tán gẫu).
2. Nếu cần thông tin cập nhật (sau mốc 03/2026, sự kiện, giá cả, thời tiết, công nghệ, tỷ số...) -> **BẮT BUỘC gọi công cụ `web_search` (và `fetch_url` nếu cần đọc chi tiết trang) trước khi trả lời.**
3. Trả lời thẳng vào trọng tâm ngay từ câu đầu tiên. Cắt bỏ mọi câu rào đón ("Dưới đây là...", "Chào bạn...", "Theo thông tin tìm được...").

# Anti-patterns (Tuyệt đối tránh)
- Không bịa đặt hoặc đoán mò khi không có dữ liệu. Nếu tìm không thấy, báo thẳng "bé chưa tìm ra thông tin chính xác".
- Không dùng bảng (Markdown Table) vì vỡ khung hiển thị trên Discord điện thoại; thay bằng danh sách gạch đầu dòng phân cấp.
- Không trả lời dài dòng cho các câu hỏi đơn giản.

# Format Rules (Chuẩn Discord)
- Dùng **in đậm** cho ý chính, gạch đầu dòng (`-`) cho các mục.
- Code đặt trong block ```ngôn_ngữ ... ```.
- Tối ưu độ dài: câu hỏi thường trả lời trong 1–4 câu; hướng dẫn kỹ thuật hoặc tổng hợp tin tức thì chia ý rõ ràng, đủ bước.

# Tool Use (Công cụ server, nhạc, web, ảnh, tệp)
- Khi thành viên yêu cầu phát nhạc ("mở bài...", "bật nhạc...", "skip", "tạm dừng", "dừng nhạc", "xem hàng đợi"), gọi `play_music` hoặc `music_control`. Bé sẽ tự vào kênh voice của người gọi để phát.
- Khi cần tra cứu web mới nhất hoặc đọc nội dung một link cụ thể, gọi `web_search` / `fetch_url`.
- Khi thành viên nhờ vẽ/tạo ảnh, gọi `generate_image` (bé tự tạo và gửi ảnh thẳng lên kênh).
- Khi thành viên nhờ tạo/xuất file (code, markdown, json, csv, txt...), gọi `send_file` để gửi tệp đính kèm thật lên Discord.
- Khi thành viên yêu cầu thao tác trên server (kick/ban/timeout, tạo/xóa kênh, đổi biệt danh, thêm/bỏ role), gọi tool tương ứng thay vì chỉ hứa làm.
- Người được nhắc trong tin nhắn đã hiển thị dạng `@Tên(id)` — dùng đúng id đó làm tham số, không tự bịa id.
- Tool quản trị chỉ chạy khi người yêu cầu là admin; nếu kết quả là DENIED thì báo thẳng người đó không đủ quyền, không thử lại hay lách.
- Không gọi tool khi thành viên chỉ đang trò chuyện thông thường."""
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
        # Peer bots discovered from the guild itself: which bots are present and
        # which slash commands they have been seen serving. Learned, never
        # hardcoded -- a bot added tomorrow shows up on the next refresh.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS peer_bots (
                bot_id BIGINT PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                commands JSONB NOT NULL DEFAULT '[]',
                last_seen TIMESTAMPTZ DEFAULT NOW()
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

# ===== PEER BOTS (discovered, never hardcoded) =====
# The roster comes from the guild itself, so a bot invited tomorrow shows up
# without a code change. Slash commands are learned from the interaction
# metadata on messages that answer them -- real usage, not a web page.
PEER_BOT_LIMIT = 40      # how many peer bots ride along in the prompt
PEER_CMD_LIMIT = 12      # how many commands per bot


def _as_json(value, default):
    """asyncpg hands jsonb back as str; accept parsed values too."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return default
    return value if value is not None else default


async def get_peer_bots() -> List[Dict]:
    """Peer bots seen in this server, most recently used first.

    Returns [] before the DB is up rather than raising: the prompt must still
    build during startup and in tests that never call init_db.
    """
    if pool is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT bot_id, username, commands FROM peer_bots "
            "ORDER BY last_seen DESC LIMIT $1", PEER_BOT_LIMIT)
    out = []
    for r in rows:
        cmds = _as_json(r["commands"], [])
        out.append({
            "bot_id": r["bot_id"],
            "username": r["username"],
            "commands": [c for c in cmds if isinstance(c, str)][:PEER_CMD_LIMIT],
        })
    return out


async def record_peer_bot(bot_id: int, username: str, command: str = "") -> None:
    """Upsert one peer bot, merging in a newly learned command.

    Never raises: discovering a bot must not be able to break message handling.
    """
    if pool is None:
        return
    username = (username or "")[:100]
    command = (command or "")[:64]
    try:
        async with pool.acquire() as conn:
            if command:
                await conn.execute(
                    """
                    INSERT INTO peer_bots (bot_id, username, commands, last_seen)
                    VALUES ($1, $2, $3::jsonb, NOW())
                    ON CONFLICT (bot_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        commands = (
                            SELECT jsonb_agg(DISTINCT c)
                            FROM jsonb_array_elements(
                                COALESCE(peer_bots.commands, '[]'::jsonb)
                                || EXCLUDED.commands
                            ) AS t(c)
                        ),
                        last_seen = NOW()
                    """,
                    bot_id, username, json.dumps([command]),
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO peer_bots (bot_id, username, commands, last_seen)
                    VALUES ($1, $2, '[]'::jsonb, NOW())
                    ON CONFLICT (bot_id) DO UPDATE SET
                        username = EXCLUDED.username, last_seen = NOW()
                    """,
                    bot_id, username,
                )
    except Exception as e:
        logger.warning(f"record_peer_bot({bot_id}) failed: {e}")


def learn_from_message(message) -> None:
    """Notice a peer bot from a message it sent, and any command it served.

    Fires on every message, so a newly invited bot is picked up the first time
    it says anything -- no privileged intent, no hardcoded list.
    """
    author = getattr(message, "author", None)
    if author is None or not getattr(author, "bot", False):
        return
    uid = getattr(author, "id", None)
    if not uid or (bot.user is not None and uid == bot.user.id):
        return  # never list ourselves
    name = (getattr(author, "display_name", None)
            or getattr(author, "name", None) or "")
    if not name:
        return  # nothing to render, so nothing worth storing
    our_names = {BOT_USERNAME}
    if bot.user is not None:
        our_names.update(filter(None, (getattr(bot.user, "name", None),
                                       getattr(bot.user, "display_name", None))))
    if name in our_names:
        return  # our own answers travel through a webhook, not this account

    # The command name lives on interaction_metadata in py-cord 2.6;
    # Message.interaction is deprecated and only warns.
    command = ""
    meta = getattr(message, "interaction_metadata", None)
    if meta is not None:
        command = getattr(meta, "name", None) or ""

    asyncio.create_task(record_peer_bot(uid, name, command))


def render_peer_bots(bots: List[Dict]) -> str:
    """Prompt section telling the model which other bots exist here."""
    lines = []
    for b in bots:
        line = f"- {b['username']} (id {b['bot_id']})"
        if b["commands"]:
            line += " — lệnh đã thấy: " + ", ".join(f"`/{c}`" for c in b["commands"])
        lines.append(line)
    return (
        "# Bot khác trong server (không phải mình)\n"
        "Đây là các bot có thật, tự nhận diện từ server. Muốn gọi bot khác "
        "thì dùng `send_to_channel` với id kênh đích, viết đúng cú pháp lệnh "
        "của bot đó. Không bịa tên bot hay lệnh chưa thấy ở trên.\n"
        + "\n".join(lines)
    )


def render_channels() -> str:
    """Prompt section listing this server's real channel ids.

    The model needs actual ids for read_channel / send_to_channel, and the
    list comes from the live guild so a renamed or newly created channel is
    never stale. Voice channels are called out because they take no text.
    """
    text, voice = [], []
    for guild in getattr(bot, "guilds", None) or []:
        for ch in getattr(guild, "channels", None) or []:
            cid = getattr(ch, "id", None)
            name = getattr(ch, "name", None)
            if not cid or not name:
                continue
            kind = str(getattr(ch, "type", "") or "")
            if "voice" in kind or "stage" in kind:
                voice.append(f"#{name} ({cid})")
            elif "text" in kind or "news" in kind or "forum" in kind:
                text.append(f"#{name} ({cid})")
    if not text and not voice:
        return ""
    out = ["# Kênh trong server (id thật, dùng cho read_channel / send_to_channel)",
           "Chỉ kênh text nhận được tin nhắn."]
    out += [f"- {t}" for t in text]
    if voice:
        out.append("Kênh voice/stage — KHÔNG gửi text vào đây; dùng `play_music` "
                   "/ `music_control` để join voice phát nhạc khi người gọi đang "
                   "ở trong kênh voice: " + ", ".join(voice))
    return "\n".join(out)


async def build_system_prompt() -> str:
    """Persona + remembered facts + the peer bots discovered in this server."""
    facts = await get_facts()
    bots = await get_peer_bots()
    channels = render_channels()
    if not facts and not bots and not channels:
        return SYSTEM_PROMPT
    parts = [SYSTEM_PROMPT]
    if facts:
        lines = "\n".join(
            f"- {f['fact']} (do {f['author']} nhờ nhớ)" if f["author"] else f"- {f['fact']}"
            for f in facts
        )
        parts.append(
            "# Facts the group asked you to remember\n"
            "Use them when relevant; do not invent any others.\n" + lines
        )
    if bots:
        parts.append(render_peer_bots(bots))
    if channels:
        parts.append(channels)
    return "\n\n".join(parts)

async def persist_turn(channel_id: int, history: List[Dict], answer: str) -> None:
    """Append the assistant reply and save, so the exchange actually persists.

    Without this the DB only ever held user turns (and only on errors), which
    made conversation memory silently stop growing.
    """
    answer = (answer or "").strip()
    if answer:
        history.append({"role": "assistant", "content": answer[:STORE_MAX]})
    await save_history(channel_id, history)


# ===== TOOL USE (function calling) =====
# The upstream gemini-web-to-api tool bridge turns these OpenAI schemas into a
# prompt; Gemini answers with tool_calls, which we execute here and feed back.
# Everything that mutates the server is admin-only — see is_admin().

# Tool schemas sent with each request when ENABLE_TOOLS is on.
TOOLS: List[Dict] = [
    {"type": "function", "function": {
        "name": "kick",
        "description": "Kick a member from the server. Admin only.",
        "parameters": {"type": "object", "properties": {
            "member_id": {"type": "string", "description": "Discord user ID of the member to kick"},
            "reason": {"type": "string", "description": "Optional reason shown in the audit log"}},
            "required": ["member_id"]}}},
    {"type": "function", "function": {
        "name": "ban",
        "description": "Ban a member from the server. Admin only.",
        "parameters": {"type": "object", "properties": {
            "member_id": {"type": "string", "description": "Discord user ID of the member to ban"},
            "reason": {"type": "string", "description": "Optional reason"},
            "delete_message_seconds": {"type": "integer",
                                       "description": "Seconds of messages to delete (0-604800)"}},
            "required": ["member_id"]}}},
    {"type": "function", "function": {
        "name": "timeout",
        "description": "Timeout (mute) a member for a number of minutes. Admin only.",
        "parameters": {"type": "object", "properties": {
            "member_id": {"type": "string", "description": "Discord user ID"},
            "minutes": {"type": "integer", "description": "How long in minutes (1-40320)"},
            "reason": {"type": "string", "description": "Optional reason"}},
            "required": ["member_id", "minutes"]}}},
    {"type": "function", "function": {
        "name": "create_channel",
        "description": "Create a new text channel in this server. Admin only.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Channel name (lowercase, hyphenated)"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "delete_channel",
        "description": "Delete a channel by ID. Admin only. Destructive.",
        "parameters": {"type": "object", "properties": {
            "channel_id": {"type": "string", "description": "Discord channel ID to delete"}},
            "required": ["channel_id"]}}},
    {"type": "function", "function": {
        "name": "set_nickname",
        "description": "Change a member's nickname in this server. Admin only.",
        "parameters": {"type": "object", "properties": {
            "member_id": {"type": "string", "description": "Discord user ID"},
            "nickname": {"type": "string", "description": "New nickname (empty string to reset)"}},
            "required": ["member_id", "nickname"]}}},
    {"type": "function", "function": {
        "name": "add_role",
        "description": "Add a role to a member by role ID. Admin only.",
        "parameters": {"type": "object", "properties": {
            "member_id": {"type": "string", "description": "Discord user ID"},
            "role_id": {"type": "string", "description": "Discord role ID"}},
            "required": ["member_id", "role_id"]}}},
    {"type": "function", "function": {
        "name": "remove_role",
        "description": "Remove a role from a member by role ID. Admin only.",
        "parameters": {"type": "object", "properties": {
            "member_id": {"type": "string", "description": "Discord user ID"},
            "role_id": {"type": "string", "description": "Discord role ID"}},
            "required": ["member_id", "role_id"]}}},
    # Bot-to-bot / coordination: the bot can hand text to another webhook or
    # read the last messages of a channel. Read-only, safe for anyone.
    {"type": "function", "function": {
        "name": "read_channel",
        "description": "Read the last N messages of a channel in this server.",
        "parameters": {"type": "object", "properties": {
            "channel_id": {"type": "string", "description": "Discord channel ID"},
            "limit": {"type": "integer", "description": "How many messages (1-20)"}},
            "required": ["channel_id"]}}},
    {"type": "function", "function": {
        "name": "send_to_channel",
        "description": "Send a message to another channel in this server (e.g. to talk to another bot).",
        "parameters": {"type": "object", "properties": {
            "channel_id": {"type": "string", "description": "Discord channel ID"},
            "content": {"type": "string", "description": "Message text to send"}},
            "required": ["channel_id", "content"]}}},
    # Music playback in voice channels (safe for everyone).
    {"type": "function", "function": {
        "name": "play_music",
        "description": "Join the caller's voice channel and play or queue music from a song name or URL.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Song name, artist, or YouTube/audio URL"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "music_control",
        "description": "Control music playback or view the queue in this server.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string",
                       "enum": ["skip", "pause", "resume", "stop", "queue"],
                       "description": "Playback action to perform"}},
            "required": ["action"]}}},
    # Web search & page reader (safe for everyone).
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the live web for current news, prices, weather, facts, or URLs.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {"type": "integer", "description": "Max results (1-8)"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "Fetch and read readable text from a public http/https webpage URL.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Public http/https URL to read"}},
            "required": ["url"]}}},
    # Built-in image generation + auto-fetch to Discord (safe for everyone).
    {"type": "function", "function": {
        "name": "generate_image",
        "description": "Generate an image using the bot's built-in Gemini image generator and post it to the channel.",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "Detailed visual description of the image to generate"},
            "aspect_ratio": {"type": "string", "description": "Optional aspect ratio, e.g. 16:9, 1:1, 9:16"}},
            "required": ["prompt"]}}},
    # Create & upload a file attachment to Discord (safe for everyone).
    {"type": "function", "function": {
        "name": "send_file",
        "description": "Create a text/code/data file and send it as an attachment in Discord.",
        "parameters": {"type": "object", "properties": {
            "filename": {"type": "string", "description": "File name with extension, e.g. report.md, script.py, data.json"},
            "content": {"type": "string", "description": "Full text contents of the file"},
            "caption": {"type": "string", "description": "Optional short message sent with the file"},
            "channel_id": {"type": "string", "description": "Optional target text channel ID (defaults to current channel)"}},
            "required": ["filename", "content"]}}},
]

# Tools that require admin rights.
DANGEROUS_TOOLS = {"kick", "ban", "timeout", "create_channel", "delete_channel",
                   "set_nickname", "add_role", "remove_role"}


# ===== MUSIC PLAYER (per-guild voice queue) =====
class GuildMusicState:
    def __init__(self):
        self.queue: deque = deque(maxlen=MUSIC_QUEUE_MAX)
        self.current: Optional[Dict[str, Any]] = None
        self.lock: asyncio.Lock = asyncio.Lock()


music_states: Dict[int, GuildMusicState] = defaultdict(GuildMusicState)


def _format_duration(seconds) -> str:
    if not seconds:
        return "?"
    sec = int(seconds)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _extract_track_info(query: str) -> Dict[str, Any]:
    """Blocking yt-dlp extraction (run via asyncio.to_thread)."""
    import yt_dlp

    q = query.strip()
    is_url = q.startswith(("http://", "https://"))
    target = q if is_url else f"ytsearch1:{q}"
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch",
        "source_address": "0.0.0.0",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
    if not info:
        raise ValueError("No matching audio found")
    if "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise ValueError("No matching audio found")
        info = entries[0]
    stream_url = info.get("url")
    if not stream_url:
        raise ValueError("No playable audio stream URL returned")
    return {
        "title": info.get("title") or q,
        "stream_url": stream_url,
        "webpage_url": info.get("webpage_url") or (q if is_url else ""),
        "duration": info.get("duration") or 0,
    }


def _create_audio_source(stream_url: str):
    return discord.FFmpegPCMAudio(
        stream_url,
        before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        options="-vn",
    )


async def _play_next_track(guild: discord.Guild) -> None:
    state = music_states[guild.id]
    async with state.lock:
        vc = getattr(guild, "voice_client", None)
        if vc is None or not getattr(vc, "is_connected", lambda: True)():
            state.current = None
            state.queue.clear()
            return
        if not state.queue:
            state.current = None
            return
        nxt = state.queue.popleft()
        # Re-resolve stream URL when starting a queued track so links never go stale.
        if nxt.get("webpage_url") and nxt.get("_queued"):
            try:
                fresh = await asyncio.to_thread(_extract_track_info, nxt["webpage_url"])
                nxt["stream_url"] = fresh["stream_url"]
            except Exception as e:
                logger.warning(f"queued track refresh failed for {nxt.get('title')}: {e}")
        state.current = nxt
        try:
            source = _create_audio_source(nxt["stream_url"])
            loop = getattr(bot, "loop", None) or asyncio.get_running_loop()

            def _after(err):
                if err:
                    logger.warning(f"voice playback error in {guild.id}: {err}")
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(_play_next_track(guild))
                )

            vc.play(source, after=_after)
        except Exception as e:
            logger.warning(f"vc.play failed in {guild.id}: {e}")
            state.current = None
            asyncio.create_task(_play_next_track(guild))


async def _handle_play_music(guild: discord.Guild, caller: Optional[discord.Member],
                             query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "ERROR: missing song name or URL"
    voice_state = getattr(caller, "voice", None)
    voice_ch = getattr(voice_state, "channel", None)
    if voice_ch is None:
        return "ERROR: Caller is not in a voice channel. Ask them to join a voice channel first."

    vc = getattr(guild, "voice_client", None)
    if vc is None:
        vc = await voice_ch.connect()
    elif getattr(vc, "channel", None) != voice_ch:
        await vc.move_to(voice_ch)

    track = await asyncio.to_thread(_extract_track_info, query)
    track["requester"] = getattr(caller, "display_name", "?")
    state = music_states[guild.id]
    if vc.is_playing() or vc.is_paused():
        if len(state.queue) >= MUSIC_QUEUE_MAX:
            return f"ERROR: Music queue is full ({MUSIC_QUEUE_MAX} tracks)."
        track["_queued"] = True
        state.queue.append(track)
        return (f"Queued #{len(state.queue)}: {track['title']} "
                f"({_format_duration(track['duration'])}) in #{voice_ch.name}")

    state.queue.appendleft(track)
    await _play_next_track(guild)
    return (f"Now playing in #{voice_ch.name}: {track['title']} "
            f"({_format_duration(track['duration'])})")


async def _handle_music_control(guild: discord.Guild, action: str) -> str:
    action = (action or "").strip().lower()
    state = music_states[guild.id]
    vc = getattr(guild, "voice_client", None)
    if action == "queue":
        lines = []
        if state.current:
            lines.append(
                f"Now playing: {state.current['title']} "
                f"({_format_duration(state.current.get('duration'))})"
            )
        for idx, t in enumerate(list(state.queue)[:10], 1):
            lines.append(f"{idx}. {t['title']} ({_format_duration(t.get('duration'))})")
        return "\n".join(lines) if lines else "Queue is empty."

    if vc is None:
        return "ERROR: Bot is not currently in a voice channel."
    if action == "skip":
        if not vc.is_playing() and not vc.is_paused():
            return "Nothing is currently playing."
        vc.stop()  # Triggers _after -> _play_next_track
        return "Skipped current track."
    if action == "pause":
        if vc.is_playing():
            vc.pause()
            return "Paused playback."
        return "Nothing is playing right now."
    if action == "resume":
        if vc.is_paused():
            vc.resume()
            return "Resumed playback."
        return "Playback is not paused."
    if action == "stop":
        state.queue.clear()
        state.current = None
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await vc.disconnect()
        return "Stopped music, cleared queue, and left voice channel."
    return f"ERROR: unknown music action '{action}'"


# ===== WEB SEARCH & URL FETCH =====
def _is_safe_public_url(raw_url: str) -> tuple[bool, str]:
    """Block non-http(s) URLs and private/loopback/link-local hosts (SSRF guard)."""
    try:
        parsed = urllib.parse.urlparse((raw_url or "").strip())
    except Exception:
        return False, "invalid URL"
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False, "only public http/https URLs are allowed"
    host = parsed.hostname.lower()
    if host in ("localhost", "localhost.localdomain"):
        return False, "localhost is blocked"
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        return False, f"DNS lookup failed: {e}"
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False, f"private/reserved IP blocked ({ip_str})"
    return True, ""


def _strip_html(raw: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    return " ".join(cleaned.split())


def _parse_rss_items(xml_text: str, limit: int) -> List[Dict[str, str]]:
    root = ET.fromstring(xml_text)
    out = []
    for item in root.findall(".//item"):
        t_el = item.find("title")
        d_el = item.find("description")
        title = _strip_html("".join(t_el.itertext()) if t_el is not None else "")
        link = (item.findtext("link") or "").strip()
        desc = _strip_html("".join(d_el.itertext()) if d_el is not None else "")
        pub = (item.findtext("pubDate") or "").strip()
        if title and link:
            out.append({"title": title, "link": link, "desc": desc[:240], "pub": pub})
        if len(out) >= limit:
            break
    return out


async def _web_search(query: str, max_results: int = 5) -> str:
    query = (query or "").strip()
    if not query:
        return "ERROR: empty search query"
    limit = max(1, min(int(max_results or 5), 8))
    q = urllib.parse.quote(query)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    }
    results: List[Dict[str, str]] = []
    seen_urls = set()
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as client:
        for url in (
            f"https://www.bing.com/search?q={q}&format=rss",
            f"https://news.google.com/rss/search?q={q}&hl=vi&gl=VN&ceid=VN:vi",
        ):
            try:
                r = await client.get(url)
                if r.status_code == 200 and "<item>" in r.text:
                    for item in _parse_rss_items(r.text, limit):
                        if item["link"] in seen_urls:
                            continue
                        seen_urls.add(item["link"])
                        results.append(item)
                        if len(results) >= limit + 2:
                            break
            except Exception as e:
                logger.warning(f"web_search fetch failed ({url[:40]}): {e}")
    if not results:
        return "No web search results found."
    lines = []
    for i, item in enumerate(results[:limit + 2], 1):
        meta = f" ({item['pub']})" if item["pub"] else ""
        snippet = f" — {item['desc']}" if item["desc"] else ""
        lines.append(f"{i}. {item['title']}{meta}\n   URL: {item['link']}{snippet}")
    return "\n".join(lines)


async def _fetch_url(url: str) -> str:
    cur = (url or "").strip()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers=headers) as client:
        resp = None
        for _ in range(5):
            ok, reason = _is_safe_public_url(cur)
            if not ok:
                return f"ERROR: blocked URL ({reason})"
            resp = await client.get(cur)
            if resp.is_redirect and resp.headers.get("location"):
                cur = urllib.parse.urljoin(cur, resp.headers["location"])
                continue
            break
        if resp is None:
            return "ERROR: failed to fetch URL"
        resp.raise_for_status()
        ct = (resp.headers.get("content-type") or "").lower()
        if "html" in ct or not ct:
            text = _strip_html(resp.text)
        elif ct.startswith("text/") or "json" in ct or "xml" in ct:
            text = resp.text
        else:
            return f"Fetched non-text content ({ct}, {len(resp.content)} bytes)."
        text = text.strip()
        if not text:
            return "(page has no readable text)"
        return text[:4000] + ("…" if len(text) > 4000 else "")


# ===== BUILT-IN IMAGE GENERATION & AUTO-FETCH =====
async def _download_image_bytes(url: str) -> Optional[bytes]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
        "Referer": "https://gemini.google.com/",
    }
    if GEMINI_COOKIES:
        headers["Cookie"] = GEMINI_COOKIES
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for candidate in (url, f"{url}=s2048-rj"):
            try:
                r = await client.get(candidate, headers=headers)
                if r.status_code == 200 and (r.headers.get("content-type") or "").startswith("image/"):
                    return r.content
            except Exception:
                continue
    return None


async def _generate_and_send_image(channel, prompt: str, aspect_ratio: str = "") -> str:
    prompt = (prompt or "").strip()
    if not prompt:
        return "ERROR: empty image prompt"
    full_prompt = f"{prompt} (aspect ratio {aspect_ratio.strip()})" if aspect_ratio.strip() else prompt
    async with httpx.AsyncClient(timeout=IMAGE_GEN_TIMEOUT) as client:
        # First try b64_json so the upstream service handles Google cookies.
        try:
            r = await client.post(IMAGE_GEN_URL, json={
                "prompt": full_prompt,
                "model": "gemini-auto",
                "n": 1,
                "response_format": "b64_json",
            })
            if r.status_code == 200:
                data = (r.json().get("data") or [{}])[0]
                b64 = data.get("b64_json") or ""
                if b64:
                    raw = base64.b64decode(b64)
                    await channel.send(file=discord.File(io.BytesIO(raw), filename="image.png"))
                    return f"Generated and sent image to #{getattr(channel, 'name', getattr(channel, 'id', 'channel'))}."
        except Exception as e:
            logger.warning(f"image b64_json failed: {e}")

        # Fallback: request URL and attempt direct fetch.
        r2 = await client.post(IMAGE_GEN_URL, json={
            "prompt": full_prompt,
            "model": "gemini-auto",
            "n": 1,
            "response_format": "url",
        })
        r2.raise_for_status()
        url = ((r2.json().get("data") or [{}])[0]).get("url") or ""
        if not url:
            return "ERROR: image generator returned no image"
        raw = await _download_image_bytes(url)
        if raw:
            await channel.send(file=discord.File(io.BytesIO(raw), filename="image.png"))
            return f"Generated and sent image to #{getattr(channel, 'name', getattr(channel, 'id', 'channel'))}."
        return (f"IMAGE_FETCH_BLOCKED: Gemini generated the image at {url} "
                f"but Google CDN returned HTTP 403 when fetching the file bytes directly. "
                f"Share this link with the user and explain the 403 CDN limitation.")


# ===== FILE CREATION & ATTACHMENT READING =====
def _sanitize_filename(name: str) -> str:
    base = os.path.basename((name or "").strip())
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return (base or "output.txt")[:100]


async def _handle_send_file(guild: discord.Guild, default_channel, args: Dict) -> str:
    content = args.get("content")
    if content is None or content == "":
        return "ERROR: file content cannot be empty"
    raw = str(content).encode("utf-8")
    if len(raw) > 8 * 1024 * 1024:
        return "ERROR: file exceeds 8 MB Discord limit"
    cid = str(args.get("channel_id") or "").strip()
    target = guild.get_channel(int(cid)) if cid.isdigit() else default_channel
    if target is None:
        return "ERROR: target channel not found"
    if not hasattr(target, "send"):
        return (f"ERROR: #{getattr(target, 'name', target.id)} is a "
                f"voice/stage channel and cannot receive files")
    fname = _sanitize_filename(args.get("filename") or "output.txt")
    caption = (args.get("caption") or "").strip()[:1900] or None
    await target.send(content=caption, file=discord.File(io.BytesIO(raw), filename=fname))
    return f"Sent file '{fname}' ({len(raw)} bytes) to #{getattr(target, 'name', target.id)}"


def _extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    chunks = []
    total = 0
    for idx, page in enumerate(reader.pages, 1):
        txt = (page.extract_text() or "").strip()
        if not txt:
            continue
        piece = f"[Page {idx}]\n{txt}"
        chunks.append(piece)
        total += len(piece)
        if total >= MAX_FILE_TEXT_CHARS:
            break
    return "\n\n".join(chunks)[:MAX_FILE_TEXT_CHARS]


_TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".csv",
    ".yaml", ".yml", ".xml", ".html", ".css", ".sql", ".sh", ".log", ".ini",
    ".toml", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".env",
}


async def extract_attachments(attachments) -> tuple[str, List[Dict[str, Any]]]:
    """Read Discord attachments into text blocks and OpenAI image/PDF parts."""
    items = list(attachments or [])[:3]
    if not items:
        return "", []
    text_blocks: List[str] = []
    media_parts: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for att in items:
            name = getattr(att, "filename", None) or "file"
            size = int(getattr(att, "size", 0) or 0)
            ct = (getattr(att, "content_type", None) or "").split(";")[0].strip().lower()
            ext = os.path.splitext(name.lower())[1]
            if size and size > MAX_ATTACHMENT_BYTES:
                text_blocks.append(f"[Tệp quá lớn (>5MB): {name}]")
                continue
            try:
                if hasattr(att, "read"):
                    data = await att.read()
                else:
                    r = await client.get(getattr(att, "url", ""))
                    r.raise_for_status()
                    data = r.content
            except Exception as e:
                text_blocks.append(f"[Không tải được tệp {name}: {e}]")
                continue
            if len(data) > MAX_ATTACHMENT_BYTES:
                text_blocks.append(f"[Tệp quá lớn (>5MB): {name}]")
                continue

            if ct.startswith("image/") or ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                mime = ct if ct.startswith("image/") else f"image/{ext.lstrip('.') or 'png'}"
                if mime == "image/jpg":
                    mime = "image/jpeg"
                b64 = base64.b64encode(data).decode("ascii")
                media_parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                text_blocks.append(f"[Ảnh đính kèm: {name}]")
                continue

            if ct == "application/pdf" or ext == ".pdf":
                pdf_text = ""
                try:
                    pdf_text = await asyncio.to_thread(_extract_pdf_text, data)
                except Exception as e:
                    logger.warning(f"PDF parse failed for {name}: {e}")
                if pdf_text.strip():
                    text_blocks.append(f"[Nội dung PDF: {name}]\n{pdf_text}")
                else:
                    b64 = base64.b64encode(data).decode("ascii")
                    media_parts.append({"type": "image_url", "image_url": {"url": f"data:application/pdf;base64,{b64}"}})
                    text_blocks.append(f"[PDF đính kèm (scan): {name}]")
                continue

            if ct.startswith("text/") or ext in _TEXT_EXTS or (b"\x00" not in data[:4096]):
                decoded = data.decode("utf-8", errors="replace")
                if len(decoded) > MAX_FILE_TEXT_CHARS:
                    decoded = decoded[:MAX_FILE_TEXT_CHARS] + "\n…(truncated)"
                text_blocks.append(f"[Nội dung tệp: {name}]\n```\n{decoded}\n```")
            else:
                text_blocks.append(f"[Tệp nhị phân đính kèm: {name} ({len(data)} bytes, {ct or 'unknown'})]")
    return "\n\n".join(text_blocks), media_parts


async def is_admin(member: Optional[discord.Member], guild: Optional[discord.Guild]) -> bool:
    """True when the member may run server-management tools.

    Admin = explicit ADMIN_IDS ∪ guild owner ∪ Administrator permission.
    Fail-closed: no member object or no guild means not an admin.
    """
    if member is None or guild is None:
        return False
    if member.id in ADMIN_IDS:
        return True
    if guild.owner_id == member.id:
        return True
    try:
        return bool(member.guild_permissions.administrator)
    except Exception:
        return False


async def resolve_member(guild: discord.Guild, member_id: str) -> Optional[discord.Member]:
    """Look up a member by ID without privileged intents (REST fetch)."""
    try:
        return await guild.fetch_member(int(member_id))
    except Exception as e:
        logger.warning(f"fetch_member({member_id}) failed: {e}")
        # Fall back to the cache if REST fails.
        return guild.get_member(int(member_id)) if str(member_id).isdigit() else None


async def execute_tool(name: str, args: Dict, guild: discord.Guild,
                       channel: discord.abc.GuildChannel,
                       caller: Optional[discord.Member],
                       admin: bool) -> str:
    """Run one tool and return a result string for the model.

    Admin-gated tools are refused for non-admins with a clear message, so the
    model can tell the user why instead of silently failing.
    """
    if name in DANGEROUS_TOOLS and not admin:
        return (f"DENIED: '{name}' requires admin rights. "
                f"{getattr(caller, 'display_name', 'This user')} is not an admin.")

    try:
        if name == "kick":
            member = await resolve_member(guild, args.get("member_id", ""))
            if member is None:
                return f"ERROR: member {args.get('member_id')} not found"
            if member.id == guild.owner_id or member.id in ADMIN_IDS:
                return "ERROR: cannot kick an owner/admin"
            await member.kick(reason=args.get("reason") or f"by {BOT_USERNAME}")
            return f"Kicked {member.display_name} ({member.id})"

        if name == "ban":
            member = await resolve_member(guild, args.get("member_id", ""))
            if member is None:
                # Banning an ID not in cache still works via REST ban.
                await guild.ban(discord.Object(id=int(args.get("member_id", 0))),
                                delete_message_seconds=int(args.get("delete_message_seconds") or 0),
                                reason=args.get("reason") or f"by {BOT_USERNAME}")
                return f"Banned user {args.get('member_id')}"
            if member.id == guild.owner_id or member.id in ADMIN_IDS:
                return "ERROR: cannot ban an owner/admin"
            await member.ban(delete_message_seconds=int(args.get("delete_message_seconds") or 0),
                             reason=args.get("reason") or f"by {BOT_USERNAME}")
            return f"Banned {member.display_name} ({member.id})"

        if name == "timeout":
            member = await resolve_member(guild, args.get("member_id", ""))
            if member is None:
                return f"ERROR: member {args.get('member_id')} not found"
            minutes = max(1, min(int(args.get("minutes") or 1), 40320))
            until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)
            await member.timeout(until, reason=args.get("reason") or f"by {BOT_USERNAME}")
            return f"Timed out {member.display_name} for {minutes} min"

        if name == "create_channel":
            cname = (args.get("name") or "").strip().lower().replace(" ", "-")[:100]
            if not cname:
                return "ERROR: empty channel name"
            new_ch = await guild.create_text_channel(cname, reason=f"by {BOT_USERNAME}")
            return f"Created channel <#{new_ch.id}> ({new_ch.id})"

        if name == "delete_channel":
            cid = int(args.get("channel_id") or 0)
            target = guild.get_channel(cid)
            if target is None:
                return f"ERROR: channel {cid} not found in this server"
            if target.id == channel.id:
                return "ERROR: refusing to delete the current channel"
            await target.delete(reason=f"by {BOT_USERNAME}")
            return f"Deleted channel {cid}"

        if name == "set_nickname":
            member = await resolve_member(guild, args.get("member_id", ""))
            if member is None:
                return f"ERROR: member {args.get('member_id')} not found"
            nick = (args.get("nickname") or "").strip() or None
            await member.edit(nick=nick, reason=f"by {BOT_USERNAME}")
            return f"Set nickname of {member.display_name} to {nick or '(default)'}"

        if name in ("add_role", "remove_role"):
            member = await resolve_member(guild, args.get("member_id", ""))
            if member is None:
                return f"ERROR: member {args.get('member_id')} not found"
            role = guild.get_role(int(args.get("role_id") or 0))
            if role is None:
                return f"ERROR: role {args.get('role_id')} not found"
            if name == "add_role":
                await member.add_roles(role, reason=f"by {BOT_USERNAME}")
                return f"Added role {role.name} to {member.display_name}"
            await member.remove_roles(role, reason=f"by {BOT_USERNAME}")
            return f"Removed role {role.name} from {member.display_name}"

        if name == "read_channel":
            target = guild.get_channel(int(args.get("channel_id") or 0))
            if target is None:
                return f"ERROR: channel not found"
            if not hasattr(target, "history"):
                return (f"ERROR: #{getattr(target, 'name', target.id)} is a "
                        f"voice/stage channel and holds no text messages")
            limit = max(1, min(int(args.get("limit") or 5), 20))
            lines = []
            async for m in target.history(limit=limit):
                who = getattr(m.author, "display_name", "?")
                body = (getattr(m, "content", "") or "").strip()
                if not body and getattr(m, "embeds", None):
                    emb_parts = []
                    for emb in m.embeds[:2]:
                        t = getattr(emb, "title", None) or ""
                        d = getattr(emb, "description", None) or ""
                        piece = f"{t}: {d}".strip(": ")
                        if piece:
                            emb_parts.append(piece)
                    body = " | ".join(emb_parts)
                lines.append(f"[{who}] {body[:300]}")
            return "\n".join(reversed(lines)) or "(no messages)"

        if name == "send_to_channel":
            target = guild.get_channel(int(args.get("channel_id") or 0))
            if target is None:
                return f"ERROR: channel not found"
            if not hasattr(target, "send"):
                return (f"ERROR: #{getattr(target, 'name', target.id)} is a "
                        f"voice/stage channel and cannot receive text")
            content = (args.get("content") or "")[:1900]
            await target.send(content)
            return f"Sent to channel {target.id}"

        if name == "play_music":
            return await _handle_play_music(guild, caller, args.get("query") or "")

        if name == "music_control":
            return await _handle_music_control(guild, args.get("action") or "")

        if name == "web_search":
            return await _web_search(args.get("query") or "", args.get("max_results") or 5)

        if name == "fetch_url":
            return await _fetch_url(args.get("url") or "")

        if name == "generate_image":
            return await _generate_and_send_image(
                channel, args.get("prompt") or "", args.get("aspect_ratio") or "")

        if name == "send_file":
            return await _handle_send_file(guild, channel, args)

        return f"ERROR: unknown tool '{name}'"
    except Exception as e:
        logger.warning(f"tool {name} failed: {e}")
        return f"ERROR: {name} failed: {e}"

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

async def call_webhook(history: List[Dict], channel_id: int,
                       tools: Optional[List[Dict]] = None) -> tuple[bool, dict]:
    payload = {
        "messages": history,
        "system": await build_system_prompt(),
        "username": BOT_USERNAME,
        "avatar_url": BOT_AVATAR
    }
    if tools and ENABLE_TOOLS:
        # OpenAI tool schemas; the upstream bridge turns them into a prompt and
        # may answer with tool_calls instead of text.
        payload["tools"] = tools
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(WEBHOOK_URL, json=payload)
            return True, resp.json()
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return False, {"error": str(e)}


async def run_tool_loop(messages: List[Dict], channel_id: int,
                        guild: Optional[discord.Guild],
                        channel, caller) -> tuple[bool, dict]:
    """Ask the model, executing whatever tools it requests, then return.

    Works on a copy of `messages`: the tool_calls/tool protocol rows are for
    one exchange only and must never reach the stored transcript.

    The forwarder posts to Discord only for a text answer, and a text answer
    ends the loop, so every completed round lands exactly one message in the
    channel. The final round offers no tools at all, which forces a text
    answer and bounds the loop even if the model keeps asking for tools.
    """
    if not ENABLE_TOOLS or guild is None:
        return await call_webhook(messages, channel_id)

    admin = await is_admin(caller, guild)
    # Non-admins are never even offered the destructive tools, so "kick X"
    # from a random member cannot reach execute_tool in the first place.
    pool = TOOLS if admin else [t for t in TOOLS
                                if t["function"]["name"] not in DANGEROUS_TOOLS]

    work = list(messages)
    for round_no in range(MAX_TOOL_ROUNDS + 1):
        use_tools = pool if round_no < MAX_TOOL_ROUNDS else None
        ok, result = await call_webhook(work, channel_id, tools=use_tools)
        if not ok:
            return False, result

        tool_calls = result.get("tool_calls") or []
        if not tool_calls:
            return True, result  # text answer: the forwarder already posted it
        if use_tools is None:
            # Unreachable while the bridge only answers with tool_calls when
            # tools were sent, but never hand raw JSON to the channel.
            return False, {"error": "model exceeded the tool round limit"}

        # Keep the protocol rows for the next round, out of the DB transcript.
        work.append({"role": "assistant", "content": result.get("content") or "",
                     "tool_calls": tool_calls})
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or "?"
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            out = await execute_tool(name, args, guild, channel, caller, admin)
            logger.info(f"tool {name}(round {round_no + 1}) admin={admin} -> {out[:120]!r}")
            work.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                         "content": out})

    return False, {"error": "tool loop did not terminate"}

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info(f"Keywords: {KEYWORDS}")
    logger.info(f"Max history: {MAX_HISTORY} exchanges (persistent)")
    # Seed the peer-bot roster from the member list so bots that have not said
    # anything yet are already known. REST, no privileged intent required.
    asyncio.create_task(_seed_peer_bots())


async def _seed_peer_bots() -> None:
    """Discover every bot currently in the guild(s) we are in."""
    if pool is None:
        return
    me = bot.user.id if bot.user else 0
    for guild in bot.guilds:
        try:
            members = await bot.http.get_members(guild.id, limit=1000,
                                                 after=None)
        except Exception as e:
            logger.warning(f"peer bot seed failed for {guild.id}: {e}")
            continue
        n = 0
        for m in members or []:
            u = (m or {}).get("user") or {}
            if not u.get("bot"):
                continue
            uid = int(u.get("id") or 0)
            if not uid or uid == me:
                continue
            await record_peer_bot(uid, u.get("username") or u.get("global_name") or "")
            n += 1
        logger.info(f"peer bots discovered in #{getattr(guild, 'name', guild.id)}: {n}")

def strip_mentions(text: str, mentioned_users, bot_id,
                   roles=(), channels=()) -> tuple[str, bool]:
    """Drop the bot's own mention token and render every other one with its ID.

    IDs are what make the moderation tools usable: "@Đạo(999)" gives the model
    everything it needs to fill a member_id / role_id argument, which plain
    "@Đạo" never could. Returns (text, mentioned) where mentioned means the
    bot itself was tagged — tagging the bot counts as a trigger with no
    keyword, and so does a Reply to one of its messages.
    """
    mentioned = any(u.id == bot_id for u in mentioned_users)
    for user in mentioned_users:
        replacement = "" if user.id == bot_id else f"@{user.display_name}({user.id})"
        text = text.replace(f"<@{user.id}>", replacement).replace(f"<@!{user.id}>", replacement)
    # Role / channel tokens have no "mention" semantics for us, but the model
    # needs their IDs too (add_role, delete_channel, send_to_channel).
    for role in roles:
        text = text.replace(f"<@&{role.id}>", f"@{role.name}({role.id})")
    for ch in channels:
        text = text.replace(f"<#{ch.id}>", f"#{ch.name}({ch.id})")
    return text.strip(), mentioned


async def resolve_reply(message, bot_id: int):
    """Fetch the message this one replies to.

    Returns (target, is_reply_to_bot). The answers people actually reply to
    are posted through a Discord webhook rather than the bot account, so
    checking `author.id == bot_id` alone would miss every one of them — any
    bot-authored target counts, which is what "reply = tag" means in practice.
    """
    ref = getattr(message, "reference", None)
    msg_id = getattr(ref, "message_id", None)
    if not msg_id:
        return None, False
    target = getattr(ref, "cached_message", None)
    if target is None:
        # Reference points at a message older than the cache: one REST fetch.
        try:
            target = await message.channel.fetch_message(msg_id)
        except Exception as e:
            logger.warning(f"reply target {msg_id} unavailable: {e}")
            return None, False
    author = getattr(target, "author", None)
    if author is None:
        return None, False
    is_bot = (getattr(author, "id", None) == bot_id
              or bool(getattr(author, "bot", False)))
    return target, is_bot


def render_reply_context(target) -> str:
    """`[Reply tới @Tên: "nội dung"]` so the model knows who is replying to whom.

    Attachments carry no text, so say that instead of quoting nothing.
    """
    if target is None:
        return ""
    author = getattr(target, "author", None)
    name = (getattr(author, "display_name", None)
            or getattr(author, "name", None) or "?")
    text = (getattr(target, "content", "") or "").strip()
    if not text and getattr(target, "attachments", None):
        text = "(đính kèm tệp)"
    if not text:
        return f"[Reply tới @{name}] "
    text = text[:160] + ("…" if len(text) > 160 else "")
    return f'[Reply tới @{name}: "{text}"] '

@bot.event
async def on_message(message):
    # Learn about peer bots from any message they send, including before the
    # author-bot guard below returns.
    if getattr(message, "author", None) is not None and message.author.bot:
        learn_from_message(message)

    if message.author.bot:
        return
    
    msg_atts = list(getattr(message, "attachments", None) or [])
    content = (message.content or "").strip()
    if not content and not msg_atts:
        return

    content, mentioned = strip_mentions(
        content, message.mentions, bot.user.id if bot.user else 0,
        roles=getattr(message, "role_mentions", ()) or (),
        channels=getattr(message, "channel_mentions", ()) or (),
    )
    # Reply to one of the bot's messages counts as tagging it — no @ needed.
    # Resolved once: the same target tells us both whether to answer and what
    # is being replied to, so the model never has to guess who is talking.
    target, is_reply_to_bot = (None, False)
    if getattr(message, "reference", None) is not None:
        target, is_reply_to_bot = await resolve_reply(
            message, bot.user.id if bot.user else 0)
    if not mentioned and is_reply_to_bot:
        mentioned = True
    if not content and (mentioned or msg_atts):
        content = "đọc tệp đính kèm này giúp bé" if msg_atts and mentioned else "alo"

    # Deep Research — "nghiên cứu X", "research X", "bot nghiên cứu X"
    topic = research_query(content)
    if topic:
        logger.info(f"research from {message.author.display_name}: {topic!r}")
        await deliver_research(message.channel.send, topic)
        return

    # Prefix the sender so the model knows who is talking (see system prompt
    # persona); a reply carries the quoted message too, so tagging mid-argument
    # needs no re-explanation.
    att_names = ", ".join(getattr(a, "filename", "file") for a in msg_atts[:3])
    att_note = f" (đính kèm: {att_names})" if att_names else ""
    base_text = render_reply_context(target) + f"[{message.author.display_name}]: {content}{att_note}"
    turn = {"role": "user", "content": base_text[:STORE_MAX]}

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

    # Extract file/image attachments from this message (or the replied message).
    atts_to_read = msg_atts or list(getattr(target, "attachments", None) or [])
    att_text, media_parts = await extract_attachments(atts_to_read)
    live_text = base_text + ("\n\n" + att_text if att_text else "")
    stored_turn = {"role": "user", "content": live_text[:STORE_MAX]}
    live_turn: Dict[str, Any] = (
        {"role": "user", "content": [{"type": "text", "text": live_text}] + media_parts}
        if media_parts else {"role": "user", "content": live_text}
    )

    logger.info(f"ask from {message.author.display_name} (mention={mentioned}): {content[:80]!r}")
    channel_id = message.channel.id
    async with channel_locks[channel_id]:
        # Stored transcript + chatter since the last answer + this message, so
        # the model already knows what the group was talking about.
        history = await get_history(channel_id)
        prior = history + list(pending.get(channel_id, ()))
        live_messages = prior + [live_turn]
        messages = prior + [stored_turn]

        async with message.channel.typing():
            success, result = await run_tool_loop(
                live_messages, channel_id,
                guild=getattr(message, "guild", None),
                channel=message.channel,
                caller=message.author,
            )

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
    
    success, result = await run_tool_loop(
        history, ctx.channel.id,
        guild=ctx.guild, channel=ctx.channel, caller=ctx.author,
    )
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