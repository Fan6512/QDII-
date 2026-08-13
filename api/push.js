import { redis } from "../lib/redis.js";
import { authorized, unauthorized } from "../lib/auth.js";
import { syncFunds } from "../lib/merge.js";

export const config = { runtime: "edge" };

export default async function handler(req) {
  if (req.method !== "POST") return new Response("Method Not Allowed", { status: 405 });
  if (!authorized(req)) return unauthorized();
  let body;
  try { body = await req.json(); } catch { return Response.json({ ok: false, error: "请求体不是合法 JSON" }, { status: 400 }); }
  if (!Array.isArray(body?.funds)) return Response.json({ ok: false, error: "缺少 funds 数组" }, { status: 400 });

  const currentFunds = await redis.get("funds-db");
  const nextFunds = syncFunds(currentFunds || { funds: [] }, body);
  const officialCodes = new Set(nextFunds.funds.map((fund) => fund.code));
  const candidates = (body.candidates || []).filter((candidate) => !officialCodes.has(candidate.code));
  const reviews = candidates.length ? await redis.hmget("candidate-reviews", ...candidates.map((candidate) => candidate.code)) : [];
  const pending = candidates.filter((_, index) => !reviews[index]);
  await Promise.all([
    redis.set("funds-db", nextFunds),
    ...pending.map((candidate) => redis.hset("candidates", { [candidate.code]: JSON.stringify({ ...candidate, review_status: "pending" }) })),
  ]);
  return Response.json({ ok: true, summary: nextFunds._meta.summary, candidates_pending: pending.length });
}
