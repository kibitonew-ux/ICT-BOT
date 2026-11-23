# Qullamaggie · BingX Scanner & Trading Bot

Escanea **todos los perpetuos USDT de BingX** buscando movimientos explosivos intradía (largos y cortos), envía las señales a **Telegram** y, opcionalmente, opera en BingX. Pensado para desplegarse en **Railway**.

> ⚠️ **Aviso:** esto es software experimental, no asesoramiento financiero. Operar con apalancamiento puede hacerte perder todo el capital. Los parámetros no están optimizados ni validados con backtest. **Empieza en modo `signals`, luego `paper`, luego demo, y solo después `live` con poco dinero.**

## Qué detecta

| Setup | Idea (Qullamaggie adaptada a cripto) | Lado |
|---|---|---|
| **Breakout / Breakdown** | Activo con ADR alto y tendencia previa (EMA10>EMA20) que consolida en rango tenso (rango contraído, higher lows / lower highs) y rompe en 15m con volumen relativo ≥ 2x | Long / Short |
| **Episodic Pivot** | Vela 15m explosiva (≥3%) con RVOL ≥ 4x que rompe el máx/mín de 24h en un activo que no venía extendido | Long / Short |
| **Parabolic Short** | Subida ≥ 40% en 5 días y muy extendida sobre la EMA20 diaria; entra corto al perder el VWAP de la sesión | Short |

**Ideas añadidas:** score 0-100, filtro de régimen de BTC (a favor +8 / en contra −12), filtro de funding (crowding), stop técnico acotado por ATR (se descarta si queda lejos), prefiltro por volumen relativo para escanear rápido, cooldown por moneda, límites diarios de operaciones y pérdida, parcial en TP1 + break-even + trailing por EMA, salida por tiempo.

## Qué te llega a Telegram
| Mensaje | Cuándo | Qué hacer |
|---|---|---|
| 👀 **Candidatas a explotar** | Moneda en consolidación tensa a ≤ `WATCH_DIST_PCT` (1.5%) del gatillo, **todavía sin romper** | Ponla en vigilancia. Indica el nivel exacto y la condición de entrada |
| 🟢/🔴 **Señal LONG/SHORT** | Cierra una vela 15m rompiendo el nivel con volumen | Es el «cuándo entrar». Trae entrada, SL y TP1 |
| 🩺 **Resumen del escáner** | Cada `DIGEST_MIN` min (60). También con `/scan` | Prueba de vida + embudo: cuántas monedas pasan cada filtro y por qué se descartan el resto |

`ALERT_MIN_SCORE` (50) decide desde qué score se **avisa**; `MIN_SCORE` (65) decide desde cuál se **ejecuta** en paper/live. Las señales entre ambos llegan marcadas como «solo aviso».

## Cómo operar lo que envía el bot (guía rápida)
| Aviso | Qué significa | Qué haces |
|---|---|---|
| 👀 **Candidata** | Caja de consolidación válida y precio cerca del gatillo. **No hay ruptura todavía** | Nada. Pon una alerta de precio en el gatillo |
| 🔔 **Casi en el gatillo** | Candidata a ≤ `WATCH_NEAR_PCT` (0.5%) o `WATCH_NEAR_ADR`·ADR del gatillo | Abre el gráfico 15m y espera el **cierre** de la vela |
| 🟢/🔴 **Señal** | Una vela 15m **ya cerró** rompiendo el nivel, con color a favor y RVOL ≥ 2x | Es el momento de entrar (mercado o límite), si el precio no pasó de «válida mientras no pase de» (entrada + `ENTRY_SLIP_R`·R) |

**Con la señal:** (1) entra y pon a la vez el SL que indica el aviso; (2) al llegar a TP1 (1.5R) vende el 50 % y sube el SL a break-even; (3) el resto con trailing bajo la EMA10 de 1h; (4) sal a las 24 h. El aviso incluye un tamaño orientativo (riesgo `RISK_PER_TRADE_PCT` del equity de referencia `PAPER_EQUITY`).

**No entres si:** una mecha o una vela sin volumen cruza el nivel (cuenta solo el cierre); el precio ya se fue más allá del límite; la señal es contra el régimen BTC con score bajo; ya tienes el máximo de posiciones o la pérdida diaria. `/guia` en Telegram muestra este resumen.

## Sistema ICT: 4h estructura → 1h barrido en POI → 15m CISD (+ volumen)
Setup `ICT Sweep+CISD`, long y short, entrada en 15m. Reglas fijadas en código (`ict.py`):
1. **4h – sesgo:** pivotes de `ICT_SWING_N` velas. HH+HL → solo largos; LH+LL → solo cortos; rango o estructura rota → nada. El rango de referencia da premium/descuento (`ICT_DISCOUNT`: largos solo en descuento, cortos en premium).
2. **1h – POI:** Order Block (última vela contraria antes de una vela de desplazamiento ≥ `ICT_DISP_ATR`·ATR) o FVG, sin mitigar. **Barrido:** una mecha supera el mín/máx de las últimas `ICT_LIQ_BARS` velas, cierra de vuelta y la mecha cae dentro del POI. La hora en curso se agrega desde las 15m (no se espera al cierre de la vela 1h).
3. **15m – CISD:** tras un tramo de ≥2 velas en contra, una vela cierra más allá de la **apertura de la 1.ª vela del tramo**. Debe ser la última vela cerrada (o la anterior) y llegar ≤ `ICT_CISD_BARS` velas tras el barrido.
4. **Volumen (puerta dura):** RVOL de la vela CISD ≥ `ICT_RVOL` (1.5), volumen ≥ `ICT_VOL_VS_RUN`× el del tramo en contra y cierre en el `ICT_CLOSE_POS` (60%) favorable de la vela. Solo se piden las velas 4h/1h si la 15m trae volumen (ahorra peticiones). Con solo OHLCV no hay delta real: el volumen es un indicador, no una prueba.
5. **Plan:** entrada = cierre de la vela CISD; SL tras la mecha del barrido (acotado por ATR); objetivo = liquidez 4h más cercana, mínimo `ICT_MIN_RR` R. Después rige la gestión habitual (TP1, break-even, trailing).

`python ict_scan.py [--top N] [BTC ETH…] [--no-volume]` lo ejecuta suelto contra BingX; con `--no-volume` ves qué patrones existen y cuáles descarta solo el volumen. `/scan` muestra el embudo ICT.

> Las definiciones de OB, FVG, barrido y CISD tienen mucha interpretación: esta es *una* versión objetiva, **sin backtest**. Calibra con `ict_scan.py` y en modo `signals` antes de arriesgar dinero.

## Entrada anticipada y break-even
Esperar al cierre de la vela 15m cuesta de media ~7,5 min (la ruptura ocurre en cualquier punto de la vela) más el retraso del escaneo. Tres mejoras lo recortan:
1. **Escaneo alineado al cierre de vela**: el escaneo se lanza 10 s después de cada múltiplo de `SCAN_INTERVAL_SEC` (antes corría con un desfase arbitrario, ~170 s de retraso medio → ~30 s).
2. **⚡ Entrada anticipada**: cada `FAST_INTERVAL_SEC` (20 s) el bot revisa las candidatas. Si el precio vivo ya pasó el gatillo y una vela **5m cerrada** rompe en verde/rojo con RVOL ≥ `EARLY_RVOL`, avisa **antes** del cierre 15m. Entra con `EARLY_RISK_MULT` (50 %) del riesgo.
3. **✅ Confirmación**: si después cierra la 15m confirmando, el bot añade el resto del riesgo (mismo stop; en paper/live lo hace solo). Si la ruptura falla, solo pierdes la mitad.
4. **Break-even a +`BE_R` R** (1.0): desde ahí la operación no pierde capital (salvo comisiones/slippage). `BE_R=0` lo desactiva.

> Ninguna entrada es «sin riesgo» *antes* de llegar a +1R: cuanto antes se entra, más rupturas falsas. Por eso la anticipada entra con medio tamaño. El break-even lo protege después, y un stop puede ejecutarse con deslizamiento (gaps/mechas). La parte de añadir en **live** no se ha probado contra BingX: prueba en demo.

## Gestión de la operación
1. Entrada a mercado tras cerrar la vela 15m de señal.
2. Stop técnico: en **live** se coloca como orden en BingX y además lo vigila el bot. Si no se puede colocar el stop, la posición se cierra al instante.
3. Al llegar a `TP1_R` (1.5R) cierra `PARTIAL_PCT` (50%) y mueve el stop a break-even.
4. El resto sigue con trailing por EMA10 de 1h. Salida por tiempo a las 24h.

## Despliegue paso a paso

### 1. Telegram
1. Habla con **@BotFather** → `/newbot` → copia el **token**.
2. Escribe cualquier mensaje a tu bot, abre `https://api.telegram.org/bot<TOKEN>/getUpdates` y copia tu `chat.id`.

### 2. BingX (solo para paper con precios públicos no hace falta; sí para live)
- Crea una API key en BingX **sin permiso de retiro** (solo lectura + trading de futuros).
- Railway no tiene IP de salida fija en el plan básico: no actives lista blanca de IP, o usa una IP estática.
- Prueba antes con la cuenta demo (`BINGX_DEMO=true`).

### 3. GitHub
```bash
git init && git add . && git commit -m "bot inicial"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/qulla-bingx-bot.git
git push -u origin main
```
(El archivo `.env` está en `.gitignore`: **nunca subas tus claves**.)

### 4. Railway
1. **New Project → Deploy from GitHub repo** y elige el repositorio.
2. En **Variables** añade las del archivo `.env.example` (mínimo: `TRADING_MODE`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
3. **Settings → Region**: elige una región de **Europa o Asia**. BingX bloquea algunas IP de EE. UU.
4. **Volume** (recomendado): añade un volumen montado en `/data` y pon `DATA_DIR=/data` para conservar el estado entre reinicios.
5. El comando de inicio ya está en `railway.json`/`Procfile`: `python main.py`.
6. Deberías recibir en Telegram el mensaje «bot iniciado».

### Ejecución local
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edítalo
set -a && source .env && set +a
python main.py
pytest                 # tests de estrategia y gestión
```

## Comandos de Telegram
`/guia` (cómo usar los avisos) · `/watchlist` (candidatas ahora) · `/debug TRX` (qué volumen lee el bot de BingX) · `/scan` (resumen y motivos de descarte) · `/status` · `/positions` · `/pause` (no abre nuevas) · `/resume` · `/closeall` (cierra todo)

## Variables principales
| Variable | Defecto | Descripción |
|---|---|---|
| `TRADING_MODE` | `signals` | `signals`, `paper` o `live` |
| `RISK_PER_TRADE_PCT` | 0.5 | % del equity arriesgado por operación |
| `LEVERAGE` / `MAX_MARGIN_PCT` | 5 / 20 | Apalancamiento y margen máximo por operación |
| `MAX_OPEN_POSITIONS` | 3 | Posiciones simultáneas |
| `MAX_TRADES_PER_DAY` / `DAILY_LOSS_LIMIT_PCT` | 6 / 3 | Frenos diarios (UTC) |
| `MIN_SCORE` | 65 | Score mínimo para **ejecutar** (paper/live) |
| `ALERT_MIN_SCORE` | 50 | Score mínimo para **avisar** por Telegram |
| `WATCH_DIST_PCT` / `WATCH_DIST_ADR` | 1.5 / 0.25 | Candidata si está a ≤ el **mayor** de: X% o X·ADR del gatillo (escala con la volatilidad) |
| `WATCH_MAX` | 8 | Máx. candidatas mostradas |
| `API_RATE_LIMIT_MS` | 250 | Pausa mínima entre peticiones a BingX. Súbela (400-500) si ves «BingX limitó N peticiones» |
| `WATCH_REALERT_MIN` | 360 | No repite el aviso de la misma candidata antes de X min |
| `DIGEST_MIN` | 60 | Resumen periódico del escáner (0 = apagado) |
| `MIN_24H_VOLUME_USD` | 3000000 | Liquidez mínima por moneda |
| `ADR_MIN_PCT`, `RVOL_MIN`, `RUNUP_MIN_PCT`… | ver `config.py` | Parámetros de cada setup |
| `RANGE_POS_MAX` | 0.40 | Long: el precio debe estar en el 40 % superior del rango de 30d (short: inferior). `1` = desactivar. Sin esto el filtro diario lo pasaba ~94 % de las monedas |
| `BREAK_FRESH_BARS` | 3 | La ruptura vale si el nivel se superó hace ≤ N velas 15m (antes solo la 1.ª vela) |
| `WATCH_NEAR_PCT` / `WATCH_NEAR_ADR` | 0.5 / 0.06 | Umbral del aviso 🔔 «casi en el gatillo» |
| `ENTRY_SLIP_R` | 0.3 | Entrada máxima aceptable = entrada de la señal ± X·R |
| `EARLY_ENABLE` / `EARLY_RVOL` / `EARLY_RISK_MULT` | true / 2 / 0.5 | Entrada anticipada por cierre 5m; RVOL mínimo; fracción del riesgo con que entra |
| `FAST_INTERVAL_SEC` | 20 | Cada cuántos segundos se vigilan las candidatas |
| `BE_R` | 1.0 | Stop a break-even al llegar a +X R (0 = off) |
| `ICT_ENABLE` | true | Activa el sistema ICT |
| `ICT_RVOL` / `ICT_VOL_VS_RUN` / `ICT_CLOSE_POS` | 1.5 / 1.0 / 0.6 | Puerta de volumen y fuerza de cierre de la vela CISD |
| `ICT_MIN_RR` | 2.0 | Objetivo (liquidez 4h) mínimo en R |
| `ICT_DISCOUNT` | true | Largos solo en descuento, cortos solo en premium |
| `ICT_SWING_N`, `ICT_DISP_ATR`, `ICT_POI_BARS`, `ICT_LIQ_BARS`, `ICT_SWEEP_BARS`, `ICT_CISD_BARS` | 2, 1.2, 72, 12, 8, 24 | Geometría de pivotes, POI, liquidez y ventanas |
| `HEDGE_MODE` | true | Pon `false` si tu cuenta BingX es One-Way |

## Estructura
```
bot/config.py      variables de entorno        bot/strategies.py  setups, score, régimen, funding
bot/scanner.py     escaneo de todo BingX       bot/trader.py      órdenes, stops, TP1, trailing
bot/telegram.py    avisos y comandos           bot/state.py       estado persistente (JSON)
bot/main.py        hilos: escaneo/gestión/cmd  tests/             pruebas con datos sintéticos
```

## Si no te llega nada
1. ¿Recibiste el mensaje «bot iniciado»? **No** → mira los logs de Railway: `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` mal, no pulsaste **/start** en tu bot, o BingX bloquea la región (cambia a Europa/Asia) y el proceso se reinicia en bucle.
2. **Sí**, pero sin avisos → escribe `/scan`. Verás cuántas monedas pasan cada filtro y el motivo principal de descarte; con eso ajustas `ADR_MIN_PCT`, `RUNUP_MIN_PCT`, `CONSOL_RANGE_ADR`, `WATCH_DIST_PCT`…
3. En los logs, la línea `Escaneo …s: N líquidos, N analizados…` confirma que el escáner trabaja. Si aparece `Errores de datos en N símbolos`, el problema es la conexión con BingX.
4. Error `100410` / «请稍后重试» en los logs = BingX limita las peticiones (Railway comparte IP). El bot reintenta solo; si persiste, sube `API_RATE_LIMIT_MS`.
5. No uses el **mismo token de Telegram** en dos bots: ambos hacen `getUpdates` y se pisan (error 409 en los logs).

## Limitaciones conocidas
- La integración con BingX (órdenes stop, modo hedge, demo) **no se ha probado contra el exchange real**; verifícala primero en la cuenta demo con importes mínimos y revisa que cada entrada tenga su stop en BingX.
- Los símbolos y la API de BingX cambian: si una orden falla, el bot avisa por Telegram.
- El PnL en live es una estimación; el valor real está en BingX.
- Sin backtest incluido: calibra los umbrales observando las señales en modo `signals`.
