"""
Sistema ICT:  4h estructura  ->  1h barrido de liquidez DENTRO de un POI  ->  15m CISD, con VOLUMEN como confirmación.

  4h  : sesgo por estructura (HH+HL = solo largos, LH+LL = solo cortos) y rango de referencia (premium/descuento).
  1h  : POI = Order Block o FVG sin mitigar (nacidos de una vela de desplazamiento). El barrido es una mecha que
        supera la liquidez previa (mín/máx de las últimas N velas), cierra de vuelta y toca el POI.
        La hora en curso se agrega desde las 15m para no esperar al cierre de la vela de 1h.
  15m : CISD = tras un tramo de velas en contra, una vela cierra más allá de la APERTURA de la primera vela del tramo.
  Volumen (puerta dura): RVOL de la vela CISD, volumen vs. el tramo en contra y fuerza del cierre.

Todo trabaja con velas CERRADAS. Devuelve Signal (de strategies.py) para reutilizar avisos y trader.
"""
from __future__ import annotations

import pandas as pd

from indicators import atr, rvol, vol_baseline
from strategies import Signal, _bump, _stop

H1 = 3_600_000
M15 = 900_000


# ───────────── 4h: estructura ─────────────
def swings(df, n: int = 2):
    """Pivotes fractales confirmados. Devuelve (altos, bajos) como listas de (índice, precio)."""
    hi, lo = df["high"].to_numpy(float), df["low"].to_numpy(float)
    sh, sl = [], []
    for i in range(n, len(df) - n):
        if hi[i] > hi[i - n:i].max() and hi[i] >= hi[i + 1:i + n + 1].max():
            sh.append((i, float(hi[i])))
        if lo[i] < lo[i - n:i].min() and lo[i] <= lo[i + 1:i + n + 1].min():
            sl.append((i, float(lo[i])))
    return sh, sl


def structure_bias(h4, n: int = 2):
    """Sesgo 4h: 'long' (HH+HL), 'short' (LH+LL) o None (rango/estructura rota). Incluye el rango de referencia."""
    sh, sl = swings(h4, n)
    if len(sh) < 2 or len(sl) < 2:
        return None
    last_close = float(h4["close"].iloc[-1])
    if sh[-1][1] > sh[-2][1] and sl[-1][1] > sl[-2][1] and last_close > sl[-1][1]:
        bias = "long"
    elif sh[-1][1] < sh[-2][1] and sl[-1][1] < sl[-2][1] and last_close < sh[-1][1]:
        bias = "short"
    else:
        return None
    return {"bias": bias, "swing_hi": sh[-1][1], "swing_lo": sl[-1][1],
            "highs": [p for _, p in sh], "lows": [p for _, p in sl], "h4": h4}


def liquidity_target(st, side: str, entry: float):
    """Objetivo: la liquidez 4h más cercana en la dirección del trade (swing high por encima / swing low por debajo)."""
    if side == "long":
        above = [p for p in st["highs"] if p > entry]
        return min(above) if above else None
    below = [p for p in st["lows"] if p < entry]
    return max(below) if below else None


# ───────────── 1h: POI y barrido ─────────────
def with_partial_hour(h1, m):
    """Añade la vela 1h en curso agregando las 15m cerradas, para ver barridos sin esperar al cierre de la hora."""
    nxt = int(h1["ts"].iloc[-1]) + H1
    part = m[m["ts"] >= nxt]
    if part.empty:
        return h1
    row = {"ts": nxt, "open": float(part["open"].iloc[0]), "high": float(part["high"].max()),
           "low": float(part["low"].min()), "close": float(part["close"].iloc[-1]),
           "volume": float(part["volume"].sum())}
    return pd.concat([h1, pd.DataFrame([row])], ignore_index=True)


def find_pois(h, side: str, a1h: float, cfg) -> list:
    """Order Blocks y FVG sin mitigar (demanda para long, oferta para short) en las últimas ict_poi_bars velas 1h."""
    long = side == "long"
    o, hi, lo, c = (h[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(h)
    pois = []
    for i in range(max(2, n - cfg.ict_poi_bars), n):
        body = c[i] - o[i]
        if (body if long else -body) < cfg.ict_disp_atr * a1h:          # vela de desplazamiento
            continue
        ob = None
        for j in range(i - 1, max(i - 4, -1), -1):                      # última vela contraria antes del impulso
            if (c[j] < o[j]) if long else (c[j] > o[j]):
                ob = (float(lo[j]), float(hi[j]))
                break
        fvg = None
        if i + 1 < n:
            if long and lo[i + 1] > hi[i - 1]:
                fvg = (float(hi[i - 1]), float(lo[i + 1]))
            if not long and hi[i + 1] < lo[i - 1]:
                fvg = (float(hi[i + 1]), float(lo[i - 1]))
        for kind, z in (("OB", ob), ("FVG", fvg)):
            if z is None:
                continue
            after = c[i + 1:]                                            # mitigado = cierre más allá del extremo
            if (long and (after < z[0]).any()) or ((not long) and (after > z[1]).any()):
                continue
            pois.append({"kind": kind, "lo": z[0], "hi": z[1], "idx": i})
    return pois


def find_sweep(h, side: str, pois: list, cfg):
    """Barrido de liquidez en 1h DENTRO de un POI (el más reciente). None si no hay."""
    long = side == "long"
    hi, lo, c = (h[k].to_numpy(float) for k in ("high", "low", "close"))
    n, lb = len(h), cfg.ict_liq_bars
    for k in range(n - 1, max(n - 1 - cfg.ict_sweep_bars, lb - 1), -1):
        if long:
            liq = lo[k - lb:k].min()
            if not (lo[k] < liq and c[k] > liq):
                continue
        else:
            liq = hi[k - lb:k].max()
            if not (hi[k] > liq and c[k] < liq):
                continue
        for z in pois:
            if z["idx"] >= k:
                continue
            tol = 0.25 * (z["hi"] - z["lo"])
            if long and z["lo"] - tol <= lo[k] <= z["hi"] and c[k] >= z["lo"]:
                return {"k": k, "ts": int(h["ts"].iloc[k]), "liq": float(liq), "poi": z}
            if (not long) and z["lo"] <= hi[k] <= z["hi"] + tol and c[k] <= z["hi"]:
                return {"k": k, "ts": int(h["ts"].iloc[k]), "liq": float(liq), "poi": z}
    return None


# ───────────── 15m: CISD + volumen ─────────────
def find_cisd(m, side: str, ts_sweep: int, cfg):
    """
    CISD fresco (en la última vela 15m cerrada o en la anterior) tras el barrido. Devuelve nivel, extremo y métricas
    de volumen de la vela CISD, o None.
    """
    long = side == "long"
    mm = m.reset_index(drop=True)
    after = mm.index[mm["ts"] >= ts_sweep]
    if len(after) < 3 or len(mm) < 30:
        return None
    ik, last = int(after[0]), len(mm) - 1
    o, c, hi, lo, v = (mm[k].to_numpy(float) for k in ("open", "close", "high", "low", "volume"))
    for cidx in (last, last - 1):
        if cidx <= ik or cidx < 21 or int(mm["ts"].iloc[cidx]) - ts_sweep > cfg.ict_cisd_bars * M15:
            continue
        j = cidx - 1                                                     # tramo en contra inmediatamente anterior
        while j >= max(0, ik - 4) and ((c[j] < o[j]) if long else (c[j] > o[j])):
            j -= 1
        rs = j + 1
        if cidx - rs < max(1, cfg.ict_cisd_min_run):
            continue
        level = o[rs]                                                    # apertura de la 1.ª vela del tramo
        if not ((c[cidx] > level) if long else (c[cidx] < level)):
            continue
        if cidx != last and not ((c[last] > level) if long else (c[last] < level)):
            continue                                                     # el CISD ya se deshizo
        base, run_v = vol_baseline(v[cidx - 20:cidx]), v[rs:cidx].mean()
        rng = hi[cidx] - lo[cidx]
        sweep_rv = max((v[i] / vol_baseline(v[i - 20:i]) for i in range(max(20, ik), cidx)
                        if vol_baseline(v[i - 20:i]) > 0), default=0.0)
        entry = float(c[last])
        return {
            "level": float(level), "cidx": cidx, "n_run": cidx - rs, "entry": entry,
            "ext": float(lo[ik:cidx + 1].min()) if long else float(hi[ik:cidx + 1].max()),
            "rv": float(v[cidx] / base) if base > 0 else 0.0,
            "vs_run": float(v[cidx] / run_v) if run_v > 0 else 0.0,
            "strength": float(((c[cidx] - lo[cidx]) if long else (hi[cidx] - c[cidx])) / rng) if rng > 0 else 0.0,
            "sweep_rv": float(sweep_rv),
            "chase": ((entry / level - 1) if long else (level / entry - 1)) * 100,
        }
    return None


def pending_cisd(m, side: str, ts_sweep: int, min_run: int = 2):
    """Nivel que debe CERRAR la 15m para que haya CISD (tramo en contra más reciente desde el barrido)."""
    long = side == "long"
    mm = m.reset_index(drop=True)
    after = mm.index[mm["ts"] >= ts_sweep]
    if len(after) == 0:
        return None
    ik = int(after[0])
    o, c = mm["open"].to_numpy(float), mm["close"].to_numpy(float)
    against = (lambda i: c[i] < o[i]) if long else (lambda i: c[i] > o[i])
    p = len(mm) - 1
    while p >= ik and not against(p):                                    # última vela en contra
        p -= 1
    if p < ik:
        return None
    rs = p
    while rs - 1 >= max(0, ik - 4) and against(rs - 1):
        rs -= 1
    level = float(o[rs])
    if (c[-1] >= level) if long else (c[-1] <= level):                   # ya superado: no está "pendiente"
        return None
    if p - rs + 1 < min_run:                                             # aún no es un tramo válido: no se promete entrada
        return None
    return {"level": level, "n_run": p - rs + 1}


def _watch_item(sym, side, sw, pend, m, cfg, why):
    z = sw["poi"]
    since = m[m["ts"] >= sw["ts"]]
    ext = float(since["low"].min()) if side == "long" else float(since["high"].max())
    return {"symbol": sym, "side": side, "kind": z["kind"], "zlo": z["lo"], "zhi": z["hi"], "ext": ext,
            "level": pend["level"], "n_run": pend["n_run"], "rv": rvol(m, 20), "why": why,
            "key": f"ICT|{sym}|{side}"}


# ───────────── orquestación ─────────────
def ict_signals(sym, h4, h1, m, cfg, stats=None, watch=None) -> list:
    """h4/h1/m = velas CERRADAS. Devuelve [] o [Signal] (setup 'ICT Sweep+CISD').
    `watch` (lista opcional) recoge los barridos ya hechos que aún esperan el CISD / su volumen."""
    if len(h4) < 30 or len(h1) < 60 or len(m) < 60:
        return []
    st = structure_bias(h4, cfg.ict_swing_n)
    if st is None:
        return []
    side = st["bias"]
    if (side == "long" and not cfg.enable_longs) or (side == "short" and not cfg.enable_shorts):
        return []
    _bump(stats, "ICT: sesgo 4h")
    long = side == "long"
    a1h = float(atr(h1, 14).iloc[-1])
    if a1h <= 0:
        return []
    h = with_partial_hour(h1, m)
    pois = find_pois(h, side, a1h, cfg)
    if cfg.ict_discount:                                                 # largos en descuento, cortos en premium
        eq = (st["swing_hi"] + st["swing_lo"]) / 2
        pois = [z for z in pois if ((z["hi"] + z["lo"]) / 2 < eq) == long]
    if not pois:
        return []
    _bump(stats, "ICT: POI")
    sw = find_sweep(h, side, pois, cfg)
    if sw is None:
        return []
    _bump(stats, "ICT: barrido en POI")
    cs = find_cisd(m, side, sw["ts"], cfg)
    if cs is None:
        pend = pending_cisd(m, side, sw["ts"], cfg.ict_cisd_min_run)
        if watch is not None and pend:
            watch.append(_watch_item(sym, side, sw, pend, m, cfg, "falta el CISD 15m"))
        return []
    _bump(stats, "ICT: CISD 15m")

    # ── el VOLUMEN decide ──
    if cs["rv"] < cfg.ict_rvol or cs["vs_run"] < cfg.ict_vol_vs_run or cs["strength"] < cfg.ict_close_pos:
        _bump(stats, "ICT: CISD sin confirmación de volumen/cierre")
        if watch is not None:
            watch.append(_watch_item(sym, side, sw, {"level": cs["level"], "n_run": cs["n_run"]}, m, cfg,
                                     "CISD sin volumen suficiente"))
        return []
    if cs["chase"] > cfg.max_chase_pct:
        _bump(stats, "ICT: CISD ya extendido")
        return []
    entry = cs["entry"]
    stop = _stop(side, entry, cs["ext"] - 0.1 * a1h if long else cs["ext"] + 0.1 * a1h, a1h, cfg)
    if stop is None:
        _bump(stats, "ICT: stop fuera de rango")
        return []
    target = liquidity_target(st, side, entry)
    rr = abs(target - entry) / abs(entry - stop) if target else 0.0
    if rr < cfg.ict_min_rr:
        _bump(stats, f"ICT: objetivo 4h a menos de {cfg.ict_min_rr:g}R")
        return []

    z = sw["poi"]
    score = (55
             + min(15, max(0.0, (cs["rv"] - cfg.ict_rvol) * 5))
             + min(8, max(0.0, (cs["vs_run"] - 1) * 8))
             + min(5, max(0.0, (cs["sweep_rv"] - 1) * 5))
             + min(7, max(0.0, (rr - cfg.ict_min_rr) * 2)))
    reasons = [
        f"4h: estructura {'alcista (HH+HL)' if long else 'bajista (LH+LL)'}",
        f"1h: barrido de {'mínimos' if long else 'máximos'} en {z['kind']} {z['lo']:.8g}–{z['hi']:.8g} "
        f"(mecha a {cs['ext']:.8g})",
        f"15m: CISD sobre {cs['level']:.8g} (tramo de {cs['n_run']} velas)",
        f"Volumen: RVOL {cs['rv']:.1f}x · {cs['vs_run']:.1f}x el tramo en contra · cierre al {cs['strength'] * 100:.0f}% del rango",
        f"Objetivo liquidez 4h {target:.8g} ({rr:.1f}R)",
    ]
    _bump(stats, "ICT: señales")
    return [Signal(sym, side, "ICT Sweep+CISD", entry, stop, min(100.0, score), reasons, a1h)]
