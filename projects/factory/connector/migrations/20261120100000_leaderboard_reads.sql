GRANT USAGE ON SCHEMA public TO factory_leaderboard;
GRANT SELECT ON arm_delivery_results TO factory_leaderboard;
GRANT SELECT (arm, review_rounds, first_build_minutes, rework_minutes, merged)
    ON pr_delivery_results TO factory_leaderboard;
