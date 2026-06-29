-- ADR 026 Task 1.2: event-driven dispatch. fc-agentd reconciles desired vs
-- actual thread state on a 5s poll, so a freshly submitted PENDING thread waits
-- up to a full tick before it is claimed (the 0-to-5s claim wait in the ADR's
-- cold-start breakdown). A NOTIFY on every PENDING insert/transition lets the
-- controller wake and run a reconcile pass immediately, collapsing that wait to
-- sub-second.
--
-- The 5s poll stays as the safety net for missed notifications (a reconnecting
-- listener, a daemon restart), so correctness never depends on the notification
-- arriving: a dropped NOTIFY only delays a claim to the next tick, never loses
-- it. Payload is the thread_id (informational; the loop reconciles the whole
-- node either way).

CREATE OR REPLACE FUNCTION claude_agent.notify_thread_pending() RETURNS trigger AS $$
BEGIN
    IF NEW.state = 'PENDING' THEN
        PERFORM pg_notify('agent_threads_pending', NEW.thread_id);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agent_threads_pending_notify
    AFTER INSERT OR UPDATE OF state ON claude_agent.agent_threads
    FOR EACH ROW
    EXECUTE FUNCTION claude_agent.notify_thread_pending();
