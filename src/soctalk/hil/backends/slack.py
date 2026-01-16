"""Slack HIL backend using slack-bolt with Socket Mode."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Callable, Optional, TYPE_CHECKING
from uuid import UUID

import structlog
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.hil.base import HILBackend, HILConnectionError, HILTimeoutError
from soctalk.hil.models import HILRequest, HILResponse
from soctalk.models.enums import HumanDecision

logger = structlog.get_logger()


class SlackHILBackend(HILBackend):
    """Slack HIL backend using Socket Mode for two-way communication.

    Uses slack-bolt library with async support and Socket Mode to enable
    outbound-only connections (no public URL required).

    Supports conversational HIL via modal dialogs for user inquiries.
    """

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        default_channel: str,
        session_factory: Optional[Callable[[], AsyncSession]] = None,
    ):
        """Initialize Slack HIL backend.

        Args:
            bot_token: Slack Bot User OAuth Token (xoxb-...).
            app_token: Slack App-Level Token with connections:write (xapp-...).
            default_channel: Default channel ID for HIL messages.
            session_factory: Optional async context manager for database sessions.
                            Used to check PendingReview status and prevent race conditions.
        """
        self._bot_token = bot_token
        self._app_token = app_token
        self._default_channel = default_channel
        self._session_factory = session_factory

        self._app: Optional[AsyncApp] = None
        self._handler: Optional[AsyncSocketModeHandler] = None
        self._connected = False
        self._handler_task: Optional[asyncio.Task] = None

        # Pending approvals: investigation_id -> (Future, sent_timestamp, channel, message_ts, state, conversation_history)
        self._pending: dict[str, tuple[asyncio.Future, float, str, str, dict, list]] = {}

        # Inquiry handler callback (set by HIL service)
        # Signature: async (investigation_id, inquiry, state, conversation_history) -> response string
        self._inquiry_handler: Optional[Callable] = None

    @property
    def name(self) -> str:
        return "slack"

    @property
    def is_connected(self) -> bool:
        return self._connected

    def set_inquiry_handler(self, handler: Callable) -> None:
        """Set the callback for handling user inquiries.

        Args:
            handler: Async callback that takes (investigation_id, inquiry, channel, thread_ts)
                     and returns the response string.
        """
        self._inquiry_handler = handler

    async def start(self) -> None:
        """Start the Slack Socket Mode connection."""
        if self._connected:
            logger.debug("slack_hil_already_connected")
            return

        logger.info("slack_hil_starting")

        try:
            # Create the Slack app
            self._app = AsyncApp(token=self._bot_token)

            # Register action handlers
            self._register_handlers()

            # Create Socket Mode handler
            self._handler = AsyncSocketModeHandler(self._app, self._app_token)

            # Start handler in background task
            self._handler_task = asyncio.create_task(self._handler.start_async())

            # Give it a moment to connect
            await asyncio.sleep(1)

            self._connected = True
            logger.info("slack_hil_connected")

        except Exception as e:
            logger.error("slack_hil_connection_failed", error=str(e))
            raise HILConnectionError(f"Failed to connect to Slack: {e}") from e

    async def stop(self) -> None:
        """Stop the Slack connection."""
        if not self._connected:
            return

        logger.info("slack_hil_stopping")

        # Cancel any pending requests
        for inv_id, pending_data in self._pending.items():
            future = pending_data[0]
            if not future.done():
                future.cancel()
        self._pending.clear()

        # Mark as disconnected first to prevent new operations
        self._connected = False

        # Give pending operations a moment to complete
        await asyncio.sleep(0.5)

        # Stop the handler
        if self._handler:
            try:
                await self._handler.close_async()
            except Exception as e:
                logger.debug("slack_hil_close_warning", error=str(e))

        if self._handler_task:
            self._handler_task.cancel()
            try:
                await self._handler_task
            except asyncio.CancelledError:
                pass

        logger.info("slack_hil_stopped")

    def _register_handlers(self) -> None:
        """Register Slack action handlers for buttons and modals."""
        if not self._app:
            return

        @self._app.action("hil_approve")
        async def handle_approve(ack: Any, body: dict, client: Any) -> None:
            await ack()
            await self._handle_decision(body, HumanDecision.APPROVE, client)

        @self._app.action("hil_reject")
        async def handle_reject(ack: Any, body: dict, client: Any) -> None:
            await ack()
            await self._handle_decision(body, HumanDecision.REJECT, client)

        @self._app.action("hil_more_info")
        async def handle_more_info(ack: Any, body: dict, client: Any) -> None:
            """Open modal for user to enter their inquiry."""
            await ack()
            await self._open_inquiry_modal(body, client)

        @self._app.view("hil_inquiry_modal")
        async def handle_inquiry_submission(ack: Any, body: dict, client: Any, view: dict) -> None:
            """Handle inquiry modal submission."""
            await ack()
            await self._handle_inquiry_submission(body, view, client)

        @self._app.event("message")
        async def handle_thread_message(event: dict, client: Any) -> None:
            """Handle follow-up messages in investigation threads."""
            await self._handle_thread_reply(event, client)

    async def _handle_decision(
        self,
        body: dict,
        decision: HumanDecision,
        client: Any,
    ) -> None:
        """Handle a button click decision."""
        try:
            # Extract investigation ID from action value
            action = body.get("actions", [{}])[0]
            inv_id = action.get("value", "")
            user = body.get("user", {}).get("name", "unknown")
            user_id = body.get("user", {}).get("id", "")

            logger.info(
                "slack_hil_decision_received",
                investigation_id=inv_id,
                decision=decision.value,
                user=user,
            )

            # Check if decision was already made via dashboard (race condition prevention)
            if self._session_factory:
                already_decided = await self._check_already_decided(inv_id)
                if already_decided:
                    logger.warning(
                        "slack_hil_decision_race_condition",
                        investigation_id=inv_id,
                        already_status=already_decided,
                    )
                    channel = body.get("channel", {}).get("id")
                    if channel:
                        await client.chat_postEphemeral(
                            channel=channel,
                            user=user_id,
                            text=f":warning: This investigation was already decided via the dashboard (status: {already_decided}). Your action was not applied.",
                        )
                    # Clean up pending if it exists
                    if inv_id in self._pending:
                        future, _, _, _, _, _ = self._pending[inv_id]
                        if not future.done():
                            # Set a "already decided" response so the workflow can continue
                            future.set_result(
                                HILResponse(
                                    investigation_id=inv_id,
                                    decision=HumanDecision.REJECT if already_decided == "rejected" else HumanDecision.APPROVE,
                                    reviewer="dashboard",
                                    feedback=f"Decision made via dashboard: {already_decided}",
                                    response_time_seconds=0,
                                    backend="dashboard",
                                )
                            )
                        del self._pending[inv_id]
                    # Update message to show it was decided elsewhere
                    await self._update_message_already_decided(body, already_decided, client)
                    return

            if inv_id in self._pending:
                future, sent_ts, channel, message_ts, state, conv_history = self._pending[inv_id]
                response_time = datetime.now().timestamp() - sent_ts

                if not future.done():
                    future.set_result(
                        HILResponse(
                            investigation_id=inv_id,
                            decision=decision,
                            reviewer=user,
                            feedback=None,
                            response_time_seconds=response_time,
                            backend="slack",
                        )
                    )

                del self._pending[inv_id]

                # Update the message to show decision was recorded
                await self._update_message_with_decision(
                    body, decision, user, client
                )
            else:
                logger.warning(
                    "slack_hil_unknown_investigation",
                    investigation_id=inv_id,
                )
                # Still acknowledge to user
                channel = body.get("channel", {}).get("id")
                if channel:
                    await client.chat_postEphemeral(
                        channel=channel,
                        user=user_id,
                        text=f"This investigation ({inv_id}) is no longer pending review.",
                    )

        except Exception as e:
            logger.error("slack_hil_decision_error", error=str(e))

    async def _check_already_decided(self, investigation_id: str) -> Optional[str]:
        """Check if a decision was already made for this investigation via dashboard.

        Args:
            investigation_id: The investigation ID to check.

        Returns:
            The status if already decided (e.g., 'approved', 'rejected'), None if still pending.
        """
        if not self._session_factory:
            return None

        try:
            from soctalk.persistence.models import PendingReview

            async with self._session_factory() as session:
                # Query by investigation_id since that's what we have from Slack
                query = select(PendingReview).where(
                    PendingReview.investigation_id == UUID(investigation_id)
                )
                result = await session.execute(query)
                review = result.scalar_one_or_none()

                if review and review.status != "pending":
                    return review.status

            return None
        except Exception as e:
            logger.warning("slack_hil_db_check_failed", error=str(e))
            return None

    async def _wait_for_decision_with_polling(
        self,
        investigation_id: str,
        future: asyncio.Future[HILResponse],
        timeout: float,
        channel: str,
        message_ts: str,
    ) -> HILResponse:
        """Wait for a decision from either Slack button or dashboard with polling.

        This method polls the database periodically to check if a decision
        was made via the dashboard, while also waiting for the Slack button click.
        Whichever comes first wins.

        Args:
            investigation_id: The investigation ID.
            future: The Future that will be resolved by Slack button click.
            timeout: Maximum time to wait in seconds.
            channel: The Slack channel ID.
            message_ts: The Slack message timestamp for updating.

        Returns:
            HILResponse with the decision.

        Raises:
            asyncio.TimeoutError: If no decision within timeout.
        """
        poll_interval = 5.0  # Check database every 5 seconds
        elapsed = 0.0
        sent_ts = datetime.now().timestamp()

        while elapsed < timeout:
            # Wait for either the Future or poll interval
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=min(poll_interval, timeout - elapsed),
                )
                # Future was resolved by Slack button click
                return result
            except asyncio.TimeoutError:
                # Poll interval elapsed, check database
                pass

            elapsed = datetime.now().timestamp() - sent_ts

            # Check if decision was made via dashboard
            if self._session_factory:
                status = await self._check_already_decided(investigation_id)
                if status:
                    logger.info(
                        "slack_hil_dashboard_decision_detected",
                        investigation_id=investigation_id,
                        status=status,
                    )

                    # Map status to HumanDecision
                    if status == "approved":
                        decision = HumanDecision.APPROVE
                    elif status == "rejected":
                        decision = HumanDecision.REJECT
                    else:
                        decision = HumanDecision.MORE_INFO

                    # Clean up pending
                    if investigation_id in self._pending:
                        del self._pending[investigation_id]

                    # Update Slack message to show dashboard decision
                    await self._update_slack_message_for_dashboard_decision(
                        channel, message_ts, status
                    )

                    return HILResponse(
                        investigation_id=investigation_id,
                        decision=decision,
                        reviewer="dashboard",
                        feedback=f"Decision made via dashboard: {status}",
                        response_time_seconds=elapsed,
                        backend="dashboard",
                    )

        # Timeout reached
        raise asyncio.TimeoutError()

    async def _update_slack_message_for_dashboard_decision(
        self,
        channel: str,
        message_ts: str,
        status: str,
    ) -> None:
        """Update Slack message when decision was made via dashboard."""
        if not self._app:
            return

        try:
            # Get the original message
            result = await self._app.client.conversations_history(
                channel=channel,
                latest=message_ts,
                limit=1,
                inclusive=True,
            )

            messages = result.get("messages", [])
            if not messages:
                return

            original_blocks = messages[0].get("blocks", [])

            # Remove action buttons and add decision block
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({"type": "divider"})
            updated_blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":desktop_computer: *Decision:* {status.upper()} (via Dashboard)\n"
                            f"*Time:* <!date^{int(datetime.now().timestamp())}^{{date_short_pretty}} at {{time}}|{datetime.now().isoformat()}>",
                },
            })

            await self._app.client.chat_update(
                channel=channel,
                ts=message_ts,
                blocks=updated_blocks,
                text=f"Investigation {status} via dashboard",
            )

            logger.info(
                "slack_hil_message_updated_for_dashboard",
                channel=channel,
                message_ts=message_ts,
                status=status,
            )

        except Exception as e:
            logger.warning(
                "slack_hil_update_for_dashboard_failed",
                error=str(e),
                channel=channel,
                message_ts=message_ts,
            )

    async def _update_message_already_decided(
        self,
        body: dict,
        status: str,
        client: Any,
    ) -> None:
        """Update the Slack message to show it was decided via another channel."""
        channel = body.get("channel", {}).get("id")
        message_ts = body.get("message", {}).get("ts")

        if not channel or not message_ts:
            return

        # Get original blocks and append decision
        original_blocks = body.get("message", {}).get("blocks", [])

        # Remove action buttons and add decision block
        updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
        updated_blocks.append({"type": "divider"})
        updated_blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":desktop_computer: *Decision:* {status.upper()} (via Dashboard)\n"
                        f"*Time:* <!date^{int(datetime.now().timestamp())}^{{date_short_pretty}} at {{time}}|{datetime.now().isoformat()}>",
            },
        })

        try:
            await client.chat_update(
                channel=channel,
                ts=message_ts,
                blocks=updated_blocks,
                text=f"Investigation {status} via dashboard",
            )
        except Exception as e:
            logger.warning("slack_hil_update_message_failed", error=str(e))

    async def _open_inquiry_modal(self, body: dict, client: Any) -> None:
        """Open modal for user to enter their inquiry about the investigation."""
        try:
            action = body.get("actions", [{}])[0]
            inv_id = action.get("value", "")
            trigger_id = body.get("trigger_id")
            channel = body.get("channel", {}).get("id", "")
            message_ts = body.get("message", {}).get("ts", "")

            if not trigger_id:
                logger.error("slack_hil_no_trigger_id")
                return

            logger.info(
                "slack_hil_opening_inquiry_modal",
                investigation_id=inv_id,
            )

            # Store context in private_metadata for the modal submission
            private_metadata = json.dumps({
                "investigation_id": inv_id,
                "channel": channel,
                "thread_ts": message_ts,
            })

            await client.views_open(
                trigger_id=trigger_id,
                view={
                    "type": "modal",
                    "callback_id": "hil_inquiry_modal",
                    "private_metadata": private_metadata,
                    "title": {
                        "type": "plain_text",
                        "text": "Ask About Investigation",
                    },
                    "submit": {
                        "type": "plain_text",
                        "text": "Submit",
                    },
                    "close": {
                        "type": "plain_text",
                        "text": "Cancel",
                    },
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*Investigation:* `{inv_id[:8]}...`\n\nAsk a question and the AI will investigate and respond in the thread.",
                            },
                        },
                        {
                            "type": "input",
                            "block_id": "inquiry_block",
                            "label": {
                                "type": "plain_text",
                                "text": "Your Question",
                            },
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "inquiry_input",
                                "multiline": True,
                                "placeholder": {
                                    "type": "plain_text",
                                    "text": "e.g., What processes were running on the affected host? What other IPs did this attacker contact?",
                                },
                            },
                        },
                    ],
                },
            )

        except Exception as e:
            logger.error("slack_hil_open_modal_error", error=str(e))

    async def _handle_thread_reply(self, event: dict, client: Any) -> None:
        """Handle follow-up messages posted directly in investigation threads.

        This enables conversational HIL where users can ask multiple questions
        in the thread without using the modal each time.
        """
        try:
            # Only process thread replies (not parent messages)
            thread_ts = event.get("thread_ts")
            if not thread_ts:
                return

            # Ignore bot messages to avoid responding to ourselves
            if event.get("bot_id") or event.get("subtype") == "bot_message":
                return

            channel = event.get("channel", "")
            user_id = event.get("user", "")
            inquiry = event.get("text", "")

            if not inquiry:
                return

            # Find the investigation associated with this thread
            inv_id = None
            state = {}
            conv_history = []

            for investigation_id, pending_data in self._pending.items():
                _, _, pending_channel, msg_ts, pending_state, pending_history = pending_data
                if pending_channel == channel and msg_ts == thread_ts:
                    inv_id = investigation_id
                    state = pending_state
                    conv_history = pending_history
                    break

            if not inv_id:
                # This thread isn't associated with a pending investigation
                logger.debug(
                    "slack_hil_thread_not_found",
                    channel=channel,
                    thread_ts=thread_ts,
                )
                return

            # Get user info for the name
            try:
                user_info = await client.users_info(user=user_id)
                user_name = user_info.get("user", {}).get("name", user_id)
            except Exception:
                user_name = user_id

            logger.info(
                "slack_hil_thread_inquiry_received",
                investigation_id=inv_id,
                inquiry_preview=inquiry[:50],
                user=user_name,
            )

            # Post thinking indicator
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":thinking_face: Investigating your question...",
            )

            # Call the inquiry handler if set
            if self._inquiry_handler:
                try:
                    response = await self._inquiry_handler(
                        inv_id, inquiry, state, conv_history
                    )

                    # Store this Q&A exchange in conversation history
                    conv_history.append({
                        "question": inquiry,
                        "answer": response,
                        "user": user_name,
                    })

                    # Update the pending tuple with new conversation history
                    if inv_id in self._pending:
                        future, sent_ts, ch, msg_ts, st, _ = self._pending[inv_id]
                        self._pending[inv_id] = (future, sent_ts, ch, msg_ts, st, conv_history)

                    # Post the response in thread
                    await client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f":robot_face: {response}",
                    )

                except Exception as e:
                    logger.error("slack_hil_thread_inquiry_error", error=str(e))
                    await client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f":warning: Error processing your question: {str(e)}",
                    )
            else:
                logger.warning("slack_hil_no_inquiry_handler_for_thread")

        except Exception as e:
            logger.error("slack_hil_thread_reply_error", error=str(e))

    async def _handle_inquiry_submission(
        self,
        body: dict,
        view: dict,
        client: Any,
    ) -> None:
        """Handle inquiry modal submission - process the question."""
        try:
            # Extract data from modal
            private_metadata = json.loads(view.get("private_metadata", "{}"))
            inv_id = private_metadata.get("investigation_id", "")
            channel = private_metadata.get("channel", "")
            thread_ts = private_metadata.get("thread_ts", "")
            user = body.get("user", {}).get("name", "unknown")

            # Get the inquiry text - note: view structure in slack-bolt
            values = view.get("state", {}).get("values", {})
            inquiry = values.get("inquiry_block", {}).get("inquiry_input", {}).get("value", "")

            logger.debug(
                "slack_hil_modal_values",
                view_keys=list(view.keys()),
                values=values,
            )

            if not inquiry:
                logger.warning("slack_hil_empty_inquiry", values=values)
                return

            logger.info(
                "slack_hil_inquiry_received",
                investigation_id=inv_id,
                inquiry_preview=inquiry[:50],
                user=user,
            )

            # Post acknowledgment in thread
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":thinking_face: *{user}* asked: _{inquiry}_\n\nInvestigating...",
            )

            # Get state and conversation history from pending
            state = {}
            conv_history = []
            if inv_id in self._pending:
                _, _, _, _, state, conv_history = self._pending[inv_id]

            # Call the inquiry handler if set
            if self._inquiry_handler:
                try:
                    response = await self._inquiry_handler(
                        inv_id, inquiry, state, conv_history
                    )

                    # Store this Q&A exchange in conversation history
                    conv_history.append({
                        "question": inquiry,
                        "answer": response,
                        "user": user,
                    })

                    # Update the pending tuple with new conversation history
                    if inv_id in self._pending:
                        future, sent_ts, ch, msg_ts, st, _ = self._pending[inv_id]
                        self._pending[inv_id] = (future, sent_ts, ch, msg_ts, st, conv_history)

                    # Post the response in thread
                    await client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f":robot_face: *Investigation Response:*\n\n{response}",
                    )

                except Exception as e:
                    logger.error("slack_hil_inquiry_handler_error", error=str(e))
                    await client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f":warning: Error processing inquiry: {str(e)}",
                    )
            else:
                logger.warning("slack_hil_no_inquiry_handler")
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=":warning: Inquiry handler not configured. Please review manually.",
                )

        except Exception as e:
            logger.error("slack_hil_inquiry_submission_error", error=str(e))

    async def _update_message_with_decision(
        self,
        body: dict,
        decision: HumanDecision,
        reviewer: str,
        client: Any,
    ) -> None:
        """Update the original message to show the decision."""
        channel = body.get("channel", {}).get("id")
        message_ts = body.get("message", {}).get("ts")

        if not channel or not message_ts:
            return

        decision_emoji = {
            HumanDecision.APPROVE: ":white_check_mark:",
            HumanDecision.REJECT: ":x:",
            HumanDecision.MORE_INFO: ":question:",
        }

        decision_text = {
            HumanDecision.APPROVE: "APPROVED - Escalating to TheHive",
            HumanDecision.REJECT: "REJECTED - Closing as false positive",
            HumanDecision.MORE_INFO: "MORE INFO REQUESTED - Continuing investigation",
        }

        # Get original blocks and append decision
        original_blocks = body.get("message", {}).get("blocks", [])

        # Remove action buttons and add decision block
        updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
        updated_blocks.append({"type": "divider"})
        updated_blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{decision_emoji.get(decision, '')} *Decision:* {decision_text.get(decision, decision.value)}\n"
                        f"*Reviewer:* {reviewer}\n"
                        f"*Time:* <!date^{int(datetime.now().timestamp())}^{{date_short_pretty}} at {{time}}|{datetime.now().isoformat()}>",
            },
        })

        try:
            await client.chat_update(
                channel=channel,
                ts=message_ts,
                blocks=updated_blocks,
                text=f"Investigation {decision.value} by {reviewer}",
            )
        except Exception as e:
            logger.warning("slack_hil_update_message_failed", error=str(e))

    async def request_approval(
        self,
        request: HILRequest,
        timeout: Optional[float] = None,
        state: Optional[dict[str, Any]] = None,
    ) -> HILResponse:
        """Send investigation to Slack and wait for human decision."""
        if not self._connected:
            raise HILConnectionError("Slack backend not connected")

        timeout = timeout or request.timeout_seconds
        channel = request.channel or self._default_channel

        logger.info(
            "slack_hil_requesting_approval",
            investigation_id=request.investigation_id,
            channel=channel,
        )

        # Build Slack Block Kit message
        blocks = self._build_message_blocks(request)

        # Create future to wait on
        future: asyncio.Future[HILResponse] = asyncio.Future()
        sent_ts = datetime.now().timestamp()

        try:
            # Send the message and capture the message_ts for threading
            result = await self._app.client.chat_postMessage(
                channel=channel,
                blocks=blocks,
                text=f"Investigation requires review: {request.title}",
            )

            message_ts = result.get("ts", "")

            # Store with channel, message_ts, state, and empty conversation history
            self._pending[request.investigation_id] = (
                future, sent_ts, channel, message_ts, state or {}, []
            )

            logger.debug(
                "slack_hil_message_sent",
                investigation_id=request.investigation_id,
                message_ts=message_ts,
            )

            # Wait for decision from either Slack button or dashboard
            response = await self._wait_for_decision_with_polling(
                request.investigation_id, future, timeout, channel, message_ts
            )
            return response

        except asyncio.TimeoutError:
            # Clean up pending request
            if request.investigation_id in self._pending:
                del self._pending[request.investigation_id]

            logger.warning(
                "slack_hil_timeout",
                investigation_id=request.investigation_id,
                timeout=timeout,
            )
            raise HILTimeoutError(
                f"No response received within {timeout} seconds"
            )

        except Exception as e:
            # Clean up pending request
            if request.investigation_id in self._pending:
                del self._pending[request.investigation_id]

            logger.error(
                "slack_hil_request_failed",
                investigation_id=request.investigation_id,
                error=str(e),
            )
            raise HILConnectionError(f"Failed to request approval: {e}") from e

    def _build_message_blocks(self, request: HILRequest) -> list[dict]:
        """Build Slack Block Kit blocks for the investigation message."""
        severity_emoji = {
            "low": ":large_green_circle:",
            "medium": ":large_yellow_circle:",
            "high": ":large_orange_circle:",
            "critical": ":red_circle:",
        }

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": ":rotating_light: Investigation Requires Review",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Investigation ID:*\n`{request.investigation_id}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Severity:*\n{severity_emoji.get(request.max_severity.value, ':white_circle:')} {request.max_severity.value.upper()}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Alerts:*\n{request.alert_count}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Created:*\n<!date^{int(request.created_at.timestamp())}^{{date_short_pretty}} at {{time}}|{request.created_at.isoformat()}>",
                    },
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Title:*\n{request.title}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Description:*\n{request.description[:500]}{'...' if len(request.description) > 500 else ''}",
                },
            },
        ]

        # Threat Intel summary
        if request.malicious_count or request.suspicious_count:
            intel_text = "*Threat Intelligence:*\n"
            if request.malicious_count:
                intel_text += f":red_circle: Malicious: {request.malicious_count}\n"
            if request.suspicious_count:
                intel_text += f":large_yellow_circle: Suspicious: {request.suspicious_count}\n"
            if request.clean_count:
                intel_text += f":large_green_circle: Clean: {request.clean_count}\n"

            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": intel_text},
            })

        # Enrichment details (Cortex results)
        if request.enrichments:
            verdict_emoji = {
                "malicious": ":red_circle:",
                "suspicious": ":large_yellow_circle:",
                "benign": ":large_green_circle:",
                "unknown": ":white_circle:",
            }

            enrichments_text = "*Enrichment Results (Cortex):*\n"
            for enrichment in request.enrichments[:5]:  # Limit to 5 to avoid message size limits
                emoji = verdict_emoji.get(enrichment.verdict.lower(), ":white_circle:")
                enrichments_text += (
                    f"{emoji} `{enrichment.observable_value}` "
                    f"({enrichment.observable_type})\n"
                    f"    Analyzer: _{enrichment.analyzer}_ | "
                    f"Verdict: *{enrichment.verdict}* | "
                    f"Confidence: {enrichment.confidence:.0%}\n"
                )
            if len(request.enrichments) > 5:
                enrichments_text += f"_...and {len(request.enrichments) - 5} more enrichments_\n"

            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": enrichments_text},
            })

        # MISP Threat Intelligence Context
        if request.misp_context:
            misp_text = "*MISP Threat Intelligence:*\n"
            misp_text += f":mag: IOCs checked: {request.misp_context.iocs_checked}, "
            misp_text += f"Matches: {request.misp_context.iocs_matched}\n"

            if request.misp_context.threat_actors:
                misp_text += f":spy: *Threat Actors:* {', '.join(request.misp_context.threat_actors[:3])}\n"

            if request.misp_context.campaigns:
                misp_text += f":clipboard: *Campaigns:* {', '.join(request.misp_context.campaigns[:3])}\n"

            if request.misp_context.matched_events:
                misp_text += f":link: *MISP Events:* {', '.join(request.misp_context.matched_events[:3])}\n"

            if request.misp_context.warninglist_hits > 0:
                misp_text += f":warning: *Warninglist hits:* {request.misp_context.warninglist_hits} (potential false positives)\n"

            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": misp_text},
            })

        # Key findings
        if request.findings:
            findings_text = "*Key Findings:*\n"
            for finding in request.findings[:5]:
                findings_text += f"• {finding[:300]}\n"
            if len(request.findings) > 5:
                findings_text += f"_...and {len(request.findings) - 5} more_"

            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": findings_text},
            })

        # AI Verdict section
        if request.ai_decision:
            ai_decision_emoji = {
                "escalate": ":rotating_light:",
                "close": ":white_check_mark:",
                "needs_more_info": ":thinking_face:",
            }

            verdict_text = (
                f"*AI Verdict:* {ai_decision_emoji.get(request.ai_decision.value, '')} "
                f"{request.ai_decision.value.upper()}\n"
            )
            if request.ai_confidence is not None:
                verdict_text += f"*Confidence:* {request.ai_confidence:.0%}\n"
            if request.ai_impact:
                verdict_text += f"*Potential Impact:* {request.ai_impact.value.upper()}\n"
            if request.ai_urgency:
                verdict_text += f"*Urgency:* {request.ai_urgency.value.upper()}\n"

            blocks.append({"type": "divider"})
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": verdict_text},
            })

            if request.ai_assessment:
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Assessment:*\n{request.ai_assessment[:500]}{'...' if len(request.ai_assessment) > 500 else ''}",
                    },
                })

            if request.ai_recommendation:
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Recommendation:*\n{request.ai_recommendation[:300]}",
                    },
                })

            if request.ai_evidence:
                evidence_text = "*Key Evidence:*\n"
                for e in request.ai_evidence[:3]:
                    evidence_text += f"• {e[:80]}\n"
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": evidence_text},
                })

        blocks.append({"type": "divider"})

        # Action buttons
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": ":white_check_mark: Approve",
                        "emoji": True,
                    },
                    "style": "primary",
                    "action_id": "hil_approve",
                    "value": request.investigation_id,
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": ":x: Reject",
                        "emoji": True,
                    },
                    "style": "danger",
                    "action_id": "hil_reject",
                    "value": request.investigation_id,
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": ":question: More Info",
                        "emoji": True,
                    },
                    "action_id": "hil_more_info",
                    "value": request.investigation_id,
                },
            ],
        })

        return blocks
