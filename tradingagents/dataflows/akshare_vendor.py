"""A-share data vendor via akshare.

Yahoo Finance has poor coverage for Chinese A-shares (SSE/SZSE).
akshare is the fallback provider for tickers ending in ``.SS`` / ``.SZ``
and for 6-digit numeric tickers without a suffix.

OHLCV data is fetched via :func:`akshare.stock_zh_a_hist` and returned in
the same schema as the yfinance path (Date, Open, High, Low, Close, Volume),
so downstream consumers (stockstats, market_data_validator) work unchanged.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime

import pandas as pd

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

    import akshare as ak

    max_retries = 3
    last_exc = None
    for attempt in range(max_retries):
        try:
            df = ak.stock_zh_a_hist(
                symbol=clean_symbol,
                period="daily",
                start_date=start_str,
                end_date=end_str,
                adjust="qfq",  # 前复权
            )
            break
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = 2 * (attempt + 1)
                logger.warning(
                    "akshare fetch failed for %r (attempt %d/%d), retrying in %ds: %s",
                    symbol, attempt + 1, max_retries, delay, exc,
                )
                time.sleep(delay)
    else:
        logger.warning("akshare fetch failed for %r after %d attempts: %s", symbol, max_retries, last_exc)
        return None

    if df is None or df.empty:
        return None

    # Normalise column names to match yfinance → stockstats convention.
    col_map = {
        "日期": "Date",
        "开盘": "Open",
        "最高": "High",
        "最低": "Low",
        "收盘": "Close",
        "成交量": "Volume",
    }
    df = df.rename(columns=col_map)

    # Ensure required columns exist.
    required = ["Date", "Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        logger.warning("akshare returned incomplete columns for %r: missing %s", symbol, missing)
        return None

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df[df["Date"] <= curr_dt]
    df = df.sort_values("Date")

    if df.empty:
        return None

    # Cache for reuse.
    if cache_file:
        df.to_csv(cache_file, index=False, encoding="utf-8")

    return df


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
