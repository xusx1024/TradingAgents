"""陈小群框架 · 历史回测。

对历史上每个交易日跑 pick_stocks 筛选，统计次日连板率和盈亏比。
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd

sys.path.insert(0, "/home/wangwu/Workspace/tradingAgents/TradingAgents/SKILLS/chenxiaoqun/tools")
from pick_stocks import (
    environment_check,
    identify_mainlines,
    score_candidates,
)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
START_DATE = "20260101"   # 回测起始
END_DATE   = "20260630"   # 回测结束

# 次日表现定义
# "连板成功" = 次日继续涨停（>= 9.5%）
# "正收益"   = 次日涨但没涨停
# "亏损"     = 次日跌


def backtest():
    date_range = pd.date_range(START_DATE, END_DATE, freq="B")  # 交易日
    results = []

    for d in date_range:
        trade_date = d.strftime("%Y%m%d")
        try:
            zt = ak.stock_zt_pool_em(date=trade_date)
        except Exception:
            time.sleep(1)
            continue

        if zt.empty:
            continue

        try:
            dt = ak.stock_zt_pool_dtgc_em(date=trade_date)
        except Exception:
            dt = pd.DataFrame()

        phase, tradeable, env = environment_check(zt, dt)
        if not tradeable:
            results.append({"date": trade_date, "phase": phase, "picks": 0,
                           "连板成功": 0, "正收益": 0, "亏损": 0, "未交易": 0})
            continue

        mainlines = identify_mainlines(zt)
        scored = score_candidates(zt, mainlines)
        top3 = scored.head(3)

        if top3.empty:
            results.append({"date": trade_date, "phase": phase, "picks": 0,
                           "连板成功": 0, "正收益": 0, "亏损": 0, "未交易": 0})
            continue

        next_day = (d + timedelta(days=1)).strftime("%Y%m%d")
        # 找实际下一个交易日（跳过周末）
        next_d = d + timedelta(days=1)
        while next_d.weekday() >= 5:
            next_d += timedelta(days=1)
        next_trade_date = next_d.strftime("%Y%m%d")

        连板成功 = 0
        正收益 = 0
        亏损 = 0
        未交易 = 0

        for _, r in top3.iterrows():
            code = r["代码"]
            name = r["名称"]
            try:
                # 查次日涨停池
                zt_next = ak.stock_zt_pool_em(date=next_trade_date)
                if not zt_next.empty and code in zt_next["代码"].values:
                    连板成功 += 1
                else:
                    # 查次日涨跌幅（简单方法：看是否在涨停池 > 看是否大涨）
                    try:
                        hist = ak.stock_zh_a_hist(symbol=code, period="daily",
                                                  start_date=next_trade_date,
                                                  end_date=next_trade_date, adjust="qfq")
                        if not hist.empty:
                            chg = (hist["收盘"].iloc[-1] - hist["开盘"].iloc[-1]) / hist["开盘"].iloc[-1] * 100
                            if chg > 0:
                                正收益 += 1
                            elif chg < 0:
                                亏损 += 1
                            else:
                                未交易 += 1
                        else:
                            未交易 += 1
                    except Exception:
                        # Fallback: 直接用腾讯接口
                        未交易 += 1

                time.sleep(0.3)
            except Exception:
                未交易 += 1

        results.append({
            "date": trade_date, "phase": phase,
            "picks": len(top3), "连板成功": 连板成功,
            "正收益": 正收益, "亏损": 亏损, "未交易": 未交易,
        })

        if len(results) % 20 == 0:
            print(f"[{trade_date}] 已处理 {len(results)} 个交易日...", flush=True)

    # --- 汇总 ---
    df = pd.DataFrame(results)
    days_with_picks = df[df["picks"] > 0]
    total_picks = days_with_picks["picks"].sum()
    total_连板 = days_with_picks["连板成功"].sum()
    total_正 = days_with_picks["正收益"].sum()
    total_亏 = days_with_picks["亏损"].sum()
    total_未 = days_with_picks["未交易"].sum()
    total_known = total_连板 + total_正 + total_亏
    total_days = len(df)
    empty_days = len(df[df["picks"] == 0])

    print("\n" + "=" * 60)
    print("陈小群框架 · 历史回测结果")
    print("=" * 60)
    print(f"回测区间: {START_DATE} → {END_DATE}")
    print(f"交易日数: {total_days}")
    print(f"空仓天数: {empty_days} ({empty_days/total_days*100:.1f}%)")
    print(f"有候选天数: {days_with_picks.shape[0]}")
    print(f"总推荐票数: {int(total_picks)}")
    print()
    if total_known > 0:
        print(f"次日连板率: {total_连板}/{int(total_picks)} = {total_连板/total_picks*100:.1f}%")
        print(f"次日正收益: {total_正}/{int(total_picks)} = {total_正/total_picks*100:.1f}%")
        print(f"次日亏损率: {total_亏}/{int(total_picks)} = {total_亏/total_picks*100:.1f}%")
        print(f"次日有正收益比率: {(total_连板+total_正)/total_picks*100:.1f}%")
    if total_未 > 0:
        print(f"数据缺失: {int(total_未)}/{int(total_picks)}")
    print()
    phase_stats = df.groupby("phase").agg(
        天数=("picks", "count"),
        有候选天数=("picks", lambda x: (x > 0).sum()),
        连板成功=("连板成功", "sum"),
        推荐总数=("picks", "sum"),
    )
    print("按情绪周期:")
    print(phase_stats.to_string())
    print()
    print("月度统计:")
    df["month"] = df["date"].str[:6]
    monthly = df.groupby("month").agg(
        推荐数=("picks", "sum"),
        连板成功=("连板成功", "sum"),
        交易日=("picks", "count"),
    )
    monthly["命中率"] = (monthly["连板成功"] / monthly["推荐数"] * 100).fillna(0).round(1)
    print(monthly.to_string())


if __name__ == "__main__":
    backtest()
