// Routed experts per layer and per token (config.json: num_experts 512,
// num_experts_per_tok 10). Which ten light up is illustrative: a seeded
// generator, so the server render and the tests are stable.
export const EXPERTS = 512;
export const PER_TOKEN = 10;

// mulberry32
function generator(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// The ten experts shown as routed for token `n` (1-based).
export function routedExperts(n) {
  const next = generator(0x4090 + n * 7919);
  const picked = new Set();
  while (picked.size < PER_TOKEN) picked.add(Math.floor(next() * EXPERTS));
  return [...picked].sort((a, b) => a - b);
}
