(* OVERLAY (bazel/ocaml/semgrep_src/overlays) -- replaces the upstream file.
   Upstream meters errors through the telemetry library (SharedCounterTable,
   Ometrics), whose closure (opentelemetry + cohttp-eio, semgrep git pins) is
   dispatched out of this phase (see semgrep_src/README.md). The API surface
   (Common_metrics.mli, kept verbatim) is preserved; the meters are no-ops
   until telemetry joins the translated frontier, at which point this overlay
   is deleted. *)

type metered_error = ..

let meter_exception (_ : exn) = ()
let meter_error (_ : metered_error) = ()
