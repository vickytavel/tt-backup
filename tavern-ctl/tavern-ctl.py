#!/usr/bin/env python3
"""Minimal localhost control page for SillyTavern: proxy on/off and cache bust.

Listens only on 127.0.0.1. Allowed actions are fixed: proxyctl on|off|status,
and a Clear-Site-Data response. No arbitrary commands are accepted.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8002
ALLOWED_ORIGIN = "https://tavern.vickytavel.online"
PROXYCTL = ["/usr/local/bin/proxyctl"]
PREFIX = "/_ctl"


def _safe_proxyctl(action: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    if action not in ("on", "off", "status"):
        raise ValueError("unsupported action")
    return subprocess.run(
        PROXYCTL + [action],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        shell=False,
    )


def _format_remaining(next_stamp: str) -> str:
    match = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", next_stamp)
    if not match:
        return "已设定 2 小时后自动关闭"
    target = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    seconds = int((target - datetime.now()).total_seconds())
    if seconds <= 0:
        return "即将自动关闭"
    hours, rem = divmod(seconds, 3600)
    minutes = (rem + 59) // 60
    if minutes == 60:
        hours += 1
        minutes = 0
    if hours > 0 and minutes > 0:
        return f"大约 {hours} 小时 {minutes} 分钟后自动关闭"
    if hours > 0:
        return f"大约 {hours} 小时后自动关闭"
    if minutes <= 1:
        return "不到 1 分钟后自动关闭"
    return f"大约 {minutes} 分钟后自动关闭"


def parse_status(text: str) -> dict:
    running = False
    state_label = "未知"
    auto_stop_label = "未设定自动关闭"
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("status:"):
            val = line.split(":", 1)[1].strip()
            if val.startswith("running"):
                running = True
                state_label = "运行中"
            elif val == "stopped":
                running = False
                state_label = "已停止"
            else:
                running = False
                state_label = "状态异常"
        elif line.startswith("idle auto-stop:"):
            val = line.split(":", 1)[1].strip()
            if val.startswith("not armed"):
                auto_stop_label = "未设定自动关闭"
            elif "next:" in val:
                auto_stop_label = _format_remaining(val.split("next:", 1)[1].strip())
            elif val.startswith("armed"):
                auto_stop_label = "已设定 2 小时后自动关闭"
    if state_label == "已停止":
        auto_stop_label = "已关闭，Discord 机器人会掉线"
    elif state_label == "运行中" and auto_stop_label == "未设定自动关闭":
        auto_stop_label = "常驻运行，不会自动关闭"
    return {
        "ok": True,
        "running": running,
        "state_label": state_label,
        "auto_stop_label": auto_stop_label,
    }


def _timer_remaining_label() -> str | None:
    """Fallback remaining-time from the idle-stop timer, if proxyctl omitted it."""
    try:
        result = subprocess.run(
            [
                "systemctl",
                "show",
                "mihomo-idle-stop.timer",
                "--property=NextElapseUSecRealtime",
                "--property=ActiveEnterTimestamp",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    props: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key] = value.strip()
    nxt = props.get("NextElapseUSecRealtime", "")
    if nxt and nxt not in ("n/a", "infinity"):
        return _format_remaining(nxt)
    entered = props.get("ActiveEnterTimestamp", "")
    match = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", entered)
    if not match:
        return None
    start = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    return _format_remaining((start + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"))


def get_status() -> dict:
    try:
        result = _safe_proxyctl("status", timeout=15)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "running": False,
            "state_label": "状态未知",
            "auto_stop_label": "查询超时",
            "message": "查询状态超时，请稍后重试。",
        }
    except OSError:
        return {
            "ok": False,
            "running": False,
            "state_label": "状态未知",
            "auto_stop_label": "无法查询",
            "message": "无法查询代理状态。",
        }
    if result.returncode != 0:
        return {
            "ok": False,
            "running": False,
            "state_label": "状态未知",
            "auto_stop_label": "无法查询",
            "message": "无法查询代理状态。",
        }
    payload = parse_status(result.stdout + "\n" + result.stderr)
    payload["ok"] = True
    if payload.get("running") and payload.get("auto_stop_label") == "已设定 2 小时后自动关闭":
        fallback = _timer_remaining_label()
        if fallback:
            payload["auto_stop_label"] = fallback
    return payload


def run_action(action: str) -> dict:
    try:
        result = _safe_proxyctl(action, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "操作超时，请稍后重试。"}
    except OSError:
        return {"ok": False, "message": "无法执行该操作。"}
    if result.returncode != 0:
        if action == "on":
            return {"ok": False, "message": "开启失败，请稍后重试。"}
        return {"ok": False, "message": "关闭失败，请稍后重试。"}
    status = get_status()
    if action == "on":
        status["message"] = "代理已开启（常驻）。"
    else:
        status["message"] = "代理已关闭。"
    return status


PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="referrer" content="same-origin">
  <meta name="robots" content="noindex,nofollow">
  <title>酒馆控制</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #12140f;
      --card: #1c2a1a;
      --text: #e8eee4;
      --muted: #a8b5a4;
      --line: #2d3d2a;
      --btn: #3d6b3a;
      --btn-text: #f4faf1;
      --btn-off: #6b3a3a;
      --btn-neutral: #2a3828;
      --warn: #e6c07b;
      --ok: #9ece88;
      --err: #e07a7a;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Noto Sans SC", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.55;
      min-height: 100vh;
      padding: 24px 18px 48px;
    }
    main {
      max-width: 520px;
      margin: 0 auto;
    }
    h1 {
      font-size: 1.4rem;
      font-weight: 650;
      margin: 0 0 8px;
    }
    .lead {
      color: var(--muted);
      margin: 0 0 22px;
      font-size: 0.95rem;
    }
    section {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 18px 16px 16px;
      margin-bottom: 14px;
    }
    h2 {
      font-size: 1.05rem;
      margin: 0 0 10px;
    }
    .status {
      margin: 0 0 6px;
      font-size: 1.02rem;
    }
    .status strong { font-weight: 650; }
    .hint, .sub {
      color: var(--muted);
      margin: 0 0 14px;
      font-size: 0.92rem;
    }
    .msg {
      display: none;
      margin: 0 0 12px;
      font-size: 0.92rem;
    }
    .msg.show { display: block; }
    .msg.ok { color: var(--ok); }
    .msg.err { color: var(--err); }
    .msg.warn { color: var(--warn); }
    .btns {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    button, .back {
      appearance: none;
      display: block;
      width: 100%;
      border: 0;
      border-radius: 12px;
      padding: 13px 14px;
      font-size: 1rem;
      font-weight: 600;
      text-align: center;
      text-decoration: none;
      cursor: pointer;
    }
    button:disabled {
      opacity: 0.6;
      cursor: wait;
    }
    .on { background: var(--btn); color: var(--btn-text); }
    .off { background: var(--btn-off); color: var(--btn-text); }
    .refresh { background: var(--btn); color: var(--btn-text); }
    .back {
      background: var(--btn-neutral);
      color: var(--text);
      border: 1px solid var(--line);
    }
  </style>
</head>
<body>
  <main>
    <h1>酒馆控制</h1>
    <p class="lead">VPS 用专用订阅常驻代理，给 Discord 机器人和访问 GitHub、装扩展用。关闭后机器人会掉线。</p>

    <section>
      <h2>代理</h2>
      <p class="status">当前状态：<strong id="state">__STATE__</strong></p>
      <p class="sub" id="autostop">__AUTOSTOP__</p>
      <p class="msg" id="proxy-msg"></p>
      <div class="btns">
        <button type="button" class="on" id="btn-on">开启代理</button>
        <button type="button" class="off" id="btn-off">关闭代理</button>
      </div>
    </section>

    <section>
      <h2>缓存</h2>
      <p class="hint">如果刚更新了酒馆或扩展，点这个可以清掉手机上的旧文件，再重新加载最新的。</p>
      <p class="msg" id="cache-msg"></p>
      <form id="cache-form" method="post" action="/_ctl/cache/clear">
        <div class="btns">
          <button type="submit" class="refresh" id="btn-cache">刷新缓存，加载最新文件</button>
        </div>
      </form>
    </section>

    <section>
      <h2>资源库</h2>
      <p class="hint">浏览机器人收到的角色卡、世界书和预设，点一下就能导入当前酒馆。</p>
      <div class="btns">
        <a class="back" href="/_lib/">打开资源库</a>
      </div>
    </section>

    <section>
      <h2>返回</h2>
      <a class="back" href="/">返回酒馆</a>
    </section>
  </main>
  <script>
    const stateEl = document.getElementById("state");
    const autoEl = document.getElementById("autostop");
    const proxyMsg = document.getElementById("proxy-msg");
    const cacheMsg = document.getElementById("cache-msg");
    const btnOn = document.getElementById("btn-on");
    const btnOff = document.getElementById("btn-off");
    const btnCache = document.getElementById("btn-cache");

    function showMsg(el, text, kind) {
      el.textContent = text || "";
      el.className = "msg" + (text ? " show " + kind : "");
    }

    function applyStatus(data) {
      if (data.state_label) stateEl.textContent = data.state_label;
      if (data.auto_stop_label) autoEl.textContent = data.auto_stop_label;
    }

    async function postAction(path) {
      const res = await fetch(path, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Accept": "application/json" },
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) {
        throw new Error(data.message || "操作失败，请稍后重试。");
      }
      return data;
    }

    async function withBusy(btns, fn) {
      btns.forEach((b) => { b.disabled = true; });
      try {
        await fn();
      } finally {
        btns.forEach((b) => { b.disabled = false; });
      }
    }

    btnOn.addEventListener("click", () => {
      withBusy([btnOn, btnOff], async () => {
        showMsg(proxyMsg, "正在开启代理，可能需要一点时间…", "warn");
        const data = await postAction("/_ctl/proxy/on");
        applyStatus(data);
        showMsg(proxyMsg, data.message || "代理已开启。", "ok");
      }).catch((err) => showMsg(proxyMsg, err.message, "err"));
    });

    btnOff.addEventListener("click", () => {
      withBusy([btnOn, btnOff], async () => {
        showMsg(proxyMsg, "正在关闭代理…", "warn");
        const data = await postAction("/_ctl/proxy/off");
        applyStatus(data);
        showMsg(proxyMsg, data.message || "代理已关闭。", "ok");
      }).catch((err) => showMsg(proxyMsg, err.message, "err"));
    });

    document.getElementById("cache-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      btnCache.disabled = true;
      showMsg(cacheMsg, "正在清除缓存…", "warn");
      try {
        if (window.caches && caches.keys) {
          const keys = await caches.keys();
          await Promise.all(keys.map((key) => caches.delete(key)));
        }
      } catch (err) {
        // Still continue so Clear-Site-Data can run.
      }
      event.target.submit();
    });
  </script>
</body>
</html>
"""


CLEAR_CACHE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="1;url=/">
  <title>正在返回酒馆</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif;
           background: #12140f; color: #e8eee4; padding: 40px 20px; text-align: center; }
    a { color: #9ece88; }
  </style>
</head>
<body>
  <p>缓存已请求清除，正在返回酒馆…</p>
  <p><a href="/">如果没有自动跳转，点这里</a></p>
  <script>
    (async () => {
      try {
        if (window.caches && caches.keys) {
          const keys = await caches.keys();
          await Promise.all(keys.map((key) => caches.delete(key)));
        }
      } catch (err) {}
      location.replace("/");
    })();
  </script>
</body>
</html>
"""


def is_allowed_origin(handler: BaseHTTPRequestHandler) -> bool:
    origin = (handler.headers.get("Origin") or "").strip()
    if origin:
        return origin == ALLOWED_ORIGIN
    referer = (handler.headers.get("Referer") or "").strip()
    if not referer:
        return False
    parsed = urlparse(referer)
    return f"{parsed.scheme}://{parsed.netloc}" == ALLOWED_ORIGIN


def normalize_path(path: str) -> str:
    parsed = urlparse(path)
    cleaned = parsed.path.rstrip("/") or "/"
    return cleaned


class CtlHandler(BaseHTTPRequestHandler):
    server_version = "tavern-ctl/1.0"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        sys_stderr_line = f"{self.address_string()} {fmt % args}"
        print(sys_stderr_line, flush=True)

    def _send(self, code: int, body: bytes, content_type: str, extra_headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _send_html(self, code: int, html: str, extra_headers: dict | None = None) -> None:
        self._send(code, html.encode("utf-8"), "text/html; charset=utf-8", extra_headers)

    def _forbidden(self) -> None:
        self._send_json(403, {"ok": False, "message": "请求被拒绝。"})

    def do_GET(self) -> None:
        path = normalize_path(self.path)
        if path in (PREFIX, f"{PREFIX}/index.html"):
            status = get_status()
            html = (
                PAGE_HTML.replace("__STATE__", status.get("state_label") or "未知")
                .replace("__AUTOSTOP__", status.get("auto_stop_label") or "未设定自动关闭")
            )
            self._send_html(200, html)
            return
        if path == f"{PREFIX}/status":
            self._send_json(200, get_status())
            return
        self._send_html(404, "<!DOCTYPE html><html><body>Not found</body></html>")

    def do_POST(self) -> None:
        if not is_allowed_origin(self):
            self._forbidden()
            return
        path = normalize_path(self.path)
        if path == f"{PREFIX}/proxy/on":
            result = run_action("on")
            self._send_json(200 if result.get("ok") else 500, result)
            return
        if path == f"{PREFIX}/proxy/off":
            result = run_action("off")
            self._send_json(200 if result.get("ok") else 500, result)
            return
        if path == f"{PREFIX}/cache/clear":
            self._send_html(
                200,
                CLEAR_CACHE_HTML,
                extra_headers={
                    "Clear-Site-Data": '"cache"',
                    "Cache-Control": "no-store, no-cache, must-revalidate",
                },
            )
            return
        self._send_html(404, "<!DOCTYPE html><html><body>Not found</body></html>")

    def do_HEAD(self) -> None:
        path = normalize_path(self.path)
        if path in (PREFIX, f"{PREFIX}/index.html", f"{PREFIX}/status"):
            self._send(200, b"", "text/html; charset=utf-8")
            return
        self._send(404, b"", "text/plain")


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), CtlHandler)
    print(f"tavern-ctl listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
