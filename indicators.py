"""Indicadores con pandas (sin dependencias extra)."""
from __future__ import annotations

import pandas as pd


def to_df(ohlcv) -> pd.DataFrame:
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.apply(pd.to_numeric, errors="coerce").dropna().reset_index(drop=True)
    df["ts"] = df["ts"].astype("int64")
    return df


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adr_pct(df: pd.DataFrame, n: int = 20) -> float:
    """Average Daily Range en %, la métrica favorita de Qullamaggie."""
    return float(((df["high"] / df["low"] - 1) * 100).tail(n).mean())


def rvol(df: pd.DataFrame, n: int = 20) -> float:
    """Volumen relativo de la última vela frente a la media de las n anteriores."""
    if len(df) < n + 1:
        return 0.0
    base = df["volume"].iloc[-n - 1 : -1].mean()
    return float(df["volume"].iloc[-1] / base) if base > 0 else 0.0


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP que se reinicia cada día UTC."""
    day = df["ts"] // 86_400_000
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]
    return pv.groupby(day).cumsum() / df["volume"].groupby(day).cumsum()
