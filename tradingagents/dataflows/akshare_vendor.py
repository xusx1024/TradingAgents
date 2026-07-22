"""A-share data vendor — multi-source with fallback chain.

Yahoo Finance has poor coverage for Chinese A-shares. This module provides
A-share OHLCV via three independent sources, tried in order:

  1. Tencent  (``web.ifzq.gtimg.cn``) — fastest, most reliable, 日线
  2. akshare  (东方财富)                — richer data but unstable
  3. Sina     (placeholder for future)

All return the same DataFrame schema (Date, Open, High, Low, Close, Volume)
so downstream consumers work unchanged.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime

import pandas as pd
import requests

from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

# Tickers ending in these suffixes are routed to akshare.
_A_SHARE_SUFFIXES = (".SS", ".SZ", ".BJ")

# 6-digit numeric tickers without a suffix are also A-shares.
_A_SHARE_BARE = re.compile(r"^\d{6}$")

# Cache window: 10 years from analysis date.
_CACHE_YEARS = 10


def _is_a_share(symbol: str) -> bool:
    """True when ``symbol`` is a Chinese A-share (SSE, SZSE, or BJ)."""
    upper = symbol.upper().strip()
    return bool(_A_SHARE_BARE.match(upper)) or upper.endswith(_A_SHARE_SUFFIXES)


def _to_akshare_symbol(symbol: str) -> str:
    """Normalise '600378.SS' / '000001.SZ' → akshare format '600378' / '000001'."""
    upper = symbol.upper().strip()
    for suffix in _A_SHARE_SUFFIXES:
        if upper.endswith(suffix):
            return upper[: -len(suffix)]
    return upper


def _load_ohlcv_tencent(
    clean_symbol: str,
    curr_dt: pd.Timestamp,
    cache_file: str | None,
    symbol: str,
) -> pd.DataFrame | None:
    """Tencent 日线 API——A 股首选数据源，独立于东方财富。"""
    exchange = "sz" if clean_symbol.startswith(("0", "3")) else "sh"
    param = f"{exchange}{clean_symbol},day,,,320,qfq"
    url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={param}"

    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=10)
            data = resp.json()
            klines = (
                data.get("data", {}).get(f"{exchange}{clean_symbol}", {}).get("qfqday")
                or data.get("data", {}).get(f"{exchange}{clean_symbol}", {}).get("day")
            )
            if not klines:
                return None

            # Tencent format: [date, open, close, high, low, volume]
            rows = []
            for k in klines:
                rows.append({
                    "Date": pd.to_datetime(k[0]),
                    "Open": float(k[1]),
                    "Close": float(k[2]),
                    "High": float(k[3]),
                    "Low": float(k[4]),
                    "Volume": float(k[5]),
                })
            df = pd.DataFrame(rows)
            df = df[df["Date"] <= curr_dt]
            df = df.dropna(subset=["Close"])
            if df.empty:
                return None

            if cache_file:
                df.to_csv(cache_file, index=False, encoding="utf-8")

            logger.info("Fetched %s via Tencent", symbol)
            return df
        except Exception:
            time.sleep(1 * (attempt + 1))

    return None


def load_ohlcv_akshare(
    symbol: str,
    curr_date: str,
    cache_dir: str | None = None,
) -> pd.DataFrame | None:
    """Fetch A-share OHLCV from akshare, with CSV caching.

    Returns a DataFrame with columns ``Date``, ``Open``, ``High``, ``Low``,
    ``Close``, ``Volume`` — the same schema as the yfinance path so stockstats
    and the verified-market-snapshot pipeline can consume it unchanged.

    Returns ``None`` when akshare returns no rows (symbol unknown / delisted).
    """
    if not _is_a_share(symbol):
        return None  # Not our jurisdiction; let yfinance handle it.

    clean_symbol = _to_akshare_symbol(symbol)
    safe_sym = safe_ticker_component(clean_symbol)

    curr_dt = pd.to_datetime(curr_date)
    start_dt = curr_dt - pd.DateOffset(years=_CACHE_YEARS)
    start_str = start_dt.strftime("%Y%m%d")
    end_str = curr_dt.strftime("%Y%m%d")

    # Cache one file per A-share symbol.
    cache_file: str | None = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_file = os.path.join(
            cache_dir,
            f"{safe_sym}-akshare-data-{start_str}-{end_str}.csv",
        )
        if os.path.exists(cache_file):
            cached = pd.read_csv(cache_file, on_bad_lines="skip", encoding="utf-8")
            if not cached.empty and "Close" in cached.columns:
                cached["Date"] = pd.to_datetime(cached["Date"], errors="coerce")
                cached = cached[cached["Date"] <= curr_dt]
                if not cached.empty:
                    return cached

    # --- Source 1: Tencent (fastest, most stable, independent of 东方财富) ---
    df = _load_ohlcv_tencent(clean_symbol, curr_dt, cache_file, symbol)
    if df is not None:
        return df

    # --- Source 2: akshare / 东方财富 ---
    import akshare as ak

    for attempt in range(3):
        try:
            df = ak.stock_zh_a_hist(
                symbol=clean_symbol, period="daily",
                start_date=start_str, end_date=end_str, adjust="qfq",
            )
            if df is not None and not df.empty:
                break
            df = None
        except Exception:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    else:
        logger.warning("All A-share sources failed for %r", symbol)
        return None

    if df is None or df.empty:
        return None

    # Normalise columns to yfinance convention.
    col_map = {
        "日期": "Date", "开盘": "Open", "最高": "High",
        "最低": "Low", "收盘": "Close", "成交量": "Volume",
    }
    df = df.rename(columns=col_map)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df[df["Date"] <= curr_dt].sort_values("Date")

    if not df.empty and cache_file:
        df.to_csv(cache_file, index=False, encoding="utf-8")

    return df if not df.empty else None


def get_akshare_stock_data(
    symbol: str,
    start_date: str,
    end_date: str,
) -> str:
    """Vendor-interface wrapper — returns the same CSV string format as yfinance.

    This is the entry point registered in ``interface.VENDOR_METHODS`` for the
    ``akshare`` vendor key, so the existing ``route_to_vendor`` machinery can
    dispatch to it without changes.
    """
    df = load_ohlcv_akshare(symbol, end_date)

    if df is None or df.empty:
        from .symbol_utils import NoMarketDataError

        raise NoMarketDataError(
            symbol, symbol, "akshare returned no rows (symbol may be invalid or delisted)"
        )

    # Filter to the requested date range.
    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)
    df = df[(df["Date"] >= start_dt) & (df["Date"] <= end_dt)]

    if df.empty:
        from .symbol_utils import NoMarketDataError

        raise NoMarketDataError(
            symbol,
            symbol,
            f"akshare: no rows in range {start_date}–{end_date}",
        )

    numeric_cols = ["Open", "High", "Low", "Close"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = df[col].astype(float).round(2)

    csv_string = df.to_csv(index=False)
    header = (
        f"# Stock data for {symbol} (via akshare) from {start_date} to {end_date}\n"
        f"# Total records: {len(df)}\n"
        f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    )
    return header + csv_string
