import assert from "node:assert/strict";
import {
  emptyCandidateStore,
  recount,
  reviewCandidate,
  syncCandidates,
  syncFunds,
} from "./lib/merge.js";

const local = {
  funds: [{ code: "019172", short_name: "摩根纳指100", category: "NASDAQ100", trackType: "index", limit_daily: 100 }],
  candidates: [{ code: "018043", short_name: "天弘纳指100A", category: "NASDAQ100", trackType: "index" }],
  _meta: { last_auto_update: "2026-08-13" },
};

// Daily sync updates local data but does not delete a web-approved fund.
const fundsDb = syncFunds({ funds: [{ code: "050025", short_name: "审核加入", category: "SP500", trackType: "index" }] }, local);
assert.equal(fundsDb.funds.length, 2);
assert.equal(fundsDb._meta.summary.total, 2);
assert.equal(fundsDb._meta.last_auto_update, "2026-08-13");

// Newly discovered candidates enter the private review queue.
let candidatesDb = syncCandidates(emptyCandidateStore(), local.candidates, fundsDb.funds);
assert.equal(candidatesDb.candidates.length, 1);
assert.equal(candidatesDb.candidates[0].review_status, "pending");

// Rejecting leaves an audit record and prevents the next discovery sync from reviving it.
let reviewed = reviewCandidate(candidatesDb, "018043", "ignore", "2026-08-13T00:00:00.000Z");
assert.equal(reviewed.store.candidates.length, 0);
assert.equal(reviewed.store.reviewed["018043"].action, "rejected");
candidatesDb = syncCandidates(reviewed.store, local.candidates, fundsDb.funds);
assert.equal(candidatesDb.candidates.length, 0);

// Approved codes are also excluded from candidates once in the public fund database.
const pending = syncCandidates(emptyCandidateStore(), local.candidates, []);
const approved = reviewCandidate(pending, "018043", "confirm", "2026-08-13T00:00:00.000Z");
const afterApproval = syncCandidates(approved.store, local.candidates, [{ code: "018043" }]);
assert.equal(afterApproval.candidates.length, 0);

assert.deepEqual(recount([{ category: "SP500", trackType: "equal_weight" }]), { total: 1, nasdaq100: 0, sp500: 1, equal_weight: 1 });

const pausedLast = (a, b, ascending) => {
  const statusDiff = (a.status === "paused" ? 1 : 0) - (b.status === "paused" ? 1 : 0);
  if (statusDiff) return statusDiff;
  return ascending ? a.limit_daily - b.limit_daily : b.limit_daily - a.limit_daily;
};
const sortable = [{ code: "open", status: "open", limit_daily: 100 }, { code: "paused", status: "paused", limit_daily: 1 }, { code: "unknown", status: "unknown", limit_daily: 10 }];
assert.equal([...sortable].sort((a, b) => pausedLast(a, b, true)).at(-1).code, "paused");
assert.equal([...sortable].sort((a, b) => pausedLast(a, b, false)).at(-1).code, "paused");
console.log("All merge and candidate-review tests passed.");
