from __future__ import annotations

import ccxt


def make_exchange(cfg, auth: bool):
    params = {
        "enableRateLimit": True,
        "rateLimit": cfg.api_rate_limit_ms,
        "timeout": 20000,
        "options": {"defaultType": "swap"},
    }
    if auth:
        params["apiKey"] = cfg.api_key
        params["secret"] = cfg.api_secret
    ex = ccxt.bingx(params)
    if cfg.demo:
        ex.set_sandbox_mode(True)
    ex.load_markets()
    return ex
