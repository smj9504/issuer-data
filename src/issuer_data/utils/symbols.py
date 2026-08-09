"""Canonical symbol normalization per market.

Different sources spell the same listing differently (HKEXnews '00700' vs Yahoo
'0700.HK'; KRX '005930' vs Yahoo '005930.KS'). We store ONE canonical symbol per
market so every source resolves to the same security:
  * US: uppercase ticker as-is (AAPL)
  * KR: 6-digit numeric code, no suffix (005930)
  * HK: 5-digit numeric code, no suffix (00700)
"""

from __future__ import annotations


def normalize_symbol(market: str, symbol: str) -> str:
    market = (market or "").upper()
    s = (symbol or "").strip().upper()
    if market == "KR":
        s = s.replace(".KS", "").replace(".KQ", "")
        digits = "".join(ch for ch in s if ch.isdigit())
        return digits.zfill(6) if digits else s
    if market == "HK":
        s = s.replace(".HK", "")
        digits = "".join(ch for ch in s if ch.isdigit())
        return digits.zfill(5) if digits else s
    return s
