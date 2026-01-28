"""Tests for docker-compose-nix.yml configuration file."""

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent.parent.parent


@pytest.fixture
def nix_compose_path(project_root: Path) -> Path:
    """Get the path to docker-compose-nix.yml."""
    return project_root / "docker-compose-nix.yml"


@pytest.fixture
def original_compose_path(project_root: Path) -> Path:
    """Get the path to the original docker-compose.yml."""
    return project_root / "docker-compose.yml"


@pytest.fixture
def nix_compose(nix_compose_path: Path) -> dict:
    """Load and parse docker-compose-nix.yml."""
    with open(nix_compose_path) as f:
        return yaml.safe_load(f)


@pytest.fixture
def original_compose(original_compose_path: Path) -> dict:
    """Load and parse the original docker-compose.yml."""
    with open(original_compose_path) as f:
        return yaml.safe_load(f)


class TestNixComposeExists:
    """Test that docker-compose-nix.yml exists and is separate from original."""

    def test_nix_compose_file_exists(self, nix_compose_path: Path) -> None:
        """New docker-compose-nix.yml file exists (separate from existing docker-compose.yml)."""
        assert nix_compose_path.exists(), "docker-compose-nix.yml must exist"

    def test_is_separate_from_original(
        self, nix_compose_path: Path, original_compose_path: Path
    ) -> None:
        """Verify docker-compose-nix.yml is separate from docker-compose.yml."""
        assert nix_compose_path != original_compose_path
        assert nix_compose_path.name == "docker-compose-nix.yml"
        assert original_compose_path.name == "docker-compose.yml"


class TestNixImages:
    """Test that services use pre-built Nix images."""

    EXPECTED_NIX_IMAGES = {
        "api": "soctalk-api:latest",
        "frontend": "soctalk-frontend:latest",
        "orchestrator": "soctalk-orchestrator:latest",
        "mock-endpoint": "soctalk-mock-endpoint:latest",
    }

    def test_uses_prebuilt_nix_images(self, nix_compose: dict) -> None:
        """Uses pre-built Nix images: soctalk-api:latest, soctalk-frontend:latest, soctalk-orchestrator:latest, soctalk-mock-endpoint:latest."""
        services = nix_compose.get("services", {})

        for service_name, expected_image in self.EXPECTED_NIX_IMAGES.items():
            assert service_name in services, f"Service '{service_name}' must exist"
            service = services[service_name]
            assert "image" in service, f"Service '{service_name}' must have 'image' directive"
            assert (
                service["image"] == expected_image
            ), f"Service '{service_name}' must use image '{expected_image}'"


class TestNoBuildsForNixServices:
    """Test that services use 'image:' directive instead of 'build:' directive."""

    def test_services_use_image_not_build(self, nix_compose: dict) -> None:
        """Services use 'image:' directive instead of 'build:' directive."""
        services = nix_compose.get("services", {})

        # Services that should use Nix images (not build)
        nix_services = ["api", "frontend", "orchestrator", "mock-endpoint"]

        for service_name in nix_services:
            if service_name in services:
                service = services[service_name]
                assert (
                    "build" not in service
                ), f"Service '{service_name}' must not have 'build' directive"
                assert "image" in service, f"Service '{service_name}' must have 'image' directive"


class TestPostgresUnchanged:
    """Test that Postgres service remains unchanged."""

    def test_postgres_uses_official_image(self, nix_compose: dict) -> None:
        """Postgres service remains unchanged (uses official postgres:16-alpine)."""
        services = nix_compose.get("services", {})
        assert "postgres" in services, "Postgres service must exist"

        postgres = services["postgres"]
        assert postgres.get("image") == "postgres:16-alpine", (
            "Postgres must use official postgres:16-alpine image"
        )

    def test_postgres_configuration_unchanged(
        self, nix_compose: dict, original_compose: dict
    ) -> None:
        """Postgres service configuration matches original."""
        nix_postgres = nix_compose.get("services", {}).get("postgres", {})
        original_postgres = original_compose.get("services", {}).get("postgres", {})

        # Check key configuration elements
        assert nix_postgres.get("environment") == original_postgres.get("environment")
        assert nix_postgres.get("ports") == original_postgres.get("ports")
        assert nix_postgres.get("healthcheck") == original_postgres.get("healthcheck")


class TestEnvironmentAndPorts:
    """Test that environment variables and port mappings match the original."""

    def test_all_environment_variables_match(
        self, nix_compose: dict, original_compose: dict
    ) -> None:
        """All environment variables and port mappings match the original docker-compose.yml."""
        nix_services = nix_compose.get("services", {})
        original_services = original_compose.get("services", {})

        for service_name in original_services:
            if service_name in nix_services:
                nix_service = nix_services[service_name]
                original_service = original_services[service_name]

                # Check environment variables
                nix_env = nix_service.get("environment", {})
                original_env = original_service.get("environment", {})
                assert nix_env == original_env, (
                    f"Environment for '{service_name}' must match original"
                )

                # Check env_file
                nix_env_file = nix_service.get("env_file")
                original_env_file = original_service.get("env_file")
                assert nix_env_file == original_env_file, (
                    f"env_file for '{service_name}' must match original"
                )

    def test_all_port_mappings_match(self, nix_compose: dict, original_compose: dict) -> None:
        """All port mappings match the original docker-compose.yml."""
        nix_services = nix_compose.get("services", {})
        original_services = original_compose.get("services", {})

        for service_name in original_services:
            if service_name in nix_services:
                nix_service = nix_services[service_name]
                original_service = original_services[service_name]

                nix_ports = nix_service.get("ports")
                original_ports = original_service.get("ports")
                assert nix_ports == original_ports, (
                    f"Ports for '{service_name}' must match original"
                )


class TestHealthchecksAndDependencies:
    """Test that healthchecks and dependencies are preserved."""

    def test_healthchecks_preserved(self, nix_compose: dict, original_compose: dict) -> None:
        """Healthchecks and dependencies are preserved."""
        nix_services = nix_compose.get("services", {})
        original_services = original_compose.get("services", {})

        for service_name in original_services:
            if service_name in nix_services:
                nix_service = nix_services[service_name]
                original_service = original_services[service_name]

                nix_healthcheck = nix_service.get("healthcheck")
                original_healthcheck = original_service.get("healthcheck")
                assert nix_healthcheck == original_healthcheck, (
                    f"Healthcheck for '{service_name}' must match original"
                )

    def test_dependencies_preserved(self, nix_compose: dict, original_compose: dict) -> None:
        """Dependencies are preserved."""
        nix_services = nix_compose.get("services", {})
        original_services = original_compose.get("services", {})

        for service_name in original_services:
            if service_name in nix_services:
                nix_service = nix_services[service_name]
                original_service = original_services[service_name]

                nix_depends = nix_service.get("depends_on")
                original_depends = original_service.get("depends_on")
                assert nix_depends == original_depends, (
                    f"depends_on for '{service_name}' must match original"
                )


class TestNixImageLoadingInstructions:
    """Test that the file includes instructions for loading Nix images."""

    def test_includes_nix_loading_instructions(self, nix_compose_path: Path) -> None:
        """Includes instructions or comments for loading Nix images before use."""
        content = nix_compose_path.read_text()

        # Check for comments about loading Nix images
        assert "#" in content, "File must contain comments"

        # Check for specific instructions about Nix images
        content_lower = content.lower()
        has_nix_instructions = any(
            [
                "nix" in content_lower and "load" in content_lower,
                "nix" in content_lower and "image" in content_lower,
                "docker load" in content_lower,
                "nix build" in content_lower,
            ]
        )

        assert has_nix_instructions, (
            "File must contain instructions or comments about loading Nix images"
        )
