#!/usr/bin/env python3
"""黄金价格监控 — SGE Au99.99 低于阈值时企业微信告警。

每分钟拉取一次上海金交所 Au99.99 现价，跌破阈值时通过企业微信 Webhook
推送告警。价格回升到阈值以上后重置告警状态，下次再跌破会重新推送。

用法:
    python gold_monitor.py                        # 阈值 868，1 分钟间隔
    python gold_monitor.py --price 860            # 自定义阈值
    python gold_monitor.py --interval 180         # 3 分钟轮询
    python gold_monitor.py --webhook <url>        # 自定义 webhook

启动:
    # 前台运行（Ctrl+C 停止）
    python scripts/gold_monitor.py

    # 后台运行
    nohup python scripts/gold_monitor.py > /tmp/gold_monitor.log 2>&1 &

停止:
    # 前台运行时直接 Ctrl+C

    # 后台运行时查找并 kill
    ps aux | grep gold_monitor | grep -v grep   # 找到 PID
    kill <PID>                                   # 优雅停止
    # 或一键停止所有 gold_monitor 进程:
    pkill -f gold_monitor.py
"""

from __future__ import annotations

import argparse
import json
import os
import warnings
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

# py_mini_racer (akshare 依赖) 仍在使用已废弃的 pkg_resources
warnings.filterwarnings("ignore", message="pkg_resources is deprecated")
import sys
import time
from datetime import datetime, timedelta

import requests

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

THRESHOLD = 868.0       # 告警阈值（元/克）
RECOVERY_MARGIN = 2.0   # 恢复缓冲区：价格需回到 threshold + margin 以上才解除告警
INTERVAL = 60  # 轮询间隔（秒）
WEBHOOK_URL = (
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    "?key=422638e1-4f68-4051-8543-0437375612da"
)

# SGE 交易时段（北京时间）
# 日盘 9:00-15:30，夜盘 20:00-次日02:30
# 非交易时段黄金价格不会更新，但仍可监控（持仓过夜场景）
TRADING_HOURS = [
    ("09:00", "15:30"),
    ("20:00", "02:30"),
]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _fetch_impl() -> tuple[float | None, str]:
    """akshare 实际调用，在独立线程中执行（避免网络挂死）。"""
    import akshare as ak

    df = ak.spot_quotations_sge()
    au = df[df["品种"] == "Au99.99"]
    if au.empty:
        return None, "未找到 Au99.99 品种"
    # 取最后 N 笔均价平滑，避免逐笔 tick 抖动导致频繁穿越阈值
    window = min(20, len(au))
    price = float(au["现价"].iloc[-window:].mean())
    update_time = str(au["更新时间"].iloc[-1])
    return price, update_time


def fetch_gold_price(timeout: int = 30) -> tuple[float | None, str]:
    """返回 (Au99.99 现价, 行情时间)，失败返回 (None, 错误描述)。

    ``timeout`` 秒后未返回则判定为网络挂死，返回错误。
    """
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_fetch_impl)
            return fut.result(timeout=timeout)
    except ImportError:
        return None, "akshare 未安装"
    except FutureTimeout:
        return None, f"akshare 调用超时（>{timeout}s），东方财富接口可能挂死"
    except Exception as exc:
        return None, f"获取失败: {exc}"


def send_webhook(text: str) -> bool:
    """发送企业微信文本消息。"""
    payload = {"msgtype": "text", "text": {"content": text}}
    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        data = resp.json()
        return data.get("errcode") == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

def run(threshold: float = THRESHOLD, interval: int = INTERVAL):
    print(f"🥇 黄金监控已启动 | 阈值: {threshold} 元/克 | 间隔: {interval}s")
    print(f"   Webhook: {'已配置' if WEBHOOK_URL else '未配置'}")
    print()

    alert_fired = False  # 是否已触发过告警（避免重复推送）
    error_fired = False  # 异常是否已推送
    last_price: float | None = None
    errors = 0
    last_log_time = datetime.now()
    window_prices: list[float] = []

    send_webhook(f"🥇 黄金监控已启动\n阈值: {threshold} 元/克\n间隔: {interval} 秒")

    while True:
        price, info = fetch_gold_price()

        if price is None:
            errors += 1
            if errors <= 3:
                print(f"[{_now()}] ⚠️ {info}")
            # 连续 5 次失败 → 推送异常告警
            if errors >= 5 and not error_fired:
                send_webhook(
                    f"🚨 黄金监控异常！\n"
                    f"连续 {errors} 次获取数据失败\n"
                    f"最近错误: {info}\n"
                    f"时间: {_now()}"
                )
                error_fired = True
            time.sleep(interval)
            continue

        # 数据恢复正常
        if error_fired:
            send_webhook(
                f"✅ 黄金监控已恢复\n"
                f"Au99.99: {price:.2f} 元/克\n"
                f"时间: {_now()}"
            )
            error_fired = False

        errors = 0
        window_prices.append(price)

        last_price = price

        # 每 10 分钟输出一条日志
        if (datetime.now() - last_log_time).total_seconds() >= 600:
            if window_prices:
                first = window_prices[0]
                end = window_prices[-1]
                delta = end - first
                trend = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
                hi = max(window_prices)
                lo = min(window_prices)
                avg = sum(window_prices) / len(window_prices)
                alert_tag = ""
                if price < threshold:
                    alert_tag = " ⚠️低于阈值!" + ("🔔" if alert_fired else "")
                print(
                    f"[{_now()}] Au99.99={price:.2f} {trend}{delta:+.2f} | "
                    f"高:{hi:.2f} 低:{lo:.2f} 均:{avg:.2f} | "
                    f"样本:{len(window_prices)}{alert_tag}"
                )
            window_prices.clear()
            last_log_time = datetime.now()

        # 告警逻辑：价格低于阈值 且 本轮尚未告警
        if price < threshold and not alert_fired:
            msg = (
                f"⚠️ 黄金跌破阈值！\n"
                f"Au99.99: {price:.2f} 元/克\n"
                f"阈值: {threshold:.2f} 元/克\n"
                f"时间: {_now()}"
            )
            ok = send_webhook(msg)
            if ok:
                alert_fired = True
                print(f"[{_now()}] 🔔 跌破告警已推送！Au99.99={price:.2f} < {threshold:.2f}")
            else:
                print(f"[{_now()}] ⚠️ 告警推送失败，下轮重试")

        # 价格回到阈值+缓冲区以上 → 重置告警状态，下次再跌破会重新推送
        if price >= threshold + RECOVERY_MARGIN and alert_fired:
            msg = (
                f"✅ 黄金回升至阈值以上\n"
                f"Au99.99: {price:.2f} 元/克\n"
                f"时间: {_now()}"
            )
            ok = send_webhook(msg)
            if ok:
                alert_fired = False
                print(f"[{_now()}] ✅ 回升通知已推送！Au99.99={price:.2f} >= {threshold:.2f}")
            else:
                print(f"[{_now()}] ⚠️ 回升通知推送失败")

        time.sleep(interval)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="黄金价格监控")
    parser.add_argument(
        "--price", type=float, default=THRESHOLD,
        help=f"告警阈值（元/克），默认 {THRESHOLD}",
    )
    parser.add_argument(
        "--interval", type=int, default=INTERVAL,
        help=f"轮询间隔（秒），默认 {INTERVAL}",
    )
    parser.add_argument(
        "--webhook", type=str, default="",
        help="企业微信 Webhook URL（覆盖默认）",
    )
    args = parser.parse_args()

    global WEBHOOK_URL
    if args.webhook:
        WEBHOOK_URL = args.webhook

    run(threshold=args.price, interval=args.interval)


if __name__ == "__main__":
    main()
