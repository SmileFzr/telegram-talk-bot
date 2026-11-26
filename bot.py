# -*- coding: utf-8 -*-
"""
Telegram 传话筒机器人（Relay Bot）
=====================================================================
别人发给机器人的任何消息（文字 / 图片 / 视频 / 文件 / 语音 / 贴纸 /
相册 / 位置 / 联系人 / 投票 ...）都会转交给「管理员」；
管理员只要【回复】任意一条收到的消息，回复内容就会原样送回给对方。

运行方式：
  本地：填好同目录的 .env，然后 python bot.py
  云端：在 Railway 等平台配置环境变量，Procfile 已写好 worker: python bot.py

配置（环境变量，或 .env 文件）：
  TOKEN          必填，@BotFather 创建机器人时给的 token
  ADMIN_CHAT_ID  必填，你自己的 Chat ID（对机器人发 /id 即可查到）
  STATE_FILE     可选，转发索引文件，默认 forward_map.json
  SEND_ACK       可选，是否给发消息的人回一条「已收到」，默认 false
  MAX_TRACKED    可选，索引保留条数，默认 5000
  REPLY_QUOTE    可选，管理员回复时对方是否看到引用，默认 true
=====================================================================
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import sys
import time
import warnings
from collections import OrderedDict
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, ReplyParameters, Update
from telegram.constants import ParseMode
from telegram.error import NetworkError, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# ---------------------------------------------------------------- 配置
BASE_DIR = Path(__file__).resolve().parent
# 本地开发读 .env；部署在 Railway 时没有这个文件，直接用平台环境变量
load_dotenv(BASE_DIR / ".env")

TOKEN = (os.getenv("TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
_raw_admin = (os.getenv("ADMIN_CHAT_ID") or "").strip()
try:
    # 私聊是正整数，群聊是负数（例如 -1001234567890）
    ADMIN_CHAT_ID: int | None = int(_raw_admin) if _raw_admin else None
except ValueError:
    sys.exit("❌ ADMIN_CHAT_ID 必须是纯数字（群聊 ID 是负数，如 -1001234567890）")

if not TOKEN:
    sys.exit(
        "❌ 缺少 TOKEN。\n"
        "   1. 在 Telegram 里找 @BotFather，发 /newbot，按提示创建机器人并复制 token\n"
        "   2. 把 token 填进 .env 的 TOKEN=...（或在 Railway 配置 TOKEN 环境变量）\n"
        "   3. 重新运行 python bot.py"
    )

STATE_FILE = Path(os.getenv("STATE_FILE") or (BASE_DIR / "forward_map.json"))
MAX_TRACKED = int(os.getenv("MAX_TRACKED") or "5000")

# 国内访问 api.telegram.org 必须走代理。系统里可能残留着已经失效的代理环境变量，
# 这里用 .env 里显式配置的 PROXY_URL 覆盖它，避免 httpx 悄悄走错通道。
PROXY_URL = (os.getenv("PROXY_URL") or "").strip()
if PROXY_URL:
    for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[_key] = PROXY_URL


def _flag(name: str, default: str = "false") -> bool:
    return (os.getenv(name) or default).strip().lower() in ("1", "true", "yes", "on")


SEND_ACK = _flag("SEND_ACK")
# 只接受私聊消息（把机器人拉进群时，群消息一概不理，避免刷屏与身份暴露）
PRIVATE_ONLY = _flag("PRIVATE_ONLY")
# 管理员发了「不是回复」的消息时，给一次友好提示（60 秒内最多一次）
ADMIN_HINT = _flag("ADMIN_HINT", "true")
# 回传时让对方看到「引用自己原消息」的引用条（Telegram Bot API 支持，默认开启）
REPLY_QUOTE = _flag("REPLY_QUOTE", "true")

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=logging.INFO,
)
# 本机器人不需要定时任务，去掉 job-queue 依赖后 PTB 会提示一条无意义警告
warnings.filterwarnings("ignore", message="No `JobQueue` set up.*")
logging.getLogger("httpx").setLevel(logging.WARNING)  # 少打印一堆 HTTP 日志
log = logging.getLogger("relay")

# ---------------------------------------------------------------- 转发索引
# 关键设计：不依赖 forward_from（用户关掉「转发消息链接」隐私开关后它会是 None）。
# 自己记录「管理员收到的那条消息 id -> (对方 chat id, 对方原消息 id)」，
# 100% 可靠，重启后仍可回复，且能带引用条回到对方那边。
# 结构：OrderedDict[key=str(message_id), value=dict(chat_id=int, origin_id=int|None)]
# 旧版本落盘的是纯 int，load_state 会自动升级，兼容历史索引文件。
forward_map: OrderedDict[str, dict] = OrderedDict()
# 相册去重：media_group_id -> 已发出的那条「发送者信息」的 message_id
album_info: dict[str, int] = {}

# 机器人自己的命令。这些不转发给管理员，其余（包括未知命令）一律照转，避免丢消息。
BOT_CMD_RE = re.compile(r"^/(?:start|help|id|send)(?:@[\w_]+)?(?:\s|$)", re.IGNORECASE)


def _normalize_entry(value) -> dict:
    """兼容老索引：旧值是纯 int（chat_id），新值是 dict。"""
    if isinstance(value, dict):
        try:
            origin = value.get("origin_id")
            return {
                "chat_id": int(value["chat_id"]),
                "origin_id": int(origin) if origin is not None else None,
            }
        except (KeyError, TypeError, ValueError):
            return {"chat_id": 0, "origin_id": None}
    try:
        return {"chat_id": int(value), "origin_id": None}
    except (TypeError, ValueError):
        return {"chat_id": 0, "origin_id": None}


def load_state() -> None:
    global forward_map
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        forward_map = OrderedDict(
            (str(k), _normalize_entry(v)) for k, v in data.items()
        )
        log.info("已载入 %d 条转发索引（%s）", len(forward_map), STATE_FILE.name)
    except Exception as exc:
        log.warning("读取索引失败，将重建：%s", exc)


def save_state() -> None:
    """原子写入：先写临时文件再替换，避免进程中断留下半截损坏的索引。"""
    try:
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        tmp.write_text(
            json.dumps(dict(forward_map), ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        log.warning("保存索引失败：%s", exc)


def track(message_id: int, user_chat_id: int, origin_id: int | None = None) -> None:
    """登记一条管理员侧消息对应的原发送者，以及对方那条原消息的 ID（用于引用）。"""
    key = str(message_id)
    entry = forward_map.get(key)
    # 已存在但这次没带 origin 时，保留旧值，避免把引用信息擦掉
    if entry and origin_id is None:
        origin_id = entry.get("origin_id")
    forward_map[key] = {"chat_id": int(user_chat_id), "origin_id": origin_id}
    forward_map.move_to_end(key)
    while len(forward_map) > MAX_TRACKED:
        forward_map.popitem(last=False)  # 淘汰最旧的
    save_state()


def lookup(message_id: int) -> int | None:
    entry = forward_map.get(str(message_id))
    return entry.get("chat_id") if entry else None


def lookup_origin(message_id: int) -> int | None:
    """取对方那条原消息的 ID，用于回传时带引用条。"""
    entry = forward_map.get(str(message_id))
    return entry.get("origin_id") if entry else None


# ---------------------------------------------------------------- 工具函数
def describe(message) -> str:
    """给管理员看的中文消息类型标签。"""
    if message.text:
        return "📝 文字"
    if message.photo:
        return "🖼️ 图片"
    if message.video:
        return "🎬 视频"
    if message.animation:
        return "🎞️ 动图 / GIF"
    if message.document:
        return "📎 文件"
    if message.voice:
        return "🎙️ 语音"
    if message.audio:
        return "🎵 音频"
    if message.video_note:
        return "📹 圆形视频"
    if message.sticker:
        return "😀 贴纸"
    if message.location:
        return "📍 位置"
    if message.contact:
        return "👤 联系人"
    if message.poll:
        return "📊 投票"
    if message.dice:
        return "🎲 骰子"
    if message.venue:
        return "🏠 地点"
    if message.game:
        return "🎮 游戏"
    return "📦 其他消息"


async def clone_message(
    bot, message, chat_id: int, reply_to_id: int | None = None
) -> int:
    """
    把 message 复制到 chat_id，返回新消息的 message_id。
    三级兜底：
      1) copyMessage（服务端复制，支持几乎所有类型，且不受 20MB 下载限制）
      2) 按类型用 file_id 重发（同样不需要下载文件，可绕过「禁止转发」限制）
    reply_to_id: 目标聊天里要引用的消息 ID；None 表示不带引用。
    """
    reply = (
        ReplyParameters(message_id=reply_to_id, allow_sending_without_reply=True)
        if reply_to_id
        else None
    )
    rp_kw = {"reply_parameters": reply} if reply is not None else {}

    try:
        copied = await message.copy(chat_id=chat_id, **rp_kw)
        return copied.message_id
    except Exception as exc:
        # copyMessage 对部分类型（投票、受保护内容…）会失败，继续走重发
        log.info("copyMessage 不可用，改用 file_id 重发：%s", exc)

    # 只有支持 caption 的发送方法才能接收这两个参数
    cap: dict = {}
    if message.caption:
        cap["caption"] = message.caption
    if message.caption_entities:
        cap["caption_entities"] = message.caption_entities

    if message.text is not None:
        sent = await bot.send_message(
            chat_id=chat_id, text=message.text, entities=message.entities, **rp_kw
        )
    elif message.photo:
        sent = await bot.send_photo(
            chat_id=chat_id, photo=message.photo[-1].file_id, **cap, **rp_kw
        )
    elif message.video:
        sent = await bot.send_video(
            chat_id=chat_id, video=message.video.file_id, **cap, **rp_kw
        )
    elif message.animation:
        sent = await bot.send_animation(
            chat_id=chat_id, animation=message.animation.file_id, **cap, **rp_kw
        )
    elif message.document:
        sent = await bot.send_document(
            chat_id=chat_id, document=message.document.file_id, **cap, **rp_kw
        )
    elif message.voice:
        sent = await bot.send_voice(
            chat_id=chat_id, voice=message.voice.file_id, **cap, **rp_kw
        )
    elif message.audio:
        sent = await bot.send_audio(
            chat_id=chat_id, audio=message.audio.file_id, **cap, **rp_kw
        )
    elif message.video_note:
        sent = await bot.send_video_note(
            chat_id=chat_id, video_note=message.video_note.file_id, **rp_kw
        )
    elif message.sticker:
        sent = await bot.send_sticker(
            chat_id=chat_id, sticker=message.sticker.file_id, **rp_kw
        )
    elif message.location:
        sent = await bot.send_location(
            chat_id=chat_id,
            latitude=message.location.latitude,
            longitude=message.location.longitude,
            **rp_kw,
        )
    elif message.contact:
        sent = await bot.send_contact(
            chat_id=chat_id,
            phone_number=message.contact.phone_number,
            first_name=message.contact.first_name,
            last_name=message.contact.last_name,
            **rp_kw,
        )
    elif message.dice:
        sent = await bot.send_dice(chat_id=chat_id, emoji=message.dice.emoji, **rp_kw)
    elif message.poll:
        lines = "\n".join(f"• {opt.text}" for opt in message.poll.options)
        sent = await bot.send_message(
            chat_id=chat_id,
            text=f"📊 投票：{message.poll.question}\n{lines}",
            **rp_kw,
        )
    else:
        raise TelegramError("无法识别的消息类型，既不能复制也无法重发")
    return sent.message_id


def sender_card(message) -> str:
    """生成给管理员看的发送者信息（HTML 格式，做了转义防注入）。"""
    user = message.from_user
    name = html.escape(user.full_name or "未知用户") if user else "未知发送者"
    username = html.escape(user.username) if user and user.username else "无用户名"
    uid = user.id if user else "?"
    chat_id = message.chat_id
    kind = describe(message)
    extra = "\n🖼️ 相册消息（多条已合并显示）" if message.media_group_id else ""
    return (
        f"📨 <b>新消息 · {kind}</b>{extra}\n"
        f"👤 {name}  @{username}\n"
        f"🆔 用户 <code>{uid}</code>   💬 会话 <code>{chat_id}</code>\n"
        f"↩️ 回复本条或上方那条消息，即可把内容传回给对方"
    )


# ---------------------------------------------------------------- 命令
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 你好，我是传话筒机器人。\n\n"
        "直接把想说的话发给我就行：文字、图片、视频、文件、语音、贴纸……都可以，\n"
        "我会原样转交给管理员，对方的回复也会送到你这里。"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_admin = ADMIN_CHAT_ID is not None and update.effective_chat.id == ADMIN_CHAT_ID
    text = (
        "📖 使用说明\n\n"
        "• 直接发消息 → 自动转交管理员\n"
        "• /id 查看自己的 Chat ID\n"
    )
    if is_admin:
        text += (
            "\n👑 管理员用法\n"
            "• 回复任意一条收到的消息（含「发送者信息」那条）→ 回复内容送回对方\n"
            "• /send <用户ID> <文字> → 主动给某人发一条文字\n"
            "• 文字/图片/视频/文件/语音/贴纸等都可以作为回复发出\n"
        )
    await update.message.reply_text(text)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.message.reply_text(
        f"🆔 你的 Chat ID：<code>{chat.id}</code>\n"
        f"👤 用户 ID：<code>{update.effective_user.id}</code>",
        parse_mode=ParseMode.HTML,
    )
    if ADMIN_CHAT_ID is None:
        await update.message.reply_text(
            "⚙️ 机器人还没配置管理员。请把上面的 Chat ID 填进 .env 的 "
            "ADMIN_CHAT_ID=... 然后重启机器人。"
        )


async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """管理员主动发消息：/send 123456789 你好"""
    if ADMIN_CHAT_ID is None or update.effective_chat.id != ADMIN_CHAT_ID:
        return
    args = context.args or []
    if len(args) < 2 or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("用法：/send <用户ChatID> <要发送的文字>")
        return
    target, text = int(args[0]), " ".join(args[1:])
    try:
        await context.bot.send_message(chat_id=target, text=text)
        await update.message.reply_text(f"✅ 已发送给 {target}")
    except TelegramError as exc:
        await update.message.reply_text(f"❌ 发送失败：{exc}")


# ---------------------------------------------------------------- 用户 -> 管理员
async def forward_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return
    # 群内的服务通知（进群、改标题、置顶…）无法转发，直接跳过
    if not (message.text or message.photo or message.video or message.animation
            or message.document or message.voice or message.audio
            or message.video_note or message.sticker or message.location
            or message.contact or message.poll or message.dice
            or message.venue or message.game):
        return
    # 机器人自己的命令由 group 0 处理，这里跳过；未知命令仍照常转发
    if message.text and BOT_CMD_RE.match(message.text):
        return

    user_chat_id = message.chat_id
    # 对方那条原消息的 ID：回传时要拿它当引用目标，这样对方能看到自己在引用哪条
    origin_id = message.message_id
    delivered_ids: list[int] = []

    # 1) 优先真·转发（保留原发送者头像与链接，最直观）
    try:
        fwd = await message.forward(chat_id=ADMIN_CHAT_ID)
        delivered_ids.append(fwd.message_id)
    except Exception as exc:
        # 2) 对方开启了「禁止转发」等情况：改用复制，消息绝不丢
        log.info("forward 失败（%s），改用复制方式", exc)
        try:
            delivered_ids.append(
                await clone_message(context.bot, message, ADMIN_CHAT_ID)
            )
        except Exception as inner:
            log.error("消息复制也失败：%s", inner)
            # 3) 最后兜底：至少把文字内容和管理员提示送过去
            try:
                snippet = (message.text or message.caption or "")[:800]
                who = html.escape(message.from_user.full_name) if message.from_user else "未知发送者"
                failed = await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=f"⚠️ 有一条{describe(message)}无法转发（对方可能开启了禁止转发）\n"
                         f"来自 {who} · <code>{user_chat_id}</code>\n"
                         f"内容：{html.escape(snippet) or '（无文字内容）'}",
                    parse_mode=ParseMode.HTML,
                )
                track(failed.message_id, user_chat_id, origin_id)
            except Exception as fatal:
                log.error("连告警消息都发送失败：%s", fatal)
            return

    for mid in delivered_ids:
        track(mid, user_chat_id, origin_id)

    # 4) 附一条发送者信息；同一个相册只发一次，避免刷屏
    group_id = message.media_group_id
    if group_id and group_id in album_info:
        # 相册后续图片不再传 origin，track 会保留第一张的引用目标
        track(album_info[group_id], user_chat_id)
    else:
        try:
            info = await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=sender_card(message),
                parse_mode=ParseMode.HTML,
            )
            track(info.message_id, user_chat_id, origin_id)
            if group_id:
                if len(album_info) > 300:
                    album_info.clear()
                album_info[group_id] = info.message_id
        except Exception as exc:
            log.error("发送「发送者信息」失败：%s", exc)

    if SEND_ACK:
        acked = context.bot_data.setdefault("acked_albums", set())
        if len(acked) > 500:
            acked.clear()
        if not group_id or group_id not in acked:
            if group_id:
                acked.add(group_id)
            try:
                await message.reply_text("✅ 已收到，已转交给管理员。")
            except Exception:
                pass


# ---------------------------------------------------------------- 管理员 -> 用户
async def reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.reply_to_message:
        return
    if message.text and BOT_CMD_RE.match(message.text):
        return  # /id、/send 等命令不算回复内容

    replied_id = message.reply_to_message.message_id
    target = lookup(replied_id)
    origin_id = lookup_origin(replied_id) if REPLY_QUOTE else None
    if target is None:
        # 兼容旧机制：万一索引丢失，但被回复的消息带 forward_from
        f = getattr(message.reply_to_message, "forward_from", None)
        target = f.id if f else None
        origin_id = None  # 这条路径拿不到原消息 ID，只能不引用
    if target is None:
        await message.reply_text(
            "⚠️ 找不到这条消息对应的用户。\n"
            "请回复一条由机器人转交过来的消息（或它下面的发送者信息）。"
        )
        return

    try:
        try:
            await clone_message(context.bot, message, target, reply_to_id=origin_id)
        except Exception as exc:
            # 引用目标失效（比如对方把原消息删了）不该导致回复丢失，去掉引用重发
            if not origin_id:
                raise
            log.info("带引用回传失败，改为不带引用重发：%s", exc)
            await clone_message(context.bot, message, target)
        ack = await message.reply_text("✅ 已送达对方")
        # 连「已送达」这条也登记，方便管理员顺着确认消息继续追问
        track(ack.message_id, target, origin_id)
    except Exception as exc:
        log.error("回传时出错：%s", exc)
        try:
            await message.reply_text(
                f"❌ 回传失败：{exc}\n"
                "常见原因：对方把机器人拉黑了，或从未点过 /start 与机器人建立会话。"
            )
        except Exception:
            pass


async def admin_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """管理员发了一条「不是回复」的消息时，提示正确用法（60 秒节流，避免刷屏）。"""
    message = update.message
    if message is None:
        return
    if message.text and BOT_CMD_RE.match(message.text):
        return
    now = time.monotonic()
    if now - context.bot_data.get("last_hint_at", 0.0) < 60:
        return
    context.bot_data["last_hint_at"] = now
    await message.reply_text(
        "💡 想回话给对方，请【回复】一条由机器人转交过来的消息；\n"
        "想主动发文字，用 /send <用户ID> <文字>。"
    )


# ---------------------------------------------------------------- 未配置管理员时
async def setup_notice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text and BOT_CMD_RE.match(update.message.text):
        return
    await update.message.reply_text(
        "⚙️ 机器人尚未配置管理员，暂时无法转交消息。\n"
        "请对机器人发送 /id 拿到 Chat ID，填进 .env 的 ADMIN_CHAT_ID 后重启。"
    )


# ---------------------------------------------------------------- 启动
async def post_init(app: Application) -> None:
    # 用官方 post_init 钩子设置命令菜单（原代码的 run_once + lambda 不会被执行）
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "开始使用传话筒"),
                BotCommand("help", "查看使用说明"),
                BotCommand("id", "查询自己的 Chat ID"),
                BotCommand("send", "（管理员）主动发消息"),
            ]
        )
        me = await app.bot.get_me()
        log.info("命令菜单已更新，机器人 @%s 已就绪", me.username)
    except Exception as exc:
        # 菜单只是锦上添花，网络抖动时不该拖垮收发消息的主流程
        log.warning("设置命令菜单失败（不影响收发消息）：%s", exc)


def build_application() -> Application:
    """只构造 Application 和注册 handler，不联网、不启动轮询（便于离线验收测试）。"""
    def make_request() -> HTTPXRequest:
        # 国内代理偶尔抖动，PTB 默认 5 秒连接超时太苛刻，放宽以减少假失败
        opts = dict(
            connection_pool_size=8,
            connect_timeout=20.0,
            read_timeout=25.0,
            media_write_timeout=40.0,
        )
        if PROXY_URL:
            opts["proxy"] = PROXY_URL
        return HTTPXRequest(**opts)

    # 两条通道都要吃进代理：request 管 getMe/sendMessage，
    # get_updates_request 管长轮询的 getUpdates；漏掉后者会「能发却收不到」。
    builder = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .request(make_request())
        .get_updates_request(make_request())
    )
    app = builder.build()

    # group 0：命令
    app.add_handler(CommandHandler("start", cmd_start), group=0)
    app.add_handler(CommandHandler("help", cmd_help), group=0)
    app.add_handler(CommandHandler("id", cmd_id), group=0)
    app.add_handler(CommandHandler("send", cmd_send), group=0)

    if ADMIN_CHAT_ID is None:
        app.add_handler(
            MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, setup_notice),
            group=2,
        )
    else:
        # group 1：管理员的回复（独立 group，且必须在普通消息之前，避免被抢先匹配）
        app.add_handler(
            MessageHandler(
                filters.Chat(chat_id=ADMIN_CHAT_ID) & filters.REPLY
                & ~filters.StatusUpdate.ALL,
                reply_to_user,
            ),
            group=1,
        )
        if ADMIN_HINT:
            app.add_handler(
                MessageHandler(
                    filters.Chat(chat_id=ADMIN_CHAT_ID) & ~filters.REPLY
                    & ~filters.StatusUpdate.ALL,
                    admin_hint,
                ),
                group=1,
            )

        # group 2：其他人发来的消息（明确排除管理员自己的聊天，杜绝自我转发）
        user_filter = ~filters.Chat(chat_id=ADMIN_CHAT_ID) & ~filters.StatusUpdate.ALL
        if PRIVATE_ONLY:
            user_filter = user_filter & filters.ChatType.PRIVATE
        app.add_handler(MessageHandler(user_filter, forward_to_admin), group=2)

    return app


def ensure_event_loop() -> None:
    """
    Python 3.12+ 起 asyncio.get_event_loop() 不再自动创建事件循环，
    3.14 直接抛 RuntimeError。PTB 21.x 的 run_polling 内部依赖旧行为，
    这里显式建一个并设为当前，避免 Termux 新版 Python 启动时崩溃。
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("event loop is closed")
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def main() -> None:
    load_state()
    log.info(
        "🤖 传话筒启动中… 管理员=%s | 仅私聊=%s", ADMIN_CHAT_ID or "未设置", PRIVATE_ONLY
    )
    # 传话筒不能丢消息：重启 / 停机期间积压的消息必须补收，所以 False
    for attempt in range(1, 4):
        ensure_event_loop()  # 兼容 Python 3.14（Termux 新版）
        app = build_application()
        try:
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
            return
        except NetworkError as exc:
            log.warning("连接 Telegram 失败（第 %d/3 次）：%s", attempt, exc)
            if attempt < 3:
                log.info("5 秒后自动重试…")
                time.sleep(5)
            else:
                sys.exit(
                    "\n❌ 连续 3 次连不上 Telegram。请按顺序检查：\n"
                    "   1. 代理软件是否在运行、端口是否变了\n"
                    "   2. .env 里 PROXY_URL 是否等于当前代理地址\n"
                    "   3. 浏览器能否打开 https://api.telegram.org\n"
                    "   4. 改好后重新双击 start.bat\n"
                )
        except TelegramError as exc:
            sys.exit(f"\n❌ Telegram 返回错误：{exc}\n   请检查 .env 里的 TOKEN 是否正确。")


if __name__ == "__main__":
    main()
