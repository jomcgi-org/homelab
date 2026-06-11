/* A menhir grammar whose nonterminal types are inferred by the OCaml compiler
   (the `main` start symbol is typed; `expr` is not, so menhir runs --infer).
   The header opens the sibling [Ast] module, which the driver compiles before
   inference. */
%{ open Ast %}
%token <int> INT
%token PLUS TIMES LPAREN RPAREN EOL
%left PLUS
%left TIMES
%start <Ast.expr> main
%%
main: e = expr EOL { e }
expr:
  | i = INT { Int i }
  | a = expr PLUS b = expr { Add (a, b) }
  | a = expr TIMES b = expr { Mul (a, b) }
  | LPAREN e = expr RPAREN { e }
