use super::{Dev, Gpu};
use anyhow::{Result, ensure};
use oominf_core::Memory;

impl Gpu {
    fn read<T: Copy>(&self, buffer: &Dev<T>) -> Result<Vec<T>> {
        self.finish()?;
        // SAFETY: completed GPU work, a typed buffer with exactly len initialized elements.
        Ok(unsafe {
            std::slice::from_raw_parts(buffer.buffer().contents().cast::<T>(), buffer.len)
        }
        .to_vec())
    }

    pub(crate) fn write<T: Copy>(&self, host: &[T], dst: &Dev<T>, offset: usize) -> Result<()> {
        ensure!(
            offset <= dst.len && host.len() <= dst.len - offset,
            "shared-buffer write out of bounds"
        );
        self.finish()?;
        // SAFETY: the checked range is in bounds, the GPU is idle, and host does not alias dst.
        unsafe {
            std::ptr::copy_nonoverlapping(
                host.as_ptr(),
                dst.buffer().contents().cast::<T>().add(offset),
                host.len(),
            )
        };
        Ok(())
    }

    fn zero<T>(&self, dst: &Dev<T>) -> Result<()> {
        self.finish()?;
        // SAFETY: this allocation is writable and no GPU commands still use it.
        unsafe {
            std::ptr::write_bytes(
                dst.buffer().contents().cast::<u8>(),
                0,
                dst.allocation.bytes,
            )
        };
        Ok(())
    }
}

impl Memory for Gpu {
    type F32 = Dev<f32>;
    type Bf16 = Dev<u16>;
    type Bytes = Dev<u8>;
    type I32 = Dev<i32>;
    type U64 = Dev<u64>;
    fn uninit(&self, n: usize) -> Result<Self::F32> {
        self.allocate(n)
    }
    fn zeros(&self, n: usize) -> Result<Self::F32> {
        let b = self.allocate(n)?;
        self.zero(&b)?;
        Ok(b)
    }
    fn fill_zero(&self, buf: &mut Self::F32) -> Result<()> {
        self.zero(buf)
    }
    fn upload_f32(&self, host: &[f32]) -> Result<Self::F32> {
        self.upload(host)
    }
    fn upload_into(&self, host: &[f32], dst: &mut Self::F32) -> Result<()> {
        ensure!(host.len() == dst.len, "upload length mismatch");
        self.write(host, dst, 0)
    }
    fn download_f32(&self, buf: &Self::F32) -> Result<Vec<f32>> {
        self.read(buf)
    }
    fn write_f32_at(&self, host: &[f32], dst: &mut Self::F32, offset: usize) -> Result<()> {
        self.write(host, dst, offset)
    }
    fn upload_bf16(&self, host: &[u16]) -> Result<Self::Bf16> {
        self.upload(host)
    }
    fn uninit_bf16(&self, n: usize) -> Result<Self::Bf16> {
        self.allocate(n)
    }
    fn upload_bytes(&self, host: &[u8]) -> Result<Self::Bytes> {
        self.upload(host)
    }
    fn uninit_bytes(&self, n: usize) -> Result<Self::Bytes> {
        self.allocate(n)
    }
    fn zeros_bytes(&self, n: usize) -> Result<Self::Bytes> {
        let b = self.allocate(n)?;
        self.zero(&b)?;
        Ok(b)
    }
    fn download_bytes(&self, buf: &Self::Bytes) -> Result<Vec<u8>> {
        self.read(buf)
    }
    fn copy_bytes(
        &self,
        src: &Self::Bytes,
        src_off: usize,
        dst: &mut Self::Bytes,
        dst_off: usize,
        n: usize,
    ) -> Result<()> {
        ensure!(
            src_off <= src.len
                && n <= src.len - src_off
                && dst_off <= dst.len
                && n <= dst.len - dst_off,
            "byte copy out of bounds"
        );
        self.finish()?;
        // SAFETY: checked in-bounds shared allocations; GPU commands have completed.
        unsafe {
            std::ptr::copy(
                src.buffer().contents().cast::<u8>().add(src_off),
                dst.buffer().contents().cast::<u8>().add(dst_off),
                n,
            )
        };
        Ok(())
    }
    fn upload_i32(&self, host: &[i32]) -> Result<Self::I32> {
        self.upload(host)
    }
    fn download_i32(&self, buf: &Self::I32) -> Result<Vec<i32>> {
        self.read(buf)
    }
    fn write_i32(&self, host: &[i32], dst: &mut Self::I32) -> Result<()> {
        if dst.len < host.len() {
            *dst = self.allocate(host.len())?;
        }
        self.write(host, dst, 0)
    }
    fn zeros_u64(&self, n: usize) -> Result<Self::U64> {
        let b = self.allocate(n)?;
        self.zero(&b)?;
        Ok(b)
    }
    fn write_u64(&self, host: &[u64], dst: &mut Self::U64) -> Result<()> {
        if dst.len < host.len() {
            *dst = self.allocate(host.len())?;
        }
        self.write(host, dst, 0)
    }
    fn bytes_addr(&self, buf: &Self::Bytes) -> u64 {
        buf.buffer().gpu_address()
    }
    fn sync(&self) -> Result<()> {
        self.finish()
    }
    fn mem_info(&self) -> Result<(usize, usize)> {
        let host = oominf_tiers::resources::HostMemory::probe()?;
        let recommended = self.memory_limit as usize;
        let used = self.device.current_allocated_size() as usize;
        Ok((
            (host.usable() as usize).min(recommended.saturating_sub(used)),
            recommended,
        ))
    }
}
