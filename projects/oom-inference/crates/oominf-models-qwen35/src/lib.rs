//! Qwen3.5 text MoE decode on Apple Silicon, with exact released NVFP4 weights.
#![cfg(target_os = "macos")]

use anyhow::{Context, Result, bail, ensure};
use oominf_core::{
    Elementwise, ExpertSource, ExpertStats, ExpertTiers, Linear, Memory, Model, Norm, Session,
};
use oominf_format::Model as Files;
use oominf_metal::{Dev, Gpu};
use oominf_tiers::host::IoConfig;
use oominf_tiers::policy::Policy;
use oominf_tiers::{TierSizes, TieredExperts};
use std::fs::File;
use std::os::unix::fs::FileExt;
use std::rc::Rc;
use std::sync::{Arc, Mutex};

const ROOT: &str = "model.language_model";
const GIB: u64 = 1 << 30;

#[derive(Clone)]
struct Dims {
    h: usize,
    layers: usize,
    vocab: usize,
    experts: usize,
    top: usize,
    inter: usize,
    qh: usize,
    kh: usize,
    d: usize,
    lkh: usize,
    lvh: usize,
    dk: usize,
    dv: usize,
    conv: usize,
    full: usize,
    rotary: usize,
    theta: f32,
    eps: f32,
}

impl Dims {
    fn load(files: &Files) -> Result<Self> {
        let config: serde_json::Value =
            serde_json::from_slice(&std::fs::read(files.dir().join("config.json"))?)?;
        ensure!(
            matches!(
                config["model_type"].as_str(),
                Some("qwen3_5_moe" | "qwen3_5_moe_text")
            ),
            "Metal supports Qwen3.5 MoE text checkpoints"
        );
        let c = config.get("text_config").unwrap_or(&config);
        let u = |name: &str| -> Result<usize> {
            let n = usize::try_from(c[name].as_u64().with_context(|| format!("config {name}"))?)?;
            ensure!(n > 0, "config {name} must be positive");
            Ok(n)
        };
        let d = u("head_dim")?;
        let rope = c.get("rope_parameters").unwrap_or(c);
        let result = Self {
            h: u("hidden_size")?,
            layers: u("num_hidden_layers")?,
            vocab: u("vocab_size")?,
            experts: u("num_experts")?,
            top: u("num_experts_per_tok")?,
            inter: u("moe_intermediate_size")?,
            qh: u("num_attention_heads")?,
            kh: u("num_key_value_heads")?,
            d,
            lkh: u("linear_num_key_heads")?,
            lvh: u("linear_num_value_heads")?,
            dk: u("linear_key_head_dim")?,
            dv: u("linear_value_head_dim")?,
            conv: u("linear_conv_kernel_dim")?,
            full: u("full_attention_interval")?,
            rotary: (d as f64
                * rope["partial_rotary_factor"]
                    .as_f64()
                    .context("partial rotary factor")?) as usize,
            theta: rope["rope_theta"].as_f64().context("rope theta")? as f32,
            eps: c["rms_norm_eps"].as_f64().context("norm epsilon")? as f32,
        };
        ensure!(
            result.qh.is_multiple_of(result.kh)
                && result.lvh.is_multiple_of(result.lkh)
                && result.top <= result.experts
                && result.rotary > 0
                && result.rotary.is_multiple_of(2)
                && result.rotary <= d,
            "invalid Qwen3.5 geometry"
        );
        ensure!(
            u("shared_expert_intermediate_size")? == result.inter,
            "Metal currently requires equal routed and shared expert widths"
        );
        if let Some(types) = c["layer_types"].as_array() {
            ensure!(
                types.len() == result.layers
                    && types.iter().enumerate().all(|(i, t)| t.as_str()
                        == Some(if (i + 1) % result.full == 0 {
                            "full_attention"
                        } else {
                            "linear_attention"
                        })),
                "layer types must match full_attention_interval"
            );
        }
        Ok(result)
    }
    fn conv_width(&self) -> usize {
        2 * self.lkh * self.dk + self.lvh * self.dv
    }
    fn sequence_bytes(&self, tokens: usize) -> usize {
        let full = self.layers / self.full;
        full * tokens * self.kh * self.d * 8
            + (self.layers - full)
                * (self.lvh * self.dv * self.dk + self.conv_width() * (self.conv - 1))
                * 4
    }
}

fn bf16(gpu: &Gpu, files: &Files, name: &str) -> Result<Dev<u16>> {
    let t = files
        .tensor(name)
        .with_context(|| format!("missing {name}"))?;
    ensure!(t.dtype == "BF16", "{name}: expected BF16, got {}", t.dtype);
    let raw = files.read_tensor(t)?;
    let values: Vec<_> = raw
        .as_chunks::<2>()
        .0
        .iter()
        .map(|c| u16::from_le_bytes(*c))
        .collect();
    gpu.upload_bf16(&values)
}

enum Matrix {
    Bf16 {
        w: Dev<u16>,
        n: usize,
        k: usize,
    },
    Nvfp4 {
        w: Dev<u8>,
        scales: Dev<u8>,
        scale2: f32,
        n: usize,
        k: usize,
    },
}
impl Matrix {
    fn load(gpu: &Gpu, files: &Files, name: &str) -> Result<Self> {
        let t = files
            .tensor(&format!("{name}.weight"))
            .with_context(|| format!("missing {name}.weight"))?;
        ensure!(t.shape.len() == 2, "{name}: matrix rank must be two");
        let n = t.shape[0] as usize;
        let k = t.shape[1] as usize;
        if t.dtype == "BF16" {
            return Ok(Self::Bf16 {
                w: bf16(gpu, files, &t.name)?,
                n,
                k,
            });
        }
        ensure!(
            t.dtype == "U8",
            "{name}: unsupported matrix dtype {}",
            t.dtype
        );
        let scales = files
            .tensor(&format!("{name}.weight_scale"))
            .context("missing NVFP4 scales")?;
        ensure!(
            scales.dtype == "F8_E4M3" && scales.shape == [n as u64, (2 * k / 16) as u64],
            "{name}: invalid NVFP4 scales"
        );
        let scale = files
            .tensor(&format!("{name}.weight_scale_2"))
            .context("missing NVFP4 global scale")?;
        ensure!(
            scale.dtype == "F32",
            "{name}: invalid NVFP4 global scale dtype"
        );
        let raw = files.read_tensor(scale)?;
        ensure!(raw.len() == 4, "{name}: global scale is not scalar");
        Ok(Self::Nvfp4 {
            w: gpu.upload_bytes(&files.read_tensor(t)?)?,
            scales: gpu.upload_bytes(&files.read_tensor(scales)?)?,
            scale2: f32::from_le_bytes(raw.try_into().unwrap()),
            n,
            k: 2 * k,
        })
    }
    fn apply(&self, gpu: &Gpu, x: &Dev<f32>) -> Result<Dev<f32>> {
        let n = match self {
            Self::Bf16 { n, .. } | Self::Nvfp4 { n, .. } => *n,
        };
        let mut y = gpu.uninit(n)?;
        match self {
            Self::Bf16 { w, n, k } => {
                let mut scratch = gpu.uninit_bf16(1)?;
                gpu.gemm_bf16(x, w, &mut y, &mut scratch, 1, *n, *k)?;
            }
            Self::Nvfp4 {
                w,
                scales,
                scale2,
                n,
                k,
            } => gpu.gemm_nvfp4(x, w, scales, *scale2, &mut y, 1, *n, *k)?,
        }
        Ok(y)
    }
}

struct Delta {
    qkv: Matrix,
    z: Matrix,
    a: Matrix,
    b: Matrix,
    o: Matrix,
    conv: Dev<u16>,
    log_a: Dev<u16>,
    bias: Dev<u16>,
    norm: Dev<u16>,
}
struct Full {
    q: Matrix,
    k: Matrix,
    v: Matrix,
    o: Matrix,
    qn: Dev<u16>,
    kn: Dev<u16>,
}
enum Attention {
    Delta(Delta),
    Full(Full),
}
struct Layer {
    input: Dev<u16>,
    post: Dev<u16>,
    attention: Attention,
    router: Matrix,
    sg: Matrix,
    su: Matrix,
    sd: Matrix,
    shared_gate: Matrix,
}

struct Embeddings {
    file: File,
    offset: u64,
}
struct Loaded {
    gpu: Arc<Gpu>,
    d: Dims,
    embeddings: Embeddings,
    head: Matrix,
    norm: Dev<u16>,
    layers: Vec<Layer>,
    files: Arc<Files>,
    experts: Mutex<Box<dyn ExpertSource<Gpu>>>,
    limit: u64,
}
pub struct Qwen35 {
    inner: Rc<Loaded>,
}

/// Shared CPU/GPU RAM budgets. Host tiers are deliberately small on unified memory.
pub struct Options {
    pub max_context: usize,
    pub reserve_bytes: u64,
    pub cache_bytes: Option<u64>,
    pub io: IoConfig,
    pub device_policy: Box<dyn Policy>,
    pub host_policy: Box<dyn Policy>,
    pub disk_only: bool,
}

/// A conservative dry-run estimate; embedding rows are paged rather than resident.
pub struct MemoryEstimate {
    pub resident_weights: usize,
    pub sequence: usize,
    pub staging: usize,
    pub record_stride: usize,
}

pub fn memory_estimate(files: &Files, tokens: usize) -> Result<MemoryEstimate> {
    ensure!(tokens > 0, "context must be positive");
    let d = Dims::load(files)?;
    let stride = files
        .expert_group(0)
        .context("no expert group")?
        .schema
        .stride as usize;
    Ok(MemoryEstimate {
        resident_weights: files
            .index()
            .tensors
            .iter()
            .filter(|t| {
                t.file == oominf_format::TensorFile::Dense
                    && t.name != format!("{ROOT}.embed_tokens.weight")
            })
            .map(|t| t.nbytes as usize)
            .sum(),
        sequence: d.sequence_bytes(tokens),
        staging: (d.top + 1) * stride,
        record_stride: stride,
    })
}

pub fn open(files: Arc<Files>, options: Options) -> Result<Box<Qwen35>> {
    let d = Dims::load(&files)?;
    let host = oominf_tiers::resources::HostMemory::probe()?;
    let limit = host.usable().saturating_sub(options.reserve_bytes);
    ensure!(
        limit >= 2 * GIB,
        "not enough available shared RAM after reserve: {}",
        host.describe()
    );
    let gpu = Arc::new(Gpu::with_memory_limit(limit)?);
    let mut layers = Vec::with_capacity(d.layers);
    for i in 0..d.layers {
        let p = format!("{ROOT}.layers.{i}");
        let m = |s: &str| Matrix::load(&gpu, &files, &format!("{p}.{s}"));
        let w = |s: &str| bf16(&gpu, &files, &format!("{p}.{s}.weight"));
        let attention = if (i + 1) % d.full == 0 {
            Attention::Full(Full {
                q: m("self_attn.q_proj")?,
                k: m("self_attn.k_proj")?,
                v: m("self_attn.v_proj")?,
                o: m("self_attn.o_proj")?,
                qn: w("self_attn.q_norm")?,
                kn: w("self_attn.k_norm")?,
            })
        } else {
            Attention::Delta(Delta {
                qkv: m("linear_attn.in_proj_qkv")?,
                z: m("linear_attn.in_proj_z")?,
                a: m("linear_attn.in_proj_a")?,
                b: m("linear_attn.in_proj_b")?,
                o: m("linear_attn.out_proj")?,
                conv: w("linear_attn.conv1d")?,
                log_a: bf16(&gpu, &files, &format!("{p}.linear_attn.A_log"))?,
                bias: bf16(&gpu, &files, &format!("{p}.linear_attn.dt_bias"))?,
                norm: w("linear_attn.norm")?,
            })
        };
        layers.push(Layer {
            input: w("input_layernorm")?,
            post: w("post_attention_layernorm")?,
            attention,
            router: m("mlp.gate")?,
            sg: m("mlp.shared_expert.gate_proj")?,
            su: m("mlp.shared_expert.up_proj")?,
            sd: m("mlp.shared_expert.down_proj")?,
            shared_gate: m("mlp.shared_expert_gate")?,
        });
    }
    let embedding = files
        .tensor(&format!("{ROOT}.embed_tokens.weight"))
        .context("missing embeddings")?;
    ensure!(
        embedding.dtype == "BF16" && embedding.shape == [d.vocab as u64, d.h as u64],
        "invalid embedding geometry"
    );
    // Only one released BF16 row is used per token. Keeping the whole vocabulary
    // resident would spend about 1 GB that can cache streamed experts instead.
    let embeddings = Embeddings {
        file: File::open(files.dir().join("dense.bin"))?,
        offset: embedding.offset,
    };
    let head = Matrix::load(&gpu, &files, "lm_head")?;
    let norm = bf16(&gpu, &files, &format!("{ROOT}.norm.weight"))?;
    let group = files.expert_group(0).context("no expert group")?;
    let stride = group.schema.stride as usize;
    // One shared budget includes host staging, sequence state and transient activations.
    let host_slots = d.top;
    let host_bytes = (host_slots + 1) * stride;
    let reserved = d.sequence_bytes(options.max_context) + host_bytes + (128 << 20);
    let (free, _) = gpu.mem_info()?;
    let available = free.saturating_sub(reserved);
    let cache = options
        .cache_bytes
        .map_or(available, |b| available.min(b as usize));
    let experts: Box<dyn ExpertSource<Gpu>> = if options.disk_only {
        Box::new(oominf_tiers::DiskExperts::new(files.clone()))
    } else {
        let slots = (cache / stride / oominf_tiers::CHUNK_SLOTS) * oominf_tiers::CHUNK_SLOTS;
        ensure!(
            slots >= oominf_tiers::CHUNK_SLOTS,
            "shared RAM cannot fit minimum expert cache and context"
        );
        Box::new(TieredExperts::new(
            gpu.clone(),
            files.clone(),
            &group.schema.layout,
            TierSizes {
                vram_slots: slots,
                host_slots,
                host_stage_slots: 1,
                max_fetch: d.top,
            },
            options.device_policy,
            options.host_policy,
            &options.io,
        )?)
    };
    Ok(Box::new(Qwen35 {
        inner: Rc::new(Loaded {
            gpu,
            d,
            embeddings,
            head,
            norm,
            layers,
            files,
            experts: Mutex::new(experts),
            limit,
        }),
    }))
}

enum State {
    Delta { conv: Dev<f32>, recurrent: Dev<f32> },
    Full { k: Dev<f32>, v: Dev<f32> },
}
struct Sequence {
    inner: Rc<Loaded>,
    state: Vec<State>,
    pos: usize,
    max: usize,
}

impl Model for Qwen35 {
    fn model_type(&self) -> &str {
        "qwen3_5_moe"
    }
    fn vocab(&self) -> usize {
        self.inner.d.vocab
    }
    fn describe(&self) -> String {
        format!(
            "Qwen3.5 MoE on {}; shared RAM limit {:.2} GiB; {}",
            self.inner.gpu.name(),
            self.inner.limit as f64 / GIB as f64,
            self.inner.experts.lock().unwrap().describe()
        )
    }
    fn expert_stats(&self) -> ExpertStats {
        self.inner.experts.lock().unwrap().stats()
    }
    fn expert_tiers(&self) -> ExpertTiers {
        self.inner.experts.lock().unwrap().tiers()
    }
    fn sequence_bytes(&self, tokens: usize) -> usize {
        self.inner.d.sequence_bytes(tokens)
    }
    fn available_bytes(&self) -> usize {
        (self.inner.gpu.mem_info().map_or(0, |v| v.0)
            + self.inner.experts.lock().unwrap().releasable_vram())
        .saturating_sub(64 << 20)
    }
    fn new_session(&self, max: usize) -> Result<Box<dyn Session>> {
        Ok(Box::new(self.new_sequence(max)?))
    }
}

impl Qwen35 {
    fn new_sequence(&self, max: usize) -> Result<Sequence> {
        ensure!(max > 0, "context must be positive");
        let g = &self.inner.gpu;
        let d = &self.inner.d;
        let needed = d.sequence_bytes(max) + (64 << 20);
        let free = g.mem_info()?.0;
        if needed > free {
            self.inner
                .experts
                .lock()
                .unwrap()
                .release_vram(g, needed - free)?;
        }
        let mut state = Vec::with_capacity(d.layers);
        for layer in &self.inner.layers {
            state.push(match layer.attention {
                Attention::Delta(_) => State::Delta {
                    conv: g.zeros(d.conv_width() * (d.conv - 1))?,
                    recurrent: g.zeros(d.lvh * d.dv * d.dk)?,
                },
                Attention::Full(_) => State::Full {
                    k: g.zeros(max * d.kh * d.d)?,
                    v: g.zeros(max * d.kh * d.d)?,
                },
            });
        }
        Ok(Sequence {
            inner: self.inner.clone(),
            state,
            pos: 0,
            max,
        })
    }
    /// Capture each decoder layer's residual output for independent validation.
    /// Synchronization and downloads occur only on this diagnostic path.
    pub fn trace_tokens(
        &self,
        tokens: &[u32],
        mut observer: impl FnMut(usize, usize, &[f32]) -> Result<()>,
    ) -> Result<Vec<Vec<f32>>> {
        let mut sequence = self.new_sequence(tokens.len())?;
        let mut logits = Vec::with_capacity(tokens.len());
        for (step, &token) in tokens.iter().enumerate() {
            logits.push(sequence.token(
                token,
                true,
                Some(&mut |layer, values| observer(step, layer, values)),
            )?);
        }
        Ok(logits)
    }
}

type LayerObserver<'a> = dyn FnMut(usize, &[f32]) -> Result<()> + 'a;

fn norm(
    g: &Gpu,
    x: &Dev<f32>,
    w: &Dev<u16>,
    len: usize,
    group: usize,
    eps: f32,
    plus: f32,
) -> Result<Dev<f32>> {
    let mut out = g.uninit(len)?;
    g.rmsnorm_groups(x, w, &mut out, 1, len, group, eps, plus)?;
    Ok(out)
}

fn route(logits: &[f32], top: usize) -> Result<Vec<(u32, f32)>> {
    ensure!(
        top > 0 && top <= logits.len() && logits.iter().all(|v| v.is_finite()),
        "invalid router scores"
    );
    let mut order: Vec<_> = (0..logits.len()).collect();
    order.sort_by(|&a, &b| logits[b].total_cmp(&logits[a]).then(a.cmp(&b)));
    order.truncate(top);
    let maximum = logits[order[0]];
    let weights: Vec<_> = order.iter().map(|&i| (logits[i] - maximum).exp()).collect();
    let sum: f32 = weights.iter().sum();
    Ok(order
        .iter()
        .zip(weights)
        .map(|(&i, w)| (i as u32, w / sum))
        .collect())
}

impl Sequence {
    fn token(
        &mut self,
        token: u32,
        need_logits: bool,
        mut observer: Option<&mut LayerObserver<'_>>,
    ) -> Result<Vec<f32>> {
        ensure!(self.pos < self.max, "context limit reached");
        let m = &self.inner;
        let g = &m.gpu;
        let d = &m.d;
        ensure!((token as usize) < d.vocab, "token outside vocabulary");
        let mut raw = vec![0u8; d.h * 2];
        m.embeddings.file.read_exact_at(
            &mut raw,
            m.embeddings.offset + u64::from(token) * d.h as u64 * 2,
        )?;
        let values: Vec<_> = raw
            .as_chunks::<2>()
            .0
            .iter()
            .map(|c| f32::from_bits((u16::from_le_bytes(*c) as u32) << 16))
            .collect();
        let mut x = g.upload_f32(&values)?;
        for (i, (layer, state)) in m.layers.iter().zip(&mut self.state).enumerate() {
            let normalized = norm(g, &x, &layer.input, d.h, d.h, d.eps, 1.)?;
            let attention = match (&layer.attention, state) {
                (Attention::Delta(a), State::Delta { conv, recurrent }) => {
                    let qkv = a.qkv.apply(g, &normalized)?;
                    let z = a.z.apply(g, &normalized)?;
                    let decay = a.a.apply(g, &normalized)?;
                    let beta = a.b.apply(g, &normalized)?;
                    let mut convolved = g.uninit(d.conv_width())?;
                    g.causal_conv(&qkv, &a.conv, conv, &mut convolved, d.conv_width(), d.conv)?;
                    g.l2norm_heads(&mut convolved, 1, d.conv_width(), 0, d.lkh, d.dk, 1e-6)?;
                    g.l2norm_heads(
                        &mut convolved,
                        1,
                        d.conv_width(),
                        d.lkh * d.dk,
                        d.lkh,
                        d.dk,
                        1e-6,
                    )?;
                    let mut value = g.uninit(d.lvh * d.dv)?;
                    g.delta_step(
                        &convolved, &decay, &beta, &a.log_a, &a.bias, recurrent, &mut value, d.lkh,
                        d.lvh, d.dk, d.dv,
                    )?;
                    let normalized = norm(g, &value, &a.norm, d.lvh * d.dv, d.dv, d.eps, 0.)?;
                    let mut gated = g.uninit(d.lvh * d.dv)?;
                    g.silu_mul(&z, &normalized, &mut gated, d.lvh * d.dv)?;
                    a.o.apply(g, &gated)?
                }
                (Attention::Full(a), State::Full { k, v }) => {
                    let projected = a.q.apply(g, &normalized)?;
                    let mut query = g.uninit(d.qh * d.d)?;
                    let mut gate = g.uninit(d.qh * d.d)?;
                    g.copy_cols(&projected, &mut query, d.qh, 2 * d.d, 0, d.d)?;
                    g.copy_cols(&projected, &mut gate, d.qh, 2 * d.d, d.d, d.d)?;
                    let mut query = norm(g, &query, &a.qn, d.qh * d.d, d.d, d.eps, 1.)?;
                    let key = a.k.apply(g, &normalized)?;
                    let mut key = norm(g, &key, &a.kn, d.kh * d.d, d.d, d.eps, 1.)?;
                    let value = a.v.apply(g, &normalized)?;
                    g.rope_half(&mut query, d.qh, d.d, d.rotary, self.pos, d.theta)?;
                    g.rope_half(&mut key, d.kh, d.d, d.rotary, self.pos, d.theta)?;
                    g.kv_append(&key, k, self.pos, d.kh * d.d)?;
                    g.kv_append(&value, v, self.pos, d.kh * d.d)?;
                    let mut out = g.uninit(d.qh * d.d)?;
                    let mut scores = g.uninit(d.qh * (self.pos + 1))?;
                    g.gqa_step(
                        &query,
                        k,
                        v,
                        &mut out,
                        &mut scores,
                        d.qh,
                        d.kh,
                        d.d,
                        self.pos + 1,
                    )?;
                    g.mul_sigmoid(&mut out, &gate, d.qh * d.d)?;
                    a.o.apply(g, &out)?
                }
                _ => bail!("layer state does not match attention kind"),
            };
            let mut residual = g.uninit(d.h)?;
            g.add(&x, &attention, &mut residual, d.h)?;
            let input = norm(g, &residual, &layer.post, d.h, d.h, d.eps, 1.)?;
            let router = layer.router.apply(g, &input)?;
            let assignments = route(&g.download_f32(&router)?, d.top)?;
            let ids: Vec<_> = assignments.iter().map(|v| v.0).collect();
            let mut source = m.experts.lock().unwrap();
            let addresses = source.fetch(g, i as u32, &ids)?;
            let sg = layer.sg.apply(g, &input)?;
            let su = layer.su.apply(g, &input)?;
            let mut product = g.uninit(d.inter)?;
            g.silu_mul(&sg, &su, &mut product, d.inter)?;
            let mut mixture = layer.sd.apply(g, &product)?;
            let shared_gate = layer.shared_gate.apply(g, &input)?;
            g.shared_gate(&mut mixture, &shared_gate, d.h)?;
            let schema = &m
                .files
                .expert_group(i as u32)
                .context("missing expert group")?
                .schema;
            let part = |name: &str| -> Result<usize> {
                Ok(schema
                    .part(name)
                    .with_context(|| format!("missing {name}"))?
                    .offset as usize)
            };
            let weights: Vec<_> = assignments.iter().map(|v| v.1).collect();
            g.routed_swiglu(
                &input,
                &addresses,
                &weights,
                [
                    part("gate.weight")?,
                    part("gate.weight_scale")?,
                    part("up.weight")?,
                    part("up.weight_scale")?,
                    part("down.weight")?,
                    part("down.weight_scale")?,
                ],
                &mut mixture,
                d.h,
                d.inter,
            )?;
            let mut next = g.uninit(d.h)?;
            g.add(&residual, &mixture, &mut next, d.h)?;
            x = next;
            if let Some(observer) = observer.as_deref_mut() {
                observer(i, &g.download_f32(&x)?)?;
            }
            // The next layer's router read completes all kernels using fetched records.
            drop(source);
        }
        self.pos += 1;
        if !need_logits {
            g.sync()?;
            return Ok(Vec::new());
        }
        let normalized = norm(g, &x, &m.norm, d.h, d.h, d.eps, 1.)?;
        let logits = m.head.apply(g, &normalized)?;
        let result = g.download_f32(&logits)?;
        ensure!(
            result.iter().all(|v| v.is_finite()),
            "non-finite model logits"
        );
        Ok(result)
    }
}
impl Session for Sequence {
    fn len(&self) -> usize {
        self.pos
    }
    fn prefill(
        &mut self,
        tokens: &[u32],
        cancelled: &dyn Fn() -> bool,
    ) -> Result<Option<Vec<f32>>> {
        ensure!(!tokens.is_empty(), "empty prompt");
        let mut logits = Vec::new();
        for (i, &t) in tokens.iter().enumerate() {
            if cancelled() {
                return Ok(None);
            }
            logits = self.token(t, i + 1 == tokens.len(), None)?;
        }
        Ok(Some(logits))
    }
    fn step(&mut self, tokens: &[u32]) -> Result<Vec<f32>> {
        ensure!(!tokens.is_empty(), "empty step");
        let mut logits = Vec::new();
        for (i, &t) in tokens.iter().enumerate() {
            logits = self.token(t, i + 1 == tokens.len(), None)?;
        }
        Ok(logits)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn routing_renormalizes_top_weights_and_breaks_ties_by_id() {
        let route = route(&[0., 1., 1., -10.], 2).unwrap();
        assert_eq!(route, [(1, 0.5), (2, 0.5)]);
        assert!(super::route(&[f32::NAN], 1).is_err());
    }
}
