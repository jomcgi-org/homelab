-- knowledge.notes: add a separate force-directed layout pass for the
-- public-only subgraph.
--
-- The existing layout_x/layout_y columns are computed by the gardener
-- across the full graph (public + private). When the public API filters
-- to visibility='public' the surviving nodes keep their global positions,
-- which leaves visible "holes" where private clusters used to anchor
-- the layout. The new columns hold positions computed over the public
-- subset alone so the public /notes page renders a dense, intentional
-- layout instead of a sparse ghost of the full graph.
--
-- Both columns are nullable. The next reconcile cycle populates them;
-- until then, get_public_graph falls back to layout_x/layout_y via
-- COALESCE so deploys never serve a blank canvas.

ALTER TABLE knowledge.notes
    ADD COLUMN layout_x_public DOUBLE PRECISION,
    ADD COLUMN layout_y_public DOUBLE PRECISION;
