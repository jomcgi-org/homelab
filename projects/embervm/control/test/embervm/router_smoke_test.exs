defmodule Embervm.RouterSmokeTest do
  @moduledoc """
  The HTTP-closure de-risk (Task 8 prerequisite): prove Bandit + Plug + Finch +
  Mint compile, link, boot, and interoperate on the RBE executor with ZERO
  network egress. `mix test` starts the :embervm application, which boots
  `Embervm.Router` behind Bandit on the default port and the `Embervm.Finch`
  pool; each test drives one real HTTP round-trip THROUGH Finch/Mint AT Bandit
  over the loopback interface. If any node of the closure (hpax, thousand_island,
  nimble_pool, ...) is missing or mislinked, this fails at boot or connect, which
  is exactly the closure-completeness risk this PR exists to retire.

  `async: false`: the app is a single shared instance (one Bandit listener bound
  to the port, one Finch pool), so these run serially against it rather than
  starting collidng second instances.
  """
  use ExUnit.Case, async: false

  # The app boots Bandit on EMBERVM_HTTP_PORT or 8080 by default; tests run with
  # neither set, so the listener is on 8080.
  @port 8080

  test "Bandit serves GET /healthz and Finch fetches it over loopback" do
    {:ok, resp} =
      Finch.build(:get, "http://127.0.0.1:#{@port}/healthz")
      |> Finch.request(Embervm.Finch)

    assert resp.status == 200
    assert resp.body == "ok"
  end

  test "unknown path returns 404 through the same Bandit/Finch path" do
    {:ok, resp} =
      Finch.build(:get, "http://127.0.0.1:#{@port}/nope")
      |> Finch.request(Embervm.Finch)

    assert resp.status == 404
  end
end
