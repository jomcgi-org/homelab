-- The priciest-session subline it fed was dropped from the factory overview,
-- so the view is now read by nothing. Leaving it would cost a query on every
-- public activity request and read as a supported surface it is not.

DROP VIEW IF EXISTS public_api.agent_session_cost_7d;
