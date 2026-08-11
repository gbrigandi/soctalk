# Nessus capture lab

Produces **real** Wazuh alerts from a **real** Nessus vulnerability-scan
campaign, so they can be captured once and replayed into SocTalk's correlation +
triage pipeline (see `../nessus_replay.py`). Not fabricated fixtures — real
scanner traffic against real services, decoded by a real Wazuh manager.

## What it is

`docker compose` stack (run on a throwaway host — this was captured on a staging host):

- `wazuh-manager` — Wazuh 4.9.2 manager (manager-only; alerts read from
  `alerts.json`, no indexer/dashboard).
- `target-1` / `target-2` / `target-3` — small hosts (nginx + a probeable web
  surface + sshd) each running a Wazuh agent. Three hosts so a scan is a
  realistic multi-host campaign.
- `nessus` — Tenable Nessus Essentials.

## Reproduce a capture

```bash
cp .env.example .env      # put your NESSUS_ACTIVATION_CODE + a NESSUS_PASSWORD in it
docker compose up -d --build
# wait for Nessus to finish loading plugins:
curl -sk https://localhost:8834/server/status   # -> "status":"ready" (~15-40 min first run)
# wait for the 3 agents to go Active:
docker exec nessus-lab-wazuh-manager-1 /var/ossec/bin/agent_control -l

./scan.sh                                        # runs one Basic Network Scan across the 3 targets
SCANNER_IP=<ip printed by scan.sh> ./capture.sh > ../nessus_scan_alerts.ndjson
docker compose down -v
```

## Notes / gotchas (learned the hard way)

- **Nessus Essentials disables the scan REST API** (`scan_api:false`). `scan.sh`
  works around it by sending the `X-API-Token` baked into `nessus6.js` alongside
  the session cookie (what the web UI does).
- **Don't mount a volume over `/var/ossec/logs`** — it lands root-owned and
  `wazuh-analysisd` then can't create `logs/archives`, so no alerts. Capture via
  `docker exec cat` instead.
- **`events_per_second` max is 1000** in the agent `client_buffer` (2000 fails
  the agent config → no enrollment).
- **`wazuh-remoted` only starts once an agent key exists** — the first agent may
  need a `wazuh-control restart` on the manager to open port 1514.
- Recreating a `target-N` re-enrolls it; a stale registration causes
  "Duplicate agent name". `docker compose down` (which also resets the manager)
  is the clean reset between full re-captures.
