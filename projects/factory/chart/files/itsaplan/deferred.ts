export type DeferredStart = {
  issueId: number;
  projectId: number;
  startDate: string;
  delegateUserId: string;
  actorUserId: string;
  key: string;
};

type Receipt = { actorUserId: string; delegateUserId: string; runId: number };
type API = {
  transaction<Result>(fn: () => Promise<Result>): Promise<Result>;
  lockIssue(id: number): Promise<void>;
  receipt(request: DeferredStart): Promise<Receipt | undefined>;
  hasRun(id: number, request: DeferredStart): Promise<boolean>;
  eligible(
    request: DeferredStart,
  ): Promise<{ todoId: number; agentId: number }>;
  comment(input: {
    issueId: number;
    actorUserId: string;
    body: string;
  }): Promise<unknown>;
  update(
    id: number,
    patch: { columnId?: number; delegateUserId?: string },
    actor: string,
  ): Promise<unknown>;
  runs(issueId: number, agentId: number): Promise<number[]>;
  saveReceipt(request: DeferredStart, runId: number): Promise<void>;
  conflict(message: string): Error;
};

// The receipt and every upstream effect share one transaction. In particular,
// updateIssue's delegate write and enqueueDelegateRun must both commit or neither
// may commit. Never infer a successful start from the delegate alone.
export function startDeferred(api: API, request: DeferredStart) {
  return api.transaction(async () => {
    await api.lockIssue(request.issueId);
    const receipt = await api.receipt(request);
    if (receipt) {
      if (
        receipt.actorUserId !== request.actorUserId ||
        receipt.delegateUserId !== request.delegateUserId
      ) {
        throw api.conflict("Deferred start key belongs to another request");
      }
      if (!(await api.hasRun(receipt.runId, request))) {
        throw api.conflict("Deferred start run was removed");
      }
      return { key: request.key, runId: receipt.runId };
    }

    const { todoId, agentId } = await api.eligible(request);
    await api.comment({
      issueId: request.issueId,
      actorUserId: request.actorUserId,
      body: `Starting deferred work scheduled for ${request.startDate} (UTC).\n\nFactory start: ${request.key}.`,
    });
    await api.update(
      request.issueId,
      { columnId: todoId },
      request.actorUserId,
    );
    const previousRuns = await api.runs(request.issueId, agentId);
    await api.update(
      request.issueId,
      { delegateUserId: request.delegateUserId },
      request.actorUserId,
    );
    const added = (await api.runs(request.issueId, agentId)).filter(
      (id) => !previousRuns.includes(id),
    );
    if (added.length !== 1) {
      // Throw inside the transaction, including when upstream quietly skips its
      // enqueue. The next delivery can safely repeat the entire start.
      throw new Error("Deferred delegation did not create exactly one run");
    }
    await api.saveReceipt(request, added[0]);
    return { key: request.key, runId: added[0] };
  });
}
