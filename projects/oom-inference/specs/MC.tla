---------------------------------- MODULE MC ----------------------------------
(* Model-checking wrapper: symmetry for the safety-only configs. Symmetry is *)
(* unsound for liveness, so the liveness config does not use it.            *)
EXTENDS ExpertTiering, TLC

Symmetry == Permutations(Experts) \cup Permutations(Slots) \cup Permutations(HostBufs)
===============================================================================
