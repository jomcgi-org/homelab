defmodule Embervm.Auth.Identity do
  @moduledoc """
  Identity returned by a Kubernetes TokenReview.

  Bound ServiceAccount tokens carry pod and, on newer clusters, node claims in
  addition to the ServiceAccount username.
  """

  defstruct [
    :username,
    :pod_uid,
    :pod_name,
    :node_name,
    audiences: [],
    audience_validated: false
  ]

  @type t :: %__MODULE__{
          username: String.t() | nil,
          pod_uid: String.t() | nil,
          pod_name: String.t() | nil,
          node_name: String.t() | nil,
          audiences: [String.t()],
          audience_validated: boolean()
        }
end
