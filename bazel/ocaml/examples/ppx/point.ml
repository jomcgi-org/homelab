(* The 159-use case from ADR 004's Semgrep inventory: ppx_deriving.show.
   The [@@deriving show] attribute is expanded at build time by the
   :show_driver ppx (an ocaml_ppx target linking ppx_deriving.show). *)
type point = { x : int; y : int } [@@deriving show]
