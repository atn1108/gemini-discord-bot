"""Tool-use layer: admin gate, executor, bounded round loop.

Three properties must hold or the upgrade is a security hole:

1. A non-admin can never reach a destructive tool — not through the schema
   (dangerous tools are not even offered) and not through the executor
   (execute_tool refuses anyway). Defense in depth, both layers tested.
2. The round loop is bounded: the last call carries no tools, so a model that
   keeps asking for tool calls still terminates with a text answer.
3. tool_calls / role=tool protocol rows stay in the working copy and never
   reach the transcript that gets persisted to Postgres.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord_lifetime_memory_bot as bot_mod

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        failures.append(name)


# ---------- fixtures ----------

class Perms:
    def __init__(self, admin=False):
        self.administrator = admin


class FakeMember:
    def __init__(self, mid, admin=False, owner=False):
        self.id = mid
        self.display_name = f"user{mid}"
        self.guild_permissions = Perms(admin)
        self.kicked = []
        self.banned = []
        self.timeouts = []
        self.edits = []
        self.roles_added = []
        self.roles_removed = []

    async def kick(self, *, reason=None):
        self.kicked.append(reason)

    async def ban(self, *, delete_message_seconds=None, reason=None):
        self.banned.append((delete_message_seconds, reason))

    async def timeout(self, until, *, reason=None):
        self.timeouts.append((until, reason))

    async def edit(self, *, nick=None, reason=None):
        self.edits.append((nick, reason))

    async def add_roles(self, *roles, reason=None):
        self.roles_added.extend(roles)

    async def remove_roles(self, *roles, reason=None):
        self.roles_removed.extend(roles)


class FakeRole:
    def __init__(self, rid, name):
        self.id = rid
        self.name = name


class FakeChannel:
    def __init__(self, cid=4242):
        self.id = cid
        self.sent = []
        self.history_log = []

    async def send(self, content, **kw):
        self.sent.append(content)

    def history(self, limit=5):
        items = list(reversed(self.history_log[-limit:]))

        class _Iter:
            def __aiter__(self_inner):
                self_inner.i = 0
                return self_inner

            async def __anext__(self_inner):
                if self_inner.i >= len(items):
                    raise StopAsyncIteration
                v = items[self_inner.i]
                self_inner.i += 1
                return v

        return _Iter()


class FakeGuild:
    def __init__(self, owner_id=1, members=None, channels=None, roles=None):
        self.owner_id = owner_id
        self._members = {m.id: m for m in (members or [])}
        self._channels = {c.id: c for c in (channels or [])}
        self._roles = {r.id: r for r in (roles or [])}
        self.created = []
        self.deleted = []

    async def fetch_member(self, mid):
        if mid in self._members:
            return self._members[mid]
        raise Exception("unknown member")

    def get_member(self, mid):
        return self._members.get(mid)

    def get_channel(self, cid):
        return self._channels.get(cid)

    def get_role(self, rid):
        return self._roles.get(rid)

    async def create_text_channel(self, name, *, reason=None, **kw):
        ch = FakeChannel(len(self.created) + 9000)
        ch.name = name
        self.created.append((name, reason))
        self._channels[ch.id] = ch
        return ch

    async def ban(self, obj, *, delete_message_seconds=None, reason=None):
        self.deleted.append(("ban", obj.id))


ADMIN_ID = 42
bot_mod.ADMIN_IDS = {ADMIN_ID}
bot_mod.ENABLE_TOOLS = True
bot_mod.MAX_TOOL_ROUNDS = 3

admin_member = FakeMember(ADMIN_ID, admin=True)
plain_member = FakeMember(55, admin=False)
owner_member = FakeMember(1, admin=True)  # owner_id == 1
other_member = FakeMember(66)
role_777 = FakeRole(777, "Mod")
guild = FakeGuild(owner_id=1, members=[admin_member, plain_member, owner_member,
                                       other_member],
                  roles=[role_777])
channel = FakeChannel()
guild._channels[channel.id] = channel


def run_now(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------- 1. is_admin ----------

check("admin: explicit ADMIN_IDS", run_now(bot_mod.is_admin(admin_member, guild)))
check("admin: plain member denied", not run_now(bot_mod.is_admin(plain_member, guild)))
check("admin: guild owner allowed",
      run_now(bot_mod.is_admin(FakeMember(999), FakeGuild(owner_id=999))))
check("admin: administrator permission allowed",
      run_now(bot_mod.is_admin(FakeMember(777, admin=True), guild)))
check("admin: fail-closed on None member", not run_now(bot_mod.is_admin(None, guild)))
check("admin: fail-closed on None guild", not run_now(bot_mod.is_admin(admin_member, None)))

# ---------- 2. execute_tool gate (defense in depth) ----------

res = run_now(bot_mod.execute_tool("kick", {"member_id": "66"}, guild, channel,
                                   plain_member, False))
check("exec: non-admin kick DENIED", res.startswith("DENIED"), res[:80])

res = run_now(bot_mod.execute_tool("delete_channel", {"channel_id": "4242"}, guild,
                                   channel, plain_member, False))
check("exec: non-admin delete_channel DENIED", res.startswith("DENIED"), res[:80])

res = run_now(bot_mod.execute_tool("kick", {"member_id": "66"}, guild, channel,
                                   admin_member, True))
check("exec: admin kick runs", res.startswith("Kicked") and other_member.kicked,
      res[:80])

# ---------- 3. executor behaviour ----------

res = run_now(bot_mod.execute_tool("ban", {"member_id": "66", "delete_message_seconds": 3600},
                                   guild, channel, admin_member, True))
check("exec: admin ban passes delete window",
      res.startswith("Banned") and other_member.banned == [(3600, None)] or
      other_member.banned == [(3600, "by " + bot_mod.BOT_USERNAME)],
      f"{res} | {other_member.banned}")

res = run_now(bot_mod.execute_tool("timeout", {"member_id": "66", "minutes": 10},
                                   guild, channel, admin_member, True))
check("exec: timeout clamps to a datetime", res.startswith("Timed out")
      and len(other_member.timeouts) == 1, res[:80])

res = run_now(bot_mod.execute_tool("set_nickname", {"member_id": "66", "nickname": "Cu"},
                                   guild, channel, admin_member, True))
check("exec: nickname edit", res.startswith("Set nickname")
      and other_member.edits and other_member.edits[0][0] == "Cu", res[:80])

res = run_now(bot_mod.execute_tool("create_channel", {"name": "My Channel"},
                                   guild, channel, admin_member, True))
check("exec: create_channel sanitises name",
      res.startswith("Created channel") and guild.created[0][0] == "my-channel",
      f"{res} | {guild.created}")

res = run_now(bot_mod.execute_tool("add_role", {"member_id": "66", "role_id": "777"},
                                   guild, channel, admin_member, True))
check("exec: add_role resolves role",
      res.startswith("Added role") and other_member.roles_added
      and other_member.roles_added[0].id == 777, res[:80])

res = run_now(bot_mod.execute_tool("delete_channel", {"channel_id": str(channel.id)},
                                   guild, channel, admin_member, True))
check("exec: refuses to delete the current channel",
      res.startswith("ERROR: refusing"), res[:80])

res = run_now(bot_mod.execute_tool("kick", {"member_id": "nope"}, guild, channel,
                                   admin_member, True))
check("exec: unknown member is an error, not a crash", res.startswith("ERROR"), res[:80])

res = run_now(bot_mod.execute_tool("send_to_channel", {"channel_id": "4242",
                                                       "content": "hi other bot"},
                                   guild, channel, plain_member, False))
check("exec: non-admin may use the safe send tool",
      res.startswith("Sent") and channel.sent == ["hi other bot"], res[:80])

# ---------- 4. run_tool_loop ----------

calls = {"webhook": [], "saved": []}
REPLY = {"answer": "final answer"}


async def stub_call_webhook(history, cid, tools=None):
    calls["webhook"].append({"history": list(history), "tools": tools})
    step = calls["webhook"][-1]
    scripted = calls.get("script")
    if scripted:
        return scripted(len(calls["webhook"]) - 1, step)
    return True, dict(REPLY)


bot_mod.call_webhook = stub_call_webhook

base = [{"role": "user", "content": "[Minh]: kick @X(66)"}]

# 4a. plain text answer: one round, tools offered, loop ends
calls["webhook"].clear()
calls.pop("script", None)
ok, res = run_now(bot_mod.run_tool_loop(list(base), 4242, guild, channel, admin_member))
check("loop: text answer -> single round", ok and len(calls["webhook"]) == 1
      and res.get("answer") == "final answer", f"rounds={len(calls['webhook'])}")
check("loop: tools offered to an admin",
      calls["webhook"][0]["tools"] is bot_mod.TOOLS, str(calls["webhook"][0]["tools"])[:60])
check("loop: working copy keeps the caller's list clean",
      len(base) == 1 and "tool_calls" not in base[0], str(base))

# 4b. non-admin is never offered the dangerous schema
calls["webhook"].clear()
run_now(bot_mod.run_tool_loop(list(base), 4242, guild, channel, plain_member))
offered = [t["function"]["name"] for t in calls["webhook"][0]["tools"]]
check("loop: non-admin gets no dangerous tools",
      not (set(offered) & bot_mod.DANGEROUS_TOOLS) and "read_channel" in offered,
      str(offered))

# 4c. a tool round executes, feeds role=tool back, then answers
calls["webhook"].clear()


def script(i, step):
    if i == 0:
        return True, {"status": "tool_calls", "content": "",
                      "tool_calls": [{"id": "c1", "type": "function",
                                      "function": {"name": "kick",
                                                   "arguments": '{"member_id":"66"}'}}]}
    return True, {"answer": "kicked him"}


calls["script"] = script
ok, res = run_now(bot_mod.run_tool_loop(list(base), 4242, guild, channel, admin_member))
check("loop: tool executed then answered", ok and res.get("answer") == "kicked him",
      str(res)[:80])
check("loop: exactly two rounds", len(calls["webhook"]) == 2, str(len(calls["webhook"])))
check("loop: tool result fed back as role=tool",
      calls["webhook"][1]["history"][-1].get("role") == "tool"
      and calls["webhook"][1]["history"][-1].get("tool_call_id") == "c1",
      str(calls["webhook"][1]["history"][-1])[:120])
check("loop: assistant tool_calls row kept for the model",
      calls["webhook"][1]["history"][-2].get("role") == "assistant"
      and calls["webhook"][1]["history"][-2].get("tool_calls"), "missing assistant row")
check("loop: caller's transcript untouched by protocol rows",
      len(base) == 1, str(base))
check("loop: last round still offered tools", calls["webhook"][1]["tools"] is not None)

# 4d. bounded: a model that always asks for tools gets a tool-less final round
calls["webhook"].clear()


def always_tools(i, step):
    return True, {"status": "tool_calls", "content": "",
                  "tool_calls": [{"id": f"c{i}", "type": "function",
                                  "function": {"name": "kick",
                                               "arguments": '{"member_id":"66"}'}}]}


calls["script"] = always_tools
ok, res = run_now(bot_mod.run_tool_loop(list(base), 4242, guild, channel, admin_member))
rounds = len(calls["webhook"])
last_tools = calls["webhook"][-1]["tools"]
check("loop: bounded at MAX_TOOL_ROUNDS + 1 calls", rounds == bot_mod.MAX_TOOL_ROUNDS + 1,
      f"rounds={rounds} cap={bot_mod.MAX_TOOL_ROUNDS}")
check("loop: final call carries no tools", last_tools is None, str(last_tools))
check("loop: unbounded request fails closed", not ok and "error" in res, str(res))

# 4e. failure propagates and stops the loop
calls["webhook"].clear()


def fail(i, step):
    return False, {"error": "boom"}


calls["script"] = fail
ok, res = run_now(bot_mod.run_tool_loop(list(base), 4242, guild, channel, admin_member))
check("loop: upstream failure surfaces", not ok and res.get("error") == "boom",
      str(res))
check("loop: failure does not retry", len(calls["webhook"]) == 1,
      str(len(calls["webhook"])))

# 4f. ENABLE_TOOLS off bypasses the tool layer entirely
calls["webhook"].clear()
calls.pop("script", None)
bot_mod.ENABLE_TOOLS = False
ok, res = run_now(bot_mod.run_tool_loop(list(base), 4242, guild, channel, admin_member))
check("loop: escape hatch skips tools",
      ok and len(calls["webhook"]) == 1 and calls["webhook"][0]["tools"] is None,
      str(calls["webhook"][0]["tools"]))
bot_mod.ENABLE_TOOLS = True

# 4g. no guild (DM / missing attribute) still answers
calls["webhook"].clear()
ok, res = run_now(bot_mod.run_tool_loop(list(base), 4242, None, channel, None))
check("loop: no guild -> plain call", ok and len(calls["webhook"]) == 1, str(res))

# ---------- 5. reply-to-bot trigger ----------

class Ref:
    def __init__(self, msg_id, target=None):
        self.message_id = msg_id
        self.cached_message = target


class Msg:
    def __init__(self, author, reference=None, channel=None):
        self.author = author
        self.reference = reference
        self.channel = channel or channel_stub


class ChanStub:
    def __init__(self, target=None, fail=False):
        self.target = target
        self.fail = fail
        self.fetches = 0

    async def fetch_message(self, mid):
        self.fetches += 1
        if self.fail:
            raise Exception("gone")
        return self.target


bot_user_obj = type("U", (), {"id": 1574473312086524057, "bot": True})()
webhook_author = type("U", (), {"id": 9876543210, "bot": True})()
human_author = type("U", (), {"id": 55, "bot": False})()

channel_stub = ChanStub()
check("reply: no reference is not a trigger",
      run_now(bot_mod.resolve_reply(Msg(human_author), bot_user_obj.id))
      == (None, False))

msg = Msg(human_author, Ref(1, type("T", (), {"author": webhook_author})()))
target, is_bot = run_now(bot_mod.resolve_reply(msg, bot_user_obj.id))
check("reply: replying to the webhook answer triggers",
      is_bot and target is not None)

msg = Msg(human_author, Ref(1, type("T", (), {"author": bot_user_obj})()))
check("reply: replying to the bot itself triggers",
      run_now(bot_mod.resolve_reply(msg, bot_user_obj.id))[1])

msg = Msg(human_author, Ref(1, type("T", (), {"author": human_author})()))
check("reply: replying to a human does not trigger",
      not run_now(bot_mod.resolve_reply(msg, bot_user_obj.id))[1])

# uncached reference -> one REST fetch
fetch_target = type("T", (), {"author": webhook_author})()
chan = ChanStub(fetch_target)
msg = Msg(human_author, Ref(424242), channel=chan)
check("reply: uncached reference is fetched once",
      run_now(bot_mod.resolve_reply(msg, bot_user_obj.id))[1] and chan.fetches == 1,
      f"fetches={chan.fetches}")

chan = ChanStub(fail=True)
msg = Msg(human_author, Ref(424242), channel=chan)
check("reply: deleted target fails closed",
      not run_now(bot_mod.resolve_reply(msg, bot_user_obj.id))[1])

# ---------- 5b. reply context rendering (who is replying to whom) ----------

class Target:
    def __init__(self, name, content, attachments=()):
        self.author = type("A", (), {"display_name": name})()
        self.content = content
        self.attachments = list(attachments)


check("ctx: no target -> empty", bot_mod.render_reply_context(None) == "")
check("ctx: quotes the replied text",
      bot_mod.render_reply_context(Target("Minh", "ai sai?"))
      == '[Reply tới @Minh: "ai sai?"] ',
      bot_mod.render_reply_context(Target("Minh", "ai sai?")))
check("ctx: truncates a long quote",
      len(bot_mod.render_reply_context(Target("Minh", "x" * 500))) < 250)
check("ctx: attachment says so instead of quoting nothing",
      bot_mod.render_reply_context(Target("Minh", "", ["a.png"]))
      == '[Reply tới @Minh: "(đính kèm tệp)"] ',
      bot_mod.render_reply_context(Target("Minh", "", ["a.png"])))
check("ctx: textless target still names the author",
      bot_mod.render_reply_context(Target("Minh", "")) == "[Reply tới @Minh] ",
      bot_mod.render_reply_context(Target("Minh", "")))

# ---------- 5c. peer-bot discovery (learned, never hardcoded) ----------

learned = []


async def fake_record(bot_id, username, command=""):
    learned.append((bot_id, username, command))


real_record = bot_mod.record_peer_bot
bot_mod.record_peer_bot = fake_record


def drain_tasks(msg):
    """Call learn_from_message() inside a running loop, then let its task finish.

    Production always calls it from on_message (a live loop); create_task()
    refuses to run outside one, so the test has to mirror that.
    """
    async def _run():
        bot_mod.learn_from_message(msg)
        await asyncio.sleep(0)
    return run_now(_run())


class PeerMsg:
    def __init__(self, author, meta=None):
        self.author = author
        self.interaction_metadata = meta


bot_mod.bot._connection.user = bot_user_obj
learned.clear()
drain_tasks(PeerMsg(webhook_author))
check("learn: self/our webhook not listed",
      not learned, str(learned))

peer = type("U", (), {"id": 411916947773587456, "bot": True,
                      "display_name": "Jockie Music"})()
learned.clear()
drain_tasks(PeerMsg(peer))
check("learn: a peer bot is picked up from its own message",
      learned and learned[0][0] == 411916947773587456
      and learned[0][1] == "Jockie Music", str(learned))

meta = type("M", (), {"name": "play"})()
learned.clear()
drain_tasks(PeerMsg(peer, meta))
check("learn: slash command recorded from interaction_metadata",
      learned and learned[0][2] == "play", str(learned))

human_msg = PeerMsg(human_author)
human_msg.author = human_author
learned.clear()
drain_tasks(human_msg)
check("learn: humans are not peer bots", not learned, str(learned))

bot_mod.record_peer_bot = real_record

# rendering
rendered = bot_mod.render_peer_bots([
    {"bot_id": 1, "username": "Jockie Music", "commands": ["play", "queue"]},
    {"bot_id": 2, "username": "TempVoice", "commands": []},
])
check("render: bot names present",
      "Jockie Music" in rendered and "TempVoice" in rendered, rendered[:80])
check("render: learned commands shown",
      "`/play`" in rendered and "`/queue`" in rendered)
check("render: bot with no commands still listed", "id 2" in rendered)
check("render: tells the model not to invent bots",
      "không bịa" in rendered.lower())

# prompt wiring: persona + facts + peer bots in one build
async def prompt_check():
    real_facts, real_bots = bot_mod.get_facts, bot_mod.get_peer_bots
    real_skills = bot_mod.get_all_skills
    real_prompt = bot_mod.SYSTEM_PROMPT
    bot_mod.SYSTEM_PROMPT = "PERSONA"

    async def no_facts():
        return []

    async def no_bots():
        return []

    async def no_skills():
        return []

    async def some_bots():
        return [{"bot_id": 1, "username": "Dyno", "commands": ["dadjoke"]}]

    bot_mod.get_facts, bot_mod.get_peer_bots, bot_mod.get_all_skills = no_facts, no_bots, no_skills
    p = await bot_mod.build_system_prompt()
    check("prompt: nothing to add -> persona untouched", p == "PERSONA", p[:60])

    bot_mod.get_peer_bots = some_bots
    p = await bot_mod.build_system_prompt()
    check("prompt: peer bots section added", "# Bot khác trong server" in p)
    check("prompt: persona still first", p.startswith("PERSONA"))
    check("prompt: learned command reaches the model", "`/dadjoke`" in p)

    bot_mod.get_facts, bot_mod.get_peer_bots, bot_mod.get_all_skills = real_facts, real_bots, real_skills
    bot_mod.SYSTEM_PROMPT = real_prompt


run_now(prompt_check())

# ---------- 5d. channel directory (real ids, discovered from the guild) ----------

check("chan: no guilds -> no section", bot_mod.render_channels() == "")


class Chan:
    def __init__(self, cid, name, kind):
        self.id, self.name, self.type = cid, name, kind


class Guild:
    def __init__(self, channels):
        self.channels = channels


real_bot = bot_mod.bot
try:
    bot_mod.bot = type("BotStub", (), {"guilds": [Guild([
        Chan(1532004555422699611, "chat-chung", "text"),
        Chan(1532053318556319764, "bot-tool", "text"),
        Chan(1532397250364309617, "General VC", "voice"),
        Chan(1, "", "text"),          # no name -> skipped
    ])]})()
    sec = bot_mod.render_channels()
    check("chan: text channel listed with its real id",
          "#chat-chung (1532004555422699611)" in sec, sec[:120])
    check("chan: second text channel too",
          "#bot-tool (1532053318556319764)" in sec)
    check("chan: voice channel flagged as no-text",
          "General VC" in sec and "KHÔNG" in sec and "join voice" in sec)
    check("chan: unnamed channel skipped", "id 1" not in sec and "(1)" not in sec)
    check("chan: mentions read/send tools",
          "read_channel" in sec and "send_to_channel" in sec)

    # a text-only guild omits the voice line entirely
    bot_mod.bot = type("BotStub", (), {"guilds": [Guild([Chan(9, "meme", "text")])]})()
    check("chan: no voice channel -> no voice warning",
          "KHÔNG" not in bot_mod.render_channels())
finally:
    bot_mod.bot = real_bot

# voice channel rejects both tools instead of crashing
async def voice_tools():
    guild = Guild([Chan(77, "lobby", "voice")])
    guild.get_channel = lambda cid: next(
        (c for c in guild.channels if c.id == cid), None)
    got = await bot_mod.execute_tool("send_to_channel",
                                     {"channel_id": "77", "content": "hi"},
                                     guild, None, None, False)
    check("chan: send to voice refused", "ERROR" in got and "voice" in got, got)
    got = await bot_mod.execute_tool("read_channel", {"channel_id": "77"},
                                     guild, None, None, False)
    check("chan: read from voice refused", "ERROR" in got and "voice" in got, got)


run_now(voice_tools())

# ---------- 6. schema sanity ----------

names = [t["function"]["name"] for t in bot_mod.TOOLS]
check("schema: unique tool names", len(names) == len(set(names)), str(names))
check("schema: every tool has parameters",
      all(t["function"].get("parameters", {}).get("type") == "object" for t in bot_mod.TOOLS))
check("schema: dangerous list is a subset of TOOLS",
      bot_mod.DANGEROUS_TOOLS <= set(names),
      str(bot_mod.DANGEROUS_TOOLS - set(names)))
check("schema: safe tools exist for non-admins",
      set(names) - bot_mod.DANGEROUS_TOOLS, str(set(names) - bot_mod.DANGEROUS_TOOLS))

# ---------- 7. music, web search, image gen, attachments, send_file ----------

# 7a. music player & queue
async def music_checks():
    class FakeVC:
        def __init__(self, ch):
            self.channel = ch
            self.playing = False
            self.paused = False
            self.played = []
            self.disconnected = False

        def is_connected(self):
            return not self.disconnected

        def is_playing(self):
            return self.playing

        def is_paused(self):
            return self.paused

        def play(self, source, after=None):
            self.playing = True
            self.paused = False
            self.played.append((source, after))

        def pause(self):
            self.playing = False
            self.paused = True

        def resume(self):
            self.paused = False
            self.playing = True

        def stop(self):
            self.playing = False
            self.paused = False

        async def move_to(self, ch):
            self.channel = ch

        async def disconnect(self):
            self.disconnected = True

    class FakeVoiceChannel:
        def __init__(self, name="Nhac"):
            self.name = name
            self.vc = None

        async def connect(self):
            self.vc = FakeVC(self)
            return self.vc

    class MusicGuild:
        def __init__(self, gid=9001):
            self.id = gid
            self._vc = None

        @property
        def voice_client(self):
            return self._vc

    g = MusicGuild()
    no_voice_caller = type("M", (), {"display_name": "Minh", "voice": None})()
    got = await bot_mod.execute_tool("play_music", {"query": "lac troi"}, g, None, no_voice_caller, False)
    check("music: refuses if caller not in voice", "ERROR" in got and "voice" in got, got)

    vch = FakeVoiceChannel("Phong Nhac")

    async def connect_and_attach():
        g._vc = FakeVC(vch)
        return g._vc

    vch.connect = connect_and_attach
    caller = type("M", (), {
        "display_name": "Minh",
        "voice": type("V", (), {"channel": vch})(),
    })()

    real_extract = bot_mod._extract_track_info
    real_source = bot_mod._create_audio_source
    bot_mod._extract_track_info = lambda q: {
        "title": f"Track:{q}", "stream_url": f"https://cdn/{q}",
        "webpage_url": f"https://yt/{q}", "duration": 185,
    }
    bot_mod._create_audio_source = lambda u: f"SRC:{u}"
    try:
        r1 = await bot_mod.execute_tool("play_music", {"query": "lac troi"}, g, None, caller, False)
        check("music: starts playing immediately", "Now playing" in r1 and "Track:lac troi" in r1, r1)
        check("music: vc.play received source", g._vc.played and g._vc.played[0][0] == "SRC:https://cdn/lac troi")

        r2 = await bot_mod.execute_tool("play_music", {"query": "chung ta cua hien tai"}, g, None, caller, False)
        check("music: second song queued", "Queued #1" in r2 and "chung ta" in r2, r2)

        qtxt = await bot_mod.execute_tool("music_control", {"action": "queue"}, g, None, caller, False)
        check("music: queue lists current and upcoming", "Now playing: Track:lac troi" in qtxt and "1. Track:chung ta cua hien tai" in qtxt, qtxt)

        p1 = await bot_mod.execute_tool("music_control", {"action": "pause"}, g, None, caller, False)
        check("music: pause works", "Paused" in p1 and g._vc.is_paused())
        p2 = await bot_mod.execute_tool("music_control", {"action": "resume"}, g, None, caller, False)
        check("music: resume works", "Resumed" in p2 and g._vc.is_playing())
        sk = await bot_mod.execute_tool("music_control", {"action": "skip"}, g, None, caller, False)
        check("music: skip stops current track", "Skipped" in sk and not g._vc.is_playing())
        st = await bot_mod.execute_tool("music_control", {"action": "stop"}, g, None, caller, False)
        check("music: stop disconnects and clears queue", "Stopped" in st and g._vc.disconnected and not bot_mod.music_states[g.id].queue)
    finally:
        bot_mod._extract_track_info = real_extract
        bot_mod._create_audio_source = real_source
        bot_mod.music_states.pop(g.id, None)


run_now(music_checks())

# 7b. web search & SSRF guard
check("ssrf: blocks file://", not bot_mod._is_safe_public_url("file:///etc/passwd")[0])
check("ssrf: blocks localhost", not bot_mod._is_safe_public_url("http://localhost:8000")[0])
check("ssrf: blocks 127.0.0.1", not bot_mod._is_safe_public_url("http://127.0.0.1/admin")[0])
check("ssrf: blocks AWS/GCP metadata IP", not bot_mod._is_safe_public_url("http://169.254.169.254/latest")[0])
check("html: strips scripts and tags",
      bot_mod._strip_html("<html><script>alert(1)</script><body><h1>Hi &amp; Bye</h1></body></html>") == "Hi & Bye")
rss_sample = "<rss><channel><item><title>Vang <b>SJC</b></title><link>https://example.com/gold</link><description>90 trieu</description><pubDate>Thu, 26 Nov 2026</pubDate></item></channel></rss>"
parsed_rss = bot_mod._parse_rss_items(rss_sample, 5)
check("rss: parses title, link, desc", len(parsed_rss) == 1 and parsed_rss[0]["title"] == "Vang SJC" and parsed_rss[0]["link"] == "https://example.com/gold")
check("gnews: vi locale for Vietnamese query", "hl=vi&gl=VN" in bot_mod._google_news_rss_url("giá vàng hôm nay"))
check("gnews: en locale for ASCII query", "hl=en-US&gl=US" in bot_mod._google_news_rss_url("latest AI model 2026"))


async def prompt_date_check():
    real_facts, real_bots, real_b = bot_mod.get_facts, bot_mod.get_peer_bots, bot_mod.bot
    async def _no(): return []
    async def _one_fact(): return [{"fact": "a", "author": ""}]
    bot_mod.get_facts, bot_mod.get_peer_bots = _one_fact, _no
    try:
        built = await bot_mod.build_system_prompt()
        check("prompt: injects live date and 03/2026 web_search reminder",
              "# Thời gian hiện tại" in built and "03/2026" in built and "web_search" in built, built[:200])
    finally:
        bot_mod.get_facts, bot_mod.get_peer_bots, bot_mod.bot = real_facts, real_bots, real_b


run_now(prompt_date_check())

# 7b-2. Google News RSS article redirect resolution + SSRF guard
async def gnews_decode_check():
    class FakePage:
        def __init__(self, code, text, headers=None):
            self.status_code = code
            self.text = text
            self.headers = headers or {}
            self.is_redirect = False

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    class FakeGNewsClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            if "news.google.com/rss/articles/" in url:
                return FakePage(200, '<c-wiz data-n-a-id="ART1" data-n-a-ts="1795000" data-n-a-sg="SIG1"></c-wiz>')
            if url == "https://publisher.example/article":
                return FakePage(200, "<html><body><p>Bedrock bug details here</p></body></html>", {"content-type": "text/html"})
            return FakePage(404, "")

        async def post(self, url, **kw):
            return FakePage(200, ')]}\'\n[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"https://publisher.example/article\\",1]"]]')

    real_client = bot_mod.httpx.AsyncClient
    real_safe = bot_mod._is_safe_public_url
    bot_mod.httpx.AsyncClient = FakeGNewsClient
    bot_mod._is_safe_public_url = lambda u: (True, "") if "publisher.example" in u or "news.google.com" in u else real_safe(u)
    try:
        body = await bot_mod._fetch_url("https://news.google.com/rss/articles/CBMiTEST")
        check("gnews decode: fetches resolved publisher article", "Bedrock bug details here" in body, body)
    finally:
        bot_mod.httpx.AsyncClient = real_client
        bot_mod._is_safe_public_url = real_safe


run_now(gnews_decode_check())
check("fetch headers: Wikimedia uses descriptive bot UA",
      bot_mod._fetch_headers_for("https://en.wiktionary.org/wiki/hello")["User-Agent"].startswith("BeYeuBot/"))
check("fetch headers: general site keeps full Chrome UA",
      "AppleWebKit/537.36" in bot_mod._fetch_headers_for("https://example.com")["User-Agent"])

# 7c. send_file & read_channel embed fallback
async def file_and_embed_checks():
    sent_files = []

    class FileChan:
        id = 555
        name = "chat-chung"

        async def send(self, content=None, file=None):
            sent_files.append((content, file.filename, file.fp.read()))

        async def history(self, limit=5):
            yield type("M", (), {
                "author": type("A", (), {"display_name": "Carl-bot"})(),
                "content": "",
                "embeds": [type("E", (), {"title": "About Carl", "description": "Uptime 99%"})()],
            })()

    class FileGuild:
        def get_channel(self, cid):
            return FileChan() if cid == 555 else None

    fch = FileChan()
    fg = FileGuild()
    got = await bot_mod.execute_tool("send_file", {
        "filename": "../../hello world.py",
        "content": "print('ok')",
        "caption": "Day ne",
    }, fg, fch, None, False)
    check("file: sanitized filename and sent",
          "Sent file 'hello_world.py'" in got and sent_files[-1][1] == "hello_world.py" and sent_files[-1][2] == b"print('ok')",
          got)
    bad = await bot_mod.execute_tool("send_file", {"filename": "a.txt", "content": ""}, fg, fch, None, False)
    check("file: empty content rejected", "ERROR" in bad, bad)

    rd = await bot_mod.execute_tool("read_channel", {"channel_id": "555", "limit": 1}, fg, fch, None, False)
    check("read_channel: extracts embed text when content is empty",
          "[Carl-bot] About Carl: Uptime 99%" in rd, rd)


run_now(file_and_embed_checks())

# 7d. generate_image auto-fetch (b64_json) & 403 CDN fallback
async def image_gen_checks():
    import base64
    sent_imgs = []

    class ImgChan:
        id = 777
        name = "art"

        async def send(self, content=None, file=None):
            sent_imgs.append((file.filename, file.fp.read()))

    class FakeResp:
        def __init__(self, code, data):
            self.status_code = code
            self._data = data

        def json(self):
            return self._data

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    png_bytes = b"\x89PNG\r\n\x1a\nFAKEPNG"

    class FakeClientB64:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            return FakeResp(200, {"data": [{"b64_json": base64.b64encode(png_bytes).decode()}]})

    real_client = bot_mod.httpx.AsyncClient
    bot_mod.httpx.AsyncClient = FakeClientB64
    try:
        out = await bot_mod._generate_and_send_image(ImgChan(), "cute cat", "1:1")
        check("img: b64_json auto-sent to Discord", "Generated and sent image" in out and sent_imgs[0][1] == png_bytes, out)
    finally:
        bot_mod.httpx.AsyncClient = real_client

    class FakeClient403(FakeClientB64):
        async def post(self, url, json=None):
            if (json or {}).get("response_format") == "b64_json":
                return FakeResp(500, {"error": "403"})
            return FakeResp(200, {"data": [{"url": "https://lh3.googleusercontent.com/gg-dl/test"}]})

        async def get(self, url, **kw):
            resp = FakeResp(200, {})
            resp.headers = {"content-type": "image/jpeg"}
            resp.content = b"\xff\xd8\xffFAKEJPG"
            return resp

    async def no_dl(_url):
        return None

    async def no_poly(_client, _prompt, _ar=""):
        return None, "HTTP 503"

    real_dl = bot_mod._download_image_bytes
    real_poly = bot_mod._generate_via_pollinations
    bot_mod.httpx.AsyncClient = FakeClient403
    bot_mod._download_image_bytes = no_dl
    try:
        # When Gemini CDN 403s, Pollinations fallback delivers a real image file.
        sent_imgs.clear()
        out_poly = await bot_mod._generate_and_send_image(ImgChan(), "cute cat", "16:9")
        check("img: Pollinations fallback sends image on CDN 403",
              "Pollinations" in out_poly and sent_imgs and sent_imgs[0][0] == "image.jpg", out_poly)

        # If IMAGE_DEFAULT_ONLY is on, skip Gemini API and Pollinations completely.
        bot_mod.IMAGE_DEFAULT_ONLY = True
        sent_imgs.clear()
        out_def = await bot_mod._generate_and_send_image(ImgChan(), "cute cat")
        check("img: IMAGE_DEFAULT_ONLY skips Pollinations fallback",
              "IMAGE_FETCH_BLOCKED" in out_def and "IMAGE_DEFAULT_ONLY" in out_def and not sent_imgs, out_def)
        bot_mod.IMAGE_DEFAULT_ONLY = False

        # If Pollinations also fails, report the 403 URL honestly.
        bot_mod._generate_via_pollinations = no_poly
        out2 = await bot_mod._generate_and_send_image(ImgChan(), "cute cat")
        check("img: 403 CDN fallback reports URL honestly",
              "IMAGE_FETCH_BLOCKED" in out2 and "lh3.googleusercontent.com" in out2, out2)
    finally:
        bot_mod.IMAGE_DEFAULT_ONLY = False
        bot_mod.httpx.AsyncClient = real_client
        bot_mod._download_image_bytes = real_dl
        bot_mod._generate_via_pollinations = real_poly

    # Chrome extension WebSocket relay server & priority check
    import aiohttp
    from aiohttp import web
    real_secret = bot_mod.RELAY_SECRET
    bot_mod.RELAY_SECRET = "test-relay-secret"
    app = web.Application()
    app.router.add_get("/health", bot_mod.relay_hub.handle_health)
    app.router.add_get("/ws/relay", bot_mod.relay_hub.handle_ws)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(f"http://127.0.0.1:{port}/ws/relay?token=wrong") as bad_r:
                check("relay: rejects wrong token with 401", bad_r.status == 401, bad_r.status)

            async with sess.ws_connect(f"ws://127.0.0.1:{port}/ws/relay?token=test-relay-secret") as ws:
                await ws.send_json({"type": "hello", "version": "1.0.0", "temp_chat": True})
                await ws.send_json({"type": "ping", "ts": 123})
                pong = await ws.receive_json()
                check("relay: ping/pong keeps WS alive", pong.get("type") == "pong" and pong.get("ts") == 123, pong)

                async def _serve_job():
                    req = await ws.receive_json()
                    await ws.send_json({
                        "type": "image_result",
                        "id": req["id"],
                        "ok": True,
                        "b64": base64.b64encode(png_bytes).decode(),
                        "mime": "image/png",
                    })

                sent_imgs.clear()
                worker = asyncio.create_task(_serve_job())
                out_relay = await bot_mod._generate_and_send_image(ImgChan(), "relay cat", "1:1")
                await worker
                check("relay: _generate_and_send_image prefers connected Chrome relay",
                      "Chrome relay" in out_relay and sent_imgs and sent_imgs[0][1] == png_bytes, out_relay)
    finally:
        await runner.cleanup()
        bot_mod.RELAY_SECRET = real_secret


run_now(image_gen_checks())
check("img query: extracts prompt after prefix", bot_mod.image_query("vẽ ảnh con mèo") == "con mèo")
check("img query: ignores plain text", bot_mod.image_query("hôm nay trời đẹp") is None)

# 7e. attachment reading (text, PDF, image -> multimodal live payload, plain text in DB)
async def attachment_checks():
    import io
    # Minimal valid 1-page PDF with text "Hello PDF" generated or mocked via _extract_pdf_text
    class FakeAtt:
        def __init__(self, filename, data, ct):
            self.filename = filename
            self._data = data
            self.size = len(data)
            self.content_type = ct

        async def read(self):
            return self._data

    real_pdf = bot_mod._extract_pdf_text
    bot_mod._extract_pdf_text = lambda data: "[Page 1]\nHello PDF World"
    try:
        txt, media = await bot_mod.extract_attachments([
            FakeAtt("notes.py", b"x = 42\n", "text/x-python"),
            FakeAtt("doc.pdf", b"%PDF-1.4...", "application/pdf"),
            FakeAtt("pic.png", b"\x89PNG\r\n\x1a\n", "image/png"),
        ])
    finally:
        bot_mod._extract_pdf_text = real_pdf

    check("att: text file extracted", "[Nội dung tệp: notes.py]" in txt and "x = 42" in txt, txt[:100])
    check("att: pdf text extracted", "[Nội dung PDF: doc.pdf]" in txt and "Hello PDF World" in txt)
    check("att: image encoded as OpenAI image_url data URI",
          len(media) == 1 and media[0]["type"] == "image_url"
          and media[0]["image_url"]["url"].startswith("data:image/png;base64,"))


run_now(attachment_checks())

# ---------- 8. Gemini API image generation + skill system ----------

async def gemini_api_image_check():
    import base64
    sent = []

    class ApiChan:
        id = 888
        name = "gemini-art"

        async def send(self, content=None, file=None):
            sent.append((file.filename, file.fp.read()))

    class FakeResp:
        def __init__(self, code, data):
            self.status_code = code
            self._data = data
            self.text = json.dumps(data)

        def json(self):
            return self._data

    png_bytes = b"\x89PNG\r\n\x1a\nGEMINI_API"
    seen_headers = []

    class FakeApiClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            seen_headers.append((url, headers))
            return FakeResp(200, {
                "candidates": [{
                    "content": {
                        "parts": [
                            {"text": "ok"},
                            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(png_bytes).decode()}},
                        ]
                    }
                }]
            })

    real_key = bot_mod.GEMINI_API_KEY
    real_client = bot_mod.httpx.AsyncClient
    bot_mod.GEMINI_API_KEY = "test-api-key"
    bot_mod.httpx.AsyncClient = FakeApiClient
    try:
        out = await bot_mod._generate_and_send_image(ApiChan(), "cyberpunk cat", "16:9")
        check("gemini api img: sends decoded bytes to Discord",
              "Gemini API" in out and sent and sent[0][0] == "image.png" and sent[0][1] == png_bytes, out)
        check("gemini api img: sends key in x-goog-api-key header (not URL)",
              seen_headers and "key=" not in seen_headers[0][0]
              and seen_headers[0][1].get("x-goog-api-key") == "test-api-key")
    finally:
        bot_mod.GEMINI_API_KEY = real_key
        bot_mod.httpx.AsyncClient = real_client


run_now(gemini_api_image_check())

# 8b. Skill parser, disk loader, prompt rendering, and admin gating
parsed_skill = bot_mod.parse_skill_md(
    "---\nname: My Cool Skill!\ndescription: Helps test\nalways_on: true\n---\nStep 1: Do X\n"
)
check("skill parse: normalizes name and frontmatter",
      parsed_skill["name"] == "my-cool-skill"
      and parsed_skill["description"] == "Helps test"
      and parsed_skill["always_on"] is True
      and "Step 1: Do X" in parsed_skill["instructions"], str(parsed_skill))

check("skill disk: starter summarize-chat skill loaded",
      "summarize-chat" in bot_mod.DISK_SKILLS, str(list(bot_mod.DISK_SKILLS)))


async def skill_tool_checks():
    import tempfile, shutil
    # Non-admin cannot install or remove skills
    denied = await bot_mod.execute_tool("install_skill", {
        "name": "evil", "description": "x", "instructions": "y"
    }, None, None, None, False)
    check("skill: non-admin install_skill DENIED", "DENIED" in denied, denied)

    denied_rm = await bot_mod.execute_tool("remove_skill", {"name": "summarize-chat"}, None, None, None, False)
    check("skill: non-admin remove_skill DENIED", "DENIED" in denied_rm, denied_rm)

    # Admin can install (in-memory/no-pool check validates args cleanly) and read starter skill
    lst = await bot_mod.execute_tool("list_skills", {}, None, None, None, False)
    check("skill: list_skills shows summarize-chat", "summarize-chat" in lst, lst)

    rd = await bot_mod.execute_tool("read_skill", {"name": "summarize-chat"}, None, None, None, False)
    check("skill: read_skill returns full instructions", "read_channel" in rd, rd[:120])

    # Progressive disclosure: large skill instructions switch to read_skill catalog mode
    rendered_small = bot_mod.render_skills([
        {"name": "s1", "description": "d1", "instructions": "short", "enabled": True, "always_on": False}
    ])
    check("skill render: small skills inline instructions", "short" in rendered_small)
    rendered_large = bot_mod.render_skills([
        {"name": "s1", "description": "d1", "instructions": "A" * 3000, "enabled": True, "always_on": False},
        {"name": "s2", "description": "d2", "instructions": "ALWAYS", "enabled": True, "always_on": True},
    ])
    check("skill render: large skills use progressive disclosure + always_on",
          "read_skill(name)" in rendered_large and ("A" * 100) not in rendered_large and "ALWAYS" in rendered_large)

    # Disk tool.py skill loading + execution
    tmp = tempfile.mkdtemp()
    real_tools = list(bot_mod.TOOLS)
    real_handlers = dict(bot_mod.SKILL_HANDLERS)
    real_disk = dict(bot_mod.DISK_SKILLS)
    try:
        sdir = os.path.join(tmp, "ping-skill")
        os.makedirs(sdir)
        with open(os.path.join(sdir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: ping-skill\ndescription: ping test\n---\nUse ping_tool.")
        with open(os.path.join(sdir, "tool.py"), "w", encoding="utf-8") as f:
            f.write(
                'TOOL_SCHEMA = {"type": "function", "function": {"name": "ping_tool", "description": "p", "parameters": {"type": "object", "properties": {}}}}\n'
                'async def run(args, ctx):\n'
                '    return "PONG!"\n'
            )
        bot_mod.load_disk_skills(tmp)
        check("skill tool.py: registered into TOOLS and SKILL_HANDLERS",
              "ping_tool" in bot_mod.SKILL_HANDLERS and any(t["function"]["name"] == "ping_tool" for t in bot_mod.TOOLS))
        got = await bot_mod.execute_tool("ping_tool", {}, None, None, None, False)
        check("skill tool.py: executes handler", got == "PONG!", got)
    finally:
        bot_mod.TOOLS[:] = real_tools
        bot_mod.SKILL_HANDLERS.clear()
        bot_mod.SKILL_HANDLERS.update(real_handlers)
        bot_mod.DISK_SKILLS.clear()
        bot_mod.DISK_SKILLS.update(real_disk)
        shutil.rmtree(tmp, ignore_errors=True)


run_now(skill_tool_checks())

print()
if failures:
    print(f"FAILED: {failures}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
