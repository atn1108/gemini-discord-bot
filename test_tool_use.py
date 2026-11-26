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
    real_prompt = bot_mod.SYSTEM_PROMPT
    bot_mod.SYSTEM_PROMPT = "PERSONA"

    async def no_facts():
        return []

    async def no_bots():
        return []

    async def some_bots():
        return [{"bot_id": 1, "username": "Dyno", "commands": ["dadjoke"]}]

    bot_mod.get_facts, bot_mod.get_peer_bots = no_facts, no_bots
    p = await bot_mod.build_system_prompt()
    check("prompt: nothing to add -> persona untouched", p == "PERSONA", p[:60])

    bot_mod.get_peer_bots = some_bots
    p = await bot_mod.build_system_prompt()
    check("prompt: peer bots section added", "# Bot khác trong server" in p)
    check("prompt: persona still first", p.startswith("PERSONA"))
    check("prompt: learned command reaches the model", "`/dadjoke`" in p)

    bot_mod.get_facts, bot_mod.get_peer_bots = real_facts, real_bots
    bot_mod.SYSTEM_PROMPT = real_prompt


run_now(prompt_check())

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

print()
if failures:
    print(f"FAILED: {failures}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
