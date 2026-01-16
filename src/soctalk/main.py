"""Main entry point for SocTalk SecOps Agent."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog
from rich.console import Console
from rich.panel import Panel

from soctalk.config import get_config, load_config
from soctalk.graph.builder import build_secops_graph, get_graph_visualization
from soctalk.graph.close import generate_closure_report
from soctalk.hil.service import HILService
from soctalk.hil.backends.slack import SlackHILBackend
from soctalk.mcp.bindings import bind_clients, cleanup_clients
from soctalk.notifications.slack_webhook import SlackWebhookNotifier, SlackNotificationSettings
from soctalk.settings_provider import (
    fetch_integration_settings,
    fetch_llm_settings,
    create_mcp_configs,
    IntegrationSettings,
    EnabledMCPServers,
    LLMSettings,
    is_settings_readonly,
    load_integration_secrets_from_env,
    load_integration_settings_from_env,
    load_llm_settings_from_env,
    seed_settings_from_env,
)
from soctalk.models.enums import Phase, InvestigationStatus
from soctalk.models.investigation import Investigation
from soctalk.models.state import create_initial_state
from soctalk.persistence import (
    init_db,
    close_db,
    get_async_session,
    get_checkpointer,
    get_checkpoint_config,
    EventEmitter,
)
from soctalk.polling.correlator import AlertCorrelator
from soctalk.polling.poller import AlertPoller
from soctalk.polling.queue import InvestigationQueue

# Configure Python logging to write to file
LOG_FILE = Path(__file__).parent.parent.parent / "soctalk.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="a"),
        logging.StreamHandler(sys.stderr),
    ],
)

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()
console = Console()


class SecOpsOrchestrator:
    """Main orchestrator for the SecOps agent.

    Coordinates:
    - Alert polling from Wazuh
    - Alert correlation
    - Investigation queue management
    - Graph execution for each investigation
    """

    def __init__(self):
        """Initialize the orchestrator."""
        self.config = get_config()
        self.poller = AlertPoller()
        self.correlator = AlertCorrelator()
        self.queue = InvestigationQueue()
        self.graph = None
        self.hil_service: Optional[HILService] = None
        self.slack_notifier: Optional[SlackWebhookNotifier] = None
        self._running = False
        self._stop_event = asyncio.Event()
        self._current_investigation: Optional[Investigation] = None
        self._db_enabled = False
        self._integration_settings: Optional[IntegrationSettings] = None
        self._llm_settings: Optional[LLMSettings] = None
        self._mcp_configs: Optional[EnabledMCPServers] = None

    async def start(self) -> None:
        """Start the orchestrator.

        Initializes MCP clients, HIL service, and starts the main loop.
        """
        logger.info("starting_secops_orchestrator")

        # Display banner
        self._display_banner()

        try:
            # Initialize database for event sourcing (optional)
            if self.config.database and self.config.database.enabled:
                console.print("[yellow]Connecting to PostgreSQL database...[/yellow]")
                try:
                    await init_db()
                    self._db_enabled = True
                    console.print("[green]Database connected (event sourcing enabled)[/green]")

                    # Load integration settings from database
                    console.print("[yellow]Loading integration settings from database...[/yellow]")
                    await self._load_integration_settings()

                except Exception as db_error:
                    logger.warning("database_init_failed", error=str(db_error))
                    console.print(f"[yellow]Database unavailable: {db_error}[/yellow]")
                    console.print("[dim]Continuing without event persistence[/dim]")
            else:
                console.print("[dim]Database not configured - event persistence disabled[/dim]")

            # Initialize MCP clients (using database settings if available)
            console.print("[yellow]Connecting to MCP servers...[/yellow]")
            if self._mcp_configs and self._mcp_configs.has_any_enabled:
                console.print(f"[dim]Using database settings ({self._mcp_configs.enabled_count} servers enabled)[/dim]")
                await bind_clients(self._mcp_configs)
            else:
                console.print("[dim]Using environment-based configuration[/dim]")
                await bind_clients()
            console.print("[green]MCP servers connected successfully![/green]")

            # Initialize Slack webhook notifier (from database settings)
            await self._init_slack_notifier()

            # Initialize HIL service based on config
            await self._init_hil_service()

            # Build the graph
            console.print("[yellow]Building LangGraph...[/yellow]")
            if self._db_enabled:
                async with get_checkpointer() as checkpointer:
                    self.graph = build_secops_graph(checkpointer=checkpointer)
                    console.print("[green]LangGraph ready![/green]")

                    self._running = True
                    await self._main_loop()
            else:
                self.graph = build_secops_graph()
                console.print("[green]LangGraph ready![/green]")

                self._running = True
                await self._main_loop()

        except KeyboardInterrupt:
            logger.info("keyboard_interrupt_received")
        except Exception as e:
            logger.error("orchestrator_error", error=str(e))
            raise
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stop the orchestrator gracefully."""
        logger.info("stopping_orchestrator")
        self._running = False
        self._stop_event.set()
        self.poller.stop()

        # Stop HIL service
        if self.hil_service:
            console.print("[yellow]Disconnecting HIL service...[/yellow]")
            await self.hil_service.stop()

        # Close Slack notifier
        if self.slack_notifier:
            console.print("[yellow]Closing Slack notifier...[/yellow]")
            await self.slack_notifier.close()

        # Cleanup MCP clients
        console.print("[yellow]Disconnecting MCP servers...[/yellow]")
        await cleanup_clients()

        # Close database connection
        if self._db_enabled:
            console.print("[yellow]Closing database connection...[/yellow]")
            await close_db()

        console.print("[green]Shutdown complete.[/green]")

    async def _init_hil_service(self) -> None:
        """Initialize the HIL service based on configuration."""
        hil_config = self.config.hil

        if not hil_config.enabled:
            console.print("[dim]HIL disabled - using CLI fallback[/dim]")
            return

        if hil_config.backend == "slack":
            # Check for required Slack config
            if not all([
                hil_config.slack_bot_token,
                hil_config.slack_app_token,
                hil_config.slack_channel,
            ]):
                console.print("[yellow]Slack HIL not configured - using CLI fallback[/yellow]")
                logger.warning("slack_hil_not_configured")
                return

            console.print("[yellow]Connecting to Slack HIL...[/yellow]")
            try:
                backend = SlackHILBackend(
                    bot_token=hil_config.slack_bot_token,
                    app_token=hil_config.slack_app_token,
                    default_channel=hil_config.slack_channel,
                    session_factory=get_async_session if self._db_enabled else None,  # For race condition prevention
                )
                self.hil_service = HILService(backend)
                await self.hil_service.start()
                console.print("[green]Slack HIL connected![/green]")
            except Exception as e:
                logger.error("slack_hil_init_failed", error=str(e))
                console.print(f"[red]Slack HIL failed: {e}[/red]")
                console.print("[yellow]Falling back to CLI[/yellow]")
                self.hil_service = None

        elif hil_config.backend == "discord":
            console.print("[yellow]Discord HIL not yet implemented - using CLI[/yellow]")

        elif hil_config.backend == "dashboard":
            console.print("[dim]Using dashboard for human review[/dim]")

        elif hil_config.backend == "cli":
            console.print("[dim]Using CLI for human review[/dim]")

        else:
            console.print(f"[yellow]Unknown HIL backend '{hil_config.backend}' - using CLI[/yellow]")

    async def _load_integration_settings(self) -> None:
        """Load integration settings from database and create MCP configs."""
        try:
            readonly = is_settings_readonly()
            async with get_async_session() as session:
                await seed_settings_from_env(session, overwrite=readonly)

                if readonly:
                    self._integration_settings = load_integration_settings_from_env()
                    self._llm_settings = load_llm_settings_from_env()
                else:
                    self._integration_settings = await fetch_integration_settings(session)
                    self._llm_settings = await fetch_llm_settings(session)

            if self._llm_settings is not None:
                self._apply_llm_settings(self._llm_settings)

            # Create MCP configs from settings
            self._mcp_configs = create_mcp_configs(self._integration_settings)

            # Log what was loaded
            enabled_servers = []
            if self._mcp_configs.wazuh:
                enabled_servers.append("wazuh")
            if self._mcp_configs.cortex:
                enabled_servers.append("cortex")
            if self._mcp_configs.thehive:
                enabled_servers.append("thehive")
            if self._mcp_configs.misp:
                enabled_servers.append("misp")

            logger.info(
                "integration_settings_loaded",
                wazuh_enabled=self._integration_settings.wazuh_enabled,
                cortex_enabled=self._integration_settings.cortex_enabled,
                thehive_enabled=self._integration_settings.thehive_enabled,
                misp_enabled=self._integration_settings.misp_enabled,
                slack_enabled=self._integration_settings.slack_enabled,
                enabled_mcp_servers=enabled_servers,
            )

            console.print(f"[green]Integration settings loaded (MCP: {enabled_servers})[/green]")

        except Exception as e:
            logger.error("integration_settings_load_failed", error=str(e))
            console.print(f"[yellow]Failed to load integration settings: {e}[/yellow]")
            console.print("[dim]Using environment-based configuration[/dim]")

    def _apply_llm_settings(self, settings: LLMSettings) -> None:
        """Apply DB-backed LLM preferences to the runtime config.

        Secrets (API keys) remain env-only and are kept from the original config.
        """
        provider = settings.llm_provider
        if provider not in ("anthropic", "openai"):
            logger.warning("invalid_llm_provider_in_settings", provider=provider)
            return

        if provider == "anthropic" and not self.config.llm.anthropic_api_key:
            logger.warning("llm_provider_requires_missing_api_key", provider=provider, key="ANTHROPIC_API_KEY")
            return
        if provider == "openai" and not self.config.llm.openai_api_key:
            logger.warning("llm_provider_requires_missing_api_key", provider=provider, key="OPENAI_API_KEY")
            return

        self.config.llm = self.config.llm.model_copy(
            update={
                "provider": provider,
                "fast_model": settings.llm_fast_model,
                "reasoning_model": settings.llm_reasoning_model,
                "temperature": settings.llm_temperature,
                "max_tokens": settings.llm_max_tokens,
                "anthropic_base_url": settings.llm_anthropic_base_url,
                "openai_base_url": settings.llm_openai_base_url,
                "openai_organization": settings.llm_openai_organization,
            }
        )

        logger.info(
            "llm_settings_applied",
            provider=provider,
            fast_model=settings.llm_fast_model,
            reasoning_model=settings.llm_reasoning_model,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )

    async def _init_slack_notifier(self) -> None:
        """Initialize Slack webhook notifier from integration settings."""
        if not self._integration_settings:
            console.print("[dim]Slack webhook notifications disabled (no integration settings)[/dim]")
            return

        if not self._integration_settings.slack_enabled:
            console.print("[dim]Slack webhook notifications disabled in settings[/dim]")
            return

        secrets = load_integration_secrets_from_env()
        if not secrets.slack_webhook_url:
            console.print("[yellow]Slack enabled but SLACK_WEBHOOK_URL is not set[/yellow]")
            return

        settings = SlackNotificationSettings(
            enabled=self._integration_settings.slack_enabled,
            webhook_url=secrets.slack_webhook_url,
            channel=self._integration_settings.slack_channel,
            notify_on_escalation=self._integration_settings.slack_notify_on_escalation,
            notify_on_verdict=self._integration_settings.slack_notify_on_verdict,
        )

        self.slack_notifier = SlackWebhookNotifier(settings)

        logger.info(
            "slack_webhook_notifier_initialized",
            notify_on_escalation=settings.notify_on_escalation,
            notify_on_verdict=settings.notify_on_verdict,
        )

        console.print(
            f"[green]Slack webhook notifications enabled "
            f"(escalation: {settings.notify_on_escalation}, verdict: {settings.notify_on_verdict})[/green]"
        )

    async def _main_loop(self) -> None:
        """Main processing loop.

        1. Do immediate poll on startup
        2. Start alert polling in background
        3. Process investigations one at a time
        """
        logger.info("starting_main_loop")

        console.print("\n[bold green]SecOps Agent is running![/bold green]")
        console.print(f"Polling interval: {self.config.polling.interval_seconds}s")
        console.print("Press Ctrl+C to stop.\n")

        # Do immediate poll on startup (don't wait for interval)
        console.print("[yellow]Performing initial alert poll...[/yellow]")
        initial_alerts = await self.poller.poll()
        await self._on_alerts_received(initial_alerts)

        # Start background polling task for subsequent polls
        polling_task = asyncio.create_task(
            self._delayed_polling()
        )
        resume_task: asyncio.Task | None = None
        if self._db_enabled:
            resume_task = asyncio.create_task(self._resume_loop())

        try:
            while self._running:
                # Get next investigation from queue (with timeout)
                investigation = await self.queue.get(timeout=5.0)

                if investigation:
                    await self._process_investigation(investigation)
                else:
                    # No investigation available, show status
                    stats = await self.queue.get_stats()
                    if stats["size"] > 0:
                        console.print(f"[dim]Queue: {stats['size']} investigations pending[/dim]")

        except asyncio.CancelledError:
            pass
        finally:
            polling_task.cancel()
            if resume_task:
                resume_task.cancel()
            try:
                await polling_task
            except asyncio.CancelledError:
                pass
            if resume_task:
                try:
                    await resume_task
                except asyncio.CancelledError:
                    pass

    async def _delayed_polling(self) -> None:
        """Run polling loop with initial delay (for subsequent polls after startup)."""
        try:
            while self._running and not self._stop_event.is_set():
                # Wait for interval first (initial poll already done)
                await asyncio.sleep(self.config.polling.interval_seconds)

                if self._stop_event.is_set():
                    break

                # Poll for alerts
                alerts = await self.poller.poll()
                await self._on_alerts_received(alerts)
        except asyncio.CancelledError:
            pass

    async def _resume_loop(self) -> None:
        """Resume interrupted investigations after dashboard decisions."""
        logger.info("resume_loop_started")
        try:
            while self._running and not self._stop_event.is_set():
                resumed = await self._resume_decided_reviews_once()
                await asyncio.sleep(0.5 if resumed else 1.5)
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("resume_loop_stopped")

    async def _resume_decided_reviews_once(self) -> int:
        """Resume a small batch of decided reviews.

        Returns:
            Number of investigations resumed.
        """
        if not self.graph:
            return 0

        from langgraph.types import Command
        from sqlalchemy import select

        from soctalk.persistence.models import InvestigationReadModel, PendingReview

        async with get_async_session() as session:
            stmt = (
                select(PendingReview)
                .where(
                    PendingReview.status.in_(("approved", "rejected", "info_requested")),
                    PendingReview.workflow_resumed_at.is_(None),
                )
                .order_by(PendingReview.responded_at)
                .limit(10)
            )
            result = await session.execute(stmt)
            reviews = result.scalars().all()
            if not reviews:
                return 0

            emitter = EventEmitter(session)
            resumed = 0
            for review in reviews:
                inv = await session.get(InvestigationReadModel, review.investigation_id)
                if inv and inv.status == "paused":
                    continue
                if inv and inv.status in ("cancelled", "closed", "auto_closed"):
                    review.workflow_resumed_at = datetime.utcnow()
                    continue

                graph_config = {"recursion_limit": 50, **get_checkpoint_config(str(review.investigation_id))}
                graph_config.setdefault("configurable", {})
                graph_config["configurable"]["event_emitter"] = emitter
                graph_config["configurable"]["hil_backend"] = self.config.hil.backend
                if self.hil_service:
                    graph_config["configurable"]["hil_service"] = self.hil_service

                try:
                    snapshot = await self.graph.aget_state(graph_config)
                except Exception as e:
                    logger.warning(
                        "resume_get_state_failed",
                        investigation_id=str(review.investigation_id),
                        error=str(e),
                    )
                    continue

                if not snapshot.interrupts:
                    review.workflow_resumed_at = datetime.utcnow()
                    continue

                interrupt_value = snapshot.interrupts[0].value
                if isinstance(interrupt_value, dict) and interrupt_value.get("type") not in (None, "human_review"):
                    logger.info(
                        "resume_skipped_unknown_interrupt",
                        investigation_id=str(review.investigation_id),
                        interrupt_type=interrupt_value.get("type"),
                    )
                    continue

                decision = {
                    "approved": "approve",
                    "rejected": "reject",
                    "info_requested": "more_info",
                }.get(review.status, "more_info")
                payload = {
                    "decision": decision,
                    "feedback": review.feedback,
                    "reviewer": review.reviewer,
                    "source": "dashboard",
                }

                try:
                    await self.graph.ainvoke(Command(resume=payload), config=graph_config)
                except Exception as e:
                    logger.error(
                        "resume_investigation_failed",
                        review_id=str(review.id),
                        investigation_id=str(review.investigation_id),
                        error=str(e),
                    )
                    continue

                review.workflow_resumed_at = datetime.utcnow()
                resumed += 1

            return resumed

    async def _on_alerts_received(self, alerts: list) -> None:
        """Handle new alerts from poller.

        Args:
            alerts: List of new Alert objects.
        """
        if not alerts:
            console.print("[dim]No new alerts found[/dim]")
            return

        logger.info("alerts_received", count=len(alerts))
        console.print(f"\n[yellow]Received {len(alerts)} new alert(s)[/yellow]")

        # Correlate alerts into investigations
        investigations = self.correlator.correlate(alerts)

        # Add to queue
        added = await self.queue.add_batch(investigations)
        console.print(f"[green]Queued {added} investigation(s)[/green]")

    async def _process_investigation(self, investigation: Investigation) -> None:
        """Process a single investigation through the graph.

        Args:
            investigation: Investigation to process.
        """
        self._current_investigation = investigation

        console.print("\n")
        console.print(
            Panel(
                f"[bold]Processing Investigation[/bold]\n\n"
                f"ID: {investigation.id}\n"
                f"Title: {investigation.title}\n"
                f"Alerts: {len(investigation.alerts)}\n"
                f"Severity: {investigation.max_severity.value.upper()}",
                border_style="cyan",
            )
        )

        try:
            # Create initial state
            initial_state = create_initial_state(investigation)

            # Run the graph with optional event sourcing
            logger.info(
                "starting_investigation",
                investigation_id=investigation.id,
                alerts=len(investigation.alerts),
                hil_backend=self.config.hil.backend,
                event_sourcing=self._db_enabled,
            )

            if self._db_enabled:
                # Run with event sourcing enabled
                final_state = await self._run_with_event_sourcing(investigation, initial_state)
            else:
                # Run without event sourcing
                final_state = await self.graph.ainvoke(
                    initial_state,
                    config={"recursion_limit": 50},
                )

            if isinstance(final_state, dict) and "__interrupt__" in final_state:
                logger.info(
                    "investigation_interrupted",
                    investigation_id=investigation.id,
                    interrupts=len(final_state.get("__interrupt__") or []),
                )
                console.print(
                    "[yellow]Investigation paused awaiting human review (use the dashboard to decide).[/yellow]"
                )
                return

            # Log completion
            final_investigation = final_state.get("investigation", {})
            status = final_investigation.get("status", "unknown")
            case_id = final_investigation.get("thehive_case_id")

            logger.info(
                "investigation_completed",
                investigation_id=investigation.id,
                status=status,
                case_id=case_id,
            )

            # Mark investigation as completed in queue (allows similar titles again)
            if self.queue:
                self.queue.mark_completed(investigation.id, investigation.title)

            # Display closure report
            report = generate_closure_report(final_state)
            console.print("\n")
            console.print(report)

        except Exception as e:
            logger.error(
                "investigation_failed",
                investigation_id=investigation.id,
                error=str(e),
            )
            console.print(f"\n[red]Investigation failed: {e}[/red]")

        finally:
            self._current_investigation = None

    async def _run_with_event_sourcing(
        self,
        investigation: Investigation,
        initial_state: dict,
    ) -> dict:
        """Run investigation with event sourcing enabled.

        Creates a database session and EventEmitter, injects it into state,
        and emits business events throughout the investigation lifecycle.

        Args:
            investigation: The investigation to process.
            initial_state: Initial graph state.

        Returns:
            Final state after graph execution.
        """
        from uuid import UUID

        async with get_async_session() as session:
            # Create event emitter for this investigation
            emitter = EventEmitter(session)

            # Inject emitter into state
            # Get investigation ID as UUID
            inv_id = investigation.id
            if isinstance(inv_id, str):
                inv_id = UUID(inv_id)

            # Emit investigation created event
            try:
                await emitter.emit_investigation_created(
                    investigation_id=inv_id,
                    title=investigation.title,
                    alert_ids=[str(a.id) for a in investigation.alerts],
                    max_severity=investigation.max_severity.value,
                    idempotency_key=f"inv-created-{inv_id}",
                )

                await emitter.emit_investigation_started(
                    investigation_id=inv_id,
                    title=investigation.title,
                    idempotency_key=f"inv-started-{inv_id}",
                )

                # Emit alert correlated events for each alert
                for alert in investigation.alerts:
                    await emitter.emit_alert_correlated(
                        investigation_id=inv_id,
                        alert_id=str(alert.id),
                        rule_id=alert.rule_id,
                        rule_description=alert.rule_description,
                        severity=alert.severity.value,
                        observable_count=len(alert.observables),
                    )

                # Emit observable extracted events for each observable
                seen_observables = set()
                for alert in investigation.alerts:
                    for obs in alert.observables:
                        obs_key = f"{obs.type.value}:{obs.value}"
                        if obs_key not in seen_observables:
                            seen_observables.add(obs_key)
                            await emitter.emit_observable_extracted(
                                investigation_id=inv_id,
                                observable_type=obs.type.value,
                                observable_value=obs.value[:200],  # Truncate long values
                                source=f"alert:{alert.id}",
                            )

                await session.commit()
            except Exception as emit_error:
                logger.warning("initial_event_emission_failed", error=str(emit_error))
                await session.rollback()

            # Run the graph
            graph_config = {"recursion_limit": 50, **get_checkpoint_config(str(inv_id))}
            graph_config.setdefault("configurable", {})
            graph_config["configurable"]["event_emitter"] = emitter
            graph_config["configurable"]["hil_backend"] = self.config.hil.backend
            if self.hil_service:
                graph_config["configurable"]["hil_service"] = self.hil_service

            final_state = await self.graph.ainvoke(
                initial_state,
                config=graph_config,
            )

            # Final commit for any remaining events
            try:
                await session.commit()
            except Exception as commit_error:
                logger.warning("final_commit_failed", error=str(commit_error))

            return final_state

    def _display_banner(self) -> None:
        """Display startup banner."""
        banner = """
   ____             _____     _ _
  / ___|  ___   ___|_   _|_ _| | | __
  \___ \ / _ \ / __| | |/ _` | | |/ /
   ___) | (_) | (__  | | (_| | |   <
  |____/ \___/ \___| |_|\__,_|_|_|\_\\

  SecOps LLM Agent - Powered by LangGraph
  ========================================
        """
        console.print(banner, style="bold cyan")
        console.print(f"Version: 0.1.0")
        console.print(f"Fast Model: {self.config.llm.fast_model}")
        console.print(f"Reasoning Model: {self.config.llm.reasoning_model}")
        console.print(f"HIL Backend: {self.config.hil.backend}")
        console.print()


async def run_single_investigation(investigation: Investigation) -> dict:
    """Run a single investigation (for testing/manual use).

    Args:
        investigation: Investigation to process.

    Returns:
        Final state dictionary.
    """
    logger.info("running_single_investigation", investigation_id=investigation.id)

    # Initialize MCP clients
    await bind_clients()

    try:
        # Build graph
        graph = build_secops_graph()

        # Create initial state
        initial_state = create_initial_state(investigation)

        # Run
        final_state = await graph.ainvoke(initial_state)

        return final_state

    finally:
        await cleanup_clients()


def main() -> None:
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="SocTalk - SecOps LLM Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--graph",
        action="store_true",
        help="Display graph visualization and exit",
    )
    parser.add_argument(
        "--config",
        type=str,
        help="Path to .env config file",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    # Load config
    if args.config:
        from pathlib import Path
        load_config(Path(args.config))

    # Show graph and exit
    if args.graph:
        print(get_graph_visualization())
        return

    # Set up signal handlers
    def handle_sigterm(sig, frame):
        logger.info("sigterm_received")
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, handle_sigterm)

    # Run orchestrator
    orchestrator = SecOpsOrchestrator()

    try:
        asyncio.run(orchestrator.start())
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down...[/yellow]")


if __name__ == "__main__":
    main()
