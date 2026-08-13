import { redis } from "../lib/redis.js";

export const config = { runtime: "edge" };

export default async function handler(req) {
  if (req.method !== "GET") return new Response("Method Not Allowed", { status: 405 });
  try {
    const [db, approved] = await Promise.all([redis.get("funds-db"), redis.hgetall("approved-funds")]);
    const local = db || { funds: [], _meta: {} };
    const additions = Object.values(approved || {}).map((value) => typeof value === "string" ? JSON.parse(value) : value);
    // The local collector is authoritative once it starts tracking an approved fund.
    const byCode = new Map([...additions, ...(local.funds || [])].map((fund) => [fund.code, fund]));
    const funds = [...byCode.values()];
    const summary = { total: funds.length, nasdaq100: funds.filter((f) => f.category === "NASDAQ100").length, sp500: funds.filter((f) => f.category === "SP500").length, equal_weight: funds.filter((f) => f.trackType === "equal_weight").length };
    const body = { funds, _meta: { ...(local._meta || {}), summary } };
    // Candidates and review history are intentionally never exposed publicly.
    return Response.json(body, { headers: { "cache-control": "no-store" } });
  } catch {
    return Response.json({ error: "数据暂时不可用" }, { status: 500 });
  }
}
