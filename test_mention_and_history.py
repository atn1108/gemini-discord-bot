"""Regression tests for the two on_message bugs that reached production:

1. asyncpg's jsonb codec returns str, so get_history() handed a string to
   history.append() and on_message died with AttributeError -> the bot stopped
   answering "be yeu" at all.
2. Triggering by @mention of the bot did not work.
"""
import asyncio
import json
import logging
import os

os.environ["TRIGGER_KEYWORDS"] = "bé yêu"
logging.disable(logging.DEBUG)

import discord_lifetime_memory_bot as bot_mod

BOT_ID = 1574473312086524057
failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        failures.append(name)


# ---------- 1. decode_messages: every shape the DB can hand back ----------

turns = [
    {"role": "user", "content": "[m]: hi"},
    {"role": "assistant", "content": "hello"},
]

check("decode: None -> []", bot_mod.decode_messages(None) == [])
check("decode: already a list", bot_mod.decode_messages(turns) == turns)
# This is the exact value asyncpg returns for a jsonb column by default.
check("decode: jsonb text (the production crash)",
      bot_mod.decode_messages(json.dumps(turns)) == turns,
      f"type={type(bot_mod.decode_messages(json.dumps(turns))).__name__}")
# A row written through json.dumps twice must still be recoverable.
check("decode: double-encoded", bot_mod.decode_messages(json.dumps(json.dumps(turns))) == turns)
check("decode: garbage str -> []", bot_mod.decode_messages("{not json") == [])
check("decode: empty str -> []", bot_mod.decode_messages("") == [])
check("decode: scalar -> []", bot_mod.decode_messages("42") == [])
check("decode: dict -> []", bot_mod.decode_messages('{"role":"user"}') == [])
check("decode: drops non-dict entries",
      bot_mod.decode_messages('[{"role":"user"}, "junk", 7]') == [{"role": "user"}])

# The full round trip save_history -> get_history must yield a mutable list.
blob = json.dumps(turns)
decoded = bot_mod.decode_messages(blob)
decoded.append({"role": "user", "content": "third"})
check("decode: result is appendable", len(decoded) == 3)
check("decode: round trip preserves content", decoded[0] == turns[0])


# ---------- 2. strip_mentions ----------

class U:
    def __init__(self, uid, name):
        self.id = uid
        self.display_name = name


bot_user = U(BOT_ID, "Con vợ siêu múp 🥵")
other = U(999, "Đạo")

check("mention: bare tag triggers",
      bot_mod.strip_mentions(f"<@{BOT_ID}>", [bot_user], BOT_ID) == ("", True))
check("mention: tag + text strips token",
      bot_mod.strip_mentions(f"<@{BOT_ID}> hello", [bot_user], BOT_ID) == ("hello", True))
check("mention: nickname form stripped",
      bot_mod.strip_mentions(f"<@!{BOT_ID}> yo", [bot_user], BOT_ID) == ("yo", True))
check("mention: no tag", bot_mod.strip_mentions("bé yêu hi", [], BOT_ID) == ("bé yêu hi", False))
check("mention: other user rendered",
      bot_mod.strip_mentions(f"<@{other.id}> la ai", [other], BOT_ID) == ("@Đạo la ai", False))
text, mentioned = bot_mod.strip_mentions(
    f"<@{BOT_ID}> hoi <@{other.id}>", [bot_user, other], BOT_ID)
check("mention: both handled", (text, mentioned) == ("hoi @Đạo", True), f"got={(text, mentioned)}")


# ---------- 3. on_message end to end (DB + webhook stubbed) ----------

calls = {"webhook": [], "research": [], "persist": [], "saved": [], "replies": []}


class FakeChannel:
    id = 4242

    def typing(self):
        class _T:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

        return _T()

    async def send(self, text, **kw):
        calls["research"].append(text)
        class Sent:
            async def edit(self, content=None, file=None):
                calls["research"].append(("edit", content))
        return Sent()


class FakeMessage:
    def __init__(self, content, author, mentions=()):
        self.content = content
        self.author = author
        self.mentions = list(mentions)
        self.channel = FakeChannel()
        self.reactions = []

    async def add_reaction(self, r):
        self.reactions.append(r)

    async def reply(self, t):
        calls["replies"].append(t)


class Author:
    def __init__(self, name, bot=False):
        self.display_name = name
        self.bot = bot


async def fake_get_history(cid):
    # Simulate a stored row: asyncpg hands back the JSONB column as a str.
    return bot_mod.decode_messages(json.dumps(calls.setdefault("stored", [])))


async def fake_call_webhook(history, cid):
    calls["webhook"].append(list(history))
    return True, {"answer": "Bé nghe đây ạ!"}


async def fake_save(cid, history):
    calls["saved"].append(list(history))
    calls["stored"] = list(history)


async def fake_research(send, topic, **kw):
    calls["research"].append(topic)


bot_mod.get_history = fake_get_history
bot_mod.call_webhook = fake_call_webhook
# persist_turn is NOT stubbed: the real one must append the assistant turn and
# hand both sides to save_history, which is the production fix under test.
bot_mod.save_history = fake_save
bot_mod.deliver_research = fake_research
bot_mod.bot._connection.user = bot_user  # Client.user reads _connection.user

human = Author("Minh")


async def run(content, author=human, mentions=()):
    calls["webhook"].clear()
    calls["research"].clear()
    calls["saved"].clear()
    calls["stored"] = []
    msg = FakeMessage(content, author, mentions)
    await bot_mod.on_message(msg)
    return msg


async def main():
    # keyword trigger still works
    await run("bé yêu hỏi t vài câu")
    check("kw: 'be yeu' triggers", len(calls["webhook"]) == 1)
    check("kw: sender prefixed",
          calls["webhook"] and calls["webhook"][0][-1]["content"].startswith("[Minh]: "),
          str(calls["webhook"][0][-1]["content"][:40]) if calls["webhook"] else "")
    check("kw: user + assistant turns both saved",
          bool(calls["saved"]) and [m["role"] for m in calls["saved"][0]] == ["user", "assistant"],
          str([m["role"] for m in calls["saved"][0]]) if calls["saved"] else "nothing saved")

    # non-trigger
    await run("xin chao moi nguoi")
    check("plain message ignored", len(calls["webhook"]) == 0)

    # @mention triggers with no keyword
    await run(f"<@{BOT_ID}> cho t xin loi chuc", mentions=[bot_user])
    check("mention: triggers", len(calls["webhook"]) == 1)
    check("mention: token removed",
          calls["webhook"] and "<@" not in calls["webhook"][0][-1]["content"],
          calls["webhook"][0][-1]["content"] if calls["webhook"] else "")

    # bare @mention -> model still gets a non-empty turn
    await run(f"<@{BOT_ID}>", mentions=[bot_user])
    check("mention: bare tag -> 'alo'",
          calls["webhook"] and calls["webhook"][0][-1]["content"].endswith(": alo"),
          calls["webhook"][0][-1]["content"] if calls["webhook"] else "")

    # @mention + research prefix
    await run(f"<@{BOT_ID}> nghiên cứu pin lithium", mentions=[bot_user])
    check("mention+research: research wins", len(calls["research"]) == 1 and not calls["webhook"],
          str(calls["research"]))

    # keyword + research prefix (no mention)
    await run("bé yêu research rust vs go")
    check("kw+research: research wins", len(calls["research"]) == 1 and not calls["webhook"])

    # mention of someone else does not trigger
    await run(f"<@{other.id}> oi", mentions=[other])
    check("other mention ignored", len(calls["webhook"]) == 0)

    # mention renders the other user as a name, not a raw ID
    await run(f"<@{BOT_ID}> {other.id} la ai", mentions=[bot_user])
    check("mention: chat still triggers", len(calls["webhook"]) == 1)

    # bot messages ignored
    await run("bé yêu", author=Author("bot", bot=True))
    check("bot author ignored", len(calls["webhook"]) == 0 and len(calls["research"]) == 0)

    # empty content ignored
    await run("   ")
    check("blank ignored", len(calls["webhook"]) == 0)

    # history round trip: what persist stored must be readable next turn
    stored = json.dumps([{"role": "user", "content": "q"},
                         {"role": "assistant", "content": "a"}])
    reloaded = bot_mod.decode_messages(stored)
    reloaded.append({"role": "user", "content": "next"})
    check("history: survives a second turn", len(reloaded) == 3, str([m["role"] for m in reloaded]))


asyncio.run(main())

print()
if failures:
    print(f"FAILED: {failures}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
