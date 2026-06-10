type token =
  | LPAREN
  | RPAREN
  | COMMA
  | INT of int
  | IDENT of string

(* An identifier is an optional '$' (metavariable marker) then a letter or
   underscore then word characters. We validate the shape with `re` -- a real,
   fetched-from-source opam dependency -- to keep that code path live. *)
let ident_re = Re.Pcre.re {|^\$?[A-Za-z_][A-Za-z0-9_]*$|} |> Re.compile

let is_ident s = Re.execp ident_re s
let is_digit c = c >= '0' && c <= '9'

let is_ident_char c =
  (c >= 'a' && c <= 'z')
  || (c >= 'A' && c <= 'Z')
  || (c >= '0' && c <= '9')
  || c = '_' || c = '$'

let tokenize (s : string) : token list =
  let n = String.length s in
  let rec go i acc =
    if i >= n then List.rev acc
    else
      let c = s.[i] in
      if c = ' ' || c = '\t' || c = '\n' then go (i + 1) acc
      else if c = '(' then go (i + 1) (LPAREN :: acc)
      else if c = ')' then go (i + 1) (RPAREN :: acc)
      else if c = ',' then go (i + 1) (COMMA :: acc)
      else if is_digit c then (
        let j = ref i in
        while !j < n && is_digit s.[!j] do
          incr j
        done;
        let tok = String.sub s i (!j - i) in
        go !j (INT (int_of_string tok) :: acc))
      else if is_ident_char c then (
        let j = ref i in
        while !j < n && is_ident_char s.[!j] do
          incr j
        done;
        let tok = String.sub s i (!j - i) in
        if not (is_ident tok) then
          failwith (Printf.sprintf "toycaml: bad identifier %S" tok);
        go !j (IDENT tok :: acc))
      else failwith (Printf.sprintf "toycaml: unexpected character %C" c)
  in
  go 0 []
