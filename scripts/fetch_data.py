#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股市场脉搏 / A-Share Market Pulse —— 数据抓取层

从东方财富公开行情接口拉取盘中快照，产出：
    docs/data/market.js   供 index.html 直接 <script> 载入（可 file:// 打开）
    docs/data/latest.json 纯数据，供二次消费 / API 使用
    docs/data/history/YYYY-MM-DD.json  每日快照归档，用于页面上的时间序列

只依赖 requests，无其它第三方依赖。

字段口径备注（东方财富 f 字段）：
    f2 最新价   f3 涨跌幅%   f6 成交额(元)   f12 代码   f14 名称
    f62 主力净流入(元)   f184 主力净占比%
    f104/f105/f106 成分上涨/下跌/平盘家数
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
DATA_DIR = os.path.join(DOCS, "data")
HIST_DIR = os.path.join(DATA_DIR, "history")

CST = timezone(timedelta(hours=8))  # 北京时间

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/",
           "Accept": "*/*"}

# push2 有多个镜像节点，某些网络环境下主域名会被拦截，逐个回退
CLIST_HOSTS = ["https://push2.eastmoney.com",
               "https://82.push2.eastmoney.com",
               "https://48.push2.eastmoney.com"]
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

INDEXES = [
    ("1.000001", "上证指数"),
    ("0.399001", "深证成指"),
    ("0.399006", "创业板指"),
    ("1.000688", "科创50"),
]
# 用于统计全市场涨跌家数：上证综指覆盖沪市全部，深证综指覆盖深市全部
BREADTH_INDEXES = [("1.000001", "沪市"), ("0.399106", "深市")]

TIMEOUT = 25
RETRY = 5
MIN_INTERVAL = 0.4  # 东财对高频请求会限流，两次请求之间至少间隔这么久

_last_call = 0.0


def _throttle() -> None:
    global _last_call
    gap = time.monotonic() - _last_call
    if gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    _last_call = time.monotonic()


def _get(url: str, params: dict, hosts=None) -> dict:
    """带镜像回退 + 指数退避重试的 GET，返回解析后的 JSON。"""
    candidates = hosts or [url]
    last_err = None
    for attempt in range(RETRY):
        for host in candidates:
            _throttle()
            full = host + url if url.startswith("/") else host
            try:
                resp = requests.get(full, params=params, headers=HEADERS, timeout=TIMEOUT)
                if resp.status_code == 200 and resp.text.strip():
                    return resp.json()
                last_err = RuntimeError(f"HTTP {resp.status_code}")
            except Exception as exc:  # noqa: BLE001
                last_err = exc
            time.sleep(0.5)
        time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"请求失败 {url}: {last_err}")


def _diff(data: dict) -> list:
    """clist 接口的 diff 可能是 list 也可能是 dict。"""
    if not data:
        return []
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        return list(diff.values())
    return diff


def fetch_clist(fs: str, fields: str, fid: str = "f3", po: int = 1, pz: int = 100) -> list:
    """行情列表：fs 为板块/市场筛选表达式，fid 排序字段，po=1 降序。"""
    data = _get("/api/qt/clist/get",
                {"pn": 1, "pz": pz, "po": po, "np": 1, "fltt": 2, "invt": 2,
                 "fid": fid, "fs": fs, "fields": fields},
                hosts=CLIST_HOSTS)
    return _diff(data.get("data") or {})


def _scaled(row: dict, key: str) -> float:
    """ulist.np 返回的是放大 10^f1 倍的整数，按 f1 还原真实值。"""
    return _num(row.get(key)) / (10 ** int(_num(row.get("f1")) or 2))


def fetch_indexes() -> list:
    """主要指数快照：点位、涨跌幅、成交额、涨跌家数。"""
    secids = ",".join(s for s, _ in INDEXES)
    data = _get("/api/qt/ulist.np/get",
                {"fields": "f1,f2,f3,f4,f6,f12,f14,f104,f105,f106", "secids": secids},
                hosts=CLIST_HOSTS)
    out = []
    for row in _diff(data.get("data") or {}):
        out.append({
            "code": row.get("f12"),
            "name": row.get("f14"),
            "price": _scaled(row, "f2"),
            "change_pct": _scaled(row, "f3"),
            "change": _scaled(row, "f4"),
            "turnover": _num(row.get("f6")),  # 成交额单位为元，不需要缩放
            "up": _num(row.get("f104")),
            "down": _num(row.get("f105")),
            "flat": _num(row.get("f106")),
        })
    return out


def fetch_breadth() -> dict:
    """
    全市场宽度：沪深两市上涨/下跌/平盘家数合计。
    同时返回两市成交额——注意成交额必须用「综指」口径，深证成指只覆盖 500 只成分股。
    """
    secids = ",".join(s for s, _ in BREADTH_INDEXES)
    data = _get("/api/qt/ulist.np/get",
                {"fields": "f1,f6,f12,f14,f104,f105,f106", "secids": secids},
                hosts=CLIST_HOSTS)
    rows = _diff(data.get("data") or {})
    up = down = flat = turnover = 0.0
    detail = []
    for row in rows:
        u, d, f = _num(row.get("f104")), _num(row.get("f105")), _num(row.get("f106"))
        up += u
        down += d
        flat += f
        turnover += _num(row.get("f6"))
        detail.append({"market": row.get("f14"), "up": u, "down": d, "flat": f,
                       "turnover": _num(row.get("f6"))})
    return {"up": up, "down": down, "flat": flat,
            "turnover": turnover, "detail": detail}


def fetch_index_klines(limit: int = 30) -> dict:
    """指数近 N 日收盘序列，用于走势折线。"""
    result = {}
    for secid, name in INDEXES:
        try:
            data = _get(KLINE_URL,
                        {"secid": secid, "fields1": "f1,f2,f3",
                         "fields2": "f51,f53,f59", "klt": 101, "fqt": 1,
                         "end": "20500101", "lmt": limit})
            klines = (data.get("data") or {}).get("klines") or []
            series = []
            for line in klines:
                parts = line.split(",")
                if len(parts) >= 3:
                    series.append({"date": parts[0], "close": _num(parts[1])})
            result[name] = series
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] K线获取失败 {name}: {exc}", file=sys.stderr)
    return result


def fetch_sectors() -> dict:
    """行业板块：涨幅榜 / 跌幅榜 / 主力资金流入榜 / 流出榜。"""
    fields = "f3,f6,f12,f14,f62,f184,f104,f105,f106"
    by_change = [{"code": r.get("f12"), "name": r.get("f14"),
                  "change_pct": _num(r.get("f3")), "turnover": _num(r.get("f6")),
                  "main_net": _num(r.get("f62")), "main_pct": _num(r.get("f184")),
                  "up": _num(r.get("f104")), "down": _num(r.get("f105"))}
                 for r in fetch_clist("m:90+t:2", fields, fid="f3", po=1, pz=100)]
    by_money = [{"code": r.get("f12"), "name": r.get("f14"),
                 "change_pct": _num(r.get("f3")), "turnover": _num(r.get("f6")),
                 "main_net": _num(r.get("f62")), "main_pct": _num(r.get("f184"))}
                for r in fetch_clist("m:90+t:2", fields, fid="f62", po=1, pz=100)]
    by_money_out = [{"code": r.get("f12"), "name": r.get("f14"),
                     "change_pct": _num(r.get("f3")), "main_net": _num(r.get("f62"))}
                    for r in fetch_clist("m:90+t:2", fields, fid="f62", po=0, pz=100)]
    # 跌幅榜（po=0 升序）。只取涨幅榜的话，「垫底板块」其实是涨幅前 100 名里的末位，会失真
    by_change_down = [{"code": r.get("f12"), "name": r.get("f14"),
                       "change_pct": _num(r.get("f3")), "turnover": _num(r.get("f6")),
                       "main_net": _num(r.get("f62")), "main_pct": _num(r.get("f184")),
                       "up": _num(r.get("f104")), "down": _num(r.get("f105"))}
                      for r in fetch_clist("m:90+t:2", fields, fid="f3", po=0, pz=100)]
    return {"by_change": by_change, "by_change_down": by_change_down,
            "by_money": by_money, "by_money_out": by_money_out}


def fetch_stocks() -> dict:
    """个股：涨幅榜 / 跌幅榜 / 主力净流入榜 / 主力净流出榜。"""
    market = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"  # 深主板 + 创业板 + 沪主板 + 科创板
    fields = "f2,f3,f6,f12,f14,f62,f184"

    def pack(rows):
        return [{"code": r.get("f12"), "name": r.get("f14"),
                 "price": _num(r.get("f2")), "change_pct": _num(r.get("f3")),
                 "turnover": _num(r.get("f6")), "main_net": _num(r.get("f62")),
                 "main_pct": _num(r.get("f184"))} for r in rows]

    return {
        "gainers": pack(fetch_clist(market, fields, fid="f3", po=1, pz=20)),
        "losers": pack(fetch_clist(market, fields, fid="f3", po=0, pz=20)),
        "inflow": pack(fetch_clist(market, fields, fid="f62", po=1, pz=15)),
        "outflow": pack(fetch_clist(market, fields, fid="f62", po=0, pz=15)),
    }


def _num(v) -> float:
    """东方财富用 '-' 表示无数据，统一转成 0。"""
    if v is None or v == "" or v == "-":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def count_limit(stocks: dict) -> dict:
    """涨停/跌停家数（口径：涨跌幅 ≥ 9.8% / ≤ -9.8%，含 20cm 板块的粗略统计）。"""
    up = sum(1 for s in stocks["gainers"] if s["change_pct"] >= 9.8)
    down = sum(1 for s in stocks["losers"] if s["change_pct"] <= -9.8)
    return {"limit_up": up, "limit_down": down}


def build_insights(snapshot: dict) -> list:
    """
    规则化洞察：把数字翻译成一句人话。
    刻意保持保守——只陈述数据支持的事实，不做预测。
    """
    out = []
    b = snapshot["breadth"]
    total = b["up"] + b["down"] + b["flat"]
    if total:
        ratio = b["up"] / total * 100
        out.append({
            "tone": "good" if ratio >= 60 else ("bad" if ratio <= 40 else "neutral"),
            "text": f"全市场 {int(b['up'])} 涨 / {int(b['down'])} 跌，上涨占比 "
                    f"{ratio:.0f}%——{'普涨格局' if ratio >= 60 else ('普跌格局' if ratio <= 40 else '多空分歧')}。",
        })

    sectors = snapshot["sectors"]["by_change"]
    sectors_down = snapshot["sectors"].get("by_change_down") or []
    if sectors and sectors_down:
        best, worst = sectors[0], sectors_down[0]
        out.append({
            "tone": "neutral",
            "text": f"板块分化：{best['name']} 领涨 {best['change_pct']:+.2f}%，"
                    f"{worst['name']} 垫底 {worst['change_pct']:+.2f}%，"
                    f"首尾差 {best['change_pct'] - worst['change_pct']:.2f} 个百分点。",
        })

    money = snapshot["sectors"]["by_money"]
    money_out = snapshot["sectors"]["by_money_out"]
    if money and money_out:
        top_in, top_out = money[0], money_out[0]
        out.append({
            "tone": "good" if top_in["main_net"] > 0 else "neutral",
            "text": f"主力资金最偏好 {top_in['name']}（净流入 {_yi(top_in['main_net'])}），"
                    f"最回避 {top_out['name']}（净流出 {_yi(top_out['main_net'])}）。",
        })
        # 价量背离：涨幅居前的板块，主力资金却在净流出
        out_names = {m["name"]: m["main_net"] for m in money_out}
        divergence = [s for s in sectors[:10]
                      if s["change_pct"] > 0 and out_names.get(s["name"], 0) < 0]
        if divergence:
            names = "、".join(s["name"] for s in divergence[:3])
            out.append({
                "tone": "warn",
                "text": f"注意背离：{names} 涨幅居前但主力资金净流出，上涨缺乏资金确认。",
            })

    for idx in snapshot["indexes"]:
        if idx["name"] == "上证指数":
            b_up = idx.get("up", 0)
            b_down = idx.get("down", 0)
            if idx["change_pct"] > 0 and b_down > b_up:
                out.append({
                    "tone": "warn",
                    "text": f"上证 {idx['change_pct']:+.2f}% 收红，但沪市下跌 "
                            f"{int(b_down)} 家多于上涨 {int(b_up)} 家——指数由权重股拉动，个股体感偏弱。",
                })
            break

    lim = snapshot["limits"]
    if lim["limit_up"] or lim["limit_down"]:
        out.append({
            "tone": "neutral",
            "text": f"涨停 {lim['limit_up']} 家 / 跌停 {lim['limit_down']} 家"
                    f"（口径：涨跌幅绝对值 ≥ 9.8%）。",
        })
    return out


def _yi(v: float) -> str:
    """元 → 亿元，带符号。"""
    return f"{v / 1e8:+.2f} 亿"


def main() -> int:
    now = datetime.now(CST)
    print(f"[{now:%Y-%m-%d %H:%M:%S}] 开始抓取 A 股盘中快照 ...")

    indexes = fetch_indexes()
    breadth = fetch_breadth()
    sectors = fetch_sectors()
    stocks = fetch_stocks()
    klines = fetch_index_klines(30)

    turnover = breadth["turnover"]  # 沪深两市成交额（综指口径）
    net_inflow = sum(s["main_net"] for s in sectors["by_money"][:20])

    snapshot = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "indexes": indexes,
        "breadth": breadth,
        "klines": klines,
        "sectors": sectors,
        "stocks": stocks,
        "limits": count_limit(stocks),
        "turnover": turnover,
        "net_inflow_top20": net_inflow,
    }
    snapshot["insights"] = build_insights(snapshot)

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(HIST_DIR, exist_ok=True)

    hist = {
        "date": snapshot["date"],
        "generated_at": snapshot["generated_at"],
        "turnover": turnover,
        "up": breadth["up"],
        "down": breadth["down"],
        "flat": breadth["flat"],
        "up_ratio": round(breadth["up"] / max(breadth["up"] + breadth["down"] + breadth["flat"], 1) * 100, 2),
        "limit_up": snapshot["limits"]["limit_up"],
        "limit_down": snapshot["limits"]["limit_down"],
        "net_inflow_top20": net_inflow,
        "sh_index": next((i["change_pct"] for i in indexes if i["name"] == "上证指数"), 0),
    }
    with open(os.path.join(HIST_DIR, f"{snapshot['date']}.json"), "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)

    # 汇总时间序列，页面据此画「市场情绪 / 成交额」随日期变化
    series = []
    for fn in sorted(os.listdir(HIST_DIR)):
        if fn.endswith(".json"):
            with open(os.path.join(HIST_DIR, fn), encoding="utf-8") as f:
                series.append(json.load(f))
    series.sort(key=lambda x: x["date"])

    with open(os.path.join(DATA_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    with open(os.path.join(DATA_DIR, "history.json"), "w", encoding="utf-8") as f:
        json.dump(series, f, ensure_ascii=False, indent=2)

    # market.js：把快照和时间序列一起注入全局变量，
    # 这样页面 file:// 双击就能打开，不需要起本地服务器（JSON 走 fetch 会被 CORS 拦）
    with open(os.path.join(DATA_DIR, "market.js"), "w", encoding="utf-8") as f:
        f.write("window.__MARKET__ = ")
        json.dump(snapshot, f, ensure_ascii=False)
        f.write(";\nwindow.__HISTORY__ = ")
        json.dump(series, f, ensure_ascii=False)
        f.write(";\n")

    print(f"完成：{len(indexes)} 个指数 / {len(sectors['by_change'])} 个板块 / "
          f"宽度 {int(breadth['up'])}涨 {int(breadth['down'])}跌 / 两市成交额 {turnover / 1e8:.0f} 亿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
