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
import hmac
import html
import ipaddress
import socket
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from typing import List, Dict, Optional, Any
import time
from aiohttp import web

try:  # load .env.local / .env for local runs without overriding container env
    from dotenv import load_dotenv
    load_dotenv(".env.local", override=False)
    load_dotenv(".env", override=False)
except Exception:  # pragma: no cover
    pass

try:  # optional dependency-free enough module that lives next to this file
    from gemini_wm import remove_watermark as _gemini_wm_remove
except Exception:  # pragma: no cover - bot must still boot without it
    _gemini_wm_remove = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== CONFIG (Railway Variables) =====
# GEMINI_CHAT_URL: gọi thẳng gemini-web-to-api, bỏ qua forwarder trung gian.
# Đặt WEBHOOK_URL nếu vẫn muốn dùng forwarder cũ (backward-compat).
GEMINI_CHAT_URL = os.getenv(
    "GEMINI_CHAT_URL",
    "https://gemini-web-to-api-production.up.railway.app/openai/v1/chat/completions",
)
# Legacy: nếu set WEBHOOK_URL thì vẫn dùng forwarder; nếu không set thì gọi thẳng.
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
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
IMAGE_DEFAULT_ONLY = os.getenv("IMAGE_DEFAULT_ONLY", "false").strip().lower() in ("1", "true", "yes", "on")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image").strip()
GEMINI_COOKIES = os.getenv("GEMINI_COOKIES", "")
RELAY_SECRET = os.getenv("RELAY_SECRET", "").strip()
RELAY_PORT = int(os.getenv("PORT", os.getenv("RELAY_PORT", "8080")))
RELAY_ENABLED = os.getenv("RELAY_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
# Strip the visible Gemini "sparkle" watermark from generated images before they
# are uploaded to Discord (server-side alpha unblend, no browser needed).
REMOVE_GEMINI_WATERMARK = os.getenv("REMOVE_GEMINI_WATERMARK", "1").strip().lower() not in (
    "0", "false", "no", "off", "")
try:
    WM_MIN_CONFIDENCE = float(os.getenv("WM_MIN_CONFIDENCE", "0.32"))
except ValueError:
    WM_MIN_CONFIDENCE = 0.32
SKILLS_DIR = os.getenv("SKILLS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills"))
MAX_ATTACHMENT_BYTES = int(os.getenv("MAX_ATTACHMENT_BYTES", str(5 * 1024 * 1024)))
MAX_FILE_TEXT_CHARS = int(os.getenv("MAX_FILE_TEXT_CHARS", "12000"))
MUSIC_QUEUE_MAX = int(os.getenv("MUSIC_QUEUE_MAX", "50"))
YTDLP_COOKIES = os.getenv("YTDLP_COOKIES", "").strip()
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", "").strip()
YTDLP_PO_TOKEN = os.getenv("YTDLP_PO_TOKEN", "").strip()
GEMINI_VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-flash").strip()
TTS_VOICE = os.getenv("TTS_VOICE", "vi-VN-HoaiMyNeural").strip()
TTS_WEBHOOK_URL = os.getenv("TTS_WEBHOOK_URL", "").strip()
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

# Tool Use (Công cụ server, nhạc, web, ảnh, tệp, skill)
- Khi thành viên yêu cầu phát nhạc ("mở bài...", "bật nhạc...", "skip", "tạm dừng", "dừng nhạc", "xem hàng đợi", "chỉnh âm lượng", "vào voice", "rời voice"), gọi `play_music`, `music_control`, `set_volume`, `join_voice` hoặc `leave_voice`.
- Khi thành viên nhờ nói trong voice ("nói câu...", "đọc lên voice"), gọi `speak`. Khi nhờ nghe trong voice hoặc bật trò chuyện giọng nói 2 chiều, gọi `listen_voice` hoặc `voice_chat`.
- Khi thành viên nhờ đọc/giải thích ảnh hoặc meme trên kênh, gọi `read_image`.
- Khi thành viên nhờ tạo bình chọn hoặc hẹn giờ nhắc kèo, gọi `create_poll` hoặc `create_reminder`.
- Khi cần tra cứu ID role/thành viên trước khi thao tác, gọi `list_roles` / `list_members`.
- Khi cần tra cứu web mới nhất hoặc đọc nội dung một link cụ thể, gọi `web_search` / `fetch_url`.
- Khi thành viên nhờ vẽ/tạo ảnh, gọi `generate_image` (bé tự tạo và gửi ảnh thẳng lên kênh).
- Khi thành viên nhờ tạo/xuất file (code, markdown, json, csv, txt...), gọi `send_file` để gửi tệp đính kèm thật lên Discord.
- Khi cần dùng hoặc cài kỹ năng mở rộng, dùng `list_skills` / `read_skill` (hoặc `install_skill` / `remove_skill` nếu người gọi là admin).
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
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        max_inactive_connection_lifetime=60,
        statement_cache_size=0,
    )
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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_skills (
                name TEXT PRIMARY KEY,
                description TEXT NOT NULL DEFAULT '',
                instructions TEXT NOT NULL DEFAULT '',
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                author TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                message TEXT NOT NULL,
                due_at TIMESTAMPTZ NOT NULL,
                sent BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_reminders_due
            ON reminders(sent, due_at);
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


# ===== SKILL SYSTEM (disk SKILL.md / tool.py + Postgres bot_skills) =====
DISK_SKILLS: Dict[str, Dict[str, Any]] = {}
SKILL_HANDLERS: Dict[str, Any] = {}
SKILL_INLINE_BUDGET = 2500


def _clean_skill_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-_")
    return cleaned[:64]


def parse_skill_md(raw: str, fallback_name: str = "") -> Dict[str, Any]:
    """Parse YAML-frontmatter SKILL.md text into a normalized skill dict."""
    text = (raw or "").strip()
    meta: Dict[str, Any] = {}
    body = text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, flags=re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k = k.strip().lower()
            v = v.strip().strip('"').strip("'")
            if v.lower() in ("true", "yes", "on"):
                meta[k] = True
            elif v.lower() in ("false", "no", "off"):
                meta[k] = False
            else:
                meta[k] = v
        body = m.group(2).strip()
    name = _clean_skill_name(str(meta.get("name") or fallback_name))
    desc = str(meta.get("description") or "").strip()[:300]
    if not desc and body:
        first_line = next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.strip()), "")
        desc = first_line[:200]
    return {
        "name": name,
        "description": desc,
        "instructions": body[:MAX_FILE_TEXT_CHARS],
        "always_on": bool(meta.get("always_on", False)),
        "enabled": bool(meta.get("enabled", True)),
    }


def load_disk_skills(skills_dir: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Load skills/<name>/SKILL.md (and optional tool.py) from disk."""
    import importlib.util

    root = skills_dir or SKILLS_DIR
    DISK_SKILLS.clear()
    SKILL_HANDLERS.clear()
    if not os.path.isdir(root):
        return DISK_SKILLS

    for entry in sorted(os.listdir(root)):
        subdir = os.path.join(root, entry)
        skill_md = os.path.join(subdir, "SKILL.md")
        if not (os.path.isdir(subdir) and os.path.isfile(skill_md)):
            continue
        try:
            raw = open(skill_md, encoding="utf-8").read()
            parsed = parse_skill_md(raw, fallback_name=entry)
            if not parsed["name"]:
                continue
            parsed["source"] = "disk"
            parsed["author"] = "repo"
            DISK_SKILLS[parsed["name"]] = parsed

            tool_py = os.path.join(subdir, "tool.py")
            if os.path.isfile(tool_py):
                spec = importlib.util.spec_from_file_location(f"skill_{parsed['name']}", tool_py)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    schema = getattr(mod, "TOOL_SCHEMA", None)
                    runner = getattr(mod, "run", None)
                    if isinstance(schema, dict) and callable(runner):
                        tname = (schema.get("function") or {}).get("name")
                        if tname:
                            SKILL_HANDLERS[tname] = runner
                            if not any((t.get("function") or {}).get("name") == tname for t in TOOLS):
                                TOOLS.append(schema)
                            if getattr(mod, "ADMIN_ONLY", False):
                                DANGEROUS_TOOLS.add(tname)
        except Exception as e:
            logger.warning(f"failed loading skill {entry}: {e}")
    return DISK_SKILLS


async def get_all_skills() -> List[Dict[str, Any]]:
    """Return disk skills merged with DB-installed skills (DB overrides enabled/content)."""
    merged = {k: dict(v) for k, v in DISK_SKILLS.items()}
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT name, description, instructions, enabled, author "
                    "FROM bot_skills ORDER BY name ASC"
                )
            for r in rows:
                name = _clean_skill_name(r["name"])
                if not name:
                    continue
                base = merged.get(name, {})
                merged[name] = {
                    "name": name,
                    "description": r["description"] or base.get("description", ""),
                    "instructions": r["instructions"] or base.get("instructions", ""),
                    "enabled": bool(r["enabled"]),
                    "always_on": base.get("always_on", False),
                    "source": "db" if name not in merged else "disk+db",
                    "author": r["author"] or base.get("author", ""),
                }
        except Exception as e:
            logger.warning(f"get_all_skills DB read failed: {e}")
    return [merged[k] for k in sorted(merged)]


async def upsert_skill(name: str, description: str, instructions: str,
                       author: str = "", enabled: bool = True) -> Dict[str, Any]:
    clean = _clean_skill_name(name)
    if not clean:
        raise ValueError("Invalid skill name (use letters, digits, '-' or '_')")
    desc = (description or "").strip()[:300]
    body = (instructions or "").strip()[:MAX_FILE_TEXT_CHARS]
    if not body:
        raise ValueError("Skill instructions cannot be empty")
    if pool is not None:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bot_skills (name, description, instructions, enabled, author, updated_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (name) DO UPDATE SET
                    description = EXCLUDED.description,
                    instructions = EXCLUDED.instructions,
                    enabled = EXCLUDED.enabled,
                    author = EXCLUDED.author,
                    updated_at = NOW()
                """,
                clean, desc, body, enabled, (author or "")[:100],
            )
    return {"name": clean, "description": desc, "instructions": body, "enabled": enabled}


async def set_skill_enabled(name: str, enabled: bool) -> bool:
    clean = _clean_skill_name(name)
    if not clean:
        return False
    disk = DISK_SKILLS.get(clean)
    if pool is not None:
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE bot_skills SET enabled = $2, updated_at = NOW() WHERE name = $1",
                clean, enabled,
            )
            if res.endswith("1"):
                return True
            if disk is not None:
                await upsert_skill(clean, disk["description"], disk["instructions"], "repo", enabled)
                return True
    elif disk is not None:
        disk["enabled"] = enabled
        return True
    return False


async def delete_skill(name: str) -> bool:
    clean = _clean_skill_name(name)
    if not clean or pool is None:
        return False
    async with pool.acquire() as conn:
        res = await conn.execute("DELETE FROM bot_skills WHERE name = $1", clean)
    return res.endswith("1")


def render_skills(skills: List[Dict[str, Any]]) -> str:
    """Format enabled skills into a prompt section with progressive disclosure."""
    active = [s for s in skills if s.get("enabled", True)]
    if not active:
        return ""
    total_chars = sum(len(s.get("instructions") or "") for s in active)
    inline_all = total_chars <= SKILL_INLINE_BUDGET
    lines = [
        "# Installed Skills (kỹ năng đã cài cho Bé Yêu)",
        "Khi yêu cầu khớp với skill bên dưới, làm theo đúng hướng dẫn của skill đó. "
        "Nếu skill chưa hiện nội dung chi tiết, gọi `read_skill(name)` trước khi thực hiện.",
    ]
    for s in active:
        desc = f": {s['description']}" if s.get("description") else ""
        lines.append(f"- **{s['name']}**{desc}")
        if (inline_all or s.get("always_on")) and s.get("instructions"):
            lines.append(s["instructions"])
    return "\n\n".join(lines)


async def build_system_prompt() -> str:
    """Persona + remembered facts + peer bots + channels + skills + live date."""
    facts = await get_facts()
    bots = await get_peer_bots()
    channels = render_channels()
    skills = await get_all_skills()
    if not facts and not bots and not channels and not skills:
        return SYSTEM_PROMPT
    parts = [SYSTEM_PROMPT]
    now_vn = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=7))
    ).strftime("%d/%m/%Y %H:%M (UTC+7)")
    parts.append(
        f"# Thời gian hiện tại\n"
        f"Hiện tại là {now_vn}. Dữ liệu huấn luyện gốc của bạn chỉ tới mốc 03/2026. "
        f"Với mọi câu hỏi về sự kiện, tin tức, giá cả, thời tiết, phiên bản/model mới, "
        f"tỷ số hay dữ liệu sau 03/2026, BẮT BUỘC gọi `web_search` (và `fetch_url` nếu "
        f"cần đọc số liệu chi tiết từ trang) trước khi trả lời — tuyệt đối không bịa."
    )
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
    rendered_skills = render_skills(skills)
    if rendered_skills:
        parts.append(rendered_skills)
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
        "description": "Control music playback or manage the queue in this server.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string",
                       "enum": ["skip", "pause", "resume", "stop", "queue", "clear", "remove"],
                       "description": "Playback or queue action ('clear' empties queue without leaving voice; 'remove' removes queued track numbers)"},
            "index": {"type": "integer", "description": "1-based queue position to remove when action='remove'"},
            "indices": {"type": "array", "items": {"type": "integer"},
                        "description": "List of 1-based queue positions to remove when action='remove'"}},
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
    # Skill management (list/read safe for everyone; install/remove admin-only).
    {"type": "function", "function": {
        "name": "list_skills",
        "description": "List all installed skills (disk and database) with status and description.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "read_skill",
        "description": "Read the full instructions of an installed skill by name.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Skill name"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "install_skill",
        "description": "Install or update a markdown skill for the bot (persisted in database). Admin only.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Short lowercase slug, e.g. code-review, summarize-chat"},
            "description": {"type": "string", "description": "One-line description of when to use the skill"},
            "instructions": {"type": "string", "description": "Full Markdown instructions (or full SKILL.md content)"}},
            "required": ["name", "instructions"]}}},
    {"type": "function", "function": {
        "name": "remove_skill",
        "description": "Uninstall a database skill (or disable a built-in disk skill). Admin only.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Skill name to remove/disable"}},
            "required": ["name"]}}},
    # Role & member inspection / management.
    {"type": "function", "function": {
        "name": "list_roles",
        "description": "List all roles in this server with their IDs, colors, and member counts.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "list_members",
        "description": "List members in this server with their user IDs, display names, and online status.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Optional name/nickname filter"},
            "limit": {"type": "integer", "description": "Max members to return (1-100, default 30)"}}}}},
    {"type": "function", "function": {
        "name": "create_role",
        "description": "Create a new role in this server. Admin only.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Role name"},
            "color": {"type": "string", "description": "Optional hex color, e.g. #ff6600"},
            "hoist": {"type": "boolean", "description": "Display role separately in member list"},
            "mentionable": {"type": "boolean", "description": "Allow anyone to @mention this role"},
            "permissions": {"type": "array", "items": {"type": "string"},
                            "description": "Optional permission names, e.g. ['manage_messages', 'kick_members']"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "edit_role",
        "description": "Edit an existing role's name, color, hoist, or mentionable flag. Admin only.",
        "parameters": {"type": "object", "properties": {
            "role_id": {"type": "string", "description": "Discord role ID"},
            "name": {"type": "string", "description": "New role name"},
            "color": {"type": "string", "description": "New hex color, e.g. #00ccff"},
            "hoist": {"type": "boolean"},
            "mentionable": {"type": "boolean"}},
            "required": ["role_id"]}}},
    {"type": "function", "function": {
        "name": "delete_role",
        "description": "Delete a role by ID. Admin only. Destructive.",
        "parameters": {"type": "object", "properties": {
            "role_id": {"type": "string", "description": "Discord role ID to delete"}},
            "required": ["role_id"]}}},
    # Channel & message management.
    {"type": "function", "function": {
        "name": "edit_channel",
        "description": "Edit a channel's name, topic, or slowmode delay. Admin only.",
        "parameters": {"type": "object", "properties": {
            "channel_id": {"type": "string", "description": "Channel ID (defaults to current channel)"},
            "name": {"type": "string", "description": "New channel name"},
            "topic": {"type": "string", "description": "New channel topic/description"},
            "slowmode_delay": {"type": "integer", "description": "Slowmode in seconds (0-21600)"}}}}},
    {"type": "function", "function": {
        "name": "create_voice_channel",
        "description": "Create a voice channel or category in this server. Admin only.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Voice channel or category name"},
            "kind": {"type": "string", "enum": ["voice", "category"], "description": "Default: voice"},
            "category_id": {"type": "string", "description": "Optional parent category ID"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "delete_messages",
        "description": "Bulk-delete the last N messages in a channel (1-50). Admin only.",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "Number of recent messages to delete (1-50)"},
            "channel_id": {"type": "string", "description": "Optional channel ID (defaults to current channel)"}},
            "required": ["limit"]}}},
    {"type": "function", "function": {
        "name": "pin_message",
        "description": "Pin or unpin a message by ID. Admin only.",
        "parameters": {"type": "object", "properties": {
            "message_id": {"type": "string", "description": "Discord message ID (or 'last' for latest message)"},
            "channel_id": {"type": "string", "description": "Optional channel ID (defaults to current channel)"},
            "unpin": {"type": "boolean", "description": "Set true to unpin instead of pin"}},
            "required": ["message_id"]}}},
    {"type": "function", "function": {
        "name": "add_reaction",
        "description": "React to a message with an emoji (unicode like 🔥 or custom emoji).",
        "parameters": {"type": "object", "properties": {
            "emoji": {"type": "string", "description": "Emoji to add, e.g. 🔥, 👍, ❤️"},
            "message_id": {"type": "string", "description": "Message ID, or 'last' for the latest message"},
            "channel_id": {"type": "string", "description": "Optional channel ID (defaults to current channel)"}},
            "required": ["emoji"]}}},
    # Voice extras.
    {"type": "function", "function": {
        "name": "join_voice",
        "description": "Join the caller's current voice channel and stay there waiting.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "leave_voice",
        "description": "Disconnect the bot from its current voice channel and clear the music queue.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "set_volume",
        "description": "Set music playback volume percentage (0-200, default 100).",
        "parameters": {"type": "object", "properties": {
            "volume": {"type": "integer", "description": "Volume percentage 0..200"}},
            "required": ["volume"]}}},
    {"type": "function", "function": {
        "name": "speak",
        "description": "Speak text aloud in the caller's voice channel using TTS (stays in voice).",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Text to speak aloud in Vietnamese or English"}},
            "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "listen_voice",
        "description": "Listen to people speaking in the voice channel for N seconds, transcribe (STT), and reply.",
        "parameters": {"type": "object", "properties": {
            "seconds": {"type": "integer", "description": "Seconds to listen (3-20, default 8)"},
            "reply_in_voice": {"type": "boolean", "description": "Whether to speak the reply aloud via TTS (default true)"}}}}},
    {"type": "function", "function": {
        "name": "voice_chat",
        "description": "Turn continuous hands-free voice listening/replying (STT+TTS) on or off in the voice channel.",
        "parameters": {"type": "object", "properties": {
            "enabled": {"type": "boolean", "description": "true to start listening continuously, false to stop"}},
            "required": ["enabled"]}}},
    # Utilities: Poll, Reminder, Image reading.
    {"type": "function", "function": {
        "name": "create_poll",
        "description": "Create a poll in the current (or specified) channel.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "Poll question"},
            "options": {"type": "array", "items": {"type": "string"}, "description": "2 to 10 answer options"},
            "hours": {"type": "integer", "description": "Poll duration in hours (1-168, default 24)"},
            "allow_multiselect": {"type": "boolean", "description": "Allow selecting multiple options"}},
            "required": ["question", "options"]}}},
    {"type": "function", "function": {
        "name": "create_reminder",
        "description": "Schedule a persistent reminder in this channel after a delay (e.g. '15m', '2h', or minutes).",
        "parameters": {"type": "object", "properties": {
            "message": {"type": "string", "description": "What to remind about"},
            "delay": {"type": "string", "description": "Delay like '10m', '2h', '1d', or '30s'"},
            "minutes": {"type": "integer", "description": "Alternative numeric delay in minutes"}},
            "required": ["message"]}}},
    {"type": "function", "function": {
        "name": "read_image",
        "description": "Read, OCR, or analyze an image/meme from a URL, message ID, or the latest image in the channel.",
        "parameters": {"type": "object", "properties": {
            "image_url": {"type": "string", "description": "Direct http/https image URL (optional)"},
            "message_id": {"type": "string", "description": "Optional message ID containing the image"},
            "question": {"type": "string", "description": "What to analyze or ask about the image"}}}}},
]

# Tools that require admin rights.
DANGEROUS_TOOLS = {"kick", "ban", "timeout", "create_channel", "delete_channel",
                   "edit_channel", "create_voice_channel", "delete_messages", "pin_message",
                   "set_nickname", "add_role", "remove_role",
                   "create_role", "edit_role", "delete_role",
                   "install_skill", "remove_skill"}

load_disk_skills()


# ===== MUSIC PLAYER (per-guild voice queue) =====
class GuildMusicState:
    def __init__(self):
        self.queue: deque = deque(maxlen=MUSIC_QUEUE_MAX)
        self.current: Optional[Dict[str, Any]] = None
        self.lock: asyncio.Lock = asyncio.Lock()
        self.volume: float = 1.0


music_states: Dict[int, GuildMusicState] = defaultdict(GuildMusicState)
_YTDLP_COOKIE_TMP: Optional[str] = None


def _get_ytdlp_cookiefile() -> Optional[str]:
    global _YTDLP_COOKIE_TMP
    if YTDLP_COOKIES_FILE and os.path.exists(YTDLP_COOKIES_FILE):
        return YTDLP_COOKIES_FILE
    if YTDLP_COOKIES:
        if not _YTDLP_COOKIE_TMP or not os.path.exists(_YTDLP_COOKIE_TMP):
            import tempfile
            fd, path = tempfile.mkstemp(prefix="ytdlp_cookies_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(YTDLP_COOKIES)
            _YTDLP_COOKIE_TMP = path
        return _YTDLP_COOKIE_TMP
    return None


def _oembed_title(endpoint: str, url: str) -> Optional[str]:
    """Fetch a track/video title from a public oEmbed endpoint (bypasses bot checks)."""
    try:
        r = httpx.get(endpoint, params={"url": url, "format": "json"}, timeout=8)
        if r.status_code == 200:
            title = (r.json().get("title") or "").strip()
            if title:
                return title
    except Exception:
        pass
    return None


def _format_duration(seconds) -> str:
    if not seconds:
        return "?"
    sec = int(seconds)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _clean_track_query(title: str) -> str:
    s = re.sub(r"\[[^\]]*\]|\([^)]*(?:official|mv|video|audio|lyrics|visualizer|teaser|hd|4k)[^)]*\)", " ", title, flags=re.I)
    s = re.sub(r"\b(?:official\s+(?:music\s+)?(?:video|mv|audio|visualizer|teaser|lyric\s+video)|lyrics?|hd|4k)\b", " ", s, flags=re.I)
    s = s.replace("|", " ")
    return re.sub(r"\s+", " ", s).strip()


def _pick_best_entry(entries: List[Dict[str, Any]], expected_title: str = "") -> Optional[Dict[str, Any]]:
    valid = [e for e in entries if e and e.get("url")]
    if not valid:
        return None
    if not expected_title or len(valid) == 1:
        return valid[0]
    words = [w for w in re.findall(r"[a-z0-9\u00c0-\u024f\u1e00-\u1eff]+", expected_title.lower()) if len(w) >= 3]
    if not words:
        return valid[0]
    best = max(valid, key=lambda e: sum(1 for w in words if w in (e.get("title") or "").lower()))
    return best


def _extract_track_info(query: str) -> Dict[str, Any]:
    """Blocking yt-dlp extraction (run via asyncio.to_thread).

    Supports YouTube (with optional YTDLP_COOKIES / YTDLP_PO_TOKEN),
    SoundCloud URLs or `scsearch:` queries, and Spotify URLs (resolved via oEmbed).
    Falls back to SoundCloud search if YouTube fails (e.g. bot check / 403).
    """
    import yt_dlp

    q = query.strip()
    if "open.spotify.com/" in q.lower():
        resolved = _oembed_title("https://open.spotify.com/oembed", q)
        if resolved:
            q = resolved

    is_url = q.startswith(("http://", "https://"))
    is_yt_url = is_url and any(h in q.lower() for h in ("youtube.com/", "youtu.be/"))
    has_prefix = q.lower().startswith(("ytsearch", "scsearch"))
    if is_url or has_prefix:
        targets = [q]
    else:
        targets = [f"ytsearch1:{q}", f"scsearch3:{q}"]

    base_opts: Dict[str, Any] = {
        "format": "bestaudio[acodec=flac]/bestaudio[acodec=opus]/bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch",
        "source_address": "0.0.0.0",
    }
    cookiefile = _get_ytdlp_cookiefile()
    if cookiefile:
        base_opts["cookiefile"] = cookiefile
    yt_args: Dict[str, Any] = {}
    if not cookiefile:
        # tv / android_vr don't use cookies; only use them when cookiefile isn't set.
        yt_args["player_client"] = ["tv", "android_vr", "web"]
    if YTDLP_PO_TOKEN:
        yt_args["po_token"] = [f"web+{YTDLP_PO_TOKEN}"]
    if yt_args:
        base_opts["extractor_args"] = {"youtube": yt_args}

    last_err: Optional[Exception] = None
    info = None
    expected_title = "" if is_url else q
    idx = 0
    while idx < len(targets):
        target = targets[idx]
        idx += 1
        try:
            run_opts = dict(base_opts)
            if target.startswith("scsearch"):
                # Skip SoundCloud Go+ DRM tracks inside search results instead of aborting.
                run_opts["ignoreerrors"] = True
            with yt_dlp.YoutubeDL(run_opts) as ydl:
                info = ydl.extract_info(target, download=False)
            if info:
                if "entries" in info:
                    info = _pick_best_entry(info.get("entries") or [], expected_title)
                if info and info.get("url"):
                    # Verify googlevideo stream URL is actually readable with its headers
                    # (Railway datacenter IPs often get 403 on ANDROID_VR / WEB without PO token).
                    if "googlevideo.com" in info["url"]:
                        hdrs = dict(info.get("http_headers") or {})
                        hdrs["Range"] = "bytes=0-1"
                        probe = httpx.get(info["url"], headers=hdrs, follow_redirects=True, timeout=8)
                        if probe.status_code == 403:
                            raise ValueError(f"googlevideo returned 403 for {info.get('title') or target}")
                    break
        except Exception as e:
            last_err = e
            yt_title = info.get("title") if isinstance(info, dict) else None
            info = None
            # If a direct YouTube URL hits bot-check or 403 on datacenter IP, resolve its title
            # via YouTube oEmbed and fall back to SoundCloud search automatically.
            if is_yt_url and target == q:
                yt_title = yt_title or _oembed_title("https://www.youtube.com/oembed", q)
                if yt_title:
                    clean_title = _clean_track_query(yt_title) or yt_title
                    expected_title = clean_title
                    targets.append(f"scsearch5:{clean_title}")

    if not info or not info.get("url"):
        raise ValueError(str(last_err) if last_err else "No playable audio stream URL returned")

    return {
        "title": info.get("title") or q,
        "stream_url": info["url"],
        "http_headers": dict(info.get("http_headers") or {}),
        "webpage_url": info.get("webpage_url") or (q if is_url else ""),
        "duration": info.get("duration") or 0,
    }


def _create_audio_source(stream_url: str, volume: float = 1.0, http_headers: Optional[Dict[str, str]] = None):
    is_hls = ".m3u8" in (stream_url or "").lower()
    is_http = (stream_url or "").startswith(("http://", "https://"))
    parts = []
    if is_http and not is_hls:
        parts.append("-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5")
    if is_http and http_headers:
        ua = http_headers.get("User-Agent")
        if ua:
            safe_ua = ua.replace('"', "")
            parts.append(f'-user_agent "{safe_ua}"')
    before = " ".join(parts) if parts else None
    raw = discord.FFmpegPCMAudio(
        stream_url,
        before_options=before,
        options="-vn",
    )
    return discord.PCMVolumeTransformer(raw, volume=max(0.0, min(float(volume), 2.0)))


def _tune_voice_encoder(vc) -> None:
    """Tune Opus encoder to maximum Discord fidelity (fullband, music signal, channel-max bitrate)."""
    enc = getattr(vc, "encoder", None)
    if enc is None:
        return
    try:
        ch_bps = getattr(getattr(vc, "channel", None), "bitrate", 96000) or 96000
        kbps = max(128, min(int(ch_bps) // 1000, 384))
        if hasattr(enc, "set_bitrate"):
            enc.set_bitrate(kbps)
        if hasattr(enc, "set_signal_type"):
            enc.set_signal_type("music")
        if hasattr(enc, "set_bandwidth"):
            enc.set_bandwidth("full")
        if hasattr(enc, "set_fec"):
            enc.set_fec(True)
        if hasattr(enc, "set_expected_packet_loss_percent"):
            enc.set_expected_packet_loss_percent(0.05)
    except Exception as e:
        logger.debug(f"Opus encoder tune skipped: {e}")


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
            hdrs = nxt.get("http_headers")
            source = _create_audio_source(nxt["stream_url"], http_headers=hdrs) if hdrs else _create_audio_source(nxt["stream_url"])
            if state.volume != 1.0 and hasattr(source, "volume"):
                source.volume = state.volume
            loop = getattr(bot, "loop", None) or asyncio.get_running_loop()

            def _after(err):
                if err:
                    logger.warning(f"voice playback error in {guild.id}: {err}")
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(_play_next_track(guild))
                )

            vc.play(source, after=_after)
            _tune_voice_encoder(vc)
        except Exception as e:
            logger.warning(f"vc.play failed in {guild.id}: {e}")
            state.current = None
            asyncio.create_task(_play_next_track(guild))


async def _ensure_voice(guild: discord.Guild, caller: Optional[discord.Member]):
    """Connect to (or move to) the caller's voice channel and verify connection."""
    voice_state = getattr(caller, "voice", None)
    voice_ch = getattr(voice_state, "channel", None)
    if voice_ch is None:
        return None, "ERROR: Caller is not in a voice channel. Ask them to join a voice channel first."

    vc = getattr(guild, "voice_client", None)
    if vc is not None and not getattr(vc, "is_connected", lambda: True)():
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
        vc = None

    if vc is None:
        vc = await voice_ch.connect()
    elif getattr(vc, "channel", None) != voice_ch:
        await vc.move_to(voice_ch)

    if vc is None or not getattr(vc, "is_connected", lambda: True)():
        return None, "ERROR: Could not establish voice connection."
    me_voice = getattr(getattr(guild, "me", None), "voice", None)
    logger.info(
        f"Voice ready in #{getattr(vc.channel, 'name', '?')} "
        f"(opus={discord.opus.is_loaded()}, mute={getattr(me_voice, 'mute', None)}, "
        f"self_mute={getattr(me_voice, 'self_mute', None)}, "
        f"dave={getattr(vc, 'dave_protocol_version', None)})"
    )
    return vc, ""


async def _handle_play_music(guild: discord.Guild, caller: Optional[discord.Member],
                             query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "ERROR: missing song name or URL"
    vc, err = await _ensure_voice(guild, caller)
    if vc is None:
        return err
    voice_ch = vc.channel

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
    me_voice = getattr(getattr(guild, "me", None), "voice", None)
    mute_note = " (Lưu ý: bot đang bị Server Mute trong kênh thoại, hãy unmute bot!)" if getattr(me_voice, "mute", False) else ""
    return (f"Now playing in #{voice_ch.name}: {track['title']} "
            f"({_format_duration(track['duration'])}){mute_note}")


async def _handle_music_control(guild: discord.Guild, action: str,
                                index: Any = None, indices: Any = None) -> str:
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

    if action == "clear":
        n = len(state.queue)
        state.queue.clear()
        return f"Cleared {n} queued track(s) (kept current playback and stayed in voice)."

    if action == "remove":
        raw_idxs: List[int] = []
        if isinstance(indices, list):
            for x in indices:
                try:
                    raw_idxs.append(int(x))
                except Exception:
                    pass
        if index is not None:
            try:
                raw_idxs.append(int(index))
            except Exception:
                pass
        q_list = list(state.queue)
        removed = []
        for pos in sorted(set(raw_idxs), reverse=True):
            if 1 <= pos <= len(q_list):
                removed.append(q_list.pop(pos - 1)["title"])
        state.queue = deque(q_list, maxlen=MUSIC_QUEUE_MAX)
        if not removed:
            return "ERROR: invalid queue index or queue is empty."
        return f"Removed {len(removed)} track(s) from queue: {', '.join(reversed(removed))}"

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


async def _handle_leave_voice(guild: discord.Guild) -> str:
    old = voice_chat_tasks.pop(guild.id, None)
    if old is not None and not old.done():
        old.cancel()
    state = music_states[guild.id]
    state.queue.clear()
    state.current = None
    vc = getattr(guild, "voice_client", None)
    if vc is None:
        return "Bot is not currently in a voice channel."
    if vc.is_playing() or vc.is_paused():
        vc.stop()
    await vc.disconnect()
    return "Left voice channel and cleared music queue."


async def _handle_set_volume(guild: discord.Guild, volume_pct: Any) -> str:
    try:
        pct = int(volume_pct)
    except Exception:
        return "ERROR: volume must be an integer between 0 and 200"
    pct = max(0, min(pct, 200))
    state = music_states[guild.id]
    state.volume = pct / 100.0
    vc = getattr(guild, "voice_client", None)
    src = getattr(vc, "source", None) if vc is not None else None
    if src is not None and hasattr(src, "volume"):
        src.volume = state.volume
    return f"Set music volume to {pct}%."


# ===== VOICE TTS & STT (Kokoro-VI / Edge-TTS + Gemini Audio) =====
voice_chat_tasks: Dict[int, asyncio.Task] = {}


def _clean_tts_text(text: str) -> str:
    s = re.sub(r"https?://\S+", "", text or "")
    s = re.sub(r"<[@#][!&]?\d+>", "", s)
    s = re.sub(r"[`*_~>|#]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:600]


async def _synthesize_tts(text: str) -> tuple[Optional[str], str]:
    """Generate a temporary audio file for TTS via TTS_WEBHOOK_URL (kokoro-vi) or edge-tts."""
    import tempfile
    clean = _clean_tts_text(text)
    if not clean:
        return None, "empty text"

    if TTS_WEBHOOK_URL:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(TTS_WEBHOOK_URL, json={"text": clean})
            if r.status_code == 200 and r.content:
                ext = ".wav" if "wav" in (r.headers.get("content-type") or "").lower() else ".mp3"
                fd, path = tempfile.mkstemp(prefix="tts_", suffix=ext)
                with os.fdopen(fd, "wb") as f:
                    f.write(r.content)
                return path, ""
        except Exception as e:
            logger.warning(f"TTS_WEBHOOK_URL failed, falling back to edge-tts: {e}")

    try:
        import edge_tts
        fd, path = tempfile.mkstemp(prefix="tts_", suffix=".mp3")
        os.close(fd)
        comm = edge_tts.Communicate(clean, TTS_VOICE)
        await comm.save(path)
        if os.path.getsize(path) > 0:
            return path, ""
        return None, "edge-tts produced empty audio"
    except Exception as e:
        return None, str(e)


async def _speak_in_voice(guild: discord.Guild, caller: Optional[discord.Member], text: str) -> str:
    vc, err = await _ensure_voice(guild, caller) if caller is not None else (getattr(guild, "voice_client", None), "")
    if vc is None:
        return err or "ERROR: Bot is not in a voice channel."

    path, tts_err = await _synthesize_tts(text)
    if not path:
        return f"ERROR: TTS failed ({tts_err})"

    state = music_states[guild.id]
    if vc.is_playing() or vc.is_paused():
        vc.stop()

    done_evt = asyncio.Event()
    loop = asyncio.get_running_loop()
    src = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(path, options="-vn"),
        volume=state.volume,
    )

    def _after(_err):
        try:
            os.remove(path)
        except OSError:
            pass
        loop.call_soon_threadsafe(done_evt.set)

    vc.play(src, after=_after)
    try:
        await asyncio.wait_for(done_evt.wait(), timeout=60)
    except asyncio.TimeoutError:
        pass
    return f"Spoke in #{getattr(vc.channel, 'name', 'voice')}: {_clean_tts_text(text)[:100]}"


def _has_speech(wav_bytes: bytes, min_rms: int = 250) -> bool:
    """Fast PCM16 WAV energy check so silent chunks never hit the STT API."""
    if not wav_bytes or len(wav_bytes) <= 46:
        return False
    pcm = wav_bytes[44:]
    if len(pcm) % 2:
        pcm = pcm[:-1]
    try:
        import numpy as np
        samples = np.frombuffer(pcm, dtype=np.int16)
        if samples.size == 0:
            return False
        rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
        return rms >= min_rms
    except Exception:
        import struct
        step = max(1, (len(pcm) // 2) // 2000)
        vals = [s[0] for s in struct.iter_unpack("<h", pcm)][::step]
        if not vals:
            return False
        rms = (sum(v * v for v in vals) / len(vals)) ** 0.5
        return rms >= min_rms


async def _transcribe_wav(wav_bytes: bytes) -> str:
    if not GEMINI_API_KEY:
        return ""
    b64 = base64.b64encode(wav_bytes).decode("ascii")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_VISION_MODEL}:generateContent"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                endpoint,
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json={
                    "contents": [{
                        "parts": [
                            {"text": "Chép chính xác lời nói trong đoạn audio này. Chỉ trả về câu nói; nếu chỉ là tiếng ồn hoặc im lặng thì trả về rỗng."},
                            {"inline_data": {"mime_type": "audio/wav", "data": b64}},
                        ]
                    }]
                },
            )
        if r.status_code == 200:
            cands = r.json().get("candidates") or []
            parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
            return " ".join(p.get("text", "") for p in parts if p.get("text")).strip()
    except Exception as e:
        logger.warning(f"STT transcribe failed: {e}")
    return ""


async def _record_voice_once(vc, seconds: int = 8) -> Dict[int, bytes]:
    """Record audio per-user from Discord VoiceClient using WaveSink."""
    if not hasattr(discord, "sinks"):
        return {}
    done_evt = asyncio.Event()
    recorded: Dict[int, bytes] = {}
    loop = asyncio.get_running_loop()

    async def _finished_cb(sink, *_args):
        for uid, audio in (getattr(sink, "audio_data", {}) or {}).items():
            try:
                audio.file.seek(0)
                recorded[int(uid)] = audio.file.read()
            except Exception:
                pass
        loop.call_soon_threadsafe(done_evt.set)

    sink = discord.sinks.WaveSink()
    vc.start_recording(sink, _finished_cb)
    await asyncio.sleep(max(3, min(int(seconds), 20)))
    try:
        vc.stop_recording()
    except Exception:
        pass
    try:
        await asyncio.wait_for(done_evt.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass
    return recorded


async def _handle_listen_voice(guild: discord.Guild, channel, caller: Optional[discord.Member],
                               seconds: int = 8, reply_in_voice: bool = True,
                               require_keyword: bool = False) -> str:
    if not GEMINI_API_KEY:
        return "ERROR: GEMINI_API_KEY is required for voice STT."
    vc = getattr(guild, "voice_client", None)
    if vc is None and caller is not None:
        vc, err = await _ensure_voice(guild, caller)
        if vc is None:
            return err
    if vc is None or not getattr(vc, "is_connected", lambda: True)():
        return "ERROR: Bot is not in a voice channel."
    if getattr(vc, "recording", False):
        return "Already recording in this voice channel."

    clips = await _record_voice_once(vc, seconds=seconds)
    transcripts = []
    for uid, wav in clips.items():
        if not _has_speech(wav):
            continue
        text = await _transcribe_wav(wav)
        if not text:
            continue
        member = guild.get_member(uid) or await resolve_member(guild, str(uid))
        who = getattr(member, "display_name", str(uid))
        transcripts.append((who, text, member))

    if not transcripts:
        return "Không nghe thấy ai nói gì trong kênh thoại."

    summary_lines = [f"🎙️ **{who}:** {txt}" for who, txt, _ in transcripts]
    if channel is not None and hasattr(channel, "send"):
        await channel.send("\n".join(summary_lines)[:1900])

    if not reply_in_voice:
        return "\n".join(summary_lines)

    combined = "\n".join(f"[{who}]: {txt}" for who, txt, _ in transcripts)
    if require_keyword and not any(kw in combined.lower() for kw in KEYWORDS):
        return "\n".join(summary_lines)

    hist = await get_history(channel.id) if channel is not None else []
    hist.append({"role": "user", "content": combined})
    ok, res = await run_tool_loop(
        hist,
        channel.id if channel is not None else guild.id,
        guild=guild,
        channel=channel,
        caller=transcripts[0][2] or caller,
    )
    if ok:
        ans = (res.get("answer") or "").strip()
        if ans:
            if channel is not None:
                await persist_turn(channel.id, hist, ans)
                if hasattr(channel, "send"):
                    for chunk in chunk_text(ans, 1990):
                        await channel.send(chunk)
            await _speak_in_voice(guild, None, ans)
            return f"Heard & replied in voice: {ans[:120]}"
    return "\n".join(summary_lines)


async def _voice_chat_loop(guild: discord.Guild, channel, caller: Optional[discord.Member]) -> None:
    try:
        while not bot.is_closed():
            vc = getattr(guild, "voice_client", None)
            if vc is None or not getattr(vc, "is_connected", lambda: False)():
                break
            if not vc.is_playing() and not vc.is_paused() and not getattr(vc, "recording", False):
                await _handle_listen_voice(
                    guild, channel, caller,
                    seconds=6, reply_in_voice=True, require_keyword=True,
                )
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        voice_chat_tasks.pop(guild.id, None)


async def _handle_voice_chat(guild: discord.Guild, channel, caller: Optional[discord.Member], enabled: bool) -> str:
    old = voice_chat_tasks.pop(guild.id, None)
    if old is not None and not old.done():
        old.cancel()
    if not enabled:
        return "Turned off continuous voice chat (STT+TTS)."
    vc, err = await _ensure_voice(guild, caller)
    if vc is None:
        return err
    voice_chat_tasks[guild.id] = asyncio.create_task(_voice_chat_loop(guild, channel, caller))
    return f"Started continuous voice chat in #{getattr(vc.channel, 'name', 'voice')}. Say '{KEYWORDS[0]} ...' to talk to me!"


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


def _ddgs_search(query: str, limit: int) -> List[Dict[str, str]]:
    """Run blocking ddgs text + news search in a worker thread."""
    from ddgs import DDGS

    out: List[Dict[str, str]] = []
    seen = set()
    client = DDGS()
    for method in ("text", "news"):
        try:
            rows = list(getattr(client, method)(query, max_results=limit))
        except Exception:
            rows = []
        for r in rows:
            link = (r.get("href") or r.get("url") or "").strip()
            title = (r.get("title") or "").strip()
            desc = (r.get("body") or "").strip()
            pub = (r.get("date") or "").strip()
            if title and link and link not in seen:
                seen.add(link)
                out.append({"title": title, "link": link, "desc": desc[:240], "pub": pub})
            if len(out) >= limit:
                return out
    return out


def _google_news_rss_url(query: str) -> str:
    q = urllib.parse.quote(query)
    if any(ord(ch) > 127 for ch in query):
        return f"https://news.google.com/rss/search?q={q}&hl=vi&gl=VN&ceid=VN:vi"
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


async def _web_search(query: str, max_results: int = 5) -> str:
    query = (query or "").strip()
    if not query:
        return "ERROR: empty search query"
    limit = max(1, min(int(max_results or 5), 8))
    results: List[Dict[str, str]] = []
    seen_urls = set()

    # 1. Direct web & news search via ddgs (returns direct publisher URLs for fetch_url).
    try:
        for item in await asyncio.to_thread(_ddgs_search, query, limit):
            if item["link"] not in seen_urls:
                seen_urls.add(item["link"])
                results.append(item)
    except Exception as e:
        logger.warning(f"ddgs search failed: {e}")

    # 2. Live Google News RSS (fresh dated headlines, resilient fallback).
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"}
    gnews_url = _google_news_rss_url(query)
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=headers) as client:
            r = await client.get(gnews_url)
            if r.status_code == 200 and "<item>" in r.text:
                for item in _parse_rss_items(r.text, max(3, limit)):
                    if item["link"] not in seen_urls:
                        seen_urls.add(item["link"])
                        results.append(item)
                        if len(results) >= limit + 3:
                            break
    except Exception as e:
        logger.warning(f"google news rss failed: {e}")

    if not results:
        return "No web search results found."
    lines = []
    for i, item in enumerate(results[:limit + 3], 1):
        meta = f" ({item['pub']})" if item["pub"] else ""
        snippet = f" — {item['desc']}" if item["desc"] else ""
        lines.append(f"{i}. {item['title']}{meta}\n   URL: {item['link']}{snippet}")
    return "\n".join(lines)


async def _resolve_google_news_url(client: httpx.AsyncClient, raw_url: str) -> str:
    """Decode a news.google.com/rss/articles/... wrapper link to its real publisher URL."""
    parsed = urllib.parse.urlparse(raw_url)
    if (parsed.hostname or "").lower() != "news.google.com":
        return raw_url
    if "/articles/" not in parsed.path and "/read/" not in parsed.path:
        return raw_url
    try:
        hdrs = _fetch_headers_for(raw_url)
        page = await client.get(raw_url, follow_redirects=True, headers=hdrs)
        if page.status_code != 200:
            return raw_url
        m_id = re.search(r'data-n-a-id="([^"]+)"', page.text)
        m_ts = re.search(r'data-n-a-ts="([^"]+)"', page.text)
        m_sg = re.search(r'data-n-a-sg="([^"]+)"', page.text)
        if not (m_id and m_ts and m_sg):
            return raw_url
        payload = [
            "Fbv4je",
            f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{m_id.group(1)}",{m_ts.group(1)},"{m_sg.group(1)}"]',
        ]
        body = "f.req=" + urllib.parse.quote(json.dumps([[payload]]))
        bresp = await client.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            content=body,
            headers={
                **hdrs,
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Referer": "https://news.google.com/",
            },
        )
        m_url = re.search(r'garturlres\\",\\"(https?://[^\\"]+)', bresp.text)
        if m_url:
            return m_url.group(1).encode("utf-8").decode("unicode_escape")
    except Exception as e:
        logger.warning(f"google news redirect decode failed: {e}")
    return raw_url


def _fetch_headers_for(url: str) -> Dict[str, str]:
    """Use a descriptive bot User-Agent for Wikimedia (required by its robot policy)."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host.endswith(("wikipedia.org", "wiktionary.org", "wikimedia.org", "wikidata.org")):
        return {"User-Agent": "BeYeuBot/1.0 (https://github.com/atn1108/gemini-discord-bot)"}
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    }


async def _fetch_url(url: str) -> str:
    cur = (url or "").strip()
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        cur = await _resolve_google_news_url(client, cur)
        resp = None
        for _ in range(5):
            ok, reason = _is_safe_public_url(cur)
            if not ok:
                return f"ERROR: blocked URL ({reason})"
            resp = await client.get(cur, headers=_fetch_headers_for(cur))
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

def _strip_gemini_watermark(raw: bytes) -> bytes:
    """Remove the Gemini sparkle watermark from image bytes before upload.

    Same maths as the browser extensions: the watermark was composited as
    px = src*(1-a) + a*255, so it is reversed with (px - a*255)/(1-a).
    Detection is template based; images without a watermark are untouched.
    """
    if not raw or not REMOVE_GEMINI_WATERMARK or _gemini_wm_remove is None:
        return raw
    try:
        cleaned, info = _gemini_wm_remove(raw, min_confidence=WM_MIN_CONFIDENCE)
    except Exception as e:
        logger.warning(f"watermark removal skipped: {e}")
        return raw
    if info.get("applied"):
        logger.info(
            "gemini watermark removed (size=%s pos=%s conf=%s gain=%s)",
            info.get("size"), info.get("position"), info.get("confidence"),
            info.get("alpha_gain"),
        )
        return cleaned
    logger.info(f"gemini watermark not detected ({info.get('reason')})")
    return raw

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


async def _generate_via_gemini_api(prompt: str, aspect_ratio: str = "") -> Optional[tuple[bytes, str]]:
    """Generate image bytes directly via official Google AI Studio Gemini API."""
    if not GEMINI_API_KEY:
        return None
    models_to_try = [GEMINI_IMAGE_MODEL]
    for fallback_m in ("gemini-2.5-flash-image", "gemini-2.0-flash-exp-image-generation"):
        if fallback_m not in models_to_try:
            models_to_try.append(fallback_m)

    ar = aspect_ratio.strip()
    valid_ratios = {"1:1", "3:4", "4:3", "9:16", "16:9"}
    async with httpx.AsyncClient(timeout=IMAGE_GEN_TIMEOUT) as client:
        for model in models_to_try:
            gen_cfg: Dict[str, Any] = {"responseModalities": ["TEXT", "IMAGE"]}
            if ar in valid_ratios and "2.0-flash-exp" not in model:
                gen_cfg["imageConfig"] = {"aspectRatio": ar}
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            try:
                r = await client.post(
                    url,
                    headers={"x-goog-api-key": GEMINI_API_KEY},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": gen_cfg,
                    },
                )
                if r.status_code == 404:
                    continue
                if r.status_code != 200:
                    logger.warning(f"Gemini API image ({model}) HTTP {r.status_code}: {r.text[:200]}")
                    continue
                data = r.json()
                for cand in data.get("candidates") or []:
                    for part in (cand.get("content") or {}).get("parts") or []:
                        inline = part.get("inlineData") or part.get("inline_data")
                        if inline and inline.get("data"):
                            mime = (inline.get("mimeType") or inline.get("mime_type") or "image/png").lower()
                            ext = ".jpg" if "jpeg" in mime or "jpg" in mime else (".webp" if "webp" in mime else ".png")
                            return base64.b64decode(inline["data"]), ext
            except Exception as e:
                logger.warning(f"Gemini API image ({model}) failed: {e}")
    return None


async def _generate_via_pollinations(client: httpx.AsyncClient, prompt: str,
                                     aspect_ratio: str = "") -> tuple[Optional[bytes], str]:
    """Free keyless image generation fallback when Gemini API / CDN are blocked."""
    dims = {"16:9": (1280, 720), "9:16": (720, 1280), "4:3": (1024, 768), "3:4": (768, 1024)}
    w, h = dims.get(aspect_ratio.strip(), (1024, 1024))
    url = (
        f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}"
        f"?width={w}&height={h}&nologo=true"
    )
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"}
    last_err = ""
    for attempt in range(2):
        try:
            r = await client.get(url, headers=headers, follow_redirects=True, timeout=60)
            if r.status_code == 200 and (r.headers.get("content-type") or "").startswith("image/") and r.content:
                return r.content, ""
            last_err = f"HTTP {r.status_code}"
            if r.status_code in (502, 503, 504) and attempt == 0:
                await asyncio.sleep(2)
                continue
            break
        except Exception as e:
            last_err = str(e)
            logger.warning(f"Pollinations image fallback failed: {e}")
            break
    return None, last_err


class RelayHub:
    """WebSocket bridge between Railway bot and user's Chrome extension relay."""

    def __init__(self) -> None:
        self._ws: Optional[web.WebSocketResponse] = None
        self._jobs: Dict[str, asyncio.Future] = {}
        self._meta: Dict[str, Any] = {}

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @staticmethod
    def _authorized(request: web.Request) -> bool:
        if not RELAY_SECRET:
            return False
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else request.query.get("token", "").strip()
        return bool(token) and hmac.compare_digest(token, RELAY_SECRET)

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "relay_connected": self.is_connected,
            "relay_configured": bool(RELAY_SECRET),
            "extension": self._meta,
        })

    async def handle_auth_check(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response(
                {"ok": False, "error": "unauthorized" if RELAY_SECRET else "relay_secret_missing"},
                status=401 if RELAY_SECRET else 503,
            )
        return web.json_response({"ok": True, "relay_connected": self.is_connected})

    async def handle_ws(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            logger.warning(f"Chrome relay rejected ({401 if RELAY_SECRET else 503})")
            return web.Response(status=401 if RELAY_SECRET else 503, text="unauthorized")
        ws = web.WebSocketResponse(max_msg_size=25 * 1024 * 1024, heartbeat=25.0)
        await ws.prepare(request)
        prev = self._ws
        self._ws = ws
        if prev is not None and prev is not ws and not prev.closed:
            await prev.close(code=1000, message=b"replaced")
        logger.info("Chrome relay connected")
        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                mtype = data.get("type")
                if mtype == "ping":
                    await ws.send_json({"type": "pong", "ts": data.get("ts")})
                elif mtype == "hello":
                    self._meta = {
                        "version": str(data.get("version") or ""),
                        "temp_chat": bool(data.get("temp_chat", True)),
                    }
                elif mtype == "image_result":
                    fut = self._jobs.pop(str(data.get("id") or ""), None)
                    if fut is not None and not fut.done():
                        fut.set_result(data)
        finally:
            if self._ws is ws:
                self._ws = None
                for jid, fut in list(self._jobs.items()):
                    if not fut.done():
                        fut.set_result({"ok": False, "error": "relay disconnected"})
                    self._jobs.pop(jid, None)
                logger.info(f"Chrome relay disconnected (code={ws.close_code}, err={ws.exception()})")
        return ws

    async def generate_image(self, prompt: str, aspect_ratio: str = "", timeout: float = 120) -> tuple[Optional[bytes], str, str]:
        if not self.is_connected or self._ws is None:
            return None, "", "relay offline"
        job_id = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._jobs[job_id] = fut
        try:
            await self._ws.send_json({
                "type": "generate_image",
                "id": job_id,
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
            })
            res = await asyncio.wait_for(fut, timeout=timeout)
        except Exception as e:
            self._jobs.pop(job_id, None)
            return None, "", str(e)
        if not res.get("ok"):
            return None, "", str(res.get("error") or "relay failed")
        b64 = res.get("b64") or ""
        if not b64:
            return None, "", "relay returned empty image"
        try:
            raw = base64.b64decode(b64)
        except Exception as e:
            return None, "", f"invalid base64: {e}"
        mime = str(res.get("mime") or "image/png").lower()
        ext = ".jpg" if ("jpeg" in mime or "jpg" in mime) else (".webp" if "webp" in mime else ".png")
        return raw, ext, ""


relay_hub = RelayHub()


async def _generate_and_send_image(channel, prompt: str, aspect_ratio: str = "") -> str:
    prompt = (prompt or "").strip()
    if not prompt:
        return "ERROR: empty image prompt"

    full_prompt = f"{prompt} (aspect ratio {aspect_ratio.strip()})" if aspect_ratio.strip() else prompt

    # 0. Chrome extension relay running on user's PC (same IP + full cookie jar).
    relay_err = ""
    if relay_hub.is_connected:
        relay_raw, relay_ext, relay_err = await relay_hub.generate_image(
            full_prompt, aspect_ratio, timeout=IMAGE_GEN_TIMEOUT
        )
        if relay_raw:
            relay_raw = _strip_gemini_watermark(relay_raw)
            await channel.send(file=discord.File(io.BytesIO(relay_raw), filename=f"image{relay_ext}"))
            return f"Generated and sent image via Chrome relay to #{getattr(channel, 'name', getattr(channel, 'id', 'channel'))}."
        logger.warning(f"Chrome relay image failed: {relay_err}")

    # 1. Official Gemini API (skipped when IMAGE_DEFAULT_ONLY forces gemini-web-to-api).
    if not IMAGE_DEFAULT_ONLY:
        api_img = await _generate_via_gemini_api(prompt, aspect_ratio)
        if api_img is not None:
            raw, ext = api_img
            raw = _strip_gemini_watermark(raw)
            await channel.send(file=discord.File(io.BytesIO(raw), filename=f"image{ext}"))
            return f"Generated and sent image via Gemini API to #{getattr(channel, 'name', getattr(channel, 'id', 'channel'))}."

    async with httpx.AsyncClient(timeout=IMAGE_GEN_TIMEOUT) as client:
        # 2. Default generator: gemini-web-to-api b64_json.
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
                    raw = _strip_gemini_watermark(base64.b64decode(b64))
                    await channel.send(file=discord.File(io.BytesIO(raw), filename="image.png"))
                    return f"Generated and sent image to #{getattr(channel, 'name', getattr(channel, 'id', 'channel'))}."
        except Exception as e:
            logger.warning(f"image b64_json failed: {e}")

        # 3. Default generator URL mode + direct fetch attempt.
        url = ""
        try:
            r2 = await client.post(IMAGE_GEN_URL, json={
                "prompt": full_prompt,
                "model": "gemini-auto",
                "n": 1,
                "response_format": "url",
            })
            if r2.status_code == 200:
                url = ((r2.json().get("data") or [{}])[0]).get("url") or ""
                if url:
                    raw = await _download_image_bytes(url)
                    if raw:
                        raw = _strip_gemini_watermark(raw)
                        await channel.send(file=discord.File(io.BytesIO(raw), filename="image.png"))
                        return f"Generated and sent image to #{getattr(channel, 'name', getattr(channel, 'id', 'channel'))}."
        except Exception as e:
            logger.warning(f"image url fallback failed: {e}")

        # 4. Final fallback: Pollinations (disabled when IMAGE_DEFAULT_ONLY is on).
        poly_err = ""
        if not IMAGE_DEFAULT_ONLY:
            poly, poly_err = await _generate_via_pollinations(client, prompt, aspect_ratio)
            if poly:
                poly = _strip_gemini_watermark(poly)
                await channel.send(file=discord.File(io.BytesIO(poly), filename="image.jpg"))
                return f"Generated and sent image via Pollinations fallback to #{getattr(channel, 'name', getattr(channel, 'id', 'channel'))}."

        if not url:
            if relay_err:
                detail = f"relay: {relay_err}"
            elif not relay_hub.is_connected:
                detail = poly_err or "Chrome relay offline & no image returned"
            else:
                detail = poly_err or "no image returned"
            return f"ERROR: image generator unavailable ({detail})"
        if IMAGE_DEFAULT_ONLY:
            return (f"IMAGE_FETCH_BLOCKED: Default Gemini Web generated the image at {url} "
                    f"but Google CDN returned HTTP 403 when fetching bytes (fallback is currently disabled via IMAGE_DEFAULT_ONLY).")
        hint = (
            " Set GEMINI_API_KEY in Railway to bypass the Google CDN 403."
            if not GEMINI_API_KEY else ""
        )
        poly_note = f" Pollinations fallback also failed ({poly_err})." if poly_err else ""
        return (f"IMAGE_FETCH_BLOCKED: Gemini generated the image at {url} "
                f"but Google CDN returned HTTP 403 when fetching the file bytes directly.{poly_note}{hint} "
                f"Share this link with the user and explain honestly.")


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


def _parse_hex_color(raw: Any) -> Optional[discord.Color]:
    s = str(raw or "").strip().lstrip("#")
    if not s:
        return None
    if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
        raise ValueError(f"invalid hex color '{raw}' (expected #RRGGBB)")
    return discord.Color(int(s, 16))


def _resolve_target_channel(guild: discord.Guild, default_channel, channel_id: Any):
    raw = str(channel_id or "").strip()
    if not raw:
        return default_channel
    if raw.isdigit():
        ch = guild.get_channel(int(raw))
        if ch is not None:
            return ch
    return None


async def _resolve_target_message(channel, message_id: Any):
    raw = str(message_id or "").strip().lower()
    if not raw or raw == "last":
        async for m in channel.history(limit=1):
            return m
        return None
    if raw.isdigit():
        return await channel.fetch_message(int(raw))
    return None


def _parse_delay_seconds(delay: str = "", minutes: Any = None) -> int:
    if minutes is not None and str(minutes).strip() != "":
        m = int(minutes)
        return max(10, min(m * 60, 30 * 86400))
    s = (delay or "").strip().lower()
    if not s:
        return 0
    total = 0
    for val, unit in re.findall(r"(\d+)\s*(s|sec|m|min|phút|phut|h|hr|giờ|gio|d|day|ngày|ngay)", s):
        n = int(val)
        if unit in ("s", "sec"):
            total += n
        elif unit in ("m", "min", "phút", "phut"):
            total += n * 60
        elif unit in ("h", "hr", "giờ", "gio"):
            total += n * 3600
        elif unit in ("d", "day", "ngày", "ngay"):
            total += n * 86400
    if total == 0 and s.isdigit():
        total = int(s) * 60
    return max(0, min(total, 30 * 86400))


async def _handle_create_poll(channel, args: Dict) -> str:
    question = (args.get("question") or "").strip()[:300]
    options = [str(o).strip()[:55] for o in (args.get("options") or []) if str(o).strip()][:10]
    if not question or len(options) < 2:
        return "ERROR: poll requires a question and at least 2 options"
    hours = max(1, min(int(args.get("hours") or 24), 168))
    allow_multi = bool(args.get("allow_multiselect", False))

    if hasattr(discord, "Poll") and hasattr(discord, "PollAnswer"):
        try:
            poll = discord.Poll(
                question=question,
                duration=datetime.timedelta(hours=hours),
                allow_multiselect=allow_multi,
            )
            for opt in options:
                poll.add_answer(text=opt)
            await channel.send(poll=poll)
            return f"Created poll '{question}' with {len(options)} options ({hours}h)."
        except Exception as e:
            logger.warning(f"native poll failed, falling back to reaction poll: {e}")

    num_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines = [f"📊 **{question}**"]
    for idx, opt in enumerate(options):
        lines.append(f"{num_emojis[idx]} {opt}")
    msg = await channel.send("\n".join(lines))
    for idx in range(len(options)):
        try:
            await msg.add_reaction(num_emojis[idx])
        except Exception:
            break
    return f"Created reaction poll '{question}' with {len(options)} options."


async def _handle_create_reminder(guild: discord.Guild, channel, caller: Optional[discord.Member], args: Dict) -> str:
    msg = (args.get("message") or "").strip()[:500]
    if not msg:
        return "ERROR: reminder message is required"
    secs = _parse_delay_seconds(args.get("delay") or "", args.get("minutes"))
    if secs < 10:
        return "ERROR: invalid or too-short delay (e.g. '10m', '2h', or minutes >= 1)"
    if pool is None:
        return "ERROR: database is not initialized"

    due_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=secs)
    uid = getattr(caller, "id", 0) or 0
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO reminders (guild_id, channel_id, user_id, message, due_at)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            guild.id, channel.id, uid, msg, due_at,
        )
    ts = int(due_at.timestamp())
    return f"Reminder #{row['id']} set for <t:{ts}:R> (<t:{ts}:t>): {msg}"


async def _reminder_loop() -> None:
    """Poll Neon Postgres every 15s for due reminders and post them to Discord."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            if pool is not None:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT id, channel_id, user_id, message
                        FROM reminders
                        WHERE NOT sent AND due_at <= NOW()
                        ORDER BY due_at ASC
                        LIMIT 20
                        """
                    )
                    for r in rows:
                        ch = bot.get_channel(r["channel_id"])
                        if ch is None:
                            try:
                                ch = await bot.fetch_channel(r["channel_id"])
                            except Exception:
                                ch = None
                        if ch is not None and hasattr(ch, "send"):
                            mention = f"<@{r['user_id']}> " if r["user_id"] else ""
                            try:
                                await ch.send(f"⏰ **Nhắc kèo:** {mention}{r['message']}")
                            except Exception as e:
                                logger.warning(f"reminder {r['id']} send failed: {e}")
                        await conn.execute("UPDATE reminders SET sent = TRUE WHERE id = $1", r["id"])
        except Exception as e:
            logger.warning(f"reminder loop error: {e}")
        await asyncio.sleep(15)


async def _find_image_bytes(channel, image_url: str = "", message_id: str = "") -> tuple[Optional[bytes], str, str]:
    """Locate an image from URL, specific message, or recent channel history."""
    url = (image_url or "").strip()
    if not url and channel is not None:
        target_msg = None
        if message_id:
            target_msg = await _resolve_target_message(channel, message_id)
        if target_msg is not None:
            for att in getattr(target_msg, "attachments", []) or []:
                ct = (getattr(att, "content_type", None) or "").lower()
                fn = (getattr(att, "filename", None) or "").lower()
                if ct.startswith("image/") or fn.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                    url = att.url
                    break
        else:
            async for m in channel.history(limit=10):
                for att in getattr(m, "attachments", []) or []:
                    ct = (getattr(att, "content_type", None) or "").lower()
                    fn = (getattr(att, "filename", None) or "").lower()
                    if ct.startswith("image/") or fn.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                        url = att.url
                        break
                if url:
                    break
                for emb in getattr(m, "embeds", []) or []:
                    img = getattr(emb, "image", None)
                    if img and getattr(img, "url", None):
                        url = img.url
                        break
                if url:
                    break

    if not url:
        return None, "", "No image found in URL or recent channel messages."
    ok, reason = _is_safe_public_url(url)
    if not ok:
        return None, "", f"Unsafe image URL ({reason})."

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200 or not r.content:
                return None, "", f"Failed to download image (HTTP {r.status_code})."
            if len(r.content) > MAX_ATTACHMENT_BYTES:
                return None, "", "Image exceeds 5MB limit."
            mime = (r.headers.get("content-type") or "image/png").split(";")[0].strip()
            if not mime.startswith("image/"):
                mime = "image/png"
            return r.content, mime, ""
    except Exception as e:
        return None, "", str(e)


async def _handle_read_image(channel, args: Dict) -> str:
    raw, mime, err = await _find_image_bytes(
        channel,
        args.get("image_url") or "",
        args.get("message_id") or "",
    )
    if not raw:
        return f"ERROR: {err}"

    question = (args.get("question") or "").strip() or (
        "Mô tả chi tiết nội dung, chữ viết (OCR) và ý nghĩa/meme của ảnh này bằng tiếng Việt."
    )
    b64 = base64.b64encode(raw).decode("ascii")

    # 1. Official Gemini API vision when GEMINI_API_KEY is available.
    if GEMINI_API_KEY:
        try:
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_VISION_MODEL}:generateContent"
            async with httpx.AsyncClient(timeout=45) as client:
                r = await client.post(
                    endpoint,
                    headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                    json={
                        "contents": [{
                            "parts": [
                                {"text": question},
                                {"inline_data": {"mime_type": mime, "data": b64}},
                            ]
                        }]
                    },
                )
            if r.status_code == 200:
                cands = r.json().get("candidates") or []
                parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
                texts = [p.get("text", "") for p in parts if p.get("text")]
                if texts:
                    return "\n".join(texts)[:3000]
        except Exception as e:
            logger.warning(f"Gemini vision failed: {e}")

    # 2. Fallback via GEMINI_CHAT_URL (OpenAI multimodal format).
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                GEMINI_CHAT_URL,
                json={
                    "model": "gemini-auto",
                    "stream": False,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": question},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        ],
                    }],
                },
            )
        if r.status_code == 200:
            ans = (r.json()["choices"][0]["message"].get("content") or "").strip()
            if ans:
                return ans[:3000]
    except Exception as e:
        logger.warning(f"GEMINI_CHAT_URL vision failed: {e}")

    return "ERROR: could not analyze image with vision backend."


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
            return await _handle_music_control(
                guild, args.get("action") or "",
                index=args.get("index"), indices=args.get("indices"),
            )

        if name == "web_search":
            return await _web_search(args.get("query") or "", args.get("max_results") or 5)

        if name == "fetch_url":
            return await _fetch_url(args.get("url") or "")

        if name == "generate_image":
            return await _generate_and_send_image(
                channel, args.get("prompt") or "", args.get("aspect_ratio") or "")

        if name == "send_file":
            return await _handle_send_file(guild, channel, args)

        if name == "list_skills":
            skills = await get_all_skills()
            if not skills:
                return "No skills installed yet."
            return "\n".join(
                f"- {s['name']} [{'on' if s['enabled'] else 'off'}, {s['source']}]: {s['description']}"
                for s in skills
            )

        if name == "read_skill":
            target_name = _clean_skill_name(args.get("name") or "")
            for s in await get_all_skills():
                if s["name"] == target_name:
                    return f"# Skill: {s['name']}\n{s['instructions']}"
            return f"ERROR: skill '{args.get('name')}' not found"

        if name == "install_skill":
            raw_inst = args.get("instructions") or ""
            parsed = parse_skill_md(raw_inst, fallback_name=args.get("name") or "")
            saved = await upsert_skill(
                args.get("name") or parsed["name"],
                args.get("description") or parsed["description"],
                parsed["instructions"] or raw_inst,
                author=getattr(caller, "display_name", "") or "admin",
            )
            return f"Installed skill '{saved['name']}': {saved['description']}"

        if name == "remove_skill":
            target_name = _clean_skill_name(args.get("name") or "")
            if await delete_skill(target_name):
                return f"Removed skill '{target_name}'."
            if await set_skill_enabled(target_name, False):
                return f"Disabled built-in skill '{target_name}'."
            return f"ERROR: skill '{args.get('name')}' not found"

        if name == "list_roles":
            roles = sorted(getattr(guild, "roles", []) or [], key=lambda r: getattr(r, "position", 0), reverse=True)
            if not roles:
                return "No roles found."
            lines = []
            for r in roles[:50]:
                col = str(getattr(r, "color", "")) or "#000000"
                cnt = len(getattr(r, "members", []) or [])
                lines.append(f"- {r.name} ({r.id}) | {col} | {cnt} members")
            return "\n".join(lines)

        if name == "list_members":
            q = (args.get("query") or "").strip().lower()
            limit = max(1, min(int(args.get("limit") or 30), 100))
            members = list(getattr(guild, "members", []) or [])
            if not members and hasattr(bot, "http"):
                try:
                    raw_members = await bot.http.get_members(guild.id, limit=min(limit * 2, 1000), after=None)
                    out = []
                    for m in raw_members or []:
                        u = m.get("user") or {}
                        disp = m.get("nick") or u.get("global_name") or u.get("username") or "?"
                        if q and q not in disp.lower() and q not in (u.get("username") or "").lower():
                            continue
                        tag = " [BOT]" if u.get("bot") else ""
                        out.append(f"- {disp} ({u.get('id')}){tag}")
                        if len(out) >= limit:
                            break
                    return "\n".join(out) or "No matching members found."
                except Exception:
                    pass
            out = []
            for m in members:
                disp = getattr(m, "display_name", getattr(m, "name", "?"))
                if q and q not in disp.lower() and q not in (getattr(m, "name", "") or "").lower():
                    continue
                st = str(getattr(m, "status", "unknown"))
                tag = " [BOT]" if getattr(m, "bot", False) else ""
                out.append(f"- {disp} ({m.id}) [{st}]{tag}")
                if len(out) >= limit:
                    break
            return "\n".join(out) or "No matching members found."

        if name == "create_role":
            rname = (args.get("name") or "").strip()[:100]
            if not rname:
                return "ERROR: role name is required"
            kwargs: Dict[str, Any] = {"name": rname, "reason": f"by {BOT_USERNAME}"}
            col = _parse_hex_color(args.get("color"))
            if col is not None:
                kwargs["color"] = col
            if "hoist" in args:
                kwargs["hoist"] = bool(args["hoist"])
            if "mentionable" in args:
                kwargs["mentionable"] = bool(args["mentionable"])
            perm_names = args.get("permissions") or []
            if perm_names:
                perms = discord.Permissions.none()
                for pn in perm_names:
                    key = str(pn).strip().lower()
                    if hasattr(perms, key):
                        setattr(perms, key, True)
                kwargs["permissions"] = perms
            role = await guild.create_role(**kwargs)
            return f"Created role {role.name} ({role.id})"

        if name == "edit_role":
            role = guild.get_role(int(args.get("role_id") or 0))
            if role is None:
                return f"ERROR: role {args.get('role_id')} not found"
            kwargs = {"reason": f"by {BOT_USERNAME}"}
            if args.get("name"):
                kwargs["name"] = str(args["name"]).strip()[:100]
            col = _parse_hex_color(args.get("color"))
            if col is not None:
                kwargs["color"] = col
            if "hoist" in args:
                kwargs["hoist"] = bool(args["hoist"])
            if "mentionable" in args:
                kwargs["mentionable"] = bool(args["mentionable"])
            await role.edit(**kwargs)
            return f"Updated role {role.name} ({role.id})"

        if name == "delete_role":
            role = guild.get_role(int(args.get("role_id") or 0))
            if role is None:
                return f"ERROR: role {args.get('role_id')} not found"
            if getattr(role, "is_default", lambda: False)():
                return "ERROR: cannot delete @everyone role"
            rname, rid = role.name, role.id
            await role.delete(reason=f"by {BOT_USERNAME}")
            return f"Deleted role {rname} ({rid})"

        if name == "edit_channel":
            target = _resolve_target_channel(guild, channel, args.get("channel_id"))
            if target is None:
                return "ERROR: channel not found"
            kwargs = {"reason": f"by {BOT_USERNAME}"}
            if args.get("name"):
                kwargs["name"] = str(args["name"]).strip()[:100]
            if "topic" in args and args["topic"] is not None:
                kwargs["topic"] = str(args["topic"])[:1024]
            if "slowmode_delay" in args and args["slowmode_delay"] is not None:
                kwargs["slowmode_delay"] = max(0, min(int(args["slowmode_delay"]), 21600))
            await target.edit(**kwargs)
            return f"Updated channel <#{target.id}> ({target.id})"

        if name == "create_voice_channel":
            cname = (args.get("name") or "").strip()[:100]
            if not cname:
                return "ERROR: name is required"
            kind = (args.get("kind") or "voice").strip().lower()
            if kind == "category":
                cat = await guild.create_category(cname, reason=f"by {BOT_USERNAME}")
                return f"Created category {cat.name} ({cat.id})"
            cat = None
            cid = str(args.get("category_id") or "").strip()
            if cid.isdigit():
                cat = guild.get_channel(int(cid))
            vc_ch = await guild.create_voice_channel(cname, category=cat, reason=f"by {BOT_USERNAME}")
            return f"Created voice channel <#{vc_ch.id}> ({vc_ch.id})"

        if name == "delete_messages":
            target = _resolve_target_channel(guild, channel, args.get("channel_id"))
            if target is None or not hasattr(target, "purge"):
                return "ERROR: channel not found or cannot purge messages"
            limit = max(1, min(int(args.get("limit") or 1), 50))
            deleted = await target.purge(limit=limit)
            return f"Deleted {len(deleted)} message(s) in <#{target.id}>."

        if name == "pin_message":
            target = _resolve_target_channel(guild, channel, args.get("channel_id"))
            if target is None:
                return "ERROR: channel not found"
            msg = await _resolve_target_message(target, args.get("message_id"))
            if msg is None:
                return "ERROR: message not found"
            if args.get("unpin"):
                await msg.unpin(reason=f"by {BOT_USERNAME}")
                return f"Unpinned message {msg.id}."
            await msg.pin(reason=f"by {BOT_USERNAME}")
            return f"Pinned message {msg.id}."

        if name == "add_reaction":
            emoji = (args.get("emoji") or "").strip()
            if not emoji:
                return "ERROR: emoji is required"
            target = _resolve_target_channel(guild, channel, args.get("channel_id"))
            if target is None:
                return "ERROR: channel not found"
            msg = await _resolve_target_message(target, args.get("message_id") or "last")
            if msg is None:
                return "ERROR: message not found"
            await msg.add_reaction(emoji)
            return f"Reacted {emoji} on message {msg.id}."

        if name == "join_voice":
            vc, err = await _ensure_voice(guild, caller)
            if vc is None:
                return err
            return f"Joined voice channel #{getattr(vc.channel, 'name', 'voice')} and waiting."

        if name == "leave_voice":
            return await _handle_leave_voice(guild)

        if name == "set_volume":
            return await _handle_set_volume(guild, args.get("volume"))

        if name == "speak":
            return await _speak_in_voice(guild, caller, args.get("text") or "")

        if name == "listen_voice":
            return await _handle_listen_voice(
                guild, channel, caller,
                seconds=int(args.get("seconds") or 8),
                reply_in_voice=bool(args.get("reply_in_voice", True)),
            )

        if name == "voice_chat":
            return await _handle_voice_chat(guild, channel, caller, bool(args.get("enabled", True)))

        if name == "create_poll":
            return await _handle_create_poll(channel, args)

        if name == "create_reminder":
            return await _handle_create_reminder(guild, channel, caller, args)

        if name == "read_image":
            return await _handle_read_image(channel, args)

        if name in SKILL_HANDLERS:
            handler = SKILL_HANDLERS[name]
            ctx = {"guild": guild, "channel": channel, "caller": caller, "admin": admin}
            res = handler(args, ctx)
            if asyncio.iscoroutine(res):
                res = await res
            return str(res)

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

IMAGE_PREFIXES = ("vẽ ảnh ", "ve anh ", "tạo ảnh ", "tao anh ", "draw ", "gen ảnh ", "gen anh ")


def image_query(content: str) -> Optional[str]:
    """Return the image prompt if the triggered message starts with a draw prefix."""
    return _match_prefix(strip_keywords(content), IMAGE_PREFIXES) or None


async def deliver_image(channel, send, prompt: str, aspect_ratio: str = "") -> None:
    """Force-run image generation and post either the image attachment or exact status/link."""
    out = await _generate_and_send_image(channel, prompt, aspect_ratio)
    if out.startswith("Generated and sent"):
        await send(f"🎨 {out}")
    elif out.startswith("IMAGE_FETCH_BLOCKED:"):
        await send(f"⚠️ {out.replace('IMAGE_FETCH_BLOCKED: ', '', 1)}")
    else:
        await send(f"❌ {out}")


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
    """Call gemini-web-to-api directly (or the legacy forwarder if WEBHOOK_URL is set).

    When WEBHOOK_URL is empty we bypass the forwarder entirely:
    - POST to GEMINI_CHAT_URL (openai/v1/chat/completions)
    - Parse the OpenAI response ourselves (choices[0].message)
    - Return the same dict shape the forwarder used to return so the
      rest of the call-site is unchanged.
    """
    system_prompt = await build_system_prompt()

    # --- direct path (no forwarder) ---
    if not WEBHOOK_URL:
        messages = list(history)
        if not messages or messages[0].get("role") != "system":
            messages = [{"role": "system", "content": system_prompt}] + messages

        payload: dict = {"model": "gemini-auto", "messages": messages, "stream": False}
        if tools and ENABLE_TOOLS:
            payload["tools"] = tools
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(GEMINI_CHAT_URL, json=payload)
                resp.raise_for_status()
                data = resp.json()
                message = data["choices"][0]["message"]
        except httpx.HTTPStatusError as e:
            logger.error(f"Gemini chat error: {e.response.status_code} - {e.response.text[:300]}")
            return False, {"error": f"Gemini API error: {e.response.status_code}"}
        except Exception as e:
            logger.error(f"Gemini chat failed: {e}")
            return False, {"error": str(e)}

        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
            logger.info(f"Tool calls requested (direct): {names}")
            return True, {
                "status": "tool_calls",
                "tool_calls": tool_calls,
                "content": message.get("content") or "",
            }

        answer = message.get("content") or ""
        if not answer:
            return False, {"error": "Gemini returned empty answer"}
        return True, {"status": "accepted", "answer": answer, "answer_preview": answer[:100]}

    # --- legacy forwarder path ---
    payload = {
        "messages": history,
        "system": system_prompt,
        "username": BOT_USERNAME,
        "avatar_url": BOT_AVATAR,
    }
    if tools and ENABLE_TOOLS:
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
    if not getattr(bot, "_reminder_task_started", False):
        bot._reminder_task_started = True
        asyncio.create_task(_reminder_loop())


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

    # Natural-language image requests go through run_tool_loop -> generate_image
    # so the model applies SYSTEM_PROMPT + conversation context (e.g. "bản thân bé").
    # Use /draw for raw prompt bypass without LLM rewriting.

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
            answer = result.get("answer", "")
            await persist_turn(channel_id, messages, answer)
            # Direct path: bot sends the answer itself (no forwarder webhook).
            # Legacy forwarder path: forwarder already posted via Discord webhook.
            if not WEBHOOK_URL and answer:
                # Split long answers so Discord 2000-char limit is respected.
                for chunk in [answer[i:i+1990] for i in range(0, len(answer), 1990)]:
                    await message.channel.send(chunk)
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
        answer = result.get("answer", "")
        await persist_turn(ctx.channel.id, history, answer)
        if not WEBHOOK_URL and answer:
            # Direct path: send answer ourselves (forwarder not used).
            for chunk in [answer[i:i+1990] for i in range(0, len(answer), 1990)]:
                await ctx.followup.send(chunk)
        else:
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

@bot.slash_command(name="draw", description="Ép bot chạy tạo ảnh trực tiếp (bỏ qua chat)")
async def draw_cmd(ctx, prompt: str, aspect_ratio: str = ""):
    await ctx.defer()

    async def _send(text: str):
        return await ctx.followup.send(text)

    await deliver_image(ctx.channel, _send, prompt, aspect_ratio)

@bot.slash_command(name="skills", description="Danh sách các skill đã cài cho Bé Yêu")
async def skills_cmd(ctx):
    rows = await get_all_skills()
    if not rows:
        await ctx.respond("🧩 Chưa có skill nào được cài. Admin có thể dùng `/skill_install`.")
        return
    lines = [
        f"- `{s['name']}` ({'bật' if s['enabled'] else 'tắt'} · {s['source']})"
        + (f" — {s['description']}" if s.get("description") else "")
        for s in rows
    ]
    out = "**🧩 Installed Skills:**\n" + "\n".join(lines)
    await ctx.respond(out[:1900])

@bot.slash_command(name="skill_install", description="[Admin] Cài hoặc cập nhật một skill cho Bé Yêu")
async def skill_install_cmd(ctx, name: str, description: str, instructions: str):
    if not await is_admin(ctx.author, ctx.guild):
        await ctx.respond("🚫 Chỉ admin mới được cài skill.", ephemeral=True)
        return
    try:
        parsed = parse_skill_md(instructions, fallback_name=name)
        saved = await upsert_skill(
            name or parsed["name"],
            description or parsed["description"],
            parsed["instructions"] or instructions,
            author=getattr(ctx.author, "display_name", ""),
        )
        await ctx.respond(f"✅ Đã cài skill **`{saved['name']}`** ({saved['description'] or 'không có mô tả'}).")
    except ValueError as e:
        await ctx.respond(f"❌ {e}", ephemeral=True)

@bot.slash_command(name="skill_toggle", description="[Admin] Bật hoặc tắt một skill")
async def skill_toggle_cmd(ctx, name: str, enabled: bool):
    if not await is_admin(ctx.author, ctx.guild):
        await ctx.respond("🚫 Chỉ admin mới được bật/tắt skill.", ephemeral=True)
        return
    ok = await set_skill_enabled(name, enabled)
    if ok:
        await ctx.respond(f"✅ Skill **`{_clean_skill_name(name)}`** đã {'bật' if enabled else 'tắt'}.")
    else:
        await ctx.respond(f"❌ Không tìm thấy skill `{name}`.", ephemeral=True)

@bot.slash_command(name="skill_remove", description="[Admin] Xóa một skill đã cài trong database")
async def skill_remove_cmd(ctx, name: str):
    if not await is_admin(ctx.author, ctx.guild):
        await ctx.respond("🚫 Chỉ admin mới được xóa skill.", ephemeral=True)
        return
    if await delete_skill(name):
        await ctx.respond(f"🗑️ Đã xóa skill **`{_clean_skill_name(name)}`**.")
    else:
        await ctx.respond(f"❌ Không tìm thấy skill `{name}` trong database.", ephemeral=True)

@bot.slash_command(name="say", description="Cho bé vào kênh thoại nói một câu (TTS)")
async def say_cmd(ctx, text: str):
    await ctx.defer(ephemeral=True)
    res = await _speak_in_voice(ctx.guild, ctx.author, text)
    await ctx.followup.send(f"🔊 {res}", ephemeral=True)


@bot.slash_command(name="listen", description="Bé nghe kênh thoại vài giây (STT) rồi trả lời")
async def listen_cmd(ctx, seconds: int = 8):
    await ctx.defer()
    res = await _handle_listen_voice(ctx.guild, ctx.channel, ctx.author, seconds=seconds, reply_in_voice=True)
    await ctx.followup.send(f"🎙️ {res[:1900]}")


@bot.slash_command(name="voice_chat", description="Bật/tắt chế độ nghe nói tự động 2 chiều trong kênh thoại")
async def voice_chat_cmd(ctx, enabled: bool = True):
    await ctx.defer()
    res = await _handle_voice_chat(ctx.guild, ctx.channel, ctx.author, enabled)
    await ctx.followup.send(f"🎧 {res}")


# ===== STARTUP =====
@bot.listen()
async def on_connect():
    await init_db()

async def _start_relay_server() -> Optional[web.AppRunner]:
    if not RELAY_ENABLED:
        return None
    app = web.Application()
    app.router.add_get("/health", relay_hub.handle_health)
    app.router.add_get("/relay/auth-check", relay_hub.handle_auth_check)
    app.router.add_get("/ws/relay", relay_hub.handle_ws)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", RELAY_PORT)
    await site.start()
    logger.info(f"Relay HTTP/WS server listening on 0.0.0.0:{RELAY_PORT}")
    return runner


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not set")
        exit(1)
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set")
        exit(1)
    _relay_runner = bot.loop.run_until_complete(_start_relay_server())
    try:
        bot.run(DISCORD_TOKEN)
    finally:
        if _relay_runner is not None and not bot.loop.is_closed():
            bot.loop.run_until_complete(_relay_runner.cleanup())