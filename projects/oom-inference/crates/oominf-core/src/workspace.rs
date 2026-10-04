use std::collections::HashMap;

use anyhow::Result;

use crate::backend::{DeviceBuffer, Memory};

/// Reusable named device buffers, so a steady-state step allocates nothing and every
/// intermediate keeps a stable address.
///
/// Components `take` a buffer of an exact length and `give` it back when done. A
/// buffer of a different length is reallocated; one that is not given back is
/// reallocated next time. Contents are unspecified unless taken with `take_zeroed`.
pub struct Workspace<B: Memory> {
    f32s: HashMap<&'static str, B::F32>,
    bf16s: HashMap<&'static str, B::Bf16>,
}

impl<B: Memory> Default for Workspace<B> {
    fn default() -> Self {
        Workspace {
            f32s: HashMap::new(),
            bf16s: HashMap::new(),
        }
    }
}

impl<B: Memory> Workspace<B> {
    pub fn new() -> Self {
        Self::default()
    }

    /// Device bytes held by buffers currently given back to the workspace.
    pub fn bytes(&self) -> usize {
        self.f32s.values().map(|b| b.len() * 4).sum::<usize>()
            + self.bf16s.values().map(|b| b.len() * 2).sum::<usize>()
    }

    pub fn take(&mut self, b: &B, name: &'static str, n: usize) -> Result<B::F32> {
        match self.f32s.remove(name) {
            Some(buf) if buf.len() == n.max(1) => Ok(buf),
            _ => b.uninit(n),
        }
    }

    pub fn take_zeroed(&mut self, b: &B, name: &'static str, n: usize) -> Result<B::F32> {
        let mut buf = self.take(b, name, n)?;
        b.fill_zero(&mut buf)?;
        Ok(buf)
    }

    pub fn give(&mut self, name: &'static str, buf: B::F32) {
        self.f32s.insert(name, buf);
    }

    /// A bf16 scratch of at least `n` elements (grows, never shrinks).
    pub fn take_bf16(&mut self, b: &B, name: &'static str, n: usize) -> Result<B::Bf16> {
        match self.bf16s.remove(name) {
            Some(buf) if buf.len() >= n.max(1) => Ok(buf),
            _ => b.uninit_bf16(n.max(1)),
        }
    }

    pub fn give_bf16(&mut self, name: &'static str, buf: B::Bf16) {
        self.bf16s.insert(name, buf);
    }
}
