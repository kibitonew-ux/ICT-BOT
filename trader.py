"""
Ejecución y gestión de posiciones (paper o live en BingX perpetuos).

Gestión estilo Qullamaggie, adaptada a intradía:
  - Entrada a mercado tras vela 15m cerrada.
  - Stop técnico (colocado EN el exchange en live + vigilado por software).
  - TP1 a N·R: vende parte (PARTIAL_PCT) y mueve el stop a break-even.
  - El resto se gestiona con trailing por EMA (1h EMA10 por defecto).
  - Salida por tiempo máximo.
"""
from __future__ import annotations

import logging
import threading
import time

from indicators import ema, to_df

log = logging.getLogger("trader")


def px(x: float) -> str:
    return f"{x:.8g}"


class Trader:
    def __init__(self, ex, cfg, state, notify):
        self.ex, self.cfg, self.st, self.notify = ex, cfg, state, notify
        self.lock = threading.RLock()
        self._setup_done = set()
        self.last_trail = 0.0
        self.last_recon = 0.0

    # ───────────── utilidades ─────────────
    @property
    def live(self) -> bool:
        return self.cfg.mode == "live"

    @property
    def positions(self) -> dict:
        return self.st.data["positions"]

    def equity(self) -> float:
        if not self.live:
            return float(self.st.data["paper_equity"])
        bal = self.ex.fetch_balance({"type": "swap"})
        u = bal.get("USDT") or {}
        return float(u.get("total") or u.get("free") or 0.0)

    def _pp(self, side: str, closing: bool = False) -> dict:
        p = {}
        if self.cfg.hedge_mode:
            p["positionSide"] = "LONG" if side == "long" else "SHORT"
        elif closing:
            p["reduceOnly"] = True
        return p

    @staticmethod
    def _order_side(side: str, closing: bool) -> str:
        return ("sell" if side == "long" else "buy") if closing else ("buy" if side == "long" else "sell")

    def _prec(self, sym: str, q: float) -> float:
        try:
            return float(self.ex.amount_to_precision(sym, q))
        except Exception:
            return q

    def _min_qty(self, sym: str) -> float:
        try:
            return float(self.ex.market(sym)["limits"]["amount"]["min"] or 0)
        except Exception:
            return 0.0

    def _fill_price(self, order: dict, sym: str, fallback: float) -> float:
        p = order.get("average") or order.get("price")
        if not p and order.get("id"):
            time.sleep(0.6)
            try:
                o = self.ex.fetch_order(order["id"], sym)
                p = o.get("average") or o.get("price")
            except Exception:
                pass
        return float(p) if p else float(fallback)

    def _last(self, sym: str) -> float:
        return float(self.ex.fetch_ticker(sym)["last"])

    def _prices(self, symbols: list) -> dict:
        try:
            t = self.ex.fetch_tickers(symbols)
            return {s: float(v["last"]) for s, v in t.items() if v.get("last")}
        except Exception:
            out = {}
            for s in symbols:
                try:
                    out[s] = self._last(s)
                except Exception:
                    pass
            return out

    def _setup_symbol(self, sym: str):
        if sym in self._setup_done:
            return
        try:
            self.ex.set_margin_mode(self.cfg.margin_mode, sym)
        except Exception as e:
            log.warning("margin mode %s: %s", sym, e)
        for side in (("LONG", "SHORT") if self.cfg.hedge_mode else ("BOTH",)):
            try:
                self.ex.set_leverage(self.cfg.leverage, sym, {"side": side})
            except Exception as e:
                log.warning("leverage %s %s: %s", sym, side, e)
        self._setup_done.add(sym)

    # ───────────── stops en exchange ─────────────
    def _place_stop(self, sym: str, side: str, qty: float, price: float) -> str:
        price = float(self.ex.price_to_precision(sym, price))
        params = {**self._pp(side, True), "stopLossPrice": price}
        o = self.ex.create_order(sym, "market", self._order_side(side, True), qty, None, params)
        return o["id"]

    def _cancel_stop(self, pos: dict):
        if self.live and pos.get("sl_id"):
            try:
                self.ex.cancel_order(pos["sl_id"], pos["symbol"])
            except Exception as e:
                log.debug("cancel stop: %s", e)
        pos["sl_id"] = None

    def set_stop(self, pos: dict, new_price: float, why: str = ""):
        sym = pos["symbol"]
        try:
            new_price = float(self.ex.price_to_precision(sym, new_price))
        except Exception:
            pass
        if self.live:
            self._cancel_stop(pos)
            try:
                pos["sl_id"] = self._place_stop(sym, pos["side"], pos["qty"], new_price)
            except Exception as e:
                self.notify(f"⚠️ No pude mover el SL en el exchange de {sym}: {e}\n"
                            f"Queda el stop por software en {px(new_price)}")
        pos["stop"] = new_price
        self.st.save()
        if why:
            self.notify(f"🔧 <b>{sym}</b> SL → {px(new_price)} ({why})")

    # ───────────── apertura ─────────────
    def can_open(self, sig):
        c, d = self.cfg, self.st.data["day"]
        if c.mode == "signals":
            return False, "modo señales"
        if self.st.data["paused"]:
            return False, "bot en pausa (/resume)"
        if sig.symbol in self.positions:
            return False, "ya hay posición abierta"
        if len(self.positions) >= c.max_open:
            return False, f"máximo de posiciones abiertas ({c.max_open})"
        if d["trades"] >= c.max_trades_day:
            return False, f"máximo de operaciones diarias ({c.max_trades_day})"
        if d["start_equity"] > 0 and d["realized"] <= -d["start_equity"] * c.daily_loss_pct / 100:
            return False, f"límite de pérdida diaria alcanzado ({c.daily_loss_pct}%)"
        return True, ""

    def size(self, sig, equity: float) -> float:
        mult = self.cfg.early_risk_mult if getattr(sig, "early", False) else 1.0
        qty = (equity * self.cfg.risk_pct / 100 * mult) / sig.risk
        max_notional = equity * self.cfg.max_margin_pct / 100 * self.cfg.leverage
        if qty * sig.entry > max_notional:
            qty = max_notional / sig.entry
        return self._prec(sig.symbol, qty)

    def _add(self, sig, pos):
        """Confirmación 15m de una entrada anticipada: añade el resto del riesgo (mismo stop)."""
        c = self.cfg
        if self.st.data["paused"]:
            return False, "bot en pausa (/resume)"
        if pos["tp1_hit"] or pos.get("be_done"):
            return False, "la anticipada ya va en beneficio (break-even): no se añade, no perseguir"
        long = pos["side"] == "long"
        if (long and sig.entry <= pos["stop"]) or ((not long) and sig.entry >= pos["stop"]):
            return False, "el precio ya está más allá del stop"
        try:
            eq = self.equity()
        except Exception as e:
            return False, f"no pude leer el balance: {e}"
        dist = abs(sig.entry - pos["stop"])
        qty = eq * c.risk_pct / 100 * (1 - c.early_risk_mult) / dist
        room = eq * c.max_margin_pct / 100 * c.leverage - pos["qty"] * pos["entry"]
        qty = self._prec(sig.symbol, min(qty, max(0.0, room) / sig.entry))
        if qty <= 0 or qty < self._min_qty(sig.symbol):
            return False, "tamaño a añadir por debajo del mínimo o sin margen disponible"
        entry = sig.entry
        if self.live:
            try:
                o = self.ex.create_order(sig.symbol, "market", self._order_side(sig.side, False),
                                         qty, None, self._pp(sig.side))
                entry = self._fill_price(o, sig.symbol, sig.entry)
            except Exception as e:
                return False, f"error al añadir: {e}"
        new_qty = pos["qty"] + qty
        avg = (pos["entry"] * pos["qty"] + entry * qty) / new_qty
        pos.update(entry=avg, qty=new_qty, qty0=pos["qty0"] + qty, added=True,
                   risk=max(abs(avg - pos["stop"]), avg * 0.001))
        pos["risk_usd"] = pos["qty"] * pos["risk"]
        self.st.save()
        if self.live:
            self.set_stop(pos, pos["stop"])            # reajusta el stop del exchange a la cantidad total
            if not pos.get("sl_id"):                   # FAIL-SAFE: nunca dejar la posición sin stop
                self.close(sig.symbol, "Sin stop en exchange tras añadir (seguridad)")
                return False, "stop no recolocado: posición cerrada por seguridad"
        self.notify(f"➕ <b>{sig.symbol}</b> confirmada en 15m: añadidas {qty} @ {px(entry)} | "
                    f"total {pos['qty']} @ media {px(avg)} | SL {px(pos['stop'])} "
                    f"| riesgo total ≈ ${pos['risk_usd']:.2f}")
        return True, ""

    def open(self, sig):
        with self.lock:
            cur = self.positions.get(sig.symbol)
            if (cur and cur.get("early") and not cur.get("added") and not getattr(sig, "early", False)
                    and cur["side"] == sig.side):
                return self._add(sig, cur)
            ok, why = self.can_open(sig)
            if not ok:
                return False, why
            try:
                eq = self.equity()
            except Exception as e:
                return False, f"no pude leer el balance: {e}"
            qty = self.size(sig, eq)
            if qty <= 0 or qty < self._min_qty(sig.symbol):
                return False, "tamaño por debajo del mínimo del exchange (saldo/riesgo muy bajo)"

            long = sig.side == "long"
            entry, sl_id = sig.entry, None
            if self.live:
                try:
                    self._setup_symbol(sig.symbol)
                    o = self.ex.create_order(sig.symbol, "market", self._order_side(sig.side, False),
                                             qty, None, self._pp(sig.side))
                    entry = self._fill_price(o, sig.symbol, sig.entry)
                except Exception as e:
                    return False, f"error al enviar la orden: {e}"
                bad = (long and entry <= sig.stop) or ((not long) and entry >= sig.stop)
                try:
                    if bad:
                        raise RuntimeError("el precio de entrada ya superó el stop")
                    sl_id = self._place_stop(sig.symbol, sig.side, qty, sig.stop)
                except Exception as e:
                    # FAIL-SAFE: nunca dejar una posición sin stop
                    try:
                        self.ex.create_order(sig.symbol, "market", self._order_side(sig.side, True),
                                             qty, None, self._pp(sig.side, True))
                        self.notify(f"🛑 {sig.symbol}: no pude colocar el stop ({e}). Posición cerrada por seguridad.")
                    except Exception as e2:
                        self.notify(f"🚨 <b>URGENTE {sig.symbol}</b>: posición abierta SIN stop y no pude cerrarla "
                                    f"({e2}). Ciérrala a mano en BingX.")
                    return False, "stop no colocado"

            risk = max(abs(entry - sig.stop), entry * 0.001)
            pos = {
                "symbol": sig.symbol, "side": sig.side, "setup": sig.setup, "score": sig.score,
                "entry": entry, "stop": sig.stop, "init_stop": sig.stop, "risk": risk,
                "qty": qty, "qty0": qty, "risk_usd": qty * risk, "opened": time.time(),
                "tp1_hit": False, "max_r": 0.0, "sl_id": sl_id, "realized": 0.0, "miss": 0,
                "early": bool(getattr(sig, "early", False)), "added": False, "be_done": False,
            }
            self.positions[sig.symbol] = pos
            self.st.data["day"]["trades"] += 1
            self.st.save()
            tp1 = entry + (1 if long else -1) * self.cfg.tp1_r * risk
            self.notify(
                f"{'🟢' if long else '🔴'} <b>ENTRADA {sig.side.upper()} {sig.symbol}</b> "
                f"[{'LIVE' if self.live else 'PAPER'}]\n"
                f"{sig.setup} · score {sig.score:.0f}\n"
                f"Cantidad {qty} @ {px(entry)} | SL {px(sig.stop)} | TP1 {px(tp1)}\n"
                f"Riesgo ≈ ${pos['risk_usd']:.2f} ({self.cfg.risk_pct * (self.cfg.early_risk_mult if pos['early'] else 1):.2f}% del equity)"
                + ("\nEntrada anticipada: el resto se añade si cierra la vela 15m confirmando" if pos["early"] else "")
            )
            return True, ""

    # ───────────── cierre ─────────────
    def _book(self, pos: dict, qty: float, exit_px: float, reason: str):
        d = 1 if pos["side"] == "long" else -1
        gross = (exit_px - pos["entry"]) * qty * d
        fees = (pos["entry"] + exit_px) * qty * self.cfg.fee_pct / 100
        pnl = gross - fees
        r_mult = (exit_px - pos["entry"]) * d / pos["risk"]
        pos["qty"] = max(0.0, pos["qty"] - qty)
        pos["realized"] = pos.get("realized", 0.0) + pnl
        self.st.data["day"]["realized"] += pnl
        if not self.live:
            self.st.data["paper_equity"] += pnl
        full = pos["qty"] <= pos["qty0"] * 1e-6
        if full:
            self.positions.pop(pos["symbol"], None)
            hist = self.st.data["history"]
            hist.append({"symbol": pos["symbol"], "side": pos["side"], "setup": pos["setup"],
                         "pnl": round(pos["realized"], 4), "closed": time.time(), "reason": reason})
            del hist[:-50]
        self.st.save()
        self.notify(
            f"{'✅' if pnl >= 0 else '❌'} <b>{'CIERRE' if full else 'PARCIAL'} {pos['side'].upper()} "
            f"{pos['symbol']}</b> [{'LIVE' if self.live else 'PAPER'}]\n"
            f"{reason} @ {px(exit_px)} | {r_mult:+.2f}R | PnL ${pnl:+.2f}"
            + (f"\nTotal operación: ${pos['realized']:+.2f}" if full else "")
        )

    def close(self, sym: str, reason: str, fraction: float = 1.0, price: float = None) -> bool:
        with self.lock:
            pos = self.positions.get(sym)
            if not pos:
                return False
            qty = pos["qty"]
            if fraction < 1.0:
                q = self._prec(sym, qty * fraction)
                if q <= 0 or qty - q < self._min_qty(sym):
                    q = qty                           # el resto sería polvo: cierra todo
                qty = q
            try:
                exit_px = price if price else self._last(sym)
                if self.live:
                    self._cancel_stop(pos)
                    o = self.ex.create_order(sym, "market", self._order_side(pos["side"], True),
                                             qty, None, self._pp(pos["side"], True))
                    exit_px = self._fill_price(o, sym, exit_px)
            except Exception as e:
                self.notify(f"⚠️ Error cerrando {sym}: {e}")
                if self.live and not pos.get("sl_id"):
                    self.set_stop(pos, pos["stop"])   # re-proteger
                return False
            self._book(pos, qty, exit_px, reason)
            return True

    def close_all(self, reason: str = "Cierre manual") -> int:
        n = 0
        for sym in list(self.positions):
            n += int(self.close(sym, reason))
        return n

    # ───────────── gestión periódica ─────────────
    def manage(self):
        with self.lock:
            if not self.positions:
                return
            now = time.time()
            if self.live and now - self.last_recon >= 30:
                self.reconcile()
                self.last_recon = now
            syms = list(self.positions)
            if not syms:
                return
            prices = self._prices(syms)
            for sym in syms:
                pos, price = self.positions.get(sym), prices.get(sym)
                if not pos or not price:
                    continue
                long = pos["side"] == "long"
                d = 1 if long else -1
                if (long and price <= pos["stop"]) or ((not long) and price >= pos["stop"]):
                    self.close(sym, "Stop break-even/trailing" if pos["tp1_hit"] else "Stop loss", price=price)
                    continue
                r = (price - pos["entry"]) * d / pos["risk"]
                pos["max_r"] = max(pos.get("max_r", 0.0), r)
                if not pos["tp1_hit"] and r >= self.cfg.tp1_r:
                    # Antes se marcaba tp1_hit ANTES de cerrar: si la orden fallaba, el parcial nunca se reintentaba.
                    if now - pos.get("tp1_try", 0) >= 60:
                        pos["tp1_try"] = now
                        if self.close(sym, f"TP1 {self.cfg.tp1_r}R", fraction=self.cfg.partial_pct / 100, price=price):
                            pos = self.positions.get(sym)
                            if pos:
                                pos["tp1_hit"] = True
                                be = pos["entry"] * (1 + d * self.cfg.fee_pct * 2 / 100)
                                if (long and pos["stop"] < be) or ((not long) and pos["stop"] > be):
                                    self.set_stop(pos, be, "break-even tras TP1")
                    continue
                if self.cfg.be_r > 0 and not pos.get("be_done") and not pos["tp1_hit"] and r >= self.cfg.be_r:
                    pos["be_done"] = True              # a partir de aquí la operación ya no puede perder capital
                    be = pos["entry"] * (1 + d * self.cfg.fee_pct * 2 / 100)
                    self.set_stop(pos, be, f"break-even a +{self.cfg.be_r:g}R (sin riesgo)")
                if now - pos["opened"] > self.cfg.max_hold_h * 3600:
                    self.close(sym, f"Tiempo máximo ({self.cfg.max_hold_h:.0f}h)", price=price)
            if now - self.last_trail >= self.cfg.trail_interval:
                self.trail()
                self.last_trail = now
            self.st.save()

    def trail(self):
        for sym, pos in list(self.positions.items()):
            if not pos["tp1_hit"]:
                continue
            try:
                df = to_df(self.ex.fetch_ohlcv(sym, self.cfg.trail_tf, limit=60)).iloc[:-1]
                e = float(ema(df["close"], self.cfg.trail_ema).iloc[-1])
                price = self._last(sym)
            except Exception as err:
                log.debug("trail %s: %s", sym, err)
                continue
            if pos["side"] == "long" and pos["stop"] < e < price:
                self.set_stop(pos, e, f"trailing EMA{self.cfg.trail_ema} {self.cfg.trail_tf}")
            elif pos["side"] == "short" and price < e < pos["stop"]:
                self.set_stop(pos, e, f"trailing EMA{self.cfg.trail_ema} {self.cfg.trail_tf}")

    def reconcile(self):
        """Live: detecta posiciones cerradas por el stop del exchange."""
        try:
            ps = self.ex.fetch_positions(list(self.positions))
        except Exception as e:
            log.debug("reconcile: %s", e)
            return
        open_keys = {(p.get("symbol"), p.get("side")) for p in ps
                     if abs(float(p.get("contracts") or 0)) > 0}
        for sym, pos in list(self.positions.items()):
            if (sym, pos["side"]) in open_keys:
                pos["miss"] = 0
                continue
            pos["miss"] = pos.get("miss", 0) + 1
            if pos["miss"] >= 2:                      # 2 lecturas seguidas para evitar falsos positivos
                self._book(pos, pos["qty"], pos["stop"], "Cerrada en exchange (stop)")

    # ───────────── informes ─────────────
    def summary(self) -> str:
        d = self.st.data["day"]
        try:
            eq = self.equity()
        except Exception:
            eq = float("nan")
        return (
            f"<b>Estado</b> [{self.cfg.mode.upper()}{' · DEMO' if self.cfg.demo else ''}]"
            f"{' ⏸ EN PAUSA' if self.st.data['paused'] else ''}\n"
            f"Equity: ${eq:,.2f} | Posiciones: {len(self.positions)}/{self.cfg.max_open}\n"
            f"Hoy: {d['trades']} ops | PnL realizado ${d['realized']:+.2f}"
        )

    def positions_text(self) -> str:
        if not self.positions:
            return "Sin posiciones abiertas."
        prices = self._prices(list(self.positions))
        lines = []
        for s, p in self.positions.items():
            price = prices.get(s)
            dsg = 1 if p["side"] == "long" else -1
            r = (price - p["entry"]) * dsg / p["risk"] if price else float("nan")
            lines.append(f"• <b>{s}</b> {p['side'].upper()} {p['setup']} | entrada {px(p['entry'])} | "
                         f"SL {px(p['stop'])} | {r:+.2f}R{' | TP1 ✔' if p['tp1_hit'] else ''}")
        return "\n".join(lines)
