//! Fault injection for the tiers, on the mock device ([`crate::mock`]) over a
//! small synthetic model: reads are delayed or failed through the reader's
//! [`FaultHook`], and every record a fetch hands out is checked byte for byte.
//! Covers the two risks the protocol spec does not model: slow read-ahead
//! exhausting a small host tier, and buffers reused while reads are in flight
//! after an I/O error.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, mpsc};
use std::time::Duration;

use anyhow::Result;
use oominf_core::{ExpertSource, Staged};
use oominf_format::{Model, RecordSchema, Source, Writer};

use super::*;
use crate::host::{Fault, FaultHook, IoConfig, ReadMode};
use crate::mock::Mock;
use crate::policy::Lru;

const LAYERS: u32 = 4;
const NE: u32 = 64;
const STRIDE: usize = 8192;

/// Byte `i` of record `key`.
fn byte(key: u32, i: usize) -> u8 {
    (key as usize * 131 + i * 7 + i / 4096) as u8
}

fn synthetic(dir: &std::path::Path) -> Arc<Model> {
    let source = Source {
        path: "synthetic".into(),
        model_type: "test".into(),
        fingerprint: String::new(),
    };
    let mut w = Writer::create(dir, source).unwrap();
    let schema = RecordSchema::new("test", &[("w", "U8", &[STRIDE as u64])]).unwrap();
    assert_eq!(schema.stride as usize, STRIDE);
    for l in 0..LAYERS {
        w.add_expert_group(l, NE, schema.clone(), |e, rec| {
            for (i, b) in rec.iter_mut().enumerate() {
                *b = byte(l * NE + e, i);
            }
            Ok(())
        })
        .unwrap();
    }
    w.finish().unwrap();
    Arc::new(Model::open(dir).unwrap())
}

struct Rig {
    b: Arc<Mock>,
    t: TieredExperts<Mock>,
    _dir: tempfile::TempDir,
}

fn rig(sizes: TierSizes, mode: ReadMode, faults: Option<FaultHook>) -> Result<Rig> {
    let dir = tempfile::tempdir()?;
    let model = synthetic(dir.path());
    let b = Arc::new(Mock::new(64 << 20));
    let t = TieredExperts::new(
        b.clone(),
        model,
        "test",
        sizes,
        Box::new(Lru::default()),
        Box::new(Lru::default()),
        &IoConfig { mode, faults },
    )?;
    Ok(Rig { b, t, _dir: dir })
}

/// Checks every record `staged` hands out for `experts` of `layer`, once its
/// fetch finished.
fn check(layer: u32, experts: &[u32], staged: &Staged) {
    for (i, &e) in experts.iter().enumerate() {
        let key = layer * NE + e;
        let ptr = match staged.host[i] {
            Some(p) => p as *const u8,
            None => staged.addrs[i] as *const u8,
        };
        // SAFETY: a record of STRIDE bytes the tier keeps until the next fetch.
        let got = unsafe { std::slice::from_raw_parts(ptr, STRIDE) };
        if let Some(bad) = (0..STRIDE).find(|&j| got[j] != byte(key, j)) {
            panic!(
                "layer {layer} expert {e} (host {}): byte {bad} is {} not {}",
                staged.host[i].is_some(),
                got[bad],
                byte(key, bad)
            );
        }
    }
}

/// A deterministic pseudo-random sequence.
struct Lcg(u64);

impl Lcg {
    fn next(&mut self) -> u32 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        (self.0 >> 33) as u32
    }

    /// `n` distinct experts.
    fn experts(&mut self, n: usize) -> Vec<u32> {
        let mut v = Vec::with_capacity(n);
        while v.len() < n {
            let e = self.next() % NE;
            if !v.contains(&e) {
                v.push(e);
            }
        }
        v
    }
}

/// One decode-like token: every layer fetches `k` experts (host compute allowed)
/// after a lookahead prediction that is usually wrong.
fn decode_token(r: &mut Rig, rng: &mut Lcg, k: usize) -> Result<()> {
    let b = r.b.clone();
    for layer in 0..LAYERS {
        let experts = rng.experts(k);
        let staged = r.t.begin_fetch(&b, layer, &experts, true)?;
        r.t.finish_fetch(&b)?;
        check(layer, &experts, &staged);
        let next = (layer + 1) % LAYERS;
        r.t.prefetch(&b, next, &rng.experts(k))?;
    }
    Ok(())
}

/// One prefill-like pass: each layer stages the next layer's predicted experts
/// ahead, then fetches its own in `groups` large groups.
fn prefill(r: &mut Rig, rng: &mut Lcg, per_group: usize, groups: usize) -> Result<()> {
    let b = r.b.clone();
    r.t.stage_ahead(&b, 0, &rng.experts(per_group))?;
    for layer in 0..LAYERS {
        for g in 0..groups {
            let experts = rng.experts(per_group);
            let staged = r.t.begin_fetch(&b, layer, &experts, false)?;
            r.t.finish_stage_ahead(&b)?;
            r.t.finish_fetch(&b)?;
            check(layer, &experts, &staged);
            if layer + 1 < LAYERS {
                let predicted = rng.experts(per_group);
                if g == 0 {
                    r.t.stage_ahead(&b, layer + 1, &predicted)?;
                } else {
                    r.t.stage_more(&b, layer + 1, &predicted)?;
                }
            }
        }
    }
    Ok(())
}

/// Runs `f` on its own thread; a hang (a tier waiting for a slot that never frees)
/// fails the test instead of blocking it.
fn within(secs: u64, f: impl FnOnce() + Send + 'static) {
    let (tx, rx) = mpsc::channel();
    let h = std::thread::spawn(move || {
        f();
        let _ = tx.send(());
    });
    match rx.recv_timeout(Duration::from_secs(secs)) {
        Ok(()) => h.join().unwrap(),
        Err(mpsc::RecvTimeoutError::Disconnected) => {
            if let Err(p) = h.join() {
                std::panic::resume_unwind(p);
            }
        }
        Err(mpsc::RecvTimeoutError::Timeout) => panic!("tier made no progress in {secs}s"),
    }
}

/// Every read path this machine supports.
fn modes() -> Vec<ReadMode> {
    let dir = tempfile::tempdir().unwrap();
    synthetic(dir.path());
    let path = dir.path().join(oominf_format::EXPERTS_FILE);
    ReadMode::ALL
        .into_iter()
        .filter(|&m| crate::host::select_read_mode(&path, Some(m)).is_ok())
        .collect()
}

/// Delays every read by up to `max_ms`, varying by offset.
fn jitter(max_ms: u64) -> FaultHook {
    Arc::new(move |off| Fault {
        delay: Duration::from_millis((off / STRIDE as u64 * 7) % (max_ms + 1)),
        fail: false,
    })
}

/// Smallest tiers with a stage: 64 main VRAM slots plus a 64-slot stage, the
/// minimum host tier and a short staging ring.
fn small() -> TierSizes {
    TierSizes {
        vram_slots: 2 * NE as usize,
        host_slots: min_host_slots(NE as usize, NE as usize, true) + 4,
        host_stage_slots: 4,
        max_fetch: NE as usize,
    }
}

#[test]
fn slow_reads_keep_records_intact_on_every_read_path() {
    for mode in modes() {
        within(120, move || {
            let mut r = rig(small(), mode, Some(jitter(3))).unwrap();
            r.t.lookahead = true;
            r.t.host_compute = 2;
            let mut rng = Lcg(1);
            for _ in 0..4 {
                decode_token(&mut r, &mut rng, 6).unwrap();
            }
            prefill(&mut r, &mut rng, 24, 2).unwrap();
            for _ in 0..4 {
                decode_token(&mut r, &mut rng, 6).unwrap();
            }
        });
    }
}

/// Risk (a): lookahead reads still in flight pin their host slots. With a host
/// tier at its minimum and slow reads, a fetch must still find slots for its own
/// records (it waits for or caps the read-ahead) rather than fail or hang.
#[test]
fn slow_lookahead_does_not_exhaust_a_small_host_tier() {
    within(120, || {
        // Only lookahead reads (decode, after a fetch) are slow.
        let slow = Arc::new(AtomicBool::new(false));
        let s = slow.clone();
        let hook: FaultHook = Arc::new(move |_| Fault {
            delay: Duration::from_millis(if s.load(Ordering::Relaxed) { 40 } else { 0 }),
            fail: false,
        });
        let mut r = rig(small(), ReadMode::Pread, Some(hook)).unwrap();
        r.t.lookahead = true;
        let mut rng = Lcg(7);
        slow.store(true, Ordering::Relaxed);
        for _ in 0..3 {
            // Fetches of the threshold size, each after a full-size wrong prediction.
            decode_token(&mut r, &mut rng, stream_threshold(NE as usize)).unwrap();
        }
    });
}

/// Risk (a) during prefill: stage-ahead reads into a small host tier and a short
/// staging ring, all slow; fetches wait for ring slots instead of failing.
#[test]
fn slow_stage_ahead_waits_for_a_short_ring() {
    within(120, || {
        let mut sizes = small();
        sizes.host_stage_slots = 1;
        let mut r = rig(sizes, ReadMode::Pread, Some(jitter(5))).unwrap();
        let mut rng = Lcg(3);
        prefill(&mut r, &mut rng, 40, 3).unwrap();
        prefill(&mut r, &mut rng, 40, 3).unwrap();
    });
}

/// Risk (b): when a read fails, its batch is abandoned only after every other
/// read in flight has landed, and slots are reused only after the copies reading
/// them ran. Later fetches (same keys and others) must see intact records.
#[test]
fn failed_reads_never_leave_slots_to_late_writes() {
    for mode in modes() {
        within(120, move || {
            let fail_at = Arc::new(AtomicU64::new(u64::MAX));
            let f = fail_at.clone();
            let hook: FaultHook = Arc::new(move |off| {
                let at = f.load(Ordering::Relaxed);
                let fail = off == at;
                Fault {
                    // While a failure is armed, the failing read finishes first and
                    // its siblings land later.
                    delay: Duration::from_millis(match (fail, at) {
                        (true, _) => 5,
                        (false, u64::MAX) => 0,
                        (false, _) => 20 + (off / STRIDE as u64 % 5) * 10,
                    }),
                    fail,
                }
            });
            let mut r = rig(small(), mode, Some(hook)).unwrap();
            r.t.lookahead = true;
            r.t.host_compute = 2;
            let b = r.b.clone();
            let mut rng = Lcg(11);
            for round in 0..3u32 {
                // A prefill-sized fetch of layer 1 with one failing record.
                let experts = rng.experts(40);
                // A record no tier holds, so the fetch reads it.
                let cold = |t: &TieredExperts<Mock>, k: u32| {
                    t.vram.peek(k).is_none()
                        && t.stage.peek(k).is_none()
                        && t.host.peek(k).is_none()
                };
                let e = *experts
                    .iter()
                    .skip(round as usize * 3)
                    .find(|&&e| cold(&r.t, NE + e))
                    .expect("a cold record");
                fail_at.store(
                    r.t.model.record_location(1, e).unwrap().0,
                    Ordering::Relaxed,
                );
                let err =
                    r.t.begin_fetch(&b, 1, &experts, false)
                        .and_then(|_| r.t.finish_fetch(&b))
                        .unwrap_err();
                assert!(format!("{err:#}").contains("injected"), "{err:#}");
                fail_at.store(u64::MAX, Ordering::Relaxed);
                // Stage-ahead and lookahead batches with a failing read too.
                let ahead = rng.experts(30);
                fail_at.store(
                    r.t.model.record_location(2, ahead[0]).unwrap().0,
                    Ordering::Relaxed,
                );
                let staged = r.t.stage_ahead(&b, 2, &ahead).and_then(|()| {
                    let s = r.t.begin_fetch(&b, 2, &ahead, false)?;
                    r.t.finish_fetch(&b)?;
                    Ok(s)
                });
                if let Ok(s) = &staged {
                    check(2, &ahead, s);
                }
                let predicted = rng.experts(8);
                fail_at.store(
                    r.t.model.record_location(3, predicted[0]).unwrap().0,
                    Ordering::Relaxed,
                );
                let _ = r.t.prefetch(&b, 3, &predicted);
                let _ =
                    r.t.begin_fetch(&b, 3, &predicted, true)
                        .and_then(|_| r.t.finish_fetch(&b));
                fail_at.store(u64::MAX, Ordering::Relaxed);
                // Everything afterwards is intact.
                let staged = r.t.begin_fetch(&b, 1, &experts, false).unwrap();
                r.t.finish_fetch(&b).unwrap();
                check(1, &experts, &staged);
                for _ in 0..2 {
                    decode_token(&mut r, &mut rng, 6).unwrap();
                }
                prefill(&mut r, &mut rng, 24, 2).unwrap();
            }
        });
    }
}

#[test]
fn pinning_retries_smaller_down_to_the_minimum() {
    let dir = tempfile::tempdir().unwrap();
    let model = synthetic(dir.path());
    let mut mock = Mock::new(64 << 20);
    // At most 20 host slots pin at once.
    mock.pin_limit = 20 * STRIDE;
    let b = Arc::new(mock);
    let sizes = TierSizes {
        host_slots: 64,
        ..small()
    };
    let make = |sizes| {
        TieredExperts::new(
            b.clone(),
            model.clone(),
            "test",
            sizes,
            Box::new(Lru::default()),
            Box::new(Lru::default()),
            &IoConfig::new(ReadMode::Pread),
        )
    };
    let t = make(sizes).unwrap();
    assert!(
        t.host.capacity() <= 20 && t.host.capacity() >= 8,
        "{}",
        t.host.capacity()
    );
    let mut mock = Mock::new(64 << 20);
    mock.pin_limit = 4 * STRIDE;
    let b = Arc::new(mock);
    let e = TieredExperts::new(
        b,
        model.clone(),
        "test",
        sizes,
        Box::new(Lru::default()),
        Box::new(Lru::default()),
        &IoConfig::new(ReadMode::Pread),
    )
    .err()
    .expect("below the minimum");
    assert!(format!("{e:#}").contains("minimum"), "{e:#}");
}

#[test]
fn vram_allocation_degrades_to_the_chunks_it_gets() {
    let dir = tempfile::tempdir().unwrap();
    let model = synthetic(dir.path());
    // Room for the stage and two of four main chunks.
    let b = Arc::new(Mock::new((NE as usize + 2 * CHUNK_SLOTS) * STRIDE));
    let sizes = TierSizes {
        vram_slots: NE as usize + 4 * CHUNK_SLOTS,
        // Enough for a main tier without a stage.
        host_slots: NE as usize,
        ..small()
    };
    let new = |b: Arc<Mock>| {
        TieredExperts::new(
            b,
            model.clone(),
            "test",
            sizes,
            Box::new(Lru::default()),
            Box::new(Lru::default()),
            &IoConfig::new(ReadMode::Pread),
        )
    };
    let t = new(b).unwrap();
    assert_eq!(
        (t.vram.capacity(), t.stage.capacity()),
        (2 * CHUNK_SLOTS, NE as usize)
    );
    // Room for the stage only: the stage is dropped so the main tier gets its
    // minimum (a whole layer without a stage).
    let t = new(Arc::new(Mock::new(NE as usize * STRIDE))).unwrap();
    assert_eq!((t.vram.capacity(), t.stage.capacity()), (NE as usize, 0));
    let e = new(Arc::new(Mock::new(NE as usize / 2 * STRIDE)))
        .err()
        .expect("below the minimum");
    assert!(format!("{e:#}").contains("minimum VRAM tier"), "{e:#}");
}
