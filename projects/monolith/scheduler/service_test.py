"""Unit tests for scheduler/service.py."""

from datetime import datetime, timezone

import pytest
from kubernetes_asyncio.client.exceptions import ApiException
from sqlmodel import Session, SQLModel, create_engine

from scheduler import service
from scheduler.api import ScheduledJob, _registry


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    """File-backed SQLite session with schema stripped (SQLite has no schemas)."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'scheduler.db'}",
        connect_args={"check_same_thread": False},
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session

    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


@pytest.fixture(autouse=True)
def _clear_registry():
    _registry.clear()
    yield
    _registry.clear()


def _seed(session: Session, name: str, *, next_run_at: datetime) -> None:
    session.add(
        ScheduledJob(
            name=name,
            interval_secs=60,
            next_run_at=next_run_at,
            ttl_secs=300,
        )
    )
    session.commit()


class TestListJobs:
    def test_returns_jobs_sorted_by_name(self, session):
        now = datetime.now(timezone.utc)
        _seed(session, "b.job", next_run_at=now)
        _seed(session, "a.job", next_run_at=now)

        jobs = service.list_jobs(session)
        assert [j.name for j in jobs] == ["a.job", "b.job"]

    def test_returns_empty_when_no_jobs(self, session):
        assert service.list_jobs(session) == []

    def test_has_handler_reflects_registry(self, session):
        async def _h(s: Session) -> None:
            return None

        _registry["registered.job"] = _h
        _seed(session, "registered.job", next_run_at=datetime.now(timezone.utc))
        _seed(session, "orphan.job", next_run_at=datetime.now(timezone.utc))

        by_name = {j.name: j for j in service.list_jobs(session)}
        assert by_name["registered.job"].has_handler is True
        assert by_name["orphan.job"].has_handler is False


class TestGetJob:
    def test_returns_view_for_existing_job(self, session):
        _seed(session, "j", next_run_at=datetime.now(timezone.utc))
        view = service.get_job(session, "j")
        assert view is not None
        assert view.name == "j"
        assert view.interval_secs == 60

    def test_returns_none_for_missing_job(self, session):
        assert service.get_job(session, "nope") is None


class FakeKubernetesClient:
    def __init__(
        self,
        cronworkflows: list[dict] | None = None,
        *,
        error: ApiException | None = None,
    ) -> None:
        self.cronworkflows = cronworkflows or []
        self.error = error
        self.created: list[tuple[str, dict]] = []
        self.closed = False

    async def list_cronworkflows(self, namespace: str) -> list[dict]:
        if self.error:
            raise self.error
        return self.cronworkflows

    async def create_workflow(self, namespace: str, body: dict) -> str:
        if self.error:
            raise self.error
        self.created.append((namespace, body))
        return "nightly-manual-abc12"

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _workflow_namespace(monkeypatch):
    monkeypatch.setenv("SCHEDULER_WORKFLOW_NAMESPACE", "workflows-test")


class TestRunNow:
    @pytest.mark.asyncio
    async def test_unknown_job_returns_404(self, session):
        result = await service.run_now(session, "nope")
        assert result.status_code == 404
        assert result.workflow_name is None
        assert result.message == "unknown job: nope"

    @pytest.mark.asyncio
    async def test_missing_namespace_returns_503(self, session, monkeypatch):
        monkeypatch.delenv("SCHEDULER_WORKFLOW_NAMESPACE", raising=False)
        _seed(session, "knowledge.layout", next_run_at=datetime.now(timezone.utc))
        result = await service.run_now(session, "knowledge.layout")
        assert result.status_code == 503
        assert result.workflow_name is None

    @pytest.mark.asyncio
    async def test_matching_cronworkflow_creates_workflow_without_db_update(
        self, session, monkeypatch
    ):
        original_next_run = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
        _seed(session, "j", next_run_at=original_next_run)
        cronworkflow = {
            "metadata": {
                "name": "nightly",
                "namespace": "workflows-test",
                "annotations": {"monolith.jomcgi.dev/replaces": "j"},
                "labels": {"app.kubernetes.io/name": "monolith"},
            },
            "spec": {
                "workflowMetadata": {
                    "annotations": {"example.test/source": "cron"},
                    "labels": {"example.test/job": "nightly"},
                },
                "workflowSpec": {
                    "entrypoint": "run",
                    "templates": [{"name": "run"}],
                },
            },
        }
        fake = FakeKubernetesClient([cronworkflow])
        monkeypatch.setattr(service, "KubernetesClient", lambda: fake)

        result = await service.run_now(session, "j")

        assert result == service.RunNowResult(
            job="j",
            workflow_name="nightly-manual-abc12",
            namespace="workflows-test",
            status_code=202,
        )
        assert fake.closed is True
        assert len(fake.created) == 1
        namespace, manifest = fake.created[0]
        assert namespace == "workflows-test"
        assert manifest["apiVersion"] == "argoproj.io/v1alpha1"
        assert manifest["kind"] == "Workflow"
        assert manifest["metadata"]["generateName"] == "nightly-manual-"
        assert manifest["metadata"]["namespace"] == "workflows-test"
        assert manifest["metadata"]["labels"] == {
            "app.kubernetes.io/name": "monolith",
            "example.test/job": "nightly",
            "workflows.argoproj.io/cron-workflow": "nightly",
        }
        assert manifest["metadata"]["annotations"] == {"example.test/source": "cron"}
        assert manifest["spec"] == cronworkflow["spec"]["workflowSpec"]
        persisted = session.get(ScheduledJob, "j")
        assert persisted is not None
        assert persisted.next_run_at.replace(tzinfo=timezone.utc) == original_next_run

    @pytest.mark.asyncio
    async def test_no_matching_cronworkflow_returns_409(self, session, monkeypatch):
        _seed(session, "j", next_run_at=datetime.now(timezone.utc))
        fake = FakeKubernetesClient(
            [
                {
                    "metadata": {
                        "name": "other",
                        "annotations": {"monolith.jomcgi.dev/replaces": "another.job"},
                    }
                }
            ]
        )
        monkeypatch.setattr(service, "KubernetesClient", lambda: fake)

        result = await service.run_now(session, "j")

        assert result.status_code == 409
        assert result.message == "no CronWorkflow replaces job j"
        assert fake.created == []

    @pytest.mark.asyncio
    async def test_kubernetes_api_error_returns_502(self, session, monkeypatch):
        _seed(session, "j", next_run_at=datetime.now(timezone.utc))
        fake = FakeKubernetesClient(error=ApiException(status=500, reason="boom"))
        monkeypatch.setattr(service, "KubernetesClient", lambda: fake)

        result = await service.run_now(session, "j")

        assert result.status_code == 502
        assert result.workflow_name is None
        assert "Kubernetes API error" in (result.message or "")
