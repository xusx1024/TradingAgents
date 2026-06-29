"""
陈小群风格 · 明日连板候选筛选器

按 SKILL.md 心智模型筛票：
  模型1（情绪周期）→ 环境过滤，退潮期直接空仓
  模型2（龙头信仰）→ 只做主线，只做连板龙
  模型5（市场合力）→ 换手充分、封板质量高
  模型4（预期差）   → 分歧转一致优先，炸板回封 > 一字硬顶

输入：今天日期（YYYYMMDD）
输出：最多 3 只概率最高的连板候选，0 只即"今天不值得出手"
"""

from __future__ import annotations

import sys
from datetime import datetime

import akshare as ak
import pandas as pd


# ---------------------------------------------------------------------------
# 配置（可按市场风格调整）
# ---------------------------------------------------------------------------

# 退潮判定阈值
BEAR_MIN_LIMIT_UPS = 40       # 涨停数低于此 → 退潮
BEAR_MAX_LIMIT_DOWNS = 80     # 跌停数高于此 → 退潮
BEAR_MAX_HIGH_BOARD = 2       # 最高连板 ≤ 此 → 空间打不开，偏退潮

# 连板候选硬门槛
MIN_CONSECUTIVE_BOARDS = 2    # 至少 2 连板（已经证明过自己）
MIN_TURNOVER_PCT = 3.0        # 换手率下限（一字板没换手不算合力）
MAX_TURNOVER_PCT = 28.0       # 换手率上限（死亡换手）
MAX_BOARD_BREAKS = 2          # 炸板次数上限（≤2 才及格）
MAX_MARKET_CAP = 500          # 总市值上限（亿），大盘子不是游资菜

# 封板时间：硬门槛放宽，改为打分权重
# 午后封板的票不直接淘汰，但在评分中大幅扣分
MAX_FIRST_SEAL_TIME = 145959  # 极端情况：最晚 15:00

# 主线识别
TOP_SECTOR_COUNT = 6          # 涨停数前 N 的行业算主线

# 最低总分阈值（避免把质量太差的后排交出去）
MIN_SCORE = 30

# 输出数量
MAX_PICKS = 3


# ---------------------------------------------------------------------------
# 数据获取
# ---------------------------------------------------------------------------

def fetch_data(trade_date: str) -> dict:
    """拉取涨停池、跌停池、行业排名。板块数据失败不阻塞主流程。"""
    import time as _time

    zt = ak.stock_zt_pool_em(date=trade_date)

    dt = pd.DataFrame()
    try:
        dt = ak.stock_zt_pool_dtgc_em(date=trade_date)
    except Exception:
        pass  # 跌停池拿不到也继续

    sector = pd.DataFrame()
    for attempt in range(3):
        try:
            sector = ak.stock_board_industry_name_em()
            break
        except Exception:
            _time.sleep(2)
    # fallback: 如果板块接口实在拿不到，用涨停池的行业分布作为代理
    if sector.empty:
        sector = (
            zt.groupby("所属行业")
            .agg(涨停数=("代码", "count"))
            .reset_index()
            .rename(columns={"所属行业": "板块名称"})
        )
        sector["涨跌幅"] = 0.0  # 无法获知

    return {"zt": zt, "dt": dt, "sector": sector}


# ---------------------------------------------------------------------------
# 模型1：情绪周期 → 环境判断
# ---------------------------------------------------------------------------

def environment_check(zt: pd.DataFrame, dt: pd.DataFrame) -> tuple[str, bool, dict]:
    """返回 (阶段名, 是否可操作)。退潮期 → 不可操作。"""
    n_zt = len(zt)
    n_dt = len(dt)
    max_board = int(zt["连板数"].max()) if "连板数" in zt.columns else 1
    n_lb2 = int((zt["连板数"] >= 2).sum()) if "连板数" in zt.columns else 0

    if n_zt < BEAR_MIN_LIMIT_UPS or n_dt > BEAR_MAX_LIMIT_DOWNS or max_board <= BEAR_MAX_HIGH_BOARD:
        phase = "退潮期"
        tradeable = False
    elif max_board >= 4 and n_lb2 >= 10:
        phase = "高潮期"
        tradeable = True
    elif max_board >= 3 and n_lb2 >= 5:
        phase = "发酵期"
        tradeable = True
    else:
        phase = "启动期"
        tradeable = True

    return phase, tradeable, {
        "涨停数": n_zt, "跌停数": n_dt, "最高连板": max_board,
        "连板≥2": n_lb2,
    }


# ---------------------------------------------------------------------------
# 模型2：主线识别
# ---------------------------------------------------------------------------

def identify_mainlines(zt: pd.DataFrame) -> set[str]:
    """涨停家数最多的前 N 个行业视为主线。"""
    sector_counts = zt["所属行业"].value_counts()
    top_sectors = sector_counts.head(TOP_SECTOR_COUNT)
    return set(top_sectors.index.tolist())


# ---------------------------------------------------------------------------
# 候选筛选（模型2+4+5）
# ---------------------------------------------------------------------------

def score_candidates(zt: pd.DataFrame, mainlines: set[str]) -> pd.DataFrame:
    """在涨停池中按龙头标准筛选 + 打分，返回排序后的候选。"""
    df = zt.copy()

    # --- 硬过滤 ---
    # 连板数
    df = df[df["连板数"] >= MIN_CONSECUTIVE_BOARDS]
    # 换手率区间
    df = df[(df["换手率"] >= MIN_TURNOVER_PCT) & (df["换手率"] <= MAX_TURNOVER_PCT)]
    # 炸板次数
    df = df[df["炸板次数"] <= MAX_BOARD_BREAKS]
    # 封板时间（转成数值）
    df["封板时间数值"] = pd.to_numeric(df["首次封板时间"], errors="coerce").fillna(140000)
    df = df[df["封板时间数值"] <= MAX_FIRST_SEAL_TIME]
    # 总市值（亿）
    df["总市值_亿"] = pd.to_numeric(df["总市值"], errors="coerce").fillna(0) / 1e8
    df = df[df["总市值_亿"] <= MAX_MARKET_CAP]

    if df.empty:
        return df

    # --- 主线标记（硬过滤：非主线直接淘汰） ---
    df["in_mainline"] = df["所属行业"].isin(mainlines)
    # 若主线内无候选，放宽为「行业内有 >=3 家涨停」算新共识方向
    if not df["in_mainline"].any():
        sector_zt_count = zt["所属行业"].value_counts()
        emerging = set(sector_zt_count[sector_zt_count >= 3].index)
        df["in_mainline"] = df["所属行业"].isin(emerging)

    df = df[df["in_mainline"]]

    if df.empty:
        return df

    # --- 打分 ---
    # 连板数：越高越好（权重 30），3板=30分，2板=20分
    df["score_board"] = df["连板数"] * 10

    # 封板时间：越早越好（权重 25）
    #   09:30 封 = 25分，10:30 封 = 15分，13:00 封 = 5分，14:00 封 = 0分
    df["封板分钟"] = (df["封板时间数值"].astype(int) - 93000) / 100
    df["封板分钟"] = df["封板分钟"].clip(lower=0)
    df["score_time"] = (25 - df["封板分钟"] * 0.3).clip(lower=0)

    # 换手率：5-20%最佳（权重 15），峰值12%
    df["score_turnover"] = 15 - abs(df["换手率"] - 12) * 0.75
    df["score_turnover"] = df["score_turnover"].clip(lower=0)

    # 炸板次数：0次=20分，1次=10分，2次=0分（权重 20）
    df["score_break"] = (2 - df["炸板次数"]) * 10
    df["score_break"] = df["score_break"].clip(lower=0)

    # 封板资金强度（封板资金/成交额，权重 10）
    df["成交额"] = pd.to_numeric(df["成交额"], errors="coerce").fillna(1)
    df["封板资金_数值"] = pd.to_numeric(df["封板资金"], errors="coerce").fillna(0)
    df["封板强度"] = (df["封板资金_数值"] / df["成交额"] * 100).clip(lower=0, upper=50)
    df["score_seal"] = (df["封板强度"] / 5).clip(lower=0, upper=10)

    df["总分"] = (
        df["score_board"]
        + df["score_time"]
        + df["score_turnover"]
        + df["score_break"]
        + df["score_seal"]
    )

    df = df[df["总分"] >= MIN_SCORE]
    return df.sort_values("总分", ascending=False)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run(trade_date: str | None = None) -> dict:
    """返回 {"phase": str, "env": dict, "picks": list[dict], "reason": str}。"""
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y%m%d")

    data = fetch_data(trade_date)
    zt, dt, sector = data["zt"], data["dt"], data["sector"]

    phase, tradeable, env = environment_check(zt, dt)

    if not tradeable:
        return {
            "phase": phase,
            "env": env,
            "picks": [],
            "reason": f"退潮期——涨停{env['涨停数']}家，跌停{env['跌停数']}家，最高{env['最高连板']}板。会空仓的才是祖师爷，今天不买。",
        }

    mainlines = identify_mainlines(zt)
    scored = score_candidates(zt, mainlines)

    if scored.empty:
        return {
            "phase": phase,
            "env": env,
            "picks": [],
            "reason": f"{phase}——涨停{env['涨停数']}家，最高{env['最高连板']}板。但主线里没有符合龙头标准的票。没看懂就不做，不做不会死。",
        }

    top = scored.head(MAX_PICKS)
    picks = []
    for _, r in top.iterrows():
        picks.append({
            "代码": r["代码"],
            "名称": r["名称"],
            "连板数": int(r["连板数"]),
            "所属行业": r["所属行业"],
            "涨跌幅": float(r["涨跌幅"]),
            "换手率": float(r["换手率"]),
            "封板资金": f'{r["封板资金"]/1e8:.2f}亿',
            "首次封板时间": r["首次封板时间"],
            "炸板次数": int(r["炸板次数"]),
            "总分": round(float(r["总分"]), 1),
        })

    # Build sector summary
    if "涨跌幅" in sector.columns and not (sector["涨跌幅"] == 0).all():
        sector_summary = sector.sort_values("涨跌幅", ascending=False).head(TOP_SECTOR_COUNT)
        mainline_names = ", ".join(
            [f'{r["板块名称"]}({r["涨跌幅"]:.1f}%)' for _, r in sector_summary.iterrows()]
        )
    else:
        mainline_names = ", ".join(list(mainlines)[:TOP_SECTOR_COUNT])

    reason = (
        f"{phase}——涨停{env['涨停数']}家，跌停{env['跌停数']}家，"
        f"最高{env['最高连板']}板，连板晋级{env['连板≥2']}家。"
        f"主线：{mainline_names}。"
        f"符合龙头标准的{len(scored)}只，给前{len(picks)}只。"
    )

    # Quality flags
    avg_score = top["总分"].mean()
    late_seal = (top["封板时间数值"].astype(int) > 110000).all()
    all_first_board = (top["连板数"] == 2).all()

    quality_note = ""
    if avg_score < 50:
        quality_note = "所有候选评分偏低（<50），确定性不足。"
    if late_seal:
        quality_note += " 全部午后封板，不是早盘合力选出来的，明天溢价空间有限。"
    if all_first_board:
        quality_note += " 全部是首板→2板，没有3板以上的高标，空间没打开。"
    if not quality_note:
        quality_note = "候选质量正常。"

    return {"phase": phase, "env": env, "picks": picks, "reason": reason, "quality_note": quality_note.strip()}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    result = run(date_arg)
    print(f"情绪周期: {result['phase']}")
    print(f"市场环境: {result['env']}")
    print()
    if result["picks"]:
        for i, p in enumerate(result["picks"], 1):
            print(f"#{i} {p['代码']} {p['名称']} 连板{p['连板数']} | {p['所属行业']} | "
                  f"换手{p['换手率']:.1f}% | 封板{p['首次封板时间']} | "
                  f"炸板{p['炸板次数']}次 | 评分{p['总分']}")
        print(f"\n⚠️ 质量警告: {result['quality_note']}")
    else:
        print("今日无候选。")
    print()
    print(f"理由: {result['reason']}")
