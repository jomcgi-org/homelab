import { qwenFlashApiBase } from "../upstream.js";

export async function GET() {
  const upstream = await fetch(`${qwenFlashApiBase()}/v1/moe-layer-profile`, {
    signal: AbortSignal.timeout(10000),
  });

  return new Response(upstream.body, {
    status: upstream.status,
    headers: { "Content-Type": "application/json" },
  });
}
