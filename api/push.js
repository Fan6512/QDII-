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
  const approved = await redis.hgetall("approved-funds");
  const officialCodes = new Set([
    ...nextFunds.funds.map((fund) => fund.code),
    ...Object.keys(approved || {}),
  ]);
  const discoveredCandidates = (body.candidates || []).filter((candidate) => !nextFunds.funds.some((fund) => fund.code === candidate.code));
  const candidates = discoveredCandidates.filter((candidate) => !officialCodes.has(candidate.code));
  // Upstash returns null for HMGET when the hash has not been created yet.
  const reviews = candidates.length ? (await redis.hmget("candidate-reviews", ...candidates.map((candidate) => candidate.code))) || [] : [];
  // Older deployments recorded an approval before status verification completed.
  // If no approved fund was written, reopen that stale review so the candidate
  // returns to the queue instead of being permanently hidden.
  const staleApprovals = candidates.filter((candidate, index) => {
    const value = reviews[index];
    const review = typeof value === "string" ? JSON.parse(value) : value;
    return review?.action === "approved" && !approved?.[candidate.code];
  });
  const staleCodes = new Set(staleApprovals.map((candidate) => candidate.code));
  const pending = candidates.filter((candidate, index) => !reviews[index] || staleCodes.has(candidate.code));
  const approvedUpdates = discoveredCandidates.flatMap((candidate) => {
    const saved = approved?.[candidate.code];
    if (!saved) return [];
    const fund = typeof saved === "string" ? JSON.parse(saved) : saved;
    const dynamic = {
      ...fund,
      limit_daily: candidate.limit_daily ?? fund.limit_daily,
      fee_original: candidate.fee_original ?? fund.fee_original,
      fee_discount: candidate.fee_discount ?? fund.fee_discount,
      min_subscribe: candidate.min_subscribe ?? fund.min_subscribe,
      status: candidate.status === "unknown" ? fund.status : candidate.status,
    };
    return [redis.hset("approved-funds", { [candidate.code]: JSON.stringify(dynamic) })];
  });
  await Promise.all([
    redis.set("funds-db", nextFunds),
    ...staleApprovals.map((candidate) => redis.hdel("candidate-reviews", candidate.code)),
    ...pending.map((candidate) => redis.hset("candidates", { [candidate.code]: JSON.stringify({ ...candidate, review_status: "pending" }) })),
    ...approvedUpdates,
  ]);
  return Response.json({ ok: true, summary: nextFunds._meta.summary, candidates_pending: pending.length });
}
