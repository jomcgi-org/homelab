(* Returns (root node type, tree has errors). *)
external parse_root : string -> string * bool = "ml_ts_parse_json_root"
