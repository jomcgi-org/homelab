defmodule Embervm.TriggerAdapter do
  @moduledoc """
  The seam that turns an EXTERNAL event source into ordinary task submits.

  A trigger adapter's whole job is to convert its own kind of event (a cron
  tick, later a NATS message, a webhook) into the exact same `submit` a client
  makes over `POST /v1/workloads/{name}/tasks`, so nothing downstream, the fair
  queue, the caps, the retry policy, the op-log, knows or cares that a task came
  from a trigger rather than an API caller. That uniformity is the point: cron is
  the only adapter in R0, and NATS (R-later) plugs in behind this same behaviour
  without touching the dispatcher.

  An adapter is a supervised process started with `start_link/1`. It receives a
  `:submit_fun` in its opts, the one function it is allowed to call to inject
  work, defaulting to a direct `Embervm.TaskStore.submit/1`; tests inject a
  recorder. Whatever principal an adapter stamps its submits with is the audit
  identity of that trigger source (cron uses `system:cron:<workload>`).

  Deliberately NOT part of the contract: any notion of exactly-once or replay.
  An adapter fires forward from now; events it missed while the control plane was
  down are SKIPPED, not backfilled (the cron adapter documents this explicitly).
  A source that needs delivery guarantees brings its own (NATS acks), but it
  still lands here as a plain submit.
  """

  @callback start_link(keyword()) :: GenServer.on_start()
end
