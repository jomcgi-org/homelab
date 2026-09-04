def test_drainer_health_ignores_the_kg_kind(monkeypatch):
    """Cap-deferred kg-drain jobs must not read as a general-lane stall."""
    from agent.config import DrainerSettings
    from swarm import health

    seen = {}

    def fake_core(session, job_kinds, threshold_seconds):
        seen["kinds"] = list(job_kinds)
        return {"ok": True}

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(health, "_drainer_health_core", fake_core)
    monkeypatch.setattr("core.db.get_engine", lambda: None)
    monkeypatch.setattr(health, "Session", lambda engine: _Session())
    settings = DrainerSettings(
        enabled=True,
        max_jobs_per_cycle=1,
        turn_timeout_seconds=1,
        stall_threshold_seconds=1,
        job_kinds=("qwen-drain", "kg-drain"),
        kg_max_jobs_per_day=40,
        repo="r",
        branch="main",
        reasoning=False,
    )
    health._read_drainer_health(settings)
    assert seen["kinds"] == ["qwen-drain"]
