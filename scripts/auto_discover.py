#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
场外 QDII 指数基金 - 自动发现候选池
================================================
扫描天天基金「纳斯达克100」「标普500」搜索结果，过滤出场外 A 类人民币 QDII
（排除：纯场内 ETF、C 类份额、美元份额），与现有 funds.json 比对，
把不在清单里的新基金写入顶层 candidates 数组（绝不自动加入 funds）。

确认流程：本脚本只产出候选 -> 用户在对话里说"加哪几只" -> 由 merge_candidates.py 写入 funds。

用法:
  python auto_discover.py            # 扫描并写 candidates
  python auto_discover.py --dry-run  # 仅打印候选，不落盘
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.request
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
FUNDS_JSON = os.path.join(DATA_DIR, "funds.json")

CTX = ssl.create_default_context()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

KEYWORDS = {
    "NASDAQ100": "纳斯达克100",
    "SP500": "标普500",
}


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


def search(keyword):
    url = (f"https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
           f"?m=1&key={urllib.parse.quote(keyword)}&_={int(time.time() * 1000)}")
    data = json.loads(_get(url))
    out = []
    for d in data.get("Datas", []):
        fbi = d.get("FundBaseInfo") or {}
        code = fbi.get("FCODE") or d.get("CODE")
        if not code:
            continue
        out.append({
            "code": str(code),
            "name": fbi.get("SHORTNAME") or d.get("NAME"),
            "company": fbi.get("JJGS"),
            "ftype": fbi.get("FTYPE", ""),
            "isbuy": str(fbi.get("ISBUY", "")),
            "minsg": fbi.get("MINSG"),
        })
    return out


def fetch_status_and_min(code):
    """按基金代码精确查询，避免关键词搜索返回没有状态字段的结果。"""
    url = (f"https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
           f"?m=1&key={urllib.parse.quote(code)}&_={int(time.time() * 1000)}")
    data = json.loads(_get(url))
    for item in data.get("Datas", []):
        info = item.get("FundBaseInfo") or {}
        result_code = str(info.get("CODE") or info.get("FCODE") or item.get("CODE") or "")
        if result_code == code:
            isbuy = str(info.get("ISBUY", ""))
            status = "open" if isbuy == "1" else ("paused" if isbuy == "0" else "unknown")
            minsg = info.get("MINSG")
            return status, (int(minsg) if minsg not in (None, "", 0) else None)
    return "unknown", None


def fetch_limit_and_fee(code):
    try:
        t = _get(f"https://fundf10.eastmoney.com/jjfl_{code}.html",
                 ref="https://fundf10.eastmoney.com/")
        m = re.search(r"日累计申购限额\s*([\d,]+(?:\.\d+)?)\s*元", t)
        if not m:
            m = re.search(r"单日累计购买上限\s*([\d,]+)\s*元", t)
        daily = int(float(m.group(1).replace(",", ""))) if m else None
        i = t.find("申购费率")
        fo = fd_ = None
        if i > 0:
            seg = re.sub(r"<[^>]+>", " ", t[i:i + 800])
            seg = re.sub(r"&nbsp;", " ", seg)
            seg = re.sub(r"\s+", " ", seg)
            fm = re.search(r"小于[^|]*?([\d.]+%)\s*\|\s*([\d.]+%)", seg)
            if fm:
                fo = float(fm.group(1).rstrip("%"))
                fd_ = float(fm.group(2).rstrip("%"))
        return daily, fo, fd_
    except Exception:
        return None, None, None


def is_offexchange_qdii(name, code):
    """过滤：只保留场外 A 类人民币 QDII。返回 (保留, 排除原因)。"""
    if not name:
        return False, "无名称"
    nm = name
    # 排除纯场内 ETF（名称含 ETF 但不含 联接）
    if "ETF" in nm and "联接" not in nm:
        return False, "纯场内ETF(非联接)"
    # 排除 C / I / D 类份额（只保留 A 类人民币）
    tail = nm.rstrip()
    if "C类" in nm or "C份额" in nm or tail.endswith("C") or "C人民币" in nm or "C类人民币" in nm:
        return False, "C类份额"
    if "I类" in nm or "I份额" in nm or tail.endswith("I"):
        return False, "I类份额"
    if "D类" in nm or "D份额" in nm or tail.endswith("D"):
        return False, "D类份额"
    # 排除美元份额
    if "美元" in nm:
        return False, "美元份额"
    # 排除明确的场内/LOF 纯交易？LOF 允许场外，保留
    # 必须含 联接 / 指数 / 等权 / LOF 标识之一，避免奇怪品种
    if not any(k in nm for k in ["联接", "指数", "等权", "LOF", "发起"]):
        # 含 ETF联接 已保留；其余若只是裸名则跳过
        if "ETF" in nm:
            return False, "其他ETF"
        # 允许裸露的被动指数名
    return True, ""


def main():
    dry = "--dry-run" in sys.argv
    with open(FUNDS_JSON, encoding="utf-8") as f:
        data = json.load(f)
    existing_codes = {x["code"] for x in data.get("funds", [])}

    # 先去重：同一 (公司, 指数, 含"等权") 可能多份额，只保留首选 A类人民币
    seen = {}        # key -> candidate dict
    rejected = []    # (code,name,reason)

    for category, kw in KEYWORDS.items():
        for r in search(kw):
            code = r["code"]
            name = r["name"] or ""
            keep, reason = is_offexchange_qdii(name, code)
            if not keep:
                rejected.append({"code": code, "name": name, "reason": reason})
                continue
            if code in existing_codes:
                continue  # 已在清单
            if code in seen:
                # 同一基金多个份额命中，保留 A类/人民币 那一个
                continue
            status, min_subscribe = fetch_status_and_min(code)
            daily, fo, fd_ = fetch_limit_and_fee(code)
            track = "equal_weight" if "等权" in name else "index"
            cand = {
                "code": code,
                "name": name,
                "short_name": name,
                "category": category,
                "trackType": track,
                "company": r["company"],
                # Missing or unfamiliar source values are not evidence of an open subscription.
                "status": status,
                "min_subscribe": min_subscribe,
                "limit_daily": daily,
                "fee_original": fo,
                "fee_discount": fd_,
                "discovered_at": date.today().isoformat(),
                "note": "自动发现候选，待人工确认后并入 funds",
            }
            seen[code] = cand
            time.sleep(0.3)

    candidates = list(seen.values())
    candidates.sort(key=lambda c: (c["category"], c["code"]))

    data["candidates"] = candidates
    data["_meta"]["last_discover_at"] = date.today().isoformat()

    if not dry:
        with open(FUNDS_JSON, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"=== 自动发现 ({{}}) ===".format(date.today().isoformat()))
    print(f"命中候选新基金: {len(candidates)} 只")
    for c in candidates:
        print(f"  ✳️ {c['code']} {c['short_name']} [{c['company']}] "
              f"{'纳指100' if c['category']=='NASDAQ100' else '标普500'}"
              f"{'·等权' if c['trackType']=='equal_weight' else ''} "
              f"| 限买¥{c['limit_daily']} | 起购¥{c['min_subscribe']}")
    print(f"\n已排除 {len(rejected)} 条（场内ETF/C类/美元等）:")
    for x in rejected[:25]:
        print(f"  ✖ {x['code']} {x['name']} -> {x['reason']}")
    if dry:
        print("\n[DRY-RUN] 未写入文件。")
    else:
        print(f"\n已写入 candidates 到 {FUNDS_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
