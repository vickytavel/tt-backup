# tt-backup

酒馆相关自研代码和 VPS 运维配置的私有备份仓库。以阿里云 VPS（`/opt` 与 systemd / nginx）上的部署版本为准。本仓库只收代码和配置模板，不含 token、密码、订阅、数据库和已入库资源。

## 目录

### `tavern-bot/`

Discord 入库机器人，以及资源库 Web 应用（`web.py`、`web_page.html`、`tavernlib/`、测试）。

VPS 部署路径：`/opt/tavern-bot/`。配置在 `/opt/tavern-bot/.env`（不要提交）。systemd 单元：`tavern-bot.service`、`tavern-lib.service`。资源库网页走 `https://tavern.vickytavel.online/_lib/`，反向代理到本机 `127.0.0.1:8003`。

`deploy/` 里是安装脚本和单元模板。注意：线上 `tavern-bot.service` 已加上对 mihomo 代理的依赖和本机代理环境变量，比 `deploy/tavern-bot.service` 新；以 `ops/systemd/tavern-bot.service` 为准。

### `tavern-library/`

VPS 上 `/opt/tavern-library/` 是资源库的数据目录（SQLite `library.db` 和 character / world / preset 等资源文件），**没有独立应用代码**。库的程序、测试和网页模板都在 `tavern-bot/`。本目录只放说明，不备份数据库和资源文件。

### `tavern-ctl/`

控制页（代理开关、缓存刷新）。

VPS 部署路径：`/opt/tavern-ctl/tavern-ctl.py`。单元：`tavern-ctl.service`。网页走 `https://tavern.vickytavel.online/_ctl/`，反向代理到本机 `127.0.0.1:8002`。

### `ops/`

VPS 运维配置与脚本：

- `ops/systemd/`：`tavern-bot`、`tavern-lib`、`tavern-ctl`、`mihomo`、`mihomo-refresh`（service + timer）以及已停用的 `mihomo-idle-stop`（service + timer）。部署到 `/etc/systemd/system/`。
- `ops/sbin/`：`mihomo-refresh` 部署到 `/usr/local/sbin/`；`proxyctl`、`withproxy` 部署到 `/usr/local/bin/`；`99-mihomo-proxy` 部署到 `/etc/update-motd.d/`。
- `ops/nginx/`：站点配置 `tavern-cache`、`tavern.vickytavel.online`、`study.vickytavel.online`，部署到 `/etc/nginx/sites-available/`（并在 `sites-enabled` 里启用）。`ops/nginx/snippets/` 部署到 `/etc/nginx/snippets/`。`study` 站点还会 `include snippets/dsh-agent.conf`（刷题前端的 agent 反代，不在本仓库）。
- `ops/mihomo/README`：本机代理说明。订阅地址在 `/etc/mihomo/sub.url`，生成的代理配置在 `/etc/mihomo/config.yaml`，都不要提交。

酒馆公网站点目前用 nginx 账号密码（`auth_basic`，密码文件在 `/etc/nginx/.htpasswd-tavern`，不要提交）。刷题前端在 `study.vickytavel.online`，根目录 `/var/www/tiku`。
