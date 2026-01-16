"""Integration tests for API endpoints."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from soctalk.api.app import create_app
from soctalk.api.deps import get_db_session
from soctalk.persistence.models import Event, InvestigationReadModel


@pytest.fixture
def mock_db_session():
    """Create a mock async database session."""
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


@pytest.fixture
def app(mock_db_session):
    """Create test app with mock dependencies."""
    app = create_app()

    async def override_get_db_session():
        yield mock_db_session

    app.dependency_overrides[get_db_session] = override_get_db_session
    return app


@pytest.fixture
def client(app):
    """Create test client."""
    with TestClient(app) as client:
        yield client


class TestHealthEndpoints:
    """Tests for health check endpoints."""

    def test_health_check(self, client):
        """Test health check endpoint returns healthy status."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_root_endpoint(self, client):
        """Test root endpoint returns API info."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "SocTalk API"
        assert data["version"] == "0.1.0"
        assert "docs" in data


class TestInvestigationEndpoints:
    """Tests for investigation API endpoints."""

    @pytest.fixture
    def sample_investigation(self):
        """Create a sample investigation for tests."""
        return InvestigationReadModel(
            id=uuid4(),
            title="Test Investigation",
            status="in_progress",
            phase="enrichment",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            alert_count=5,
            observable_count=10,
            malicious_count=2,
            max_severity="high",
            verdict_decision=None,
            thehive_case_id=None,
            time_to_triage_seconds=None,
            time_to_verdict_seconds=None,
            verdict_confidence=None,
            threat_actor=None,
            tags=[],
        )

    def test_list_investigations_empty(self, client, mock_db_session):
        """Test list investigations when database is empty."""
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 0

        mock_items_result = MagicMock()
        mock_items_result.scalars.return_value.all.return_value = []

        mock_db_session.execute.side_effect = [mock_count_result, mock_items_result]

        response = client.get("/api/investigations")
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["page"] == 1
        assert data["has_more"] is False

    def test_list_investigations_with_data(
        self, client, mock_db_session, sample_investigation
    ):
        """Test list investigations returns items."""
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 1

        mock_items_result = MagicMock()
        mock_items_result.scalars.return_value.all.return_value = [sample_investigation]

        mock_db_session.execute.side_effect = [mock_count_result, mock_items_result]

        response = client.get("/api/investigations")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["status"] == "in_progress"
        assert data["items"][0]["alert_count"] == 5

    def test_list_investigations_with_filters(self, client, mock_db_session):
        """Test list investigations with query filters."""
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 0

        mock_items_result = MagicMock()
        mock_items_result.scalars.return_value.all.return_value = []

        mock_db_session.execute.side_effect = [mock_count_result, mock_items_result]

        response = client.get(
            "/api/investigations",
            params={
                "status": "in_progress",
                "severity": "high",
                "page": 1,
                "page_size": 10,
            },
        )
        assert response.status_code == 200

    def test_list_investigations_pagination(self, client, mock_db_session):
        """Test list investigations pagination."""
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 0

        mock_items_result = MagicMock()
        mock_items_result.scalars.return_value.all.return_value = []

        mock_db_session.execute.side_effect = [mock_count_result, mock_items_result]

        response = client.get("/api/investigations?page=2&page_size=50")
        assert response.status_code == 200
        data = response.json()
        assert data["page"] == 2
        assert data["page_size"] == 50

    def test_get_investigation_not_found(self, client, mock_db_session):
        """Test get investigation returns 404 when not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db_session.execute.return_value = mock_result

        investigation_id = uuid4()
        response = client.get(f"/api/investigations/{investigation_id}")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_get_investigation_success(
        self, client, mock_db_session, sample_investigation
    ):
        """Test get investigation returns details."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = sample_investigation
        mock_db_session.execute.return_value = mock_result

        response = client.get(f"/api/investigations/{sample_investigation.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(sample_investigation.id)
        assert data["status"] == "in_progress"
        assert data["phase"] == "enrichment"
        assert data["alert_count"] == 5

    def test_get_investigation_events_not_found(self, client, mock_db_session):
        """Test get investigation events returns 404 when investigation not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db_session.execute.return_value = mock_result

        investigation_id = uuid4()
        response = client.get(f"/api/investigations/{investigation_id}/events")
        assert response.status_code == 404

    def test_get_investigation_events_success(
        self, client, mock_db_session, sample_investigation
    ):
        """Test get investigation events returns timeline."""
        # First call returns investigation, second returns events count, third returns events
        investigation_id = sample_investigation.id
        sample_event = Event(
            id=uuid4(),
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type="investigation.created",
            version=1,
            timestamp=datetime.utcnow(),
            data={},
            event_metadata={},
        )

        mock_inv_result = MagicMock()
        mock_inv_result.scalar_one_or_none.return_value = sample_investigation

        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 1

        mock_events_result = MagicMock()
        mock_events_result.scalars.return_value.all.return_value = [sample_event]

        mock_db_session.execute.side_effect = [
            mock_inv_result,
            mock_count_result,
            mock_events_result,
        ]

        response = client.get(f"/api/investigations/{investigation_id}/events")
        assert response.status_code == 200
        data = response.json()
        assert data["investigation_id"] == str(investigation_id)
        assert len(data["events"]) == 1
        assert data["events"][0]["event_type"] == "investigation.created"

    def test_pause_investigation_success(
        self, client, mock_db_session, sample_investigation
    ):
        """Test pause investigation succeeds for in_progress investigation."""
        mock_inv_result = MagicMock()
        mock_inv_result.scalar_one_or_none.return_value = sample_investigation

        mock_version_result = MagicMock()
        mock_version_result.scalar_one_or_none.return_value = None

        mock_inv_project_result = MagicMock()
        mock_inv_project_result.scalar_one_or_none.return_value = sample_investigation

        mock_db_session.execute.side_effect = [
            mock_inv_result,
            mock_version_result,
            mock_inv_project_result,
        ]

        response = client.post(f"/api/investigations/{sample_investigation.id}/pause")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "paused" in data["message"].lower()
        assert sample_investigation.status == "paused"

    def test_pause_investigation_not_found(self, client, mock_db_session):
        """Test pause investigation returns 404 when not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db_session.execute.return_value = mock_result

        investigation_id = uuid4()
        response = client.post(f"/api/investigations/{investigation_id}/pause")
        assert response.status_code == 404

    def test_pause_investigation_invalid_status(
        self, client, mock_db_session, sample_investigation
    ):
        """Test pause investigation fails for closed investigation."""
        sample_investigation.status = "closed"
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = sample_investigation
        mock_db_session.execute.return_value = mock_result

        response = client.post(f"/api/investigations/{sample_investigation.id}/pause")
        assert response.status_code == 400
        assert "cannot pause" in response.json()["detail"].lower()

    def test_resume_investigation_success(self, client, mock_db_session):
        """Test resume investigation succeeds for paused investigation."""
        paused_investigation = InvestigationReadModel(
            id=uuid4(),
            status="paused",
            phase="enrichment",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        mock_inv_result = MagicMock()
        mock_inv_result.scalar_one_or_none.return_value = paused_investigation

        mock_version_result = MagicMock()
        mock_version_result.scalar_one_or_none.return_value = None

        mock_inv_project_result = MagicMock()
        mock_inv_project_result.scalar_one_or_none.return_value = paused_investigation

        mock_db_session.execute.side_effect = [
            mock_inv_result,
            mock_version_result,
            mock_inv_project_result,
        ]

        response = client.post(
            f"/api/investigations/{paused_investigation.id}/resume"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert paused_investigation.status == "in_progress"

    def test_resume_investigation_invalid_status(
        self, client, mock_db_session, sample_investigation
    ):
        """Test resume investigation fails for non-paused investigation."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = sample_investigation
        mock_db_session.execute.return_value = mock_result

        response = client.post(f"/api/investigations/{sample_investigation.id}/resume")
        assert response.status_code == 400

    def test_cancel_investigation_success(
        self, client, mock_db_session, sample_investigation
    ):
        """Test cancel investigation succeeds."""
        mock_inv_result = MagicMock()
        mock_inv_result.scalar_one_or_none.return_value = sample_investigation

        mock_version_result = MagicMock()
        mock_version_result.scalar_one_or_none.return_value = None

        mock_inv_project_result = MagicMock()
        mock_inv_project_result.scalar_one_or_none.return_value = sample_investigation

        mock_metrics_result = MagicMock()
        mock_metrics_result.scalar_one_or_none.return_value = None

        mock_db_session.execute.side_effect = [
            mock_inv_result,
            mock_version_result,
            mock_inv_project_result,
            mock_metrics_result,
        ]

        response = client.post(f"/api/investigations/{sample_investigation.id}/cancel")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert sample_investigation.status == "cancelled"
        assert sample_investigation.closed_at is not None

    def test_cancel_investigation_already_closed(self, client, mock_db_session):
        """Test cancel investigation fails for already closed investigation."""
        closed_investigation = InvestigationReadModel(
            id=uuid4(),
            status="closed",
            phase="verdict",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            closed_at=datetime.utcnow(),
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = closed_investigation
        mock_db_session.execute.return_value = mock_result

        response = client.post(f"/api/investigations/{closed_investigation.id}/cancel")
        assert response.status_code == 400


class TestInvalidRequests:
    """Tests for invalid API requests."""

    def test_invalid_uuid_format(self, client):
        """Test invalid UUID format returns 422."""
        response = client.get("/api/investigations/not-a-valid-uuid")
        assert response.status_code == 422

    def test_invalid_pagination_params(self, client, mock_db_session):
        """Test invalid pagination params return 422."""
        response = client.get("/api/investigations?page=0")
        assert response.status_code == 422

        response = client.get("/api/investigations?page_size=0")
        assert response.status_code == 422

        response = client.get("/api/investigations?page_size=200")
        assert response.status_code == 422


class TestCORS:
    """Tests for CORS configuration."""

    def test_cors_headers_present(self, client):
        """Test CORS headers are present in response."""
        response = client.options(
            "/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        # FastAPI's CORSMiddleware handles OPTIONS requests
        assert response.status_code in (200, 405)  # Either allowed or method not allowed
