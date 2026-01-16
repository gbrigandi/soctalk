#!/usr/bin/env python3
"""Inject test investigation data directly into the database for UI testing."""

import asyncio
import json
import uuid
from datetime import datetime

import asyncpg


async def inject_test_data():
    """Inject test investigation and events into the database."""
    # Connect to database
    conn = await asyncpg.connect(
        host="localhost",
        port=5432,
        user="soctalk",
        password="soctalk",
        database="soctalk",
    )

    try:
        investigation_id = uuid.uuid4()
        now = datetime.utcnow()

        print(f"Creating test investigation: {investigation_id}")

        # Create investigation.created event
        await conn.execute(
            """
            INSERT INTO events (id, aggregate_id, aggregate_type, event_type, version, timestamp, data, event_metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            uuid.uuid4(),
            investigation_id,
            "investigation",
            "investigation.created",
            1,
            now,
            json.dumps({
                "investigation_id": str(investigation_id),
                "title": "Test Alert: Suspicious PowerShell Activity Detected",
                "alert_ids": ["alert-001", "alert-002", "alert-003"],
                "max_severity": "critical",
                "source_agent": "web-server-01",
                "source_ip": "192.168.1.100",
            }),
            json.dumps({}),
        )

        # Create alert.correlated event
        await conn.execute(
            """
            INSERT INTO events (id, aggregate_id, aggregate_type, event_type, version, timestamp, data, event_metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            uuid.uuid4(),
            investigation_id,
            "investigation",
            "alert.correlated",
            2,
            now,
            json.dumps({
                "investigation_id": str(investigation_id),
                "alert_id": "alert-001",
                "rule_id": "100001",
                "severity": "critical",
                "description": "Suspicious process: powershell.exe spawning cmd.exe",
            }),
            json.dumps({}),
        )

        # Create investigation.started event
        await conn.execute(
            """
            INSERT INTO events (id, aggregate_id, aggregate_type, event_type, version, timestamp, data, event_metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            uuid.uuid4(),
            investigation_id,
            "investigation",
            "investigation.started",
            3,
            now,
            json.dumps({
                "investigation_id": str(investigation_id),
                "phase": "triage",
            }),
            json.dumps({}),
        )

        # Create observable.extracted event
        await conn.execute(
            """
            INSERT INTO events (id, aggregate_id, aggregate_type, event_type, version, timestamp, data, event_metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            uuid.uuid4(),
            investigation_id,
            "investigation",
            "observable.extracted",
            4,
            now,
            json.dumps({
                "investigation_id": str(investigation_id),
                "observable_type": "ip",
                "observable_value": "192.168.1.100",
                "classification": "internal",
            }),
            json.dumps({}),
        )

        # Create observable.extracted event for hash
        await conn.execute(
            """
            INSERT INTO events (id, aggregate_id, aggregate_type, event_type, version, timestamp, data, event_metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            uuid.uuid4(),
            investigation_id,
            "investigation",
            "observable.extracted",
            5,
            now,
            json.dumps({
                "investigation_id": str(investigation_id),
                "observable_type": "hash_md5",
                "observable_value": "5d41402abc4b2a76b9719d911017c592",
                "classification": "unknown",
            }),
            json.dumps({}),
        )

        # Create enrichment.completed event
        await conn.execute(
            """
            INSERT INTO events (id, aggregate_id, aggregate_type, event_type, version, timestamp, data, event_metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            uuid.uuid4(),
            investigation_id,
            "investigation",
            "enrichment.completed",
            6,
            now,
            json.dumps({
                "investigation_id": str(investigation_id),
                "enrichment_type": "virustotal",
                "observable_value": "5d41402abc4b2a76b9719d911017c592",
                "result": {"malicious": 45, "suspicious": 12, "harmless": 3},
            }),
            json.dumps({}),
        )

        # Insert into investigations projection table
        await conn.execute(
            """
            INSERT INTO investigations (
                id, title, status, phase, max_severity,
                alert_count, observable_count, malicious_count,
                created_at, updated_at, tags
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                status = EXCLUDED.status,
                phase = EXCLUDED.phase,
                max_severity = EXCLUDED.max_severity,
                alert_count = EXCLUDED.alert_count,
                observable_count = EXCLUDED.observable_count,
                malicious_count = EXCLUDED.malicious_count,
                updated_at = EXCLUDED.updated_at
            """,
            investigation_id,
            "Test Alert: Suspicious PowerShell Activity Detected",
            "active",
            "enrichment",
            "critical",
            3,
            2,
            1,
            now,
            now,
            [],
        )

        print(f"Successfully created test investigation: {investigation_id}")
        print("Check the UI at http://localhost:5173/investigations")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(inject_test_data())
