# -*- coding: utf-8 -*-
"""
传话筒机器人 · 离线验收测试
=====================================================================
不需要网络、不需要 Token、不启动轮询。用模拟对象跑通全部核心路径：
  消息类型识别 / 命令过滤 / 索引持久化 / 转发与三级兜底 / 相册合并 /
  双向回传 / handler 分组矩阵 / 私聊开关 / 配置模式

运行：python test_bot.py
"""
from __future__ import annotations

import itertools
import json
import os
import shutil
import sys
import tempfile
import unittest
import warnings
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

TMP = tempfile.mkdtemp(prefix="relay-test-")
# 必须在本模块 import bot 之前设好，否则 bot.py 的启动自检会退出
os.environ["TOKEN"] = "123456:TESTTOKEN"
os.environ["ADMIN_CHAT_ID"] = "999"
os.environ["STATE_FILE"] = str(Path(TMP) / "map.json")
os.environ["SEND_ACK"] = "false"
os.environ["ADMIN_HINT"] = "true"
os.environ["PRIVATE_ONLY"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parent))
warnings.filterwarnings("ignore", message="No `JobQueue` set up.*")
import bot  # noqa: E402

from telegram import Chat, Message, MessageEntity, Update, User  # noqa: E402
from telegram.constants import MessageEntityType  # noqa: E402

ADMIN = bot.ADMIN_CHAT_ID
FIELDS = (
    "text caption caption_entities entities photo video animation document voice "
    "audio video_note sticker location contact poll dice venue game "
    "media_group_id reply_to_message forward_from"
).split()


def make_message(**kw):
    """造一条可编程的假消息，所有媒体字段默认 None（避免 MagicMock 的 truthy 陷阱）。"""
    msg_id = kw.pop("message_id", 1)
    chat_id = kw.pop("chat_id", 111)
    user = kw.pop("from_user", SimpleNamespace(id=111, full_name="张三", username="zhangsan"))
    forward = kw.pop("forward", None)
    copy = kw.pop("copy", None)
    reply_text = kw.pop("reply_text", None)

    data = {k: None for k in FIELDS}
    data.update(message_id=msg_id, chat_id=chat_id, from_user=user)
    data.update(kw)
    m = SimpleNamespace(**data)
    m.forward = forward or AsyncMock(return_value=SimpleNamespace(message_id=1001))
    m.copy = copy or AsyncMock(return_value=SimpleNamespace(message_id=2001))
    m.reply_text = reply_text or AsyncMock(return_value=SimpleNamespace(message_id=3001))
    return m


def make_bot():
    """造一个假 Bot，所有 send_* 返回递增的 message_id。"""
    counter = itertools.count(5000)

    def sent(*_a, **_kw):
        return SimpleNamespace(message_id=next(counter))

    b = MagicMock()
    for name in (
        "send_message", "send_photo", "send_video", "send_animation",
        "send_document", "send_voice", "send_audio", "send_video_note",
        "send_sticker", "send_location", "send_contact", "send_dice",
    ):
        setattr(b, name, AsyncMock(side_effect=sent))
    return b


def make_context():
    return SimpleNamespace(bot=make_bot(), bot_data={})


def real_update(chat_id, text="hi", reply=None, chat_type="private", entities=None):
    """造一个真正的 PTB Update，用于验证 handler 的 check_update 匹配矩阵。"""
    msg = Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=chat_id, first_name="T", is_bot=False),
        text=text,
        reply_to_message=reply,
        entities=entities,
    )
    return Update(update_id=1, message=msg)


class RelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.forward_map.clear()
        bot.album_info.clear()
        bot.MAX_TRACKED = 5000
        bot.PRIVATE_ONLY = False
        bot.ADMIN_CHAT_ID = ADMIN
        bot.ADMIN_HINT = True
        bot.REPLY_QUOTE = True
        if bot.STATE_FILE.exists():
            bot.STATE_FILE.unlink()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    # ---------------------------------------------------------- 纯逻辑
    def test_describe_all_types(self):
        cases = [
            ("text", "📝 文字"), ("photo", "🖼️ 图片"), ("video", "🎬 视频"),
            ("animation", "🎞️ 动图 / GIF"), ("document", "📎 文件"),
            ("voice", "🎙️ 语音"), ("audio", "🎵 音频"),
            ("video_note", "📹 圆形视频"), ("sticker", "😀 贴纸"),
            ("location", "📍 位置"), ("contact", "👤 联系人"),
            ("poll", "📊 投票"), ("dice", "🎲 骰子"),
        ]
        for field, label in cases:
            with self.subTest(field=field):
                m = make_message(**{field: SimpleNamespace(file_id="X")})
                self.assertEqual(bot.describe(m), label)

    def test_bot_command_regex(self):
        for s in ("/start", "/start@my_bot", "/help", "/id 123", "/send 1 hi"):
            self.assertIsNotNone(bot.BOT_CMD_RE.match(s), s)
        for s in ("/unknown", "hello", "请发 /start"):
            self.assertIsNone(bot.BOT_CMD_RE.match(s), s)

    def test_sender_card_escapes_html(self):
        m = make_message(
            text="hi",
            from_user=SimpleNamespace(id=1, full_name="<script>x</script>", username="u"),
        )
        card = bot.sender_card(m)
        self.assertNotIn("<script>", card)
        self.assertIn("&lt;script&gt;", card)
        self.assertIn("<code>1</code>", card)

    # ---------------------------------------------------------- 转发索引
    def test_track_persist_reload_and_evict(self):
        bot.MAX_TRACKED = 3
        for mid, uid in ((1, 111), (2, 222), (3, 333), (4, 444)):
            bot.track(mid, uid)
        self.assertIsNone(bot.lookup(1), "最旧记录应被淘汰")
        self.assertEqual(bot.lookup(4), 444)
        self.assertTrue(bot.STATE_FILE.exists(), "索引应已落盘")
        bot.forward_map.clear()
        bot.load_state()
        self.assertEqual(bot.lookup(2), 222, "重启后仍应能找回用户")

    # ---------------------------------------------------------- 复制 / 兜底
    async def test_clone_copy_success(self):
        m, ctx = make_message(text="hi"), make_context()
        self.assertEqual(await bot.clone_message(ctx.bot, m, 111), 2001)
        ctx.bot.send_message.assert_not_awaited()

    async def test_clone_copy_fails_falls_back_to_file_id(self):
        m = make_message(
            photo=[SimpleNamespace(file_id="PHOTO_1")],
            caption="说明文字",
            copy=AsyncMock(side_effect=Exception("copyMessage 不支持")),
        )
        ctx = make_context()
        await bot.clone_message(ctx.bot, m, 111)
        ctx.bot.send_photo.assert_awaited_once()
        self.assertEqual(ctx.bot.send_photo.await_args.kwargs["photo"], "PHOTO_1")
        self.assertEqual(ctx.bot.send_photo.await_args.kwargs["caption"], "说明文字")

    # ---------------------------------------------------------- 用户 -> 管理员
    async def test_forward_text_success(self):
        m, ctx = make_message(text="你好"), make_context()
        await bot.forward_to_admin(SimpleNamespace(message=m), ctx)
        m.forward.assert_awaited_once_with(chat_id=ADMIN)
        self.assertEqual(bot.lookup(1001), 111, "转发消息应可回复")
        ctx.bot.send_message.assert_awaited_once()
        sent = ctx.bot.send_message.await_args
        self.assertEqual(sent.kwargs["chat_id"], ADMIN)
        self.assertIn("张三", sent.kwargs["text"])
        self.assertEqual(bot.lookup(5000), 111, "发送者信息条也应可回复")
        self.assertEqual(bot.lookup_origin(1001), 1, "应记下对方原消息 ID 用于引用")

    async def test_forward_fails_then_copy(self):
        m = make_message(
            text="你好",
            forward=AsyncMock(side_effect=Exception("禁止转发")),
            copy=AsyncMock(return_value=SimpleNamespace(message_id=2002)),
        )
        ctx = make_context()
        await bot.forward_to_admin(SimpleNamespace(message=m), ctx)
        m.copy.assert_awaited_once_with(chat_id=ADMIN)
        self.assertEqual(bot.lookup(2002), 111, "复制的消息也要能回复")

    async def test_forward_and_copy_fail_text_still_delivered(self):
        m = make_message(
            text="重要内容",
            forward=AsyncMock(side_effect=Exception("禁止转发")),
            copy=AsyncMock(side_effect=Exception("copyMessage 不支持")),
        )
        ctx = make_context()
        await bot.forward_to_admin(SimpleNamespace(message=m), ctx)
        texts = [c.kwargs.get("text", "") for c in ctx.bot.send_message.await_args_list]
        self.assertTrue(any("重要内容" in t for t in texts), "正文绝不能丢")

    async def test_last_resort_warning_is_trackable(self):
        m = make_message(text="重要内容", forward=AsyncMock(side_effect=Exception("x")))
        ctx = make_context()
        with patch.object(bot, "clone_message", new=AsyncMock(side_effect=Exception("boom"))):
            await bot.forward_to_admin(SimpleNamespace(message=m), ctx)
        ctx.bot.send_message.assert_awaited_once()
        text = ctx.bot.send_message.await_args.kwargs["text"]
        self.assertIn("⚠️", text)
        self.assertEqual(bot.lookup(5000), 111, "告警消息也可直接回复")

    async def test_album_sends_only_one_info_card(self):
        ctx = make_context()
        for i in range(3):
            m = make_message(
                message_id=10 + i,
                photo=[SimpleNamespace(file_id=f"p{i}")],
                media_group_id="album-1",
            )
            await bot.forward_to_admin(SimpleNamespace(message=m), ctx)
        self.assertEqual(ctx.bot.send_message.await_count, 1, "相册只发一条发送者信息")
        self.assertEqual(len(bot.album_info), 1)

    async def test_bot_command_is_not_forwarded(self):
        m, ctx = make_message(text="/start"), make_context()
        await bot.forward_to_admin(SimpleNamespace(message=m), ctx)
        m.forward.assert_not_awaited()

    async def test_unknown_command_is_forwarded(self):
        m, ctx = make_message(text="/whatever"), make_context()
        await bot.forward_to_admin(SimpleNamespace(message=m), ctx)
        m.forward.assert_awaited_once()

    async def test_service_message_is_skipped(self):
        m, ctx = make_message(), make_context()  # 所有媒体字段为 None
        await bot.forward_to_admin(SimpleNamespace(message=m), ctx)
        m.forward.assert_not_awaited()

    # ---------------------------------------------------------- 管理员 -> 用户
    async def test_reply_to_user_success(self):
        bot.track(777, 111)
        m = make_message(
            text="收到",
            chat_id=ADMIN,
            reply_to_message=SimpleNamespace(message_id=777, forward_from=None),
        )
        ctx = make_context()
        await bot.reply_to_user(SimpleNamespace(message=m), ctx)
        m.copy.assert_awaited_once_with(chat_id=111)
        m.reply_text.assert_awaited_once()
        self.assertEqual(bot.lookup(3001), 111, "确认消息也应可继续追问")

    async def test_reply_unknown_target_gives_hint(self):
        m = make_message(reply_to_message=SimpleNamespace(message_id=888, forward_from=None))
        ctx = make_context()
        await bot.reply_to_user(SimpleNamespace(message=m), ctx)
        m.copy.assert_not_awaited()
        self.assertIn("找不到", m.reply_text.await_args.args[0])

    async def test_reply_via_forward_from_fallback(self):
        m = make_message(
            reply_to_message=SimpleNamespace(
                message_id=888, forward_from=SimpleNamespace(id=555)
            )
        )
        ctx = make_context()
        await bot.reply_to_user(SimpleNamespace(message=m), ctx)
        m.copy.assert_awaited_once_with(chat_id=555)

    async def test_reply_command_is_not_sent_to_user(self):
        m = make_message(
            text="/id",
            reply_to_message=SimpleNamespace(message_id=777, forward_from=None),
        )
        bot.track(777, 111)
        await bot.reply_to_user(SimpleNamespace(message=m), make_context())
        m.copy.assert_not_awaited()

    # ---------------------------------------------------------- 引用 / 旧索引兼容
    def test_legacy_index_compat(self):
        bot.STATE_FILE.write_text(
            json.dumps({"1001": 111, "1002": 222}), encoding="utf-8"
        )
        bot.forward_map.clear()
        bot.load_state()
        self.assertEqual(bot.lookup(1001), 111)
        self.assertIsNone(bot.lookup_origin(1001), "老索引没有原消息 ID，应优雅降级")

    def test_track_keeps_existing_origin(self):
        bot.track(1, 111, origin_id=42)
        bot.track(1, 111)  # 相册后续图片复用信息条，不能擦掉第一张的引用目标
        self.assertEqual(bot.lookup_origin(1), 42)

    async def test_reply_carries_quote(self):
        bot.track(777, 111, origin_id=42)
        m = make_message(
            text="收到",
            chat_id=ADMIN,
            reply_to_message=SimpleNamespace(message_id=777, forward_from=None),
        )
        ctx = make_context()
        await bot.reply_to_user(SimpleNamespace(message=m), ctx)
        rp = m.copy.await_args.kwargs["reply_parameters"]
        self.assertEqual(rp.message_id, 42)
        self.assertEqual(bot.lookup_origin(3001), 42, "确认消息也应保留引用目标")

    async def test_reply_quote_disabled_by_config(self):
        bot.REPLY_QUOTE = False
        bot.track(777, 111, origin_id=42)
        m = make_message(
            text="收到",
            reply_to_message=SimpleNamespace(message_id=777, forward_from=None),
        )
        await bot.reply_to_user(SimpleNamespace(message=m), make_context())
        self.assertNotIn("reply_parameters", m.copy.await_args.kwargs)

    async def test_quote_failure_falls_back_without_quote(self):
        bot.track(777, 111, origin_id=42)
        m = make_message(
            text="收到",
            reply_to_message=SimpleNamespace(message_id=777, forward_from=None),
        )
        fake_clone = AsyncMock(side_effect=[Exception("引用目标不存在"), 2001])
        with patch.object(bot, "clone_message", new=fake_clone):
            await bot.reply_to_user(SimpleNamespace(message=m), make_context())
        self.assertEqual(fake_clone.await_count, 2)
        self.assertEqual(fake_clone.await_args_list[0].kwargs["reply_to_id"], 42)
        self.assertIsNone(fake_clone.await_args_list[1].kwargs.get("reply_to_id"))
        m.reply_text.assert_awaited_once()  # 对方仍然收到回复

    # ---------------------------------------------------------- handler 注册矩阵
    def test_handler_groups_matrix(self):
        app = bot.build_application()
        reply_msg = Message(
            message_id=2, date=datetime.now(),
            chat=Chat(id=ADMIN, type="private"),
            from_user=User(id=ADMIN, first_name="A", is_bot=False), text="原消息",
        )

        def hit(group, update):
            return any(bool(h.check_update(update)) for h in app.handlers[group])

        # 真实 Telegram 的命令消息会带 BOT_COMMAND 实体，CommandHandler 依赖它识别；
        # 校验 /start@botname 形式时还需要 Message 绑定 Bot 实例（线上由 PTB 自动注入）
        start_cmd = real_update(
            111, text="/start",
            entities=[MessageEntity(type=MessageEntityType.BOT_COMMAND, offset=0, length=6)],
        )
        start_cmd.message.set_bot(SimpleNamespace(username="testbot"))
        cases = [
            ("用户普通消息", real_update(111), (False, False, True)),
            ("用户 /start", start_cmd, (True, False, True)),
            ("管理员普通消息", real_update(ADMIN), (False, True, False)),
            ("管理员回复消息", real_update(ADMIN, text="回", reply=reply_msg),
             (False, True, False)),
        ]
        for name, upd, expected in cases:
            with self.subTest(case=name):
                got = (hit(0, upd), hit(1, upd), hit(2, upd))
                self.assertEqual(got, expected, name)
        # 核心回归：管理员侧消息绝不能被用户转发 handler 匹配（原项目最大 bug）
        for upd in (real_update(ADMIN), real_update(ADMIN, text="回", reply=reply_msg)):
            self.assertFalse(hit(2, upd))
        self.assertIn(1, app.handlers)
        self.assertIn(2, app.handlers)
        self.assertNotEqual(app.handlers[1][0].callback, app.handlers[2][0].callback)

    def test_private_only_filter(self):
        bot.PRIVATE_ONLY = True
        app = bot.build_application()

        def group2_hit(update):
            return any(bool(h.check_update(update)) for h in app.handlers[2])

        self.assertTrue(group2_hit(real_update(111, chat_type="private")))
        self.assertFalse(group2_hit(real_update(-100123, chat_type="supergroup")))

    def test_setup_mode_without_admin_id(self):
        bot.ADMIN_CHAT_ID = None
        app = bot.build_application()
        self.assertTrue(
            any(bool(h.check_update(real_update(111))) for h in app.handlers[2])
        )
        self.assertNotIn(1, app.handlers)

    def test_post_init_failure_does_not_crash(self):
        async def scenario():
            fake_app = SimpleNamespace(bot=SimpleNamespace(
                set_my_commands=AsyncMock(side_effect=Exception("网络抖动")),
                get_me=AsyncMock(),
            ))
            await bot.post_init(fake_app)  # 不应抛异常
        import asyncio
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
