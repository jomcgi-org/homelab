//! A host-memory stand-in for a device, for testing the tiers without a GPU.
//!
//! "Device" buffers are heap memory and addresses are their pointers. Copies are
//! queued and run as late as the [`Transfer`] contract allows: only when an event
//! covering them is waited on (`event_wait`, `compute_wait`, a later queue's
//! `copies_wait` dependency being run) or a queue is synchronised. `event_done`
//! reports an event done only once something else ran it, so a tier that reuses a
//! copy's source or destination too early copies the wrong bytes and the tests see
//! them.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use anyhow::{Result, bail};
use oominf_core::{DeviceBuffer, Memory, Transfer};

/// A buffer type the tiers never use.
pub struct Unused;

impl DeviceBuffer for Unused {
    fn len(&self) -> usize {
        0
    }
}

pub struct Bytes(Box<[u8]>, Arc<Mutex<usize>>);

impl Drop for Bytes {
    fn drop(&mut self) {
        *self.1.lock().unwrap() += self.0.len();
    }
}

impl DeviceBuffer for Bytes {
    fn len(&self) -> usize {
        self.0.len()
    }
}

#[derive(Clone)]
struct Op {
    dst: u64,
    src: u64,
    len: usize,
    /// Must run first: copies of other queues, each up to a count.
    after: Vec<(usize, u64)>,
}

#[derive(Default)]
struct Queue {
    /// Copies queued so far, and run so far (a prefix).
    pushed: u64,
    ran: u64,
    pending: VecDeque<Op>,
    /// Applies to copies queued from now on: (queue, count) to run first.
    barrier: Vec<(usize, u64)>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Event {
    /// Compute runs synchronously here: always complete.
    Compute,
    Copies {
        queue: usize,
        upto: u64,
    },
}

pub struct Mock {
    queues: Mutex<Vec<Queue>>,
    /// Device memory `zeros_bytes` may still hand out.
    free: Arc<Mutex<usize>>,
    total: usize,
    /// Largest pinning that succeeds (bytes).
    pub pin_limit: usize,
}

impl Mock {
    pub fn new(device_bytes: usize) -> Self {
        Mock {
            queues: Mutex::default(),
            free: Arc::new(Mutex::new(device_bytes)),
            total: device_bytes,
            pin_limit: usize::MAX,
        }
    }

    /// Runs `queue`'s copies up to number `upto`.
    fn run(&self, queue: usize, upto: u64) {
        loop {
            let (index, op) = {
                let qs = self.queues.lock().unwrap();
                let q = &qs[queue];
                match q.pending.front() {
                    Some(op) if q.ran < upto => (q.ran, op.clone()),
                    _ => return,
                }
            };
            for &(queue, upto) in &op.after {
                self.run(queue, upto);
            }
            let mut qs = self.queues.lock().unwrap();
            let q = &mut qs[queue];
            // A dependency may have run this queue further meanwhile.
            if q.ran != index {
                continue;
            }
            q.pending.pop_front();
            q.ran += 1;
            // SAFETY: both addresses are live buffers (device buffers or pinned host
            // slots) of at least `len` bytes, as the tier promised when queueing.
            unsafe { std::ptr::copy(op.src as *const u8, op.dst as *mut u8, op.len) };
        }
    }

    fn run_event(&self, e: &Event) {
        if let Event::Copies { queue, upto } = *e {
            self.run(queue, upto);
        }
    }

    fn push(&self, queue: usize, dst: u64, src: u64, len: usize) {
        let mut qs = self.queues.lock().unwrap();
        let q = &mut qs[queue];
        let after = q.barrier.clone();
        q.pending.push_back(Op {
            dst,
            src,
            len,
            after,
        });
        q.pushed += 1;
    }

    fn run_all(&self) {
        let n = self.queues.lock().unwrap().len();
        for q in 0..n {
            self.run(q, u64::MAX);
        }
    }
}

impl Memory for Mock {
    type F32 = Unused;
    type Bf16 = Unused;
    type Bytes = Bytes;
    type I32 = Unused;
    type U64 = Unused;

    fn uninit(&self, _: usize) -> Result<Unused> {
        unimplemented!()
    }
    fn zeros(&self, _: usize) -> Result<Unused> {
        unimplemented!()
    }
    fn fill_zero(&self, _: &mut Unused) -> Result<()> {
        unimplemented!()
    }
    fn upload_f32(&self, _: &[f32]) -> Result<Unused> {
        unimplemented!()
    }
    fn upload_into(&self, _: &[f32], _: &mut Unused) -> Result<()> {
        unimplemented!()
    }
    fn download_f32(&self, _: &Unused) -> Result<Vec<f32>> {
        unimplemented!()
    }
    fn write_f32_at(&self, _: &[f32], _: &mut Unused, _: usize) -> Result<()> {
        unimplemented!()
    }
    fn upload_bf16(&self, _: &[u16]) -> Result<Unused> {
        unimplemented!()
    }
    fn uninit_bf16(&self, _: usize) -> Result<Unused> {
        unimplemented!()
    }
    fn upload_bytes(&self, _: &[u8]) -> Result<Bytes> {
        unimplemented!()
    }
    fn uninit_bytes(&self, n: usize) -> Result<Bytes> {
        self.zeros_bytes(n)
    }
    fn zeros_bytes(&self, n: usize) -> Result<Bytes> {
        let mut free = self.free.lock().unwrap();
        if n > *free {
            bail!(
                "out of mock device memory ({n} bytes asked, {} free)",
                *free
            );
        }
        *free -= n;
        Ok(Bytes(vec![0u8; n].into_boxed_slice(), self.free.clone()))
    }
    fn download_bytes(&self, b: &Bytes) -> Result<Vec<u8>> {
        self.sync()?;
        Ok(b.0.to_vec())
    }
    fn copy_bytes(&self, _: &Bytes, _: usize, _: &mut Bytes, _: usize, _: usize) -> Result<()> {
        unimplemented!()
    }
    fn upload_i32(&self, _: &[i32]) -> Result<Unused> {
        unimplemented!()
    }
    fn download_i32(&self, _: &Unused) -> Result<Vec<i32>> {
        unimplemented!()
    }
    fn write_i32(&self, _: &[i32], _: &mut Unused) -> Result<()> {
        unimplemented!()
    }
    fn zeros_u64(&self, _: usize) -> Result<Unused> {
        unimplemented!()
    }
    fn write_u64(&self, _: &[u64], _: &mut Unused) -> Result<()> {
        unimplemented!()
    }
    fn bytes_addr(&self, b: &Bytes) -> u64 {
        b.0.as_ptr() as u64
    }
    fn sync(&self) -> Result<()> {
        self.run_all();
        Ok(())
    }
    fn mem_info(&self) -> Result<(usize, usize)> {
        Ok((*self.free.lock().unwrap(), self.total))
    }
}

impl Transfer for Mock {
    type CopyQueue = usize;
    type Event = Event;
    type Download = ();

    fn copy_queue(&self) -> Result<usize> {
        let mut qs = self.queues.lock().unwrap();
        qs.push(Queue::default());
        Ok(qs.len() - 1)
    }
    unsafe fn pin_host(&self, _: *mut u8, len: usize) -> Result<()> {
        if len > self.pin_limit {
            bail!("mock pinning of {len} bytes refused");
        }
        Ok(())
    }
    unsafe fn unpin_host(&self, _: *mut u8) {}
    unsafe fn copy_to_device(&self, q: &usize, dst: u64, src: *const u8, len: usize) -> Result<()> {
        self.push(*q, dst, src as u64, len);
        Ok(())
    }
    unsafe fn download_async(&self, _: &Unused, _: *mut i32, _: usize) -> Result<()> {
        unimplemented!()
    }
    fn download_start_i32(&self, _: &Unused, _: usize) -> Result<()> {
        unimplemented!()
    }
    fn download_start_f32(&self, _: &Unused, _: usize) -> Result<()> {
        unimplemented!()
    }
    fn download_wait_i32(&self, _: ()) -> Result<Vec<i32>> {
        unimplemented!()
    }
    fn download_wait_f32(&self, _: ()) -> Result<Vec<f32>> {
        unimplemented!()
    }
    unsafe fn copy_on_device(&self, q: &usize, dst: u64, src: u64, len: usize) -> Result<()> {
        self.push(*q, dst, src, len);
        Ok(())
    }
    fn record_compute(&self) -> Result<Event> {
        Ok(Event::Compute)
    }
    fn record_copies(&self, q: &usize) -> Result<Event> {
        let qs = self.queues.lock().unwrap();
        Ok(Event::Copies {
            queue: *q,
            upto: qs[*q].pushed,
        })
    }
    fn copies_wait(&self, q: &usize, e: &Event) -> Result<()> {
        if let Event::Copies { queue, upto } = *e {
            let mut qs = self.queues.lock().unwrap();
            let b = &mut qs[*q].barrier;
            match b.iter_mut().find(|(w, _)| *w == queue) {
                Some((_, n)) => *n = (*n).max(upto),
                None => b.push((queue, upto)),
            }
        }
        Ok(())
    }
    fn compute_wait(&self, e: &Event) -> Result<()> {
        self.run_event(e);
        Ok(())
    }
    fn event_done(&self, e: &Event) -> Result<bool> {
        Ok(match *e {
            Event::Compute => true,
            Event::Copies { queue, upto } => self.queues.lock().unwrap()[queue].ran >= upto,
        })
    }
    fn event_wait(&self, e: &Event) -> Result<()> {
        self.run_event(e);
        Ok(())
    }
    fn copies_sync(&self, q: &usize) -> Result<()> {
        self.run(*q, u64::MAX);
        Ok(())
    }
}
