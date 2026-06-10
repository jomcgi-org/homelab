type bindings = (string * Tc_ast.expr) list

(* Metavariable names are interned via the C stub (Tc_intern.hash) as a fast
   pre-check before the exact name compare, mirroring how a hot engine path
   pushes string work into C. Correctness still rests on String.equal. *)
let bound_value (b : bindings) name =
  let h = Tc_intern.hash name in
  let rec find = function
    | [] -> None
    | (k, v) :: _ when Tc_intern.hash k = h && String.equal k name -> Some v
    | _ :: rest -> find rest
  in
  find b

let rec go (pat : Tc_ast.expr) (tgt : Tc_ast.expr) (acc : bindings) :
    bindings option =
  match pat with
  | Tc_ast.Var name when Tc_pattern.is_metavar name -> (
      match bound_value acc name with
      | Some prev -> if prev = tgt then Some acc else None
      | None -> Some ((name, tgt) :: acc))
  | Tc_ast.Int n -> (
      match tgt with Tc_ast.Int m when m = n -> Some acc | _ -> None)
  | Tc_ast.Var name -> (
      match tgt with
      | Tc_ast.Var t when String.equal t name -> Some acc
      | _ -> None)
  | Tc_ast.Call (f, pargs) -> (
      match tgt with
      | Tc_ast.Call (g, targs)
        when String.equal f g && List.length pargs = List.length targs ->
          go_list pargs targs acc
      | _ -> None)

and go_list ps ts acc =
  match (ps, ts) with
  | [], [] -> Some acc
  | p :: ps', t :: ts' -> (
      match go p t acc with Some acc' -> go_list ps' ts' acc' | None -> None)
  | _ -> None

let match_expr ~pattern ~target = go pattern target []
