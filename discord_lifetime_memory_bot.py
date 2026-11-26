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
- Tối ưu độ dài: câu hỏi thường trả lời trong 1–4 câu; hướng dẫn kỹ thuật hoặc tổng hợp tin tức thì chia ý rõ ràng, đủ bước.

# Tool Use (Công cụ quản lý server)
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


async def build_system_prompt() -> str:
    """Persona + remembered facts + the peer bots discovered in this server."""
    facts = await get_facts()
    bots = await get_peer_bots()
    if not facts and not bots:
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
]

# Tools that require admin rights. read_channel/send_to_channel are open.
DANGEROUS_TOOLS = {"kick", "ban", "timeout", "create_channel", "delete_channel",
                   "set_nickname", "add_role", "remove_role"}


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
            limit = max(1, min(int(args.get("limit") or 5), 20))
            lines = []
            async for m in target.history(limit=limit):
                who = getattr(m.author, "display_name", "?")
                lines.append(f"[{who}] {m.content[:200]}")
            return "\n".join(reversed(lines)) or "(no messages)"

        if name == "send_to_channel":
            target = guild.get_channel(int(args.get("channel_id") or 0))
            if target is None:
                return f"ERROR: channel not found"
            content = (args.get("content") or "")[:1900]
            await target.send(content)
            return f"Sent to channel {target.id}"

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
    
    content = (message.content or "").strip()
    if not content:
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
    if not content and mentioned:
        content = "alo"

    # Deep Research — "nghiên cứu X", "research X", "bot nghiên cứu X"
    topic = research_query(content)
    if topic:
        logger.info(f"research from {message.author.display_name}: {topic!r}")
        await deliver_research(message.channel.send, topic)
        return

    # Prefix the sender so the model knows who is talking (see system prompt
    # persona); a reply carries the quoted message too, so tagging mid-argument
    # needs no re-explanation.
    turn = {"role": "user",
            "content": (render_reply_context(target)
                        + f"[{message.author.display_name}]: {content}")[:STORE_MAX]}

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
            success, result = await run_tool_loop(
                messages, channel_id,
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