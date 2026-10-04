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

DEFAULT_MODEL = "/var/lib/longhorn/nvme-02/freetoken/models/flash-next-nvfp4"
DEFAULT_OUT = "/disks/nvme-02/src/oominf-data/fixtures/qwen38-flash"

# Fixed workload. The user message goes through the checkpoint's own chat template; decode tokens
# are teacher-forced from CONTINUATION (a single layer cannot produce real next tokens).
USER_MESSAGE = "In two short sentences, explain why a sparse mixture-of-experts model can run on a gaming PC."
CONTINUATION = "The user asks why a sparse"
DECODE_STEPS = 3

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


def run_step(
    layer_mod, residual, cache, layer: int, ple_ids=None
) -> dict[str, torch.Tensor]:
    with Recorder(layer_mod) as rec, torch.no_grad():
        rec.put("residual_in", residual.reshape(-1, residual.shape[-1]))
        out = layer_mod(
            residual,
            position_embeddings=None,
            attention_mask=None,
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
    if layer_mod.ple is not None:
        cl = cache.layers[layer]
        rec.out["state.ple_conv"] = cl.conv_states[1].detach().clone()
        rec.out["state.ple_tokens"] = cl.conv_states[2].detach().clone().to(torch.int64)
    return rec.out


def run_sequence(layer_mod, tc, layer, residuals, ids_per_step, names):
    """Prefill then single-token decode steps through one layer with one cache; {step: stages}."""
    cache = DynamicCache(config=tc)
    return {
        name: run_step(layer_mod, r, cache, layer, ids)
        for name, r, ids in zip(names, residuals, ids_per_step)
    }


def compare(ref: dict, other: dict) -> dict:
    """Per-stage deltas of ``other`` against fp32 ``ref``."""
    rows = {}
    for k, a in ref.items():
        b = other[k]
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
    ap.add_argument("--model", type=Path, default=Path(DEFAULT_MODEL))
    ap.add_argument("--out", type=Path, default=Path(DEFAULT_OUT))
    ap.add_argument("--layers", type=int, nargs="+", default=[0])
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
    prompt = tok.apply_chat_template(
        [{"role": "user", "content": USER_MESSAGE}],
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
            "stages": STAGES,
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
