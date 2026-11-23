"""Configuración 100% por variables de entorno (ideal para Railway)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _s(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def _i(name: str, default: int) -> int:
    return int(_f(name, default))


def _b(name: str, default: bool) -> bool:
    return _s(name, "true" if default else "false").lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Config:
    # modo y credenciales
    mode: str = "signals"
    api_key: str = ""
    api_secret: str = ""
    demo: bool = False
    hedge_mode: bool = True
    tg_token: str = ""
    tg_chat_id: str = ""
    data_dir: str = "data"

    # riesgo
    leverage: int = 5
    margin_mode: str = "isolated"
    risk_pct: float = 0.5
    max_margin_pct: float = 20.0
    max_open: int = 3
    max_trades_day: int = 6
    daily_loss_pct: float = 3.0
    paper_equity: float = 1000.0
    fee_pct: float = 0.05

    # escaneo
    api_rate_limit_ms: int = 250    # pausa mínima entre peticiones a BingX (evita el error 100410)
    scan_interval: int = 300
    manage_interval: int = 15
    trail_interval: int = 300
    min_volume_usd: float = 3_000_000
    max_symbols: int = 250
    prefilter_rvol: float = 1.8
    prefilter_move24: float = 15.0
    min_score: float = 65.0         # score mínimo para EJECUTAR (paper/live)
    alert_min_score: float = 50.0   # score mínimo para AVISAR por Telegram
    cooldown_min: int = 240
    max_signals_per_scan: int = 3
    exclude_regex: str = r"^(NCSK|NCCO|NCFX|NCSI)"
    enable_longs: bool = True
    enable_shorts: bool = True

    # candidatas (watchlist) y resúmenes
    watch_dist_pct: float = 1.5     # candidata si el precio está a <= X% del gatillo
    watch_dist_adr: float = 0.25    # ...o a <= X * ADR (se usa el mayor de los dos): escala con la volatilidad
    watch_max: int = 8              # máx. candidatas mostradas
    watch_near_pct: float = 0.5     # "casi en el gatillo": a <= max(X%, watch_near_adr * ADR) -> aviso extra 🔔
    watch_near_adr: float = 0.06
    watch_realert_min: int = 360    # no repetir el aviso de la misma candidata antes de X min
    digest_min: int = 60            # resumen periódico del escáner (0 = desactivado)

    # estrategia (Qullamaggie adaptado a cripto intradía)
    adr_min: float = 4.0            # ADR diario mínimo (%) -> "stocks que se mueven"
    runup_min: float = 20.0         # subida/caída previa 30d (%)
    consol_bars: int = 12           # velas de 1h de consolidación
    consol_range_adr: float = 0.8   # rango de consolidación <= 0.8 * ADR
    consol_contract: float = 1.0    # contracción: rango medio 2.ª mitad < 1.ª mitad * X (sube a 1.1-1.2 para relajar)
    rvol_cap: float = 40.0          # RVOL por encima de esto se comprueba (suele ser un dato raro de BingX)
    rvol_day_frac: float = 0.2      # ...y se descarta si esa vela supera X del volumen 24h o hay >=3 velas a cero
    rvol_min: float = 2.0           # volumen relativo mínimo en vela de ruptura (15m)
    max_chase_pct: float = 1.5      # no perseguir: máx % por encima del nivel roto
    ep_move_15m: float = 3.0        # Episodic Pivot: movimiento mínimo en 15m (%)
    ep_rvol: float = 4.0
    ep_max_prior: float = 40.0      # EP: no debe venir ya extendido (%)
    para_gain5: float = 40.0        # Parabolic short: subida mínima 5d (%)
    para_ext_atr: float = 3.0       # distancia a EMA20 diaria en ATR
    min_stop_atr: float = 0.8
    max_stop_atr: float = 3.0
    max_stop_pct: float = 8.0
    range_pos_max: float = 0.40     # long: precio en el 40% superior del rango 30d (short: 40% inferior). >=1 = desactivado
    break_fresh_bars: int = 3       # la ruptura vale si el nivel se superó hace <= N velas 15m (no solo en la 1.ª)

    # sistema ICT (4h estructura -> 1h barrido en POI -> 15m CISD + volumen)
    ict_enable: bool = True
    ict_swing_n: int = 2            # pivotes 4h: N velas a cada lado
    ict_disp_atr: float = 1.2       # vela de desplazamiento: cuerpo >= X * ATR 1h (nace el OB/FVG)
    ict_poi_bars: int = 72          # antigüedad máxima del POI (velas 1h)
    ict_liq_bars: int = 12          # liquidez = mín/máx de las últimas N velas 1h
    ict_sweep_bars: int = 8         # el barrido debe ser de las últimas N velas 1h
    ict_cisd_bars: int = 24         # el CISD debe llegar <= N velas 15m tras el barrido
    ict_rvol: float = 1.5           # VOLUMEN: RVOL mínimo de la vela CISD (15m)
    ict_vol_vs_run: float = 1.0     # VOLUMEN: vela CISD >= X * volumen medio del tramo en contra
    ict_close_pos: float = 0.6      # cierre de la vela CISD en el 60% favorable de su rango
    ict_min_rr: float = 2.0         # objetivo (liquidez 4h) a >= X R
    ict_discount: bool = True       # long solo en descuento / short solo en premium del rango 4h

    # gestión
    tp1_r: float = 1.5
    partial_pct: float = 50.0
    be_r: float = 1.0               # stop a break-even al llegar a +X R (antes de TP1). 0 = desactivado
    early_enable: bool = True       # entrada ANTICIPADA: cierre 5m sobre el gatillo de una candidata
    early_rvol: float = 2.0         # RVOL mínimo de la vela 5m de ruptura
    early_risk_mult: float = 0.5    # la anticipada entra con X del riesgo; el resto se añade al confirmar el 15m
    fast_interval: int = 20         # s entre revisiones rápidas de las candidatas
    entry_slip_r: float = 0.3       # no entrar si el precio ya se alejó más de X·R de la entrada de la señal
    max_hold_h: float = 24.0
    trail_tf: str = "1h"
    trail_ema: int = 10


def load_config() -> Config:
    mode = _s("TRADING_MODE", "signals").lower()
    if mode not in {"signals", "paper", "live"}:
        raise SystemExit("TRADING_MODE debe ser signals, paper o live")
    d = Config()
    return Config(
        mode=mode,
        api_key=_s("BINGX_API_KEY"),
        api_secret=_s("BINGX_API_SECRET"),
        demo=_b("BINGX_DEMO", d.demo),
        hedge_mode=_b("HEDGE_MODE", d.hedge_mode),
        tg_token=_s("TELEGRAM_BOT_TOKEN"),
        tg_chat_id=_s("TELEGRAM_CHAT_ID"),
        data_dir=_s("DATA_DIR", d.data_dir),
        leverage=_i("LEVERAGE", d.leverage),
        margin_mode=_s("MARGIN_MODE", d.margin_mode).lower(),
        risk_pct=_f("RISK_PER_TRADE_PCT", d.risk_pct),
        max_margin_pct=_f("MAX_MARGIN_PCT", d.max_margin_pct),
        max_open=_i("MAX_OPEN_POSITIONS", d.max_open),
        max_trades_day=_i("MAX_TRADES_PER_DAY", d.max_trades_day),
        daily_loss_pct=_f("DAILY_LOSS_LIMIT_PCT", d.daily_loss_pct),
        paper_equity=_f("PAPER_EQUITY", d.paper_equity),
        fee_pct=_f("FEE_PCT", d.fee_pct),
        api_rate_limit_ms=_i("API_RATE_LIMIT_MS", d.api_rate_limit_ms),
        scan_interval=_i("SCAN_INTERVAL_SEC", d.scan_interval),
        manage_interval=_i("MANAGE_INTERVAL_SEC", d.manage_interval),
        trail_interval=_i("TRAIL_INTERVAL_SEC", d.trail_interval),
        min_volume_usd=_f("MIN_24H_VOLUME_USD", d.min_volume_usd),
        max_symbols=_i("SCAN_MAX_SYMBOLS", d.max_symbols),
        prefilter_rvol=_f("PREFILTER_RVOL", d.prefilter_rvol),
        prefilter_move24=_f("PREFILTER_MOVE24_PCT", d.prefilter_move24),
        min_score=_f("MIN_SCORE", d.min_score),
        alert_min_score=_f("ALERT_MIN_SCORE", d.alert_min_score),
        watch_dist_pct=_f("WATCH_DIST_PCT", d.watch_dist_pct),
        watch_dist_adr=_f("WATCH_DIST_ADR", d.watch_dist_adr),
        watch_max=_i("WATCH_MAX", d.watch_max),
        watch_near_pct=_f("WATCH_NEAR_PCT", d.watch_near_pct),
        watch_near_adr=_f("WATCH_NEAR_ADR", d.watch_near_adr),
        watch_realert_min=_i("WATCH_REALERT_MIN", d.watch_realert_min),
        digest_min=_i("DIGEST_MIN", d.digest_min),
        cooldown_min=_i("COOLDOWN_MIN", d.cooldown_min),
        max_signals_per_scan=_i("MAX_SIGNALS_PER_SCAN", d.max_signals_per_scan),
        exclude_regex=_s("EXCLUDE_REGEX", d.exclude_regex),
        enable_longs=_b("ENABLE_LONGS", d.enable_longs),
        enable_shorts=_b("ENABLE_SHORTS", d.enable_shorts),
        adr_min=_f("ADR_MIN_PCT", d.adr_min),
        runup_min=_f("RUNUP_MIN_PCT", d.runup_min),
        consol_bars=_i("CONSOL_BARS", d.consol_bars),
        consol_range_adr=_f("CONSOL_RANGE_ADR", d.consol_range_adr),
        rvol_min=_f("RVOL_MIN", d.rvol_min),
        consol_contract=_f("CONSOL_CONTRACT", d.consol_contract),
        rvol_cap=_f("RVOL_CAP", d.rvol_cap),
        rvol_day_frac=_f("RVOL_DAY_FRAC", d.rvol_day_frac),
        max_chase_pct=_f("MAX_CHASE_PCT", d.max_chase_pct),
        ep_move_15m=_f("EP_MOVE_15M_PCT", d.ep_move_15m),
        ep_rvol=_f("EP_RVOL", d.ep_rvol),
        ep_max_prior=_f("EP_MAX_PRIOR_PCT", d.ep_max_prior),
        para_gain5=_f("PARABOLIC_GAIN_5D_PCT", d.para_gain5),
        para_ext_atr=_f("PARABOLIC_EXT_ATR", d.para_ext_atr),
        min_stop_atr=_f("MIN_STOP_ATR", d.min_stop_atr),
        max_stop_atr=_f("MAX_STOP_ATR", d.max_stop_atr),
        max_stop_pct=_f("MAX_STOP_PCT", d.max_stop_pct),
        range_pos_max=_f("RANGE_POS_MAX", d.range_pos_max),
        break_fresh_bars=_i("BREAK_FRESH_BARS", d.break_fresh_bars),
        ict_enable=_b("ICT_ENABLE", d.ict_enable),
        ict_swing_n=_i("ICT_SWING_N", d.ict_swing_n),
        ict_disp_atr=_f("ICT_DISP_ATR", d.ict_disp_atr),
        ict_poi_bars=_i("ICT_POI_BARS", d.ict_poi_bars),
        ict_liq_bars=_i("ICT_LIQ_BARS", d.ict_liq_bars),
        ict_sweep_bars=_i("ICT_SWEEP_BARS", d.ict_sweep_bars),
        ict_cisd_bars=_i("ICT_CISD_BARS", d.ict_cisd_bars),
        ict_rvol=_f("ICT_RVOL", d.ict_rvol),
        ict_vol_vs_run=_f("ICT_VOL_VS_RUN", d.ict_vol_vs_run),
        ict_close_pos=_f("ICT_CLOSE_POS", d.ict_close_pos),
        ict_min_rr=_f("ICT_MIN_RR", d.ict_min_rr),
        ict_discount=_b("ICT_DISCOUNT", d.ict_discount),
        tp1_r=_f("TP1_R", d.tp1_r),
        partial_pct=_f("PARTIAL_PCT", d.partial_pct),
        be_r=_f("BE_R", d.be_r),
        early_enable=_b("EARLY_ENABLE", d.early_enable),
        early_rvol=_f("EARLY_RVOL", d.early_rvol),
        early_risk_mult=_f("EARLY_RISK_MULT", d.early_risk_mult),
        fast_interval=_i("FAST_INTERVAL_SEC", d.fast_interval),
        entry_slip_r=_f("ENTRY_SLIP_R", d.entry_slip_r),
        max_hold_h=_f("MAX_HOLD_HOURS", d.max_hold_h),
        trail_tf=_s("TRAIL_TIMEFRAME", d.trail_tf),
        trail_ema=_i("TRAIL_EMA", d.trail_ema),
    )
