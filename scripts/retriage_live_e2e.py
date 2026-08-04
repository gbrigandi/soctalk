#!/usr/bin/env python3
"""Re-triage (bounded LLM-failure retry) — FULL-LOOP live proof, no RunPod.

Stands up the real API + real runs-worker against a throwaway Postgres,
injects a high-severity alert, and points the worker's LLM at a local fake
that always answers 404 "The model ... does not exist" — a body that matches
no cold-start marker, so it classifies as provider_error, the category the
re-triage feature releases and the serverless fix never touched.

Phase 1 (re-triage ON, X=3 via SOCTALK_MAX_TRIAGE_ATTEMPTS on the API):
  expect attempts 1..2 released with last_error_category=provider_error,
  terminal `failed` at the cap, and the investigation still ACTIVE — retried,
  bounded, and never a silently closed alert.

Phase 2 (SOCTALK_RETRIAGE_CATEGORIES=off, fresh alert):
  expect the run to fail on the FIRST error with attempts=0 — the pre-feature
  behaviour, proving the gate is what changed and the default is doing work.

Evidence, not assertions: the run row trajectory is printed as it changes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg2

REPO = Path(__file__).resolve().parent.parent
VENV = REPO / ".venv" / "bin"
PORT = 58012
API = f"http://127.0.0.1:{PORT}"
FAKE_PORT = 58033
PG_ASYNC = "postgresql+asyncpg://soctalk:soctalk@localhost:5433/retriage_e2e"
PG_SYNC = "postgresql+psycopg2://soctalk:soctalk@localhost:5433/retriage_e2e"
DSN = "host=localhost port=5433 dbname=retriage_e2e user=soctalk password=soctalk"
SIGNING_KEY = "dev-signing-key"
LOG = Path("/tmp/retriage-e2e")
LOG.mkdir(exist_ok=True)


def sh(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


def db(sql: str, args=None, fetch=True):
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute(sql, args or ())
        rows = cur.fetchall() if fetch and cur.description else None
        c.commit()
        return rows


class _AlwaysBadModel(BaseHTTPRequestHandler):
    """Every POST is the permanent-looking failure under test: a 404 whose
    body matches no cold-start marker. GET /health answers 200 so nothing
    mistakes this for a downed socket (connection refused IS a marker)."""

    calls = 0

    def do_POST(self):  # noqa: N802
        _AlwaysBadModel.calls += 1
        body = json.dumps({"error": {
            "message": "The model 'retriage-test' does not exist",
            "type": "invalid_request_error", "code": "model_not_found"}}).encode()
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):  # quiet
        pass


def start_worker(procs, extra_env, log_name):
    wenv = {**os.environ, **extra_env}
    wenv["ANTHROPIC_API_KEY"] = ""
    wlog = open(LOG / log_name, "w")
    p = subprocess.Popen([str(VENV / "python"), "-m", "soctalk.runs_worker.main"],
                         cwd=REPO, env=wenv, stdout=wlog, stderr=subprocess.STDOUT)
    procs.append(p)
    return p


def inject_alert(tid, atok, tag):
    uniq = uuid4().hex[:8]
    batch = {"tenant_id": tid, "schema_version": 1, "events": [{
        "source_event_id": f"rt-{tag}-{uniq}", "source": "wazuh",
        "rule_id": f"57{uniq[:3]}", "severity": 10,
        "asset_ids": [f"web-{uniq}"],
        "initial_iocs": [{"type": "ip", "value": f"203.0.113.{int(uniq[:2], 16) % 254 + 1}"}],
        "title": f"[{tag}] failed logins then success from foreign IP",
        "description": "sshd: repeated failures then Accepted password.",
    }]}
    r = httpx.post(f"{API}/api/internal/adapter/events", json=batch,
                   headers={"Authorization": f"Bearer {atok}"}, timeout=30)
    print(f"ingest[{tag}]:", r.status_code, r.text[:200])
    r.raise_for_status()


def watch_run(run_id, deadline_s):
    t0, last = time.time(), None
    releases = []
    while time.time() - t0 < deadline_s:
        row = db("SELECT status, attempts, last_error_category "
                 "FROM investigation_runs WHERE id=%s", (run_id,))[0]
        if row != last:
            print(f"  [t+{int(time.time()-t0):3}s] status={row[0]:9} "
                  f"attempts={row[1]} err_cat={row[2]}", flush=True)
            if last is not None and row[1] > (last[1] or 0):
                releases.append(row)
            last = row
        if row[0] in ("completed", "failed"):
            return row, releases
        time.sleep(3)
    return last, releases


def newest_run(tid):
    for _ in range(30):
        rows = db("SELECT id, status, attempts, max_attempts FROM investigation_runs "
                  "WHERE tenant_id=%s ORDER BY started_at DESC LIMIT 1", (tid,))
        if rows:
            return rows[0]
        time.sleep(1)
    raise SystemExit("no run created")


def case_status(tid, run_id):
    # The investigation table is ``investigations`` in this schema lineage;
    # the API's SQL says ``cases`` because prod runs a view/rename ahead of
    # this repo's alembic head. Either way, what matters is the STATUS.
    return db("SELECT i.status FROM investigations i JOIN investigation_runs r "
              "ON r.investigation_id = i.id WHERE r.id=%s", (run_id,))[0][0]


def main() -> int:
    procs: list[subprocess.Popen] = []
    httpd = ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), _AlwaysBadModel)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"fake endpoint on :{FAKE_PORT} (always 404 model_not_found)")
    try:
        sh("alembic upgrade head")
        r = subprocess.run([str(VENV / "alembic"), "upgrade", "head"], cwd=REPO,
                           env={**os.environ, "DATABASE_URL": PG_ASYNC},
                           capture_output=True, text=True)
        if r.returncode:
            print(r.stdout, r.stderr)
            return 1

        sh("start API (X=3 via SOCTALK_MAX_TRIAGE_ATTEMPTS, backoff=5s)")
        api_env = {**os.environ,
                   "DATABASE_URL": PG_ASYNC, "DATABASE_URL_APP": PG_ASYNC,
                   "DATABASE_URL_MSSP": PG_ASYNC, "DATABASE_URL_ADMIN": PG_ASYNC,
                   "SOCTALK_AUTH_MODE": "internal",
                   "SOCTALK_ADAPTER_SIGNING_KEY": SIGNING_KEY,
                   "SOCTALK_PROVISIONING_WORKER": "0",
                   "SOCTALK_PUBLIC_ORIGIN": API,
                   "SOCTALK_AUTH_COOKIE_SECURE": "0",
                   "SOCTALK_MAX_TRIAGE_ATTEMPTS": "3",
                   "SOCTALK_RUN_RETRY_BACKOFF_SECONDS": "5"}
        for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            api_env.pop(k, None)
        apilog = open(LOG / "api.log", "w")
        procs.append(subprocess.Popen(
            [str(VENV / "uvicorn"), "soctalk.core.api.app_v1:app",
             "--port", str(PORT), "--host", "127.0.0.1"],
            cwd=REPO, env=api_env, stdout=apilog, stderr=subprocess.STDOUT))
        for _ in range(60):
            try:
                if httpx.get(f"{API}/health/ready", timeout=2).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)
        else:
            print("API never ready")
            print((LOG / "api.log").read_text()[-1500:])
            return 1
        print("API ready")

        sh("seed org + admin, login, tenant")
        sys.path.insert(0, str(REPO / "src"))
        import soctalk.core.auth.models as am
        import soctalk.core.ir.models  # noqa: F401
        import soctalk.core.tenancy.models as tm
        import soctalk.persistence.models  # noqa: F401
        from soctalk.core.auth.passwords import hash_password
        from sqlmodel import Session, create_engine, select
        eng = create_engine(PG_SYNC)
        with Session(eng) as s:
            if s.exec(select(tm.Organization)).first() is None:
                s.add(tm.Organization(mssp_id=uuid4(), mssp_name="RT MSSP",
                                      install_id=uuid4(), install_label="retriage"))
                s.commit()
            u = s.exec(select(tm.User).where(tm.User.email == "rt@acme.example")).first()
            if u is None:
                u = tm.User(email="rt@acme.example", display_name="RT Admin",
                            user_type="mssp", role="mssp_admin")
                s.add(u)
                s.commit()
                s.refresh(u)
                s.add(am.PasswordCredential(user_id=u.id,
                                            password_hash=hash_password("rt-pw-12345")))
                s.commit()
        cj = httpx.Client(base_url=API, headers={"Origin": API}, timeout=15)
        cj.post("/api/auth/login",
                json={"email": "rt@acme.example", "password": "rt-pw-12345"}).raise_for_status()
        cr = cj.post("/api/mssp/tenants", json={"slug": "rt-t", "display_name": "RT Tenant"})
        if cr.status_code == 201:
            tid = cr.json()["id"]
        else:
            tid = next(t["id"] for t in cj.get("/api/mssp/tenants").json()
                       if t["slug"] == "rt-t")
        print("tenant:", tid)

        from soctalk.core.tenancy.auth import mint_adapter_token, mint_worker_token
        os.environ["SOCTALK_ADAPTER_SIGNING_KEY"] = SIGNING_KEY
        atok = mint_adapter_token(UUID(tid))
        wtok = mint_worker_token(UUID(tid))
        Path("/tmp/rt-worker-token").write_text(wtok)

        base_worker_env = {
            "SOCTALK_API_URL": API, "WORKER_TOKEN_PATH": "/tmp/rt-worker-token",
            "SOCTALK_ADAPTER_SIGNING_KEY": SIGNING_KEY,
            "SOCTALK_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "fake-key",
            "OPENAI_BASE_URL": f"http://127.0.0.1:{FAKE_PORT}/v1",
            "SOCTALK_FAST_MODEL": "retriage-test",
            "SOCTALK_REASONING_MODEL": "retriage-test",
            "SOCTALK_LLM_TIMEOUT_SECONDS": "10", "SOCTALK_LLM_MAX_RETRIES": "0",
            "DATABASE_URL": PG_ASYNC, "DATABASE_URL_APP": PG_ASYNC,
            "DATABASE_URL_MSSP": PG_ASYNC,
        }

        # ---------------- Phase 1: re-triage ON, default categories ----
        sh("PHASE 1: re-triage ON — expect release x2 then failed at X=3")
        inject_alert(tid, atok, "on")
        run = newest_run(tid)
        print(f"run {run[0]}  max_attempts={run[3]} (from SOCTALK_MAX_TRIAGE_ATTEMPTS)")
        w = start_worker(procs, base_worker_env, "worker_on.log")
        final, releases = watch_run(run[0], 420)
        w.terminate()
        case1 = case_status(tid, run[0])
        print(f"phase1 final: {final}  releases_seen={len(releases)}  case={case1}")

        ok1 = (final is not None and final[0] == "failed" and final[1] == 3
               and final[2] == "provider_error" and len(releases) >= 2
               and case1 == "active")

        # ---------------- Phase 2: gate OFF — first error is terminal --
        sh("PHASE 2: SOCTALK_RETRIAGE_CATEGORIES=off — expect failed, attempts=0")
        inject_alert(tid, atok, "off")
        run2 = None
        for _ in range(30):
            rows = db("SELECT id, status, attempts FROM investigation_runs "
                      "WHERE tenant_id=%s AND id != %s ORDER BY started_at DESC LIMIT 1",
                      (tid, run[0]))
            if rows:
                run2 = rows[0]
                break
            time.sleep(1)
        if run2 is None:
            raise SystemExit("no phase-2 run created")
        w2 = start_worker(procs, {**base_worker_env,
                                  "SOCTALK_RETRIAGE_CATEGORIES": "off"}, "worker_off.log")
        final2, releases2 = watch_run(run2[0], 240)
        w2.terminate()
        case2 = case_status(tid, run2[0])
        print(f"phase2 final: {final2}  releases_seen={len(releases2)}  case={case2}")

        ok2 = (final2 is not None and final2[0] == "failed" and final2[1] == 0
               and len(releases2) == 0 and case2 == "active")

        sh("RESULT")
        print(f"fake endpoint served {_AlwaysBadModel.calls} LLM calls, all 404 model_not_found")
        print(f"PHASE 1 (retriage on):  {'PASS' if ok1 else 'FAIL'}")
        print(f"PHASE 2 (retriage off): {'PASS' if ok2 else 'FAIL'}")
        return 0 if (ok1 and ok2) else 1
    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
        httpd.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
