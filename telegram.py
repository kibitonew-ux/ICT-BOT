from __future__ import annotations

import html
import inspect
import logging
import re
import time

import requests

log = logging.getLogger("telegram")
_TAGS = re.compile(r"<[^>]+>")


class Notifier:
    def __init__(self, cfg):
        self.token = cfg.tg_token
        self.chat = cfg.tg_chat_id
        self.base = f"https://api.telegram.org/bot{self.token}"
        self._last_alert = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    def send(self, text: str) -> bool:
        """Envía el mensaje. Devuelve True si Telegram lo aceptó."""
        log.info(text.replace("\n", " | "))
        if not self.enabled:
            return False
        # 1.º intento con HTML; si Telegram rechaza el formato (400), reintenta en texto plano
        for as_html in (True, False):
            body = text[:4000] if as_html else html.unescape(_TAGS.sub("", text))[:4000]
            payload = {"chat_id": self.chat, "text": body, "disable_web_page_preview": True}
            if as_html:
                payload["parse_mode"] = "HTML"
            try:
                r = requests.post(f"{self.base}/sendMessage", json=payload, timeout=15)
            except Exception as e:
                log.error("Telegram no accesible: %s", e)
                return False
            if r.status_code == 200:
                return True
            log.error("Telegram respondió %s: %s", r.status_code, r.text[:200])
            if r.status_code != 400:                  # 401 token malo, 403 sin /start, 429 límite...
                return False
        return False

    def alert_once(self, key: str, text: str, every: int = 1800):
        """Evita spamear el mismo error."""
        now = time.time()
        if now - self._last_alert.get(key, 0) >= every:
            self._last_alert[key] = now
            self.send(text)

    def listen(self, handlers: dict, stop):
        """Comandos de Telegram por long-polling. Solo responde al chat autorizado."""
        if not self.enabled:
            return
        offset = None
        while not stop.is_set():
            try:
                r = requests.get(f"{self.base}/getUpdates",
                                 params={"timeout": 25, "offset": offset}, timeout=35).json()
                if not r.get("ok"):
                    # 409 = otro proceso usa el mismo token con getUpdates (p. ej. otro bot)
                    log.warning("getUpdates falló: %s", r.get("description"))
                    stop.wait(15)
                    continue
                for u in r.get("result", []):
                    offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    if str(msg.get("chat", {}).get("id")) != str(self.chat):
                        continue
                    parts = (msg.get("text") or "").split()
                    if not parts:
                        continue
                    fn = handlers.get(parts[0].split("@")[0].lower())
                    if fn:
                        n = len(inspect.signature(fn).parameters)   # comandos con argumento: /debug TRX
                        self.send(fn(*parts[1:1 + n]))
            except Exception as e:
                log.warning("poll error: %s", e)
                stop.wait(5)
