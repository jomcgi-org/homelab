(* Depends on Message — so `ocamldep -sort` must place message.ml first. *)
let render name = Printf.sprintf "%s \u{2014} greetings, %s!" Message.banner name
