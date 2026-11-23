from __future__ import annotations

import html
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from config import load_config
from exchange import make_exchange
from indicators import rvol
from scanner import Scanner
from strategies import adjust_score
from state import State
from telegram import Notifier
from trader import Trader, px

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("main")


def secs_to_next_slot(now: float, interval: int, offset: float = 10.0) -> float:
    """Segundos hasta el próximo múltiplo de `interval` (+offset). Alinea el escaneo con el cierre de las velas."""
    return interval - ((now - offset) % interval)


def plan_text(cfg, equity, entry: float, stop: float, mult: float = 1.0) -> str:
    """Tamaño orientativo con el mismo criterio que usa el bot (riesgo % del equity, tope de margen)."""
    risk = abs(entry - stop)
    if not equity or risk <= 0:
        return ""
    qty = equity * cfg.risk_pct / 100 * mult / risk
    cap = equity * cfg.max_margin_pct / 100 * cfg.leverage
    capped = qty * entry > cap
    if capped:
        qty = cap / entry
    return (f"Tamaño (equity ref. ${equity:,.0f}): {px(round(qty, 6))} uds ≈ ${qty * entry:,.0f} nominal · "
            f"margen ≈ ${qty * entry / cfg.leverage:,.0f} a {cfg.leverage}x · riesgo ≈ ${qty * risk:.2f}"
            + (" (limitado por MAX_MARGIN_PCT)" if capped else ""))


def fmt_signal(s, regime: str, cfg, tradable: bool = True, equity=None, after_early=None) -> str:
    long = s.side == "long"
    d = 1 if long else -1
    tp1 = s.entry + d * cfg.tp1_r * s.risk
    max_entry = s.entry + d * cfg.entry_slip_r * s.risk
    mult = cfg.early_risk_mult if s.early else (1 - cfg.early_risk_mult if after_early is not None else 1.0)
    plan = plan_text(cfg, equity, s.entry, s.stop, mult)
    head = ("⚡ <b>ANTICIPADA</b> · entra solo con "
            f"{cfg.early_risk_mult * 100:.0f}% del tamaño; el resto, si cierra la vela 15m confirmando\n"
            if s.early else
            f"✅ <b>CONFIRMA la anticipada de hace {after_early:.0f} min</b> → añade el {mult * 100:.0f}% restante "
            f"(solo si entraste con la parte anticipada)\n" if after_early is not None else "")
    return (
        head +
        f"{'🟢' if long else '🔴'} <b>{s.side.upper()} {s.symbol}</b> · {s.setup}\n"
        f"Score <b>{s.score:.0f}/100</b> | BTC: {regime}\n"
        f"Entrada ≈ {px(s.entry)} (válida mientras no pase de {px(max_entry)}; más allá, déjala pasar)\n"
        f"SL {px(s.stop)} ({s.risk / s.entry * 100:.2f}%) | TP1 {px(tp1)} ({cfg.tp1_r}R)\n"
        + (plan + "\n" if plan else "")
        + f"Plan: {cfg.partial_pct:.0f}% en TP1, stop a break-even y trailing EMA{cfg.trail_ema} {cfg.trail_tf}\n"
        + "\n".join(f"• {r}" for r in s.reasons)
        + ("" if tradable or cfg.mode == "signals"
           else f"\nℹ️ Score por debajo de MIN_SCORE ({cfg.min_score:.0f}): solo aviso, el bot no la ejecuta")
    )


def fmt_cand(c, cfg, equity=None) -> str:
    long = c.side == "long"
    d = 1 if long else -1
    tp1 = c.trigger + d * cfg.tp1_r * c.risk
    plan = plan_text(cfg, equity, c.trigger, c.stop)
    return (
        f"{'🟢' if long else '🔴'} <b>{c.symbol}</b> {c.side.upper()} · {c.setup} · score {c.score:.0f}"
        f"{' · ' + c.regime_note if c.regime_note else ''}\n"
        f"   Precio {px(c.price)} → gatillo {px(c.trigger)} (a {c.dist_pct:.2f}%)\n"
        f"   AÚN NO ENTRES. Entrada solo si una vela 15m CIERRA {'&gt;' if long else '&lt;'} {px(c.trigger)} "
        f"{'verde' if long else 'roja'} y con RVOL ≥ {cfg.rvol_min:g}x (ahora {c.rvol:.1f}x). "
        f"Una mecha o una vela sin volumen NO cuenta\n"
        f"   Si se confirma → SL ≈ {px(c.stop)} ({c.risk / c.trigger * 100:.2f}%) · TP1 ≈ {px(tp1)}. "
        f"Se anula si toca {px(c.stop)} antes de romper\n"
        + (f"   {plan}\n" if plan else "")
        + f"   ADR {c.adr:.1f}% | caja 1h {c.rng:.2f}% | previo {c.prior:.0f}%"
    )


def fmt_ict_watch(items, cfg) -> str:
    out = ["🧠 <b>ICT en vigilancia</b> (barrido hecho, aún SIN entrada)"]
    for w in items:
        long = w["side"] == "long"
        out.append(
            f"\n{'🟢' if long else '🔴'} <b>{w['symbol']}</b> {w['side'].upper()} · barrido en {w['kind']} "
            f"{px(w['zlo'])}–{px(w['zhi'])} (mecha a {px(w['ext'])})\n"
            f"   {w['why'].capitalize()}. ENTRAR solo si una vela 15m CIERRA {'&gt;' if long else '&lt;'} {px(w['level'])} "
            f"(tramo de {w['n_run']} velas) con RVOL ≥ {cfg.ict_rvol:g}x (ahora {w['rv']:.1f}x) y cierre fuerte\n"
            f"   Se anula si una vela 15m cierra {'bajo' if long else 'sobre'} {px(w['ext'])}")
    return "\n".join(out)


def fmt_watch(cands, cfg, title="👀 <b>Candidatas a explotar</b> (aún sin ruptura)", equity=None) -> str:
    return title + "\n\n" + "\n\n".join(fmt_cand(c, cfg, equity) for c in cands)


RVOL_SUSPECT = 40.0      # un RVOL 15m por encima de esto casi siempre es un dato raro de BingX, no mercado

NEAR_TITLE = "🔔 <b>Casi en el gatillo</b> (abre el gráfico 15m y espera el cierre de vela)"

GUIDE = (
    "📖 <b>Cómo usar los avisos</b>\n"
    "👀 <b>Candidata</b>: SOLO vigilar. Pon una alerta de precio en el gatillo. No se entra todavía.\n"
    "🔔 <b>Casi en el gatillo</b>: prepárate y mira el 15m.\n"
    "⚡ <b>Anticipada</b>: una vela 5m ya cerró rompiendo el gatillo con volumen (antes del cierre 15m). "
    "Entra con la mitad del tamaño. Si luego llega ✅ CONFIRMA, añade la otra mitad; si no, el stop te protege.\n"
    "🟢/🔴 <b>Señal</b>: la vela 15m YA cerró rompiendo con volumen. Es el momento de entrar (mercado o límite) "
    "si el precio no pasó de «válida mientras no pase de».\n"
    "🧠 <b>ICT en vigilancia</b>: barrido hecho, falta el CISD: solo vigilar, con el nivel que debe cerrar la 15m.\n"
    "🧠 <b>ICT Sweep+CISD</b>: estructura 4h a favor, barrido de liquidez en 1h dentro de un POI y CISD en 15m "
    "con volumen. Entrada = cierre de la vela CISD; SL tras la mecha del barrido; objetivo = liquidez 4h.\n"
    "A +1R el stop pasa a break-even: desde ahí la operación ya no pierde capital (salvo comisiones/slippage).\n"
    "Al entrar: 1) pon el SL de la señal en el exchange a la vez; 2) en TP1 vende el parcial y sube el SL a "
    "break-even; 3) el resto con trailing bajo la EMA10 1h; 4) sal a las 24 h.\n"
    "<b>No entres</b> si: el precio ya se fue más allá del límite, la señal es contra BTC con score bajo, "
    "o ya tienes el máximo de posiciones/pérdida diaria.\n"
    "Sin backtest: trátalo como un filtro de ideas, no como una garantía."
)


def pick_alerts(res, st, cfg) -> list:
    """Señales a avisar: filtra por score y cooldown ANTES de limitar (antes un top-3 en cooldown tapaba al resto)."""
    out, seen = [], set()
    for s in res.signals:                                   # ya vienen ordenadas por score
        if s.score < cfg.alert_min_score or s.symbol in seen or st.cooldown_active(s.symbol, cfg.cooldown_min):
            continue
        seen.add(s.symbol)
        out.append(s)
    return out[: cfg.max_signals_per_scan]


def pick_early(sigs, st, cfg) -> list:
    """Anticipadas a avisar: score mínimo, sin cooldown propio y sin señal 15m ya enviada para esa moneda."""
    out, seen = [], set()
    for s in sorted(sigs, key=lambda x: x.score, reverse=True):
        if (s.score < cfg.alert_min_score or s.symbol in seen
                or st.cooldown_active(s.symbol + "|early", cfg.cooldown_min)
                or st.cooldown_active(s.symbol, cfg.cooldown_min)):
            continue
        seen.add(s.symbol)
        out.append(s)
    return out[: cfg.max_signals_per_scan]


def pick_ict_watch(res, st, cfg) -> list:
    """Barridos ICT a avisar (una vez por ventana de re-aviso)."""
    return [w for w in res.ict_watch[: cfg.watch_max] if st.watch_due(w["key"], cfg.watch_realert_min)]


def pick_watch(res, st, cfg):
    """(casi_en_gatillo, nuevas). Una candidata casi lista avisa aunque ya se hubiera avisado más lejos."""
    cands = res.candidates[: cfg.watch_max]
    near = [c for c in cands if c.near and st.watch_due(c.key + "|near", cfg.watch_realert_min)]
    new = [c for c in cands if not c.near and st.watch_due(c.key, cfg.watch_realert_min)]
    return near, new


def fmt_digest(res, cfg) -> str:
    """Resumen del último escaneo: prueba de vida + por qué no hay señales."""
    if res is None:
        return "Aún no hay ningún escaneo completado."
    st = res.stats
    age = max(0, int((time.time() - res.ts) / 60))
    out = [
        f"🩺 <b>Escáner</b> · último escaneo hace {age} min (duró {res.secs:.0f}s)",
        f"BTC: {res.regime} | Líquidos: {res.universe} | Analizados: {st['analizados']}",
        f"Filtro diario OK: {st['filtro diario ok']} | Cajas 1h válidas: {st['cajas de consolidación válidas']} | "
        f"Candidatas: {len(res.candidates)} | Señales: {len(res.signals)}",
    ]
    if res.candidates:
        out.append("\n<b>Top candidatas</b> (/watchlist para el detalle):")
        out += [f"• {c.symbol} {c.side.upper()} gatillo {px(c.trigger)} (a {c.dist_pct:.2f}%)"
                for c in res.candidates[:5]]
    if res.far:
        out.append("\n<b>Cajas válidas aún lejos del gatillo</b>:")
        def _ctr(side):
            return " ⚠️ contra BTC" if (side == "short" and res.regime == "bull") or (side == "long" and res.regime == "bear") else ""
        out += [f"• {s} {side.upper()} gatillo {px(t)} (a {d:.2f}%){_ctr(side)}" for s, side, d, t in res.far[:3]]
    if res.movers:
        out.append("\n<b>Con volumen ahora</b> (RVOL 15m · %24h → por qué no es señal):")
        out += [f"• {s} {rv:.1f}x · {p:+.1f}% → {why}"
                + (f" ⚠️ RVOL absurdo: verifica con /debug {s.split('/')[0]}" if rv > RVOL_SUSPECT else "")
                for s, rv, p, why in res.movers[:5]]
    if st["ICT: con volumen"]:
        out.append(f"\n<b>ICT 4h→1h→15m</b>: con volumen {st['ICT: con volumen']} → sesgo 4h {st['ICT: sesgo 4h']} → "
                   f"POI {st['ICT: POI']} → barrido {st['ICT: barrido en POI']} → CISD {st['ICT: CISD 15m']} → "
                   f"señales {st['ICT: señales']}")
    if res.ict_watch:
        out.append("\n<b>🧠 ICT esperando CISD</b> (/ict para el detalle):")
        out += [f"• {w['symbol']} {w['side'].upper()} cerrar 15m {'>' if w['side'] == 'long' else '<'} {px(w['level'])} "
                f"(RVOL ahora {w['rv']:.1f}x)" for w in res.ict_watch[:4]]
    rs = res.reasons(5)
    if rs:
        out.append("\n<b>Por qué se descartan las demás:</b>")
        out += [f"• {k}: {v}" for k, v in rs]
    n_retry = st["peticiones reintentadas por límite de BingX"]
    if n_retry:
        out.append(f"\n⏳ BingX limitó {n_retry} peticiones (el bot las reintentó con espera)")
    if st["errores de datos"]:
        out.append(f"\n⚠️ Errores de datos en {st['errores de datos']} símbolos. Ej.: "
                   + html.escape(res.errors[0] if res.errors else ""))
    return "\n".join(out)


def fmt_debug(scanner, sym: str) -> str:
    """Muestra qué volumen está leyendo el bot de BingX para un símbolo (para validar el RVOL)."""
    if not sym:
        return "Uso: /debug TRX   (o /debug TRX/USDT:USDT)"
    s = sym.upper()
    if "/" not in s:
        s += "/USDT:USDT"
    try:
        m = scanner._df(s, "15m", 130).iloc[:-1]
    except Exception as e:
        return f"No pude leer {html.escape(s)}: {html.escape(str(e))[:200]}"
    v = m["volume"]
    base = v.iloc[-21:-1]
    lines = [
        f"🔎 <b>{html.escape(s)}</b> · velas 15m cerradas",
        f"Volumen última: {v.iloc[-1]:.6g} | media 20 previas: {base.mean():.6g} | "
        f"mediana: {base.median():.6g} | mín: {base.min():.6g}",
        f"RVOL calculado: {rvol(m, 20):.1f}x",
    ]
    for _, r in m.tail(6).iterrows():
        hhmm = datetime.fromtimestamp(r["ts"] / 1000, timezone.utc).strftime("%H:%M")
        lines.append(f"• {hhmm} UTC  cierre {px(r['close'])}  vol {r['volume']:.6g}")
    return "\n".join(lines)


def fmt_day(prev: dict) -> str:
    return (f"📊 <b>Resumen {prev['date']}</b>\nOperaciones: {prev['trades']} | "
            f"PnL realizado: ${prev['realized']:+.2f}")


def main():
    cfg = load_config()
    if cfg.mode == "live" and not (cfg.api_key and cfg.api_secret):
        raise SystemExit("TRADING_MODE=live requiere BINGX_API_KEY y BINGX_API_SECRET")

    st = State(cfg.data_dir, cfg.paper_equity)
    tg = Notifier(cfg)
    scan_ex = make_exchange(cfg, auth=False)                 # datos públicos, sin claves
    fast_ex = make_exchange(cfg, auth=False)                 # instancia propia: ccxt no es thread-safe
    trade_ex = make_exchange(cfg, auth=cfg.mode == "live")
    scanner = Scanner(scan_ex, cfg)
    fast = Scanner(fast_ex, cfg)
    trader = Trader(trade_ex, cfg, st, tg.send)
    stop = threading.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    last = {"res": None, "digest": 0.0}

    def ref_equity() -> float:
        try:
            return float(trader.equity())
        except Exception:
            return float(cfg.paper_equity)

    def scan_loop():
        while not stop.is_set():
            t0 = time.time()
            try:
                res = scanner.scan()
                last["res"] = res

                # 1) Señales: ruptura confirmada (aviso desde ALERT_MIN_SCORE; ejecuta desde MIN_SCORE)
                for s in pick_alerts(res, st, cfg):
                    tradable = s.score >= cfg.min_score
                    tg.send(fmt_signal(s, res.regime, cfg, tradable, ref_equity(),
                                       after_early=st.early_age_min(s.symbol)))
                    st.set_cooldown(s.symbol)
                    if cfg.mode != "signals" and tradable:
                        ok, why = trader.open(s)
                        if not ok:
                            tg.send(f"⏭ {s.symbol} no ejecutada: {why}")

                # 2) Candidatas: consolidando cerca del gatillo (solo las nuevas)
                iw = pick_ict_watch(res, st, cfg)
                if iw:
                    tg.send(fmt_ict_watch(iw, cfg))
                    st.watch_mark([w["key"] for w in iw])
                near, new = pick_watch(res, st, cfg)
                if near:
                    tg.send(fmt_watch(near, cfg, NEAR_TITLE, ref_equity()))
                    st.watch_mark([k for c in near for k in (c.key, c.key + "|near")])
                if new:
                    tg.send(fmt_watch(new, cfg, equity=ref_equity()))
                    st.watch_mark([c.key for c in new])

                # 3) Resumen periódico (prueba de vida + embudo de descartes)
                if cfg.digest_min > 0 and time.time() - last["digest"] >= cfg.digest_min * 60:
                    tg.send(fmt_digest(res, cfg))
                    last["digest"] = time.time()
            except Exception as e:
                log.exception("scan error")
                tg.alert_once("scan", f"⚠️ Error en el escaneo: {html.escape(str(e))[:300]}")
            stop.wait(max(5, secs_to_next_slot(time.time(), cfg.scan_interval)))   # alineado al cierre de vela

    def fast_loop():
        """Vigila las candidatas cada FAST_INTERVAL_SEC: ruptura confirmada en 5m = entrada anticipada."""
        while not stop.is_set():
            try:
                res = last["res"]
                if cfg.early_enable and res is not None and res.candidates:
                    sigs = fast.early_signals(res.candidates[: cfg.watch_max])
                    for s in sigs:
                        adjust_score(s, res.regime, fast.funding(s.symbol))
                    for s in pick_early(sigs, st, cfg):
                        tradable = s.score >= cfg.min_score
                        tg.send(fmt_signal(s, res.regime, cfg, tradable, ref_equity()))
                        st.set_cooldown(s.symbol + "|early")
                        st.early_mark(s.symbol)
                        if cfg.mode != "signals" and tradable:
                            ok, why = trader.open(s)
                            if not ok:
                                tg.send(f"⏭ {s.symbol} anticipada no ejecutada: {why}")
            except Exception as e:
                log.exception("fast error")
                tg.alert_once("fast", f"⚠️ Error en la vigilancia rápida: {html.escape(str(e))[:300]}")
            stop.wait(cfg.fast_interval)

    def manage_loop():
        while not stop.is_set():
            try:
                prev = st.ensure_day(trader.equity)
                if prev:
                    tg.send(fmt_day(prev))
                trader.manage()
            except Exception as e:
                log.exception("manage error")
                tg.alert_once("manage", f"⚠️ Error gestionando posiciones: {html.escape(str(e))[:300]}")
            stop.wait(cfg.manage_interval)

    def cmd_pause():
        st.data["paused"] = True
        st.save()
        return "⏸ Bot en pausa: no abrirá nuevas operaciones (las abiertas se siguen gestionando)."

    def cmd_resume():
        st.data["paused"] = False
        st.save()
        return "▶️ Bot reanudado."

    def cmd_closeall():
        return f"🧹 Posiciones cerradas: {trader.close_all()}"

    def cmd_ict():
        res = last["res"]
        if res is None:
            return "Aún no hay ningún escaneo completado."
        if not res.ict_watch:
            return "ICT: ningún barrido pendiente de CISD ahora mismo. /scan muestra el embudo."
        return fmt_ict_watch(res.ict_watch[: cfg.watch_max], cfg)

    def cmd_watchlist():
        res = last["res"]
        if res is None:
            return "Aún no hay ningún escaneo completado (el primero tarda 1-2 min)."
        if not res.candidates:
            return "Sin candidatas ahora mismo. Usa /scan para ver por qué."
        return fmt_watch(res.candidates[: cfg.watch_max], cfg, "👀 <b>Candidatas ahora</b>", ref_equity())

    handlers = {
        "/watchlist": cmd_watchlist,
        "/scan": lambda: fmt_digest(last["res"], cfg),
        "/debug": lambda sym="": fmt_debug(scanner, sym),
        "/status": trader.summary,
        "/positions": trader.positions_text,
        "/pause": cmd_pause,
        "/resume": cmd_resume,
        "/closeall": cmd_closeall,
        "/ict": cmd_ict,
        "/guia": lambda: GUIDE,
        "/help": lambda: "/guia /ict /watchlist /scan /debug SIMBOLO /status /positions /pause /resume /closeall",
    }

    prev = st.ensure_day(trader.equity)
    if not tg.enabled:
        log.error("TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID vacíos: NO se enviará nada a Telegram")
    started = tg.send(
        f"🤖 <b>Qullamaggie-BingX bot iniciado</b>\n"
        f"Modo: <b>{cfg.mode.upper()}</b>{' (DEMO)' if cfg.demo else ''} | Escaneo cada {cfg.scan_interval}s\n"
        f"Riesgo/op {cfg.risk_pct}% | Lev {cfg.leverage}x | Score mín {cfg.min_score:.0f}\n"
        f"Avisa señales desde score {cfg.alert_min_score:.0f}"
        f"{'' if cfg.mode == 'signals' else f' (ejecuta desde {cfg.min_score:.0f})'} y candidatas a ≤{cfg.watch_dist_pct}% del gatillo\n"
        f"Comandos: /guia /watchlist /scan /status /positions /pause /resume /closeall"
    )
    if tg.enabled and not started:
        log.error("El mensaje de arranque NO llegó a Telegram: revisa token, chat_id y haber pulsado /start en el bot")
    for fn in (scan_loop, fast_loop, manage_loop, lambda: tg.listen(handlers, stop)):
        threading.Thread(target=fn, daemon=True).start()
    stop.wait()
    log.info("Apagando...")


if __name__ == "__main__":
    main()
