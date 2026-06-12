(* OVERLAY (bazel/ocaml/semgrep_src/overlays) -- ADDS a file upstream does not
   have here. UCmd.ml spans its subprocess runs through libs/telemetry's
   Tracing module; telemetry's closure (opentelemetry + cohttp-eio, semgrep
   git pins) is dispatched out of this phase. This stub provides the two
   entry points UCmd uses with the same shapes (user_data mirrors
   Opentelemetry.value); spans are no-ops. Deleted when telemetry joins the
   translated frontier, where the real Tracing shadows this module. *)

type user_data =
  [ `Int of int | `String of string | `Bool of bool | `Float of float | `None ]

type scope = unit

let with_span ?level:_ ?__FUNCTION__:_ ~__FILE__:_ ~__LINE__:_
    ?(data : (string * user_data) list option) (_name : string)
    (f : scope -> 'a) : 'a =
  ignore data;
  f ()

let add_data_to_span (_ : scope) (_ : (string * user_data) list) = ()
