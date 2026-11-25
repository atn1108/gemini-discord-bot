"""Regression tests for on_message behaviour.

Covers the bugs that reached production plus the context feature:

1. asyncpg's jsonb codec returns str, so get_history() handed a string to
   history.append() and on_message died with AttributeError -> the bot stopped
   answering "be yeu" at all.
2. Triggering by @mention of the bot did not work.
3. Plain chatter was invisible to the model, so tagging it mid-argument made it
   ask for the context again.
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
crash_value = bot_mod.decode_messages(json.dumps(turns))
check("decode: jsonb text (the production crash)", crash_value == turns,
      f"type={type(crash_value).__name__}")
check("decode: double-encoded", bot_mod.decode_messages(json.dumps(json.dumps(turns))) == turns)
check("decode: garbage str -> []", bot_mod.decode_messages("{not json") == [])
check("decode: empty str -> []", bot_mod.decode_messages("") == [])
check("decode: scalar -> []", bot_mod.decode_messages("42") == [])
check("decode: dict -> []", bot_mod.decode_messages('{"role":"user"}') == [])
check("decode: drops non-dict entries",
      bot_mod.decode_messages('[{"role":"user"}, "junk", 7]') == [{"role": "user"}])

decoded = bot_mod.decode_messages(json.dumps(turns))
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

tmp_buf = bot_mod.pending[999999]
check("pending buffer is capped", tmp_buf.maxlen == bot_mod.PENDING_MAX,
      f"maxlen={tmp_buf.maxlen}")
del bot_mod.pending[999999]


# ---------- 3. on_message end to end (DB + webhook stubbed) ----------

calls = {"webhook": [], "research": [], "saved": [], "replies": [], "ok": True}


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
    # Simulate a stored row: asyncpg hands the JSONB column back as a str.
    return bot_mod.decode_messages(json.dumps(calls.setdefault("stored", [])))


async def fake_call_webhook(history, cid):
    calls["webhook"].append(list(history))
    await asyncio.sleep(0.02)  # let a concurrent message interleave
    if not calls["ok"]:
        return False, {"error": "boom"}
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


async def run(content, author=human, mentions=(), reset=True, ok=True):
    if reset:
        calls["webhook"].clear()
        calls["research"].clear()
        calls["saved"].clear()
        calls["replies"].clear()
        calls["stored"] = []
        bot_mod.pending.clear()
    calls["ok"] = ok
    msg = FakeMessage(content, author, mentions)
    await bot_mod.on_message(msg)
    return msg


async def main():
    # keyword trigger
    await run("bé yêu hỏi t vài câu")
    check("kw: 'be yeu' triggers", len(calls["webhook"]) == 1)
    check("kw: sender prefixed",
          calls["webhook"] and calls["webhook"][0][-1]["content"].startswith("[Minh]: "),
          calls["webhook"][0][-1]["content"][:40] if calls["webhook"] else "")
    check("kw: user + assistant turns both saved",
          bool(calls["saved"]) and [m["role"] for m in calls["saved"][0]] == ["user", "assistant"],
          str([m["role"] for m in calls["saved"][0]]) if calls["saved"] else "nothing saved")

    # plain chatter: buffered, never answered
    await run("ai sai trong cau ay?")
    check("chatter: not answered", len(calls["webhook"]) == 0)
    check("chatter: buffered as context",
          len(bot_mod.pending[FakeChannel.id]) == 1,
          str(bot_mod.pending[FakeChannel.id]))

    await run("t cho rang ban doi sai", reset=False)
    check("chatter: accumulates", len(bot_mod.pending[FakeChannel.id]) == 2)

    # the tag must carry everything that was said since the last answer
    await run(f"<@{BOT_ID}> ai sai day", mentions=[bot_user], reset=False)
    payload = calls["webhook"][0] if calls["webhook"] else []
    check("tag: payload = 2 chatter + 1 question", len(payload) == 3,
          str([m["content"][:30] for m in payload]))
    check("tag: context comes first",
          payload and payload[0]["content"].endswith("ai sai trong cau ay?"),
          payload[0]["content"][:40] if payload else "")
    check("tag: question is last",
          payload and payload[-1]["content"].endswith("ai sai day"),
          payload[-1]["content"][:40] if payload else "")
    check("tag: context flushed after success", FakeChannel.id not in bot_mod.pending)
    check("tag: whole transcript persisted",
          bool(calls["saved"]) and [m["role"] for m in calls["saved"][0]]
          == ["user", "user", "user", "assistant"],
          str([m["role"] for m in calls["saved"][0]]) if calls["saved"] else "")

    # non-trigger without any keyword
    await run("xin chao moi nguoi")
    check("plain message ignored", len(calls["webhook"]) == 0)

    # @mention triggers with no keyword
    await run(f"<@{BOT_ID}> cho t xin loi chuc", mentions=[bot_user])
    check("mention: triggers", len(calls["webhook"]) == 1)
    check("mention: token removed", "<@" not in calls["webhook"][0][-1]["content"],
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

    # bot messages ignored
    await run("bé yêu", author=Author("bot", bot=True))
    check("bot author ignored", len(calls["webhook"]) == 0 and len(calls["research"]) == 0)

    # blank content ignored
    await run("   ")
    check("blank ignored", len(calls["webhook"]) == 0)

    # a failed call must not swallow the buffered context
    await run("chatter truoc loi", ok=True)
    await run(f"<@{BOT_ID}> hoi sau loi", mentions=[bot_user], reset=False, ok=False)
    check("failure: error reported", len(calls["replies"]) == 1 and "boom" in calls["replies"][0],
          str(calls["replies"]))
    check("failure: context kept for retry",
          FakeChannel.id in bot_mod.pending and len(bot_mod.pending[FakeChannel.id]) == 1,
          f"pending={len(bot_mod.pending.get(FakeChannel.id, []))}")
    check("failure: nothing persisted", len(calls["saved"]) == 0)

    # retry after the failure must still carry the buffered context
    await run(f"<@{BOT_ID}> hoi lai", mentions=[bot_user], reset=False, ok=True)
    retry = calls["webhook"][-1] if calls["webhook"] else []
    check("retry: carries the kept context", len(retry) == 2,
          str([m["content"][:30] for m in retry]))

    # two tags at once: the per-channel lock must not lose either turn
    calls["webhook"].clear()
    calls["saved"].clear()
    calls["stored"] = []
    bot_mod.pending.clear()
    calls["ok"] = True
    msgs = [FakeMessage(f"<@{BOT_ID}> cau {i}", human, [bot_user]) for i in range(2)]
    await asyncio.gather(*(bot_mod.on_message(m) for m in msgs))
    final = calls["saved"][-1] if calls["saved"] else []
    contents = [m["content"] for m in final]
    check("concurrent: both questions stored",
          any("cau 0" in c for c in contents) and any("cau 1" in c for c in contents),
          str(contents))
    check("concurrent: answers not overwritten",
          [m["role"] for m in final].count("assistant") == 2,
          str([m["role"] for m in final]))
    check("concurrent: order kept",
          contents.index(next(c for c in contents if "cau 0" in c))
          < contents.index(next(c for c in contents if "cau 1" in c)))

    # history survives a second turn
    reloaded = bot_mod.decode_messages(json.dumps(
        [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]))
    reloaded.append({"role": "user", "content": "next"})
    check("history: survives a second turn", len(reloaded) == 3,
          str([m["role"] for m in reloaded]))


asyncio.run(main())

print()
if failures:
    print(f"FAILED: {failures}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
