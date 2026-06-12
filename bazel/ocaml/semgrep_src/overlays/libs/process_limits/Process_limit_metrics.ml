(* OVERLAY (bazel/ocaml/semgrep_src/overlays) -- replaces the upstream file.
   Upstream meters time-limit hits through telemetry's Ometrics; telemetry's
   closure is dispatched out of this phase. The API surface
   (Process_limit_metrics.mli, kept verbatim) is preserved as a no-op until
   telemetry joins the translated frontier. *)

let record_time_limit ~info:(_ : Exception.timeout_info)
    ~result_info:(_ : Exception.timeout_result_info) =
  ()
