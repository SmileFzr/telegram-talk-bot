# 用安卓手机（Termux）24 小时跑传话筒机器人

零成本、免绑信用卡。把闲置安卓手机变成常驻服务器。

---

## 一、准备（一次性）

1. **安装 Termux**（重要：不要从 Google Play 装，那个版本已停更、装不了依赖）
   - 去 F-Droid 下载：https://f-droid.org/packages/com.termux/
   - 或官网 https://termux.dev/cn/ 按其推荐渠道安装
2. **安装 Termux:Boot**（开机自启插件，同样从 F-Droid 装，免费）
3. 手机开启**全局代理/VPN**（否则连不上 Telegram 和 GitHub）

## 二、一键部署

打开 Termux，粘贴下面这一整行，回车：

```
pkg install -y curl && curl -fsSL https://raw.githubusercontent.com/SmileFzr/telegram-talk-bot/main/termux/setup.sh | bash
```

脚本会依次：装 Python/Git → 下载代码 → 装依赖 → **问你 TOKEN 和 Chat ID** → 生成守护脚本和开机自启 → 试启动。

> TOKEN 从 @BotFather 拿；管理员 Chat ID 你之前已经查到是 `8128875103`。
> 输入的内容只写在手机本地 `.env`，不会上传。

## 三、手机端必做的 3 个设置（不然会掉线）

| 设置 | 位置 | 目的 |
|---|---|---|
| 关闭电池优化 | 系统设置 → 应用 → Termux → 电池 → 不优化 | 防止系统冻结后台 |
| 锁定后台 | 多任务界面下拉 Termux 卡片 → 锁 | 防止一键清理杀进程 |
| 开机自启 | 装好 Termux:Boot 后自动生效 | 重启后自动拉起机器人 |

另外：**插着电**、**连着 WiFi**、**别关代理软件**。

## 四、日常使用

```bash
# 手动启动（前台看日志）
cd ~/telegram-talk-bot && bash run.sh

# 后台启动（关掉 Termux 窗口也继续跑）
cd ~/telegram-talk-bot && nohup bash run.sh > bot.log 2>&1 &

# 查看日志
tail -f ~/telegram-talk-bot/bot.log

# 停止机器人
pkill -f "python bot.py"

# 更新到最新版本（会问是否保留已有配置，一般直接回车）
cd ~/telegram-talk-bot && bash setup.sh
```

## 五、常见问题

**Q：脚本卡在 "检测 Telegram 连通性" 或报超时？**
A：代理没开全局模式。确认手机能通过浏览器打开 https://api.telegram.org ，且 Termux 被代理覆盖。

**Q：机器人跑一会儿就断？**
A：九成是后台被系统杀了。回第三步把电池优化和后台锁定都设好；不同品牌手机路径不同（小米叫"自启动"，华为叫"启动管理"）。

**Q：图片/视频收不到？**
A：手机网络波动时可能失败，守护脚本会自动重启。看 `tail -f bot.log` 里的报错。

**Q：手机没电/关机了怎么办？**
A：机器人停了，开机后 Termux:Boot 会自动恢复。这期间的消息 Telegram 会缓存，重启后 `drop_pending_updates=False` 会补收。

---

## ⚠️ 关于数据持久化（重要）

转发索引 `forward_map.json` 存在手机本地，**卸载 Termux 或清理数据会丢失**，届时旧消息无法回复（重新发新消息即可恢复）。
建议重要时期定期备份：

```bash
cp ~/telegram-talk-bot/forward_map.json ~/forward_map.backup.json
```
