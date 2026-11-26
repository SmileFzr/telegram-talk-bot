#!/data/data/com.termux/files/usr/bin/bash
# Telegram 传话筒 · Termux 一键部署脚本
# 用法：bash setup.sh   （或粘贴一行命令，见仓库 README）
set -u

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }
fail() { echo -e "${RED}❌ $1${NC}"; exit 1; }

REPO="https://github.com/SmileFzr/telegram-talk-bot.git"
DIR="$HOME/telegram-talk-bot"

echo ""
echo "============================================"
echo "  Telegram 传话筒 · Termux 一键部署"
echo "============================================"
echo ""

# ---------------------------------------------------------------- 环境检查
command -v pkg >/dev/null 2>&1 || fail "当前不是 Termux 环境。请安装 F-Droid 版 Termux（Play 商店版已停更，无法安装依赖）。"

echo ">>> [1/7] 更新软件包（第一次会稍慢）..."
pkg update -y >/dev/null 2>&1 && pkg upgrade -y >/dev/null 2>&1
ok "软件包已更新"

echo ">>> [2/7] 安装 Python / Git / curl..."
pkg install -y python git curl termux-api >/dev/null 2>&1
ok "基础工具已安装"

# ---------------------------------------------------------------- 网络检测
echo ">>> [3/7] 检测 Telegram 连通性..."
CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 12 https://api.telegram.org 2>/dev/null || echo "000")
if [ "$CODE" = "000" ]; then
  warn "连不上 Telegram。请确保手机代理软件已开启【全局 VPN / 全局代理】模式。"
  warn "如果是分应用代理，请把 Termux 也加入代理列表，否则机器人收不到消息。"
else
  ok "Telegram 可访问（HTTP $CODE）"
fi

# ---------------------------------------------------------------- 拉代码
echo ">>> [4/7] 获取机器人代码..."
if [ -d "$DIR/.git" ]; then
  (cd "$DIR" && git pull -q) && ok "代码已更新" || warn "更新失败，继续使用本地版本"
else
  git clone -q "$REPO" "$DIR" || fail "克隆失败。请确认手机能访问 github.com（同样需要全局代理）。"
  ok "代码已下载"
fi
cd "$DIR" || fail "无法进入 $DIR"

# ---------------------------------------------------------------- Python 依赖
echo ">>> [5/7] 安装 Python 依赖..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt || fail "依赖安装失败，请检查网络后重试 bash setup.sh"
ok "Python 依赖已就绪"

# ---------------------------------------------------------------- 配置
echo ""
echo ">>> [6/7] 配置机器人"
echo "（提示：TOKEN 从 @BotFather 获取；输入内容只会写进本机 .env，不会上传）"
echo ""

if [ -f "$DIR/.env" ]; then
  read -r -p "检测到已有配置，是否保留？(Y/n，回车=保留): " KEEP
  case "$KEEP" in
    [Nn]*)
      read -r -p "请输入 BOT TOKEN: " TOKEN
      [ -z "$TOKEN" ] && fail "TOKEN 不能为空"
      read -r -p "管理员 Chat ID（直接回车 = 8128875103）: " ADMIN
      ADMIN=${ADMIN:-8128875103}
      read -r -p "代理地址（全局 VPN 请直接回车留空）: " PROXY
      cat > "$DIR/.env" <<EOF
TOKEN=$TOKEN
ADMIN_CHAT_ID=$ADMIN
PROXY_URL=$PROXY
STATE_FILE=$DIR/forward_map.json
SEND_ACK=false
PRIVATE_ONLY=false
ADMIN_HINT=true
REPLY_QUOTE=true
EOF
      ok "配置已更新" ;;
    *)
      ok "保留已有配置" ;;
  esac
else
  read -r -p "请输入 BOT TOKEN: " TOKEN
  [ -z "$TOKEN" ] && fail "TOKEN 不能为空"
  read -r -p "管理员 Chat ID（直接回车 = 8128875103）: " ADMIN
  ADMIN=${ADMIN:-8128875103}
  read -r -p "代理地址（全局 VPN 请直接回车留空；分应用代理填 http://IP:端口）: " PROXY
  cat > "$DIR/.env" <<EOF
TOKEN=$TOKEN
ADMIN_CHAT_ID=$ADMIN
PROXY_URL=$PROXY
STATE_FILE=$DIR/forward_map.json
SEND_ACK=false
PRIVATE_ONLY=false
ADMIN_HINT=true
REPLY_QUOTE=true
EOF
  ok "配置已写入 .env"
fi

# ---------------------------------------------------------------- 启动脚本
echo ">>> [7/7] 创建守护脚本与开机自启..."
cat > "$DIR/run.sh" <<'RUNEOF'
#!/data/data/com.termux/files/usr/bin/bash
# 传话筒守护脚本：防休眠 + 崩溃自动重启
cd "$HOME/telegram-talk-bot" || exit 1
termux-wake-lock 2>/dev/null   # 防止系统休眠杀掉进程
while true; do
  echo "[$(date '+%F %T')] 启动机器人..."
  python bot.py
  echo "[$(date '+%F %T')] 进程退出，5 秒后自动重启..."
  sleep 5
done
RUNEOF
chmod +x "$DIR/run.sh"

mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/start-relay-bot.sh" <<'BOOTEOF'
#!/data/data/com.termux/files/usr/bin/sh
# 开机自启（需安装 Termux:Boot 插件）
termux-wake-lock 2>/dev/null
cd "$HOME/telegram-talk-bot" || exit 1
nohup bash run.sh >> bot.log 2>&1 &
BOOTEOF
chmod +x "$HOME/.termux/boot/start-relay-bot.sh"
ok "守护脚本 + 开机自启已配置"

# ---------------------------------------------------------------- 启动测试
echo ""
echo ">>> 试启动 8 秒，检查是否能连上 Telegram..."
timeout 8 python bot.py 2>&1 | head -12 || true

# ---------------------------------------------------------------- 完成提示
echo ""
echo "============================================"
ok "安装完成！"
echo ""
echo "【还差 3 步手机设置，缺一可能掉线】"
echo "1. 系统设置 → 应用 → Termux → 电池 → 选「不优化 / 允许后台运行」"
echo "2. 多任务界面把 Termux 卡片【锁定】，别被一键清理杀掉"
echo "3. 安装 Termux:Boot（F-Droid 免费插件）→ 开机自动拉起机器人"
echo ""
echo "【常用命令】"
echo "  启动机器人：  cd ~/telegram-talk-bot && bash run.sh"
echo "  查看日志：    tail -f ~/telegram-talk-bot/bot.log"
echo "  更新代码：    cd ~/telegram-talk-bot && bash setup.sh"
echo ""
echo "提示：手机长时间插电运行，机器人才能 24 小时在线。"
echo "============================================"
echo ""
