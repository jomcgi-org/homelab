---------------------------------- MODULE MC ----------------------------------
(* Model-checking wrapper: symmetry for the safety-only configs. Symmetry is *)
(* unsound for liveness, so the liveness config does not use it.            *)
EXTENDS ExpertTiering, TLC

\* Host buffers are only interchangeable within the cache or within staging.
Symmetry == Permutations(Experts) \cup Permutations(Slots)
            \cup Permutations(CacheBufs) \cup Permutations(StageBufs)
===============================================================================
