let parse s = Parser.main Lexer.token (Lexing.from_string s)

let () =
  assert (Ast.eval (parse "1 + 2 * 3\n") = 7);
  assert (Ast.eval (parse "(1 + 2) * 3\n") = 9);
  print_endline "lexparse: ok"
