export function authorized(req) {
  const provided = req.headers.get("x-admin-key");
  const expected = process.env.ADMIN_KEY;
  if (!expected || !provided || provided.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i += 1) diff |= provided.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

export function unauthorized() {
  return Response.json({ ok: false, error: "未授权" }, { status: 401 });
}
