#!/usr/bin/env python3
# push_to_vercel.py — 读取本地 data/funds.json，调用 /api/push 同步进 Vercel KV（保留 candidates）
# 用法:
#   python push_to_vercel.py --url https://你的vercel域名 --key 你的ADMIN_KEY
# 或由 update_funds.py 自动更新后调用，实现"抓取→同步→公网自动最新"
import argparse, json, urllib.request, sys

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True, help="Vercel 部署域名，例如 https://qdii-limit.vercel.app")
    p.add_argument("--key", required=True, help="ADMIN_KEY")
    p.add_argument("--json", default="../data/funds.json", help="本地 funds.json 路径")
    a = p.parse_args()

    data = json.load(open(a.json, encoding="utf-8"))
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        a.url.rstrip("/") + "/api/push",
        data=payload,
        headers={"content-type": "application/json", "x-admin-key": a.key},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        print("✅ 同步成功:", resp.read().decode("utf-8", "ignore"))
    except Exception as e:
        print("❌ 同步失败:", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
