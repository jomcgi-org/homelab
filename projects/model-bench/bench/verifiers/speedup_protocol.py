"""Stdlib-only trusted paired-v1 helper, copied to the isolated grading directory.

The task harness calls the injected benchmark() once. It supplies fresh positional
arguments via make_input(seed) and independent oracle cases as (args, expected).
All result emission and candidate loading belong to this helper.
"""

import copy
import importlib.util
import json
import os
import runpy
import sys
import time


class CandidateFailure(Exception):
    pass


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Dataclasses and similar stdlib helpers look up the defining module.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_harness(harness_path, pair_count, seed):
    """Read the nonce before candidate import, capture capabilities, then exit hard."""
    nonce = sys.stdin.readline().strip()
    timer = time.perf_counter_ns
    dumps = json.dumps
    write, flush, exit_process = sys.stdout.write, sys.stdout.flush, os._exit
    deepcopy = copy.deepcopy
    load = _load
    results = []
    retained_inputs = []

    def benchmark(*, candidate_path, baseline_path, function, make_input, oracle_cases):
        if results:
            raise ValueError("harness must call benchmark exactly once")
        base = getattr(load(baseline_path, "_speedup_baseline"), function)
        cases = list(oracle_cases)
        if not cases:
            raise ValueError("harness needs independent baseline oracle cases")
        for args, expected in cases:
            if base(*deepcopy(args)) != expected:
                raise ValueError("baseline failed its own oracle")
        try:
            candidate = getattr(load(candidate_path, "_speedup_candidate"), function)
        except BaseException as exc:
            raise CandidateFailure(
                f"candidate import error: {type(exc).__name__}: {exc}"
            ) from exc
        for args, expected in cases:
            try:
                actual = candidate(*deepcopy(args))
            except BaseException as exc:
                raise CandidateFailure(
                    f"candidate exception: {type(exc).__name__}: {exc}"
                ) from exc
            if actual != expected:
                raise CandidateFailure("hidden oracle output mismatch")

        def pair(index):
            # Dataset generation and equality checking are outside the timed region.
            base_args = make_input(seed + index)
            candidate_args = make_input(seed + index)
            if not isinstance(base_args, tuple) or base_args != candidate_args:
                raise ValueError(
                    "make_input must return identical fresh argument tuples"
                )
            # Detach even if a task's builder accidentally shares nested objects.
            base_args, candidate_args = deepcopy(base_args), deepcopy(candidate_args)
            retained_inputs.extend((base_args, candidate_args))
            baseline_first = index < 0 or index % 2 == 0
            outputs, durations = {}, {}
            order = (
                ("baseline", "candidate")
                if baseline_first
                else ("candidate", "baseline")
            )
            for side in order:
                fn, args = (
                    (base, base_args)
                    if side == "baseline"
                    else (candidate, candidate_args)
                )
                try:
                    start = timer()
                    output = fn(*args)
                    stop = timer()
                except BaseException as exc:
                    if side == "candidate":
                        raise CandidateFailure(
                            f"candidate exception: {type(exc).__name__}: {exc}"
                        ) from exc
                    raise
                durations[side] = (stop - start) / 1_000_000_000
                outputs[side] = deepcopy(output)
            if outputs["baseline"] != outputs["candidate"]:
                raise CandidateFailure("benchmark pair output mismatch")
            return {
                "baseline_s": durations["baseline"],
                "candidate_s": durations["candidate"],
                "order": "baseline-first" if baseline_first else "candidate-first",
            }

        warmup = [pair(-1)]
        samples = [pair(index) for index in range(pair_count)]
        results.append({"status": "ok", "warmup": warmup, "samples": samples})

    try:
        runpy.run_path(harness_path, init_globals={"benchmark": benchmark})
        if len(results) != 1:
            raise ValueError("harness must call benchmark exactly once")
        result = results[0]
    except CandidateFailure as exc:
        result = {"status": "graded_failure", "detail": str(exc)}
    except BaseException as exc:  # noqa: BLE001 - setup failures become authenticated harness errors
        result = {"status": "harness_error", "detail": f"{type(exc).__name__}: {exc}"}
    result["nonce"] = nonce
    write("\n" + dumps(result) + "\n")
    flush()
    exit_process(0)  # Candidate finalizers never run after the authenticated result.
