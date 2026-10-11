defmodule Embervm.SessionInvokeChannelTest do
  @moduledoc """
  Which invoke failures tear down the SHARED per-brick NodeChannel (D-R2.7.2).
  Every session on a brick multiplexes its invokes on one channel, and grpc
  1.0.3's disconnect never fails the streams pending on it: they block until
  the 12h invoke watchdog. So only a dead transport may drop it; a server
  answer, including a suspect SessionAssignResponse, rode a healthy channel.
  """
  use ExUnit.Case, async: true

  alias Embervm.Session

  @healthy %{health: :healthy, draining: false, tombstoned: false}

  test "a suspect response fails the session but keeps the shared channel" do
    assert %{reason: :suspect, invalidate_channel: false} = Session.classify_invoke_error(:suspect, @healthy)
  end

  test "server statuses keep the channel, dead transports drop it" do
    dead = %GRPC.RPCError{status: 2, message: "error occurred while receiving data: the connection is closed"}

    for status <- [4, 8, 9, 14] do
      assert %{invalidate_channel: false} = Session.classify_invoke_error(%GRPC.RPCError{status: status}, @healthy)
    end

    assert %{invalidate_channel: true} = Session.classify_invoke_error(dead, @healthy)
    assert %{invalidate_channel: true} = Session.classify_invoke_error(:closed, @healthy)
  end
end
