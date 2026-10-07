//! Direct Metal operations for Apple Silicon, with shared CPU/GPU buffers.
//!
//! Commands are ordered on one queue and submitted at host synchronization points.
//! The model and expert cache share the same physical memory budget.
#![cfg(target_os = "macos")]

use std::collections::{BTreeMap, HashMap};
use std::marker::PhantomData;
use std::sync::{Arc, Mutex, Weak};

use anyhow::{Context, Result, ensure};
use metal::{
    Buffer, CommandBuffer, CommandQueue, ComputePipelineState, Device, MTLCommandBufferStatus,
    MTLResourceOptions, MTLSize,
};
use oominf_core::DeviceBuffer;

mod decoder;
mod elementwise;
mod linear;
mod memory;
mod norm;
#[cfg(test)]
mod tests;
mod transfer;

struct Allocation {
    buffer: Buffer,
    bytes: usize,
}

/// Typed shared-memory buffer. Metal retains encoded buffers until commands finish.
pub struct Dev<T> {
    allocation: Arc<Allocation>,
    len: usize,
    marker: PhantomData<T>,
}

impl<T> DeviceBuffer for Dev<T> {
    fn len(&self) -> usize {
        self.len
    }
}

impl<T> Dev<T> {
    fn buffer(&self) -> &metal::BufferRef {
        &self.allocation.buffer
    }
}

/// A unified-memory Metal GPU. It owns one ordered compute queue.
pub struct Gpu {
    device: Device,
    queue: CommandQueue,
    kernels: HashMap<&'static str, ComputePipelineState>,
    pending: Mutex<Option<CommandBuffer>>,
    allocations: Mutex<BTreeMap<u64, Weak<Allocation>>>,
    memory_limit: u64,
}

impl Gpu {
    pub fn new() -> Result<Self> {
        Self::with_memory_limit(u64::MAX)
    }

    /// Cap Metal allocations within the process's combined host/GPU RAM budget.
    /// The composition root must also account for host tiers and session state.
    pub fn with_memory_limit(memory_limit: u64) -> Result<Self> {
        objc::rc::autoreleasepool(|| {
            let device = Device::system_default().context("no Metal device")?;
            ensure!(
                device.has_unified_memory(),
                "oominf-metal requires an Apple unified-memory GPU"
            );
            let options = metal::CompileOptions::new();
            options.set_fast_math_enabled(false);
            let library = device
                .new_library_with_source(include_str!("kernels.metal"), &options)
                .map_err(|e| anyhow::anyhow!("compile Metal kernels: {e}"))?;
            let mut kernels = HashMap::new();
            for name in [
                "gemm_bf16",
                "gemm_fp8",
                "gemm_nvfp4",
                "elementwise",
                "copy_columns",
                "rmsnorm",
                "l2norm",
                "gated_norm",
                "embedding",
                "causal_conv",
                "delta_step",
                "rope_half",
                "kv_append",
                "gqa_step",
                "scaled_add",
                "shared_gate",
            ] {
                let function = library
                    .get_function(name, None)
                    .map_err(|e| anyhow::anyhow!("Metal function {name}: {e}"))?;
                let pipeline = device
                    .new_compute_pipeline_state_with_function(&function)
                    .map_err(|e| anyhow::anyhow!("Metal pipeline {name}: {e}"))?;
                ensure!(
                    pipeline.thread_execution_width() == 32,
                    "Metal kernels require 32-thread SIMD groups"
                );
                kernels.insert(name, pipeline);
            }
            let queue = device.new_command_queue();
            let memory_limit = memory_limit.min(device.recommended_max_working_set_size());
            Ok(Self {
                device,
                queue,
                kernels,
                pending: Mutex::new(None),
                allocations: Mutex::new(BTreeMap::new()),
                memory_limit,
            })
        })
    }

    pub fn name(&self) -> &str {
        self.device.name()
    }

    fn allocate<T>(&self, len: usize) -> Result<Dev<T>> {
        let bytes = len
            .checked_mul(std::mem::size_of::<T>())
            .context("buffer size overflow")?;
        ensure!(
            bytes as u64 <= self.device.max_buffer_length(),
            "Metal buffer exceeds device limit"
        );
        let mut addresses = self.allocations.lock().unwrap();
        let used = self.device.current_allocated_size();
        ensure!(
            bytes.max(4) as u64 <= self.memory_limit.saturating_sub(used),
            "Metal allocation exceeds memory budget: requested {bytes} bytes, {used} allocated, limit {}",
            self.memory_limit
        );
        let buffer = self
            .device
            .new_buffer(bytes.max(4) as u64, MTLResourceOptions::StorageModeShared);
        let allocation = Arc::new(Allocation { buffer, bytes });
        addresses.retain(|_, a| a.strong_count() > 0);
        addresses.insert(allocation.buffer.gpu_address(), Arc::downgrade(&allocation));
        Ok(Dev {
            allocation,
            len,
            marker: PhantomData,
        })
    }

    fn upload<T: Copy>(&self, host: &[T]) -> Result<Dev<T>> {
        let buffer = self.allocate(host.len())?;
        // SAFETY: the new shared allocation holds host.len() elements of T and has no work in flight.
        unsafe {
            std::ptr::copy_nonoverlapping(
                host.as_ptr(),
                buffer.buffer().contents().cast(),
                host.len(),
            )
        };
        Ok(buffer)
    }

    fn finish(&self) -> Result<()> {
        objc::rc::autoreleasepool(|| {
            if let Some(command) = self.pending.lock().unwrap().take() {
                command.commit();
                command.wait_until_completed();
                ensure!(
                    command.status() == MTLCommandBufferStatus::Completed,
                    "Metal command failed: {:?}",
                    command.status()
                );
            }
            Ok(())
        })
    }

    fn launch(
        &self,
        name: &'static str,
        buffers: &[&metal::BufferRef],
        params: &[u32],
        threads: usize,
        group: usize,
    ) -> Result<()> {
        let bindings: Vec<_> = buffers.iter().map(|b| (*b, 0)).collect();
        self.launch_offsets(name, &bindings, params, threads, group)
    }

    fn launch_offsets(
        &self,
        name: &'static str,
        buffers: &[(&metal::BufferRef, u64)],
        params: &[u32],
        threads: usize,
        group: usize,
    ) -> Result<()> {
        if threads == 0 {
            return Ok(());
        }
        objc::rc::autoreleasepool(|| {
            let mut pending = self.pending.lock().unwrap();
            let command = pending.get_or_insert_with(|| self.queue.new_command_buffer().to_owned());
            let encoder = command.new_compute_command_encoder();
            let pipeline = &self.kernels[name];
            encoder.set_compute_pipeline_state(pipeline);
            for (index, (buffer, offset)) in buffers.iter().enumerate() {
                encoder.set_buffer(index as u64, Some(*buffer), *offset);
            }
            encoder.set_bytes(
                buffers.len() as u64,
                std::mem::size_of_val(params) as u64,
                params.as_ptr().cast(),
            );
            encoder.dispatch_threads(
                MTLSize::new(threads as u64, 1, 1),
                MTLSize::new(
                    group.min(pipeline.max_total_threads_per_threadgroup() as usize) as u64,
                    1,
                    1,
                ),
            );
            encoder.end_encoding();
            Ok(())
        })
    }
}

impl Drop for Gpu {
    fn drop(&mut self) {
        let _ = self.finish();
    }
}
