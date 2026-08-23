defmodule Embervm.KeyService do
  @moduledoc """
  Custodian for platform-managed and customer-managed principal data keys.

  KEKs are derived on demand from the configured root generation and are never
  stored. One previous generation may coexist during root rotation.
  The only durable per-principal state is the current epoch and the minimum
  accepted epoch. Epoch 0 is a valid first epoch for a principal with no row.
  Mutations append their op-log fact before updating the ETS projection, so a
  failed append is never observable as live state.

  Customer-managed principals use an HTTPS KMS oracle that retains the KEK and
  returns only plaintext data keys plus opaque wrapped keys. With no platform
  root configured the server still starts and loads epoch facts; customer KMS
  principals remain usable while platform cryptographic operations return
  `{:error, :no_root}`.
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
    defstruct [
      :root,
      :roots,
      :root_generation,
      :epochs,
      :op_log,
      :op_log_mod,
      :clock,
      :tenant,
      :customer_kms
    ]
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
          {:ok, binary()} |
            {:error, :no_root | :no_principal | :below_floor | :customer_managed}
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
    wrap(server, principal, data_key, %{})
  end

  @spec wrap(server(), String.t(), binary(), map()) ::
          {:ok, Envelope.t()} | {:error, term()}
  def wrap(server, principal, data_key, artifact) do
    case customer_kms_config(server, principal) do
      {:ok, config} -> customer_wrap(config, principal, data_key, artifact)
      :platform -> GenServer.call(server, {:wrap, principal, data_key})
    end
  end

  @doc "Issues one data key and its envelope for an artifact export."
  @spec issue_data_key(server(), String.t(), map()) ::
          {:ok, binary(), Envelope.t()} | {:error, term()}
  def issue_data_key(server \\ __MODULE__, principal, artifact) do
    case customer_kms_config(server, principal) do
      {:ok, config} -> customer_issue(config, principal, artifact)
      :platform -> GenServer.call(server, {:issue_platform_data_key, principal, artifact})
    end
  end

  @spec unwrap(server(), Envelope.t()) ::
          {:ok, binary()} | {:error, term()}
  def unwrap(server \\ __MODULE__, %Envelope{version: version, principal: principal} = envelope)
      when version in [1, 3] do
    case customer_kms_config(server, principal) do
      {:ok, _config} -> {:error, :custody_mismatch}
      :platform -> GenServer.call(server, {:unwrap, envelope})
    end
  end

  def unwrap(server, %Envelope{version: 2, principal: principal} = envelope) do
    case customer_kms_config(server, principal) do
      {:ok, config} -> customer_unwrap(config, envelope)
      :platform -> {:error, :customer_kms_not_configured}
    end
  end

  def unwrap(_server, _envelope) do
    {:error, :auth_failed}
  end

  @impl true
  def init(opts) do
    op_log_mod = Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite)
    op_log = Keyword.get(opts, :op_log, op_log_mod)
    epochs = :ets.new(:key_epochs, [:set, :protected, read_concurrency: true])

    root_generation = Keyword.get(opts, :root_generation, 1)
    configured_root = Keyword.get(opts, :root)

    roots =
      Keyword.get_lazy(opts, :roots, fn ->
        if is_binary(configured_root), do: %{root_generation => configured_root}, else: %{}
      end)

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
           root: Map.get(roots, root_generation, configured_root),
           roots: roots,
           root_generation: root_generation,
           epochs: epochs,
           op_log: op_log,
           op_log_mod: op_log_mod,
           clock: Keyword.get(opts, :clock, fn -> System.system_time(:millisecond) end),
           tenant: Keyword.get(opts, :tenant, "homelab"),
           customer_kms: Keyword.get(opts, :customer_kms, %{})
         }}

      {:error, reason} ->
        {:stop, {:load_key_epochs_failed, reason}}
    end
  end

  @impl true
  def handle_call({:derive_kek, principal, epoch}, _from, state) do
    reply =
      if Map.has_key?(state.customer_kms, principal),
        do: {:error, :customer_managed},
        else: do_derive(state, principal, epoch)

    {:reply, reply, state}
  end

  def handle_call({:customer_kms_config, principal}, _from, state) do
    {:reply, Map.fetch(state.customer_kms, principal), state}
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
           {:ok, envelope} <- wrap_platform(state, principal, epoch, data_key) do
        {:ok, envelope}
      end

    {:reply, reply, state}
  end

  def handle_call({:issue_platform_data_key, principal, artifact}, _from, state) do
    reply =
      with :ok <- validate_principal(principal),
           :ok <- validate_artifact(artifact),
           {:ok, epoch} <- ensure_first_epoch(state, principal),
           {:ok, data_key} <- platform_data_key(state, principal, epoch, artifact),
           {:ok, envelope} <- wrap_platform(state, principal, epoch, data_key) do
        {:ok, data_key, envelope}
      end

    {:reply, reply, state}
  end

  def handle_call({:unwrap, _envelope}, _from, %State{root: nil} = state) do
    {:reply, {:error, :no_root}, state}
  end

  def handle_call({:unwrap, %Envelope{version: version} = envelope}, _from, state)
      when version in [1, 3] do
    reply = unwrap_platform(state, envelope)

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

  defp customer_kms_config(server, principal) do
    case GenServer.call(server, {:customer_kms_config, principal}) do
      {:ok, config} -> {:ok, config}
      :error -> :platform
    end
  end

  defp customer_issue(config, principal, artifact) do
    with :ok <- validate_principal(principal),
         :ok <- validate_artifact(artifact),
         {:ok, data_key, wrapped_key} <- config.adapter.issue(config, principal, artifact),
         :ok <- validate_data_key(data_key),
         :ok <- validate_wrapped_key(wrapped_key) do
      emit(:customer_kms_issue, %{principal: principal, key_ref: config.key_ref})

      {:ok, data_key,
       %Envelope{
         version: 2,
         principal: principal,
         key_ref: config.key_ref,
         wrapped_key: wrapped_key
       }}
    else
      {:error, reason} = error ->
        emit(:customer_kms_refused, %{principal: principal, operation: :issue, reason: reason})
        error
    end
  end

  defp customer_wrap(config, principal, data_key, artifact) do
    with :ok <- validate_principal(principal),
         :ok <- validate_data_key(data_key),
         {:ok, wrapped_key} <- config.adapter.wrap(config, principal, data_key, artifact),
         :ok <- validate_wrapped_key(wrapped_key) do
      emit(:customer_kms_wrap, %{principal: principal, key_ref: config.key_ref})

      {:ok,
       %Envelope{
         version: 2,
         principal: principal,
         key_ref: config.key_ref,
         wrapped_key: wrapped_key
       }}
    else
      {:error, reason} = error ->
        emit(:customer_kms_refused, %{principal: principal, operation: :wrap, reason: reason})
        error
    end
  end

  defp customer_unwrap(config, envelope) do
    with :ok <- validate_customer_envelope(envelope),
         {:ok, data_key} <-
           config.adapter.unwrap(
             config,
             envelope.principal,
             envelope.key_ref,
             envelope.wrapped_key
           ),
         :ok <- validate_data_key(data_key) do
      emit(:customer_kms_unwrap, %{principal: envelope.principal, key_ref: envelope.key_ref})
      {:ok, data_key}
    else
      {:error, reason} = error ->
        emit(:customer_kms_refused, %{
          principal: envelope.principal,
          operation: :unwrap,
          reason: reason
        })

        error
    end
  end

  defp ensure_first_epoch(state, principal) do
    case epochs(state.epochs, principal) do
      {0, min_epoch} ->
        with {:ok, _seq} <- append_epoch_set(state, principal, 1, "first_use") do
          true = :ets.insert(state.epochs, {principal, 1, min_epoch})
          {:ok, 1}
        end

      {epoch, _min_epoch} ->
        {:ok, epoch}
    end
  end

  defp platform_data_key(state, principal, epoch, artifact) do
    if artifact_kind(artifact) == "volume" do
      with {:ok, kek} <- do_derive(state, principal, epoch) do
        info = "embervm-volume-key-v1" <> artifact_workload(artifact)
        pseudorandom_key = :crypto.mac(:hmac, :sha256, <<0::256>>, kek)
        {:ok, :crypto.mac(:hmac, :sha256, pseudorandom_key, info <> <<1>>)}
      end
    else
      {:ok, :crypto.strong_rand_bytes(@kek_size)}
    end
  end

  defp wrap_platform(state, principal, epoch, data_key) do
    root_generation = state.root_generation

    with {:ok, kek} <- do_derive_generation(state, principal, epoch, root_generation) do
      nonce = :crypto.strong_rand_bytes(@nonce_size)
      version = if root_generation == 1, do: 1, else: 3
      aad = envelope_aad(version, principal, epoch, root_generation)

      {wrapped_key, tag} =
        :crypto.crypto_one_time_aead(
          :aes_256_gcm,
          kek,
          nonce,
          data_key,
          aad,
          @tag_size,
          true
        )

      {:ok,
       %Envelope{
         version: version,
         principal: principal,
         epoch: epoch,
         root_generation: if(version == 3, do: root_generation),
         nonce: nonce,
         tag: tag,
         wrapped_key: wrapped_key
       }}
    end
  end

  defp unwrap_platform(state, envelope) do
    root_generation = envelope_root_generation(envelope)

    with :ok <- validate_envelope(envelope),
         {:ok, kek} <-
           do_derive_generation(state, envelope.principal, envelope.epoch, root_generation) do
      plaintext =
        :crypto.crypto_one_time_aead(
          :aes_256_gcm,
          kek,
          envelope.nonce,
          envelope.wrapped_key,
          envelope_aad(envelope.version, envelope.principal, envelope.epoch, root_generation),
          envelope.tag,
          false
        )

      case plaintext do
        :error -> {:error, :auth_failed}
        <<data_key::binary-size(@kek_size)>> -> {:ok, data_key}
        _other -> {:error, :auth_failed}
      end
    else
      {:error, reason}
      when reason in [:no_root, :below_floor, :root_generation_unavailable] ->
        {:error, reason}

      _other ->
        {:error, :auth_failed}
    end
  end

  defp envelope_root_generation(%Envelope{version: 1}), do: 1
  defp envelope_root_generation(%Envelope{version: 3, root_generation: generation}), do: generation

  defp envelope_aad(1, principal, epoch, 1), do: info(principal, epoch)

  defp envelope_aad(3, principal, epoch, root_generation) do
    <<3, root_generation::unsigned-64, byte_size(principal)::unsigned-32, principal::binary,
      epoch::unsigned-64>>
  end

  defp do_derive(%State{root: nil}, _principal, _epoch), do: {:error, :no_root}

  defp do_derive(state, principal, epoch) do
    do_derive_generation(state, principal, epoch, state.root_generation)
  end

  defp do_derive_generation(state, principal, epoch, root_generation) do
    with :ok <- validate_principal(principal),
         :ok <- validate_epoch(epoch),
         {:ok, root} <- fetch_root(state, root_generation) do
      {_current_epoch, min_epoch} = epochs(state.epochs, principal)

      if epoch < min_epoch do
        emit(:refused_below_floor, %{principal: principal, epoch: epoch, min_epoch: min_epoch})
        {:error, :below_floor}
      else
        kek = hkdf(root, info(principal, epoch))
        emit(:derivation, %{principal: principal, epoch: epoch, root_generation: root_generation})
        {:ok, kek}
      end
    end
  end

  defp fetch_root(state, root_generation) do
    case Map.fetch(state.roots, root_generation) do
      {:ok, root} -> {:ok, root}
      :error -> {:error, :root_generation_unavailable}
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
         version: 1,
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

  defp validate_envelope(%Envelope{
         version: 3,
         principal: principal,
         root_generation: root_generation,
         epoch: epoch,
         nonce: nonce,
         tag: tag,
         wrapped_key: wrapped_key
       })
       when is_integer(root_generation) and root_generation > 0 and
              is_binary(nonce) and byte_size(nonce) == @nonce_size and
              is_binary(tag) and byte_size(tag) == @tag_size and
              is_binary(wrapped_key) and byte_size(wrapped_key) == @kek_size do
    with :ok <- validate_principal(principal), do: validate_epoch(epoch)
  end

  defp validate_envelope(_envelope), do: {:error, :bad_envelope}

  defp validate_customer_envelope(%Envelope{
         version: 2,
         principal: principal,
         key_ref: key_ref,
         wrapped_key: wrapped_key
       })
       when is_binary(key_ref) and byte_size(key_ref) > 0 and is_binary(wrapped_key) and
              byte_size(wrapped_key) > 0 do
    validate_principal(principal)
  end

  defp validate_customer_envelope(_envelope), do: {:error, :bad_envelope}

  defp validate_artifact(artifact) when is_map(artifact) do
    if artifact_kind(artifact) != "" and artifact_workload(artifact) != "",
      do: :ok,
      else: {:error, :invalid_artifact}
  end

  defp validate_artifact(_artifact), do: {:error, :invalid_artifact}

  defp validate_wrapped_key(wrapped_key)
       when is_binary(wrapped_key) and byte_size(wrapped_key) > 0,
       do: :ok

  defp validate_wrapped_key(_wrapped_key), do: {:error, :bad_envelope}

  defp artifact_kind(artifact),
    do: Map.get(artifact, "kind") || Map.get(artifact, :kind) || ""

  defp artifact_workload(artifact),
    do: Map.get(artifact, "workload") || Map.get(artifact, :workload) || ""

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
      root: :redacted,
      roots: :redacted,
      root_generation: state.root_generation,
      customer_kms: :redacted
    }

    concat(["#Embervm.KeyService.State<", to_doc(safe_state, opts), ">"])
  end
end
