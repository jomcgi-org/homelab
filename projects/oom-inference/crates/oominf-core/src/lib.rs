//! Platform- and model-neutral interfaces of oom-inference.
//!
//! - [`Backend`]: the device operations models need; a platform implements it.
//! - [`Model`] and [`Session`]: what the server and tools use to run a model.
//! - [`ExpertSource`]: where routed-expert records come from.
//! - [`Workspace`] and [`Probe`]: shared plumbing for model code.

mod backend;
mod experts;
mod model;
mod probe;
mod workspace;

pub use backend::{
    Attention, Backend, DeviceBuffer, Elementwise, Experts, HyperConnection, Linear, Lookup,
    Memory, Norm, Nvfp4Record, Recurrent, Transfer, View,
};
pub use experts::{ExpertFactory, ExpertSource, ExpertStats, Staged};
pub use model::{Model, Session};
pub use probe::{NoProbe, Probe, tap, tap_cols};
pub use workspace::Workspace;
