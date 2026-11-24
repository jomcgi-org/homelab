#!/usr/bin/python3 -I
"""Passive isolation audit of the live gvisor-fork worker (README step 9: X1 to X3).

Run as root on the worker pod's node. It only reads: /proc, kcmp and sysfs. It
does not attach to any process (no ptrace), signal or write, so it is safe against
the children that engine-gate owns.

    python3 -I collect.py <outdir> label=pid [label=pid ...]

Each pid is the ROOT of a process tree (a child sentry, the template stub, forkd,
the worker). List the children first: a pid belongs to the first label whose tree
holds it. The children are the direct children of the seed sandbox (`pgrep -P`),
and the seed sandbox hangs below the template stub, below forkd, below the worker.

Collects per process: status (caps, seccomp, NoNewPrivs, threads), namespaces,
cgroup, fds, MAP_SHARED mappings, VmFlags (the KSM `mg` flag). Then, across all pairs of
processes with different labels:
  - every pair of fds that refer to the same open file description (kcmp
    KCMP_FILE), classified rw / ro,
  - every MAP_SHARED mapping with the same (device, inode), except per-process
    anonymous inodes (kvm-vcpu).
It prints a summary, including the count of kcmp errors, and writes <outdir>/iso4.json.
x86_64 only (the kcmp syscall number). The seccomp filter bytes are
not dumped here: that needs PTRACE_SECCOMP_GET_FILTER, which stops a thread. The
byte comparison (X1) ran off-cluster on node-1 (design note, results7).
"""

import os, sys, json, re, ctypes

libc = ctypes.CDLL(None, use_errno=True)


def rd(p, mode="r"):
    try:
        with open(p, mode) as f:
            return f.read()
    except OSError:
        return None


def rl(p):
    try:
        return os.readlink(p)
    except OSError as e:
        return "ERR:%s" % e.strerror


def procs():
    t = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        st = rd("/proc/%s/stat" % d)
        if not st:
            continue
        t[int(d)] = int(st[st.rfind(")") + 2 :].split()[1])
    return t


def desc(root, tab):
    kids = {}
    for p, pp in tab.items():
        kids.setdefault(pp, []).append(p)
    seen, st = set(), [root]
    while st:
        p = st.pop()
        if p in seen:
            continue
        seen.add(p)
        st.extend(kids.get(p, []))
    return seen


FIELDS = [
    "Name",
    "Pid",
    "PPid",
    "Uid",
    "Gid",
    "CapInh",
    "CapPrm",
    "CapEff",
    "CapBnd",
    "CapAmb",
    "NoNewPrivs",
    "Seccomp",
    "Seccomp_filters",
    "Threads",
    "NSpid",
]


def status(path):
    d = {}
    for line in (rd(path) or "").splitlines():
        k, _, v = line.partition(":")
        if k in FIELDS:
            d[k] = v.strip()
    return d


def proc_info(pid):
    base = "/proc/%d" % pid
    info = {"pid": pid, "status": status(base + "/status"), "exe": rl(base + "/exe")}
    info["ns"] = {
        n: rl("%s/ns/%s" % (base, n))
        for n in ("cgroup", "ipc", "mnt", "net", "pid", "time", "user", "uts")
    }
    info["cgroup"] = (rd(base + "/cgroup") or "").strip()
    fds = {}
    try:
        for fd in os.listdir(base + "/fd"):
            p = "%s/fd/%s" % (base, fd)
            flags = ""
            for l in (rd("%s/fdinfo/%s" % (base, fd)) or "").splitlines():
                if l.startswith("flags:"):
                    flags = l.split()[1]
            fl = int(flags, 8) if flags else 0
            fds[fd] = {
                "link": rl(p),
                "acc": {0: "ro", 1: "wo", 2: "rw"}.get(fl & 3, "?"),
                "flags": flags,
            }
    except OSError:
        pass
    info["fds"] = fds
    shared, vmflags_mg = [], 0
    cur = None
    for line in (rd(base + "/smaps") or "").splitlines():
        m = re.match(
            r"^([0-9a-f]+)-([0-9a-f]+) (\S{4}) (\S+) (\S+:\S+) (\d+)\s*(.*)$", line
        )
        if m:
            a, b, perms, off, dev, ino, path = m.groups()
            cur = None
            if perms[3] == "s":
                cur = {
                    "range": "%s-%s" % (a, b),
                    "perms": perms,
                    "dev": dev,
                    "ino": ino,
                    "path": path,
                    "kb": (int(b, 16) - int(a, 16)) // 1024,
                }
                shared.append(cur)
        elif line.startswith("VmFlags:") and " mg" in line:
            vmflags_mg += 1
    info["shared_maps"] = shared
    info["ksm_mergeable_vmas"] = vmflags_mg
    th = {}
    try:
        for tid in os.listdir(base + "/task"):
            s = status("%s/task/%s/status" % (base, tid))
            key = "Seccomp=%s nfilters=%s caps=%s" % (
                s.get("Seccomp"),
                s.get("Seccomp_filters"),
                s.get("CapEff"),
            )
            th.setdefault(key, []).append(int(tid))
    except OSError:
        pass
    info["threads"] = {k: len(v) for k, v in th.items()}
    return info


def kcmp_file(pa, pb, fa, fb):
    # KCMP_FILE is 0. kcmp returns 0 for the same open file description and -1 on
    # an error (a process or fd that went away, or no permission). The syscall
    # number 312 is x86_64.
    return libc.syscall(312, pa, pb, 0, fa, fb)


outdir = sys.argv[1]
os.makedirs(outdir, exist_ok=True)
specs = [a.split("=") for a in sys.argv[2:]]
tab = procs()
groups, claimed = (
    {},
    set(),
)  # a pid belongs to the first label whose tree contains it: list children before the template
for lab, pid in specs:
    groups[lab] = sorted(desc(int(pid), tab) - claimed)
    claimed |= set(groups[lab])
res = {
    "groups": {},
    "yama_ptrace_scope": (rd("/proc/sys/kernel/yama/ptrace_scope") or "").strip(),
    "ksm_run": (rd("/sys/kernel/mm/ksm/run") or "").strip(),
}
allp = []
for lab, pids in groups.items():
    res["groups"][lab] = [proc_info(p) for p in pids]
    allp += [(lab, p) for p in res["groups"][lab]]
shared_fds = []
kcmp_errors = 0
for i in range(len(allp)):
    for j in range(i + 1, len(allp)):
        la, pa = allp[i]
        lb, pb = allp[j]
        if la == lb:
            continue
        for fa, da in pa["fds"].items():
            for fb, db in pb["fds"].items():
                if da["acc"] == "?" or db["acc"] == "?":
                    continue
                r = kcmp_file(pa["pid"], pb["pid"], int(fa), int(fb))
                if r < 0:
                    kcmp_errors += 1
                if r == 0:
                    shared_fds.append(
                        {
                            "a": "%s/%d fd%s" % (la, pa["pid"], fa),
                            "b": "%s/%d fd%s" % (lb, pb["pid"], fb),
                            "link": da["link"],
                            "acc_a": da["acc"],
                            "acc_b": db["acc"],
                            "rw": "w" in (da["acc"] + db["acc"])
                            or "rw" in (da["acc"], db["acc"]),
                        }
                    )
res["shared_open_file_descriptions"] = shared_fds
res["kcmp_errors"] = kcmp_errors
shared_maps = []
for i in range(len(allp)):
    for j in range(i + 1, len(allp)):
        la, pa = allp[i]
        lb, pb = allp[j]
        if la == lb:
            continue
        for ma in pa["shared_maps"]:
            if ma["path"].startswith("anon_inode"):
                continue  # per-fd kernel objects (kvm-vcpu), compared via the fd
            for mb in pb["shared_maps"]:
                if (ma["dev"], ma["ino"], ma["path"]) == (
                    mb["dev"],
                    mb["ino"],
                    mb["path"],
                ):
                    shared_maps.append(
                        {
                            "a": "%s/%d" % (la, pa["pid"]),
                            "b": "%s/%d" % (lb, pb["pid"]),
                            "path": ma["path"],
                            "dev": ma["dev"],
                            "ino": ma["ino"],
                            "perms_a": ma["perms"],
                            "perms_b": mb["perms"],
                            "kb": ma["kb"],
                        }
                    )
res["shared_mappings"] = shared_maps
json.dump(res, open(outdir + "/iso4.json", "w"), indent=1)


# human summary
def summarize(pairs, keyf):
    c = {}
    for p in pairs:
        k = keyf(p)
        c[k] = c.get(k, 0) + 1
    return c


print("KSM run=%s  yama_ptrace_scope=%s" % (res["ksm_run"], res["yama_ptrace_scope"]))
for lab, procs_ in res["groups"].items():
    for p in procs_:
        s = p["status"]
        print(
            "%-8s pid=%-8d %-14s CapPrm=%s CapEff=%s CapBnd=%s CapAmb=%s nnp=%s seccomp=%s/%s threads=%s fds=%d mg_vmas=%d"
            % (
                lab,
                p["pid"],
                s.get("Name"),
                s.get("CapPrm"),
                s.get("CapEff"),
                s.get("CapBnd"),
                s.get("CapAmb"),
                s.get("NoNewPrivs"),
                s.get("Seccomp"),
                s.get("Seccomp_filters"),
                s.get("Threads"),
                len(p["fds"]),
                p["ksm_mergeable_vmas"],
            )
        )
        print("         thread seccomp:", p.get("threads"))
print(
    "kcmp errors (a pair that could not be compared; a child that exits mid-run adds some):",
    kcmp_errors,
)
print(
    "SHARED open file descriptions between different labels:",
    len(shared_fds),
    " (rw:",
    sum(1 for x in shared_fds if x["rw"]),
    ")",
)
for k, v in sorted(
    summarize(shared_fds, lambda x: (x["link"][:70], "rw" if x["rw"] else "ro")).items()
):
    print("   %3d x %s [%s]" % (v, k[0], k[1]))
print(
    "SHARED MAP_SHARED mappings (same dev:ino) between different labels:",
    len(shared_maps),
)
for k, v in sorted(
    summarize(shared_maps, lambda x: (x["path"][:60], x["perms_a"], x["kb"])).items()
):
    print("   %3d x %s %s %dkB" % (v, k[0], k[1], k[2]))
