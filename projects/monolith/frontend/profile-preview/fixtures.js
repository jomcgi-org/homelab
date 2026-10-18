// Invented, deterministic snapshots. No backend or production data is read.
export const stats = {
  cluster: {
    nodes: 3,
    pods: 42,
    cpu_used_cores: 8,
    cpu_capacity_cores: 36,
    memory_used_gb: 24,
    memory_capacity_gb: 48,
    argocd_apps: 28,
  },
  knowledge: { facts: 1309 },
  deploy: { latest_commit_sha: "fixture", deployed_at: "2026-10-18T11:50:00Z" },
};
