"""
Setups inspirados en Qullamaggie, adaptados a cripto intradía:

  1. Breakout / Breakdown : ruptura de una consolidación tensa (higher lows / lower highs,
                            rango contraído) en un activo con ADR alto y tendencia previa.
  2. Episodic Pivot       : vela explosiva con volumen anómalo que rompe el máximo/mínimo de 24h
                            en un activo que NO venía ya extendido.
  3. Parabolic Short      : subida vertical (+40% en 5d) muy extendida sobre la EMA20 diaria;
                            se entra corto al perder el VWAP de la sesión.

Todas las funciones trabajan con velas CERRADAS (15m y 1h). Diario incluye la vela de hoy.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from indicators import adr_pct, atr, ema, rvol, session_vwap


@dataclass
class Signal:
    symbol: str
    side: str            # "long" | "short"
    setup: str
    entry: float
    stop: float
    score: float
    reasons: list = field(default_factory=list)
    atr1h: float = 0.0
    early: bool = False  # entrada anticipada (cierre 5m), riesgo reducido

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)


def _stop(side: str, entry: float, raw: float, atr1h: float, cfg):
    """Stop técnico acotado por ATR: si queda muy lejos, el setup se descarta."""
    dist = max(abs(entry - raw), cfg.min_stop_atr * atr1h)
    if dist > cfg.max_stop_atr * atr1h or dist / entry * 100 > cfg.max_stop_pct:
        return None
    return entry - dist if side == "long" else entry + dist


def daily_ctx(d):
    c = d["close"]
    e10, e20 = ema(c, 10), ema(c, 20)
    hi30, lo30 = float(d["high"].tail(30).max()), float(d["low"].tail(30).min())
    return {
        # posición en el rango de 30d: 1 = en máximos, 0 = en mínimos
        "pos30": float((c.iloc[-1] - lo30) / (hi30 - lo30)) if hi30 > lo30 else 0.5,
        "close": float(c.iloc[-1]),
        "e10": float(e10.iloc[-1]),
        "e20": float(e20.iloc[-1]),
        "adr": adr_pct(d, 20),
        "atr": float(atr(d, 14).iloc[-1]),
        "runup30": float((c.iloc[-1] / d["low"].tail(30).min() - 1) * 100),
        "drop30": float((1 - c.iloc[-1] / d["high"].tail(30).max()) * 100),
        "gain5": float((c.iloc[-1] / c.iloc[-6] - 1) * 100),
    }


def _bump(stats, key: str):
    """Cuenta motivos de descarte (stats es un collections.Counter o None)."""
    if stats is not None:
        stats[key] += 1


@dataclass
class Candidate:
    """Moneda en consolidación tensa, cerca del gatillo de ruptura (aún SIN señal)."""
    symbol: str
    side: str            # "long" | "short"
    setup: str           # "Breakout" | "Breakdown"
    trigger: float       # nivel que debe romper el CIERRE de una vela 15m
    stop: float
    price: float
    dist_pct: float
    adr: float
    rng: float
    prior: float
    rvol: float
    score: float
    near: bool = False          # a punto de romper (muy cerca del gatillo)
    regime_note: str = ""
    raw_stop: float = 0.0       # extremo opuesto de la caja (base del stop)
    atr1h: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.side}"

    @property
    def risk(self) -> float:
        return abs(self.trigger - self.stop)


def trend_gate(side: str, ctx, cfg):
    """Filtro diario (ADR alto + tendencia + movimiento previo). Devuelve el movimiento previo (%) o None."""
    if ctx["adr"] < cfg.adr_min:
        return None
    # Qullamaggie: cerca de máximos (long) / mínimos (short) tras un gran movimiento. El run-up se mide
    # desde el mínimo de 30d, así que por sí solo lo pasa casi cualquier alt volátil (76 de 81 monedas en
    # tu captura): pos30 exige además que el precio conserve la mayor parte del movimiento.
    if side == "long":
        ok = (ctx["close"] > ctx["e20"] and ctx["e10"] > ctx["e20"] and ctx["runup30"] >= cfg.runup_min
              and ctx["pos30"] >= 1 - cfg.range_pos_max)
        return ctx["runup30"] if ok else None
    ok = (ctx["close"] < ctx["e20"] and ctx["e10"] < ctx["e20"] and ctx["drop30"] >= cfg.runup_min
          and ctx["pos30"] <= cfg.range_pos_max)
    return ctx["drop30"] if ok else None


def why_no_trend(ctx, cfg) -> str:
    """Motivo (único) por el que una moneda no pasa el filtro diario; sirve para el resumen."""
    if ctx["adr"] < cfg.adr_min:
        return "ADR diario bajo"
    if ctx["close"] > ctx["e20"] and ctx["e10"] > ctx["e20"]:
        if ctx["runup30"] < cfg.runup_min:
            return "alcista pero sin subida 30d suficiente"
        return "alcista pero lejos de máximos 30d"
    if ctx["close"] < ctx["e20"] and ctx["e10"] < ctx["e20"]:
        if ctx["drop30"] < cfg.runup_min:
            return "bajista pero sin caída 30d suficiente"
        return "bajista pero lejos de mínimos 30d"
    return "sin tendencia diaria clara (EMA10/20)"


def range_trend(h, bars: int):
    """Rango medio 1h de la 2.ª mitad de la ventana / el de la 1.ª. >1 = la volatilidad se EXPANDE; <1 = se contrae."""
    w = h.tail(bars)
    half = len(w) // 2
    if half < 2:
        return None
    tr = w["high"] - w["low"]
    first = float(tr.iloc[:half].mean())
    return float(tr.iloc[half:].mean()) / first if first > 0 else None


def box(side: str, ctx, h, cfg, stats=None):
    """Consolidación tensa en 1h: rango contraído + higher lows (long) / lower highs (short)."""
    long = side == "long"
    w = h.tail(cfg.consol_bars)
    hi, lo = float(w["high"].max()), float(w["low"].min())
    rng = (hi / lo - 1) * 100
    max_rng = cfg.consol_range_adr * ctx["adr"]
    if rng > max_rng:
        _bump(stats, "caja 1h demasiado ancha")
        return None
    half = len(w) // 2
    tr = w["high"] - w["low"]
    if not tr.iloc[half:].mean() < tr.iloc[:half].mean() * cfg.consol_contract:    # contracción de rango
        _bump(stats, "caja sin contracción de rango")
        return None
    if long and not w["low"].iloc[half:].min() >= w["low"].iloc[:half].min():      # higher lows
        _bump(stats, "caja sin higher lows")
        return None
    if not long and not w["high"].iloc[half:].max() <= w["high"].iloc[:half].max():  # lower highs
        _bump(stats, "caja sin lower highs")
        return None
    return {"hi": hi, "lo": lo, "rng": rng, "max_rng": max_rng}


def _bars_beyond(m, level: float, long: bool, max_scan: int = 8) -> int:
    """Nº de velas 15m cerradas CONSECUTIVAS (terminando en la última) que cierran más allá del nivel."""
    n = 0
    for c in m["close"].iloc[::-1].head(max_scan):
        if (c > level) if long else (c < level):
            n += 1
        else:
            break
    return n


def _consolidation_break(sym, side, ctx, h, m, a1h, cfg, stats=None):
    long = side == "long"
    prior = trend_gate(side, ctx, cfg)
    if prior is None:
        return None
    b = box(side, ctx, h, cfg)
    if b is None:
        return None
    hi, lo, rng, max_rng = b["hi"], b["lo"], b["rng"], b["max_rng"]

    last, prev = m.iloc[-1], m.iloc[-2]
    rv = rvol(m, 20)
    if rv < cfg.rvol_min:
        _bump(stats, "con caja válida: RVOL 15m bajo")
        return None
    level = hi if long else lo
    n_out = _bars_beyond(m, level, long)
    green = last["close"] > last["open"] if long else last["close"] < last["open"]
    if not (1 <= n_out <= max(1, cfg.break_fresh_bars) and green):
        _bump(stats, "con caja válida: sin ruptura confirmada")
        return None
    chase = (last["close"] / hi - 1) * 100 if long else (lo / last["close"] - 1) * 100
    if chase > cfg.max_chase_pct:
        _bump(stats, "con caja válida: ruptura ya extendida")
        return None

    entry = float(last["close"])
    stop = _stop(side, entry, lo if long else hi, a1h, cfg)
    if stop is None:
        _bump(stats, "con caja válida: stop fuera de rango")
        return None

    score = (
        55
        + min(15, (rv - cfg.rvol_min) * 5)
        + min(10, (ctx["adr"] - cfg.adr_min) * 2)
        + min(10, max(0.0, (prior - cfg.runup_min) / 3))
        + max(0.0, (1 - rng / max_rng) * 10)
    )
    name = "Breakout" if long else "Breakdown"
    reasons = [
        f"ADR {ctx['adr']:.1f}%",
        f"{'Subida' if long else 'Caída'} 30d {prior:.0f}%",
        f"Consolidación 1h {rng:.2f}% (contraída)",
        f"Ruptura 15m con RVOL {rv:.1f}x" + ("" if n_out == 1 else f" (vela nº {n_out} sobre el nivel)"),
    ]
    return Signal(sym, side, name, entry, stop, min(100.0, score), reasons, a1h)


def watch_candidates(sym, priors: dict, ctx, h, m, cfg, stats=None, far=None) -> list:
    """
    Candidatas a explotar: pasan el filtro diario, tienen la caja de consolidación válida y el
    precio está cerca del gatillo (<= max(WATCH_DIST_PCT, WATCH_DIST_ADR x ADR)) sin haberlo roto aún.
    `far` (lista opcional) recoge las cajas válidas que aún están lejos: (símbolo, lado, dist%, gatillo).
    priors = {side: movimiento_previo%} de los lados que ya pasaron trend_gate.
    """
    if len(h) < 60 or len(m) < 30:
        return []
    a1h = float(atr(h, 14).iloc[-1])
    if a1h <= 0:
        return []
    price = float(m.iloc[-1]["close"])
    rv = rvol(m, 20)
    limit = max(cfg.watch_dist_pct, cfg.watch_dist_adr * ctx["adr"])
    out = []
    for side, prior in priors.items():
        long = side == "long"
        b = box(side, ctx, h, cfg, stats)
        if b is None:
            continue
        _bump(stats, "cajas de consolidación válidas")
        trigger = b["hi"] if long else b["lo"]
        dist = (trigger / price - 1) * 100 if long else (price / trigger - 1) * 100
        if dist <= 0:
            _bump(stats, "caja válida ya rota")
            continue
        if dist > limit:
            _bump(stats, "caja válida pero lejos del gatillo")
            if far is not None:
                far.append((sym, side, dist, trigger))
            continue
        stop = _stop(side, trigger, b["lo"] if long else b["hi"], a1h, cfg)
        if stop is None:
            _bump(stats, "caja válida con stop fuera de rango")
            continue
        score = (
            40
            + max(0.0, 1 - b["rng"] / b["max_rng"]) * 20
            + min(15, max(0.0, (ctx["adr"] - cfg.adr_min) * 2))
            + min(10, max(0.0, (prior - cfg.runup_min) / 3))
            + max(0.0, 1 - dist / limit) * 15
        )
        near = dist <= min(limit, max(cfg.watch_near_pct, cfg.watch_near_adr * ctx["adr"]))
        out.append(Candidate(sym, side, "Breakout" if long else "Breakdown", trigger, stop, price,
                             dist, ctx["adr"], b["rng"], prior, rv, min(100.0, score), near,
                             raw_stop=b["lo"] if long else b["hi"], atr1h=a1h))
    return out


def early_break(c: Candidate, m5, cfg):
    """
    Ruptura ANTICIPADA: una vela de 5m CERRADA ya cruzó el gatillo de una candidata (verde/roja, con
    volumen), sin esperar al cierre de la 15m. Se acepta la 1.ª o 2.ª vela 5m fuera de la caja.
    `m5` = velas 5m cerradas. Devuelve Signal(early=True) o None.
    """
    if len(m5) < 30 or c.atr1h <= 0 or c.raw_stop <= 0:
        return None
    long = c.side == "long"
    last = m5.iloc[-1]
    n_out = _bars_beyond(m5, c.trigger, long)
    green = last["close"] > last["open"] if long else last["close"] < last["open"]
    if not (1 <= n_out <= 2 and green):
        return None
    rv = rvol(m5, 20)
    if rv < cfg.early_rvol:
        return None
    chase = (last["close"] / c.trigger - 1) * 100 if long else (c.trigger / last["close"] - 1) * 100
    if chase > cfg.max_chase_pct:
        return None
    entry = float(last["close"])
    stop = _stop(c.side, entry, c.raw_stop, c.atr1h, cfg)
    if stop is None:
        return None
    max_rng = cfg.consol_range_adr * c.adr
    score = (
        50                                               # 5 pts menos que la ruptura confirmada en 15m
        + min(15, (rv - cfg.early_rvol) * 5)
        + min(10, (c.adr - cfg.adr_min) * 2)
        + min(10, max(0.0, (c.prior - cfg.runup_min) / 3))
        + max(0.0, (1 - c.rng / max_rng) * 10)
    )
    reasons = [
        f"ADR {c.adr:.1f}%",
        f"{'Subida' if long else 'Caída'} 30d {c.prior:.0f}%",
        f"Consolidación 1h {c.rng:.2f}% (contraída)",
        f"ANTICIPADA: cierre 5m más allá de {c.trigger:.8g} con RVOL {rv:.1f}x",
        "Aún sin cierre 15m: entra con riesgo reducido",
    ]
    return Signal(c.symbol, c.side, f"{c.setup} (anticipada)", entry, stop, min(100.0, score), reasons,
                  c.atr1h, early=True)


def adjust_candidate(c: Candidate, regime: str) -> Candidate:
    """Misma lógica de régimen BTC que las señales, para que la lista de candidatas no engañe."""
    aligned = (c.side == "long" and regime == "bull") or (c.side == "short" and regime == "bear")
    counter = (c.side == "long" and regime == "bear") or (c.side == "short" and regime == "bull")
    if aligned:
        c.score = min(100.0, c.score + 8)
        c.regime_note = f"a favor de BTC ({regime})"
    elif counter:
        c.score = max(0.0, c.score - 12)
        c.regime_note = f"⚠️ contra BTC ({regime})"
    return c


def why_no_signal(ctx, priors: dict, h, m, cfg) -> str:
    """Por qué una moneda CON volumen no genera señal ahora (para el resumen /scan)."""
    if not priors:
        return "filtro diario: " + why_no_trend(ctx, cfg)
    side = next(iter(priors))
    local = Counter()
    b = box(side, ctx, h, cfg, local)
    if b is None:
        return next(iter(local), "sin caja")
    long = side == "long"
    level = b["hi"] if long else b["lo"]
    price = float(m.iloc[-1]["close"])
    dist = (level / price - 1) * 100 if long else (price / level - 1) * 100
    if dist > 0:
        return f"caja OK, aún a {dist:.1f}% del gatillo"
    n_out = _bars_beyond(m, level, long)
    if n_out > cfg.break_fresh_bars or -dist > cfg.max_chase_pct:
        return "ruptura ya extendida (no perseguir)"
    return "caja rota pero sin vela verde/RVOL suficiente"


def _episodic(sym, side, ctx, m, a1h, cfg):
    long = side == "long"
    last = m.iloc[-1]
    rng = float(last["high"] - last["low"])
    if rng <= 0:
        return None
    move = (last["close"] / last["open"] - 1) * 100
    rv = rvol(m, 20)
    if rv < cfg.ep_rvol:
        return None
    window = m.iloc[-97:-1]
    if long:
        if not (move >= cfg.ep_move_15m and (last["close"] - last["low"]) / rng >= 0.75
                and last["close"] > window["high"].max() and ctx["runup30"] < cfg.ep_max_prior):
            return None
        raw = float(last["low"])
    else:
        if not (move <= -cfg.ep_move_15m and (last["high"] - last["close"]) / rng >= 0.75
                and last["close"] < window["low"].min() and ctx["drop30"] < cfg.ep_max_prior):
            return None
        raw = float(last["high"])
    entry = float(last["close"])
    stop = _stop(side, entry, raw, a1h, cfg)
    if stop is None:
        return None
    score = 60 + min(20, (rv - cfg.ep_rvol) * 3) + min(10, (abs(move) - cfg.ep_move_15m) * 3)
    reasons = [
        f"Impulso 15m {move:+.1f}%",
        f"RVOL {rv:.1f}x",
        "Rompe máx/mín de 24h",
        "Activo no extendido previamente",
    ]
    return Signal(sym, side, "Episodic Pivot", entry, stop, min(100.0, score), reasons, a1h)


def _parabolic_short(sym, ctx, m, a1h, cfg):
    if ctx["gain5"] < cfg.para_gain5:
        return None
    ext = (ctx["close"] - ctx["e20"]) / ctx["atr"] if ctx["atr"] > 0 else 0.0
    if ext < cfg.para_ext_atr:
        return None
    vw = session_vwap(m)
    day = m["ts"] // 86_400_000
    today = m[day == day.iloc[-1]]
    if len(today) < 4:
        return None
    day_high = float(today["high"].max())
    last, prev = m.iloc[-1], m.iloc[-2]
    if not (prev["close"] >= vw.iloc[-2] and last["close"] < vw.iloc[-1] and last["close"] < last["open"]):
        return None
    if (day_high / vw.iloc[-1] - 1) * 100 < 2.0:
        return None
    entry = float(last["close"])
    stop = _stop("short", entry, day_high * 1.001, a1h, cfg)
    if stop is None:
        return None
    rv = rvol(m, 20)
    score = (
        58
        + min(15, (ctx["gain5"] - cfg.para_gain5) / 4)
        + min(15, (ext - cfg.para_ext_atr) * 3)
        + (5 if rv >= 1.5 else 0)
    )
    reasons = [
        f"Subida 5d {ctx['gain5']:.0f}%",
        f"{ext:.1f} ATR sobre EMA20 diaria",
        "Pierde VWAP de la sesión",
        f"Máx del día a {(day_high / entry - 1) * 100:.1f}% sobre entrada",
    ]
    return Signal(sym, "short", "Parabolic Short", entry, stop, min(100.0, score), reasons, a1h)


def evaluate(sym, d, h, m, cfg, stats=None) -> list:
    """d: diario (incluye hoy) | h: 1h cerradas | m: 15m cerradas."""
    if len(d) < 30 or len(h) < 60 or len(m) < 110:
        return []
    ctx = daily_ctx(d)
    a1h = float(atr(h, 14).iloc[-1])
    if a1h <= 0:
        return []
    out = []
    if cfg.enable_longs:
        out.append(_consolidation_break(sym, "long", ctx, h, m, a1h, cfg, stats))
        out.append(_episodic(sym, "long", ctx, m, a1h, cfg))
    if cfg.enable_shorts:
        out.append(_consolidation_break(sym, "short", ctx, h, m, a1h, cfg, stats))
        out.append(_episodic(sym, "short", ctx, m, a1h, cfg))
        out.append(_parabolic_short(sym, ctx, m, a1h, cfg))
    return [s for s in out if s]


def btc_regime(h) -> str:
    c = h["close"]
    e20, e50 = float(ema(c, 20).iloc[-1]), float(ema(c, 50).iloc[-1])
    last = float(c.iloc[-1])
    if last > e50 and e20 > e50:
        return "bull"
    if last < e50 and e20 < e50:
        return "bear"
    return "neutral"


def adjust_score(sig: Signal, regime: str, funding):
    """Ideas extra: filtro de régimen BTC y de funding (crowding)."""
    s = sig.score
    aligned = (sig.side == "long" and regime == "bull") or (sig.side == "short" and regime == "bear")
    counter = (sig.side == "long" and regime == "bear") or (sig.side == "short" and regime == "bull")
    if aligned:
        s += 8
        sig.reasons.append(f"A favor del régimen BTC ({regime})")
    elif counter and sig.setup != "Parabolic Short":
        s -= 12
        sig.reasons.append(f"Contra el régimen BTC ({regime})")
    if funding is not None:
        if sig.side == "long" and funding > 0.0008:
            s -= 6
            sig.reasons.append(f"Funding alto ({funding * 100:.3f}%): largos saturados")
        elif sig.side == "short" and funding < -0.0008:
            s -= 6
            sig.reasons.append(f"Funding muy negativo ({funding * 100:.3f}%): cortos saturados")
        elif sig.side == "long" and funding < -0.0005:
            s += 4
            sig.reasons.append("Funding negativo: combustible para squeeze")
    sig.score = max(0.0, min(100.0, s))
    return sig
