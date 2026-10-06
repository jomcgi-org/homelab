"""Per-layer reference fixtures for Qwen 3.8 Flash from the HF transformers implementation.

The fixtures define "correct" for oominf: every stage boundary of one decoder layer, captured
from HF's own ``Qwen4ExpTextDecoderLayer.forward`` with forward hooks (hooks only observe; nothing
is recomputed outside HF code), on CPU, for a fixed chat-templated prompt (prefill) followed by a
few teacher-forced single-token decode steps that exercise the recurrent state.

Only the tensors a layer needs are read from the release checkpoint. HF transformers cannot load
this ModelOpt NVFP4 checkpoint itself (see README), so routed experts are densified with ModelOpt's
own NVFP4 dequantisation and loaded into HF's dense expert parameters.

Two MoE modes:
  w4a16  weights NVFP4 as released, activations unquantised (HF's own experts forward).
  w4a4   weights as above, plus ModelOpt NVFP4 activation fake-quant on every routed expert
         input using the released ``input_scale`` (emulation; see ``fake_quant_nvfp4``).

Usage (CPU only; production may hold the GPU and cores 0-7):
  CUDA_VISIBLE_DEVICES= nice -n 19 taskset -c 8-15 uv run python make_fixtures.py --layers 0
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import hashlib
import json
import os
import platform
import time
from pathlib import Path

import torch
import transformers
from safetensors import safe_open
from safetensors.torch import load_file, save_file
from transformers import AutoTokenizer, DynamicCache
from transformers.models.qwen4_exp import modeling_qwen4_exp as hf
from transformers.models.qwen4_exp.configuration_qwen4_exp import Qwen4ExpConfig

# The release checkpoint (safetensors) defaults to $OOMINF_RELEASE_CHECKPOINT.
DEFAULT_MODEL = os.environ.get("OOMINF_RELEASE_CHECKPOINT")
DEFAULT_OUT = "/disks/nvme-02/src/oominf-data/fixtures/qwen38-flash"

# Fixed workload. The user message goes through the checkpoint's own chat template; decode tokens
# are teacher-forced from CONTINUATION (a single layer cannot produce real next tokens).
USER_MESSAGE = "In two short sentences, explain why a sparse mixture-of-experts model can run on a gaming PC."
CONTINUATION = "The user asks why a sparse"
DECODE_STEPS = 3

# Long-context workload (--long): enough tokens that the QSA indexer keeps fewer blocks than are
# visible (top block_topk of more than 512 blocks), so its top-k pruning is exercised. The text is
# a fixed prefix of the repository's MPL-2.0 LICENSE, stored next to this script.
LONG_PROMPT_FILE = Path(__file__).with_name("long_prompt.txt")
LONG_INSTRUCTION = "Summarize the obligations this license places on distributors in three bullet points.\n\n"

DTYPES = {"fp32": torch.float32, "bf16": torch.bfloat16}

# ---------------------------------------------------------------------------------------------
# NVFP4 (ModelOpt) semantics. Source: nvidia-modelopt 0.46.0 (the producer recorded in the
# checkpoint's hf_quant_config.json), modelopt/torch/quantization/qtensor/nvfp4_tensor.py:
#   e2m1_values (line 27), quantize() packing ``(q[..., 1::2] << 4) | q[..., 0::2]`` (low nibble
#   = even element), dequantize(): per_block_scale = fp8_scale.float() * weight_scale_2, value =
#   e2m1[nibble] * per_block_scale, computed in fp32 then cast to the target dtype.
# ---------------------------------------------------------------------------------------------
E2M1_VALUES = torch.tensor(
    [0, 0.5, 1, 1.5, 2, 3, 4, 6, 0, -0.5, -1, -1.5, -2, -3, -4, -6]
)
E4M3_MAX = 448.0
BLOCK = 16


def dequant_nvfp4(
    packed: torch.Tensor, scale: torch.Tensor, scale_2: torch.Tensor
) -> torch.Tensor:
    """ModelOpt NVFP4QTensor.dequantize (non-fast path), returned in fp32."""
    unpacked = torch.empty((*packed.shape[:-1], packed.shape[-1] * 2), dtype=torch.long)
    unpacked[..., 1::2] = (packed >> 4).long()
    unpacked[..., 0::2] = (packed & 0x0F).long()
    values = E2M1_VALUES[unpacked]
    per_block_scale = scale.to(torch.float32) * scale_2.to(torch.float32)
    values = values.view(*values.shape[:-1], -1, BLOCK) * per_block_scale.unsqueeze(-1)
    return values.reshape(*packed.shape[:-1], packed.shape[-1] * 2)


def fp4_round_magnitude(a: torch.Tensor) -> torch.Tensor:
    """ModelOpt fp4_round_magnitude: |x|/scale to the nearest E2M1 magnitude, ties to even."""
    return torch.where(a <= 0.25, 0.0,
           torch.where(a < 0.75, 0.5,
           torch.where(a <= 1.25, 1.0,
           torch.where(a < 1.75, 1.5,
           torch.where(a <= 2.5, 2.0,
           torch.where(a < 3.5, 3.0,
           torch.where(a <= 5.0, 4.0, 6.0)))))))  # fmt: skip


def fake_quant_nvfp4(x: torch.Tensor, input_scale: torch.Tensor) -> torch.Tensor:
    """NVFP4 activation quantise-dequantise with a static global scale (W4A4 emulation).

    Mirrors ModelOpt's runtime fake-quant kernel, modelopt/torch/kernels/quantization/gemm/
    fp4_kernel_hopper.py ``fp4_fake_quant_block`` with ``fp8_quantize_scale`` and
    ``fp4_round_magnitude`` from modelopt/torch/kernels/quantization/common/nvfp4_quant.py.
    The exported ``input_scale`` is ``amax / (6 * 448)`` (NVFP4QTensor.get_activation_scaling_factor),
    which is exactly that kernel's ``global_scale``. Per 16-element block:
      s = fp8_e4m3(min(block_amax / (6 * gs), 448)) * gs;  s = 1.0 if s < 1e-5
      y = sign(x) * round_e2m1(|x| / s) * s
    Hardware W4A4 GEMMs compute the same quantities but may differ in the last ulp of the scale.
    """
    xf = x.to(torch.float32)
    gs = input_scale.to(torch.float32)
    gs = torch.where(gs > 0, gs, torch.tensor(1e-12))
    blocks = xf.view(*xf.shape[:-1], -1, BLOCK)
    block_max = blocks.abs().amax(dim=-1, keepdim=True)
    s = (
        torch.clamp(block_max / (6.0 * gs), max=E4M3_MAX)
        .to(torch.float8_e4m3fn)
        .to(torch.float32)
        * gs
    )
    s = torch.where(s >= 1e-5, s, torch.tensor(1.0))
    q = fp4_round_magnitude(blocks.abs() / s) * s
    y = torch.where(blocks >= 0, q, -q)
    return y.view_as(xf).to(x.dtype)


# ---------------------------------------------------------------------------------------------
# Checkpoint access: read only what one layer needs.
# ---------------------------------------------------------------------------------------------
class Checkpoint:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.weight_map = json.loads(
            (path / "model.safetensors.index.json").read_text()
        )["weight_map"]
        self._files: dict[str, object] = {}

    def _open(self, name: str):
        fname = self.weight_map[name]
        if fname not in self._files:
            self._files[fname] = safe_open(self.path / fname, framework="pt")
        return self._files[fname]

    def get(self, name: str) -> torch.Tensor:
        return self._open(name).get_tensor(name)

    def shape(self, name: str) -> list[int]:
        return self._open(name).get_slice(name).get_shape()

    def rows(self, name: str, ids: list[int]) -> torch.Tensor:
        sl = self._open(name).get_slice(name)
        return torch.cat([sl[i : i + 1] for i in ids])


class LazyPleTable(torch.nn.Module):
    """Stands in for HF's ``ngram_embedding`` (an nn.Embedding over the 128 concatenated shards,
    320M x 160, about 51 GB): gathers only the rows a forward touches, straight from the shards.

    The release stores the table as FP8 E4M3 shards plus one per-table scalar ``weight_scale``
    (declared by ``text_config.ple_embedding_dtype``; HF 5.16.1 does not know this key). Rows are
    dequantised as ``fp8.float() * weight_scale.float()`` (the scale is a dequant multiplier) and
    cast to the run dtype.
    """

    def __init__(
        self,
        ckpt: "Checkpoint",
        prefix: str,
        num_shards: int,
        dtype: torch.dtype,
        on_rows=None,
    ) -> None:
        super().__init__()
        self.ckpt, self.prefix, self.dtype, self.on_rows = ckpt, prefix, dtype, on_rows
        self.shards = [f"{prefix}shard_{i}.weight" for i in range(num_shards)]
        self.rows_per_shard = ckpt.shape(self.shards[0])[0]
        self.scale = ckpt.get(prefix + "weight_scale").to(torch.float32)
        self.weight = torch.empty(0)  # HF reads ``ngram_embedding.weight.device``

    def forward(self, ids: torch.Tensor) -> torch.Tensor:
        if self.on_rows is not None:
            self.on_rows(ids)
        flat = ids.reshape(-1).tolist()
        rows = [
            self.ckpt.rows(
                self.shards[r // self.rows_per_shard], [r % self.rows_per_shard]
            )
            for r in flat
        ]
        out = (torch.cat(rows).to(torch.float32) * self.scale).to(self.dtype)
        return out.view(*ids.shape, -1)


def layer_prefix(layer: int) -> str:
    return f"model.language_model.layers.{layer}."


def build_layer(
    ckpt: Checkpoint, tc, layer: int, dtype: torch.dtype
) -> torch.nn.Module:
    torch.set_default_dtype(dtype)
    try:
        with torch.device("meta"):
            mod = hf.Qwen4ExpTextDecoderLayer(tc, layer)
    finally:
        torch.set_default_dtype(torch.float32)
    prefix = layer_prefix(layer)
    if mod.ple is not None:
        mod.ple.ple_embedding.ngram_embedding = LazyPleTable(
            ckpt,
            prefix + "ple.ple_embedding.ngram_embedding.",
            tc.split_ngram_parts,
            dtype,
        )
    mod = mod.to_empty(device="cpu").eval()
    with torch.no_grad():
        for name, param in mod.named_parameters():
            if name.startswith("mlp.experts."):
                continue
            src = ckpt.get(prefix + name)
            if src.shape != param.shape:
                raise ValueError(
                    f"{name}: checkpoint {tuple(src.shape)} vs module {tuple(param.shape)}"
                )
            param.copy_(src.to(torch.float32).to(dtype))
        experts = mod.mlp.experts
        inter = tc.moe_intermediate_size
        ep = prefix + "mlp.experts."
        for e in range(tc.num_experts):
            w = {}
            for proj in ("gate", "up", "down"):
                k = f"{ep}{e}.{proj}_proj."
                w[proj] = dequant_nvfp4(
                    ckpt.get(k + "weight"),
                    ckpt.get(k + "weight_scale"),
                    ckpt.get(k + "weight_scale_2"),
                )
            experts.gate_up_proj[e, :inter].copy_(w["gate"].to(dtype))
            experts.gate_up_proj[e, inter:].copy_(w["up"].to(dtype))
            experts.down_proj[e].copy_(w["down"].to(dtype))
        for name, buf in mod.named_buffers():
            buf.copy_(ckpt.get(prefix + name))
    return mod


def load_input_scales(ckpt: Checkpoint, tc, layer: int) -> dict[str, torch.Tensor]:
    ep = layer_prefix(layer) + "mlp.experts."
    return {
        proj: torch.stack(
            [
                ckpt.get(f"{ep}{e}.{proj}_proj.input_scale")
                for e in range(tc.num_experts)
            ]
        )
        for proj in ("gate", "up", "down")
    }


# ---------------------------------------------------------------------------------------------
# W4A4 experts forward: HF Qwen4ExpTextExperts.forward line for line, plus activation fake-quant.
# With quantisation off it must reproduce HF bit-exactly (checked at runtime).
# ---------------------------------------------------------------------------------------------
def experts_forward(experts, hidden_states, top_k_index, top_k_weights, scales=None):
    final = torch.zeros_like(hidden_states)
    with torch.no_grad():
        mask = torch.nn.functional.one_hot(
            top_k_index, num_classes=experts.num_experts
        ).permute(2, 1, 0)
        hit = torch.greater(mask.sum(dim=(-1, -2)), 0).nonzero()
    inter = experts.intermediate_dim
    for expert_idx in hit:
        expert_idx = expert_idx[0]
        if expert_idx == experts.num_experts:
            continue
        top_k_pos, token_idx = torch.where(mask[expert_idx])
        x = hidden_states[token_idx]
        w = experts.gate_up_proj[expert_idx]
        if scales is None:
            gate, up = torch.nn.functional.linear(x, w).chunk(2, dim=-1)
        else:
            gate = torch.nn.functional.linear(
                fake_quant_nvfp4(x, scales["gate"][expert_idx]), w[:inter]
            )
            up = torch.nn.functional.linear(
                fake_quant_nvfp4(x, scales["up"][expert_idx]), w[inter:]
            )
        h = experts.act_fn(gate) * up
        if scales is not None:
            h = fake_quant_nvfp4(h, scales["down"][expert_idx])
        h = torch.nn.functional.linear(h, experts.down_proj[expert_idx])
        h = h * top_k_weights[token_idx, top_k_pos, None]
        final.index_add_(0, token_idx, h.to(final.dtype))
    return final


@contextlib.contextmanager
def patched_experts(experts, scales):
    # Instance attribute shadows the class forward; deleting it restores HF's without leaving a
    # bound-method reference cycle that would keep the (multi-GB) layer alive until a GC pass.
    experts.forward = lambda hs, idx, w: experts_forward(experts, hs, idx, w, scales)
    try:
        yield
    finally:
        del experts.forward


# ---------------------------------------------------------------------------------------------
# Stage capture.
# ---------------------------------------------------------------------------------------------
STAGES = {
    "residual_in": "layer input residual [T, hc*H] (layer 0: token embedding repeated over hc streams)",
    "ple_out": "PLE output added to the residual before the attention hyper-connection [T, hc*H] (PLE layers only)",
    "ple.ngram_ids": "PLE hashed n-gram row ids into the concatenated table [T, ngram_heads] int64 (PLE layers only)",
    "ple.ngram_embed": "PLE gathered + dequantised n-gram embedding, heads concatenated [T, ple_embed_dim] (PLE layers only)",
    "attn_hc.mixed": "attention hyper-connection mix: block input [T, H]",
    "attn_hc.inject": "attention hyper-connection injection weights [T, hc]",
    "gdn.in_proj_qkv": "GDN in_proj_qkv output, pre-conv [T, 2*Kd+Vd]",
    "gdn.in_proj_z": "GDN output-gate projection [T, Vd]",
    "gdn.in_proj_b": "GDN beta logits [T, Hv]",
    "gdn.in_proj_a": "GDN decay logits [T, Hv]",
    "gdn.conv_out": "GDN causal conv1d + SiLU output [T, 2*Kd+Vd]",
    "gdn.core_out": "GDN delta-rule output before gated norm [T, Hv, Dv]",
    "gdn.norm_out": "GDN gated RMSNorm output [T*Hv, Dv]",
    "mixer_out": "token mixer (GDN or attention) output [T, H]",
    "attn_combine_out": "residual after attention combine = mlp hyper-connection input [T, hc*H]",
    "mlp_hc.mixed": "MLP hyper-connection mix: MoE block input [T, H]",
    "mlp_hc.inject": "MLP hyper-connection injection weights [T, hc]",
    "router_logits": "router logits [T, E]",
    "topk_weights": "renormalised top-k routing weights [T, k] (router dtype)",
    "topk_ids": "top-k expert ids [T, k] int64",
    "routed_out": "weighted sum of routed expert outputs [T, H]",
    "shared_out": "shared expert MLP output before its gate [T, H]",
    "shared_gate_logit": "shared expert gate logit (pre-sigmoid) [T, 1]",
    "moe_out": "MoE block output = routed + sigmoid(gate) * shared [T, H]",
    "layer_out": "layer output residual [T, hc*H]",
    "state.conv": "GDN conv state after this step [1, 2*Kd+Vd, conv_kernel]",
    "state.recurrent": "GDN recurrent state after this step [1, Hv, Dk, Dv]",
    "state.ple_conv": "PLE dilated short-conv state after this step (PLE layers only)",
    "state.ple_tokens": "PLE n-gram token context after this step, int64 (PLE layers only)",
    "attn.q_proj": "attention q_proj output [T, heads*2*head_dim]; per head the first head_dim are the query, the next head_dim the output gate",
    "attn.k_proj": "attention k_proj output [T, kv_heads*head_dim]",
    "attn.v_proj": "attention v_proj output [T, kv_heads*head_dim]",
    "attn.q_normed": "query after q_norm (RMSNorm, 1 + w), before RoPE [T, heads, head_dim]",
    "attn.k_normed": "key after k_norm (RMSNorm, 1 + w), before RoPE [T, kv_heads, head_dim]",
    "attn.q_rope": "query after RoPE (first rotary_dim dims rotated, rotate_half layout) [T, heads, head_dim]",
    "attn.k_rope": "key after RoPE [T, kv_heads, head_dim]",
    "attn.core_out": "softmax(q k^T * scale + mask) v, before the output gate [T, heads, head_dim]",
    "attn.gated_out": "core_out * sigmoid(gate), flattened: o_proj input [T, heads*head_dim]",
    "indexer.qk_proj": "indexer index_qk_proj output [T, (index_heads + index_kv_heads)*index_head_dim]",
    "indexer.q_normed": "indexer query after q_layernorm (1 + w), before RoPE [T, index_heads, index_head_dim]",
    "indexer.q_rope": "indexer query after RoPE at the current positions [T, index_heads, index_head_dim]",
    "indexer.num_blocks": "complete compress_ratio blocks visible to each query [T] int64",
    "indexer.block_scores": "per-block score sum_h relu(q_h . k_block) / sqrt(index_head_dim) [T, max_blocks], zero padded beyond num_blocks (recomputed with HF's indexer loop; its selection is asserted equal to HF's mask)",
    "indexer.selected_tokens": "token positions the indexer keeps per query: selected blocks' tokens then the incomplete tail block [T, budget + compress_ratio - 1] int64, -1 padded (recomputed, asserted equal to HF's mask)",
    "indexer.mask": "HF indexer output: 1 where a key position is kept [T, kv_len] int64",
    "state.k": "attention key cache after this step (normed, RoPE'd) [1, kv_heads, kv_len, head_dim]",
    "state.v": "attention value cache after this step [1, kv_heads, kv_len, head_dim]",
    "state.indexer_k": "indexer raw key cache after this step (pre k_layernorm, unpooled, no RoPE) [1, kv_len, index_head_dim]",
}


class Recorder:
    def __init__(self, layer_mod) -> None:
        self.layer = layer_mod
        self.out: dict[str, torch.Tensor] = {}
        self._handles = []
        self._fn_patches = []

    def put(self, name: str, t: torch.Tensor) -> None:
        if name in self.out:
            raise RuntimeError(f"stage {name} captured twice")
        self.out[name] = t.detach().clone().contiguous()

    def __enter__(self):
        L = self.layer
        flat = lambda t: t.reshape(-1, t.shape[-1])

        def hook(mod, fn):
            self._handles.append(mod.register_forward_hook(lambda m, a, o: fn(a, o)))

        def pre(mod, fn):
            self._handles.append(mod.register_forward_pre_hook(lambda m, a: fn(a)))

        if L.ple is not None:
            hook(L.ple, lambda a, o: self.put("ple_out", flat(o)))
            hook(L.ple.ple_embedding, lambda a, o: self.put("ple.ngram_embed", flat(o)))
            L.ple.ple_embedding.ngram_embedding.on_rows = lambda ids: self.put(
                "ple.ngram_ids", flat(ids)
            )

        def hc(prefix):
            def f(a, o):
                mixed, _, inject = o
                self.put(f"{prefix}.mixed", flat(mixed))
                self.put(f"{prefix}.inject", flat(inject))

            return f

        hook(L.attn_hyper_connection, hc("attn_hc"))
        pre(L.mlp_hyper_connection, lambda a: self.put("attn_combine_out", flat(a[0])))
        mixer = L.linear_attn if L.layer_type == "linear_attention" else L.self_attn
        if L.layer_type == "linear_attention":
            for p in ("in_proj_qkv", "in_proj_z", "in_proj_b", "in_proj_a"):
                hook(getattr(mixer, p), lambda a, o, p=p: self.put(f"gdn.{p}", flat(o)))
            hook(mixer.norm, lambda a, o: self.put("gdn.norm_out", o))
            self._wrap_fn(
                "causal_conv1d_fn",
                lambda o: self.put("gdn.conv_out", flat(o.transpose(1, 2))),
            )
            self._wrap_fn(
                "causal_conv1d_update",
                lambda o: self.put("gdn.conv_out", flat(o.transpose(1, 2))),
            )
            core = lambda o: self.put(
                "gdn.core_out", o[0].reshape(-1, *o[0].shape[-2:])
            )
            self._wrap_fn("torch_chunk_gated_delta_rule", core)
            self._wrap_fn("torch_recurrent_gated_delta_rule", core)
            hook(mixer, lambda a, o: self.put("mixer_out", flat(o)))
        else:
            self._attention_hooks(mixer, hook, pre, flat)
            hook(mixer, lambda a, o: self.put("mixer_out", flat(o[0])))
        hook(L.mlp_hyper_connection, hc("mlp_hc"))

        def router(a, o):
            logits, weights, ids = o
            self.put("router_logits", logits)
            self.put("topk_weights", weights)
            self.put("topk_ids", ids.to(torch.int64))

        hook(L.mlp.gate, router)
        hook(L.mlp.experts, lambda a, o: self.put("routed_out", o))
        hook(L.mlp.shared_expert, lambda a, o: self.put("shared_out", o))
        hook(L.mlp.shared_expert_gate, lambda a, o: self.put("shared_gate_logit", o))
        hook(L.mlp, lambda a, o: self.put("moe_out", flat(o)))
        return self

    def _attention_hooks(self, A, hook, pre, flat) -> None:
        heads_of = lambda o: o.reshape(-1, *o.shape[-2:])
        hook(A.q_proj, lambda a, o: self.put("attn.q_proj", flat(o)))
        hook(A.k_proj, lambda a, o: self.put("attn.k_proj", flat(o)))
        hook(A.v_proj, lambda a, o: self.put("attn.v_proj", flat(o)))
        hook(A.q_norm, lambda a, o: self.put("attn.q_normed", heads_of(o)))
        hook(A.k_norm, lambda a, o: self.put("attn.k_normed", heads_of(o)))
        ix = A.indexer
        hook(ix.index_qk_proj, lambda a, o: self.put("indexer.qk_proj", flat(o)))
        hook(ix.q_layernorm, lambda a, o: self.put("indexer.q_normed", heads_of(o)))

        def rope(o):
            if isinstance(o, tuple):  # attention q and k: [B, H, T, D]
                self.put("attn.q_rope", heads_of(o[0].transpose(1, 2)))
                self.put("attn.k_rope", heads_of(o[1].transpose(1, 2)))
            elif (
                o.dim() == 4 and "indexer.q_rope" not in self.out
            ):  # indexer q: [B, T, H, D]
                self.put("indexer.q_rope", heads_of(o))
            # 3D outputs are the indexer's pooled block keys, one call per query: not stages.

        self._wrap_fn("apply_rotary_pos_emb", rope)
        self._wrap_fn(
            "eager_attention_forward",
            lambda o: self.put("attn.core_out", heads_of(o[0])),
        )
        pre(A.o_proj, lambda a: self.put("attn.gated_out", flat(a[0])))

        def indexer(args, mask):
            hidden, (full_cos, full_sin), attention_mask, cache = args
            raw_keys = cache.layers[ix.layer_idx].indexer_keys
            ref = indexer_reference(
                ix,
                self.out["indexer.q_rope"],
                raw_keys[0],
                full_cos[0],
                full_sin[0],
                attention_mask,
            )
            kept = mask if mask.dtype == torch.bool else mask == 0
            kept = kept[0, 0]
            if not torch.equal(ref["mask"], kept):
                raise RuntimeError(
                    "indexer reference selection diverges from HF's mask"
                )
            self.put("indexer.num_blocks", ref["num_blocks"])
            self.put("indexer.block_scores", ref["scores"])
            self.put("indexer.selected_tokens", ref["selected"])
            self.put("indexer.mask", kept.to(torch.int64))

        self._handles.append(ix.register_forward_hook(lambda m, a, o: indexer(a, o)))

    def _wrap_fn(self, name: str, on_out) -> None:
        orig = getattr(hf, name)

        def wrapped(*args, **kwargs):
            out = orig(*args, **kwargs)
            on_out(out)
            return out

        setattr(hf, name, wrapped)
        self._fn_patches.append((name, orig))

    def __exit__(self, *exc):
        for h in self._handles:
            h.remove()
        for name, orig in self._fn_patches:
            setattr(hf, name, orig)
        if self.layer.ple is not None:
            self.layer.ple.ple_embedding.ngram_embedding.on_rows = None
        return False


def indexer_reference(ix, q_rope, raw_keys, full_cos, full_sin, attention_mask) -> dict:
    """HF ``Qwen4ExpTextQSAIndexer.forward``'s per-query selection loop, line for line (batch 0),
    also returning the per-block scores. Its kept-token mask is asserted equal to HF's output."""
    visible = (
        attention_mask if attention_mask.dtype == torch.bool else attention_mask == 0
    )
    visible = visible[0, 0]
    T, kv_len = visible.shape
    r = ix.compress_ratio
    max_blocks = kv_len // r
    scores_out = torch.zeros(T, max(max_blocks, 1), dtype=torch.float32)
    num_blocks = torch.zeros(T, dtype=torch.int64)
    selected_out = torch.full((T, ix.token_budget + r - 1), -1, dtype=torch.int64)
    mask = torch.zeros(T, kv_len, dtype=torch.bool)
    for qi in range(T):
        local = torch.nonzero(visible[qi], as_tuple=False).flatten()
        nb = local.shape[-1] // r
        num_blocks[qi] = nb
        if nb > 0:
            block_tok = local[: nb * r].view(nb, r)
            groups = raw_keys.index_select(0, block_tok.flatten()).view(
                *block_tok.shape, ix.index_head_dim
            )
            pooled = ix.k_layernorm(groups.float().mean(dim=1).to(raw_keys.dtype))
            starts = block_tok[:, 0]
            block_k = hf.apply_rotary_pos_emb(
                pooled.unsqueeze(1),
                cos=full_cos.index_select(0, starts),
                sin=full_sin.index_select(0, starts),
            ).squeeze(1)
            sc = torch.matmul(
                q_rope[qi].float(), block_k.float().transpose(-1, -2)
            ).transpose(-1, -2)
            sc = torch.relu(sc).sum(dim=-1) / (ix.index_head_dim**0.5)
            scores_out[qi, :nb] = sc
            sel_blocks = sc.topk(min(ix.block_topk, nb), dim=0).indices
            sel = block_tok.index_select(0, sel_blocks).flatten()
        else:
            sel = torch.tensor([], dtype=torch.int64)
        sel = torch.cat([sel, local[nb * r :]]).to(torch.int64)
        selected_out[qi, : sel.numel()] = sel
        mask[qi, sel] = True
    return {
        "scores": scores_out,
        "num_blocks": num_blocks,
        "selected": selected_out,
        "mask": mask,
    }


def attention_inputs(rotary, residual, past: int):
    """What ``Qwen4ExpTextModel.forward`` hands a full-attention layer for text-only input with no
    padding: cos/sin over all positions so far (all three mRoPE axes equal the text position), and
    the eager causal float mask (0 visible, dtype min hidden) of shape [1, 1, T, past + T]."""
    T = residual.shape[1]
    pos = torch.arange(past + T).view(1, 1, -1).expand(3, 1, -1)
    cos, sin = rotary(residual, pos)
    q = torch.arange(T).view(-1, 1) + past
    k = torch.arange(past + T).view(1, -1)
    mask = torch.where(k <= q, 0.0, torch.finfo(residual.dtype).min).to(residual.dtype)
    return (cos, sin), mask.view(1, 1, T, past + T)


def run_step(
    layer_mod, residual, cache, layer: int, ple_ids=None, attn=None
) -> dict[str, torch.Tensor]:
    with Recorder(layer_mod) as rec, torch.no_grad():
        rec.put("residual_in", residual.reshape(-1, residual.shape[-1]))
        out = layer_mod(
            residual,
            position_embeddings=attn[0] if attn else None,
            attention_mask=attn[1] if attn else None,
            conv_mask=None,
            past_key_values=cache,
            ple_input_ids=ple_ids,
        )
        rec.put("layer_out", out.reshape(-1, out.shape[-1]))
    if "gdn.conv_out" in rec.out:
        # The prefill conv runs over cached + new positions; keep the current step's T rows.
        rec.out["gdn.conv_out"] = rec.out["gdn.conv_out"][
            -residual.shape[1] :
        ].contiguous()
    if layer_mod.layer_type == "linear_attention":
        cl = cache.layers[layer]
        rec.out["state.conv"] = cl.conv_states[0].detach().clone()
        rec.out["state.recurrent"] = cl.recurrent_states[0].detach().clone()
    if layer_mod.layer_type != "linear_attention":
        cl = cache.layers[layer]
        rec.out["state.k"] = cl.keys.detach().clone()
        rec.out["state.v"] = cl.values.detach().clone()
        rec.out["state.indexer_k"] = cl.indexer_keys.detach().clone()
    if layer_mod.ple is not None:
        cl = cache.layers[layer]
        rec.out["state.ple_conv"] = cl.conv_states[1].detach().clone()
        rec.out["state.ple_tokens"] = cl.conv_states[2].detach().clone().to(torch.int64)
    return rec.out


def run_sequence(layer_mod, tc, layer, residuals, ids_per_step, names):
    """Prefill then single-token decode steps through one layer with one cache; {step: stages}."""
    cache = DynamicCache(config=tc)
    rotary = (
        hf.Qwen4ExpTextRotaryEmbedding(config=tc)
        if layer_mod.layer_type != "linear_attention"
        else None
    )
    out, past = {}, 0
    for name, r, ids in zip(names, residuals, ids_per_step):
        attn = attention_inputs(rotary, r, past) if rotary is not None else None
        out[name] = run_step(layer_mod, r, cache, layer, ids, attn)
        past += r.shape[1]
    return out


# ---------------------------------------------------------------------------------------------
# Whole-model chain: every layer, one in memory at a time, then the final mixer and lm_head.
# ---------------------------------------------------------------------------------------------
TOP_LOGPROBS = 20


def run_layer_plain(
    layer_mod, tc, layer, residuals, ids_per_step
) -> list[torch.Tensor]:
    """``run_sequence`` without stage capture: the same calls, so the same numbers."""
    cache = DynamicCache(config=tc)
    rotary = (
        hf.Qwen4ExpTextRotaryEmbedding(config=tc)
        if layer_mod.layer_type != "linear_attention"
        else None
    )
    outs, past = [], 0
    with torch.no_grad():
        for r, ids in zip(residuals, ids_per_step):
            attn = attention_inputs(rotary, r, past) if rotary is not None else None
            outs.append(
                layer_mod(
                    r,
                    position_embeddings=attn[0] if attn else None,
                    attention_mask=attn[1] if attn else None,
                    conv_mask=None,
                    past_key_values=cache,
                    ple_input_ids=ids,
                )
                .detach()
                .clone()
            )
            past += r.shape[1]
    return outs


def build_head(ckpt: Checkpoint, tc, dtype: torch.dtype):
    """The top-level hyper-connection mixer (no combine) and lm_head."""
    torch.set_default_dtype(dtype)
    try:
        with torch.device("meta"):
            mixer = hf.Qwen4ExpTextGatedResidual(tc, use_combine=False)
            head = torch.nn.Linear(tc.hidden_size, tc.vocab_size, bias=False)
    finally:
        torch.set_default_dtype(torch.float32)
    mixer = mixer.to_empty(device="cpu").eval()
    head = head.to_empty(device="cpu").eval()
    with torch.no_grad():
        for name, param in mixer.named_parameters():
            param.copy_(
                ckpt.get("model.language_model.hyper_connection_mixer." + name)
                .to(torch.float32)
                .to(dtype)
            )
        head.weight.copy_(ckpt.get("lm_head.weight").to(torch.float32).to(dtype))
    return mixer, head


def model_chain(
    args,
    ckpt,
    tc,
    embeds,
    ids_per_step,
    step_names,
    all_ids,
    ids_prefill,
    ids_decode,
    prompt,
    index_sha,
):
    out = args.out / "model"
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    n_layers = tc.num_hidden_layers
    lo, hi = args.chain_layers if args.chain_layers else (0, n_layers - 1)

    def work_file(layer: int, dname: str) -> Path:
        return work / f"layer-{layer:03d}-{dname}.safetensors"

    for layer in range(lo, hi + 1):
        for dname, dtype in DTYPES.items():
            wf = work_file(layer, dname)
            if wf.exists():
                continue
            t0 = time.time()
            if layer == 0:
                residuals = [
                    e.to(torch.float32).to(dtype).unsqueeze(0).repeat(1, 1, tc.hc_count)
                    for e in embeds
                ]
            else:
                prev = work_file(layer - 1, dname)
                if not prev.exists():
                    raise SystemExit(
                        f"layer {layer} needs {prev}: run the earlier layers first"
                    )
                prev_t = load_file(str(prev))
                residuals = [prev_t[n].unsqueeze(0) for n in step_names]
            mod = build_layer(ckpt, tc, layer, dtype)
            outs = run_layer_plain(mod, tc, layer, residuals, ids_per_step)
            del mod
            # Drop the shard mmaps too, or every layer's touched pages stay mapped (RSS).
            ckpt._files.clear()
            gc.collect()
            tmp = wf.with_suffix(".tmp")
            save_file(
                {
                    n: o.reshape(-1, o.shape[-1]).contiguous()
                    for n, o in zip(step_names, outs)
                },
                str(tmp),
            )
            tmp.rename(wf)
            print(f"chain layer {layer} {dname}: {time.time() - t0:.1f}s", flush=True)

    if not all(work_file(n_layers - 1, d).exists() for d in DTYPES):
        print(f"chain: layers {lo}..{hi} done; rerun to continue", flush=True)
        return

    results = {}
    for dname, dtype in DTYPES.items():
        mixer, head = build_head(ckpt, tc, dtype)
        per_layer = [load_file(str(work_file(l, dname))) for l in range(n_layers)]
        steps = {}
        for i, (name, e) in enumerate(zip(step_names, embeds)):
            st = {
                "residual_in": e.to(torch.float32)
                .to(dtype)
                .repeat(1, tc.hc_count)
                .contiguous()
            }
            for l in range(n_layers):
                st[f"layer_out.{l}"] = per_layer[l][name]
            with torch.no_grad():
                mixed = mixer(per_layer[-1][name].unsqueeze(0))
                logits = head(mixed)
            st["mixer_out"] = mixed.reshape(-1, mixed.shape[-1]).contiguous()
            st["logits"] = logits.reshape(-1, logits.shape[-1]).contiguous()
            lp = torch.log_softmax(st["logits"].float(), dim=-1)
            top = lp.topk(TOP_LOGPROBS, dim=-1)
            st["top_logprobs.ids"] = top.indices.to(torch.int64).contiguous()
            st["top_logprobs.values"] = top.values.contiguous()
            steps[name] = st
        results[dname] = steps
        del mixer, head
        gc.collect()

    tolerances = {}
    for dname, steps in results.items():
        d = out / dname
        d.mkdir(parents=True, exist_ok=True)
        for name, st in steps.items():
            save_file(st, str(d / f"{name}.safetensors"))
        if dname != "fp32":
            ref = results["fp32"]
            tol = {}
            for name in steps:
                keys = [k for k in ref[name] if not k.startswith("top_logprobs.")]
                tol[name] = compare(
                    {k: ref[name][k] for k in keys}, {k: steps[name][k] for k in keys}
                )
                a = ref[name]["logits"].float().argmax(-1)
                b = steps[name]["logits"].float().argmax(-1)
                tol[name]["logits.top1_agreement"] = {
                    "agree": int((a == b).sum()),
                    "positions": a.numel(),
                }
            tolerances[f"w4a16/{dname}_vs_fp32"] = tol
    (out / "tolerances.json").write_text(json.dumps(tolerances, indent=1))
    manifest = {
        "model": str(args.model),
        "model_index_sha256": index_sha,
        "mode": "w4a16 (NVFP4 weights densified with ModelOpt dequant; activations unquantised)",
        "prompt": {
            "user_message": USER_MESSAGE,
            "templated": prompt,
            "token_ids": ids_prefill,
        },
        "decode": {
            "continuation": CONTINUATION,
            "token_ids": ids_decode,
            "note": "teacher-forced",
        },
        "steps": step_names,
        "keys": {
            "residual_in": "layer 0 input: token embedding repeated over hc streams [T, hc*H]",
            "layer_out.L": "output residual of layer L (plain decimal L, 0..47) [T, hc*H]",
            "mixer_out": "top-level hyper_connection_mixer output (no combine) [T, H]",
            "logits": "lm_head(mixer_out) in the run dtype [T, vocab]",
            "top_logprobs.ids": f"top-{TOP_LOGPROBS} token ids of log_softmax(logits.float()) per position [T, {TOP_LOGPROBS}] int64",
            "top_logprobs.values": f"their log-probabilities, fp32 [T, {TOP_LOGPROBS}]",
        },
        "dtypes": {
            "fp32": "all parameters and activations fp32 (truth)",
            "bf16": "all parameters bf16 as released (budget reference), stored in bf16",
        },
        "chain": "each layer runs alone (one in memory at a time) on the previous layer's stored output of the same dtype and step; per-layer caches carry decode state",
        "versions": {
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "python": platform.python_version(),
        },
        "threads": args.threads,
        "deterministic_algorithms": True,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"wrote {out}", flush=True)


def long_layer(
    args,
    ckpt,
    tc,
    layer: int,
    embeds,
    ids_per_step,
    step_names,
    user_message,
    prompt,
    ids_prefill,
    ids_decode,
    index_sha,
):
    """w4a16 stage fixtures for ``layer`` on the long workload. Layers before it run in memory
    without capture (``run_layer_plain``: the same calls, so the same numbers)."""
    out_dir = args.out / f"layer-{layer:03d}-long"
    results = {}
    for dname, dtype in DTYPES.items():
        t0 = time.time()
        residuals = [
            e.to(torch.float32).to(dtype).unsqueeze(0).repeat(1, 1, tc.hc_count)
            for e in embeds
        ]
        for prev in range(layer):
            mod = build_layer(ckpt, tc, prev, dtype)
            residuals = run_layer_plain(mod, tc, prev, residuals, ids_per_step)
            del mod
            gc.collect()
            print(f"  chain layer {prev} {dname}: {time.time() - t0:.1f}s", flush=True)
        mod = build_layer(ckpt, tc, layer, dtype)
        results[dname] = run_sequence(
            mod, tc, layer, residuals, ids_per_step, step_names
        )
        del mod, residuals
        gc.collect()
        print(f"layer {layer} {dname}: {time.time() - t0:.1f}s", flush=True)

    pre = results["fp32"]["prefill"]
    if "indexer.mask" in pre:
        mask = pre["indexer.mask"].bool()
        causal = torch.ones_like(mask).tril()
        dropped = (causal & ~mask).sum(dim=1)
        if int(dropped.max()) == 0:
            raise RuntimeError("long workload does not prune any indexer block")
        print(
            f"indexer pruning: {int((dropped > 0).sum())} of {mask.shape[0]} prefill queries drop "
            f"keys, up to {int(dropped.max())} tokens ({int(dropped.max()) // tc.indexer_compress_ratio} blocks)",
            flush=True,
        )

    for dname, steps in results.items():
        d = out_dir / "w4a16" / dname
        d.mkdir(parents=True, exist_ok=True)
        for step, stages in steps.items():
            save_file(stages, str(d / f"{step}.safetensors"))
    tolerances = {
        "w4a16/bf16_vs_fp32": {
            s: compare(results["fp32"][s], results["bf16"][s]) for s in step_names
        }
    }
    (out_dir / "tolerances.json").write_text(json.dumps(tolerances, indent=1))
    manifest = {
        "model": str(args.model),
        "model_index_sha256": index_sha,
        "layer": layer,
        "layer_type": tc.layer_types[layer],
        "prompt": {
            "user_message": user_message,
            "templated": prompt,
            "token_ids": ids_prefill,
        },
        "decode": {
            "continuation": CONTINUATION,
            "token_ids": ids_decode,
            "note": "teacher-forced",
        },
        "steps": step_names,
        "input": f"layers 0..{layer - 1} chained in memory from the token embeddings (same mode and dtype)",
        "modes": {
            "w4a16": "NVFP4 weights densified with ModelOpt dequant; activations unquantised; HF experts forward",
        },
        "dtypes": {
            "fp32": "all parameters and activations fp32 (bf16 checkpoint values upcast exactly)",
            "bf16": "all parameters bf16 as released; dequantised expert weights computed in fp32 then cast to bf16",
        },
        "stages": {k: v for k, v in STAGES.items() if k in results["fp32"]["prefill"]},
        "versions": {
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "python": platform.python_version(),
        },
        "threads": args.threads,
        "deterministic_algorithms": True,
        "randomness": "none (no sampling; fixed inputs)",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"wrote {out_dir}")


def compare(ref: dict, other: dict) -> dict:
    """Per-stage deltas of ``other`` against fp32 ``ref``."""
    rows = {}
    for k, a in ref.items():
        b = other[k]
        if a.dtype == torch.int64 and k != "topk_ids":
            # Other integer stages (indexer selections, row ids, masks): exact comparison.
            rows[k] = {
                "mismatches": int((a != b).sum()) if a.shape == b.shape else a.numel(),
                "numel": a.numel(),
            }
            continue
        if a.dtype == torch.int64:
            # Top-k ids: order-insensitive. Fraction of reference (token, expert) picks missing.
            missing = sum(
                len(set(ra.tolist()) - set(rb.tolist())) for ra, rb in zip(a, b)
            )
            rows[k] = {
                "set_mismatch_frac": missing / a.numel(),
                "tokens_differing": int(
                    sum(set(ra.tolist()) != set(rb.tolist()) for ra, rb in zip(a, b))
                ),
                "tokens": a.shape[0],
            }
            continue
        a32, b32 = a.to(torch.float64), b.to(torch.float64)
        d = (a32 - b32).abs()
        scale = a32.abs().max().item() or 1.0
        cos = torch.nn.functional.cosine_similarity(
            a32.flatten(), b32.flatten(), dim=0
        ).item()
        rows[k] = {
            "max_abs": d.max().item(),
            "max_abs_over_absmax": d.max().item() / scale,
            "rms_rel": (
                d.pow(2).mean().sqrt() / a32.pow(2).mean().sqrt().clamp_min(1e-30)
            ).item(),
            "cosine": cos,
        }
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--model",
        type=Path,
        default=Path(DEFAULT_MODEL) if DEFAULT_MODEL else None,
        required=DEFAULT_MODEL is None,
        help="release checkpoint directory (default: $OOMINF_RELEASE_CHECKPOINT)",
    )
    ap.add_argument("--out", type=Path, default=Path(DEFAULT_OUT))
    ap.add_argument("--layers", type=int, nargs="+", default=[0])
    ap.add_argument(
        "--model-chain",
        action="store_true",
        help="whole-model w4a16 chain into <out>/model (resumable; see --chain-layers)",
    )
    ap.add_argument(
        "--chain-layers",
        type=int,
        nargs=2,
        metavar=("FIRST", "LAST"),
        help="with --model-chain: only run these layers this invocation",
    )
    ap.add_argument(
        "--long",
        action="store_true",
        help="long-context workload: chain layers 0..L-1 in memory and write w4a16 stage "
        "fixtures for the single --layers L into <out>/layer-LLL-long",
    )
    ap.add_argument(
        "--threads",
        type=int,
        default=8,
        help="fixed: CPU matmul reduction order depends on it",
    )
    args = ap.parse_args()

    torch.set_num_threads(args.threads)
    torch.use_deterministic_algorithms(True)
    if torch.cuda.is_available():
        raise SystemExit(
            "run with CUDA_VISIBLE_DEVICES= (fixtures are CPU-only by design)"
        )

    ckpt = Checkpoint(args.model)
    cfg = Qwen4ExpConfig.from_pretrained(args.model)
    tc = cfg.text_config
    # HF's reference loops, never an optimised backend.
    tc._experts_implementation = "eager"
    tc._attn_implementation = "eager"
    tok = AutoTokenizer.from_pretrained(args.model)
    user_message = (
        LONG_INSTRUCTION + LONG_PROMPT_FILE.read_text() if args.long else USER_MESSAGE
    )
    prompt = tok.apply_chat_template(
        [{"role": "user", "content": user_message}],
        tokenize=False,
        add_generation_prompt=True,
    )
    ids_prefill = tok(prompt, add_special_tokens=False)["input_ids"]
    ids_decode = tok(CONTINUATION, add_special_tokens=False)["input_ids"][:DECODE_STEPS]
    all_ids = ids_prefill + ids_decode
    emb_rows = ckpt.rows("model.language_model.embed_tokens.weight", all_ids)
    ids_t = torch.tensor([all_ids])
    index_sha = hashlib.sha256(
        (args.model / "model.safetensors.index.json").read_bytes()
    ).hexdigest()

    step_names = ["prefill"] + [f"decode-{i + 1}" for i in range(len(ids_decode))]
    ids_per_step = [ids_t[:, : len(ids_prefill)]] + [
        ids_t[:, len(ids_prefill) + i :][:, :1] for i in range(len(ids_decode))
    ]
    embeds = [emb_rows[: len(ids_prefill)]] + [
        emb_rows[len(ids_prefill) + i :][:1] for i in range(len(ids_decode))
    ]

    def residuals_for(
        layer: int, mode: str, dname: str, dtype: torch.dtype
    ) -> list[torch.Tensor]:
        if layer == 0:
            return [
                e.to(torch.float32).to(dtype).unsqueeze(0).repeat(1, 1, tc.hc_count)
                for e in embeds
            ]
        prev = args.out / f"layer-{layer - 1:03d}" / mode / dname
        if not prev.exists():
            raise SystemExit(
                f"layer {layer} needs {prev}: generate layer {layer - 1} first"
            )
        return [
            load_file(str(prev / f"{n}.safetensors"))["layer_out"].unsqueeze(0)
            for n in step_names
        ]

    if args.long:
        if len(args.layers) != 1:
            raise SystemExit("--long takes exactly one --layers value")
        long_layer(
            args,
            ckpt,
            tc,
            args.layers[0],
            embeds,
            ids_per_step,
            step_names,
            user_message,
            prompt,
            ids_prefill,
            ids_decode,
            index_sha,
        )
        return

    if args.model_chain:
        model_chain(
            args,
            ckpt,
            tc,
            embeds,
            ids_per_step,
            step_names,
            all_ids,
            ids_prefill,
            ids_decode,
            prompt,
            index_sha,
        )
        return

    for layer in sorted(args.layers):
        out_dir = args.out / f"layer-{layer:03d}"
        out_dir.mkdir(parents=True, exist_ok=True)
        scales = load_input_scales(ckpt, tc, layer)
        results = {}
        for dname, dtype in DTYPES.items():
            t0 = time.time()
            mod = build_layer(ckpt, tc, layer, dtype)
            r16 = residuals_for(layer, "w4a16", dname, dtype)
            results[("w4a16", dname)] = run_sequence(
                mod, tc, layer, r16, ids_per_step, step_names
            )
            with patched_experts(mod.mlp.experts, None):
                mirror = run_sequence(mod, tc, layer, r16, ids_per_step, step_names)
            for step, stages in results[("w4a16", dname)].items():
                for k, v in stages.items():
                    if not torch.equal(v, mirror[step][k]):
                        raise RuntimeError(
                            f"experts mirror diverges from HF at {dname}/{step}/{k}"
                        )
            with patched_experts(mod.mlp.experts, scales):
                r4 = residuals_for(layer, "w4a4", dname, dtype)
                results[("w4a4", dname)] = run_sequence(
                    mod, tc, layer, r4, ids_per_step, step_names
                )
            del mod
            gc.collect()
            print(f"layer {layer} {dname}: {time.time() - t0:.1f}s", flush=True)

        tolerances = {}
        for (mode, dname), steps in results.items():
            d = out_dir / mode / dname
            d.mkdir(parents=True, exist_ok=True)
            for step, stages in steps.items():
                save_file(stages, str(d / f"{step}.safetensors"))
            if dname != "fp32":
                ref = results[(mode, "fp32")]
                tolerances[f"{mode}/{dname}_vs_fp32"] = {
                    s: compare(ref[s], steps[s]) for s in steps
                }
        ref = results[("w4a16", "fp32")]
        tolerances["w4a4_vs_w4a16/fp32"] = {
            s: compare(ref[s], results[("w4a4", "fp32")][s]) for s in ref
        }
        (out_dir / "tolerances.json").write_text(json.dumps(tolerances, indent=1))

        manifest = {
            "model": str(args.model),
            "model_index_sha256": index_sha,
            "layer": layer,
            "layer_type": tc.layer_types[layer],
            "prompt": {
                "user_message": USER_MESSAGE,
                "templated": prompt,
                "token_ids": ids_prefill,
            },
            "decode": {
                "continuation": CONTINUATION,
                "token_ids": ids_decode,
                "note": "teacher-forced",
            },
            "steps": step_names,
            "input": "token embeddings repeated over hc streams"
            if layer == 0
            else f"layer-{layer - 1:03d} layer_out of the same mode, dtype and step (chained)",
            "modes": {
                "w4a16": "NVFP4 weights densified with ModelOpt dequant; activations unquantised; HF experts forward",
                "w4a4": "as w4a16 plus ModelOpt NVFP4 activation fake-quant (released input_scale) on routed expert inputs",
            },
            "dtypes": {
                "fp32": "all parameters and activations fp32 (bf16 checkpoint values upcast exactly)",
                "bf16": "all parameters bf16 as released; dequantised expert weights computed in fp32 then cast to bf16",
            },
            "stages": {
                k: v
                for k, v in STAGES.items()
                if k in results[("w4a16", "fp32")]["prefill"]
            },
            "versions": {
                "torch": torch.__version__,
                "transformers": transformers.__version__,
                "python": platform.python_version(),
            },
            "threads": args.threads,
            "deterministic_algorithms": True,
            "randomness": "none (no sampling; fixed inputs)",
        }
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
        print(f"wrote {out_dir}")


if __name__ == "__main__":
    main()
