defmodule Embervm.KeyService do
  @moduledoc """
  Custodian for platform-managed, principal-scoped key-encryption keys.

  KEKs are derived on demand from the configured root and are never stored.
  The only durable per-principal state is the current epoch and the minimum
  accepted epoch. Epoch 0 is a valid first epoch for a principal with no row.
  Mutations append their op-log fact before updating the ETS projection, so a
  failed append is never observable as live state.

  With no root configured the server still starts and loads epoch facts, but
  all cryptographic operations return `{:error, :no_root}`.
  """

  use GenServer

  alias Embervm.KeyService.Envelope
  alias Embervm.OpLog.Op

  @salt "embervm-kek-v1"
  @kek_size 32
  @nonce_size 12
  @tag_size 16
  @max_epoch 18_446_744_073_709_551_615

  defmodule State do
    @moduledoc false
    defstruct [:root, :epochs, :op_log, :op_log_mod, :clock, :tenant]
  end

  @type server :: GenServer.server()

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @spec derive_kek(server(), String.t(), non_neg_integer()) ::
          {:ok, binary()} | {:error, :no_root | :no_principal | :below_floor}
  def derive_kek(server \\ __MODULE__, principal, epoch) do
    GenServer.call(server, {:derive_kek, principal, epoch})
  end

  @doc "Returns the current epoch. A principal with no row starts at valid epoch 0."
  @spec current_epoch(server(), String.t()) :: {:ok, non_neg_integer()}
  def current_epoch(server \\ __MODULE__, principal) do
    GenServer.call(server, {:current_epoch, principal})
  end

  @spec set_epoch(server(), String.t(), non_neg_integer(), term()) ::
          {:ok, non_neg_integer()} | {:error, term()}
  def set_epoch(server \\ __MODULE__, principal, epoch, reason) do
    GenServer.call(server, {:set_epoch, principal, epoch, reason})
  end

  @spec raise_min_epoch(server(), String.t(), non_neg_integer(), term()) ::
          {:ok, non_neg_integer()} | {:error, term()}
  def raise_min_epoch(server \\ __MODULE__, principal, min_epoch, reason) do
    GenServer.call(server, {:raise_min_epoch, principal, min_epoch, reason})
  end

  @spec wrap(server(), String.t(), binary()) :: {:ok, Envelope.t()} | {:error, term()}
  def wrap(server \\ __MODULE__, principal, data_key) do
    GenServer.call(server, {:wrap, principal, data_key})
  end

  @spec unwrap(server(), Envelope.t()) ::
          {:ok, binary()} | {:error, :below_floor | :no_root | :auth_failed}
  def unwrap(server \\ __MODULE__, envelope) do
    GenServer.call(server, {:unwrap, envelope})
  end

  @impl true
  def init(opts) do
    op_log_mod = Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite)
    op_log = Keyword.get(opts, :op_log, op_log_mod)
    epochs = :ets.new(:key_epochs, [:set, :protected, read_concurrency: true])

    case op_log_mod.load_key_epochs(op_log) do
      {:ok, rows} ->
        Enum.each(rows, fn row ->
          :ets.insert(
            epochs,
            {Map.fetch!(row, :principal), Map.fetch!(row, :current_epoch),
             Map.fetch!(row, :min_epoch)}
          )
        end)

        {:ok,
         %State{
           root: Keyword.get(opts, :root),
           epochs: epochs,
           op_log: op_log,
           op_log_mod: op_log_mod,
           clock: Keyword.get(opts, :clock, fn -> System.system_time(:millisecond) end),
           tenant: Keyword.get(opts, :tenant, "homelab")
         }}

      {:error, reason} ->
        {:stop, {:load_key_epochs_failed, reason}}
    end
  end

  @impl true
  def handle_call({:derive_kek, principal, epoch}, _from, state) do
    {:reply, do_derive(state, principal, epoch), state}
  end

  def handle_call({:current_epoch, principal}, _from, state) do
    reply =
      with :ok <- validate_principal(principal) do
        {current_epoch, _min_epoch} = epochs(state.epochs, principal)
        {:ok, current_epoch}
      end

    {:reply, reply, state}
  end

  def handle_call({:set_epoch, principal, epoch, reason}, _from, state) do
    reply =
      with :ok <- validate_principal(principal),
           :ok <- validate_epoch(epoch),
           {current_epoch, min_epoch} <- epochs(state.epochs, principal),
           :ok <- require_higher(epoch, current_epoch, :epoch_not_increased),
           {:ok, _seq} <- append_epoch_set(state, principal, epoch, reason) do
        true = :ets.insert(state.epochs, {principal, epoch, min_epoch})
        {:ok, epoch}
      end

    {:reply, reply, state}
  end

  def handle_call({:raise_min_epoch, principal, min_epoch, reason}, _from, state) do
    reply =
      with :ok <- validate_principal(principal),
           :ok <- validate_epoch(min_epoch),
           {current_epoch, current_min_epoch} <- epochs(state.epochs, principal),
           :ok <- require_higher(min_epoch, current_min_epoch, :floor_not_raised),
           :ok <- require_not_above_current(min_epoch, current_epoch),
           {:ok, _seq} <- append_min_epoch_raised(state, principal, min_epoch, reason) do
        true = :ets.insert(state.epochs, {principal, current_epoch, min_epoch})
        emit(:floor_raise, %{principal: principal, min_epoch: min_epoch})
        {:ok, min_epoch}
      end

    {:reply, reply, state}
  end

  def handle_call({:wrap, _principal, _data_key}, _from, %State{root: nil} = state) do
    {:reply, {:error, :no_root}, state}
  end

  def handle_call({:wrap, principal, data_key}, _from, state) do
    reply =
      with :ok <- validate_principal(principal),
           :ok <- validate_data_key(data_key),
           {epoch, _min_epoch} <- epochs(state.epochs, principal),
           {:ok, kek} <- do_derive(state, principal, epoch) do
        nonce = :crypto.strong_rand_bytes(@nonce_size)
        aad = info(principal, epoch)

        {wrapped_key, tag} =
          :crypto.crypto_one_time_aead(:aes_256_gcm, kek, nonce, data_key, aad, @tag_size, true)

        {:ok,
         %Envelope{
           principal: principal,
           epoch: epoch,
           nonce: nonce,
           tag: tag,
           wrapped_key: wrapped_key
         }}
      end

    {:reply, reply, state}
  end

  def handle_call({:unwrap, _envelope}, _from, %State{root: nil} = state) do
    {:reply, {:error, :no_root}, state}
  end

  def handle_call({:unwrap, %Envelope{version: 1} = envelope}, _from, state) do
    reply =
      with :ok <- validate_envelope(envelope),
           {:ok, kek} <- do_derive(state, envelope.principal, envelope.epoch) do
        plaintext =
          :crypto.crypto_one_time_aead(
            :aes_256_gcm,
            kek,
            envelope.nonce,
            envelope.wrapped_key,
            info(envelope.principal, envelope.epoch),
            envelope.tag,
            false
          )

        case plaintext do
          :error -> {:error, :auth_failed}
          <<data_key::binary-size(@kek_size)>> -> {:ok, data_key}
          _other -> {:error, :auth_failed}
        end
      else
        {:error, reason} when reason in [:no_root, :below_floor] -> {:error, reason}
        _other -> {:error, :auth_failed}
      end

    {:reply, reply, state}
  rescue
    _error -> {:reply, {:error, :auth_failed}, state}
  end

  def handle_call({:unwrap, _envelope}, _from, state) do
    {:reply, {:error, :auth_failed}, state}
  end

  defp append_epoch_set(state, principal, epoch, reason) do
    state.op_log_mod.append(state.op_log, %Op{
      kind: :key_epoch_set,
      tenant: state.tenant,
      principal: principal,
      ts: state.clock.(),
      payload: %{principal: principal, epoch: epoch, reason: reason}
    })
  end

  defp append_min_epoch_raised(state, principal, min_epoch, reason) do
    state.op_log_mod.append(state.op_log, %Op{
      kind: :key_min_epoch_raised,
      tenant: state.tenant,
      principal: principal,
      ts: state.clock.(),
      payload: %{principal: principal, min_epoch: min_epoch, reason: reason}
    })
  end

  defp do_derive(%State{root: nil}, _principal, _epoch), do: {:error, :no_root}

  defp do_derive(state, principal, epoch) do
    with :ok <- validate_principal(principal),
         :ok <- validate_epoch(epoch) do
      {_current_epoch, min_epoch} = epochs(state.epochs, principal)

      if epoch < min_epoch do
        emit(:refused_below_floor, %{principal: principal, epoch: epoch, min_epoch: min_epoch})
        {:error, :below_floor}
      else
        kek = hkdf(state.root, info(principal, epoch))
        emit(:derivation, %{principal: principal, epoch: epoch})
        {:ok, kek}
      end
    end
  end

  defp epochs(table, principal) do
    case :ets.lookup(table, principal) do
      [{^principal, current_epoch, min_epoch}] -> {current_epoch, min_epoch}
      [] -> {0, 0}
    end
  end

  defp info(principal, epoch) do
    <<byte_size(principal)::unsigned-32, principal::binary, epoch::unsigned-64>>
  end

  defp hkdf(root, info) do
    if function_exported?(:crypto, :hkdf, 5) do
      apply(:crypto, :hkdf, [:sha256, root, @salt, info, @kek_size])
    else
      # RFC 5869 extract and the single expand block needed for a 32-byte SHA-256 key.
      pseudorandom_key = :crypto.mac(:hmac, :sha256, @salt, root)
      :crypto.mac(:hmac, :sha256, pseudorandom_key, info <> <<1>>)
    end
  end

  defp validate_principal(principal) when is_binary(principal) and byte_size(principal) > 0,
    do: :ok

  defp validate_principal(_principal), do: {:error, :no_principal}

  defp validate_epoch(epoch) when is_integer(epoch) and epoch >= 0 and epoch <= @max_epoch,
    do: :ok

  defp validate_epoch(_epoch), do: {:error, :invalid_epoch}

  defp validate_data_key(data_key) when is_binary(data_key) and byte_size(data_key) == @kek_size,
    do: :ok

  defp validate_data_key(_data_key), do: {:error, :invalid_data_key}

  defp validate_envelope(%Envelope{
         principal: principal,
         epoch: epoch,
         nonce: nonce,
         tag: tag,
         wrapped_key: wrapped_key
       })
       when is_binary(nonce) and byte_size(nonce) == @nonce_size and
              is_binary(tag) and byte_size(tag) == @tag_size and
              is_binary(wrapped_key) and byte_size(wrapped_key) == @kek_size do
    with :ok <- validate_principal(principal), do: validate_epoch(epoch)
  end

  defp validate_envelope(_envelope), do: {:error, :bad_envelope}

  defp require_higher(value, current, _error) when value > current, do: :ok
  defp require_higher(_value, _current, error), do: {:error, error}

  defp require_not_above_current(value, current) when value <= current, do: :ok
  defp require_not_above_current(_value, _current), do: {:error, :floor_above_current_epoch}

  defp emit(event, metadata) do
    :telemetry.execute([:embervm, :key_service, event], %{count: 1}, metadata)
  end
end

defimpl Inspect, for: Embervm.KeyService.State do
  import Inspect.Algebra

  def inspect(state, opts) do
    safe_state = %{
      epochs: state.epochs,
      op_log: state.op_log,
      op_log_mod: state.op_log_mod,
      clock: state.clock,
      tenant: state.tenant,
      root: :redacted
    }

    concat(["#Embervm.KeyService.State<", to_doc(safe_state, opts), ">"])
  end
end
