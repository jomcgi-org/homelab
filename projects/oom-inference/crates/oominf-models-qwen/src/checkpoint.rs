//! Prefix checkpoints: the recurrent state a sequence had at chosen positions of a
//! prefill, so it can later rewind there and serve a prompt that shares only that
//! prefix (the same documents with a different question).
//!
//! Attention layers need nothing: their caches are truncated. GDN layers' conv and
//! recurrent state and PLE layers' conv state and n-gram tokens cannot be rewound,
//! so layer-major prefill copies them on the device as each layer passes a
//! checkpoint, and the copies come to host memory once the prefill ends.

use anyhow::{Result, ensure};
use oominf_core::{Backend, DeviceBuffer};

use crate::layer::LayerState;

/// One layer's recurrent state at a checkpoint (host memory).
#[derive(Clone)]
pub struct LayerSnap {
    /// GDN conv and recurrent state.
    pub gdn: Option<(Vec<f32>, Vec<f32>)>,
    /// PLE conv state and last n-gram tokens.
    pub ple: Option<(Vec<f32>, Vec<u32>)>,
}

/// Every layer's recurrent state after the first `pos` tokens.
#[derive(Clone)]
pub struct Checkpoint {
    pub pos: usize,
    pub layers: Vec<LayerSnap>,
}

/// A layer's state copied on the device during prefill.
pub(crate) struct DevSnap<B: Backend> {
    gdn: Option<(B::F32, B::F32)>,
    ple: Option<(B::F32, Vec<u32>)>,
    captured: bool,
}

/// Device bytes one layer's [`DevSnap`] takes.
pub(crate) fn snap_bytes<B: Backend>(st: &LayerState<B>) -> usize {
    let g = st
        .gdn
        .as_ref()
        .map_or(0, |g| g.conv.len() + g.recurrent.len());
    let p = st.ple.as_ref().map_or(0, |p| p.conv.len());
    (g + p) * std::mem::size_of::<f32>()
}

/// Device buffers for one capture of `st`. Prefill allocates every capture's
/// buffers before it starts: small copies allocated between its large transient
/// buffers would each pin a block of the stream-ordered pool until the prefill
/// ends, fragmenting gigabytes over a long prompt.
pub(crate) fn alloc<B: Backend>(gpu: &B, st: &LayerState<B>) -> Result<DevSnap<B>> {
    Ok(DevSnap {
        gdn: match &st.gdn {
            Some(g) => Some((gpu.uninit(g.conv.len())?, gpu.uninit(g.recurrent.len())?)),
            None => None,
        },
        ple: match &st.ple {
            Some(p) => Some((gpu.uninit(p.conv.len())?, Vec::new())),
            None => None,
        },
        captured: false,
    })
}

/// Copies `st`'s recurrent state into `snap` (queued after the layer's work).
pub(crate) fn capture<B: Backend>(
    gpu: &B,
    st: &LayerState<B>,
    snap: &mut DevSnap<B>,
) -> Result<()> {
    if let (Some(g), Some((c, r))) = (&st.gdn, snap.gdn.as_mut()) {
        gpu.copy_range(&g.conv, 0, c, 0, g.conv.len())?;
        gpu.copy_range(&g.recurrent, 0, r, 0, g.recurrent.len())?;
    }
    if let (Some(p), Some((c, t))) = (&st.ple, snap.ple.as_mut()) {
        gpu.copy_range(&p.conv, 0, c, 0, p.conv.len())?;
        t.clone_from(&p.tokens);
    }
    snap.captured = true;
    Ok(())
}

/// Brings a checkpoint's device copies (one per layer) to host memory.
pub(crate) fn download<B: Backend>(
    gpu: &B,
    pos: usize,
    layers: Vec<DevSnap<B>>,
) -> Result<Checkpoint> {
    let layers = layers
        .into_iter()
        .map(|s| {
            ensure!(s.captured, "checkpoint {pos} missed a layer");
            Ok(LayerSnap {
                gdn: match s.gdn {
                    Some((c, r)) => Some((gpu.download_f32(&c)?, gpu.download_f32(&r)?)),
                    None => None,
                },
                ple: match s.ple {
                    Some((c, t)) => Some((gpu.download_f32(&c)?, t)),
                    None => None,
                },
            })
        })
        .collect::<Result<_>>()?;
    Ok(Checkpoint { pos, layers })
}

/// Puts a layer's recurrent state back from `snap`.
pub(crate) fn restore<B: Backend>(gpu: &B, st: &mut LayerState<B>, snap: &LayerSnap) -> Result<()> {
    match (st.gdn.as_mut(), &snap.gdn) {
        (Some(g), Some((c, r))) => {
            ensure!(
                c.len() == g.conv.len() && r.len() == g.recurrent.len(),
                "checkpoint GDN state has the wrong size"
            );
            gpu.upload_into(c, &mut g.conv)?;
            gpu.upload_into(r, &mut g.recurrent)?;
        }
        (None, None) => {}
        _ => anyhow::bail!("checkpoint layer kinds differ"),
    }
    match (st.ple.as_mut(), &snap.ple) {
        (Some(p), Some((c, t))) => {
            ensure!(
                c.len() == p.conv.len(),
                "checkpoint PLE state has the wrong size"
            );
            gpu.upload_into(c, &mut p.conv)?;
            p.tokens = t.clone();
        }
        (None, None) => {}
        _ => anyhow::bail!("checkpoint layer kinds differ"),
    }
    Ok(())
}
