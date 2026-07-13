defmodule Embervm.Router do
  @moduledoc """
  The control-plane HTTP router, served by Bandit (see `Embervm.Application`).

  R0 skeleton: this replaces the dependency-free `:gen_tcp` health endpoint with
  a real `Plug.Router` so the submit API (Task 8) has a router to grow into. In
  this PR it answers only `GET /healthz` with `200 ok`; the `/v1/...` task routes
  land in Task 8 as additional clauses here (or a forwarded sub-router). The
  process contract is unchanged from the old `Embervm.Health`: a supervised child
  listening on the configured port, answering the same health path the chart's
  readiness/liveness probes hit.

  Bandit + Plug are the first HTTP dependencies in the control-plane closure; this
  router existing and serving in-cluster is the deploy-time proof that the whole
  Bandit/Plug/Finch/Mint hex closure builds and boots (the Task 8 de-risk).
  """
  use Plug.Router

  plug(:match)
  plug(:dispatch)

  get "/healthz" do
    conn
    |> put_resp_content_type("text/plain")
    |> send_resp(200, "ok")
  end

  match _ do
    send_resp(conn, 404, "")
  end
end
