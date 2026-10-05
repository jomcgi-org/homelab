//! FP8 e4m3 ("fn": no infinities, largest finite 448) on the host: blocked
//! quantization of dense weights at load, so only the FP8 bytes reach the device.

/// The e4m3 code nearest `x` (ties to even), saturating at +-448.
pub fn e4m3_from_f32(x: f32) -> u8 {
    let sign = if x.is_sign_negative() { 0x80 } else { 0 };
    let a = x.abs();
    if a.is_nan() || a >= 448.0 {
        return sign | 0x7e;
    }
    if a < 2f32.powi(-6) {
        // Subnormal: steps of 2^-9; rounding up to 8 steps gives the smallest
        // normal, whose code is 0x08.
        return sign | (a * 512.0).round_ties_even() as u8;
    }
    let mut e = ((a.to_bits() >> 23) & 0xff) as i32 - 127;
    let mut m = ((a / 2f32.powi(e) - 1.0) * 8.0).round_ties_even() as u8;
    if m == 8 {
        m = 0;
        e += 1;
    }
    let biased = e + 7;
    if biased > 15 || (biased == 15 && m == 7) {
        return sign | 0x7e;
    }
    sign | ((biased as u8) << 3) | m
}

/// The value of e4m3 code `b` (0x7f and 0xff, NaN, never produced here, read as 448).
pub fn e4m3_to_f32(b: u8) -> f32 {
    let s = if b & 0x80 != 0 { -1.0 } else { 1.0 };
    let e = ((b >> 3) & 0xf) as i32;
    let m = (b & 7) as f32;
    let v = if e == 0 {
        m / 8.0 * 2f32.powi(-6)
    } else if e == 15 && m == 7.0 {
        448.0
    } else {
        (1.0 + m / 8.0) * 2f32.powi(e - 7)
    };
    s * v
}

/// Weights per FP8 scale along a row: a block's absolute maximum sets its scale,
/// so one outlier costs only its block precision.
pub const BLOCK: usize = 128;

/// Blocked FP8 of bf16 `w` (`[n, k]`, raw bits): for row `r` and block
/// `b = c / BLOCK`, `scale[r * nb + b]` is the block's absolute maximum over 448 (1
/// for an all-zero block, `nb = k.div_ceil(BLOCK)`), and `q[r, c] = e4m3(w[r, c] /
/// scale)`. Rows are split over the available cores.
pub fn quantize_blocks(w: &[u16], n: usize, k: usize) -> (Vec<u8>, Vec<f32>) {
    assert_eq!(w.len(), n * k, "quantize_blocks shape");
    let nb = k.div_ceil(BLOCK);
    let mut q = vec![0u8; n * k];
    let mut scale = vec![1f32; n * nb];
    let threads = std::thread::available_parallelism()
        .map_or(4, |t| t.get())
        .min(n.max(1));
    let rows = n.div_ceil(threads.max(1)).max(1);
    std::thread::scope(|s| {
        for ((qc, sc), wc) in q
            .chunks_mut(rows * k)
            .zip(scale.chunks_mut(rows * nb))
            .zip(w.chunks(rows * k))
        {
            s.spawn(move || {
                for ((qr, sr), wr) in qc.chunks_mut(k).zip(sc.chunks_mut(nb)).zip(wc.chunks(k)) {
                    for ((qb, sb), wb) in qr
                        .chunks_mut(BLOCK)
                        .zip(sr.iter_mut())
                        .zip(wr.chunks(BLOCK))
                    {
                        let amax = wb
                            .iter()
                            .map(|&b| f32::from_bits((b as u32) << 16).abs())
                            .fold(0f32, f32::max);
                        *sb = if amax > 0.0 { amax / 448.0 } else { 1.0 };
                        for (o, &b) in qb.iter_mut().zip(wb) {
                            *o = e4m3_from_f32(f32::from_bits((b as u32) << 16) / *sb);
                        }
                    }
                }
            });
        }
    });
    (q, scale)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_code_round_trips() {
        for b in 0..=255u8 {
            if b & 0x7f == 0x7f {
                continue;
            }
            let v = e4m3_to_f32(b);
            let back = e4m3_from_f32(v);
            // +0 and -0 are both zero.
            assert!(
                back == b || (v == 0.0 && back & 0x7f == 0),
                "{b:#x} -> {v} -> {back:#x}"
            );
        }
    }

    #[test]
    fn rounds_to_nearest_even_and_saturates() {
        // Between 1 (0x38) and 1.125 (0x39): 1.0625 is a tie, to the even code.
        assert_eq!(e4m3_from_f32(1.0625), 0x38);
        assert_eq!(e4m3_from_f32(1.07), 0x39);
        assert_eq!(e4m3_from_f32(1000.0), 0x7e);
        assert_eq!(e4m3_from_f32(-1000.0), 0xfe);
        // Smallest subnormal 2^-9; half of it is a tie to zero.
        assert_eq!(e4m3_from_f32(2f32.powi(-9)), 0x01);
        assert_eq!(e4m3_from_f32(2f32.powi(-10)), 0x00);
        // Just under the smallest normal rounds up to it.
        assert_eq!(e4m3_from_f32(2f32.powi(-6) * 0.99), 0x08);
    }

    #[test]
    fn blocks_scale_to_448() {
        // Two rows of BLOCK + 2: the second block of each row is partial.
        let k = BLOCK + 2;
        let mut vals = vec![0f32; 2 * k];
        vals[0] = 1.0;
        vals[1] = -2.0;
        vals[BLOCK] = 0.5;
        let w: Vec<u16> = vals.iter().map(|v| (v.to_bits() >> 16) as u16).collect();
        let (q, s) = quantize_blocks(&w, 2, k);
        assert_eq!(s, vec![2.0 / 448.0, 0.5 / 448.0, 1.0, 1.0]);
        assert_eq!(e4m3_to_f32(q[1]) * s[0], -2.0);
        assert_eq!(e4m3_to_f32(q[BLOCK]) * s[1], 0.5);
        assert!(q[k..].iter().all(|&b| b == 0));
    }
}
