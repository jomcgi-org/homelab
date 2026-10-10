//! Platform- and model-neutral interfaces of oom-inference.
//!
//! - [`Backend`]: the device operations models need; a platform implements it.
//! - [`Model`] and [`Session`]: what the server and tools use to run a model.
//! - [`ExpertSource`]: where routed-expert records come from.
//! - [`Workspace`] and [`Probe`]: shared plumbing for model code.

mod backend;
pub mod decode;
mod experts;
pub mod fp8;
mod model;
mod probe;
mod workspace;

pub use backend::{
    Attention, AttentionPrecision, Backend, Bf16Record, DenseFormat, DeviceBuffer, Elementwise,
    ExpertPrecision, Experts, HyperConnection, KvFormat, Linear, Lookup, Memory, Norm, Nvfp4Record,
    Recurrent, Transfer, View, Weight,
};
pub use decode::{
    Decoded, LOOKUP_MATCH, PromptLookup, Proposal, argmax, decode_many, decode_step,
    decode_step_with,
};
pub use experts::{
    ExpertFactory, ExpertSource, ExpertStats, ExpertTiers, Fetched, HostDemand, Staged,
};
pub use model::{Feed, Model, Session};
pub use probe::{NoProbe, Probe, tap, tap_cols};
pub use workspace::Workspace;
