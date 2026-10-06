use anyhow::Result;

use crate::experts::{ExpertStats, ExpertTiers};

/// A loaded model, ready to run sequences. Everything outside a model crate (the
/// server, the CLI, benchmarks) works through this trait and [`Session`].
pub trait Model {
    /// The checkpoint's model family (`model_type` in `config.json`).
    fn model_type(&self) -> &str;
    fn vocab(&self) -> usize;
    /// A fresh sequence that may grow to `max_tokens` tokens.
    fn new_session(&self, max_tokens: usize) -> Result<Box<dyn Session>>;
    /// One line describing what was loaded and how experts are stored.
    fn describe(&self) -> String;
    /// Where routed-expert records came from so far.
    fn expert_stats(&self) -> ExpertStats;
    /// How many routed-expert records each tier holds.
    fn expert_tiers(&self) -> ExpertTiers {
        ExpertTiers::default()
    }
    /// Feeds every feed's tokens to its session in one step and returns, per feed,
    /// the logits after each of its tokens (as [`Session::step_all`]: each session
    /// may afterwards [`Session::rewind`] a suffix of its own tokens). A model that
    /// batches runs the sequences' tokens through one pass, so work shared by all
    /// rows (weights, routed-expert fetches) is paid once; the default runs the
    /// sessions one after another. Every session must come from this model.
    fn step_many(&self, feeds: &mut [Feed<'_>]) -> Result<Vec<Vec<f32>>> {
        feeds
            .iter_mut()
            .map(|f| match f.tokens.len() {
                1 => f.session.step(f.tokens),
                _ => f.session.step_all(f.tokens),
            })
            .collect()
    }
    /// Device memory, in bytes, a sequence that grows to `tokens` tokens holds
    /// (caches and per-sequence state), for admission control; 0 when unknown.
    fn sequence_bytes(&self, tokens: usize) -> usize {
        let _ = tokens;
        0
    }
    /// Device memory sequences may still take: free memory plus what the expert
    /// tiers can give back, less the headroom steps need; `usize::MAX` when unknown.
    fn available_bytes(&self) -> usize {
        usize::MAX
    }
}

/// One sequence's share of a [`Model::step_many`] step.
pub struct Feed<'a> {
    pub session: &'a mut dyn Session,
    pub tokens: &'a [u32],
}

/// One sequence: its caches and recurrent state.
pub trait Session {
    /// Tokens processed so far.
    fn len(&self) -> usize;
    fn is_empty(&self) -> bool {
        self.len() == 0
    }
    /// Feeds a prompt (any length) and returns the logits after its last token, or
    /// `None` if `cancelled()` turned true; a cancelled session must be discarded.
    fn prefill(&mut self, tokens: &[u32], cancelled: &dyn Fn() -> bool)
    -> Result<Option<Vec<f32>>>;
    /// Feeds one step of tokens and returns the logits after the last one.
    fn step(&mut self, tokens: &[u32]) -> Result<Vec<f32>>;
    /// Feeds `tokens` and returns the logits after each of them (`tokens.len()` rows
    /// of the vocabulary); afterwards [`Session::rewind`] may drop a suffix of them.
    fn step_all(&mut self, tokens: &[u32]) -> Result<Vec<f32>> {
        let _ = tokens;
        anyhow::bail!("this model cannot verify drafted tokens")
    }
    /// Drops the last `n` tokens fed by the last [`Session::step_all`] (fewer than it
    /// fed), as if they had never been fed.
    fn rewind(&mut self, n: usize) -> Result<()> {
        anyhow::ensure!(n == 0, "this model cannot rewind");
        Ok(())
    }
    /// Positions the next prefill should keep prefix checkpoints at, so the sequence
    /// can later return there ([`Session::rewind_to`]); a no-op where unsupported.
    fn plan_checkpoints(&mut self, positions: &[usize]) {
        let _ = positions;
    }
    /// Positions this sequence can return to with [`Session::rewind_to`], ascending.
    fn checkpoints(&self) -> Vec<usize> {
        Vec::new()
    }
    /// Returns to the state after the first `pos` tokens, one of
    /// [`Session::checkpoints`] (or the current length).
    fn rewind_to(&mut self, pos: usize) -> Result<()> {
        anyhow::ensure!(pos == self.len(), "no checkpoint at {pos}");
        Ok(())
    }
    /// Writes this sequence's state (between steps) so [`Session::load`] can
    /// resume it in another session of the same model.
    fn save(&self, w: &mut dyn std::io::Write) -> Result<()> {
        let _ = w;
        anyhow::bail!("this model cannot save sequences")
    }
    /// Restores a state written by [`Session::save`] into this fresh session.
    fn load(&mut self, r: &mut dyn std::io::Read) -> Result<()> {
        let _ = r;
        anyhow::bail!("this model cannot load sequences")
    }
    /// Up to `k` tokens the model predicts will follow `next`, the token it is about
    /// to be fed (speculative decoding); empty when the model has no draft head.
    fn draft(&mut self, next: u32, k: usize) -> Result<Vec<u32>> {
        let _ = (next, k);
        Ok(Vec::new())
    }
    /// The concrete session, so its model can batch it with others
    /// ([`Model::step_many`]); `None` for sessions that are only run alone.
    fn as_any_mut(&mut self) -> Option<&mut dyn std::any::Any> {
        None
    }
}
