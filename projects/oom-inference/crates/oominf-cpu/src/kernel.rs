//! Matrix-vector products over expert records, row by row: NVFP4 (ModelOpt,
//! group 16) and bf16 weights, fp32 activations and accumulation.
//!
//! NVFP4 weights are decoded exactly as the device kernels and the reference do:
//! `w = e2m1(code) * (fp8(scale) * scale_2)` in fp32. Only the summation order
//! differs between the AVX-512 and the scalar paths.

/// E2M1 values by 4-bit code (low three bits magnitude, high bit sign).
const E2M1: [f32; 16] = [
    0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, -0.0, -0.5, -1.0, -1.5, -2.0, -3.0, -4.0, -6.0,
];

/// E4M3 (fn variant: no infinities, `S.1111.111` is NaN) to f32, exactly.
fn e4m3(byte: u8) -> f32 {
    let sign = if byte & 0x80 != 0 { -1.0 } else { 1.0 };
    let exp = (byte >> 3) & 0x0f;
    let man = (byte & 0x07) as f32;
    if exp == 0x0f && byte & 0x07 == 0x07 {
        return f32::NAN;
    }
    let mag = if exp == 0 {
        man / 8.0 * 2f32.powi(-6)
    } else {
        (1.0 + man / 8.0) * 2f32.powi(exp as i32 - 7)
    };
    sign * mag
}

fn e4m3_table() -> &'static [f32; 256] {
    static TABLE: std::sync::OnceLock<[f32; 256]> = std::sync::OnceLock::new();
    TABLE.get_or_init(|| std::array::from_fn(|b| e4m3(b as u8)))
}

/// One NVFP4 projection inside a record: `rows x cols` weights, two per byte (low
/// nibble first), one fp8 scale per 16 columns, and `scale2` for the whole matrix.
#[derive(Clone, Copy)]
pub struct Nvfp4Matrix<'a> {
    pub packed: &'a [u8],
    pub scales: &'a [u8],
    pub scale2: f32,
    pub cols: usize,
}

/// One bf16 projection inside a record: `rows x cols` weights, row-major.
#[derive(Clone, Copy)]
pub struct Bf16Matrix<'a> {
    pub weights: &'a [u8],
    pub cols: usize,
}

/// `out[r * xs.len() + t] = W[row0 + r] . xs[t]` for `r in 0..n_rows`.
pub fn nvfp4_rows(m: Nvfp4Matrix, row0: usize, n_rows: usize, xs: &[&[f32]], out: &mut [f32]) {
    #[cfg(target_arch = "x86_64")]
    if xs.len() <= MAX_TOKENS && is_x86_feature_detected!("avx512f") {
        // SAFETY: the feature was detected at run time.
        unsafe { avx512::nvfp4_rows(m, row0, n_rows, xs, out) };
        return;
    }
    nvfp4_rows_scalar(m, row0, n_rows, xs, out);
}

/// As [`nvfp4_rows`] for bf16 weights.
pub fn bf16_rows(m: Bf16Matrix, row0: usize, n_rows: usize, xs: &[&[f32]], out: &mut [f32]) {
    #[cfg(target_arch = "x86_64")]
    if xs.len() <= MAX_TOKENS && is_x86_feature_detected!("avx512f") {
        // SAFETY: the feature was detected at run time.
        unsafe { avx512::bf16_rows(m, row0, n_rows, xs, out) };
        return;
    }
    bf16_rows_scalar(m, row0, n_rows, xs, out);
}

/// Most activation rows the vector kernels take at once.
pub const MAX_TOKENS: usize = 4;

pub fn nvfp4_rows_scalar(
    m: Nvfp4Matrix,
    row0: usize,
    n_rows: usize,
    xs: &[&[f32]],
    out: &mut [f32],
) {
    let fp8 = e4m3_table();
    let nt = xs.len();
    for r in 0..n_rows {
        let row = row0 + r;
        let packed = &m.packed[row * m.cols / 2..(row + 1) * m.cols / 2];
        let scales = &m.scales[row * m.cols / 16..(row + 1) * m.cols / 16];
        for (t, x) in xs.iter().enumerate() {
            let mut acc = 0.0f32;
            for (c, &x) in x.iter().enumerate().take(m.cols) {
                let byte = packed[c / 2];
                let code = if c % 2 == 0 { byte & 0x0f } else { byte >> 4 };
                let w = E2M1[code as usize] * (fp8[scales[c / 16] as usize] * m.scale2);
                acc += w * x;
            }
            out[r * nt + t] = acc;
        }
    }
}

pub fn bf16_rows_scalar(m: Bf16Matrix, row0: usize, n_rows: usize, xs: &[&[f32]], out: &mut [f32]) {
    let nt = xs.len();
    for r in 0..n_rows {
        let row = &m.weights[(row0 + r) * m.cols * 2..(row0 + r + 1) * m.cols * 2];
        for (t, x) in xs.iter().enumerate() {
            let mut acc = 0.0f32;
            for (c, &x) in x.iter().enumerate().take(m.cols) {
                let w =
                    f32::from_bits((u16::from_le_bytes([row[2 * c], row[2 * c + 1]]) as u32) << 16);
                acc += w * x;
            }
            out[r * nt + t] = acc;
        }
    }
}

/// Token columns a GEMM activation row is padded to (one vector of fp32 lanes).
pub const LANES: usize = 16;

/// Decodes rows `row0..row0 + n_rows` of `m` to fp32 into `w` (`[n_rows, cols]`):
/// `e2m1(code) * fp8(scale)`, exact in fp32. `scale2` is left out, for the caller to
/// apply to the products' sums as the device kernels do.
pub fn nvfp4_decode(m: Nvfp4Matrix, row0: usize, n_rows: usize, w: &mut [f32]) {
    assert!(w.len() >= n_rows * m.cols && m.packed.len() >= (row0 + n_rows) * m.cols / 2);
    assert!(m.scales.len() >= (row0 + n_rows) * m.cols / 16);
    #[cfg(target_arch = "x86_64")]
    if is_x86_feature_detected!("avx512f") {
        // SAFETY: the feature was detected and the bounds checked above.
        unsafe { avx512::nvfp4_decode(m, row0, n_rows, w) };
        return;
    }
    nvfp4_decode_scalar(m, row0, n_rows, w);
}

pub fn nvfp4_decode_scalar(m: Nvfp4Matrix, row0: usize, n_rows: usize, w: &mut [f32]) {
    let fp8 = e4m3_table();
    let groups = m.cols / 16;
    for r in 0..n_rows {
        let row = row0 + r;
        let packed = &m.packed[row * m.cols / 2..(row + 1) * m.cols / 2];
        let scales = &m.scales[row * groups..(row + 1) * groups];
        let out = &mut w[r * m.cols..(r + 1) * m.cols];
        for g in 0..groups {
            let s = fp8[scales[g] as usize];
            for i in 0..8 {
                let b = packed[g * 8 + i];
                out[g * 16 + 2 * i] = E2M1[(b & 0x0f) as usize] * s;
                out[g * 16 + 2 * i + 1] = E2M1[(b >> 4) as usize] * s;
            }
        }
    }
}

/// As [`nvfp4_decode`] for bf16 weights (exact in fp32).
pub fn bf16_decode(m: Bf16Matrix, row0: usize, n_rows: usize, w: &mut [f32]) {
    let base = row0 * m.cols * 2;
    for (i, v) in w[..n_rows * m.cols].iter_mut().enumerate() {
        let b = base + 2 * i;
        *v = f32::from_bits((u16::from_le_bytes([m.weights[b], m.weights[b + 1]]) as u32) << 16);
    }
}

/// `out[r * ntp + t] = scale * sum_c w[r * cols + c] * xt[c * ntp + t]`: decoded
/// weight rows (`[n_rows, cols]`) times activations stored by column (`[cols, ntp]`,
/// `ntp` a multiple of [`LANES`]), every product and sum in fp32.
pub fn gemm(
    w: &[f32],
    n_rows: usize,
    cols: usize,
    xt: &[f32],
    ntp: usize,
    scale: f32,
    out: &mut [f32],
) {
    assert!(ntp.is_multiple_of(LANES) && w.len() >= n_rows * cols && xt.len() >= cols * ntp);
    assert!(out.len() >= n_rows * ntp);
    #[cfg(target_arch = "x86_64")]
    if is_x86_feature_detected!("avx512f") {
        // SAFETY: the feature was detected and the bounds checked above.
        unsafe { avx512::gemm(w, n_rows, cols, xt, ntp, scale, out) };
        return;
    }
    gemm_scalar(w, n_rows, cols, xt, ntp, scale, out);
}

pub fn gemm_scalar(
    w: &[f32],
    n_rows: usize,
    cols: usize,
    xt: &[f32],
    ntp: usize,
    scale: f32,
    out: &mut [f32],
) {
    for r in 0..n_rows {
        for t in 0..ntp {
            let mut acc = 0.0f32;
            for c in 0..cols {
                acc = w[r * cols + c].mul_add(xt[c * ntp + t], acc);
            }
            out[r * ntp + t] = acc * scale;
        }
    }
}

#[cfg(target_arch = "x86_64")]
mod avx512 {
    use std::arch::x86_64::*;

    use super::{Bf16Matrix, E2M1, MAX_TOKENS, Nvfp4Matrix, e4m3_table};

    /// Sums the lanes of each accumulator into `out[r * nt + t]`.
    #[target_feature(enable = "avx512f")]
    fn store(acc: &[__m512; MAX_TOKENS], nt: usize, out: &mut [f32], r: usize) {
        for t in 0..nt {
            out[r * nt + t] = _mm512_reduce_add_ps(acc[t]);
        }
    }

    /// One pass of the GEMM over columns `k0..k0 + kc`, for the token vectors from
    /// `t0`: `w` and `xt` point at column `k0`.
    struct Pass {
        w: *const f32,
        stride: usize,
        kc: usize,
        xt: *const f32,
        ntp: usize,
        t0: usize,
        /// The first pass stores its sums; later ones add to them.
        first: bool,
        /// Applied when storing (the last pass's).
        scale: f32,
    }

    /// A tile of `R` weight rows by `TV` vectors of token columns, accumulated in
    /// registers over the pass's columns.
    ///
    /// # Safety
    /// `p.w` holds `R` rows of `p.stride` (from `p.kc` columns on), `p.xt` `p.kc`
    /// rows of `p.ntp` from `p.t0 + TV * 16`, and `out` `R` rows of `p.ntp`.
    #[target_feature(enable = "avx512f")]
    unsafe fn tile<const R: usize, const TV: usize>(p: &Pass, w: *const f32, out: *mut f32) {
        let mut acc = [[_mm512_setzero_ps(); TV]; R];
        if !p.first {
            for (r, a) in acc.iter_mut().enumerate() {
                for (v, x) in a.iter_mut().enumerate() {
                    *x = unsafe { _mm512_loadu_ps(out.add(r * p.ntp + p.t0 + v * 16)) };
                }
            }
        }
        for c in 0..p.kc {
            let mut xv = [_mm512_setzero_ps(); TV];
            for (v, x) in xv.iter_mut().enumerate() {
                *x = unsafe { _mm512_loadu_ps(p.xt.add(c * p.ntp + p.t0 + v * 16)) };
            }
            for (r, a) in acc.iter_mut().enumerate() {
                let wv = _mm512_set1_ps(unsafe { *w.add(r * p.stride + c) });
                for v in 0..TV {
                    a[v] = _mm512_fmadd_ps(wv, xv[v], a[v]);
                }
            }
        }
        let s = _mm512_set1_ps(p.scale);
        for (r, a) in acc.iter().enumerate() {
            for (v, &x) in a.iter().enumerate() {
                let o = unsafe { out.add(r * p.ntp + p.t0 + v * 16) };
                unsafe { _mm512_storeu_ps(o, _mm512_mul_ps(x, s)) };
            }
        }
    }

    /// Rows in tiles of `R`, then smaller tiles, for `TV` token vectors.
    ///
    /// # Safety
    /// As for [`tile`], for `n_rows` rows.
    #[target_feature(enable = "avx512f")]
    unsafe fn rows<const R: usize, const TV: usize>(p: &Pass, n_rows: usize, out: *mut f32) {
        let mut r = 0;
        while r + R <= n_rows {
            unsafe { tile::<R, TV>(p, p.w.add(r * p.stride), out.add(r * p.ntp)) };
            r += R;
        }
        // The remainder in smaller tiles (each smaller than `R`), not row by row.
        for (size, call) in [
            (8, tile::<8, TV> as unsafe fn(&Pass, *const f32, *mut f32)),
            (4, tile::<4, TV>),
            (2, tile::<2, TV>),
            (1, tile::<1, TV>),
        ] {
            while size < R && r + size <= n_rows {
                unsafe { call(p, p.w.add(r * p.stride), out.add(r * p.ntp)) };
                r += size;
            }
        }
    }

    /// # Safety
    /// Requires AVX-512F; bounds as checked by [`super::nvfp4_decode`].
    #[target_feature(enable = "avx512f,avx512bw")]
    pub unsafe fn nvfp4_decode(m: Nvfp4Matrix, row0: usize, n_rows: usize, w: &mut [f32]) {
        let fp8 = e4m3_table();
        let lut = unsafe { _mm512_loadu_ps(E2M1.as_ptr()) };
        let groups = m.cols / 16;
        for r in 0..n_rows {
            let row = row0 + r;
            let packed = &m.packed[row * m.cols / 2..(row + 1) * m.cols / 2];
            let scales = &m.scales[row * groups..(row + 1) * groups];
            let out = &mut w[r * m.cols..(r + 1) * m.cols];
            for g in 0..groups {
                let v = u64::from_le_bytes(packed[g * 8..g * 8 + 8].try_into().unwrap());
                let lo = v & 0x0f0f_0f0f_0f0f_0f0f;
                let hi = (v >> 4) & 0x0f0f_0f0f_0f0f_0f0f;
                let codes =
                    _mm_unpacklo_epi8(_mm_cvtsi64_si128(lo as i64), _mm_cvtsi64_si128(hi as i64));
                let idx = _mm512_cvtepu8_epi32(codes);
                let s = _mm512_set1_ps(fp8[scales[g] as usize]);
                let wv = _mm512_mul_ps(_mm512_permutexvar_ps(idx, lut), s);
                // SAFETY: the row holds m.cols values.
                unsafe { _mm512_storeu_ps(out.as_mut_ptr().add(g * 16), wv) };
            }
        }
    }

    /// Columns per pass: a pass's activations (`KC x 64` fp32) stay in L2 while
    /// every row tile reads them.
    const KC: usize = 512;

    /// # Safety
    /// Requires AVX-512F; bounds as checked by [`super::gemm`].
    #[target_feature(enable = "avx512f")]
    pub unsafe fn gemm(
        w: &[f32],
        n_rows: usize,
        cols: usize,
        xt: &[f32],
        ntp: usize,
        scale: f32,
        out: &mut [f32],
    ) {
        let (w, xt, out) = (w.as_ptr(), xt.as_ptr(), out.as_mut_ptr());
        let vectors = ntp / 16;
        let mut v = 0;
        // Up to 24 accumulators: 4 vectors x 6 rows, 3 x 8, 2 x 12, 1 x 16.
        while v < vectors {
            let t0 = v * 16;
            let mut k0 = 0;
            while k0 < cols {
                let kc = KC.min(cols - k0);
                let pass = Pass {
                    w: unsafe { w.add(k0) },
                    stride: cols,
                    kc,
                    xt: unsafe { xt.add(k0 * ntp) },
                    ntp,
                    t0,
                    first: k0 == 0,
                    scale: if k0 + kc == cols { scale } else { 1.0 },
                };
                unsafe {
                    match vectors - v {
                        1 => rows::<16, 1>(&pass, n_rows, out),
                        2 => rows::<12, 2>(&pass, n_rows, out),
                        3 => rows::<8, 3>(&pass, n_rows, out),
                        _ => rows::<6, 4>(&pass, n_rows, out),
                    }
                }
                k0 += kc;
            }
            v += (vectors - v).min(4);
        }
    }

    /// # Safety
    /// Requires AVX-512F and BW; `m` must hold `row0 + n_rows` rows and every `xs[t]`
    /// at least `m.cols` values (a multiple of 16).
    #[target_feature(enable = "avx512f,avx512bw")]
    pub unsafe fn nvfp4_rows(
        m: Nvfp4Matrix,
        row0: usize,
        n_rows: usize,
        xs: &[&[f32]],
        out: &mut [f32],
    ) {
        let fp8 = e4m3_table();
        let nt = xs.len();
        let lut = unsafe { _mm512_loadu_ps(E2M1.as_ptr()) };
        let groups = m.cols / 16;
        for r in 0..n_rows {
            let row = row0 + r;
            let packed = &m.packed[row * m.cols / 2..(row + 1) * m.cols / 2];
            let scales = &m.scales[row * groups..(row + 1) * groups];
            let mut acc = [_mm512_setzero_ps(); MAX_TOKENS];
            for g in 0..groups {
                let v = u64::from_le_bytes(packed[g * 8..g * 8 + 8].try_into().unwrap());
                let lo = v & 0x0f0f_0f0f_0f0f_0f0f;
                let hi = (v >> 4) & 0x0f0f_0f0f_0f0f_0f0f;
                // Bytes lo0, hi0, lo1, hi1, ...: the codes of columns 0, 1, 2, 3, ...
                let codes =
                    _mm_unpacklo_epi8(_mm_cvtsi64_si128(lo as i64), _mm_cvtsi64_si128(hi as i64));
                let idx = _mm512_cvtepu8_epi32(codes);
                let sg = fp8[scales[g] as usize] * m.scale2;
                let w = _mm512_mul_ps(_mm512_permutexvar_ps(idx, lut), _mm512_set1_ps(sg));
                for t in 0..nt {
                    // SAFETY: xs[t] holds m.cols values.
                    let x = unsafe { _mm512_loadu_ps(xs[t].as_ptr().add(g * 16)) };
                    acc[t] = _mm512_fmadd_ps(w, x, acc[t]);
                }
            }
            store(&acc, nt, out, r);
        }
    }

    /// # Safety
    /// As for [`nvfp4_rows`].
    #[target_feature(enable = "avx512f,avx512bw")]
    pub unsafe fn bf16_rows(
        m: Bf16Matrix,
        row0: usize,
        n_rows: usize,
        xs: &[&[f32]],
        out: &mut [f32],
    ) {
        let nt = xs.len();
        for r in 0..n_rows {
            let base = (row0 + r) * m.cols * 2;
            let row = &m.weights[base..base + m.cols * 2];
            let mut acc = [_mm512_setzero_ps(); MAX_TOKENS];
            for g in 0..m.cols / 16 {
                // SAFETY: the row holds m.cols bf16 values.
                let words = unsafe { _mm256_loadu_si256(row.as_ptr().add(g * 32).cast()) };
                let w = _mm512_castsi512_ps(_mm512_slli_epi32(_mm512_cvtepu16_epi32(words), 16));
                for t in 0..nt {
                    // SAFETY: xs[t] holds m.cols values.
                    let x = unsafe { _mm512_loadu_ps(xs[t].as_ptr().add(g * 16)) };
                    acc[t] = _mm512_fmadd_ps(w, x, acc[t]);
                }
            }
            store(&acc, nt, out, r);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rng(seed: &mut u64) -> u64 {
        *seed ^= *seed << 13;
        *seed ^= *seed >> 7;
        *seed ^= *seed << 17;
        *seed
    }

    /// The vector path matches a float64 evaluation of the same exactly-decoded
    /// weights to fp32 summation accuracy, as the scalar path does.
    #[test]
    fn nvfp4_vector_matches_reference() {
        let (rows, cols, nt) = (37, 2560, 3);
        let mut seed = 0x9e37_79b9_7f4a_7c15;
        let packed: Vec<u8> = (0..rows * cols / 2).map(|_| rng(&mut seed) as u8).collect();
        // Finite, positive scales (no NaN codes).
        let scales: Vec<u8> = (0..rows * cols / 16)
            .map(|_| (rng(&mut seed) % 0x77) as u8)
            .collect();
        let xs: Vec<Vec<f32>> = (0..nt)
            .map(|_| {
                (0..cols)
                    .map(|_| (rng(&mut seed) % 2001) as f32 / 1000.0 - 1.0)
                    .collect()
            })
            .collect();
        let xr: Vec<&[f32]> = xs.iter().map(Vec::as_slice).collect();
        let m = Nvfp4Matrix {
            packed: &packed,
            scales: &scales,
            scale2: 0.0123,
            cols,
        };
        let (mut vec_out, mut sc_out) = (vec![0.0; rows * nt], vec![0.0; rows * nt]);
        nvfp4_rows(m, 0, rows, &xr, &mut vec_out);
        nvfp4_rows_scalar(m, 0, rows, &xr, &mut sc_out);
        let fp8 = e4m3_table();
        for r in 0..rows {
            for t in 0..nt {
                let (mut exact, mut mag) = (0f64, 0f64);
                for c in 0..cols {
                    let b = packed[r * cols / 2 + c / 2];
                    let code = if c % 2 == 0 { b & 15 } else { b >> 4 };
                    let w = E2M1[code as usize]
                        * (fp8[scales[r * cols / 16 + c / 16] as usize] * m.scale2);
                    exact += w as f64 * xs[t][c] as f64;
                    mag += (w as f64 * xs[t][c] as f64).abs();
                }
                let tol = mag * 1e-6 + 1e-12;
                for got in [vec_out[r * nt + t], sc_out[r * nt + t]] {
                    assert!(
                        (got as f64 - exact).abs() <= tol,
                        "row {r} token {t}: {got} vs {exact}"
                    );
                }
            }
        }
    }

    #[test]
    fn bf16_vector_matches_scalar() {
        let (rows, cols, nt) = (19, 640, 2);
        let mut seed = 42;
        let w: Vec<u8> = (0..rows * cols)
            .flat_map(|_| {
                let v = ((rng(&mut seed) % 2001) as f32 / 1000.0 - 1.0).to_bits() >> 16;
                (v as u16).to_le_bytes()
            })
            .collect();
        let xs: Vec<Vec<f32>> = (0..nt)
            .map(|_| {
                (0..cols)
                    .map(|_| (rng(&mut seed) % 2001) as f32 / 1000.0 - 1.0)
                    .collect()
            })
            .collect();
        let xr: Vec<&[f32]> = xs.iter().map(Vec::as_slice).collect();
        let m = Bf16Matrix { weights: &w, cols };
        let (mut a, mut b) = (vec![0.0; rows * nt], vec![0.0; rows * nt]);
        bf16_rows(m, 0, rows, &xr, &mut a);
        bf16_rows_scalar(m, 0, rows, &xr, &mut b);
        for (x, y) in a.iter().zip(&b) {
            assert!((x - y).abs() <= 1e-4 * y.abs().max(1.0), "{x} vs {y}");
        }
    }

    #[test]
    fn e4m3_decodes_known_values() {
        assert_eq!(e4m3(0x00), 0.0);
        assert_eq!(e4m3(0x38), 1.0);
        assert_eq!(e4m3(0x7e), 448.0);
        assert_eq!(e4m3(0x01), 2f32.powi(-9));
        assert_eq!(e4m3(0xb8), -1.0);
        assert!(e4m3(0x7f).is_nan());
    }
}
