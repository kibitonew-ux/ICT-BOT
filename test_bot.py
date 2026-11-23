import numpy as np
import pandas as pd
import pytest

from config import Config
from state import State
from strategies import Signal, adjust_score, evaluate
from trader import Trader

CFG = Config(mode="paper")
H = 3_600_000
M15 = 900_000
DAY = 86_400_000
T0 = 1_700_006_400_000  # múltiplo de 1 día UTC (00:00)


def mk(ts, o, h, l, c, v):
    return pd.DataFrame({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v})


def daily_trend(n=60, g=1.008, rng=0.03):
    c = 1.0 * g ** np.arange(n)
    return mk(T0 + np.arange(n) * DAY, c, c * (1 + rng), c * (1 - rng), c, np.full(n, 1e6))


def hourly(n_pre=108, base=1.6, rng=0.01, consol=True):
    ts = T0 + np.arange(n_pre + 12) * H
    c = np.full(n_pre + 12, base)
    hi = c * (1 + rng / 2)
    lo = c * (1 - rng / 2)
    if consol:
        for k in range(12):
            hi[n_pre + k] = base * (1.004 - 0.0001 * k)
            lo[n_pre + k] = base * (0.996 + 0.0004 * k)
    return mk(ts, c, hi, lo, c, np.full(len(c), 1e4))


def m15(n=129, base=1.6, last_open=None, last_close=None, last_vol=400, vol=100):
    ts = T0 + np.arange(n) * M15
    c = np.full(n, base)
    o, h, l = c.copy(), c * 1.0007, c * 0.9993
    v = np.full(n, float(vol))
    o[-1], c[-1] = last_open, last_close
    h[-1], l[-1] = max(o[-1], c[-1]) * 1.0003, min(o[-1], c[-1]) * 0.9997
    v[-1] = last_vol
    return mk(ts, o, h, l, c, v)


def test_breakout_long_fires():
    d, h = daily_trend(), hourly()
    m = m15(last_open=1.6 * 1.002, last_close=1.6 * 1.0045)
    sigs = evaluate("TEST/USDT:USDT", d, h, m, CFG)
    s = [x for x in sigs if x.setup == "Breakout"]
    assert s and s[0].side == "long" and s[0].stop < s[0].entry
    assert s[0].score >= 55


def test_breakout_needs_volume():
    d, h = daily_trend(), hourly()
    m = m15(last_open=1.6 * 1.002, last_close=1.6 * 1.0045, last_vol=100)
    assert not [x for x in evaluate("T", d, h, m, CFG) if x.setup == "Breakout"]


def test_breakout_rejects_loose_consolidation():
    d, h = daily_trend(), hourly(consol=False, rng=0.12)   # velas de 12% de rango: nada tenso
    m = m15(last_open=1.6 * 1.002, last_close=1.6 * 1.0045)
    assert not [x for x in evaluate("T", d, h, m, CFG) if x.setup == "Breakout"]


def test_episodic_pivot_long():
    n = 60
    c = np.full(n, 1.0)
    d = mk(T0 + np.arange(n) * DAY, c, c * 1.03, c * 0.97, c, np.full(n, 1e6))
    h = hourly(base=1.0, rng=0.02, consol=False)
    m = m15(base=1.0, last_open=1.0, last_close=1.04, last_vol=900)
    s = [x for x in evaluate("T", d, h, m, CFG) if x.setup == "Episodic Pivot"]
    assert s and s[0].side == "long"


def test_parabolic_short():
    n = 60
    c = np.full(n, 1.0)
    c[-6:] = [1.0, 1.1, 1.2, 1.3, 1.45, 1.6]
    d = mk(T0 + np.arange(n) * DAY, c, c * 1.04, c * 0.97, c, np.full(n, 1e6))
    h = hourly(base=1.6, rng=0.03, consol=False)
    # 15m: la sesión de hoy termina a las 20:00 UTC
    nb = 129
    end = T0 + 20 * H
    ts = end - (nb - 1) * M15 + np.arange(nb) * M15
    cl = np.full(nb, 1.55)
    cl[-30:-2] = np.linspace(1.55, 1.64, 28)
    cl[-2] = 1.62
    cl[-1] = 1.55
    op = np.roll(cl, 1)
    op[0] = 1.55
    op[-1] = 1.61
    hi = np.maximum(op, cl) * 1.001
    lo = np.minimum(op, cl) * 0.999
    hi[-40] = 1.66   # máximo del día
    m = mk(ts, op, hi, lo, cl, np.full(nb, 100.0))
    s = [x for x in evaluate("T", d, h, m, CFG) if x.setup == "Parabolic Short"]
    assert s and s[0].side == "short" and s[0].stop > s[0].entry


def test_regime_adjustment():
    s = Signal("X", "long", "Breakout", 1.0, 0.98, 70.0, [])
    assert adjust_score(s, "bull", None).score > 70
    s = Signal("X", "long", "Breakout", 1.0, 0.98, 70.0, [])
    assert adjust_score(s, "bear", None).score < 70


class FakeEx:
    def __init__(self):
        self.price = {"TEST/USDT:USDT": 100.0}

    def amount_to_precision(self, s, q): return f"{q:.3f}"
    def price_to_precision(self, s, p): return f"{p:.4f}"
    def market(self, s): return {"limits": {"amount": {"min": 0.001}}}
    def fetch_ticker(self, s): return {"last": self.price[s]}
    def fetch_tickers(self, syms): return {s: {"last": self.price[s]} for s in syms}
    def fetch_ohlcv(self, *a, **k): raise RuntimeError("sin datos")


def make_trader(tmp_path):
    st = State(str(tmp_path), 1000.0)
    msgs = []
    return Trader(FakeEx(), CFG, st, msgs.append), st, msgs


def test_paper_lifecycle_tp1_then_breakeven(tmp_path):
    tr, st, msgs = make_trader(tmp_path)
    sig = Signal("TEST/USDT:USDT", "long", "Breakout", 100.0, 98.0, 80.0, [])
    ok, why = tr.open(sig)
    assert ok, why
    pos = st.data["positions"]["TEST/USDT:USDT"]
    # riesgo 0.5% de 1000 = $5 ; distancia 2 -> qty 2.5
    assert pos["qty"] == pytest.approx(2.5, rel=1e-3)
    tr.ex.price["TEST/USDT:USDT"] = 103.5          # 1.75R -> TP1
    tr.manage()
    pos = st.data["positions"]["TEST/USDT:USDT"]
    assert pos["tp1_hit"] and pos["qty"] == pytest.approx(1.25, rel=1e-3)
    assert pos["stop"] >= 100.0                     # break-even
    tr.ex.price["TEST/USDT:USDT"] = 99.0            # vuelve al stop
    tr.manage()
    assert "TEST/USDT:USDT" not in st.data["positions"]
    assert st.data["paper_equity"] > 1000.0         # cerró en positivo gracias al parcial


def test_paper_stop_loss_and_limits(tmp_path):
    tr, st, _ = make_trader(tmp_path)
    sig = Signal("TEST/USDT:USDT", "short", "Breakdown", 100.0, 102.0, 80.0, [])
    assert tr.open(sig)[0]
    assert not tr.open(sig)[0]                      # ya abierta
    tr.ex.price["TEST/USDT:USDT"] = 102.5
    tr.manage()
    assert not st.data["positions"]
    assert st.data["paper_equity"] < 1000.0
    st.data["paused"] = True
    assert not tr.can_open(sig)[0]


# ───────────── candidatas (watchlist) ─────────────
from collections import Counter

from strategies import daily_ctx, trend_gate, watch_candidates


def _cands(d, h, m, cfg=CFG, stats=None):
    ctx = daily_ctx(d)
    priors = {s: trend_gate(s, ctx, cfg) for s in ("long", "short")}
    priors = {s: p for s, p in priors.items() if p is not None}
    return watch_candidates("T", priors, ctx, h, m, cfg, stats)


def test_candidate_near_trigger_has_plan():
    d, h = daily_trend(), hourly()
    m = m15(last_open=1.6, last_close=1.6, last_vol=100)        # sin volumen, 0.4% bajo el gatillo
    c = _cands(d, h, m)
    assert len(c) == 1 and c[0].side == "long" and c[0].setup == "Breakout"
    assert 0 < c[0].dist_pct <= CFG.watch_dist_pct
    assert c[0].trigger > c[0].price > c[0].stop
    # el gatillo de la candidata es exactamente el nivel que luego dispara la señal
    sig = [x for x in evaluate("T", d, h, m15(last_open=1.6 * 1.002, last_close=1.6 * 1.0045), CFG)
           if x.setup == "Breakout"]
    assert sig and sig[0].entry > c[0].trigger


def test_candidate_far_or_broken_is_not_listed():
    d, h = daily_trend(), hourly()
    stats = Counter()
    assert not _cands(d, h, m15(last_open=1.55, last_close=1.55, last_vol=100), stats=stats)
    assert stats["caja válida pero lejos del gatillo"] == 1
    stats = Counter()
    assert not _cands(d, h, m15(last_open=1.6 * 1.002, last_close=1.6 * 1.0045), stats=stats)
    assert stats["caja válida ya rota"] == 1             # eso ya es señal, no candidata


def test_candidate_needs_tight_box():
    d, h = daily_trend(), hourly(consol=False, rng=0.12)
    stats = Counter()
    assert not _cands(d, h, m15(last_open=1.6, last_close=1.6, last_vol=100), stats=stats)
    assert stats["caja 1h demasiado ancha"] == 1


# ───────────── escaneo completo con exchange simulado ─────────────
from scanner import Scanner


class FakeScanEx:
    def __init__(self, data):
        self.data = data                                     # {sym: (daily, hourly, m15)}
        self.markets = {s: {"swap": True, "linear": True, "quote": "USDT", "active": True, "id": s.split("/")[0] + "-USDT"}
                        for s in data}

    def fetch_tickers(self):
        return {s: {"quoteVolume": 5e6, "percentage": 1.0} for s in self.data}

    def fetch_ohlcv(self, sym, tf, limit=100):
        d, h, m = self.data.get(sym) or next(iter(self.data.values()))
        df = {"1d": d, "1h": h, "15m": m}[tf]
        return df.values.tolist()

    def fetch_funding_rate(self, sym):
        raise RuntimeError("sin funding")


def _with_open_candle(m):
    """El escáner descarta la última vela (en curso): añadimos una de relleno."""
    return pd.concat([m, m.iloc[[-1]].assign(ts=m["ts"].iloc[-1] + M15)], ignore_index=True)


def _with_open_hour(h):
    return pd.concat([h, h.iloc[[-1]].assign(ts=h["ts"].iloc[-1] + H)], ignore_index=True)


def test_scan_separates_candidates_and_signals():
    d, h = daily_trend(), _with_open_hour(hourly())
    flat = daily_trend(g=1.0, rng=0.01)                       # ADR bajo: ni candidata ni señal
    data = {
        "WAIT/USDT:USDT": (d, h, _with_open_candle(m15(last_open=1.6, last_close=1.6, last_vol=100))),
        "GO/USDT:USDT": (d, h, _with_open_candle(m15(last_open=1.6 * 1.002, last_close=1.6 * 1.0045))),
        "DEAD/USDT:USDT": (flat, h, _with_open_candle(m15(last_open=1.6, last_close=1.6, last_vol=100))),
    }
    res = Scanner(FakeScanEx(data), CFG).scan()
    assert [c.symbol for c in res.candidates] == ["WAIT/USDT:USDT"]
    assert [s.symbol for s in res.signals if s.setup == "Breakout"] == ["GO/USDT:USDT"]
    assert res.stats["analizados"] == 3 and res.stats["errores de datos"] == 0
    assert res.stats["no pasa filtro diario: ADR diario bajo"] == 1
    assert res.movers and res.movers[0][0] == "GO/USDT:USDT"


def test_scan_reports_data_errors_instead_of_hiding_them():
    class Broken(FakeScanEx):
        def fetch_ohlcv(self, sym, tf, limit=100):
            raise ConnectionError("403 forbidden")
    d, h = daily_trend(), _with_open_hour(hourly())
    ex = Broken({"WAIT/USDT:USDT": (d, h, _with_open_candle(m15(last_open=1.6, last_close=1.6)))})
    res = Scanner(ex, CFG).scan()
    assert res.stats["errores de datos"] == 1 and "ConnectionError" in res.errors[0]


# ───────────── avisos ─────────────
import main as botmain
import telegram as tgmod


def test_formatters_do_not_crash_and_escape_html():
    d, h = daily_trend(), _with_open_hour(hourly())
    data = {"WAIT/USDT:USDT": (d, h, _with_open_candle(m15(last_open=1.6, last_close=1.6, last_vol=100)))}
    res = Scanner(FakeScanEx(data), CFG).scan()
    txt = botmain.fmt_digest(res, CFG)
    assert "Candidatas: 1" in txt and "WAIT/USDT:USDT" in txt
    assert "AÚN NO ENTRES" in botmain.fmt_watch(res.candidates, CFG)
    assert "CIERRA &gt;" in botmain.fmt_watch(res.candidates, CFG)
    assert "aún no" in botmain.fmt_digest(None, CFG).lower()


def test_watch_dedupe(tmp_path):
    st = State(str(tmp_path), 1000.0)
    assert st.watch_due("A|long", 360)
    st.watch_mark(["A|long"])
    assert not st.watch_due("A|long", 360) and st.watch_due("B|long", 360)
    assert not State(str(tmp_path), 1000.0).watch_due("A|long", 360)      # sobrevive a reinicios


def test_telegram_retries_as_plain_text_when_html_is_rejected(monkeypatch):
    calls = []

    class R:
        def __init__(self, code): self.status_code, self.text = code, "x"

    def fake_post(url, json=None, timeout=0):
        calls.append(json)
        return R(400 if "parse_mode" in json else 200)

    monkeypatch.setattr(tgmod.requests, "post", fake_post)
    n = tgmod.Notifier(Config(tg_token="t", tg_chat_id="1"))
    assert n.send("<b>x</b> a &lt; b") is True
    assert "parse_mode" in calls[0] and "parse_mode" not in calls[1]
    assert calls[1]["text"] == "x a < b"


# ───────────── límite de BingX (100410), distancia por ADR, /debug ─────────────
RATE_ERR = 'bingx {"code":100410,"msg":"请稍后重试"}'


def test_scan_retries_when_bingx_rate_limits():
    d, h = daily_trend(), _with_open_hour(hourly())
    m = _with_open_candle(m15(last_open=1.6, last_close=1.6, last_vol=100))

    class Flaky(FakeScanEx):
        fails = 2
        def fetch_ohlcv(self, sym, tf, limit=100):
            if tf == "15m" and Flaky.fails > 0:
                Flaky.fails -= 1
                raise Exception(RATE_ERR)
            return super().fetch_ohlcv(sym, tf, limit)

    sc = Scanner(Flaky({"WAIT/USDT:USDT": (d, h, m)}), CFG)
    waits = []
    sc._sleep = waits.append                                   # sin esperar de verdad
    res = sc.scan()
    assert res.stats["errores de datos"] == 0                  # se recuperó solo
    assert res.stats["peticiones reintentadas por límite de BingX"] == 2 and waits == [2.0, 5.0]
    assert [c.symbol for c in res.candidates] == ["WAIT/USDT:USDT"]


def test_scan_gives_up_after_retries_and_reports():
    class AlwaysLimited(FakeScanEx):
        def fetch_ohlcv(self, sym, tf, limit=100):
            raise Exception(RATE_ERR)
    d, h = daily_trend(), _with_open_hour(hourly())
    sc = Scanner(AlwaysLimited({"WAIT/USDT:USDT": (d, h, _with_open_candle(m15(last_open=1.6, last_close=1.6)))}), CFG)
    sc._sleep = lambda s: None
    res = sc.scan()
    assert res.stats["errores de datos"] == 1 and "100410" in res.errors[0]


def test_watch_distance_scales_with_adr():
    d, h = daily_trend(rng=0.05), hourly()                     # ADR ≈ 10%
    # precio ~2.3% bajo el gatillo: fuera del 1.5% fijo, dentro de 0.25 x ADR (≈2.5%)
    m = m15(last_open=1.57, last_close=1.57, last_vol=100)
    c = _cands(d, h, m)
    assert c and 1.5 < c[0].dist_pct <= 0.25 * c[0].adr
    far = []
    ctx = daily_ctx(d)
    strict = Config(mode="paper", watch_dist_adr=0.0)
    assert not watch_candidates("T", {"long": 30.0}, ctx, h, m, strict, None, far)
    assert far and far[0][0] == "T" and far[0][1] == "long" and far[0][2] > 1.5


def test_debug_command_shows_raw_volumes():
    d, h = daily_trend(), _with_open_hour(hourly())
    sc = Scanner(FakeScanEx({"TRX/USDT:USDT": (d, h, _with_open_candle(m15(last_open=1.6, last_close=1.6, last_vol=100)))}), CFG)
    txt = botmain.fmt_debug(sc, "trx")
    assert "TRX/USDT:USDT" in txt and "RVOL calculado" in txt and "UTC" in txt
    assert "Uso:" in botmain.fmt_debug(sc, "")



# ───────────── mejoras: rupturas tardías, filtro 30d, avisos, sizing, TP1 ─────────────
from strategies import adjust_candidate, why_no_signal


def _m_with_prev_break(prev_vol, last_vol=400, extra_above=0):
    """Una o más velas previas ya cerraron sobre el nivel (1.6064); la última rompe con volumen."""
    m = m15(last_open=1.6 * 1.0045, last_close=1.6 * 1.0070, last_vol=last_vol)
    for k in range(2, 2 + 1 + extra_above):
        m.loc[m.index[-k], ["open", "high", "low", "close", "volume"]] = [1.6 * 1.002, 1.6 * 1.0048, 1.6 * 1.0015, 1.6 * 1.0045, prev_vol]
    return m


def test_breakout_fires_on_second_candle_when_first_had_no_volume():
    d, h = daily_trend(), hourly()
    s = [x for x in evaluate("T", d, h, _m_with_prev_break(100), CFG) if x.setup == "Breakout"]
    assert s and any("nº 2" in r for r in s[0].reasons)


def test_breakout_not_late_after_too_many_bars_above_level():
    d, h = daily_trend(), hourly()
    m = _m_with_prev_break(100, extra_above=3)               # 4 cierres seguidos sobre el nivel > 3
    assert not [x for x in evaluate("T", d, h, m, CFG) if x.setup == "Breakout"]


def test_trend_gate_requires_price_near_30d_extreme():
    ctx = {"adr": 6.0, "close": 1.0, "e10": 1.0, "e20": 0.9, "runup30": 50.0, "drop30": 30.0, "pos30": 0.2}
    assert trend_gate("long", ctx, CFG) is None                      # subió mucho, pero ya devolvió el movimiento
    assert trend_gate("long", {**ctx, "pos30": 0.8}, CFG) == 50.0
    sh = {**ctx, "close": 0.95, "e10": 0.9, "e20": 1.0, "pos30": 0.8}
    assert trend_gate("short", sh, CFG) is None
    assert trend_gate("short", {**sh, "pos30": 0.2}, CFG) == 30.0
    assert trend_gate("long", {**ctx, "pos30": 0.2}, Config(mode="paper", range_pos_max=1.0)) == 50.0  # desactivable


def test_candidate_near_flag_and_regime_adjust():
    d, h = daily_trend(), hourly()
    c = _cands(d, h, m15(last_open=1.6, last_close=1.6, last_vol=100))[0]          # a 0.4% -> casi
    assert c.near
    far = _cands(d, h, m15(last_open=1.592, last_close=1.592, last_vol=100))        # ~0.9% -> candidata, no casi
    assert far and not far[0].near
    base = c.score
    assert adjust_candidate(c, "bear").score == pytest.approx(base - 12) and "contra" in c.regime_note
    c.score = base
    assert adjust_candidate(c, "bull").score == pytest.approx(base + 8)


def test_why_no_signal_explains_movers():
    d, h = daily_trend(), hourly()
    ctx = daily_ctx(d)
    pri = {"long": trend_gate("long", ctx, CFG)}
    assert "caja OK" in why_no_signal(ctx, pri, h, m15(last_open=1.6, last_close=1.6), CFG)
    assert why_no_signal(ctx, {}, h, m15(last_open=1.6, last_close=1.6), CFG).startswith("filtro diario")
    wide = hourly(consol=False, rng=0.12)
    assert "caja 1h demasiado ancha" in why_no_signal(ctx, pri, wide, m15(last_open=1.6, last_close=1.6), CFG)


def test_pick_alerts_skips_cooldown_before_limiting(tmp_path):
    st = State(str(tmp_path), 1000.0)
    mk_sig = lambda n, sc: Signal(n, "long", "Breakout", 1.0, 0.98, sc, [])
    res = type("R", (), {"signals": [mk_sig("A", 90), mk_sig("B", 85), mk_sig("C", 80), mk_sig("D", 70), mk_sig("D", 60), mk_sig("E", 40)]})()
    for n in "ABC":
        st.set_cooldown(n)
    got = botmain.pick_alerts(res, st, CFG)
    assert [s.symbol for s in got] == ["D"]          # antes: top-3 (A,B,C) en cooldown tapaban a D; E < ALERT_MIN_SCORE


def test_pick_watch_near_alerts_even_if_seen_far_before(tmp_path):
    st = State(str(tmp_path), 1000.0)
    d, h = daily_trend(), hourly()
    near_c = _cands(d, h, m15(last_open=1.6, last_close=1.6, last_vol=100))[0]
    res = type("R", (), {"candidates": [near_c]})()
    st.watch_mark([near_c.key])                       # ya avisada como candidata lejana
    near, new = botmain.pick_watch(res, st, CFG)
    assert near == [near_c] and new == []
    st.watch_mark([near_c.key, near_c.key + "|near"])
    assert botmain.pick_watch(res, st, CFG) == ([], [])


def test_signal_and_candidate_messages_include_plan_and_size():
    s = Signal("VVV/USDT:USDT", "long", "Breakout", 25.9, 24.72, 70.0, ["x"])
    txt = botmain.fmt_signal(s, "bull", CFG, True, equity=1000.0)
    risk = 25.9 - 24.72
    assert f"válida mientras no pase de {botmain.px(25.9 + 0.3 * risk)}" in txt
    # riesgo 0.5% de 1000 = $5 -> qty 4.237 ; nominal ≈ $109.7 ; margen 5x ≈ $22
    assert "riesgo ≈ $5.00" in txt and "margen ≈ $22 a 5x" in txt
    assert "Tamaño" not in botmain.fmt_signal(s, "bull", CFG, True)          # sin equity no inventa
    capped = botmain.plan_text(CFG, 1000.0, 100.0, 99.9)                     # stop minúsculo -> tope de margen
    assert "limitado por MAX_MARGIN_PCT" in capped
    d, h = daily_trend(), hourly()
    c = _cands(d, h, m15(last_open=1.6, last_close=1.6, last_vol=100))[0]
    t = botmain.fmt_cand(c, CFG, 1000.0)
    assert "TP1 ≈" in t and "Se anula si toca" in t and "Tamaño" in t


def test_digest_shows_mover_reason():
    d, h = daily_trend(), _with_open_hour(hourly())
    m = _with_open_candle(m15(last_open=1.6, last_close=1.6, last_vol=1000))     # RVOL 10x pero sin ruptura
    res = Scanner(FakeScanEx({"HOT/USDT:USDT": (d, h, m)}), CFG).scan()
    assert res.movers and res.movers[0][3].startswith("caja OK")
    assert "por qué no es señal" in botmain.fmt_digest(res, CFG)


def test_tp1_is_retried_if_the_partial_close_fails(tmp_path):
    tr, st, _ = make_trader(tmp_path)
    assert tr.open(Signal("TEST/USDT:USDT", "long", "Breakout", 100.0, 98.0, 80.0, []))[0]
    tr.ex.price["TEST/USDT:USDT"] = 103.5
    real_close = tr.close
    tr.close = lambda *a, **k: False                     # el exchange rechaza la orden
    tr.manage()
    pos = st.data["positions"]["TEST/USDT:USDT"]
    assert not pos["tp1_hit"] and pos["qty"] == pytest.approx(2.5, rel=1e-3)
    tr.close = real_close
    pos["tp1_try"] = 0                                   # pasa el enfriamiento de 60 s
    tr.manage()
    assert st.data["positions"]["TEST/USDT:USDT"]["tp1_hit"]


# ───────────── anticipación: 5m, escaneo alineado, añadir al confirmar, break-even ─────────────
from strategies import early_break


def _cand():
    return _cands(daily_trend(), hourly(), m15(last_open=1.6, last_close=1.6, last_vol=100))[0]   # gatillo 1.6064


def _m5(o, c, vol=400, n=60):
    return m15(n=n, last_open=o, last_close=c, last_vol=vol)


def test_early_break_fires_on_5m_close_and_is_marked_early():
    c = _cand()
    s = early_break(c, _m5(1.603, 1.6075), CFG)
    assert s and s.early and s.side == "long" and s.setup.endswith("(anticipada)") and s.stop < s.entry
    assert any("ANTICIPADA" in r for r in s.reasons)
    assert s.score < 100 and c.raw_stop > 0 and c.atr1h > 0


def test_early_break_rejects_wicks_low_volume_and_late_breaks():
    c = _cand()
    assert early_break(c, _m5(1.603, 1.6040), CFG) is None            # cierra dentro de la caja (mecha)
    assert early_break(c, _m5(1.603, 1.6075, vol=120), CFG) is None   # sin volumen
    assert early_break(c, _m5(1.6075, 1.6030), CFG) is None           # vela roja en un long
    assert early_break(c, _m5(1.603, 1.6400), CFG) is None            # ya persiguiendo (>1.5%)
    m = _m5(1.603, 1.6075)
    for k in (2, 3):                                                   # 3 cierres seguidos fuera: ya no es "anticipada"
        m.loc[m.index[-k], ["open", "high", "low", "close"]] = [1.603, 1.609, 1.602, 1.6075]
    assert early_break(c, m, CFG) is None


def test_scanner_early_signals_uses_live_price_then_5m_candles():
    c = _cand()
    d, h = daily_trend(), _with_open_hour(hourly())
    m5 = _with_open_candle(_m5(1.603, 1.6075))

    class Ex(FakeScanEx):
        last = 1.61
        def fetch_tickers(self):
            return {c.symbol: {"last": Ex.last}}
        def fetch_ohlcv(self, sym, tf, limit=100):
            return m5.values.tolist() if tf == "5m" else super().fetch_ohlcv(sym, tf, limit)

    sc = Scanner(Ex({c.symbol: (d, h, _with_open_candle(m15(last_open=1.6, last_close=1.6)))}), CFG)
    assert [s.early for s in sc.early_signals([c])] == [True]
    Ex.last = 1.60                                                      # el precio vivo aún no pasó el gatillo
    assert sc.early_signals([c]) == [] and sc.early_signals([]) == []


def test_scan_is_aligned_to_candle_closes():
    base = 1_700_000_100                                                # múltiplo de 300
    for off in (0, 5, 10, 11, 43, 150, 299):
        wait = botmain.secs_to_next_slot(base + off, 300, 10)
        assert 0 < wait <= 300
        assert (base + off + wait - 10) % 300 == 0                      # aterriza en cierre de vela + 10 s
    assert botmain.secs_to_next_slot(base + 53, 300, 10) == pytest.approx(257)


def test_pick_early_respects_cooldowns(tmp_path):
    st = State(str(tmp_path), 1000.0)
    mk_sig = lambda n, sc: Signal(n, "long", "Breakout (anticipada)", 1.0, 0.98, sc, [], early=True)
    sigs = [mk_sig("A", 70), mk_sig("B", 65), mk_sig("C", 30), mk_sig("D", 60)]
    st.set_cooldown("A|early")
    st.set_cooldown("D")                                                # ya hubo señal 15m de D
    assert [s.symbol for s in botmain.pick_early(sigs, st, CFG)] == ["B"]   # C < ALERT_MIN_SCORE


def test_early_message_and_state_marker(tmp_path):
    s = Signal("X/USDT:USDT", "long", "Breakout (anticipada)", 100.0, 98.0, 70.0, ["r"], early=True)
    t = botmain.fmt_signal(s, "bull", CFG, True, equity=1000.0)
    assert "ANTICIPADA" in t and "riesgo ≈ $2.50" in t                  # mitad del 0.5% de 1000
    full = Signal("X/USDT:USDT", "long", "Breakout", 100.5, 98.0, 75.0, ["r"])
    t2 = botmain.fmt_signal(full, "bull", CFG, True, equity=1000.0, after_early=7)
    assert "CONFIRMA la anticipada de hace 7 min" in t2 and "riesgo ≈ $2.50" in t2
    st = State(str(tmp_path), 1000.0)
    assert st.early_age_min("X") is None
    st.early_mark("X")
    assert st.early_age_min("X") < 1 and State(str(tmp_path), 1000.0).early_age_min("X") is not None


def test_early_entry_half_risk_then_add_on_confirmation(tmp_path):
    tr, st, msgs = make_trader(tmp_path)
    early = Signal("TEST/USDT:USDT", "long", "Breakout (anticipada)", 100.0, 98.0, 80.0, [], early=True)
    assert tr.open(early)[0]
    pos = st.data["positions"]["TEST/USDT:USDT"]
    assert pos["early"] and pos["qty"] == pytest.approx(1.25, rel=1e-3)          # $2.5 de riesgo / 2
    confirm = Signal("TEST/USDT:USDT", "long", "Breakout", 100.5, 98.0, 80.0, [])
    ok, why = tr.open(confirm)
    assert ok, why
    pos = st.data["positions"]["TEST/USDT:USDT"]
    assert pos["added"] and pos["qty"] == pytest.approx(2.25, rel=1e-3)          # +1.0 (riesgo $2.5 / 2.5 de distancia)
    assert pos["entry"] == pytest.approx((100 * 1.25 + 100.5 * 1.0) / 2.25, rel=1e-4)
    assert pos["risk_usd"] == pytest.approx(5.0, rel=0.02)                       # riesgo total ≈ 0.5% del equity
    assert st.data["day"]["trades"] == 1                                          # añadir no cuenta como operación nueva
    assert not tr.open(confirm)[0]                                                # una sola vez


def test_no_add_when_early_already_at_breakeven(tmp_path):
    tr, st, _ = make_trader(tmp_path)
    assert tr.open(Signal("TEST/USDT:USDT", "long", "B (anticipada)", 100.0, 98.0, 80.0, [], early=True))[0]
    tr.ex.price["TEST/USDT:USDT"] = 102.4                                         # +1.2R -> break-even
    tr.manage()
    ok, why = tr.open(Signal("TEST/USDT:USDT", "long", "Breakout", 102.4, 98.0, 80.0, []))
    assert not ok and "no perseguir" in why


def test_breakeven_at_1R_makes_the_trade_riskless(tmp_path):
    tr, st, msgs = make_trader(tmp_path)
    assert tr.open(Signal("TEST/USDT:USDT", "long", "Breakout", 100.0, 98.0, 80.0, []))[0]
    tr.ex.price["TEST/USDT:USDT"] = 102.2                                         # 1.1R: aún sin TP1 (1.5R)
    tr.manage()
    pos = st.data["positions"]["TEST/USDT:USDT"]
    assert pos["be_done"] and not pos["tp1_hit"] and pos["stop"] > 100.0 and pos["qty"] == pytest.approx(2.5, rel=1e-3)
    assert any("break-even a +1R" in m for m in msgs)
    tr.ex.price["TEST/USDT:USDT"] = 99.9                                          # vuelve: sale ~a cero, no a -1R
    tr.manage()
    assert "TEST/USDT:USDT" not in st.data["positions"]
    assert st.data["paper_equity"] > 999.0                                        # pérdida ≈ comisiones, no los $5 de riesgo
    tr2, st2, _ = make_trader(tmp_path / "b")
    tr2.cfg = Config(mode="paper", be_r=0.0)                                      # desactivable
    assert tr2.open(Signal("TEST/USDT:USDT", "long", "Breakout", 100.0, 98.0, 80.0, []))[0]
    tr2.ex.price["TEST/USDT:USDT"] = 102.2
    tr2.manage()
    assert not st2.data["positions"]["TEST/USDT:USDT"]["be_done"]


# ───────────── sistema ICT: 4h estructura → 1h barrido en POI → 15m CISD + volumen ─────────────
from ict import find_cisd, ict_signals, structure_bias, swings, with_partial_hour

H4MS = 4 * H


def zigzag_4h(points, per=8):
    closes = [points[0]]
    for a, b in zip(points[:-1], points[1:]):
        closes += [a + (b - a) * s / per for s in range(1, per + 1)]
    c = np.array(closes)
    o = np.roll(c, 1)
    o[0] = c[0]
    return mk(T0 + np.arange(len(c)) * H4MS, o, np.maximum(o, c) * 1.002, np.minimum(o, c) * 0.998, c, np.full(len(c), 1e5))


def ict_h4_bull():
    return zigzag_4h([80, 100, 92, 120, 108, 110])          # HH (120>100) + HL (108>92), equilibrio ≈ 114


def ict_h1():
    rows = [(110.0, 110.2, 109.8, 110.0)] * 60
    rows += [(110.0, 110.1, 109.5, 109.6),                  # 60: vela bajista = Order Block [109.5, 110.1]
             (109.6, 111.0, 109.55, 110.9),                 # 61: desplazamiento alcista
             (110.9, 111.3, 110.5, 111.2)]                  # 62: deja un FVG [110.1, 110.5]
    rows += [(111.0, 111.2, 110.8, 111.0)] * 18             # 63..80
    rows += [(111.0, 111.1, 110.6, 110.7), (110.7, 110.8, 110.3, 110.4), (110.4, 110.5, 110.2, 110.3),
             (110.3, 110.4, 110.1, 110.2), (110.2, 110.3, 110.05, 110.15), (110.15, 110.25, 110.0, 110.1),
             (110.1, 110.2, 110.0, 110.1)]                  # 81..87: la liquidez (mín 110.0) queda justo debajo
    a = np.array(rows)
    n = len(a)
    return mk(T0 + np.arange(n) * H, a[:, 0], a[:, 1], a[:, 2], a[:, 3], np.full(n, 1000.0))


def ict_m15(cisd_vol=500, a_low=109.9, b_low=109.8, c_low=109.84, extra_up=0):
    """117 velas planas + tramo bajista (a, b) que barre 110.0 dentro de la hora 88 + vela CISD alcista (c)."""
    t_a = T0 + 88 * H
    flat = [(110.15, 110.2, 110.1, 110.15, 100.0)] * 117
    tail = [(110.1, 110.12, a_low, 109.95, 120.0),          # a: bajista, abre en 110.1 (= nivel CISD)
            (109.95, 109.98, b_low, 109.85, 110.0),         # b: bajista, mecha a 109.8 (barrido)
            (109.85, 110.25, c_low, 110.2, float(cisd_vol))]  # c: cierra 110.2 > 110.1 -> CISD
    p = 110.2
    for _ in range(extra_up):
        tail.append((p, p + 0.12, p - 0.02, p + 0.1, 100.0))
        p += 0.1
    rows = flat + tail
    a = np.array(rows)
    n = len(a)
    return mk(t_a - 117 * M15 + np.arange(n) * M15, a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4])


def mirror(df, k=220.0):
    out = df.copy()
    out["open"], out["close"] = k - df["open"], k - df["close"]
    out["high"], out["low"] = k - df["low"], k - df["high"]
    return out


def test_swings_and_structure_bias():
    sh, sl = swings(ict_h4_bull(), 2)
    assert len(sh) == 2 and sh[1][1] > sh[0][1] and len(sl) == 2 and sl[1][1] > sl[0][1]
    assert structure_bias(ict_h4_bull())["bias"] == "long"
    assert structure_bias(mirror(ict_h4_bull()))["bias"] == "short"
    assert structure_bias(zigzag_4h([80, 100, 92, 105, 90, 95])) is None       # HH + LL = rango: no se opera


def test_ict_long_fires_with_volume_and_full_plan():
    stats = Counter()
    s = ict_signals("T/USDT:USDT", ict_h4_bull(), ict_h1(), ict_m15(), CFG, stats)
    assert len(s) == 1
    s = s[0]
    assert s.side == "long" and s.setup == "ICT Sweep+CISD" and s.stop < 109.8 < s.entry == pytest.approx(110.2)
    assert s.score >= CFG.min_score
    txt = " | ".join(s.reasons)
    assert "estructura alcista" in txt and "barrido de mínimos en OB" in txt and "CISD sobre 110.1" in txt
    assert "RVOL" in txt and "Objetivo liquidez 4h" in txt
    assert [stats[k] for k in ("ICT: sesgo 4h", "ICT: POI", "ICT: barrido en POI", "ICT: CISD 15m", "ICT: señales")] == [1] * 5


def test_ict_short_is_the_mirror_image():
    s = ict_signals("T", mirror(ict_h4_bull()), mirror(ict_h1()), mirror(ict_m15()), CFG)
    assert len(s) == 1 and s[0].side == "short" and s[0].stop > s[0].entry
    assert "estructura bajista" in s[0].reasons[0] and "barrido de máximos" in s[0].reasons[1]


def test_ict_volume_is_the_gate():
    stats = Counter()
    assert ict_signals("T", ict_h4_bull(), ict_h1(), ict_m15(cisd_vol=110), CFG, stats) == []
    assert stats["ICT: CISD sin confirmación de volumen/cierre"] == 1 and stats["ICT: CISD 15m"] == 1
    assert ict_signals("T", ict_h4_bull(), ict_h1(), ict_m15(cisd_vol=110), Config(mode="paper", ict_rvol=0.0, ict_vol_vs_run=0.0))


def test_ict_requires_a_recent_sweep_of_liquidity():
    stats = Counter()
    nosweep = Config(mode="paper", ict_sweep_bars=0)                           # ninguna vela 1h cuenta como barrido reciente
    assert ict_signals("T", ict_h4_bull(), ict_h1(), ict_m15(), nosweep, stats) == []
    assert stats["ICT: POI"] == 1 and stats["ICT: barrido en POI"] == 0
    # sin ningún POI (no hay vela de desplazamiento): tampoco hay señal
    stats = Counter()
    flat = ict_h1().copy()
    flat.loc[61, ["open", "high", "low", "close"]] = [109.6, 109.8, 109.55, 109.7]
    assert ict_signals("T", ict_h4_bull(), flat, ict_m15(), CFG, stats) == [] and stats["ICT: POI"] == 0


def test_ict_needs_4h_bias_and_discount_and_fresh_cisd():
    flat4h = zigzag_4h([80, 100, 92, 105, 90, 95])
    assert ict_signals("T", flat4h, ict_h1(), ict_m15(), CFG) == []
    up = ict_h1().copy()
    m_up = ict_m15()
    for df in (up, m_up):                                                      # POI a ~120 > equilibrio 4h (114): premium
        for col in ("open", "high", "low", "close"):
            df[col] = df[col] + 10
    stats = Counter()
    assert ict_signals("T", ict_h4_bull(), up, m_up, CFG, stats) == [] and stats["ICT: POI"] == 0
    assert ict_signals("T", ict_h4_bull(), ict_h1(), ict_m15(extra_up=3), CFG) == []   # CISD de hace 3 velas: tarde


def test_partial_hour_and_cisd_helpers():
    h, m = ict_h1(), ict_m15()
    hp = with_partial_hour(h, m)
    assert len(hp) == len(h) + 1 and hp["low"].iloc[-1] == pytest.approx(109.8) and hp["open"].iloc[-1] == pytest.approx(110.1)
    cs = find_cisd(m, "long", T0 + 88 * H, CFG)
    assert cs and cs["level"] == pytest.approx(110.1) and cs["n_run"] == 2 and cs["rv"] > 4 and cs["strength"] > 0.8


class IctEx(FakeScanEx):
    def __init__(self, frames):
        super().__init__({"ICT/USDT:USDT": (frames["1d"], frames["1h"], frames["15m"])})
        self.frames = frames

    def fetch_ohlcv(self, sym, tf, limit=100):
        return self.frames[tf].values.tolist()


def test_scanner_runs_ict_and_digest_shows_its_funnel():
    openc = lambda df, step: pd.concat([df, df.iloc[[-1]].assign(ts=df["ts"].iloc[-1] + step)], ignore_index=True)
    frames = {"1d": daily_trend(), "4h": openc(ict_h4_bull(), H4MS), "1h": openc(ict_h1(), H),
              "15m": openc(ict_m15(), M15)}
    res = Scanner(IctEx(frames), CFG).scan()
    ict = [s for s in res.signals if s.setup == "ICT Sweep+CISD"]
    assert len(ict) == 1 and ict[0].side == "long"
    assert res.stats["ICT: señales"] == 1 and res.stats["ICT: errores de datos"] == 0
    assert "ICT 4h→1h→15m" in botmain.fmt_digest(res, CFG) and "señales 1" in botmain.fmt_digest(res, CFG)


def test_ict_failure_does_not_hide_breakout_detection():
    d, h = daily_trend(), _with_open_hour(hourly())
    m = _with_open_candle(m15(last_open=1.6 * 1.002, last_close=1.6 * 1.0045))   # ruptura con volumen -> activa ICT
    class No4h(FakeScanEx):
        def fetch_ohlcv(self, sym, tf, limit=100):
            if tf == "4h":
                raise ConnectionError("sin 4h")
            return super().fetch_ohlcv(sym, tf, limit)
    res = Scanner(No4h({"GO/USDT:USDT": (d, h, m)}), CFG).scan()
    assert res.stats["ICT: errores de datos"] == 1 and res.stats["errores de datos"] == 0
    assert [s for s in res.signals if s.setup == "Breakout"]                      # la ruptura normal sigue funcionando
