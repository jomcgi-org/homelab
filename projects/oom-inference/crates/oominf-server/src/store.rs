//! A persistent prefix store behind [`PrefixCache`]: sequences evicted from the
//! device are saved to disk, and a later request whose prompt extends one resumes
//! from it instead of prefilling those tokens (agent loops that come back hours
//! later, repeated long system prompts).
//!
//! The live sequence stays on the device as in [`LastSequence`]. When a request
//! does not extend it, the store saves it (the device-to-host copy on the engine
//! thread, the file write on a writer thread) before it is dropped; on shutdown it
//! saves it too. On a miss the store restores the longest saved sequence whose
//! tokens prefix the prompt, after checking its exact token ids, the checkpoint
//! and KV-format identity and a checksum. The directory is bounded by a byte
//! budget (least recently used first) and a time to live; host-memory caching of
//! entries comes from the page cache.
//!
//! Entry file: `OOMSNAP1`, `u32` header length, JSON header (identity, token and
//! logit counts, payload checksum), token ids (`u32`), logits (`f32`), then the
//! session's own snapshot ([`oominf_core::Session::save`]).

use std::fs::File;
use std::io::{BufReader, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime};

use anyhow::{Context, Result, ensure};
use oominf_core::Session;
use serde_json::json;

use crate::engine::{CachedSeq, LastSequence, PrefixCache};

const MAGIC: &[u8; 8] = b"OOMSNAP1";

/// Where and how much to keep.
#[derive(Debug, Clone)]
pub struct StoreConfig {
    pub dir: PathBuf,
    /// Most bytes of entries on disk.
    pub budget_bytes: u64,
    /// Entries unused for longer are deleted.
    pub ttl: Duration,
    /// Shorter sequences are not worth saving (they prefill in moments).
    pub min_tokens: usize,
    /// Identifies what produced an entry (checkpoint, KV format): entries with
    /// another identity are never restored.
    pub identity: String,
}

/// One saved sequence.
struct Entry {
    path: PathBuf,
    ids: Vec<u32>,
    /// The entry saved the logits after its last token, so an identical prompt can
    /// resume from it too.
    has_logits: bool,
    bytes: u64,
    used: SystemTime,
}

/// Counters for the log.
#[derive(Debug, Default, Clone, Copy)]
pub struct StoreStats {
    pub restored: u64,
    pub restored_tokens: u64,
    pub saved: u64,
    pub dropped: u64,
    pub corrupt: u64,
}

struct Shared {
    cfg: StoreConfig,
    index: Vec<Entry>,
    stats: StoreStats,
}

/// A saved sequence on its way to disk.
struct Pending {
    ids: Vec<u32>,
    logits: Vec<f32>,
    payload: Vec<u8>,
}

type Seq = Box<dyn Session>;

pub struct PrefixStore {
    live: LastSequence<Seq>,
    shared: Arc<Mutex<Shared>>,
    writes: Option<mpsc::SyncSender<Pending>>,
    writer: Option<std::thread::JoinHandle<()>>,
}

impl PrefixStore {
    /// Opens (creating) the store directory and indexes its entries.
    pub fn open(cfg: StoreConfig) -> Result<Self> {
        std::fs::create_dir_all(&cfg.dir)
            .with_context(|| format!("create {}", cfg.dir.display()))?;
        let mut shared = Shared {
            index: Vec::new(),
            stats: StoreStats::default(),
            cfg,
        };
        for e in std::fs::read_dir(&shared.cfg.dir)? {
            let path = e?.path();
            if path.extension().is_some_and(|x| x == "tmp") {
                let _ = std::fs::remove_file(&path);
                continue;
            }
            if path.extension().is_none_or(|x| x != "oomsnap") {
                continue;
            }
            match read_index(&path, &shared.cfg.identity) {
                Ok(Some(entry)) => shared.index.push(entry),
                // Another checkpoint or KV format: not ours to restore.
                Ok(None) => {}
                Err(_) => {
                    shared.stats.corrupt += 1;
                    let _ = std::fs::remove_file(&path);
                }
            }
        }
        shared.enforce();
        let shared = Arc::new(Mutex::new(shared));
        let (tx, rx) = mpsc::sync_channel::<Pending>(1);
        let writer_shared = shared.clone();
        let writer = std::thread::Builder::new()
            .name("oominf-store".into())
            .spawn(move || {
                for w in rx {
                    let r = write_entry(&writer_shared, w);
                    if let Err(e) = r {
                        eprintln!("oominf: prefix store write failed: {e:#}");
                    }
                }
            })?;
        Ok(PrefixStore {
            live: LastSequence::default(),
            shared,
            writes: Some(tx),
            writer: Some(writer),
        })
    }

    pub fn stats(&self) -> StoreStats {
        self.shared.lock().unwrap().stats
    }

    /// Saves `seq` if it is long enough and not already stored.
    fn save(&mut self, seq: &CachedSeq<Seq>) {
        let (min, stored) = {
            let s = self.shared.lock().unwrap();
            (s.cfg.min_tokens, s.index.iter().any(|e| e.ids == seq.ids))
        };
        if seq.ids.len() < min || stored {
            return;
        }
        let mut payload = Vec::new();
        if let Err(e) = seq.state.save(&mut payload) {
            eprintln!("oominf: prefix store cannot save this sequence: {e:#}");
            return;
        }
        let w = Pending {
            ids: seq.ids.clone(),
            logits: seq.logits.clone(),
            payload,
        };
        // One write in flight; drop rather than stall the engine.
        if let Some(tx) = &self.writes
            && tx.try_send(w).is_err()
        {
            self.shared.lock().unwrap().stats.dropped += 1;
        }
    }

    /// Restores the longest saved sequence that `prompt` extends.
    fn restore(
        &mut self,
        prompt: &[u32],
        new_session: &mut dyn FnMut() -> Result<Seq>,
    ) -> Option<CachedSeq<Seq>> {
        let (path, identity) = {
            let s = self.shared.lock().unwrap();
            let best = s
                .index
                .iter()
                .filter(|e| {
                    prompt.starts_with(&e.ids) && (prompt.len() > e.ids.len() || e.has_logits)
                })
                .max_by_key(|e| e.ids.len())?;
            (best.path.clone(), s.cfg.identity.clone())
        };
        match read_entry(&path, &identity, new_session) {
            Ok(seq) => {
                let mut s = self.shared.lock().unwrap();
                s.stats.restored += 1;
                s.stats.restored_tokens += seq.ids.len() as u64;
                if let Some(e) = s.index.iter_mut().find(|e| e.path == path) {
                    e.used = SystemTime::now();
                }
                if let Ok(f) = File::options().write(true).open(&path) {
                    let _ = f.set_modified(SystemTime::now());
                }
                Some(seq)
            }
            Err(e) => {
                eprintln!(
                    "oominf: prefix store entry {} unusable: {e:#}",
                    path.display()
                );
                let mut s = self.shared.lock().unwrap();
                s.stats.corrupt += 1;
                s.index.retain(|e| e.path != path);
                let _ = std::fs::remove_file(&path);
                None
            }
        }
    }
}

impl PrefixCache<Seq> for PrefixStore {
    fn take(
        &mut self,
        prompt: &[u32],
        new_session: &mut dyn FnMut() -> Result<Seq>,
    ) -> Option<CachedSeq<Seq>> {
        if let Some(seq) = self.live.take(prompt, new_session) {
            return Some(seq);
        }
        // The live sequence does not fit this prompt: save it, then free it before
        // a restored or fresh sequence takes the device memory.
        self.clear();
        self.restore(prompt, new_session)
    }

    fn put(&mut self, seq: CachedSeq<Seq>) {
        self.live.put(seq);
    }

    fn clear(&mut self) {
        if let Some(seq) = self.live.take_all() {
            self.save(&seq);
        }
    }

    fn discard(&mut self) {
        self.live.discard();
    }
}

impl Drop for PrefixStore {
    fn drop(&mut self) {
        self.clear();
        // Close the queue and let the writer finish the last entry.
        self.writes = None;
        if let Some(w) = self.writer.take() {
            let _ = w.join();
        }
    }
}

impl Shared {
    /// Deletes entries past their time to live, then the least recently used until
    /// the directory fits its budget.
    fn enforce(&mut self) {
        let now = SystemTime::now();
        let ttl = self.cfg.ttl;
        self.index.retain(|e| {
            let fresh = now.duration_since(e.used).map_or(true, |age| age <= ttl);
            if !fresh {
                let _ = std::fs::remove_file(&e.path);
            }
            fresh
        });
        self.index.sort_by_key(|e| e.used);
        let mut total: u64 = self.index.iter().map(|e| e.bytes).sum();
        while total > self.cfg.budget_bytes && !self.index.is_empty() {
            let e = self.index.remove(0);
            let _ = std::fs::remove_file(&e.path);
            total -= e.bytes;
        }
    }
}

fn ids_bytes(ids: &[u32]) -> Vec<u8> {
    ids.iter().flat_map(|t| t.to_le_bytes()).collect()
}

/// Writes an entry (to a temporary file, renamed when complete) and indexes it.
fn write_entry(shared: &Mutex<Shared>, w: Pending) -> Result<()> {
    let (dir, identity) = {
        let s = shared.lock().unwrap();
        (s.cfg.dir.clone(), s.cfg.identity.clone())
    };
    let name = format!(
        "{}-{}",
        oominf_format::checksum(&ids_bytes(&w.ids)),
        w.ids.len()
    );
    let path = dir.join(format!("{name}.oomsnap"));
    let tmp = dir.join(format!("{name}.tmp"));
    let header = json!({
        "identity": identity,
        "tokens": w.ids.len(),
        "logits": w.logits.len(),
        "payload_bytes": w.payload.len(),
        "payload_xxh3": oominf_format::checksum(&w.payload),
    });
    {
        let mut f = BufWriter::new(File::create(&tmp)?);
        let h = serde_json::to_vec(&header)?;
        f.write_all(MAGIC)?;
        f.write_all(&(h.len() as u32).to_le_bytes())?;
        f.write_all(&h)?;
        f.write_all(&ids_bytes(&w.ids))?;
        for l in &w.logits {
            f.write_all(&l.to_le_bytes())?;
        }
        f.write_all(&w.payload)?;
        f.into_inner()?.sync_all()?;
    }
    std::fs::rename(&tmp, &path)?;
    let bytes = std::fs::metadata(&path)?.len();
    let mut s = shared.lock().unwrap();
    s.index.retain(|e| e.path != path);
    s.index.push(Entry {
        path,
        has_logits: !w.logits.is_empty(),
        ids: w.ids,
        bytes,
        used: SystemTime::now(),
    });
    s.stats.saved += 1;
    s.enforce();
    Ok(())
}

/// Reads an entry's header: `(header, token ids)`.
fn read_header(r: &mut impl Read) -> Result<(serde_json::Value, Vec<u32>)> {
    let mut magic = [0u8; 8];
    r.read_exact(&mut magic)?;
    ensure!(&magic == MAGIC, "not a prefix store entry");
    let mut len = [0u8; 4];
    r.read_exact(&mut len)?;
    let mut h = vec![0u8; u32::from_le_bytes(len) as usize];
    r.read_exact(&mut h)?;
    let header: serde_json::Value = serde_json::from_slice(&h)?;
    let n = header["tokens"].as_u64().context("entry tokens")? as usize;
    let mut ids = vec![0u8; n * 4];
    r.read_exact(&mut ids)?;
    let ids = ids
        .as_chunks::<4>()
        .0
        .iter()
        .map(|c| u32::from_le_bytes(*c))
        .collect();
    Ok((header, ids))
}

/// Indexes an entry; `None` if it belongs to another identity.
fn read_index(path: &Path, identity: &str) -> Result<Option<Entry>> {
    let mut r = BufReader::new(File::open(path)?);
    let (header, ids) = read_header(&mut r)?;
    if header["identity"] != identity {
        return Ok(None);
    }
    let meta = std::fs::metadata(path)?;
    Ok(Some(Entry {
        path: path.to_owned(),
        has_logits: header["logits"].as_u64().is_some_and(|n| n > 0),
        ids,
        bytes: meta.len(),
        used: meta.modified()?,
    }))
}

/// Reads and verifies an entry, restoring its state into a new session.
fn read_entry(
    path: &Path,
    identity: &str,
    new_session: &mut dyn FnMut() -> Result<Seq>,
) -> Result<CachedSeq<Seq>> {
    let mut r = BufReader::new(File::open(path)?);
    let (header, ids) = read_header(&mut r)?;
    ensure!(
        header["identity"] == identity,
        "entry from another checkpoint"
    );
    let mut logits = vec![0u8; header["logits"].as_u64().context("entry logits")? as usize * 4];
    r.read_exact(&mut logits)?;
    let logits = logits
        .as_chunks::<4>()
        .0
        .iter()
        .map(|c| f32::from_le_bytes(*c))
        .collect();
    let mut payload = vec![0u8; header["payload_bytes"].as_u64().context("payload size")? as usize];
    r.read_exact(&mut payload)?;
    ensure!(
        header["payload_xxh3"] == oominf_format::checksum(&payload).as_str(),
        "payload checksum mismatch"
    );
    let mut state = new_session()?;
    state.load(&mut payload.as_slice())?;
    ensure!(
        state.len() == ids.len(),
        "restored {} of {} tokens",
        state.len(),
        ids.len()
    );
    Ok(CachedSeq { ids, state, logits })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A session whose state is just its length; saving writes the length.
    struct Fake(usize);

    impl Session for Fake {
        fn len(&self) -> usize {
            self.0
        }
        fn prefill(&mut self, t: &[u32], _: &dyn Fn() -> bool) -> Result<Option<Vec<f32>>> {
            self.0 += t.len();
            Ok(Some(vec![0.0; 4]))
        }
        fn step(&mut self, t: &[u32]) -> Result<Vec<f32>> {
            self.0 += t.len();
            Ok(vec![0.0; 4])
        }
        fn save(&self, w: &mut dyn Write) -> Result<()> {
            Ok(w.write_all(&(self.0 as u64).to_le_bytes())?)
        }
        fn load(&mut self, r: &mut dyn Read) -> Result<()> {
            let mut b = [0u8; 8];
            r.read_exact(&mut b)?;
            self.0 = u64::from_le_bytes(b) as usize;
            Ok(())
        }
    }

    fn config(dir: &Path, identity: &str) -> StoreConfig {
        StoreConfig {
            dir: dir.to_owned(),
            budget_bytes: 1 << 30,
            ttl: Duration::from_secs(3600),
            min_tokens: 4,
            identity: identity.into(),
        }
    }

    fn seq(ids: &[u32]) -> CachedSeq<Seq> {
        CachedSeq {
            ids: ids.to_vec(),
            state: Box::new(Fake(ids.len())),
            logits: vec![1.0, 2.0],
        }
    }

    fn fresh() -> Result<Seq> {
        Ok(Box::new(Fake(0)))
    }

    fn scratch(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("oominf-store-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        d
    }

    #[test]
    fn evicted_sequence_resumes_a_later_extension() {
        let dir = scratch("resume");
        {
            let mut s = PrefixStore::open(config(&dir, "a")).unwrap();
            s.put(seq(&[1, 2, 3, 4, 5]));
            // A prompt that does not extend the live sequence evicts (saves) it.
            assert!(s.take(&[9, 9, 9], &mut fresh).is_none());
        }
        let mut s = PrefixStore::open(config(&dir, "a")).unwrap();
        let got = s.take(&[1, 2, 3, 4, 5, 6], &mut fresh).expect("restored");
        assert_eq!(got.ids, vec![1, 2, 3, 4, 5]);
        assert_eq!(got.state.len(), 5);
        assert_eq!(got.logits, vec![1.0, 2.0]);
        assert!(s.take(&[1, 2, 7], &mut fresh).is_none(), "not a prefix");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn short_sequences_and_other_identities_are_ignored() {
        let dir = scratch("identity");
        {
            let mut s = PrefixStore::open(config(&dir, "a")).unwrap();
            s.put(seq(&[1, 2, 3]));
            s.clear();
            s.put(seq(&[5, 6, 7, 8, 9]));
        }
        let mut other = PrefixStore::open(config(&dir, "b")).unwrap();
        assert!(
            other.take(&[5, 6, 7, 8, 9, 10], &mut fresh).is_none(),
            "other identity"
        );
        drop(other);
        let mut s = PrefixStore::open(config(&dir, "a")).unwrap();
        assert!(
            s.take(&[1, 2, 3, 4], &mut fresh).is_none(),
            "below min_tokens"
        );
        assert!(s.take(&[5, 6, 7, 8, 9, 10], &mut fresh).is_some());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn corrupt_entries_are_deleted() {
        let dir = scratch("corrupt");
        {
            let mut s = PrefixStore::open(config(&dir, "a")).unwrap();
            s.put(seq(&[1, 2, 3, 4, 5]));
        }
        let entry = std::fs::read_dir(&dir)
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path();
        let mut bytes = std::fs::read(&entry).unwrap();
        let n = bytes.len();
        bytes[n - 1] ^= 0xff;
        std::fs::write(&entry, bytes).unwrap();
        let mut s = PrefixStore::open(config(&dir, "a")).unwrap();
        assert!(s.take(&[1, 2, 3, 4, 5, 6], &mut fresh).is_none());
        assert_eq!(s.stats().corrupt, 1);
        assert!(!entry.exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn budget_keeps_the_most_recently_used() {
        let dir = scratch("budget");
        let mut cfg = config(&dir, "a");
        {
            let mut s = PrefixStore::open(cfg.clone()).unwrap();
            s.put(seq(&[1, 1, 1, 1, 1]));
            s.clear();
            std::thread::sleep(Duration::from_millis(20));
            s.put(seq(&[2, 2, 2, 2, 2]));
        }
        let one = std::fs::read_dir(&dir).unwrap().next().unwrap().unwrap();
        cfg.budget_bytes = one.metadata().unwrap().len();
        let mut s = PrefixStore::open(cfg).unwrap();
        assert!(
            s.take(&[1, 1, 1, 1, 1, 1], &mut fresh).is_none(),
            "oldest evicted"
        );
        assert!(s.take(&[2, 2, 2, 2, 2, 2], &mut fresh).is_some());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
