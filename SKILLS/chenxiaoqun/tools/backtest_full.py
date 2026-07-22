"""陈小群框架 · 一年回测。

通过日线 OHLCV 重构涨停日 + 模拟筛选器，统计连板率。
"""

from __future__ import annotations

import sys
import time
import warnings
from collections import defaultdict
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")

sys.path.insert(0, "/home/wangwu/Workspace/tradingAgents/TradingAgents/SKILLS/chenxiaoqun/tools")
from pick_stocks import environment_check, identify_mainlines, score_candidates

START = "20250601"
END   = "20260701"

print(f"📊 回测区间: {START} → {END}")
print()

# --- Step 1: 收集活跃股池（最近涨停过的票） ---
print("Step 1/4: 收集活跃股票池...")
active_codes = set()
for d in pd.date_range("20260601", END, freq="B"):
    try:
        zt = ak.stock_zt_pool_em(date=d.strftime("%Y%m%d"))
        for c in zt["代码"].unique():
            active_codes.add(c)
        time.sleep(0.15)
    except Exception:
        pass
print(f"  活跃股票: {len(active_codes)} 只")
print()

# --- Step 2: 拉一年日线 ---
print("Step 2/4: 拉取日线 OHLCV...")
ohlcv_cache: dict[str, pd.DataFrame] = {}
total = len(active_codes)
for i, code in enumerate(sorted(active_codes)):
    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                start_date=START, end_date=END, adjust="qfq")
        if not df.empty:
            df["日期"] = pd.to_datetime(df["日期"])
            ohlcv_cache[code] = df
    except Exception:
        pass
    if (i + 1) % 50 == 0:
        print(f"  {i+1}/{total}...", flush=True)
    time.sleep(0.15)
print(f"  拉取成功: {len(ohlcv_cache)} 只")
print()

# --- Step 3: 重建每日涨停池 ---
print("Step 3/4: 重建涨停池...")

# 判断涨停：收盘价 >= 昨日收盘 × 1.098（主板），>= 1.198（科创/创业）
def is_limit_up(today_close: float, yesterday_close: float, code: str) -> bool:
    if yesterday_close <= 0:
        return False
    ratio = today_close / yesterday_close
    if code.startswith("30") or code.startswith("68"):
        return ratio >= 1.198  # 20% 板
    elif code.startswith("8") or code.startswith("4"):
        return ratio >= 1.298  # 30% 板
    else:
        return ratio >= 1.098  # 10% 板

# Build daily ZT pool
daily_zt: dict[str, list[dict]] = defaultdict(list)

for code, df in ohlcv_cache.items():
    if len(df) < 2:
        continue
    for i in range(1, len(df)):
        today = df.iloc[i]
        yesterday = df.iloc[i - 1]
        if is_limit_up(today["收盘"], yesterday["收盘"], code):
            date_str = today["日期"].strftime("%Y%m%d")
            # 简算换手率（成交量/流通股本，这里用总股本近似）
            # akshare 的 volume 是手数，总股本在 individual_info 里没有——跳过
            daily_zt[date_str].append({
                "代码": code,
                "收盘": today["收盘"],
                "开盘": today["开盘"],
                "最高": today["最高"],
                "最低": today["最低"],
                "成交量": today["成交量"],
                "前收": yesterday["收盘"],
                "涨幅": (today["收盘"] - yesterday["收盘"]) / yesterday["收盘"] * 100,
            })

print(f"  涵盖交易日: {len(daily_zt)} 天")
total_zt = sum(len(v) for v in daily_zt.values())
print(f"  涨停记录: {total_zt} 条")
print()

# --- Step 4: 逐日模拟回测 ---
print("Step 4/4: 模拟筛选 & 统计连板率...")

# We need 连板数 — compute by tracking consecutive limit-ups per stock
# For simplicity: just use the daily pool to mark consecutive boards
consecutive: dict[str, int] = defaultdict(int)  # code → current board count

results = []
trade_dates = sorted(daily_zt.keys())

for date_str in trade_dates:
    today_zt = daily_zt[date_str]

    # Update consecutive board counts
    today_codes = {z["代码"] for z in today_zt}
    new_consecutive = {}
    for code, cnt in consecutive.items():
        if code in today_codes:
            new_consecutive[code] = cnt + 1
        # else: board broken, reset to 0 (implicit)
    for code in today_codes:
        if code not in new_consecutive:
            new_consecutive[code] = 1
    consecutive = new_consecutive

    # Build a DataFrame that looks like the ZT pool
    rows = []
    for z in today_zt:
        code = z["代码"]
        rows.append({
            "代码": code,
            "名称": "",
            "涨跌幅": z["涨幅"],
            "换手率": 10.0,  # 无法从 OHLCV 获取，给中值
            "首次封板时间": "100000",  # 无法获取，给中值
            "炸板次数": 0,
            "连板数": consecutive.get(code, 1),
            "所属行业": "通用",  # 无法获取
            "总市值": 100e8,  # 无法获取，给中值
            "成交额": z["成交量"] * z["收盘"] * 100,  # 估算
            "封板资金": z["成交量"] * z["收盘"] * 10,  # 估算
            "涨停统计": f"{consecutive.get(code,1)}/1",
        })
    zt_df = pd.DataFrame(rows)

    # Simple environment check
    dt_df = pd.DataFrame()  # 无跌停数据
    phase, tradeable, _ = environment_check(zt_df, dt_df)

    if not tradeable:
        continue

    # We don't have real sector data, use all as one sector
    mainlines = {"通用"}

    scored = score_candidates(zt_df, mainlines)
    top3 = scored.head(3)

    if top3.empty:
        continue

    # Find next trading day
    next_dates = [nd for nd in trade_dates if nd > date_str]
    if not next_dates:
        continue
    next_date = next_dates[0]
    next_zt_codes = {z["代码"] for z in daily_zt.get(next_date, [])}

    hit = 0
    for _, r in top3.iterrows():
        if r["代码"] in next_zt_codes:
            hit += 1

    results.append({
        "date": date_str,
        "phase": phase,
        "picks": len(top3),
        "hit": hit,
    })

    if len(results) % 30 == 0:
        print(f"  {date_str}: {len(results)}天已处理...", flush=True)

# --- Summarize ---
df = pd.DataFrame(results)
total_days = len(df)
days_with_picks = df[df["picks"] > 0]
total_picks = days_with_picks["picks"].sum()
total_hits = days_with_picks["hit"].sum()
empty_days = total_days - len(days_with_picks)

print()
print("=" * 60)
print("陈小群框架 · 一年回测（日线重建）")
print("=" * 60)
print(f"回测区间: {START} → {END}")
print(f"有涨停天数: {len(trade_dates)}")
print(f"有候选天数: {len(days_with_picks)}")
print(f"空仓天数: {empty_days}")
print(f"总推荐票数: {int(total_picks)}")
print()
if total_picks > 0:
    hit_rate = total_hits / total_picks * 100
    print(f"次日连板率: {int(total_hits)}/{int(total_picks)} = {hit_rate:.1f}%")
print()

# Monthly breakdown
df["month"] = df["date"].str[:6]
monthly = df.groupby("month").agg(
    推荐数=("picks", "sum"),
    连板成功=("hit", "sum"),
    有候选天数=("picks", lambda x: (x > 0).sum()),
    总交易日=("picks", "count"),
)
monthly["连板率"] = (monthly["连板成功"] / monthly["推荐数"] * 100).fillna(0).round(1)
print("月度统计:")
print(monthly.to_string())
