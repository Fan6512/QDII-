import { redis } from "../lib/redis.js";
import { authorized, unauthorized } from "../lib/auth.js";
import { normalizeFund } from "../lib/merge.js";

export const config = { runtime: "edge" };

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
  // Hash fields are independent per code, avoiding whole-document update races.
  await Promise.all([
    redis.hset("candidate-reviews", { [body.code]: JSON.stringify({ action: review, reviewed_at: new Date().toISOString() }) }),
    redis.hdel("candidates", body.code),
  ]);
  if (body.action === "confirm") {
    await redis.hset("approved-funds", { [body.code]: JSON.stringify(normalizeFund(item)) });
  }
  return Response.json({ ok: true, reason: review === "approved" ? `已加入 ${body.code}` : `已忽略 ${body.code}` });
}
