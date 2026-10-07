//! Fresh checks of the machine's memory at start-up: what the kernel says is
//! available and what the process's control group (container) still allows.
//!
//! These run on every start and are never cached: a container's limit, other
//! processes and the page cache change between runs.

use std::path::{Path, PathBuf};

#[cfg(any(not(target_os = "macos"), test))]
use anyhow::Context;
use anyhow::Result;

const GIB: f64 = (1u64 << 30) as f64;

#[cfg(target_os = "macos")]
unsafe extern "C" {
    fn mach_port_deallocate(
        task: libc::mach_port_t,
        name: libc::mach_port_t,
    ) -> libc::kern_return_t;
}

/// A control group's memory limit and use, in bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CgroupMemory {
    /// cgroup v1 or v2.
    pub version: u8,
    /// The group (or ancestor) whose limit binds.
    pub path: PathBuf,
    /// The tightest limit (`memory.max` or `memory.high` on v2,
    /// `memory.limit_in_bytes` on v1).
    pub limit: u64,
    /// What the group uses now, page cache included.
    pub usage: u64,
    /// Page cache in that use the kernel can drop under pressure (inactive file
    /// pages).
    pub reclaimable: u64,
}

impl CgroupMemory {
    /// Bytes the group may still take: its limit less what it uses that cannot be
    /// reclaimed.
    pub fn headroom(&self) -> u64 {
        self.limit
            .saturating_sub(self.usage.saturating_sub(self.reclaimable))
    }
}

/// Host memory available to this process.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostMemory {
    /// `MemTotal` from `/proc/meminfo`.
    pub total: u64,
    /// `MemAvailable` from `/proc/meminfo`.
    pub available: u64,
    /// The binding control group limit, if any.
    pub cgroup: Option<CgroupMemory>,
}

impl HostMemory {
    /// Reads `/proc/meminfo` and the process's control group.
    #[cfg(not(target_os = "macos"))]
    pub fn probe() -> Result<Self> {
        let info = std::fs::read_to_string("/proc/meminfo").context("read /proc/meminfo")?;
        let (total, available) = parse_meminfo(&info)?;
        let own = std::fs::read_to_string("/proc/self/cgroup").unwrap_or_default();
        Ok(HostMemory {
            total,
            available,
            cgroup: cgroup_memory(Path::new("/sys/fs/cgroup"), &own),
        })
    }

    /// Reads physical memory and reclaimable pages from the macOS kernel.
    #[cfg(target_os = "macos")]
    #[allow(deprecated)]
    pub fn probe() -> Result<Self> {
        use anyhow::ensure;
        let mut total = 0u64;
        let mut size = std::mem::size_of_val(&total);
        // SAFETY: sysctl receives a valid u64 output and its size; no write value.
        ensure!(
            unsafe {
                libc::sysctlbyname(
                    c"hw.memsize".as_ptr(),
                    (&mut total as *mut u64).cast(),
                    &mut size,
                    std::ptr::null_mut(),
                    0,
                )
            } == 0,
            "hw.memsize: {}",
            std::io::Error::last_os_error()
        );
        let mut stats = std::mem::MaybeUninit::<libc::vm_statistics64>::zeroed();
        let mut count = libc::HOST_VM_INFO64_COUNT;
        // SAFETY: a host send right is acquired, queried into a sufficiently sized
        // vm_statistics64, then released. The output is used only on success.
        let result = unsafe {
            let host = libc::mach_host_self();
            let result = libc::host_statistics64(
                host,
                libc::HOST_VM_INFO64,
                stats.as_mut_ptr().cast(),
                &mut count,
            );
            mach_port_deallocate(libc::mach_task_self(), host);
            result
        };
        ensure!(
            result == libc::KERN_SUCCESS,
            "host_statistics64: Mach error {result}"
        );
        // SAFETY: the successful call initialized this output.
        let stats = unsafe { stats.assume_init() };
        // Inactive pages may be reclaimed; wired and compressed memory are excluded.
        // Darwin already includes speculative pages in free_count.
        let pages = u64::from(stats.free_count) + u64::from(stats.inactive_count);
        // SAFETY: sysconf has no pointer arguments.
        let page_size = unsafe { libc::sysconf(libc::_SC_PAGESIZE) };
        ensure!(page_size > 0, "could not read the host page size");
        Ok(HostMemory {
            total,
            available: (pages * page_size as u64).min(total),
            cgroup: None,
        })
    }

    /// Bytes this process may still take: the smaller of `MemAvailable` and the
    /// control group's headroom.
    pub fn usable(&self) -> u64 {
        match &self.cgroup {
            Some(c) => self.available.min(c.headroom()),
            None => self.available,
        }
    }

    /// One line for logs: where the figure comes from.
    pub fn describe(&self) -> String {
        let mut s = format!(
            "host memory: {:.1} GiB available of {:.1} GiB",
            self.available as f64 / GIB,
            self.total as f64 / GIB
        );
        if let Some(c) = &self.cgroup {
            s += &format!(
                "; cgroup v{} {} limit {:.1} GiB, using {:.1} GiB ({:.1} GiB reclaimable), headroom {:.1} GiB",
                c.version,
                c.path.display(),
                c.limit as f64 / GIB,
                c.usage as f64 / GIB,
                c.reclaimable as f64 / GIB,
                c.headroom() as f64 / GIB
            );
        }
        s + &format!("; usable {:.1} GiB", self.usable() as f64 / GIB)
    }
}

/// `(MemTotal, MemAvailable)` in bytes.
#[cfg(any(not(target_os = "macos"), test))]
fn parse_meminfo(info: &str) -> Result<(u64, u64)> {
    let field = |name: &str| -> Option<u64> {
        info.lines()
            .find_map(|l| l.strip_prefix(name)?.strip_prefix(':'))
            .and_then(|v| v.trim().trim_end_matches("kB").trim().parse::<u64>().ok())
            .map(|kib| kib << 10)
    };
    Ok((
        field("MemTotal").context("MemTotal missing from /proc/meminfo")?,
        field("MemAvailable").context("MemAvailable missing from /proc/meminfo")?,
    ))
}

fn read_u64(path: &Path) -> Option<u64> {
    std::fs::read_to_string(path).ok()?.trim().parse().ok()
}

/// A field of a `memory.stat` file.
fn stat_field(dir: &Path, name: &str) -> u64 {
    std::fs::read_to_string(dir.join("memory.stat"))
        .ok()
        .and_then(|s| {
            s.lines().find_map(|l| {
                let (k, v) = l.split_once(' ')?;
                (k == name).then(|| v.trim().parse().ok())?
            })
        })
        .unwrap_or(0)
}

/// The binding memory limit of the control group described by `own` (the
/// contents of `/proc/self/cgroup`) under the cgroup mount `root`: the group or
/// ancestor with the least headroom. `None` when no group sets a limit.
pub fn cgroup_memory(root: &Path, own: &str) -> Option<CgroupMemory> {
    // cgroup v2: one line `0::/path`.
    if let Some(rel) = own.lines().find_map(|l| l.strip_prefix("0::")) {
        let mut best: Option<CgroupMemory> = None;
        let mut dir = root.join(rel.trim_start_matches('/'));
        loop {
            let limit = ["memory.max", "memory.high"]
                .iter()
                .filter_map(|f| read_u64(&dir.join(f)))
                .min();
            if let (Some(limit), Some(usage)) = (limit, read_u64(&dir.join("memory.current"))) {
                let c = CgroupMemory {
                    version: 2,
                    path: dir.strip_prefix(root).unwrap_or(&dir).to_owned(),
                    limit,
                    usage,
                    reclaimable: stat_field(&dir, "inactive_file"),
                };
                if best.as_ref().is_none_or(|b| c.headroom() < b.headroom()) {
                    best = Some(c);
                }
            }
            if dir == root || !dir.pop() || !dir.starts_with(root) {
                break;
            }
        }
        if best.is_some() {
            return best;
        }
    }
    // cgroup v1: a `N:memory:/path` line, the controller mounted at root/memory.
    let rel = own.lines().find_map(|l| {
        let mut f = l.splitn(3, ':');
        let (_, ctrl, path) = (f.next()?, f.next()?, f.next()?);
        ctrl.split(',').any(|c| c == "memory").then_some(path)
    })?;
    let base = root.join("memory");
    let mut dir = base.join(rel.trim_start_matches('/'));
    // Inside a container the group's own path is often not mounted: use the root.
    if !dir.join("memory.limit_in_bytes").exists() {
        dir = base;
    }
    let limit = read_u64(&dir.join("memory.limit_in_bytes"))?;
    // "No limit" is a page-rounded huge number.
    if limit >= 1 << 60 {
        return None;
    }
    Some(CgroupMemory {
        version: 1,
        path: dir.strip_prefix(root).unwrap_or(&dir).to_owned(),
        limit,
        usage: read_u64(&dir.join("memory.usage_in_bytes"))?,
        reclaimable: stat_field(&dir, "total_inactive_file"),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(dir: &Path, name: &str, text: &str) {
        std::fs::create_dir_all(dir).unwrap();
        std::fs::write(dir.join(name), text).unwrap();
    }

    #[test]
    fn meminfo_fields_in_bytes() {
        let info = "MemTotal:       64000000 kB\nMemFree: 1 kB\nMemAvailable:   50000000 kB\n";
        assert_eq!(
            parse_meminfo(info).unwrap(),
            (64_000_000 << 10, 50_000_000 << 10)
        );
        assert!(parse_meminfo("MemTotal: 1 kB\n").is_err());
    }

    #[test]
    fn v2_takes_the_tightest_ancestor_and_counts_reclaimable_cache() {
        let root = tempfile::tempdir().unwrap();
        let r = root.path();
        // The parent limits to 32 GiB and uses 20 GiB; the leaf is unlimited.
        write(&r.join("a"), "memory.max", &format!("{}\n", 32u64 << 30));
        write(
            &r.join("a"),
            "memory.current",
            &format!("{}\n", 20u64 << 30),
        );
        write(
            &r.join("a"),
            "memory.stat",
            &format!("anon 1\ninactive_file {}\n", 4u64 << 30),
        );
        write(&r.join("a/b"), "memory.max", "max\n");
        write(&r.join("a/b"), "memory.current", "1000\n");
        let c = cgroup_memory(r, "0::/a/b\n").unwrap();
        assert_eq!(c.version, 2);
        assert_eq!(c.path, Path::new("a"));
        assert_eq!(c.headroom(), (32u64 << 30) - (16u64 << 30));
        // memory.high below memory.max binds.
        write(&r.join("a/b"), "memory.high", &format!("{}\n", 8u64 << 30));
        let c = cgroup_memory(r, "0::/a/b\n").unwrap();
        assert_eq!((c.path.as_path(), c.limit), (Path::new("a/b"), 8u64 << 30));
    }

    #[test]
    fn unlimited_or_missing_groups_give_none() {
        let root = tempfile::tempdir().unwrap();
        write(&root.path().join("x"), "memory.max", "max\n");
        write(&root.path().join("x"), "memory.current", "5\n");
        assert_eq!(cgroup_memory(root.path(), "0::/x\n"), None);
        assert_eq!(cgroup_memory(root.path(), ""), None);
    }

    #[test]
    fn v1_limit_falls_back_to_the_mount_root() {
        let root = tempfile::tempdir().unwrap();
        let m = root.path().join("memory");
        write(&m, "memory.limit_in_bytes", &format!("{}\n", 16u64 << 30));
        write(&m, "memory.usage_in_bytes", &format!("{}\n", 2u64 << 30));
        write(&m, "memory.stat", "total_inactive_file 0\n");
        let c = cgroup_memory(root.path(), "4:memory:/docker/abc\n1:cpu:/x\n").unwrap();
        assert_eq!((c.version, c.headroom()), (1, 14u64 << 30));
        // The unlimited sentinel.
        write(&m, "memory.limit_in_bytes", "9223372036854771712\n");
        assert_eq!(cgroup_memory(root.path(), "4:memory:/\n"), None);
    }
}
