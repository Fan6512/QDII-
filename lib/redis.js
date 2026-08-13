import { Redis } from "@upstash/redis";

// The SDK reads UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN from Vercel.
export const redis = Redis.fromEnv();
