//! Saving and restoring a sequence's state, so a later request that extends the
//! same tokens resumes without prefilling them (the server's prefix store).
//!
//! A snapshot is taken between steps and holds what the next step reads: per GDN
//! layer its conv and recurrent state; per attention layer the cached keys and
//! values (in the cache's [`oominf_core::KvFormat`]), raw indexer keys and pooled
//! block keys; per PLE layer its conv state and last n-gram tokens; the draft
//! head's input row. Rewind checkpoints are not kept (a restored sequence cannot
//! rewind its last step), nor is the draft head's attention, which every draft
//! restarts.
//!
//! Layout: `u32` header length, a JSON header (sizes and scalars), then the
//! buffers in header order, little-endian.

use std::io::{Read, Write};

use anyhow::{Context, Result, ensure};
use oominf_core::{Backend, DeviceBuffer, ExpertSource};
use serde_json::{Value, json};

use crate::{QwenModel, SeqState};

const VERSION: u64 = 1;

/// Writes `state` to `w`.
pub fn save<B: Backend>(
    model: &QwenModel<B>,
    gpu: &B,
    state: &SeqState<B>,
    w: &mut dyn Write,
) -> Result<()> {
    let r = model.dims.residual();
    let mut blobs: Vec<Vec<u8>> = Vec::new();
    let f32s = |v: Vec<f32>| -> Vec<u8> { v.iter().flat_map(|x| x.to_le_bytes()).collect() };
    // The first `n` elements of `buf`.
    let head_f32 = |buf: &B::F32, n: usize| -> Result<Vec<f32>> {
        if n == 0 {
            return Ok(Vec::new());
        }
        let mut tmp = gpu.uninit(n)?;
        gpu.copy_range(buf, 0, &mut tmp, 0, n)?;
        gpu.download_f32(&tmp)
    };
    let head_bytes = |buf: &B::Bytes, n: usize| -> Result<Vec<u8>> {
        if n == 0 {
            return Ok(Vec::new());
        }
        let mut tmp = gpu.uninit_bytes(n)?;
        gpu.copy_bytes(buf, 0, &mut tmp, 0, n)?;
        gpu.download_bytes(&tmp)
    };
    let mut layers = Vec::new();
    for (layer, st) in model.layers().iter().zip(&state.layers) {
        let mut entry = serde_json::Map::new();
        if let Some(g) = &st.gdn {
            blobs.push(f32s(gpu.download_f32(&g.conv)?));
            blobs.push(f32s(gpu.download_f32(&g.recurrent)?));
            entry.insert("gdn".into(), json!({}));
        }
        if let Some(a) = &st.attn {
            let (k, v, ik, bk) = layer.cache_extent(a);
            blobs.push(head_bytes(&a.k, k)?);
            blobs.push(head_bytes(&a.v, v)?);
            blobs.push(f32s(head_f32(&a.idx_keys, ik)?));
            blobs.push(f32s(head_f32(&a.block_keys, bk)?));
            entry.insert("attn".into(), json!({"len": a.len, "blocks": a.blocks}));
        }
        if let Some(p) = &st.ple {
            blobs.push(f32s(gpu.download_f32(&p.conv)?));
            entry.insert("ple".into(), json!({"tokens": p.tokens}));
        }
        layers.push(Value::Object(entry));
    }
    let hidden = match &state.hidden {
        Some(h) => {
            let mut row = gpu.uninit(r)?;
            gpu.copy_range(h, state.hidden_row * r, &mut row, 0, r)?;
            blobs.push(f32s(gpu.download_f32(&row)?));
            true
        }
        None => false,
    };
    let header = json!({
        "version": VERSION,
        "pos": state.pos,
        "hidden": hidden,
        "layers": layers,
        "blobs": blobs.iter().map(Vec::len).collect::<Vec<_>>(),
    });
    let h = serde_json::to_vec(&header)?;
    w.write_all(&(h.len() as u32).to_le_bytes())?;
    w.write_all(&h)?;
    for b in &blobs {
        w.write_all(b)?;
    }
    Ok(())
}

/// Restores a snapshot from `r` into `state`, a fresh sequence (no tokens yet) of
/// the same model, growing its caches (and asking `experts` for memory) as needed.
pub fn load<B: Backend>(
    model: &QwenModel<B>,
    gpu: &B,
    state: &mut SeqState<B>,
    r: &mut dyn Read,
    experts: &mut dyn ExpertSource<B>,
) -> Result<()> {
    ensure!(state.pos == 0, "snapshots restore into a fresh sequence");
    let mut len = [0u8; 4];
    r.read_exact(&mut len)?;
    let mut h = vec![0u8; u32::from_le_bytes(len) as usize];
    r.read_exact(&mut h)?;
    let header: Value = serde_json::from_slice(&h)?;
    ensure!(
        header["version"] == VERSION,
        "snapshot version {}",
        header["version"]
    );
    let pos = header["pos"].as_u64().context("snapshot pos")? as usize;
    let sizes: Vec<usize> = header["blobs"]
        .as_array()
        .context("snapshot blobs")?
        .iter()
        .map(|v| v.as_u64().map(|n| n as usize).context("blob size"))
        .collect::<Result<_>>()?;
    let mut sizes = sizes.into_iter();
    let mut next = |want: Option<usize>| -> Result<Vec<u8>> {
        let n = sizes
            .next()
            .context("snapshot has fewer buffers than its layers")?;
        if let Some(w) = want {
            ensure!(n == w, "snapshot buffer of {n} bytes, expected {w}");
        }
        let mut b = vec![0u8; n];
        r.read_exact(&mut b)?;
        Ok(b)
    };
    let to_f32 = |b: Vec<u8>| -> Vec<f32> {
        b.as_chunks::<4>()
            .0
            .iter()
            .map(|c| f32::from_le_bytes(*c))
            .collect()
    };
    let layers = header["layers"].as_array().context("snapshot layers")?;
    ensure!(
        layers.len() == state.layers.len(),
        "snapshot has {} layers, the model {}",
        layers.len(),
        state.layers.len()
    );
    // Every attention cache must hold `pos` tokens before its rows are written.
    model.reserve_kv(gpu, state, pos, 0, experts)?;
    for ((layer, st), entry) in model
        .layers()
        .iter()
        .zip(state.layers.iter_mut())
        .zip(layers)
    {
        if let Some(g) = st.gdn.as_mut() {
            ensure!(entry.get("gdn").is_some(), "snapshot layer kinds differ");
            gpu.upload_into(&to_f32(next(Some(g.conv.len() * 4))?), &mut g.conv)?;
            gpu.upload_into(
                &to_f32(next(Some(g.recurrent.len() * 4))?),
                &mut g.recurrent,
            )?;
        }
        if let Some(a) = st.attn.as_mut() {
            let meta = entry.get("attn").context("snapshot layer kinds differ")?;
            a.len = meta["len"].as_u64().context("attn len")? as usize;
            a.blocks = meta["blocks"].as_u64().context("attn blocks")? as usize;
            ensure!(a.len == pos, "attention length {} at position {pos}", a.len);
            let (k, v, ik, bk) = layer.cache_extent(a);
            for (n, cache) in [(k, &mut a.k), (v, &mut a.v)] {
                let b = next(Some(n))?;
                if n > 0 {
                    gpu.copy_bytes(&gpu.upload_bytes(&b)?, 0, cache, 0, n)?;
                }
            }
            for (n, cache) in [(ik, &mut a.idx_keys), (bk, &mut a.block_keys)] {
                let b = to_f32(next(Some(n * 4))?);
                if n > 0 {
                    gpu.copy_range(&gpu.upload_f32(&b)?, 0, cache, 0, n)?;
                }
            }
        }
        if let Some(p) = st.ple.as_mut() {
            let meta = entry.get("ple").context("snapshot layer kinds differ")?;
            gpu.upload_into(&to_f32(next(Some(p.conv.len() * 4))?), &mut p.conv)?;
            p.tokens = serde_json::from_value(meta["tokens"].clone())?;
        }
    }
    if header["hidden"] == true {
        let row = to_f32(next(Some(model.dims.residual() * 4))?);
        state.hidden = Some(gpu.upload_f32(&row)?);
        state.hidden_row = 0;
    }
    // The draft head's cache is not saved: it restarts at the restored position.
    state.hidden_tokens.clear();
    state.mtp_end = 0;
    state.pos = pos;
    state.rewindable = None;
    Ok(())
}
