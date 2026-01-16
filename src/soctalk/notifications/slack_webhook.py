"""Slack webhook notification service for SocTalk.

This service sends one-way notifications via Slack webhooks for:
- Investigation escalations
- Verdict decisions

This is separate from the Slack HIL backend which handles two-way
interactive approvals via Socket Mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx
import structlog

logger = structlog.get_logger()


@dataclass
class SlackNotificationSettings:
    """Settings for Slack webhook notifications."""

    enabled: bool = False
    webhook_url: Optional[str] = None
    channel: Optional[str] = None
    notify_on_escalation: bool = True
    notify_on_verdict: bool = True


class SlackWebhookNotifier:
    """Sends notifications to Slack via webhook.

    Uses simple incoming webhook to post messages without requiring
    Socket Mode or bot tokens.
    """

    def __init__(self, settings: SlackNotificationSettings):
        """Initialize the Slack webhook notifier.

        Args:
            settings: Notification settings from database.
        """
        self._settings = settings
        self._client = httpx.AsyncClient(timeout=10.0)

    @property
    def is_enabled(self) -> bool:
        """Check if notifications are enabled and configured."""
        return (
            self._settings.enabled
            and self._settings.webhook_url is not None
            and len(self._settings.webhook_url) > 0
        )

    async def notify_escalation(
        self,
        investigation_id: str,
        title: str,
        severity: str,
        alert_count: int,
        malicious_count: int,
        thehive_case_id: Optional[str] = None,
    ) -> bool:
        """Send escalation notification to Slack.

        Args:
            investigation_id: The investigation ID.
            title: Investigation title.
            severity: Max severity level.
            alert_count: Number of alerts.
            malicious_count: Number of malicious observables.
            thehive_case_id: Optional TheHive case ID if created.

        Returns:
            True if notification was sent successfully.
        """
        if not self.is_enabled:
            logger.debug("slack_webhook_disabled")
            return False

        if not self._settings.notify_on_escalation:
            logger.debug("slack_escalation_notifications_disabled")
            return False

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
                    "text": ":rotating_light: Investigation Escalated",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Investigation ID:*\n`{investigation_id[:8]}...`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Severity:*\n{severity_emoji.get(severity, ':white_circle:')} {severity.upper()}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Alerts:*\n{alert_count}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Malicious IOCs:*\n{malicious_count}",
                    },
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Title:*\n{title}",
                },
            },
        ]

        if thehive_case_id:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":card_index_dividers: *TheHive Case:* `{thehive_case_id}`",
                },
            })

        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f":clock1: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}",
                },
            ],
        })

        return await self._send_message(blocks, f"Investigation {investigation_id} escalated")

    async def notify_verdict(
        self,
        investigation_id: str,
        title: str,
        verdict: str,
        confidence: float,
        assessment: Optional[str] = None,
    ) -> bool:
        """Send verdict notification to Slack.

        Args:
            investigation_id: The investigation ID.
            title: Investigation title.
            verdict: The verdict decision (escalate, close, etc.).
            confidence: Confidence score (0-1).
            assessment: Optional AI assessment summary.

        Returns:
            True if notification was sent successfully.
        """
        if not self.is_enabled:
            logger.debug("slack_webhook_disabled")
            return False

        if not self._settings.notify_on_verdict:
            logger.debug("slack_verdict_notifications_disabled")
            return False

        verdict_emoji = {
            "escalate": ":rotating_light:",
            "close": ":white_check_mark:",
            "needs_more_info": ":thinking_face:",
        }

        verdict_color = {
            "escalate": ":red_circle:",
            "close": ":large_green_circle:",
            "needs_more_info": ":large_yellow_circle:",
        }

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{verdict_emoji.get(verdict, ':memo:')} Verdict Rendered",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Investigation ID:*\n`{investigation_id[:8]}...`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Verdict:*\n{verdict_color.get(verdict, ':white_circle:')} {verdict.upper()}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Confidence:*\n{confidence:.0%}",
                    },
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Title:*\n{title}",
                },
            },
        ]

        if assessment:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Assessment:*\n{assessment[:500]}{'...' if len(assessment) > 500 else ''}",
                },
            })

        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f":clock1: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}",
                },
            ],
        })

        return await self._send_message(blocks, f"Verdict for investigation {investigation_id}: {verdict}")

    async def _send_message(self, blocks: list, fallback_text: str) -> bool:
        """Send a message to Slack webhook.

        Args:
            blocks: Slack Block Kit blocks.
            fallback_text: Plain text fallback.

        Returns:
            True if message was sent successfully.
        """
        if not self._settings.webhook_url:
            return False

        payload = {
            "blocks": blocks,
            "text": fallback_text,  # Fallback for notifications
        }

        # Add channel override if specified
        if self._settings.channel:
            payload["channel"] = self._settings.channel

        try:
            response = await self._client.post(
                self._settings.webhook_url,
                json=payload,
            )

            if response.status_code == 200:
                logger.info(
                    "slack_webhook_sent",
                    status_code=response.status_code,
                )
                return True
            else:
                logger.warning(
                    "slack_webhook_failed",
                    status_code=response.status_code,
                    response_text=response.text[:200],
                )
                return False

        except Exception as e:
            logger.error("slack_webhook_error", error=str(e))
            return False

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
