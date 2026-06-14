//! Minimal example service for the buck2 image-tooling demo: a static musl
//! binary built by buck2 (same `rust_binary` primitive loom uses), layered onto
//! an apko/Wolfi base image, and deployed by an example Helm chart.

fn main() {
    println!("hello from a buck2-built rust service");
}
