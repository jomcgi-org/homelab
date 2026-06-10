type t = Ast.expr

let is_metavar name = String.length name > 0 && name.[0] = '$'
