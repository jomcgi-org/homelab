//! The simplest expert source: every fetch reads its records from the model files.

use std::sync::Arc;

use anyhow::Result;
use oominf_core::{ExpertSource, Memory};
use oominf_format::Model;

/// Reads every requested record from disk and uploads it (no caching). A baseline
/// and a reference for cached sources: bytes are bytes, so results must match.
pub struct DiskExperts<B: Memory> {
    model: Arc<Model>,
    host: Vec<u8>,
    held: Vec<B::Bytes>,
}

impl<B: Memory> DiskExperts<B> {
    pub fn new(model: Arc<Model>) -> Self {
        DiskExperts {
            model,
            host: Vec::new(),
            held: Vec::new(),
        }
    }
}

impl<B: Memory> ExpertSource<B> for DiskExperts<B> {
    fn fetch(&mut self, b: &B, layer: u32, experts: &[u32]) -> Result<Vec<u64>> {
        self.held.clear();
        for &expert in experts {
            let (_, stride) = self.model.record_location(layer, expert)?;
            self.host.resize(stride as usize, 0);
            self.model.read_record(layer, expert, &mut self.host)?;
            self.held.push(b.upload_bytes(&self.host)?);
        }
        Ok(self.held.iter().map(|h| b.bytes_addr(h)).collect())
    }

    fn describe(&self) -> String {
        "experts read from disk on every fetch (no cache)".to_owned()
    }
}
