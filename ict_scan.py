"""
Escáner ICT independiente (solo datos públicos de BingX, no envía nada ni opera).

  python ict_scan.py                    # los 60 perpetuos más líquidos
  python ict_scan.py --top 120
  python ict_scan.py BTC ETH SOL        # símbolos concretos
  python ict_scan.py --no-volume        # ignora la puerta de volumen: ve qué patrones ICT existen y cuáles
                                        # descarta solo el volumen (útil para calibrar ICT_RVOL)
"""
from __future__ import annotations

import argparse
import dataclasses
from collections import Counter

from config import load_config
from exchange import make_exchange
from ict import ict_signals
from scanner import H4_TTL, HOURLY_TTL, Scanner


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="*", help="p. ej. BTC ETH (por defecto: los más líquidos)")
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--no-volume", action="store_true", help="desactiva la puerta de volumen")
    a = ap.parse_args()

    cfg = load_config()
    if a.no_volume:
        cfg = dataclasses.replace(cfg, ict_rvol=0.0, ict_vol_vs_run=0.0, ict_close_pos=0.0)
    sc = Scanner(make_exchange(cfg, auth=False), cfg)
    if a.symbols:
        syms = [s.upper() if "/" in s else f"{s.upper()}/USDT:USDT" for s in a.symbols]
    else:
        syms = [s for s, _qv, _p in sc.liquid_symbols()[: a.top]]

    stats, found = Counter(), []
    for sym in syms:
        try:
            m = sc._df(sym, "15m", 130).iloc[:-1]
            h4 = sc._get(sym, "4h", 120, H4_TTL, drop_last=True)
            h1 = sc._get(sym, "1h", 120, HOURLY_TTL, fresh=True, drop_last=True)
            found += ict_signals(sym, h4, h1, m, cfg, stats)
        except Exception as e:
            stats["errores de datos"] += 1
            print(f"! {sym}: {type(e).__name__}: {e}"[:150])

    print(f"\nAnalizados {len(syms)} símbolos | volumen {'IGNORADO' if a.no_volume else f'RVOL≥{cfg.ict_rvol:g}'}")
    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}")
    for s in sorted(found, key=lambda x: x.score, reverse=True):
        print(f"\n{'🟢' if s.side == 'long' else '🔴'} {s.side.upper()} {s.symbol} · score {s.score:.0f}")
        print(f"   entrada {s.entry:.8g} | SL {s.stop:.8g} ({s.risk / s.entry * 100:.2f}%)")
        for r in s.reasons:
            print(f"   • {r}")
    if not found:
        print("\nSin señales ICT ahora mismo (lo normal: el patrón completo es poco frecuente).")


if __name__ == "__main__":
    main()
