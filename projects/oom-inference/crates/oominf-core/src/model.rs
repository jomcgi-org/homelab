use anyhow::Result;

use crate::experts::ExpertStats;

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
}
