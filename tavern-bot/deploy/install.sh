#!/bin/bash
# Install or update the tavern library + Discord bot on the VPS.
# Safe to re-run. Does not start tavern-bot.service (token may be empty).
# Does not touch dsh-web, Cloudflare Access, or SillyTavern config.yaml.
set -euo pipefail

SRC="${1:-/tmp/tavern-bot-src}"
CTL_SRC="${2:-/tmp/tavern-ctl.py}"
STAMP="$(date +%Y%m%d%H%M%S)"

if [[ ! -d "$SRC" ]]; then
  echo "source tree not found: $SRC" >&2
  exit 1
fi

echo "==> $STAMP starting tavern-lib/bot install"

# --- users / group ---
if ! getent group tavernlib >/dev/null; then
  groupadd --system tavernlib
fi
if ! id -u tavernbot >/dev/null 2>&1; then
  useradd --system --home-dir /opt/tavern-bot --no-create-home --shell /usr/sbin/nologin --gid tavernlib tavernbot
fi
if ! id -nG tavern | tr ' ' '\n' | grep -qx tavernlib; then
  usermod -aG tavernlib tavern
fi

# --- copy code, keep venv and .env ---
mkdir -p /opt/tavern-bot /opt/tavern-library
rsync -a --delete \
  --exclude venv \
  --exclude .env \
  --exclude '__pycache__/' \
  --exclude '.git/' \
  "$SRC/" /opt/tavern-bot/

if [[ ! -f /opt/tavern-bot/.env ]]; then
  cp /opt/tavern-bot/.env.example /opt/tavern-bot/.env
fi

# --- venv / deps ---
if [[ ! -x /opt/tavern-bot/venv/bin/python ]]; then
  python3 -m venv /opt/tavern-bot/venv
fi

pip_install() {
  /opt/tavern-bot/venv/bin/pip install --upgrade pip
  /opt/tavern-bot/venv/bin/pip install -r /opt/tavern-bot/requirements.txt "$@"
}

if ! pip_install -i https://pypi.tuna.tsinghua.edu.cn/simple; then
  echo "Tsinghua PyPI mirror failed; will try with proxy if needed" >&2
  exit 41
fi

# --- permissions ---
chown -R root:tavernlib /opt/tavern-bot
# Keep venv usable; do not make the tree group-writable.
find /opt/tavern-bot -type d -exec chmod 755 {} +
find /opt/tavern-bot -type f -exec chmod 644 {} +
chmod 755 /opt/tavern-bot/venv/bin/* || true
chmod 755 /opt/tavern-bot/bot.py /opt/tavern-bot/web.py /opt/tavern-bot/import_helper.py
chown root:root /opt/tavern-bot/import_helper.py
chmod 755 /opt/tavern-bot/import_helper.py

chown tavernbot:tavernlib /opt/tavern-bot/.env
chmod 600 /opt/tavern-bot/.env

# venv should stay owned by root but executable by tavernbot (already 755).
chown -R tavernbot:tavernlib /opt/tavern-library
chmod 2770 /opt/tavern-library
# Initialize DB as tavernbot so the file is writable by the services.
sudo -u tavernbot env PYTHONPATH=/opt/tavern-bot /opt/tavern-bot/venv/bin/python -c "from tavernlib.store import LibraryStore; LibraryStore('/opt/tavern-library')"
chmod 2770 /opt/tavern-library
find /opt/tavern-library -type d -exec chmod 2770 {} +
chmod 660 /opt/tavern-library/library.db /opt/tavern-library/library.db-wal /opt/tavern-library/library.db-shm 2>/dev/null || chmod 660 /opt/tavern-library/library.db
chown -R tavernbot:tavernlib /opt/tavern-library

# --- sudoers ---
install -m 440 /opt/tavern-bot/deploy/tavern-lib-sudoers /etc/sudoers.d/tavern-lib
visudo -cf /etc/sudoers.d/tavern-lib

# --- systemd ---
install -m 644 /opt/tavern-bot/deploy/tavern-bot.service /etc/systemd/system/tavern-bot.service
install -m 644 /opt/tavern-bot/deploy/tavern-lib.service /etc/systemd/system/tavern-lib.service
systemctl daemon-reload

# --- nginx (backup first) ---
cp -a /etc/nginx/sites-available/tavern-cache "/etc/nginx/sites-available/tavern-cache.bak-${STAMP}"
install -m 644 /opt/tavern-bot/deploy/tavern-lib-proxy.conf /etc/nginx/snippets/tavern-lib-proxy.conf
python3 - <<'PY'
from pathlib import Path
p = Path("/etc/nginx/sites-available/tavern-cache")
t = p.read_text()
if "location ^~ /_lib" in t:
    print("nginx /_lib location already present")
else:
    needle = """    # Control page: proxy + cache refresh. Never cache.
    location ^~ /_ctl {
        include snippets/tavern-ctl-proxy.conf;
        client_max_body_size 1m;
    }
"""
    insert = needle + """
    # Resource library. Never cache.
    location ^~ /_lib {
        include snippets/tavern-lib-proxy.conf;
        client_max_body_size 20m;
    }
"""
    if needle not in t:
        raise SystemExit("nginx needle for /_ctl not found; aborting without edit")
    p.write_text(t.replace(needle, insert, 1))
    print("inserted nginx /_lib location")
PY
nginx -t

# --- control page ---
if [[ -f "$CTL_SRC" ]]; then
  cp -a /opt/tavern-ctl/tavern-ctl.py "/opt/tavern-ctl/tavern-ctl.py.bak-${STAMP}"
  install -m 755 -o root -g root "$CTL_SRC" /opt/tavern-ctl/tavern-ctl.py
fi

# --- rollback note ---
if [[ -d /opt/st-backups ]]; then
  cp /opt/tavern-bot/deploy/rollback.txt /opt/st-backups/tavern-lib-rollback.txt
  echo "backup stamp: ${STAMP}" >> /opt/st-backups/tavern-lib-rollback.txt
  echo "nginx backup: /etc/nginx/sites-available/tavern-cache.bak-${STAMP}" >> /opt/st-backups/tavern-lib-rollback.txt
  echo "ctl backup: /opt/tavern-ctl/tavern-ctl.py.bak-${STAMP}" >> /opt/st-backups/tavern-lib-rollback.txt
fi

# --- tests ---
cd /opt/tavern-bot
./venv/bin/python -m unittest discover -s tests -v

# --- start library web only; do not start the bot ---
systemctl enable tavern-lib.service
systemctl restart tavern-lib.service
# Make sure the bot unit is installed but not enabled or started.
systemctl disable tavern-bot.service >/dev/null 2>&1 || true
if systemctl is-active --quiet tavern-bot.service; then
  echo "Refusing to leave tavern-bot running without an explicit token install" >&2
  systemctl stop tavern-bot.service
fi

# Reload nginx and restart control page after HTML change.
systemctl reload nginx
systemctl restart tavern-ctl.service

# Give services a moment to bind.
sleep 1

echo "==> install finished, stamp=${STAMP}"
