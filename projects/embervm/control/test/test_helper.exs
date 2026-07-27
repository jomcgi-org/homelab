# assert_receive_timeout raised from ExUnit's 100ms default. The suite runs on a
# shared BuildBuddy executor, so 100ms is a wall-clock bet on scheduler latency
# rather than a statement about the code: DrainCoordinatorTest's
# `assert_receive {:drained, :serving, "node-4"}` failed CI on a PR that touched
# no Elixir, and took the whole Test check red with it (issue #4078). Raising the
# default costs nothing on the passing path (assert_receive returns the moment the
# message lands) and only lengthens how long a genuinely failing assertion waits
# before reporting. refute_receive is unaffected: it reads refute_receive_timeout,
# which stays at 100ms, so no test that must wait out a full window got slower.
ExUnit.start(assert_receive_timeout: 2_000)

defmodule Embervm.TestProcess do
  @moduledoc """
  Test-teardown helpers shared by the control-plane suite.

  Defined here rather than in a support/ tree because `mix test` evaluates
  `test_helper.exs` before it compiles the test files, so every test module can
  call this without a build-graph change.
  """

  @doc """
  Stop `pid` gracefully, tolerating a process that has already exited.

  The pattern this replaces, `if Process.alive?(pid), do: GenServer.stop(pid)`,
  reads as if it handles the dead-process case, but check and stop are two steps:
  the process can exit in the gap, and then `GenServer.stop/1` raises `:noproc`
  INSIDE the teardown callback. ExUnit attributes that to the test whose body
  already passed, so the failure surfaces as an unrelated test failing with a
  stack ending in `ExUnit.OnExitHandler` and no assertion diff (issue #4078,
  observed on `Embervm.Placement.RetryTest` and `Embervm.BrickControllerTest`).

  Catching the exit closes the race instead of narrowing it. Graceful shutdown is
  preserved, so a `terminate/2` a test depends on still runs; only the
  already-dead case is swallowed. Works for `Agent` too, whose `stop/1` is
  `GenServer.stop/1`.
  """
  @spec stop_safely(pid() | GenServer.name()) :: :ok
  def stop_safely(pid) do
    GenServer.stop(pid)
  catch
    :exit, _ -> :ok
  end
end
