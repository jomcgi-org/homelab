type expr =
  | Int of (int[@opaque])
  | Var of (string[@opaque])
  | Call of (string[@opaque]) * expr list
[@@deriving show, visitors { variety = "iter" }]

let node_count expr =
  let count = ref 0 in
  let visitor =
    object
      inherit [_] iter as super

      method! visit_expr env node =
        incr count;
        super#visit_expr env node
    end
  in
  visitor#visit_expr () expr;
  !count

let debug_string = show_expr

let rec to_string = function
  | Int n -> string_of_int n
  | Var v -> v
  | Call (f, args) -> f ^ "(" ^ String.concat ", " (List.map to_string args) ^ ")"
