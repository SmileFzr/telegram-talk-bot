import logging
import os
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# 从 Railway 环境变量读取（必须这样设置！）
TOKEN = os.getenv("TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))

if not TOKEN or not ADMIN_CHAT_ID:
    raise ValueError("请在 Railway 设置 TOKEN 和 ADMIN_CHAT_ID 环境变量！")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

application = Application.builder().token(TOKEN).build()

async def set_commands(app):
    commands = [BotCommand("start", "开始使用传话筒")]
    await app.bot.set_my_commands(commands)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ 你好！我是你的传话筒机器人。\n\n"
        "直接发任何消息（文字、图片、语音、视频、文件等）给我，\n"
        "我会立刻转发给管理员。\n"
        "管理员回复你时，你会直接收到。"
    )

# 用户 → 管理员
async def forward_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = message.from_user
    try:
        await message.forward(chat_id=ADMIN_CHAT_ID)
        info = f"📨 来自用户：{user.full_name} (@{user.username or '无用户名'}) ID: {user.id}"
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=info)
    except Exception as e:
        logging.error(f"转发失败: {e}")

# 管理员回复 → 用户
async def reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.chat_id != ADMIN_CHAT_ID:
        return
    if message.reply_to_message and message.reply_to_message.forward_from:
        target_user_id = message.reply_to_message.forward_from.id
        try:
            await message.copy(chat_id=target_user_id)
            await message.reply_text("✅ 已回复给用户")
        except Exception as e:
            await message.reply_text(f"回复失败: {e}")
    else:
        await message.reply_text("⚠️ 请回复一条从用户转发的消息，才能正确回传。")

application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, forward_to_admin))
application.add_handler(MessageHandler(filters.Chat(ADMIN_CHAT_ID) & filters.REPLY, reply_to_user))

application.job_queue.run_once(lambda _: set_commands(application), 1)

if __name__ == "__main__":
    print("🤖 机器人启动中...")
    application.run_polling()
