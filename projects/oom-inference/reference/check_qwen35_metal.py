"""Compare Metal logits with an independent, layer-streamed MLX fp32 reference.

Reads the original safetensors checkpoint, not the converted expert records.
Requires numpy and mlx on Apple Silicon. Run the Rust `logits` example first.
Each token feeds all 40 layers, carrying recurrent and attention state. The LM
head is read in chunks so the reference also runs on a 16 GiB Mac.
"""

import argparse
import json
import mmap
import pathlib
import time

import mlx.core as mx
import numpy as np


class Checkpoint:
    def __init__(self, directory):
        self.file = open(directory / "model.safetensors", "rb")
        self.map = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
        length = int.from_bytes(self.map[:8], "little")
        self.start = 8 + length
        self.header = json.loads(self.map[8 : self.start])

    def array(self, name, first=None, count=None):
        meta = self.header[name]
        dtype = (
            np.uint16
            if meta["dtype"] == "BF16"
            else np.float32
            if meta["dtype"] == "F32"
            else np.uint8
        )
        shape = meta["shape"]
        offset = self.start + meta["data_offsets"][0]
        if first is not None:
            shape = [count, *shape[1:]]
            offset += first * int(np.prod(meta["shape"][1:])) * np.dtype(dtype).itemsize
        result = np.ndarray(shape, dtype=dtype, buffer=self.map, offset=offset)
        if meta["dtype"] == "BF16":
            result = (result.astype(np.uint32) << 16).view(np.float32)
        return mx.array(result)

    def matrix(self, name, x):
        if self.header[name + ".weight"]["dtype"] == "BF16":
            return x @ self.array(name + ".weight").T
        packed = self.array(name + ".weight")
        codes = mx.stack([packed & 15, packed >> 4], axis=-1).reshape(
            packed.shape[0], -1
        )
        table = mx.array(
            [
                0.0,
                0.5,
                1.0,
                1.5,
                2.0,
                3.0,
                4.0,
                6.0,
                -0.0,
                -0.5,
                -1.0,
                -1.5,
                -2.0,
                -3.0,
                -4.0,
                -6.0,
            ]
        )
        raw = self.array(name + ".weight_scale").astype(mx.uint32)
        exponent = (raw >> 3) & 15
        fraction = raw & 7
        scales = mx.where(
            exponent == 0,
            fraction / 512.0,
            (1.0 + fraction / 8.0) * mx.power(2.0, exponent.astype(mx.int32) - 7),
        )
        scales = mx.where((raw & 128) != 0, -scales, scales)
        weight = table[codes] * mx.repeat(scales, 16, axis=1)
        return (x @ weight.T) * self.array(name + ".weight_scale_2")


def sigmoid(x):
    return 1.0 / (1.0 + mx.exp(-x))


def silu(x):
    return x * sigmoid(x)


def rms(x, weight, eps, plus=0.0):
    return mx.fast.rms_norm(x, weight + plus, eps)


def rotate(x, pos, dimensions, theta):
    half = dimensions // 2
    angle = pos * mx.power(theta, -2.0 * mx.arange(half, dtype=mx.float32) / dimensions)
    a, b = x[:, :half], x[:, half:dimensions]
    return mx.concatenate(
        [
            a * mx.cos(angle) - b * mx.sin(angle),
            b * mx.cos(angle) + a * mx.sin(angle),
            x[:, dimensions:],
        ],
        axis=-1,
    )


def run(args):
    root = pathlib.Path(args.source)
    checkpoint = Checkpoint(root)
    config = json.loads((root / "config.json").read_text())["text_config"]
    nk, nv = config["linear_num_key_heads"], config["linear_num_value_heads"]
    dk, dv = config["linear_key_head_dim"], config["linear_value_head_dim"]
    qh, kh, d = (
        config["num_attention_heads"],
        config["num_key_value_heads"],
        config["head_dim"],
    )
    key, value = nk * dk, nv * dv
    width = key * 2 + value
    conv = config["linear_conv_kernel_dim"]
    eps = config["rms_norm_eps"]
    rope = config["rope_parameters"]
    rotary = int(d * rope["partial_rotary_factor"])
    states = {}
    started = time.monotonic()
    mx.set_cache_limit(128 << 20)
    prefix = "model.language_model"
    for pos, token in enumerate(args.tokens):
        x = checkpoint.array(prefix + ".embed_tokens.weight", token, 1).reshape(-1)
        for layer in range(config["num_hidden_layers"]):
            p = f"{prefix}.layers.{layer}"
            mat = lambda name, x: checkpoint.matrix(p + "." + name, x)
            weight = lambda name: checkpoint.array(p + "." + name)
            inp = rms(x, weight("input_layernorm.weight"), eps, 1.0)
            if (layer + 1) % config["full_attention_interval"]:
                qkv = mat("linear_attn.in_proj_qkv", inp)
                z = mat("linear_attn.in_proj_z", inp).reshape(nv, dv)
                a = mat("linear_attn.in_proj_a", inp) + weight("linear_attn.dt_bias")
                decay = mx.exp(
                    -mx.exp(weight("linear_attn.A_log"))
                    * (mx.maximum(a, 0.0) + mx.logaddexp(0.0, -mx.abs(a)))
                )
                beta = sigmoid(mat("linear_attn.in_proj_b", inp))
                history, state = states.get(
                    layer, (mx.zeros((conv - 1, width)), mx.zeros((nv, dv, dk)))
                )
                history = mx.concatenate([history, qkv[None]], axis=0)
                convolved = silu(
                    mx.sum(
                        history
                        * weight("linear_attn.conv1d.weight").reshape(width, conv).T,
                        axis=0,
                    )
                )
                history = history[1:]
                q = convolved[:key].reshape(nk, dk)
                k = convolved[key : 2 * key].reshape(nk, dk)
                v = convolved[2 * key :].reshape(nv, dv)
                q = (
                    q
                    * mx.rsqrt(mx.sum(q * q, axis=-1, keepdims=True) + 1e-6)
                    / np.sqrt(dk)
                )
                k = k * mx.rsqrt(mx.sum(k * k, axis=-1, keepdims=True) + 1e-6)
                q, k = mx.repeat(q, nv // nk, axis=0), mx.repeat(k, nv // nk, axis=0)
                state = state * decay[:, None, None]
                prediction = mx.sum(state * k[:, None, :], axis=-1)
                correction = (v - prediction) * beta[:, None]
                state = state + correction[:, :, None] * k[:, None, :]
                out = mx.sum(state * q[:, None, :], axis=-1)
                out = rms(out, weight("linear_attn.norm.weight"), eps) * silu(z)
                attention = mat("linear_attn.out_proj", out.reshape(-1))
                states[layer] = (history, state)
            else:
                projection = mat("self_attn.q_proj", inp).reshape(qh, 2 * d)
                q, gate = projection[:, :d], projection[:, d:]
                q = rms(q, weight("self_attn.q_norm.weight"), eps, 1.0)
                k = rms(
                    mat("self_attn.k_proj", inp).reshape(kh, d),
                    weight("self_attn.k_norm.weight"),
                    eps,
                    1.0,
                )
                v = mat("self_attn.v_proj", inp).reshape(kh, d)
                q, k = (
                    rotate(q, pos, rotary, rope["rope_theta"]),
                    rotate(k, pos, rotary, rope["rope_theta"]),
                )
                old_k, old_v = states.get(
                    layer, (mx.zeros((0, kh, d)), mx.zeros((0, kh, d)))
                )
                keys, values = (
                    mx.concatenate([old_k, k[None]]),
                    mx.concatenate([old_v, v[None]]),
                )
                states[layer] = keys, values
                keys = mx.repeat(keys, qh // kh, axis=1).transpose(1, 0, 2)
                values = mx.repeat(values, qh // kh, axis=1).transpose(1, 0, 2)
                scores = mx.sum(q[:, None, :] * keys, axis=-1) / np.sqrt(d)
                out = mx.sum(mx.softmax(scores, axis=-1)[:, :, None] * values, axis=1)
                attention = mat("self_attn.o_proj", (out * sigmoid(gate)).reshape(-1))
            residual = x + attention
            inp = rms(residual, weight("post_attention_layernorm.weight"), eps, 1.0)
            router = mat("mlp.gate", inp)
            mx.eval(router)
            indices = np.lexsort((np.arange(config["num_experts"]), -np.array(router)))[
                : config["num_experts_per_tok"]
            ]
            probabilities = mx.softmax(router[mx.array(indices.astype(np.int32))])
            shared = silu(mat("mlp.shared_expert.gate_proj", inp)) * mat(
                "mlp.shared_expert.up_proj", inp
            )
            mixture = mat("mlp.shared_expert.down_proj", shared) * sigmoid(
                mat("mlp.shared_expert_gate", inp)
            )
            for slot, expert in enumerate(indices):
                stem = f"mlp.experts.{expert}."
                product = silu(mat(stem + "gate_proj", inp)) * mat(
                    stem + "up_proj", inp
                )
                mixture = (
                    mixture + mat(stem + "down_proj", product) * probabilities[slot]
                )
            x = residual + mixture
            mx.eval(x, *states[layer])
        x = rms(x, checkpoint.array(prefix + ".norm.weight"), eps, 1.0)
        head = []
        for first in range(0, config["vocab_size"], 4096):
            n = min(4096, config["vocab_size"] - first)
            logits = x @ checkpoint.array("lm_head.weight", first, n).T
            mx.eval(logits)
            head.append(np.array(logits))
        reference = np.concatenate(head)
        actual = np.fromfile(pathlib.Path(args.metal) / f"{pos}.f32", dtype="<f4")
        diff = actual - reference
        relative_rmse = float(np.linalg.norm(diff) / np.linalg.norm(reference))
        report = {
            "step": pos,
            "token": token,
            "metal_top1": int(actual.argmax()),
            "reference_top1": int(reference.argmax()),
            "relative_rmse": relative_rmse,
            "max_abs": float(np.abs(diff).max()),
            "seconds": time.monotonic() - started,
        }
        print(json.dumps(report), flush=True)
        assert actual.shape == reference.shape and np.isfinite(actual).all()
        assert actual.argmax() == reference.argmax(), report
        assert relative_rmse < 1e-4 and np.abs(diff).max() < 0.02, report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--metal", required=True)
    parser.add_argument(
        "--tokens",
        type=lambda v: [int(t) for t in v.split(",")],
        default=[100, 200, 300],
    )
    run(parser.parse_args())
