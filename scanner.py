"""Escanea todos los perpetuos USDT de BingX: señales (ruptura confirmada) y candidatas (pre-ruptura)."""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field

import ccxt

from ict import ict_signals
from indicators import rvol, to_df
from strategies import (adjust_candidate, adjust_score, btc_regime, daily_ctx, early_break, evaluate,
                        trend_gate, watch_candidates, why_no_signal, why_no_trend)

BTC = "BTC/USDT:USDT"
DAILY_TTL = 1200     # s: la vela diaria cambia despacio, no hace falta pedirla en cada escaneo
H4_TTL = 1800        # s: velas 4h (estructura) cambian muy despacio
HOURLY_TTL = 600     # s: velas 1h cerradas para candidatas (para señales se piden frescas)
RETRY_WAITS = (2.0, 5.0, 10.0)   # s de espera creciente si BingX limita las peticiones
log = logging.getLogger("scanner")

# Claves del embudo (el resto de claves del Counter son motivos de descarte)
FUNNEL_KEYS = ("analizados", "filtro diario ok", "con volumen ahora", "cajas de consolidación válidas",
               "errores de datos", "peticiones reintentadas por límite de BingX",
               "ICT: con volumen", "ICT: sesgo 4h", "ICT: POI", "ICT: barrido en POI", "ICT: CISD 15m",
               "ICT: señales", "ICT: errores de datos")


def _transient(e: Exception) -> bool:
    """Errores temporales: límite de peticiones de BingX (code 100410), red, timeouts."""
    msg = str(e)
    return "100410" in msg or "请稍后重试" in msg or isinstance(e, ccxt.NetworkError)


@dataclass
class ScanResult:
    signals: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    movers: list = field(default_factory=list)       # (símbolo, rvol15m, %24h, motivo/estado)
    far: list = field(default_factory=list)         # cajas válidas aún lejos: (símbolo, lado, dist%, gatillo)
    regime: str = "neutral"
    stats: Counter = field(default_factory=Counter)
    errors: list = field(default_factory=list)
    universe: int = 0
    secs: float = 0.0
    ts: float = field(default_factory=time.time)

    def reasons(self, top: int = 5):
        return [(k, v) for k, v in self.stats.most_common() if k not in FUNNEL_KEYS][:top]


class Scanner:
    def __init__(self, ex, cfg):
        self.ex = ex
        self.cfg = cfg
        self.exclude = re.compile(cfg.exclude_regex) if cfg.exclude_regex else None
        self._cache = {}
        self.retries = 0
        self._sleep = time.sleep

    def _call(self, fn, *a, **k):
        """Llama al exchange reintentando con espera creciente si el error es temporal."""
        for i in range(len(RETRY_WAITS) + 1):
            try:
                return fn(*a, **k)
            except Exception as e:
                if i == len(RETRY_WAITS) or not _transient(e):
                    raise
                self.retries += 1
                self._sleep(RETRY_WAITS[i])

    def liquid_symbols(self):
        tickers = self._call(self.ex.fetch_tickers)
        out = []
        for sym, t in tickers.items():
            mk = self.ex.markets.get(sym)
            if not mk or not (mk.get("swap") and mk.get("linear") and mk.get("quote") == "USDT"):
                continue
            if mk.get("active") is False:
                continue
            if self.exclude and self.exclude.search(str(mk.get("id", "")).replace("-", "")):
                continue
            qv = float(t.get("quoteVolume") or 0)
            if qv < self.cfg.min_volume_usd:
                continue
            out.append((sym, qv, float(t.get("percentage") or 0)))
        out.sort(key=lambda x: x[1], reverse=True)
        return out[: self.cfg.max_symbols]

    def _df(self, sym, tf, limit):
        return to_df(self._call(self.ex.fetch_ohlcv, sym, tf, limit=limit))

    def _get(self, sym, tf, limit, ttl, fresh=False, drop_last=False):
        """Velas con caché corta. fresh=True fuerza descarga (símbolos con señal potencial)."""
        key, now = (sym, tf), time.time()
        hit = self._cache.get(key)
        if hit and not fresh and now - hit[0] < ttl:
            return hit[1]
        df = self._df(sym, tf, limit)
        if drop_last:
            df = df.iloc[:-1]
        self._cache[key] = (now, df)
        return df

    def early_signals(self, cands) -> list:
        """Revisión rápida: ¿alguna candidata acaba de romper el gatillo con una vela 5m cerrada?"""
        if not cands:
            return []
        tickers = self._call(self.ex.fetch_tickers)           # 1 petición para todas las candidatas
        out = []
        for c in cands:
            p = float((tickers.get(c.symbol) or {}).get("last") or 0)
            if p <= 0 or not ((p > c.trigger) if c.side == "long" else (p < c.trigger)):
                continue                                       # el precio vivo aún no pasó el gatillo
            try:
                m5 = self._df(c.symbol, "5m", 60).iloc[:-1]    # descarta la 5m en curso
            except Exception as e:
                log.debug("early %s: %s", c.symbol, e)
                continue
            s = early_break(c, m5, self.cfg)
            if s:
                out.append(s)
        return out

    def funding(self, sym):
        try:
            return float(self.ex.fetch_funding_rate(sym).get("fundingRate"))
        except Exception:
            return None

    def scan(self) -> ScanResult:
        t0, cfg = time.time(), self.cfg
        self.retries = 0
        res = ScanResult()
        self._cache = {k: v for k, v in self._cache.items() if t0 - v[0] < 3600}
        universe = self.liquid_symbols()
        res.universe = len(universe)
        try:
            res.regime = btc_regime(self._df(BTC, "1h", 120).iloc[:-1])
        except Exception as e:
            log.warning("No se pudo calcular el régimen BTC: %s", e)

        sides = [s for s, on in (("long", cfg.enable_longs), ("short", cfg.enable_shorts)) if on]
        st = res.stats
        for sym, _qv, pct in universe:
            try:
                m = self._df(sym, "15m", 130)
                if len(m) < 110:
                    st["historial 15m insuficiente"] += 1
                    continue
                m = m.iloc[:-1]                      # descarta la vela en curso
                st["analizados"] += 1
                rv = rvol(m, 20)
                hot = rv >= cfg.prefilter_rvol or abs(pct) >= cfg.prefilter_move24

                # Sistema ICT: el VOLUMEN es la puerta de entrada (solo se piden 4h/1h si la 15m trae volumen).
                # Va en su propio try: un fallo aquí no debe tapar la detección de rupturas de esta moneda.
                if cfg.ict_enable and max(rv, rvol(m.iloc[:-1], 20)) >= cfg.ict_rvol:
                    st["ICT: con volumen"] += 1
                    try:
                        h4 = self._get(sym, "4h", 120, H4_TTL, drop_last=True)
                        h1i = self._get(sym, "1h", 120, HOURLY_TTL, fresh=True, drop_last=True)
                        for s in ict_signals(sym, h4, h1i, m, cfg, st):
                            adjust_score(s, res.regime, self.funding(sym))
                            res.signals.append(s)
                    except Exception as e:
                        st["ICT: errores de datos"] += 1
                        if len(res.errors) < 3:
                            res.errors.append(f"ICT {sym}: {type(e).__name__}: {e}"[:160])

                d = self._get(sym, "1d", 60, DAILY_TTL, fresh=hot)
                if len(d) < 30:
                    st["historial diario insuficiente"] += 1
                    continue
                ctx = daily_ctx(d)
                priors = {}
                for side in sides:
                    p = trend_gate(side, ctx, cfg)
                    if p is not None:
                        priors[side] = p
                if priors:
                    st["filtro diario ok"] += 1
                else:
                    st["no pasa filtro diario: " + why_no_trend(ctx, cfg)] += 1
                if not hot and not priors:
                    continue

                h = self._get(sym, "1h", 120, HOURLY_TTL, fresh=hot, drop_last=True)
                if hot:                               # algo se mueve AHORA: busca ruptura confirmada
                    st["con volumen ahora"] += 1
                    sigs = evaluate(sym, d, h, m, cfg, st)
                    for s in sigs:
                        adjust_score(s, res.regime, self.funding(sym))
                        res.signals.append(s)
                    res.movers.append((sym, rv, pct, "SEÑAL" if sigs else why_no_signal(ctx, priors, h, m, cfg)))
                if priors:                            # consolidando cerca del gatillo: candidata
                    for c in watch_candidates(sym, priors, ctx, h, m, cfg, st, res.far):
                        res.candidates.append(adjust_candidate(c, res.regime))
            except Exception as e:                    # un símbolo roto no debe parar el escaneo...
                st["errores de datos"] += 1           # ...pero ya NO se silencia
                if len(res.errors) < 3:
                    res.errors.append(f"{sym}: {type(e).__name__}: {e}"[:160])

        res.signals.sort(key=lambda s: s.score, reverse=True)
        res.candidates.sort(key=lambda c: c.score, reverse=True)
        res.movers.sort(key=lambda x: x[1], reverse=True)
        res.far.sort(key=lambda x: x[2])
        if self.retries:
            st["peticiones reintentadas por límite de BingX"] = self.retries
        res.secs = time.time() - t0
        log.info("Escaneo %.0fs: %d líquidos, %d analizados, %d filtro diario, %d cajas válidas, "
                 "%d candidatas, %d señales | BTC %s", res.secs, res.universe, st["analizados"],
                 st["filtro diario ok"], st["cajas de consolidación válidas"], len(res.candidates),
                 len(res.signals), res.regime)
        if st["errores de datos"]:
            log.warning("Errores de datos en %d símbolos. Ejemplos: %s",
                        st["errores de datos"], " || ".join(res.errors))
        return res
