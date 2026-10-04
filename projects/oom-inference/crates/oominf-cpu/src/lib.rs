//! Routed experts computed on the host, from records in host memory.
//!
//! When an expert's record is already in host memory, computing that expert on the
//! CPU for a decode-sized step is cheaper than copying the record to the device: the
//! input and output are a few rows of activations, the record is megabytes. This
//! crate runs expert SwiGLU MLPs over NVFP4 and bf16 records on a worker pool, with
//! the same exactly decoded weights and fp32 arithmetic as the device kernels (only
//! the summation order differs).

pub mod kernel;

use std::sync::mpsc;

use anyhow::{Context, Result, ensure};
use oominf_core::{Bf16Record, Nvfp4Record};

use kernel::{Bf16Matrix, MAX_TOKENS, Nvfp4Matrix, bf16_rows, nvfp4_rows};

/// How an expert record is laid out.
#[derive(Debug, Clone, Copy)]
pub enum Geometry {
    Nvfp4(Nvfp4Record),
    Bf16(Bf16Record),
}

impl Geometry {
    fn dims(&self) -> (usize, usize) {
        match self {
            Geometry::Nvfp4(g) => (g.hidden, g.inter),
            Geometry::Bf16(g) => (g.hidden, g.inter),
        }
    }
}

/// One expert of a step: its record in host memory and the activation rows (token
/// indices into the step's input) routed to it.
pub struct Job {
    pub record: usize,
    pub len: usize,
    pub tokens: Vec<usize>,
}

/// Rows of a projection per work item.
const GATE_ROWS: usize = 32;
const DOWN_ROWS: usize = 128;

/// A worker pool that computes experts on the host.
pub struct HostExperts {
    pool: rayon::ThreadPool,
}

/// Results of [`HostExperts::submit`], available once the work finishes. Dropping
/// it waits for the work, so the records it reads are never released early.
pub struct Pending(Option<mpsc::Receiver<Vec<f32>>>);

impl Pending {
    /// Waits for the outputs: for each job in order, one `hidden`-wide row per
    /// routed token, in the job's token order (unweighted down projections).
    pub fn wait(mut self) -> Result<Vec<f32>> {
        let rx = self.0.take().expect("pending results are taken once");
        rx.recv().context("host expert worker failed")
    }
}

impl Drop for Pending {
    fn drop(&mut self) {
        if let Some(rx) = self.0.take() {
            let _ = rx.recv();
        }
    }
}

impl HostExperts {
    pub fn new(threads: usize) -> Result<Self> {
        ensure!(threads > 0, "host expert pool needs at least one thread");
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(threads)
            .thread_name(|i| format!("oominf-expert-{i}"))
            .build()?;
        Ok(HostExperts { pool })
    }

    pub fn threads(&self) -> usize {
        self.pool.current_num_threads()
    }

    /// Starts computing every job's expert for its tokens of `x` (`[t, hidden]`) and
    /// returns at once.
    ///
    /// # Safety
    /// Each job's `record..record + len` must be readable and stay unchanged until
    /// [`Pending::wait`] returns.
    pub unsafe fn submit(&self, geo: Geometry, x: Vec<f32>, jobs: Vec<Job>) -> Pending {
        let (tx, rx) = mpsc::channel();
        self.pool.spawn(move || {
            let y = run(geo, &x, &jobs);
            let _ = tx.send(y);
        });
        Pending(Some(rx))
    }
}

fn silu(x: f32) -> f32 {
    x / (1.0 + (-x).exp())
}

/// Computes every job on the current pool (see [`HostExperts::submit`]).
fn run(geo: Geometry, x: &[f32], jobs: &[Job]) -> Vec<f32> {
    use rayon::prelude::*;
    let (hidden, inter) = geo.dims();
    // SAFETY: the submitter guarantees the records stay valid and unchanged.
    let records: Vec<&[u8]> = jobs
        .iter()
        .map(|j| unsafe { std::slice::from_raw_parts(j.record as *const u8, j.len) })
        .collect();
    let inputs: Vec<Vec<&[f32]>> = jobs
        .iter()
        .map(|j| {
            j.tokens
                .iter()
                .map(|&t| &x[t * hidden..(t + 1) * hidden])
                .collect()
        })
        .collect();

    // SwiGLU activations per job, `[inter, n_tokens]`, rows split across workers.
    let mut acts: Vec<Vec<f32>> = jobs
        .iter()
        .map(|j| vec![0.0; inter * j.tokens.len()])
        .collect();
    let mut work: Vec<(usize, usize, &mut [f32])> = Vec::new();
    for (j, a) in acts.iter_mut().enumerate() {
        let nt = jobs[j].tokens.len();
        for (c, chunk) in a.chunks_mut(GATE_ROWS * nt).enumerate() {
            work.push((j, c * GATE_ROWS, chunk));
        }
    }
    work.into_par_iter().for_each(|(j, row0, out)| {
        let xs = &inputs[j];
        let mut up = vec![0.0; out.len()];
        let rows = out.len() / xs.len();
        project(geo, records[j], Proj::Gate, row0, rows, xs, out);
        project(geo, records[j], Proj::Up, row0, rows, xs, &mut up);
        for (g, u) in out.iter_mut().zip(&up) {
            *g = silu(*g) * u;
        }
    });

    // Down projections, `[hidden, n_tokens]` per job, from the activations by token.
    let act_rows: Vec<Vec<Vec<f32>>> = jobs
        .iter()
        .zip(&acts)
        .map(|(j, a)| {
            let nt = j.tokens.len();
            (0..nt)
                .map(|t| (0..inter).map(|r| a[r * nt + t]).collect())
                .collect()
        })
        .collect();
    let mut downs: Vec<Vec<f32>> = jobs
        .iter()
        .map(|j| vec![0.0; hidden * j.tokens.len()])
        .collect();
    let mut work: Vec<(usize, usize, &mut [f32])> = Vec::new();
    for (j, d) in downs.iter_mut().enumerate() {
        let nt = jobs[j].tokens.len();
        for (c, chunk) in d.chunks_mut(DOWN_ROWS * nt).enumerate() {
            work.push((j, c * DOWN_ROWS, chunk));
        }
    }
    work.into_par_iter().for_each(|(j, row0, out)| {
        let xs: Vec<&[f32]> = act_rows[j].iter().map(Vec::as_slice).collect();
        let rows = out.len() / xs.len();
        project(geo, records[j], Proj::Down, row0, rows, &xs, out);
    });

    // Rows by token, jobs in order.
    let mut y = Vec::with_capacity(jobs.iter().map(|j| j.tokens.len()).sum::<usize>() * hidden);
    for (j, d) in jobs.iter().zip(&downs) {
        let nt = j.tokens.len();
        for t in 0..nt {
            y.extend((0..hidden).map(|i| d[i * nt + t]));
        }
    }
    y
}

#[derive(Clone, Copy)]
enum Proj {
    Gate,
    Up,
    Down,
}

/// `out[r * xs.len() + t]` for rows `row0..row0 + rows` of one projection, in
/// groups of at most [`MAX_TOKENS`] activation rows.
fn project(
    geo: Geometry,
    rec: &[u8],
    p: Proj,
    row0: usize,
    rows: usize,
    xs: &[&[f32]],
    out: &mut [f32],
) {
    if xs.len() > MAX_TOKENS {
        // Rare: more rows than the vector kernels take; split by token.
        let nt = xs.len();
        for (t, x) in xs.iter().enumerate() {
            let mut one = vec![0.0; rows];
            project(geo, rec, p, row0, rows, std::slice::from_ref(x), &mut one);
            for r in 0..rows {
                out[r * nt + t] = one[r];
            }
        }
        return;
    }
    match geo {
        Geometry::Nvfp4(g) => {
            let (w, s, idx, rows_total, cols) = match p {
                Proj::Gate => (g.gate_weight, g.gate_scale, g.scale2[0], g.inter, g.hidden),
                Proj::Up => (g.up_weight, g.up_scale, g.scale2[1], g.inter, g.hidden),
                Proj::Down => (g.down_weight, g.down_scale, g.scale2[2], g.hidden, g.inter),
            };
            let scale2 = f32::from_le_bytes(rec[idx * 4..idx * 4 + 4].try_into().unwrap());
            let m = Nvfp4Matrix {
                packed: &rec[w..w + rows_total * cols / 2],
                scales: &rec[s..s + rows_total * cols / 16],
                scale2,
                cols,
            };
            nvfp4_rows(m, row0, rows, xs, out);
        }
        Geometry::Bf16(g) => {
            let (w, rows_total, cols) = match p {
                Proj::Gate => (g.gate_weight, g.inter, g.hidden),
                Proj::Up => (g.up_weight, g.inter, g.hidden),
                Proj::Down => (g.down_weight, g.hidden, g.inter),
            };
            let m = Bf16Matrix {
                weights: &rec[w..w + rows_total * cols * 2],
                cols,
            };
            bf16_rows(m, row0, rows, xs, out);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A tiny NVFP4 record (`hidden` 32, `inter` 16) with every weight decoding to
    /// 1.0 * scale and scale_2 = 1: gate = up = sum(x) for every row.
    #[test]
    fn swiglu_expert_on_a_known_record() {
        let (hidden, inter) = (32, 16);
        let geo = Nvfp4Record {
            hidden,
            inter,
            gate_weight: 64,
            gate_scale: 64 + inter * hidden / 2,
            up_weight: 512,
            up_scale: 512 + inter * hidden / 2,
            down_weight: 1024,
            down_scale: 1024 + hidden * inter / 2,
            scale2: [0, 2, 4],
        };
        let mut rec = vec![0u8; 2048];
        for i in [0, 2, 4] {
            rec[i * 4..i * 4 + 4].copy_from_slice(&1.0f32.to_le_bytes());
        }
        // Code 2 (1.0) in both nibbles, scale byte 0x38 (1.0).
        let fill = |rec: &mut [u8], w: usize, s: usize, rows: usize, cols: usize| {
            rec[w..w + rows * cols / 2].fill(0x22);
            rec[s..s + rows * cols / 16].fill(0x38);
        };
        fill(&mut rec, geo.gate_weight, geo.gate_scale, inter, hidden);
        fill(&mut rec, geo.up_weight, geo.up_scale, inter, hidden);
        fill(&mut rec, geo.down_weight, geo.down_scale, hidden, inter);
        let x: Vec<f32> = (0..2 * hidden).map(|i| (i % 7) as f32 * 0.01).collect();
        let pool = HostExperts::new(2).unwrap();
        let job = Job {
            record: rec.as_ptr() as usize,
            len: rec.len(),
            tokens: vec![1, 0],
        };
        // SAFETY: `rec` outlives the wait below.
        let y = unsafe { pool.submit(Geometry::Nvfp4(geo), x.clone(), vec![job]) }
            .wait()
            .unwrap();
        for (i, &t) in [1usize, 0].iter().enumerate() {
            let s: f32 = x[t * hidden..(t + 1) * hidden].iter().sum();
            let h = silu(s) * s;
            let want = h * inter as f32;
            for &v in &y[i * hidden..(i + 1) * hidden] {
                assert!(
                    (v - want).abs() <= 1e-4 * want.abs().max(1.0),
                    "{v} vs {want}"
                );
            }
        }
    }
}
