# Test fixtures for the no-create-event-dispatcher-runes semgrep rule. # #
Annotations: # # ruleid: no-create-event-dispatcher-runes — the next
non-annotation line MUST be flagged # # ok: no-create-event-dispatcher-runes —
the next non-annotation line MUST NOT be flagged # Positive examples —
createEventDispatcher in a runes file (should be flagged) # Bare named import: #
ruleid: no-create-event-dispatcher-runes import {createEventDispatcher} from 'svelte';
# Combined import alongside other Svelte exports: # ruleid: no-create-event-dispatcher-runes
import {(createEventDispatcher, onMount)} from 'svelte'; # Double-quoted module specifier:
# ruleid: no-create-event-dispatcher-runes import {createEventDispatcher} from "svelte";
# Negative examples — safe imports that must not be flagged # ok: no-create-event-dispatcher-runes
import {onMount} from 'svelte'; # ok: no-create-event-dispatcher-runes import {$state}
from 'svelte/reactivity'; # ok: no-create-event-dispatcher-runes import {createEventDispatcher}
from 'svelte/legacy';
