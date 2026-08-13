#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
场外 QDII 指数基金额度 - 自动更新脚本
================================================
只刷新「动态字段」：limit_daily(单日限购) / status(申购状态) /
min_subscribe(起购) / fee_original / fee_discount(费率)。
保留「静态字段」：code / name / short_name / category / trackType。

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
from datetime import date

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
        if fbi.get("CODE") == code:
            isbuy = str(fbi.get("ISBUY", ""))
            minsg = fbi.get("MINSG")
            status = "open" if isbuy == "1" else ("paused" if isbuy == "0" else "unknown")
            return status, (int(minsg) if minsg not in (None, "", 0) else None)
    return None, None


def subscription_status_from_page(html):
    """从费率页的产品状态文字补充搜索 API 的申购状态。"""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text)
    if re.search(r"暂停(?:办理)?申购|暂停申购", text):
        return "paused"
    if re.search(r"暂停大额申购|限制大额申购|限额申购", text):
        return "limited"
    return None


def fetch_limit_fee_and_page_status(code):
    """从费率页取单日限额、费率及补充申购状态。"""
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
    return daily, fee_orig, fee_disc, subscription_status_from_page(t)


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
            daily, fo, fd_, page_status = fetch_limit_fee_and_page_status(code)
        except Exception as e:
            warnings.append(f"{code} {name}: 抓取失败 {e}")
            report.append({"code": code, "name": name, "ok": False, "error": str(e)})
            time.sleep(0.3)
            continue

        # 保留静态字段，仅覆盖动态字段
        old = {
            "limit_daily": fd.get("limit_daily"),
            "status": fd.get("status"),
            "min_subscribe": fd.get("min_subscribe"),
            "fee_original": fd.get("fee_original"),
            "fee_discount": fd.get("fee_discount"),
        }
        fd["limit_daily"] = daily
        fd["status"] = page_status or status or "unknown"
        if minsub is not None:
            fd["min_subscribe"] = minsub
        if fo is not None:
            fd["fee_original"] = fo
        if fd_ is not None:
            fd["fee_discount"] = fd_

        new = {
            "limit_daily": fd["limit_daily"],
            "status": fd["status"],
            "min_subscribe": fd["min_subscribe"],
            "fee_original": fd["fee_original"],
            "fee_discount": fd["fee_discount"],
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
        "静态字段(code/name/category/trackType)由人工维护"
    )
    # 修正字段口径说明里关于 limit_daily 的旧描述
    if "limit_daily" in data["_meta"].get("字段口径说明", {}):
        data["_meta"]["字段口径说明"]["limit_daily"] = (
            "单日申购上限（元），由脚本自动抓取自天天基金 jjfl 费率页「日累计申购限额」字段；"
            "为 None 表示该基金无单日上限限制"
        )

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
