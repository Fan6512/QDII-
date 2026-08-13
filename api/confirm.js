import { redis } from "../lib/redis.js";
import { authorized, unauthorized } from "../lib/auth.js";
import { normalizeFund } from "../lib/merge.js";

export const config = { runtime: "edge" };

async function verifyStatus(code) {
  const url = `https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx?m=1&key=${encodeURIComponent(code)}&_=${Date.now()}`;
  const response = await fetch(url, { headers: { "user-agent": "QDII-Limit-Tracker/1.0" } });
  if (!response.ok) throw new Error("申购状态数据源暂时不可用");
  const data = await response.json();
  const match = (data.Datas || []).find((entry) => {
    const info = entry.FundBaseInfo || {};
    return String(info.CODE || info.FCODE || entry.CODE || "") === code;
  });
  if (!match) throw new Error("未能在数据源中核验该基金");
  const isBuy = String((match.FundBaseInfo || {}).ISBUY ?? "");
  const searchStatus = isBuy === "1" ? "open" : isBuy === "0" ? "paused" : "unknown";
  const pageResponse = await fetch(`https://fundf10.eastmoney.com/jjfl_${encodeURIComponent(code)}.html`, {
    headers: { "user-agent": "QDII-Limit-Tracker/1.0" },
  });
  if (!pageResponse.ok) return searchStatus;
  const page = await pageResponse.text();
  const text = page.replace(/<[^>]+>/g, " ").replace(/&nbsp;/g, " ").replace(/\s+/g, " ");
  if (/暂停(?:办理)?申购|暂停申购/.test(text)) return "paused";
  if (/暂停大额申购|限制大额申购|限额申购/.test(text)) return "limited";
  return searchStatus;
}

export default async function handler(req) {
  if (req.method !== "POST") return new Response("Method Not Allowed", { status: 405 });
  if (!authorized(req)) return unauthorized();
  let body;
  try { body = await req.json(); } catch { return Response.json({ ok: false, error: "请求体不是合法 JSON" }, { status: 400 }); }
  if (!body?.code || !["confirm", "ignore"].includes(body.action)) return Response.json({ ok: false, error: "action 仅支持 confirm/ignore，且必须提供 code" }, { status: 400 });

  const [candidate, existingReview] = await Promise.all([redis.hget("candidates", body.code), redis.hget("candidate-reviews", body.code)]);
  if (!candidate || existingReview) {
    const review = typeof existingReview === "string" ? JSON.parse(existingReview) : existingReview;
    const message = review?.action === "approved" ? "该候选曾被审核，但加入未完成；请先执行一次数据同步后重试"
      : review?.action === "rejected" ? "该候选已被忽略"
      : `候选里没有 ${body.code}`;
    return Response.json({ ok: false, error: message }, { status: 404 });
  }
  const item = typeof candidate === "string" ? JSON.parse(candidate) : candidate;
  const review = body.action === "confirm" ? "approved" : "rejected";
  let verifiedStatus;
  if (body.action === "confirm") {
    try { verifiedStatus = await verifyStatus(body.code); }
    catch (error) { return Response.json({ ok: false, error: error.message }, { status: 502 }); }
  }
  // Hash fields are independent per code, avoiding whole-document update races.
  await Promise.all([
    redis.hset("candidate-reviews", { [body.code]: JSON.stringify({ action: review, reviewed_at: new Date().toISOString() }) }),
    redis.hdel("candidates", body.code),
  ]);
  if (body.action === "confirm") {
    await redis.hset("approved-funds", { [body.code]: JSON.stringify(normalizeFund({ ...item, status: verifiedStatus })) });
  }
  return Response.json({ ok: true, reason: review === "approved" ? `已加入 ${body.code}` : `已忽略 ${body.code}` });
}
