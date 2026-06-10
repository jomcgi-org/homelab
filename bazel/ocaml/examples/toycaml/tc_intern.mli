(* Stable 62-bit string hash, implemented by a C stub (FNV-1a). Mirrors the one
   first-party C foreign-stub an engine of this kind keeps: a small, hot helper
   pushed into C. Used here to intern metavariable names for fast comparison. *)

val hash : string -> int
