import { redis } from "../../lib/redis.js";
import { authorized, unauthorized } from "../../lib/auth.js";

export const config = { runtime: "edge" };

export default async function handler(req) {
  if (req.method !== "GET") return new Response("Method Not Allowed", { status: 405 });
  if (!authorized(req)) return unauthorized();
  const [items, db, approved] = await Promise.all([
    redis.hgetall("candidates"),
    redis.get("funds-db"),
    redis.hgetall("approved-funds"),
  ]);
  const officialCodes = new Set([
    ...((db?.funds || []).map((fund) => fund.code)),
    ...Object.keys(approved || {}),
  ]);
  const entries = Object.entries(items || {}).map(([code, value]) => [code, typeof value === "string" ? JSON.parse(value) : value]);
  const duplicates = entries.filter(([code, candidate]) => officialCodes.has(code) || officialCodes.has(candidate.code));
  // Keep the review queue consistent with the public list, including records
  // written by older deployments before the approved-fund record existed.
  await Promise.all(duplicates.map(([code]) => redis.hdel("candidates", code)));
  const candidates = entries.filter(([code, candidate]) => !officialCodes.has(code) && !officialCodes.has(candidate.code)).map(([, candidate]) => candidate);
  return Response.json({ candidates }, { headers: { "cache-control": "no-store" } });
}
