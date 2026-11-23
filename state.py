from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class State:
    def __init__(self, data_dir: str, paper_equity: float):
        self.path = Path(data_dir) / "state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.data = {
            "paused": False,
            "paper_equity": paper_equity,
            "positions": {},
            "cooldowns": {},
            "watch_seen": {},
            "early_seen": {},
            "history": [],
            "day": {"date": "", "start_equity": 0.0, "realized": 0.0, "trades": 0},
        }
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text()))
            except Exception:
                pass

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1))
            os.replace(tmp, self.path)

    def ensure_day(self, equity_fn):
        """Reinicia contadores diarios (UTC). Devuelve el resumen del día anterior si hubo cambio."""
        with self.lock:
            today = utc_date()
            d = self.data["day"]
            if d.get("date") == today:
                return None
            prev = dict(d) if d.get("date") else None
            try:
                eq = float(equity_fn())
            except Exception:
                eq = float(d.get("start_equity", 0.0))
            self.data["day"] = {"date": today, "start_equity": eq, "realized": 0.0, "trades": 0}
            self.save()
            return prev

    def cooldown_active(self, sym: str, minutes: int) -> bool:
        return time.time() - self.data["cooldowns"].get(sym, 0) < minutes * 60

    def set_cooldown(self, sym: str):
        with self.lock:
            now = time.time()
            self.data["cooldowns"][sym] = now
            self.data["cooldowns"] = {k: v for k, v in self.data["cooldowns"].items() if now - v < 86400}
            self.save()

    def early_mark(self, sym: str):
        with self.lock:
            now = time.time()
            self.data["early_seen"][sym] = now
            self.data["early_seen"] = {k: v for k, v in self.data["early_seen"].items() if now - v < 86400}
            self.save()

    def early_age_min(self, sym: str, max_min: float = 90.0):
        """Minutos desde la última señal anticipada de `sym` (None si no hay o es antigua)."""
        t = self.data["early_seen"].get(sym)
        age = (time.time() - t) / 60 if t else None
        return age if age is not None and age <= max_min else None

    def watch_due(self, key: str, minutes: int) -> bool:
        """True si esta candidata no se ha avisado en los últimos `minutes`."""
        return time.time() - self.data["watch_seen"].get(key, 0) >= minutes * 60

    def watch_mark(self, keys):
        with self.lock:
            now = time.time()
            for k in keys:
                self.data["watch_seen"][k] = now
            self.data["watch_seen"] = {k: v for k, v in self.data["watch_seen"].items() if now - v < 172800}
            self.save()
