// K concurrent streaming requests of 128 tokens, temperature 0, max-perf
// config, median of two rounds (ARCHITECTURE.md, bench/http_bench.py table).
export const rows = [
  {
    k: 1,
    batched: { aggregate: 43.6, perStream: 53.4, ttftP50: 0.55, ttftMax: 0.55 },
    serial: { aggregate: 43.4, ttftP50: 0.55, ttftMax: 0.55 },
  },
  {
    k: 2,
    batched: { aggregate: 48.0, perStream: 31.3, ttftP50: 0.64, ttftMax: 1.3 },
    serial: { aggregate: 42.6, ttftP50: 0.64, ttftMax: 3.6 },
  },
  {
    k: 4,
    batched: { aggregate: 54.9, perStream: 16.9, ttftP50: 2.1, ttftMax: 2.8 },
    serial: { aggregate: 41.5, ttftP50: 6.7, ttftMax: 9.9 },
  },
  {
    k: 8,
    batched: { aggregate: 47.3, perStream: 7.1, ttftP50: 3.7, ttftMax: 5.9 },
    serial: { aggregate: 39.9, ttftP50: 13.4, ttftMax: 23.1 },
  },
];

export const RATE_MAX = 60;
export const TTFT_MAX = 24;
