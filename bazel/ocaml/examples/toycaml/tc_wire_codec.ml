external parse_json_root : string -> string * bool =
  "ml_toycaml_parse_json_root"

let decode source =
  let root, has_errors = parse_json_root source in
  if root <> "document" || has_errors then
    failwith "toycaml: fetched JSON grammar rejected input";
  Tc_wire_j.expr_of_string source
