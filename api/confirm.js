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
  return isBuy === "1" ? "open" : isBuy === "0" ? "paused" : "unknown";
}

export default async function handler(req) {
  if (req.method !== "POST") return new Response("Method Not Allowed", { status: 405 });
  if (!authorized(req)) return unauthorized();
  let body;
  try { body = await req.json(); } catch { return Response.json({ ok: false, error: "请求体不是合法 JSON" }, { status: 400 }); }
  if (!body?.code || !["confirm", "ignore"].includes(body.action)) return Response.json({ ok: false, error: "action 仅支持 confirm/ignore，且必须提供 code" }, { status: 400 });

  const [candidate, existingReview] = await Promise.all([redis.hget("candidates", body.code), redis.hget("candidate-reviews", body.code)]);
  if (!candidate || existingReview) return Response.json({ ok: false, error: `候选里没有 ${body.code}` }, { status: 404 });
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
