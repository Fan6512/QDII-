#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
场外 QDII 指数基金额度 - 自动更新脚本
================================================
刷新申购状态、限额、申购费率，以及长期定投比较所需的成本、规模、存续和
统一观察期收益率。抓取失败时保留上一份有效数据，避免错误地清空页面。

数据源（已验证，2026-08-14）：
  - 申购状态/起购金额: 天天基金搜索 API (FundBaseInfo.ISBUY / MINSG)
  - 单日限购额度/费率: 天天基金费率页 jjfl_{code}.html -> 「日累计申购限额」「申购费率」
  - 管理费/托管费/规模/成立日期: 天天基金基金概况页 jbgk_{code}.html
      （原 jjxx_{code}.html 已 404 下线，2026-08 迁移至 jbgk）
  - 历史净值/业绩: 天天基金 pingzhongdata_{code}.js (Data_ACWorthTrend 累计净值)
  - 兜底: 当上述东财字段缺失时，可选 AkShare(fund_individual_basic_info_xq /
    fund_fee_em / fund_open_fund_info_em) 补缺；未安装 akshare 则自动跳过，不影响运行
  - 默认仅用标准库（无第三方依赖即可运行）。

用法:
  python update_funds.py          # 更新 data/funds.json
  python update_funds.py --dry-run  # 仅打印抓取结果，不写文件
"""

import json
import os
import re
import random
import shutil
import ssl
import sys
import time
import urllib.request
from datetime import date, datetime, timezone

# ---------- 可选：AkShare 兜底数据源 ----------
# 仅作为主源（天天基金标准库抓取）缺字段时的补缺后端。未安装 akshare 时自动跳过，
# 因此部署在 Vercel 等无依赖环境仍可正常运行。本地/CI 如需兜底，执行：pip install akshare
try:
    import akshare as ak
    AKSHARE_AVAILABLE = True
except ImportError:
    ak = None
    AKSHARE_AVAILABLE = False

# ---------- 路径 ----------
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
FUNDS_JSON = os.path.join(DATA_DIR, "funds.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backup")

# ---------- 网络 ----------
CTX = ssl.create_default_context()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


def _get(url, ref="https://fund.eastmoney.com/", attempts=3, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref})
    last_error = None
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=CTX).read().decode("utf-8", "ignore")
        except (urllib.error.URLError, TimeoutError, ssl.SSLError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep((1.5 * (2 ** attempt)) + random.uniform(0.2, 0.8))
    raise last_error


def fetch_status_and_min(code):
    """从搜索 API 取 ISBUY(申购状态) 与 MINSG(起购金额)。"""
    url = f"https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx?m=1&key={code}&_={int(time.time() * 1000)}"
    data = json.loads(_get(url, attempts=5))
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
             ref="https://fundf10.eastmoney.com/", attempts=4)
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
    """获取管理费、托管费、最新披露规模与成立日期（基金概况页 jbgk）。

    旧接口 jjxx_{code}.html 已于 2026 年前后下线（返回 404），现改用
    jbgk_{code}.html（基金概况）。该页字段写法为：
      - 管理费率 0.50%（每年）/ 托管费率 0.10%（每年）
      - 净资产规模：29.69亿元（截止至：2026-06-30）
      - 成立日期：2023-09-25（部分表格区写作「成立日期/规模 2023年09月25日」）
    """
    html = _get(f"https://fundf10.eastmoney.com/jbgk_{code}.html",
                ref="https://fundf10.eastmoney.com/", attempts=3)
    text = _strip_html(html)

    def rate(label):
        match = re.search(label + r"[^\d]{0,30}([\d.]+)%", text)
        return _as_number(match.group(1)) if match else None

    management = rate("(?:基金)?管理费率?")
    custody = rate("(?:基金)?托管费率?")

    # 规模：净资产规模 29.69亿元（截止至：2026-06-30）
    scale = scale_date = None
    scale_match = re.search(
        r"(?:净资产|基金)规模[^\d]{0,15}([\d.]+)\s*亿元[^\d]{0,30}"
        r"(\d{4}[-/年]\d{2}[-/月]\d{2})", text)
    if scale_match:
        scale = _as_number(scale_match.group(1))
        scale_date = (scale_match.group(2)
                      .replace("年", "-").replace("月", "-")
                      .replace("日", "").replace("/", "-"))

    # 成立日期：2023-09-25  OR  成立日期/规模 2023年09月25日
    inception = None
    inception_match = re.search(r"成立日期[：:\s]{0,4}(\d{4}-\d{2}-\d{2})", text)
    if inception_match:
        inception = inception_match.group(1)
    else:
        inception_match = re.search(
            r"成立日期/规模[^\d]{0,8}(\d{4})年(\d{2})月(\d{2})日", text)
        if inception_match:
            inception = f"{inception_match.group(1)}-{inception_match.group(2)}-{inception_match.group(3)}"
    return {
        "management_fee": management,
        "custody_fee": custody,
        "fund_scale_billion": scale,
        "fund_scale_date": scale_date,
        "inception_date": inception,
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


def _compute_returns(series):
    """由已排序的 [(date, 累计净值), ...] 计算统一的 3/6/12/60 月收益率与年化。

    同时被 fetch_performance（东财 pingzhongdata）与 AkShare 兜底路径复用。
    """
    if len(series) < 2:
        raise ValueError("历史净值不足")
    series = sorted(series)
    latest_day, latest_nav = series[-1]

    def return_for(months):
        target = _months_before(latest_day, months)
        eligible = [item for item in series if item[0] <= target]
        if not eligible:
            return None
        start_day, start_nav = eligible[-1]
        # A missing history must not turn a one-year-old value into a "3-month" return.
        if (target - start_day).days > 10 or start_nav <= 0:
            return None
        return round((latest_nav / start_nav - 1) * 100, 2)

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


def fetch_performance(code):
    """以累计净值（含分红再投资影响）计算统一的 3/6/12/60 月收益率（东财主源）。"""
    text = _get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
                ref=f"https://fund.eastmoney.com/{code}.html", attempts=3)
    match = re.search(r"var\s+Data_ACWorthTrend\s*=\s*(\[.*?\]);", text, re.S)
    if not match:
        raise ValueError("未找到累计净值序列")
    rows = json.loads(match.group(1))
    series = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        stamp, nav = row[0], _as_number(row[1])
        if nav is None or stamp is None:
            continue
        day = datetime.fromtimestamp(int(stamp) / 1000, tz=timezone.utc).date()
        series.append((day, nav))
    return _compute_returns(series)


# ---------- AkShare 兜底（仅在主源缺字段时调用） ----------
def _profile_from_akshare(code):
    """用 AkShare 补缺：成立日期、规模、管理费、托管费。"""
    out = {}
    if not AKSHARE_AVAILABLE:
        return out
    # 基本资料：成立时间 + 最新规模
    try:
        df = ak.fund_individual_basic_info_xq(symbol=code)
        mp = dict(zip(df["item"], df["value"]))
        if str(mp.get("成立时间", "nan")) not in ("", "nan", "None"):
            out["inception_date"] = str(mp["成立时间"]).strip()
        if str(mp.get("最新规模", "nan")) not in ("", "nan", "None"):
            val = _as_number(str(mp["最新规模"]).replace("亿", ""))
            if val is not None:
                out["fund_scale_billion"] = val
    except Exception:
        pass
    # 费率：管理费 / 托管费（indicator 须为「运作费用」，其它取值多返回空）
    try:
        fee = ak.fund_fee_em(symbol=code, indicator="运作费用")
        if not fee.empty:
            row = fee.iloc[0]
            for i in range(0, len(row) - 1, 2):
                label = str(row[i])
                val = _as_number(str(row[i + 1]).replace("（每年）", ""))
                if label.startswith("管理费") and val is not None:
                    out["management_fee"] = val
                elif label.startswith("托管费") and val is not None:
                    out["custody_fee"] = val
    except Exception:
        pass
    return out


def _performance_from_akshare(code):
    """用 AkShare 累计净值序列补缺业绩（复用 _compute_returns）。"""
    if not AKSHARE_AVAILABLE:
        return {}
    df = ak.fund_open_fund_info_em(symbol=code, indicator="累计净值走势", period="成立来")
    series = []
    for _, r in df.iterrows():
        try:
            d = datetime.strptime(str(r["净值日期"]).strip(), "%Y-%m-%d").date()
        except Exception:
            continue
        nav = _as_number(r["累计净值"])
        if d and nav:
            series.append((d, nav))
    return _compute_returns(series)


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
            warn = f"{code} {name}: 基金资料(东财)抓取失败，保留旧值 {e}"
            if AKSHARE_AVAILABLE:
                try:
                    profile = _profile_from_akshare(code)
                    if profile:
                        warn += "；已用 AkShare 补缺"
                except Exception as e2:
                    warn += f"；AkShare 补缺也失败 {e2}"
            warnings.append(warn)
        else:
            # 主源成功但个别字段为空时，用 AkShare 补缺（限管理费/托管费/成立日/规模）
            if AKSHARE_AVAILABLE:
                missing = [k for k in ("management_fee", "custody_fee",
                                       "inception_date", "fund_scale_billion")
                           if profile.get(k) is None]
                if missing:
                    try:
                        filled = {k: v for k, v in _profile_from_akshare(code).items()
                                  if k in missing and v is not None}
                        if filled:
                            profile.update(filled)
                            warnings.append(
                                f"{code} {name}: AkShare 补缺字段 {list(filled)}")
                    except Exception:
                        pass

        try:
            performance = fetch_performance(code)
        except Exception as e:
            performance = {}
            warn = f"{code} {name}: 历史净值(东财)抓取失败，保留旧值 {e}"
            if AKSHARE_AVAILABLE:
                try:
                    performance = _performance_from_akshare(code)
                    warn += "；已用 AkShare 补缺"
                except Exception as e2:
                    warn += f"；AkShare 补缺也失败 {e2}"
            warnings.append(warn)

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
