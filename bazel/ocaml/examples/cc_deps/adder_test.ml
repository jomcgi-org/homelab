let () =
  assert (Adder.add 3 4 = 7);
  assert (Adder.add (-2) 5 = 3);
  print_endline "cc_deps: ok"
