//! Compiles every `kernels/*.cu` to PTX with nvcc and writes `kernels.rs`, a table of
//! `(name, ptx)` the backend loads at start-up. `NVCC` overrides the compiler path and
//! `OOMINF_CUDA_ARCH` the virtual architecture (default `compute_89`).
//!
//! Without nvcc (a machine without the CUDA toolkit) the table is empty and the
//! build warns: everything else, CPU tests included, builds and runs, and the CUDA
//! backend fails at start-up saying why. A kernel nvcc rejects still fails the
//! build.

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
    println!("cargo::rerun-if-changed=kernels");

    let mut names: Vec<String> = std::fs::read_dir("kernels")
        .expect("kernels/ directory")
        .filter_map(|e| {
            let p = e.ok()?.path();
            (p.extension()? == "cu").then(|| p.file_stem()?.to_str().map(str::to_owned))?
        })
        .collect();
    names.sort();
    if Command::new(&nvcc).arg("--version").output().is_err() {
        println!(
            "cargo::warning=nvcc not found ({nvcc}): building without CUDA kernels; the CUDA backend will refuse to start (set NVCC to build them)"
        );
        names.clear();
    }
    let mut table = String::from("pub(crate) const KERNELS: &[(&str, &str)] = &[\n");
    for name in &names {
        let src = format!("kernels/{name}.cu");
        println!("cargo::rerun-if-changed={src}");
        let ptx = out.join(format!("{name}.ptx"));
        let status = Command::new(&nvcc)
            .args(["-ptx", "-O3", "--std=c++17", &format!("-arch={arch}"), "-o"])
            .arg(&ptx)
            .arg(&src)
            .status()
            .unwrap_or_else(|e| panic!("failed to run {nvcc}: {e}"));
        assert!(status.success(), "nvcc failed on {src}");
        table.push_str(&format!(
            "    ({name:?}, include_str!({:?})),\n",
            ptx.display().to_string()
        ));
    }
    table.push_str("];\n");
    std::fs::write(out.join("kernels.rs"), table).unwrap();
}
