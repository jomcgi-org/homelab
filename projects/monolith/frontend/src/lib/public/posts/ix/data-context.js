// KV cache one sequence holds, computed the way the engine sizes it
// (oominf-models-qwen: attention.rs buffer_lens; oominf-core backend.rs
// KvFormat::row_bytes). Dimensions from the model's config.json.

import { RECORD_BYTES } from "./data.js";

export const MAX_CONTEXT = 262_144; // the slider's cap, as --max-context
export const MIN_CONTEXT = 2_048; // attention.rs INITIAL_KV_TOKENS
const RATIO = 4; // indexer_compress_ratio
const IDX_DIM = 128; // indexer_head_dim
const HEAD_DIM = 256; // head_dim
const KV_HEADS = 2; // num_key_value_heads
export const ATTENTION_LAYERS = 13; // 12 full-attention decoder layers + MTP
const F32 = 4;

export const KV_FORMATS = {
  k8v6: { key: 8, value: 6 },
  fp32: { key: 0, value: 0 },
};

// KvFormat::row_bytes: one head's row of `d` values at `bits` (0 = fp32).
export function rowBytes(bits, d = HEAD_DIM) {
  if (!bits) return 4 * d;
  const scales = Math.ceil(((d / 32) * 2) / 16) * 16;
  return scales + (d / 32) * bits * 4;
}

// One attention layer's buffers for `rows` tokens (buffer_lens).
export function attentionBytes(rows, format = "k8v6") {
  const f = KV_FORMATS[format];
  const kv = rows * KV_HEADS * (rowBytes(f.key) + rowBytes(f.value));
  const indexer =
    rows * IDX_DIM + Math.max(Math.floor(rows / RATIO), 1) * IDX_DIM;
  return kv + indexer * F32;
}

// The attention layers' KV cache (keys, values and indexer keys) for `tokens`,
// and how many 2.7 MB expert records that much VRAM would otherwise hold.
export function kvCache(tokens, format = "k8v6") {
  const bytes = ATTENTION_LAYERS * attentionBytes(tokens, format);
  return { bytes, records: Math.round(bytes / RECORD_BYTES) };
}

// The slider runs on a log scale from 2k to 256k tokens.
export const sliderToTokens = (v) =>
  Math.round(MIN_CONTEXT * (MAX_CONTEXT / MIN_CONTEXT) ** (v / 1000));
export const tokensToSlider = (t) =>
  Math.round(
    (1000 * Math.log(t / MIN_CONTEXT)) / Math.log(MAX_CONTEXT / MIN_CONTEXT),
  );

export const gb = (bytes) => (bytes / 1e9).toFixed(2);
