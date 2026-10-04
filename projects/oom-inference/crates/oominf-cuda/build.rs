//! Compiles `kernels/*.cu` to PTX with nvcc. `NVCC` overrides the compiler path and
//! `OOMINF_CUDA_ARCH` the virtual architecture (default `compute_89`).

use std::path::PathBuf;
use std::process::Command;

fn main() {
    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap());
    let nvcc = std::env::var("NVCC").unwrap_or_else(|_| {
        for c in ["/usr/local/cuda/bin/nvcc", "/usr/local/cuda-13.0/bin/nvcc"] {
            if std::path::Path::new(c).exists() {
                return c.to_owned();
            }
        }
        "nvcc".to_owned()
    });
    let arch = std::env::var("OOMINF_CUDA_ARCH").unwrap_or_else(|_| "compute_89".to_owned());
    println!("cargo::rerun-if-env-changed=NVCC");
    println!("cargo::rerun-if-env-changed=OOMINF_CUDA_ARCH");
    // One kernel file for now; add names here as backends grow.
    #[allow(clippy::single_element_loop)]
    for name in ["ops"] {
        let src = format!("kernels/{name}.cu");
        println!("cargo::rerun-if-changed={src}");
        let status = Command::new(&nvcc)
            .args(["-ptx", "-O3", "--std=c++17", &format!("-arch={arch}"), "-o"])
            .arg(out.join(format!("{name}.ptx")))
            .arg(&src)
            .status()
            .unwrap_or_else(|e| panic!("failed to run {nvcc}: {e}"));
        assert!(status.success(), "nvcc failed on {src}");
    }
}
