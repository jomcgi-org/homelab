type expr =
  | Int of int
  | Add of expr * expr
  | Mul of expr * expr

let rec eval = function
  | Int n -> n
  | Add (a, b) -> eval a + eval b
  | Mul (a, b) -> eval a * eval b
