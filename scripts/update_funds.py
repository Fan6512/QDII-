#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
场外 QDII 指数基金额度 - 自动更新脚本
================================================
刷新申购状态、限额、申购费率，以及长期定投比较所需的成本、规模、存续和
统一观察期收益率。抓取失败时保留上一份有效数据，避免错误地清空页面。

数据源（已验证，2026-08-13）：
  - 申购状态/起购金额: 天天基金搜索 API (FundBaseInfo.ISBUY / MINSG)
  - 单日限购额度/费率: 天天基金费率页 jjfl_{code}.html -> 「日累计申购限额」「申购费率」
  - 仅用标准库，无第三方依赖。

用法:
  python update_funds.py          # 更新 data/funds.json
  python update_funds.py --dry-run  # 仅打印抓取结果，不写文件
"""

import json
import os
import re
import shutil
import ssl
import sys
import time
import urllib.request
from datetime import date, datetime, timezone

# ---------- 路径 ----------
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
FUNDS_JSON = os.path.join(DATA_DIR, "funds.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backup")

# ---------- 网络 ----------
CTX = ssl.create_default_context()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


def _get(url, ref="https://fund.eastmoney.com/", attempts=3):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref})
    last_error = None
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=20, context=CTX).read().decode("utf-8", "ignore")
        except (urllib.error.URLError, TimeoutError, ssl.SSLError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(1.5 * (2 ** attempt))
    raise last_error


def fetch_status_and_min(code):
    """从搜索 API 取 ISBUY(申购状态) 与 MINSG(起购金额)。"""
    url = f"https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx?m=1&key={code}&_={int(time.time() * 1000)}"
    data = json.loads(_get(url))
    for d in data.get("Datas", []):
        fbi = d.get("FundBaseInfo") or {}
        result_code = str(fbi.get("CODE") or fbi.get("FCODE") or d.get("CODE") or "")
        if result_code == code:
            isbuy = str(fbi.get("ISBUY", ""))
            minsg = fbi.get("MINSG")
            status = "open" if isbuy == "1" else ("paused" if isbuy == "0" else "unknown")
            return status, (int(minsg) if minsg not in (None, "", 0) else None)
    return None, None


def fetch_limit_and_fee(code):
    """从费率页取单日限额与费率；申购状态只以搜索 API 为准。"""
    t = _get(f"https://fundf10.eastmoney.com/jjfl_{code}.html",
             ref="https://fundf10.eastmoney.com/")
    # 单日限购额度
    m = re.search(r"日累计申购限额\s*([\d,]+(?:\.\d+)?)\s*元", t)
    if not m:
        m = re.search(r"单日累计购买上限\s*([\d,]+)\s*元", t)
    daily = int(float(m.group(1).replace(",", ""))) if m else None

    # 费率：找「申购费率」表段，取第一档 原费率 | 优惠费率
    i = t.find("申购费率")
    fee_orig = fee_disc = None
    if i > 0:
        seg = t[i:i + 800]
        seg = re.sub(r"<[^>]+>", " ", seg)
        seg = re.sub(r"&nbsp;", " ", seg)
        seg = re.sub(r"\s+", " ", seg)
        fm = re.search(r"小于[^|]*?([\d.]+%)\s*\|\s*([\d.]+%)", seg)
        if fm:
            fee_orig = float(fm.group(1).rstrip("%"))
            fee_disc = float(fm.group(2).rstrip("%"))
    return daily, fee_orig, fee_disc


def _as_number(value):
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _strip_html(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def fetch_profile(code):
    """获取管理费、托管费、最新披露规模与成立日期。"""
    text = _strip_html(_get(f"https://fundf10.eastmoney.com/jjxx_{code}.html",
                            ref="https://fundf10.eastmoney.com/"))

    def rate(label):
        match = re.search(label + r"[^\d]{0,30}([\d.]+)%", text)
        return _as_number(match.group(1)) if match else None

    management = rate("基金管理费率")
    custody = rate("基金托管费率")
    scale = None
    scale_date = None
    scale_match = re.search(r"基金规模\s*([\d.]+)亿元[^\d]*(\d{4}-\d{2}-\d{2})", text)
    if scale_match:
        scale = _as_number(scale_match.group(1))
        scale_date = scale_match.group(2)
    inception_match = re.search(r"成立日期\s*(\d{4}-\d{2}-\d{2})", text)
    return {
        "management_fee": management,
        "custody_fee": custody,
        "fund_scale_billion": scale,
        "fund_scale_date": scale_date,
        "inception_date": inception_match.group(1) if inception_match else None,
    }


def _months_before(day, months):
    year = day.year - (months // 12)
    month = day.month - (months % 12)
    if month <= 0:
        year -= 1
        month += 12
    # Clamp to the last day of the target month without external dependencies.
    for candidate in range(day.day, 0, -1):
        try:
            return date(year, month, candidate)
        except ValueError:
            pass


def fetch_performance(code):
    """以复权单位净值计算统一的 3/6/12/60 月区间收益率。"""
    text = _get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
                ref=f"https://fund.eastmoney.com/{code}.html")
    match = re.search(r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\]);", text, re.S)
    if not match:
        raise ValueError("未找到历史净值序列")
    rows = json.loads(match.group(1))
    series = []
    for row in rows:
        nav = _as_number(row.get("y"))
        stamp = row.get("x")
        if nav is None or stamp is None:
            continue
        day = datetime.fromtimestamp(int(stamp) / 1000, tz=timezone.utc).date()
        series.append((day, nav))
    series.sort()
    if len(series) < 2:
        raise ValueError("历史净值不足")

    latest_day, latest_nav = series[-1]

    def return_for(months):
        target = _months_before(latest_day, months)
        eligible = [item for item in series if item[0] <= target]
        if not eligible:
            return None
        return round((latest_nav / eligible[-1][1] - 1) * 100, 2)

    first_day, first_nav = series[0]
    years = (latest_day - first_day).days / 365.2425
    annualized = None
    if years >= 0.25 and first_nav > 0:
        annualized = round(((latest_nav / first_nav) ** (1 / years) - 1) * 100, 2)
    return {
        "performance_as_of": latest_day.isoformat(),
        "return_3m": return_for(3),
        "return_6m": return_for(6),
        "return_1y": return_for(12),
        "return_5y": return_for(60),
        "since_inception_annualized": annualized,
    }


def main():
    dry = "--dry-run" in sys.argv
    with open(FUNDS_JSON, encoding="utf-8") as f:
        data = json.load(f)

    funds = data["funds"]
    report = []
    changed = 0
    warnings = []

    for fd in funds:
        code = fd["code"]
        name = fd.get("short_name", code)
        try:
            status, minsub = fetch_status_and_min(code)
            daily, fo, fd_ = fetch_limit_and_fee(code)
        except Exception as e:
            warnings.append(f"{code} {name}: 抓取失败 {e}")
            report.append({"code": code, "name": name, "ok": False, "error": str(e)})
            time.sleep(0.3)
            continue

        # Profile and NAV history are supplementary. Their intermittent failure must not
        # stop the core daily quota/status refresh or overwrite prior valid values.
        try:
            profile = fetch_profile(code)
        except Exception as e:
            profile = {}
            warnings.append(f"{code} {name}: 基金资料抓取失败，保留旧值 {e}")
        try:
            performance = fetch_performance(code)
        except Exception as e:
            performance = {}
            warnings.append(f"{code} {name}: 历史净值抓取失败，保留旧值 {e}")

        # 保留静态字段，仅覆盖动态字段
        old = {
            "limit_daily": fd.get("limit_daily"),
            "status": fd.get("status"),
            "min_subscribe": fd.get("min_subscribe"),
            "fee_original": fd.get("fee_original"),
            "fee_discount": fd.get("fee_discount"),
            "management_fee": fd.get("management_fee"),
            "custody_fee": fd.get("custody_fee"),
            "fund_scale_billion": fd.get("fund_scale_billion"),
            "fund_scale_date": fd.get("fund_scale_date"),
            "inception_date": fd.get("inception_date"),
            "performance_as_of": fd.get("performance_as_of"),
            "return_3m": fd.get("return_3m"),
            "return_6m": fd.get("return_6m"),
            "return_1y": fd.get("return_1y"),
            "return_5y": fd.get("return_5y"),
            "since_inception_annualized": fd.get("since_inception_annualized"),
        }
        fd["limit_daily"] = daily
        fd["status"] = status or "unknown"
        if minsub is not None:
            fd["min_subscribe"] = minsub
        if fo is not None:
            fd["fee_original"] = fo
        if fd_ is not None:
            fd["fee_discount"] = fd_
        for key, value in profile.items():
            if value is not None:
                fd[key] = value
        fd.update(performance)

        new = {
            "limit_daily": fd["limit_daily"],
            "status": fd["status"],
            "min_subscribe": fd["min_subscribe"],
            "fee_original": fd["fee_original"],
            "fee_discount": fd["fee_discount"],
            "management_fee": fd.get("management_fee"),
            "custody_fee": fd.get("custody_fee"),
            "fund_scale_billion": fd.get("fund_scale_billion"),
            "fund_scale_date": fd.get("fund_scale_date"),
            "inception_date": fd.get("inception_date"),
            "performance_as_of": fd.get("performance_as_of"),
            "return_3m": fd.get("return_3m"),
            "return_6m": fd.get("return_6m"),
            "return_1y": fd.get("return_1y"),
            "return_5y": fd.get("return_5y"),
            "since_inception_annualized": fd.get("since_inception_annualized"),
        }
        if new != old:
            changed += 1
        report.append({"code": code, "name": name, "ok": True,
                       "old": old, "new": new,
                       "fee_ok": fo is not None, "limit_ok": daily is not None})
        time.sleep(0.3)  # 礼貌限速

    today = date.today().isoformat()
    data["_meta"]["generated_at"] = today
    data["_meta"]["last_auto_update"] = today
    data["_meta"]["update_method"] = (
        "脚本自动刷新(update_funds.py)：动态字段取自天天基金公开页面(搜索API FundBaseInfo + jjfl 费率页)，"
        "基金资料页与净值序列；分类字段(code/name/category/trackType)由人工维护"
    )
    # 修正字段口径说明里关于 limit_daily 的旧描述
    if "limit_daily" in data["_meta"].get("字段口径说明", {}):
        data["_meta"]["字段口径说明"]["limit_daily"] = (
            "单日申购上限（元），由脚本自动抓取自天天基金 jjfl 费率页「日累计申购限额」字段；"
            "为 None 表示该基金无单日上限限制"
        )
    data["_meta"].setdefault("字段口径说明", {}).update({
        "management_fee": "基金合同披露的年管理费率（%）",
        "custody_fee": "基金合同披露的年托管费率（%）",
        "fund_scale_billion": "基金资料页披露的最新基金规模（亿元），需结合披露日期阅读",
        "inception_date": "基金成立日期",
        "return_3m/return_6m/return_1y/return_5y": "按同一截至日的复权单位净值计算的区间收益率（%）；存续期不足对应期间则为空",
        "since_inception_annualized": "按复权单位净值计算的成立以来年化收益率（%），仅作补充，不与固定观察期横向比较",
    })

    if not dry:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        backup_path = os.path.join(BACKUP_DIR, f"funds_{data['_meta'].get('last_auto_update_backup','')}.bak")
        # 备份上一版（用时间戳命名更直观）
        backup_path = os.path.join(BACKUP_DIR, f"funds_{time.strftime('%Y%m%d_%H%M%S')}.bak")
        shutil.copy2(FUNDS_JSON, backup_path)
        with open(FUNDS_JSON, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---------- 输出报告 ----------
    print(f"=== 场外 QDII 额度自动更新 ({today}) ===")
    print(f"基金数: {len(funds)} | 有变动: {changed} | 警告: {len(warnings)}")
    for r in report:
        if not r["ok"]:
            print(f"  ❌ {r['code']} {r['name']}: {r['error']}")
        else:
            mark = "" if r["new"] == r["old"] else " *"
            print(f"  {'✅' if (r['limit_ok'] and r['fee_ok']) else '⚠️'} "
                  f"{r['code']} {r['name']} | 限买¥{r['new']['limit_daily']} "
                  f"| 起购¥{r['new']['min_subscribe']} | 费{r['new']['fee_original']}%/"
                  f"{r['new']['fee_discount']}%{mark}")
    for w in warnings:
        print(f"  ⚠️ {w}")
    if dry:
        print("\n[DRY-RUN] 未写入文件。")
    else:
        print(f"\n已写入 {FUNDS_JSON}\n备份: {backup_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
