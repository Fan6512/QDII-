// Pure data transforms shared by the Vercel handlers and local tests.

export function recount(funds) {
  return {
    total: funds.length,
    nasdaq100: funds.filter((f) => f.category === "NASDAQ100").length,
    sp500: funds.filter((f) => f.category === "SP500").length,
    equal_weight: funds.filter((f) => f.trackType === "equal_weight").length,
  };
}

export function normalizeFund(candidate) {
  return {
    code: candidate.code,
    name: candidate.name || candidate.short_name || "",
    short_name: candidate.short_name || candidate.name || "",
    category: candidate.category || "NASDAQ100",
    trackType: candidate.trackType || "index",
    fee_original: candidate.fee_original ?? null,
    fee_discount: candidate.fee_discount ?? null,
    status: candidate.status || "open",
    min_subscribe: candidate.min_subscribe ?? null,
    limit_daily: candidate.limit_daily ?? null,
  };
}

// The local file refreshes known funds. Funds approved through the web admin are
// retained until the local collector has them in its own official list.
export function syncFunds(existingDb, localData) {
  const byCode = new Map((existingDb.funds || []).map((fund) => [fund.code, fund]));
  for (const fund of localData.funds || []) byCode.set(fund.code, fund);
  const funds = [...byCode.values()];
  return {
    funds,
    _meta: { ...(localData._meta || {}), summary: recount(funds) },
  };
}

export function emptyCandidateStore() {
  return { candidates: [], reviewed: {} };
}

// Keep an audit record so an ignored item does not reappear on every discovery run.
export function syncCandidates(store, localCandidates, officialFunds) {
  const current = store || emptyCandidateStore();
  const reviewed = current.reviewed || {};
  const officialCodes = new Set((officialFunds || []).map((fund) => fund.code));
  const candidates = (localCandidates || [])
    .filter((candidate) => !officialCodes.has(candidate.code) && !reviewed[candidate.code])
    .map((candidate) => ({ ...candidate, review_status: "pending" }));
  return { candidates, reviewed };
}

export function reviewCandidate(store, code, action, now = new Date().toISOString()) {
  const current = store || emptyCandidateStore();
  const candidate = (current.candidates || []).find((item) => item.code === code);
  if (!candidate) return { store: current, candidate: null, reason: `候选里没有 ${code}` };

  const review = action === "confirm" ? "approved" : "rejected";
  const next = {
    candidates: current.candidates.filter((item) => item.code !== code),
    reviewed: { ...(current.reviewed || {}), [code]: { action: review, reviewed_at: now } },
  };
  return { store: next, candidate, reason: review === "approved" ? `已加入 ${code}` : `已忽略 ${code}` };
}
