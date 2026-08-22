defmodule Embervm.Auth do
  @moduledoc """
  Bearer-token authentication for the submit API, porting the fc-invoke
  TokenReview pattern (auth/reviewer.go + auth/cache.go) to the BEAM.

  A token is authenticated by a Kubernetes `TokenReview` (via `Embervm.K8s`),
  then checked against an allow-list of ServiceAccount usernames from values.
  Successful reviewed identities are cached keyed by `sha256(token)` with a 60s
  TTL and collapsed by singleflight, because without caching a saturating submit rate
  becomes an equal rate of TokenReview calls against the API server. That is the
  exact failure the fc-invoke 5-QPS TokenReview incident (PR #3352) taught us to
  design out: there the client-side rate limiter silently capped throughput
  upstream of every span but auth. We have no client-go limiter here (Finch has
  no hidden QPS bucket), so the cache is purely to spare the API server; the cap
  it removes is real all the same.

  ## Why the network review runs off the GenServer

  This GenServer serializes its `handle_call/3`, which is exactly what makes the
  singleflight free: two concurrent misses for the same token cannot both start a
  review, because the second sees the first already recorded as in-flight. But
  the review itself is a network round-trip, so it MUST NOT run inside the call
  handler, or one slow miss would block every other token's cache hit behind it
  (re-creating the incident in a new place). Instead the handler spawns a short
  process to run the review and reply via `{:review_done, ...}`, so the GenServer
  stays responsive and only the waiters for that one token block.

  ## What is and is not cached

  Only ALLOWED principals are cached. A transient review error is never cached (a
  just-fixed token must re-check immediately). An authenticated-but-not-allowed
  token is not cached either: the allow-list is tiny and the denial is cheap to
  recompute, and not caching denials keeps the cache a pure success set (a
  rejected-token flood cannot grow it), matching the fc-invoke posture.
  """
  use GenServer
  require Logger
  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.Auth.Identity

  @default_ttl_ms 60_000
  @max_entries 4096

  # -- Client API ----------------------------------------------------------

  # :name defaults to __MODULE__ for the supervised singleton; tests that need
  # several isolated instances alive at once pass name: nil for an unnamed,
  # PID-addressed process (mirroring OpLog.SQLite / TaskStore).
  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Authenticates `token`, returning `{:ok, principal}` for an allow-listed
  ServiceAccount or `{:error, reason}` (`:unauthenticated`, `{:forbidden,
  username}` for an authenticated-but-not-allow-listed SA so the caller can audit
  WHO was rejected, or a transport reason). Blocks the caller only for a cache
  miss on this token; cache hits for other tokens are served concurrently.
  """
  @spec authenticate(GenServer.server(), String.t()) ::
          {:ok, String.t()} | {:error, term()}
  def authenticate(server \\ __MODULE__, token) do
    server
    |> authenticate_identity(token)
    |> identity_username()
  end

  @doc """
  Authenticates `token` like `authenticate/2`, but returns the full reviewed
  identity in successful and authenticated-but-forbidden results.
  """
  @spec authenticate_identity(GenServer.server(), String.t()) ::
          {:ok, Identity.t()} | {:error, {:forbidden, Identity.t()} | term()}
  def authenticate_identity(server \\ __MODULE__, token) do
    GenServer.call(server, {:authenticate, token}, :infinity)
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    state = %{
      cache: %{},
      inflight: %{},
      ttl_ms: Keyword.get(opts, :ttl_ms, @default_ttl_ms),
      clock: Keyword.get(opts, :clock, &default_clock/0),
      reviewer: Keyword.get(opts, :reviewer, &Embervm.K8s.review_token/1),
      allowed: MapSet.new(Keyword.get(opts, :allowed, [])),
      max_entries: Keyword.get(opts, :max_entries, @max_entries)
    }

    if MapSet.size(state.allowed) == 0 do
      Logger.warning(
        "embervm auth allow-list is EMPTY: every submit will be denied (fail-closed). " <>
          "Populate values.auth.allowedServiceAccounts."
      )
    end

    {:ok, state}
  end

  @impl true
  def handle_call({:authenticate, token}, from, state) do
    key = hash(token)
    now = state.clock.()

    case lookup(state, key, now) do
      {:ok, principal} ->
        {:reply, {:ok, principal}, state}

      :miss ->
        {:noreply, start_or_join_flight(state, key, token, from)}
    end
  end

  @impl true
  def handle_info({:review_done, key, token, result}, state) do
    {froms, inflight} = Map.pop(state.inflight, key, [])
    {reply, state} = resolve(%{state | inflight: inflight}, key, token, result)
    Enum.each(froms, fn from -> GenServer.reply(from, reply) end)
    {:noreply, state}
  end

  # -- singleflight core -----------------------------------------------------

  # Serial handle_call gives the singleflight lock for free: if `key` is already
  # in flight we just add this caller to its waiter list; otherwise we record it
  # in flight and spawn the network review, which reports back via handle_info.
  defp start_or_join_flight(state, key, token, from) do
    case Map.get(state.inflight, key) do
      nil ->
        server = self()

        spawn(fn ->
          # Catch EVERYTHING (raise/throw/exit): the reviewer runs a network
          # call, and if it dies without reporting back, the parked callers block
          # on their :infinity GenServer.call forever. A caught crash becomes a
          # non-cached error reply instead.
          result =
            try do
              # The TokenReview network round-trip in its own span (Task 13): the
              # 5-QPS incident taught us the guilty phase must never be
              # uninstrumented. Root span (the Auth GenServer holds no request
              # context); latency is what matters here.
              Tracer.with_span "embervm.auth" do
                state.reviewer.(token)
              end
            catch
              kind, reason -> {:error, {:review_crashed, kind, reason}}
            end

          send(server, {:review_done, key, token, result})
        end)

        %{state | inflight: Map.put(state.inflight, key, [from])}

      froms ->
        %{state | inflight: Map.put(state.inflight, key, [from | froms])}
    end
  end

  # Turns a raw reviewer result into the caller reply and (for an allow-listed
  # principal only) a fresh cache entry. Re-reads the clock so the TTL is dated
  # from when the review actually completed, not when it was enqueued.
  defp resolve(state, _key, _token, {:error, reason}) do
    {{:error, reason}, state}
  end

  defp resolve(state, key, _token, {:ok, %Identity{} = identity}) do
    if MapSet.member?(state.allowed, identity.username) do
      now = state.clock.()
      {{:ok, identity}, store(state, key, identity, now)}
    else
      # Surface the identity so callers can audit WHO was rejected. Still not
      # cached (a pure success set, per the moduledoc).
      {{:error, {:forbidden, identity}}, state}
    end
  end

  # -- cache -----------------------------------------------------------------

  defp lookup(state, key, now) do
    case Map.get(state.cache, key) do
      {principal, expires_at} when expires_at > now -> {:ok, principal}
      _ -> :miss
    end
  end

  # Store an allow-listed principal, keeping the cache bounded at max_entries.
  # Refreshing an existing key never grows the map; a genuinely new key at
  # capacity first sweeps expired entries, and only if that still leaves the map
  # full is the new entry dropped (correctness is unaffected: the token is simply
  # re-reviewed on its next request).
  defp store(state, key, principal, now) do
    entry = {principal, now + state.ttl_ms}

    cache =
      cond do
        Map.has_key?(state.cache, key) or map_size(state.cache) < state.max_entries ->
          Map.put(state.cache, key, entry)

        true ->
          swept = :maps.filter(fn _k, {_p, exp} -> exp > now end, state.cache)
          if map_size(swept) < state.max_entries, do: Map.put(swept, key, entry), else: swept
      end

    %{state | cache: cache}
  end

  defp hash(token) do
    :crypto.hash(:sha256, token) |> Base.encode16(case: :lower)
  end

  defp identity_username({:ok, %Identity{username: username}}), do: {:ok, username}

  defp identity_username({:error, {:forbidden, %Identity{username: username}}}),
    do: {:error, {:forbidden, username}}

  defp identity_username({:error, reason}), do: {:error, reason}

  defp default_clock, do: System.system_time(:millisecond)
end
