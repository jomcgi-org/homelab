(* Validate JSON with the fetched tree-sitter grammar, then decode the typed
   recursive value with atdgen-generated code. *)
val decode : string -> Tc_wire_t.expr
