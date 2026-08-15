export function workspaceRecoveryMessage(turnOrUsage) {
  const usage = turnOrUsage?.usage ?? turnOrUsage;
  const degraded = usage?.workspace_recovery?.degraded;

  if (degraded == null) return null;

  if (degraded === "restore_denied") {
    return "workspaceRecoveryRestoreDenied";
  }
  if (degraded === "restore_fallback") {
    return "workspaceRecoveryRestoreFallback";
  }
  return typeof degraded === "string" ? "workspaceRecoveryUnknown" : null;
}

export function workspaceRecoveryTitle(turnOrUsage) {
  const usage = turnOrUsage?.usage ?? turnOrUsage;
  const degraded = usage?.workspace_recovery?.degraded;

  if (degraded === "restore_denied") {
    return "Workspace restore create was denied or timed out";
  }
  if (degraded === "restore_fallback") {
    return "Workspace restore completed without restoring the session";
  }
  if (typeof degraded === "string") {
    return "Workspace degradation cause unknown";
  }
  return null;
}
