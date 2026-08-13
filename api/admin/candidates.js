import { redis } from "../../lib/redis.js";
import { authorized, unauthorized } from "../../lib/auth.js";

export const config = { runtime: "edge" };

export default async function handler(req) {
  if (req.method !== "GET") return new Response("Method Not Allowed", { status: 405 });
  if (!authorized(req)) return unauthorized();
  const items = await redis.hgetall("candidates");
  const candidates = Object.values(items || {}).map((value) => typeof value === "string" ? JSON.parse(value) : value);
  return Response.json({ candidates }, { headers: { "cache-control": "no-store" } });
}
