//! Routed experts computed on the host, from records in host memory.
//!
//! When an expert's record is already in host memory, computing that expert on the
//! CPU for a decode-sized step is cheaper than copying the record to the device: the
//! input and output are a few rows of activations, the record is megabytes. This
//! crate runs expert SwiGLU MLPs over NVFP4 and bf16 records on a worker pool, with
//! the same exactly decoded weights and fp32 arithmetic as the device kernels (only
//! the summation order differs).

pub mod kernel;

use std::sync::{Arc, Mutex, mpsc};

use anyhow::{Context, Result, ensure};
use oominf_core::{Bf16Record, Nvfp4Record};

use kernel::{
    Bf16Matrix, LANES, MAX_TOKENS, Nvfp4Matrix, bf16_decode, bf16_rows, gemm, nvfp4_decode,
    nvfp4_rows,
};

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
    buffers: Arc<Buffers>,
}

/// Work buffers kept between steps. A prefill step's buffers run to tens of
/// megabytes; allocating them fresh each step costs page faults that serialize the
/// workers (measured: half the pool's throughput at 8 threads).
#[derive(Default)]
struct Buffers(Mutex<Vec<Vec<f32>>>);

impl Buffers {
    /// A zeroed buffer of `len` values, reusing a kept one when one is large enough.
    fn take(&self, len: usize) -> Vec<f32> {
        let mut free = self.0.lock().unwrap();
        let mut v = match free.iter().position(|v| v.capacity() >= len) {
            Some(i) => free.swap_remove(i),
            None => free.pop().unwrap_or_default(),
        };
        drop(free);
        v.clear();
        v.resize(len, 0.0);
        v
    }

    /// Keeps `v` for a later [`Buffers::take`] (a bounded number of them).
    fn give(&self, v: Vec<f32>) {
        let mut free = self.0.lock().unwrap();
        if free.len() < MAX_KEPT {
            free.push(v);
        }
    }
}

/// Most buffers kept between steps.
const MAX_KEPT: usize = 1024;

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
        Ok(HostExperts {
            pool,
            buffers: Arc::default(),
        })
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
        let buffers = self.buffers.clone();
        self.pool.spawn(move || {
            let y = run(geo, &x, &jobs, &buffers);
            buffers.give(x);
            let _ = tx.send(y);
        });
        Pending(Some(rx))
    }

    /// A zeroed buffer of `len` values for a step's input, reusing a kept one: an
    /// input passed to [`HostExperts::submit`] is kept for reuse once the step ends.
    pub fn buffer(&self, len: usize) -> Vec<f32> {
        self.buffers.take(len)
    }

    /// Returns an output of [`Pending::wait`] (or any buffer) for reuse by later
    /// steps.
    pub fn recycle(&self, v: Vec<f32>) {
        self.buffers.give(v);
    }
}

fn silu(x: f32) -> f32 {
    x / (1.0 + (-x).exp())
}

/// Token columns a job's activations are stored with: its token count for the
/// vector-at-a-time kernels, padded to whole vectors for the GEMM.
fn stride(nt: usize) -> usize {
    if nt <= MAX_TOKENS {
        nt
    } else {
        nt.next_multiple_of(LANES)
    }
}

thread_local! {
    /// Decoded weight rows for the GEMM, reused across work items.
    static DECODED: std::cell::RefCell<Vec<f32>> = const { std::cell::RefCell::new(Vec::new()) };
}

/// Computes every job on the current pool (see [`HostExperts::submit`]).
fn run(geo: Geometry, x: &[f32], jobs: &[Job], buffers: &Buffers) -> Vec<f32> {
    use rayon::prelude::*;
    let (hidden, inter) = geo.dims();
    // SAFETY: the submitter guarantees the records stay valid and unchanged.
    let records: Vec<&[u8]> = jobs
        .iter()
        .map(|j| unsafe { std::slice::from_raw_parts(j.record as *const u8, j.len) })
        .collect();
    // Inputs of the GEMM jobs by column (`[hidden, stride]`, padding zero); the
    // others read their token rows of `x` directly.
    let columns: Vec<Vec<f32>> = jobs
        .par_iter()
        .map(|j| {
            let nt = j.tokens.len();
            if nt <= MAX_TOKENS {
                return Vec::new();
            }
            let s = stride(nt);
            let mut xt = buffers.take(hidden * s);
            for (t, &tok) in j.tokens.iter().enumerate() {
                for (c, &v) in x[tok * hidden..(tok + 1) * hidden].iter().enumerate() {
                    xt[c * s + t] = v;
                }
            }
            xt
        })
        .collect();
    let inputs: Vec<Vec<&[f32]>> = jobs
        .iter()
        .map(|j| {
            if j.tokens.len() > MAX_TOKENS {
                return Vec::new();
            }
            j.tokens
                .iter()
                .map(|&t| &x[t * hidden..(t + 1) * hidden])
                .collect()
        })
        .collect();

    // SwiGLU activations per job, `[inter, stride]`, rows split across workers.
    let mut acts: Vec<Vec<f32>> = jobs
        .iter()
        .map(|j| buffers.take(inter * stride(j.tokens.len())))
        .collect();
    let mut work: Vec<(usize, usize, &mut [f32])> = Vec::new();
    for (j, a) in acts.iter_mut().enumerate() {
        let s = stride(jobs[j].tokens.len());
        for (c, chunk) in a.chunks_mut(GATE_ROWS * s).enumerate() {
            work.push((j, c * GATE_ROWS, chunk));
        }
    }
    work.into_par_iter().for_each(|(j, row0, out)| {
        let mut up = vec![0.0; out.len()];
        if columns[j].is_empty() {
            let xs = &inputs[j];
            let rows = out.len() / xs.len();
            project(geo, records[j], Proj::Gate, row0, rows, xs, out);
            project(geo, records[j], Proj::Up, row0, rows, xs, &mut up);
        } else {
            let s = stride(jobs[j].tokens.len());
            let rows = out.len() / s;
            project_gemm(geo, records[j], Proj::Gate, row0, rows, &columns[j], s, out);
            project_gemm(
                geo,
                records[j],
                Proj::Up,
                row0,
                rows,
                &columns[j],
                s,
                &mut up,
            );
        }
        for (g, u) in out.iter_mut().zip(&up) {
            *g = silu(*g) * u;
        }
    });

    // Down projections, `[hidden, stride]` per job: the GEMM jobs read their
    // activations as stored, the others by token.
    let act_rows: Vec<Vec<Vec<f32>>> = jobs
        .iter()
        .zip(&acts)
        .map(|(j, a)| {
            let nt = j.tokens.len();
            if nt > MAX_TOKENS {
                return Vec::new();
            }
            (0..nt)
                .map(|t| (0..inter).map(|r| a[r * nt + t]).collect())
                .collect()
        })
        .collect();
    let mut downs: Vec<Vec<f32>> = jobs
        .iter()
        .map(|j| buffers.take(hidden * stride(j.tokens.len())))
        .collect();
    let mut work: Vec<(usize, usize, &mut [f32])> = Vec::new();
    for (j, d) in downs.iter_mut().enumerate() {
        let s = stride(jobs[j].tokens.len());
        for (c, chunk) in d.chunks_mut(DOWN_ROWS * s).enumerate() {
            work.push((j, c * DOWN_ROWS, chunk));
        }
    }
    work.into_par_iter().for_each(|(j, row0, out)| {
        let nt = jobs[j].tokens.len();
        if nt <= MAX_TOKENS {
            let xs: Vec<&[f32]> = act_rows[j].iter().map(Vec::as_slice).collect();
            let rows = out.len() / xs.len();
            project(geo, records[j], Proj::Down, row0, rows, &xs, out);
        } else {
            let s = stride(nt);
            project_gemm(
                geo,
                records[j],
                Proj::Down,
                row0,
                out.len() / s,
                &acts[j],
                s,
                out,
            );
        }
    });

    // Rows by token, jobs in order.
    let rows: Vec<(usize, usize)> = jobs
        .iter()
        .enumerate()
        .flat_map(|(j, job)| (0..job.tokens.len()).map(move |t| (j, t)))
        .collect();
    let mut y = buffers.take(rows.len() * hidden);
    y.par_chunks_mut(hidden)
        .zip(&rows)
        .for_each(|(out, &(j, t))| {
            let s = stride(jobs[j].tokens.len());
            for (i, o) in out.iter_mut().enumerate() {
                *o = downs[j][i * s + t];
            }
        });
    for v in columns.into_iter().chain(acts).chain(downs) {
        if v.capacity() > 0 {
            buffers.give(v);
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

/// `out[r * s + t]` for rows `row0..row0 + rows` of one projection over
/// activations by column (`xt`, `[cols, s]`): the rows are decoded once, then
/// multiplied with every token.
#[allow(clippy::too_many_arguments)]
fn project_gemm(
    geo: Geometry,
    rec: &[u8],
    p: Proj,
    row0: usize,
    rows: usize,
    xt: &[f32],
    s: usize,
    out: &mut [f32],
) {
    DECODED.with_borrow_mut(|w| {
        let (cols, scale) = match geo {
            Geometry::Nvfp4(g) => {
                let (wo, so, idx, rows_total, cols) = match p {
                    Proj::Gate => (g.gate_weight, g.gate_scale, g.scale2[0], g.inter, g.hidden),
                    Proj::Up => (g.up_weight, g.up_scale, g.scale2[1], g.inter, g.hidden),
                    Proj::Down => (g.down_weight, g.down_scale, g.scale2[2], g.hidden, g.inter),
                };
                let m = Nvfp4Matrix {
                    packed: &rec[wo..wo + rows_total * cols / 2],
                    scales: &rec[so..so + rows_total * cols / 16],
                    scale2: 1.0,
                    cols,
                };
                w.resize(rows * cols, 0.0);
                nvfp4_decode(m, row0, rows, w);
                (
                    cols,
                    f32::from_le_bytes(rec[idx * 4..idx * 4 + 4].try_into().unwrap()),
                )
            }
            Geometry::Bf16(g) => {
                let (wo, rows_total, cols) = match p {
                    Proj::Gate => (g.gate_weight, g.inter, g.hidden),
                    Proj::Up => (g.up_weight, g.inter, g.hidden),
                    Proj::Down => (g.down_weight, g.hidden, g.inter),
                };
                let m = Bf16Matrix {
                    weights: &rec[wo..wo + rows_total * cols * 2],
                    cols,
                };
                w.resize(rows * cols, 0.0);
                bf16_decode(m, row0, rows, w);
                (cols, 1.0)
            }
        };
        gemm(w, rows, cols, xt, s, scale, out);
    });
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

    /// The GEMM path (more than [`MAX_TOKENS`] tokens) gives each token what the
    /// vector path gives it alone, to fp32 summation accuracy.
    #[test]
    fn gemm_jobs_match_per_token_jobs() {
        let (hidden, inter) = (256, 64);
        let half = inter * hidden / 2;
        let sc = inter * hidden / 16;
        let geo = Nvfp4Record {
            hidden,
            inter,
            gate_weight: 64,
            gate_scale: 64 + half,
            up_weight: 64 + half + sc,
            up_scale: 64 + 2 * half + sc,
            down_weight: 64 + 2 * half + 2 * sc,
            down_scale: 64 + 3 * half + 2 * sc,
            scale2: [0, 2, 4],
        };
        let len = 64 + 3 * half + 3 * sc;
        let mut seed = 0x2545_f491_4f6c_dd1du64;
        let mut next = || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        let mut rec: Vec<u8> = (0..len).map(|_| next() as u8).collect();
        for (i, v) in [(0, 0.02f32), (2, 0.03), (4, 0.015)] {
            rec[i * 4..i * 4 + 4].copy_from_slice(&v.to_le_bytes());
        }
        for (s, n) in [
            (geo.gate_scale, sc),
            (geo.up_scale, sc),
            (geo.down_scale, sc),
        ] {
            for b in &mut rec[s..s + n] {
                *b %= 0x70;
            }
        }
        let n = 70;
        let x: Vec<f32> = (0..n * hidden)
            .map(|_| (next() % 2001) as f32 / 1000.0 - 1.0)
            .collect();
        let pool = HostExperts::new(3).unwrap();
        for nt in [5, 16, 17, 37, 70] {
            let tokens: Vec<usize> = (0..nt).map(|i| (i * 31 + 7) % n).collect();
            let job = |tokens: Vec<usize>| Job {
                record: rec.as_ptr() as usize,
                len,
                tokens,
            };
            // SAFETY: `rec` outlives the waits below.
            let all =
                unsafe { pool.submit(Geometry::Nvfp4(geo), x.clone(), vec![job(tokens.clone())]) }
                    .wait()
                    .unwrap();
            let one: Vec<Job> = tokens.iter().map(|&t| job(vec![t])).collect();
            let each = unsafe { pool.submit(Geometry::Nvfp4(geo), x.clone(), one) }
                .wait()
                .unwrap();
            let scale = each.iter().fold(0f32, |m, v| m.max(v.abs()));
            for (a, b) in all.iter().zip(&each) {
                assert!((a - b).abs() <= 1e-4 * scale, "{nt} tokens: {a} vs {b}");
            }
        }
    }
}
