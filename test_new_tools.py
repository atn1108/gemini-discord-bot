import asyncio
import types
from unittest.mock import AsyncMock, MagicMock, patch
import discord
import discord_lifetime_memory_bot as b


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"[FAIL] {name}: {detail}")
    print(f"[PASS] {name}" + (f" -> {detail}" if detail else ""))


async def main():
    # 1. DANGEROUS_TOOLS includes new admin tools, excludes safe tools
    for t in ("create_role", "edit_role", "delete_role", "edit_channel",
              "create_voice_channel", "delete_messages", "pin_message"):
        check(f"dangerous: {t}", t in b.DANGEROUS_TOOLS)
    for t in ("list_roles", "list_members", "add_reaction", "leave_voice",
              "set_volume", "create_poll", "create_reminder", "read_image"):
        check(f"safe: {t}", t not in b.DANGEROUS_TOOLS)

    # 2. _parse_delay_seconds
    check("delay 15m", b._parse_delay_seconds("15m") == 900)
    check("delay 2h30m", b._parse_delay_seconds("2h 30m") == 9000)
    check("delay 1d", b._parse_delay_seconds("1d") == 86400)
    check("delay minutes=5", b._parse_delay_seconds("", 5) == 300)

    # Mock guild / channel / caller
    role1 = MagicMock(id=101, color=discord.Color(0xFF6600), position=2, members=[1, 2])
    role1.name = "VIP"
    role1.is_default = lambda: False
    role1.edit = AsyncMock()
    role1.delete = AsyncMock()
    role_ev = MagicMock(id=100, color=discord.Color(0), position=0, members=[1])
    role_ev.name = "@everyone"
    role_ev.is_default = lambda: True

    member1 = MagicMock(id=201, display_name="Dao", status="online", bot=False)
    member1.name = "dao"
    guild = MagicMock(id=999, owner_id=1, roles=[role_ev, role1], members=[member1], voice_client=None)
    guild.get_role = lambda rid: {101: role1, 100: role_ev}.get(rid)
    created_role = MagicMock(id=102)
    created_role.name = "Gamer"
    created_vc = MagicMock(id=301)
    created_vc.name = "Talk"
    created_cat = MagicMock(id=302)
    created_cat.name = "Games"
    guild.create_role = AsyncMock(return_value=created_role)
    guild.create_voice_channel = AsyncMock(return_value=created_vc)
    guild.create_category = AsyncMock(return_value=created_cat)

    msg_obj = MagicMock(id=501)
    msg_obj.pin = AsyncMock()
    msg_obj.unpin = AsyncMock()
    msg_obj.add_reaction = AsyncMock()

    channel = MagicMock(id=401, name="general")
    channel.edit = AsyncMock()
    channel.purge = AsyncMock(return_value=[1, 2, 3])
    channel.fetch_message = AsyncMock(return_value=msg_obj)
    channel.send = AsyncMock(return_value=msg_obj)

    async def fake_hist(limit=1):
        yield msg_obj

    channel.history = fake_hist
    guild.get_channel = lambda cid: channel if cid == 401 else None

    caller = MagicMock(id=201, display_name="Dao")

    # 3. Non-admin denied for dangerous tools
    res = await b.execute_tool("create_role", {"name": "X"}, guild, channel, caller, admin=False)
    check("non-admin create_role denied", res.startswith("DENIED:"), res)

    # 4. Admin role/channel/message tools
    res = await b.execute_tool("list_roles", {}, guild, channel, caller, admin=False)
    check("list_roles", "VIP (101)" in res, res)

    res = await b.execute_tool("list_members", {"query": "dao"}, guild, channel, caller, admin=False)
    check("list_members", "Dao (201)" in res, res)

    res = await b.execute_tool("create_role", {"name": "Gamer", "color": "#ff0000"}, guild, channel, caller, admin=True)
    check("create_role", "Gamer (102)" in res, res)

    res = await b.execute_tool("edit_role", {"role_id": "101", "name": "SuperVIP"}, guild, channel, caller, admin=True)
    check("edit_role", "Updated role" in res, res)

    res = await b.execute_tool("delete_role", {"role_id": "100"}, guild, channel, caller, admin=True)
    check("delete_role blocks @everyone", "cannot delete @everyone" in res, res)

    res = await b.execute_tool("edit_channel", {"name": "chill", "slowmode_delay": 5}, guild, channel, caller, admin=True)
    check("edit_channel", "Updated channel" in res, res)

    res = await b.execute_tool("create_voice_channel", {"name": "Talk"}, guild, channel, caller, admin=True)
    check("create_voice_channel", "301" in res, res)

    res = await b.execute_tool("create_voice_channel", {"name": "Games", "kind": "category"}, guild, channel, caller, admin=True)
    check("create_category", "302" in res, res)

    res = await b.execute_tool("delete_messages", {"limit": 3}, guild, channel, caller, admin=True)
    check("delete_messages", "Deleted 3 message(s)" in res, res)

    res = await b.execute_tool("pin_message", {"message_id": "501"}, guild, channel, caller, admin=True)
    check("pin_message", "Pinned message 501" in res, res)

    res = await b.execute_tool("add_reaction", {"emoji": "🔥", "message_id": "last"}, guild, channel, caller, admin=False)
    check("add_reaction", "Reacted 🔥" in res, res)

    # 5. Voice volume + leave
    src = types.SimpleNamespace(volume=1.0)
    vc = MagicMock(source=src)
    vc.is_playing = lambda: True
    vc.is_paused = lambda: False
    vc.stop = MagicMock()
    vc.disconnect = AsyncMock()
    guild.voice_client = vc

    res = await b.execute_tool("set_volume", {"volume": 150}, guild, channel, caller, admin=False)
    check("set_volume", res == "Set music volume to 150%." and src.volume == 1.5, res)

    res = await b.execute_tool("leave_voice", {}, guild, channel, caller, admin=False)
    check("leave_voice", "Left voice channel" in res, res)

    # 6. Poll
    res = await b.execute_tool("create_poll", {"question": "Ăn gì?", "options": ["Phở", "Bún bò"]}, guild, channel, caller, admin=False)
    check("create_poll", "Created" in res and "poll" in res, res)

    # 7. Reminder (real Neon DB round-trip if pool initialized, else mock)
    await b.init_db()
    res = await b.execute_tool("create_reminder", {"message": "test kèo", "delay": "10m"}, guild, channel, caller, admin=False)
    check("create_reminder (Neon)", "Reminder #" in res, res)
    async with b.pool.acquire() as conn:
        await conn.execute("DELETE FROM reminders WHERE guild_id = 999 AND message = 'test kèo'")
    await b.pool.close()

    # 8. read_image (mocked download + Gemini vision)
    with patch.object(b, "_find_image_bytes", AsyncMock(return_value=(b"fakeimg", "image/png", ""))), \
         patch("httpx.AsyncClient.post", AsyncMock(return_value=MagicMock(
             status_code=200,
             json=lambda: {"candidates": [{"content": {"parts": [{"text": "Ảnh con mèo"}]}}]}
         ))):
        old_key = b.GEMINI_API_KEY
        b.GEMINI_API_KEY = "test-key"
        res = await b.execute_tool("read_image", {"image_url": "https://example.com/cat.png"}, guild, channel, caller, admin=False)
        b.GEMINI_API_KEY = old_key
        check("read_image", res == "Ảnh con mèo", res)

    print("\nALL NEW TOOLS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
