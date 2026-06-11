{ open Parser }
rule token = parse
  | [' ' '\t'] { token lexbuf }
  | ['0'-'9']+ as n { INT (int_of_string n) }
  | '+' { PLUS }
  | '*' { TIMES }
  | '(' { LPAREN }
  | ')' { RPAREN }
  | '\n' { EOL }
  | eof { EOL }
