// FreeToken on node-4, reached over the Tailscale egress Service. The URL comes
// from the frontend.qwenApiBase Helm value; the page answers 503 without it.
export function qwenApiBase() {
  const base = process.env.QWEN_API_BASE;
  return base ? base.replace(/\/$/, "") : null;
}

export function notConfigured() {
  return new Response(
    JSON.stringify({ error: "QWEN_API_BASE is not configured" }),
    { status: 503, headers: { "Content-Type": "application/json" } },
  );
}
