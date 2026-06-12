(* OVERLAY (bazel/ocaml/semgrep_src/overlays) -- ADDS a file upstream does not
   have here. Memory_limit.ml tags its log lines with libs/telemetry's
   Logging.no_telemetry_tag_set (a Logs.Tag.set marking lines that must not
   ship as otel logs); telemetry's closure is dispatched out of this phase.
   The empty tag set is the faithful telemetry-less stand-in. Deleted when
   telemetry joins the translated frontier. *)

let no_telemetry_tag_set = Logs.Tag.empty
