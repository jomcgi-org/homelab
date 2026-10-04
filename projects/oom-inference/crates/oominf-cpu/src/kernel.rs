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
