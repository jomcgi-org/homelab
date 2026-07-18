-- Wake history for the private k8s-terminal demo (demos/k8s_terminal_api.py).
-- One row per wake the terminal backend drove: when, from which lifecycle
-- state, how long to running, and the honest classification (relit vs
-- cold-boot, judged from inner-node age). Renders as the demo's up/down band.
-- Private tier only: no public_reader grants.

CREATE TABLE ember_k8s_wake_event (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at             timestamptz NOT NULL DEFAULT now(),
    from_state     text NOT NULL,
    duration_ms    integer NOT NULL,
    classification text NOT NULL
);

CREATE INDEX ember_k8s_wake_event_at_idx ON ember_k8s_wake_event (at DESC);
