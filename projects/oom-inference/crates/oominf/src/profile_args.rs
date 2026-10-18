use std::path::PathBuf;

/// How `serve` gets its profile.
#[derive(clap::Args, Debug, Clone, Default)]
pub struct ProfileArgs {
    /// Do not measure or read a hardware profile: use the built-in defaults.
    #[arg(long, conflicts_with_all = ["reprobe", "profile"])]
    pub no_probe: bool,
    /// Measure the hardware again even when a cached profile matches.
    #[arg(long)]
    pub reprobe: bool,
    /// Read the profile from this file (e.g. written by `oominf tune --out`)
    /// instead of the cache.
    #[arg(long)]
    pub profile: Option<PathBuf>,
}
