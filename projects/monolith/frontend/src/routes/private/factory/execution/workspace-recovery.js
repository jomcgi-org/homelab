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
