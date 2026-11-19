# 酒馆资源库与 Discord 入库机器人

把社区里看到的角色卡、世界书、预设、面具、文风、美化和生图转发到 Discord 机器人（或私聊它，或直接发到对应论坛频道），会登记到本机 `/opt/tavern-library/`，并在「酒馆资源库」分类下按类型开子区。网页 https://tavern.vickytavel.online/_lib/ 可以浏览、筛选、改元数据，并把带文件的资源一键复制进当前酒馆。

一条资源同时对应 Discord 子区和资源库页面，两边改完会同步。日常打标签以 Discord 里的点选卡片为主；资源库更适合以后搜索和偶尔整理。

控制页 https://tavern.vickytavel.online/_ctl/ 里也有「资源库」入口。两个页面都走 Cloudflare Access，外网打不开。

## 路径

| 用途 | 路径 |
|---|---|
| 程序与虚拟环境 | `/opt/tavern-bot/` |
| 配置（含 token） | `/opt/tavern-bot/.env`（权限 600，所有者 `tavernbot`） |
| 资源库文件与数据库 | `/opt/tavern-library/`（`library.db` + 按类型分子目录） |
| 导入助手（以 `tavern` 用户复制文件） | `/opt/tavern-bot/import_helper.py` |
| 网页服务 | `tavern-lib.service`，监听 `127.0.0.1:8003`，nginx `/_lib/` |
| Discord 机器人 | `tavern-bot.service`（未写入 token 前不要启动） |

## 安装 Discord token 并启动机器人

1. 把 token 写进 `/opt/tavern-bot/.env` 的 `DISCORD_TOKEN=`（不要把 token 发到聊天或记进仓库）。
2. 可选：`DISCORD_GUILD_ID=` 填服务器 ID。若机器人只在一个服务器里，可以留空，启动时会自动识别。
3. 可选：`ALLOWED_USER_IDS=` 填允许投递的用户 ID，逗号分隔。留空表示该服务器的成员都可以投递（包括这些人私聊机器人）。
4. 启动：

```bash
# 确认文件权限
chmod 600 /opt/tavern-bot/.env
chown tavernbot:tavernlib /opt/tavern-bot/.env

systemctl enable --now tavern-bot.service
systemctl status tavern-bot.service --no-pager
journalctl -u tavern-bot.service -e -n 80 --no-pager
```

以后改 token 或权限后：`systemctl restart tavern-bot.service`。

停止机器人：`systemctl disable --now tavern-bot.service`。

机器人访问 Discord 必须走本机常驻代理（单元里已配置 `HTTP(S)_PROXY`）。`bot.py` 创建 `discord.Client` 时会传入该代理。

## Discord 频道布局（启动时自动补齐）

机器人在目标服务器里会确保存在：

- 分类：`酒馆资源库`
- 论坛频道：`角色卡`、`世界书`、`预设`、`面具`、`文风`、`美化`、`生图`、`其他`

如果这些名字已经存在，会直接复用，不会重复创建。原来的「投递」频道会原样保留，继续当投递入口。需要「管理频道」权限才能建分类和论坛。

## 入库和整理流程

1. 按文件内容预填类型（只是预填，你的选择优先），存进 `/opt/tavern-library/`。同一文件（相同 sha256）再次投递会更新原记录，不会另存一份。
2. 若消息已经在某个论坛子区里，就用这个子区，不再新开一个。否则在对应论坛里开子区并尽量重新上传文件。
3. 在子区里发送一张点选卡片：类型、主题分类、标签、许可、推荐、基本信息、关联。用菜单和按钮，不必打字回复；也可以自定义标签或主题。同一张卡以后随时可以再点「整理标签」打开。
4. 改完会写入 `library.db`，并更新子区说明和论坛的「待确认」标签。改类型时，机器人会尽量把子区挪到对应频道（旧帖会留下跳转并归档）。
5. 一次转发多个文件会自动记成「成套包」。
6. 在原频道或私聊里回复「已入库」和子区跳转链接。

### 资源类型和存储

| 类型 | 论坛频道 | 资源库文件 | 一键导入酒馆 |
|---|---|---|---|
| 角色卡、世界书、预设、面具 | 对应频道 | 保存原文件 | 可以（面具只复制到 User Avatars） |
| 文风 | 文风 | 若有文件则保存；世界书/预设里的文风板块会另拆成可检索片段，原文件不动 | 否 |
| 美化（主题 / CSS / 背景 / 字体） | 美化 | 主题、CSS、背景等酒馆文件会保存 | 主题、CSS、背景可以；字体不行 |
| 生图 | 生图 | **不存原图**，只记条目、标签和 Discord 跳转（可用 Discord CDN 缩略图） | 否 |
| 其他 | 其他 | 保存原文件 | 否（聊天记录、正则不会写入酒馆设置） |

### 字段和标签

每条记录固定有：类型、标题、作者、来源链接、简介摘要、推荐预设 / 模型 / 画师串、二传二改 / 酒馆 / 小手机许可（及备注）、主题分类（可多条，路径如 `科幻/赛博朋克`）、分类标签、备注、基础信息（年龄、性别、称呼，用于角色卡和面具）、关联和版本、可检索片段。

标签必须落在固定维度里，避免混成一锅（词表来自 `酒馆标签词库-v2.2`，共 340 个受控标签）：

- 世界（时代 / 场景 / 类型 / 地域）
- 设定（机制 / 叙事）
- 玩法（模式）
- 角色（身份 / 原型 / 性格 / 年龄 / 能力 / 外貌 / 心理）
- 关系（攻受 / 类型 / 年龄 / 动态 / 外形 / 配对 / 性向）
- 情节（元素 / 装置）
- 氛围（风格 / 基调 / 情感 / 节奏）

每个标签带 `标签ID`（W001–W340）和 `作用域`（卡片 / 开场白 / 角色 / 组合）。分词表外的自由标签仍可手填，但 AI 或规则产生的会标成「待确认」；已废弃的 84 个旧标签会被拒绝。核准的词表标签为锁定状态，不能合并或删除。

以后可以合并自定义标签、拉黑或锁定。AI 尚未接入；卡片上留了预填位置，你的修改始终优先。

世界书来源区分为：独立、绑定、卡内嵌、番外。番外是单独一条，可有自己的主题和标签，并链回主世界书和角色卡。

支持的关联：角色卡 ↔ 世界书、世界书 ↔ 番外、角色关系、版本、成套包、面具 ↔ 角色卡、预设 ↔ 世界书、主题 ↔ 背景。

## 导入酒馆时的行为

- 导入是把文件复制到 `/opt/sillytavern/data/default-user/` 下对应目录，**不会**改 `settings.json`，也**不会**重启酒馆。
- 重名时使用 `_2`、`_3` 后缀，绝不覆盖已有文件。
- 复制后的文件所有者是 `tavern:tavern`，权限 `644`。
- 角色卡：PNG 原样复制；JSON / CharX 会在导入时包进带 `chara` 数据块的 PNG（酒馆角色列表只认 PNG）。库里的原件不变。
- 面具：只复制到 `User Avatars/`，需要在酒馆里自己添加或选择。
- 美化：主题和 CSS 进 `themes/`，背景进 `backgrounds/`。
- 生图、文风、聊天记录、正则脚本：不提供一键导入。

导入后请刷新酒馆页面再查看。

## 常用命令

```bash
systemctl status tavern-lib.service tavern-bot.service --no-pager
journalctl -u tavern-lib.service -e -n 80 --no-pager
# 网页配置改完后
systemctl restart tavern-lib.service
```

本机检查（在 VPS 上）：

```bash
curl -sI http://127.0.0.1:8003/_lib/ | head
curl -sI http://127.0.0.1:8001/_lib/ | head
curl -sI http://127.0.0.1:8001/_ctl/ | head
curl -sI http://127.0.0.1:8001/ | head
```

单元测试（在程序目录）：

```bash
cd /opt/tavern-bot && ./venv/bin/python -m unittest discover -s tests -v
```

升级数据库（启动服务时会自动迁移，不会删已有行）。升级前请先备份：

```bash
stamp=$(date +%Y%m%d%H%M%S)
cp -a /opt/tavern-library/library.db /opt/tavern-library/library.db.bak-$stamp
```

## 回滚

详见 `/opt/st-backups/tavern-lib-rollback.txt`。简要步骤：

```bash
systemctl disable --now tavern-bot.service
systemctl disable --now tavern-lib.service
# 还原 nginx（把下面的时间戳换成部署时实际备份名）
# cp -a /etc/nginx/sites-available/tavern-cache.bak-YYYYMMDDHHMMSS /etc/nginx/sites-available/tavern-cache
nginx -t && systemctl reload nginx
# 如需还原控制页
# cp -a /opt/tavern-ctl/tavern-ctl.py.bak-YYYYMMDDHHMMSS /opt/tavern-ctl/tavern-ctl.py
# systemctl restart tavern-ctl.service
```

`/opt/tavern-library/` 里的入库文件不会自动删除。不要改 Cloudflare Access、酒馆的监听地址或 `config.yaml` 的认证设置。不要动 `dsh-web`。
