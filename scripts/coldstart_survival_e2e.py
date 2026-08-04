#!/usr/bin/env python3
"""#77 Phase 1 — FULL-LOOP cold-start SURVIVAL proof against real RunPod.

Stands up the real API + real runs-worker against a throwaway Postgres, injects
a high-severity alert (→ promoted → investigation_run), and points the worker's
LLM at a genuinely COLD scale-to-zero RunPod endpoint with a short client
timeout. Then it watches the run row and proves the fix:

  the cold endpoint makes the first triage call(s) fail; instead of the run
  going to `failed`, the worker RELEASES it (attempts++, last_error_category=
  serverless_unavailable, status stays claimable) and RETRIES; once RunPod
  finishes warming, a retry succeeds and the run reaches `completed`.

Without Phase 1 the very first cold failure terminalizes the run to `failed`
with attempts=0 — that is the baseline this supersedes.

Env in: RUNPOD_API_KEY (.env). Cold endpoint id from /tmp/coldstart_ids.txt.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg2

REPO = Path(__file__).resolve().parent.parent
VENV = REPO / ".venv" / "bin"
PORT = 58011
API = f"http://127.0.0.1:{PORT}"
PG_ASYNC = "postgresql+asyncpg://soctalk:soctalk@localhost:5433/coldstart_e2e"
PG_SYNC = "postgresql+psycopg2://soctalk:soctalk@localhost:5433/coldstart_e2e"
DSN = "host=localhost port=5433 dbname=coldstart_e2e user=soctalk password=soctalk"
SIGNING_KEY = "dev-signing-key"
K = os.environ["RUNPOD_API_KEY"]
CS_EID = Path("/tmp/coldstart_ids.txt").read_text().split()[1]
CS_URL = f"https://api.runpod.ai/v2/{CS_EID}/openai/v1"
CS_HEALTH = f"https://api.runpod.ai/v2/{CS_EID}/health"
# MUST match MODEL_NAME in coldstart_make_endpoint.sh. It briefly said 7B while
# the endpoint served 1.5B, and once the worker warmed, that mismatch is a 404
# from vLLM — indistinguishable from the exact bug this script investigates,
# except it is an artifact of the harness. Overridable for ad-hoc endpoints.
MODEL = os.environ.get("COLDSTART_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
LOG = Path("/tmp/gpu-bench")
LOG.mkdir(exist_ok=True)


def sh(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


def rp_gql(query: str) -> dict:
    return httpx.post("https://api.runpod.io/graphql",
                      headers={"Authorization": f"Bearer {K}"},
                      json={"query": query}, timeout=40).json()


def rp_active() -> int:
    w = httpx.get(CS_HEALTH, headers={"Authorization": f"Bearer {K}"}, timeout=10).json().get("workers", {})
    return sum(w.get(k, 0) for k in ("idle", "ready", "running", "initializing"))


def _set_max(mx: int) -> None:
    read = Path("/tmp/coldstart_ids.txt").read_text().split()
    ctid = read[0]
    rp_gql('mutation{saveEndpoint(input:{id:"%s",name:"soctalk-coldstart",'
           'templateId:"%s",gpuIds:"ADA_24",workersMin:0,workersMax:%d,idleTimeout:10,'
           'scalerType:"QUEUE_DELAY",scalerValue:1,networkVolumeId:""}){id}}' % (CS_EID, ctid, mx))


def force_cold() -> None:
    """Drain any auto-warmed worker so the first triage call really cold-starts."""
    _set_max(0)
    for _ in range(30):
        if rp_active() == 0:
            break
        time.sleep(5)
    _set_max(1)  # min stays 0 (cold); max 1 lets the first call trigger a cold start


def db(sql: str, args=None, fetch=True):
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute(sql, args or ())
        rows = cur.fetchall() if fetch and cur.description else None
        c.commit()
        return rows


def main() -> int:
    procs: list[subprocess.Popen] = []
    try:
        # 1. migrations -------------------------------------------------
        sh("alembic upgrade head (coldstart_e2e)")
        r = subprocess.run([str(VENV / "alembic"), "upgrade", "head"],
                           cwd=REPO, env={**os.environ, "DATABASE_URL": PG_ASYNC},
                           capture_output=True, text=True)
        if r.returncode:
            print(r.stdout, r.stderr); return 1
        print("migrated head:", db("SELECT version_num FROM alembic_version")[0][0])

        # 2. API --------------------------------------------------------
        sh("start API (SOCTALK_RUN_RETRY_BACKOFF_SECONDS=15)")
        api_env = {**os.environ,
                   "DATABASE_URL": PG_ASYNC, "DATABASE_URL_APP": PG_ASYNC,
                   "DATABASE_URL_MSSP": PG_ASYNC, "DATABASE_URL_ADMIN": PG_ASYNC,
                   "SOCTALK_AUTH_MODE": "internal",
                   "SOCTALK_ADAPTER_SIGNING_KEY": SIGNING_KEY,
                   "SOCTALK_PROVISIONING_WORKER": "0",
                   "SOCTALK_PUBLIC_ORIGIN": API,
                   "SOCTALK_AUTH_COOKIE_SECURE": "0",  # httpx over plain HTTP must get the cookie back
                   "SOCTALK_RUN_RETRY_BACKOFF_SECONDS": "15"}
        for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            api_env.pop(k, None)
        apilog = open(LOG / "cs_api.log", "w")
        procs.append(subprocess.Popen(
            [str(VENV / "uvicorn"), "soctalk.core.api.app_v1:app",
             "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=REPO, env=api_env, stdout=apilog, stderr=subprocess.STDOUT))
        for _ in range(60):
            try:
                if httpx.get(f"{API}/health/ready", timeout=2).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            print("API never became ready"); print((LOG / "cs_api.log").read_text()[-2000:]); return 1
        print("API ready")

        # 3. seed org + mssp admin -------------------------------------
        sh("seed org + mssp admin")
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
                s.add(tm.Organization(mssp_id=uuid4(), mssp_name="CS MSSP",
                                      install_id=uuid4(), install_label="coldstart"))
                s.commit()
            u = s.exec(select(tm.User).where(tm.User.email == "cs@acme.example")).first()
            if u is None:
                u = tm.User(email="cs@acme.example", display_name="CS Admin",
                            user_type="mssp", role="mssp_admin")
                s.add(u); s.commit(); s.refresh(u)
                s.add(am.PasswordCredential(user_id=u.id,
                                            password_hash=hash_password("cs-pw-12345")))
                s.commit()
        print("seeded")

        # 4. login + create tenant -------------------------------------
        sh("login + create tenant")
        cj = httpx.Client(base_url=API, headers={"Origin": API}, timeout=15)
        lr = cj.post("/api/auth/login",
                     json={"email": "cs@acme.example", "password": "cs-pw-12345"})
        print("login:", lr.status_code, lr.text[:200])
        lr.raise_for_status()
        cr = cj.post("/api/mssp/tenants",
                     json={"slug": "cs-t", "display_name": "CS Tenant"})
        print("create tenant:", cr.status_code, cr.text[:300])
        if cr.status_code == 201:
            tid = cr.json()["id"]
        else:
            lst = cj.get("/api/mssp/tenants")
            print("list tenants:", lst.status_code, lst.text[:300])
            data = lst.json()
            if not isinstance(data, list):
                raise SystemExit(f"tenant list not a list: {data}")
            tid = next(t["id"] for t in data if t["slug"] == "cs-t")
        print("tenant_id:", tid)

        # 5. inject high-severity alert -> promoted --------------------
        sh("inject severity-10 alert (adapter) -> promoted")
        from soctalk.core.tenancy.auth import mint_adapter_token, mint_worker_token
        os.environ["SOCTALK_ADAPTER_SIGNING_KEY"] = SIGNING_KEY
        from uuid import UUID
        atok = mint_adapter_token(UUID(tid))
        uniq = uuid4().hex[:8]  # unique per run so it PROMOTES (not dedup-attaches)
        batch = {"tenant_id": tid, "schema_version": 1, "events": [{
            "source_event_id": f"cs-{uniq}", "source": "wazuh",
            "rule_id": f"57{uniq[:3]}", "severity": 10,
            "asset_ids": [f"web-{uniq}"],
            "initial_iocs": [{"type": "ip", "value": f"203.0.113.{int(uniq[:2],16)%254+1}"}],
            "title": "Repeated failed logins then success from foreign IP",
            "description": "sshd: 8 failed passwords for root from a foreign IP then Accepted password.",
        }]}
        ir = httpx.post(f"{API}/api/internal/adapter/events", json=batch,
                        headers={"Authorization": f"Bearer {atok}"}, timeout=30)
        print("ingest:", ir.status_code, ir.text[:300])
        ir.raise_for_status()

        # 6. locate the run, widen the retry budget for the demo -------
        sh("locate run + set max_attempts=8 (cover a no-volume cold start)")
        for _ in range(20):
            rows = db("SELECT id, status, attempts, max_attempts, not_before "
                      "FROM investigation_runs WHERE tenant_id=%s ORDER BY started_at DESC LIMIT 1",
                      (tid,))
            if rows:
                break
            time.sleep(1)
        if not rows:
            print("no investigation_run created"); return 1
        run_id = rows[0][0]
        db("UPDATE investigation_runs SET max_attempts=15 WHERE id=%s", (run_id,), fetch=False)
        print("run_id:", run_id, "initial:", rows[0][1:])

        # 7. force the endpoint COLD (drain any auto-warmed worker) -----
        sh("force cold endpoint -> 0 workers (drain), then allow 1")
        force_cold()
        print("cold endpoint active workers:", rp_active())

        # 8. worker token ----------------------------------------------
        wtok = mint_worker_token(UUID(tid))
        Path("/tmp/cs-worker-token").write_text(wtok)

        # 9. worker -> COLD RunPod endpoint, short timeout -------------
        sh("start runs-worker -> COLD endpoint (timeout=15s, retries=0)")
        wenv = {**os.environ,
                "SOCTALK_API_URL": API, "WORKER_TOKEN_PATH": "/tmp/cs-worker-token",
                "SOCTALK_ADAPTER_SIGNING_KEY": SIGNING_KEY,
                "SOCTALK_LLM_PROVIDER": "openai", "OPENAI_API_KEY": K,
                "OPENAI_BASE_URL": CS_URL,
                "SOCTALK_FAST_MODEL": MODEL, "SOCTALK_REASONING_MODEL": MODEL,
                "SOCTALK_LLM_TIMEOUT_SECONDS": "15", "SOCTALK_LLM_MAX_RETRIES": "0",
                "DATABASE_URL": PG_ASYNC, "DATABASE_URL_APP": PG_ASYNC,
                "DATABASE_URL_MSSP": PG_ASYNC}
        # .env re-injects ANTHROPIC_API_KEY via load_dotenv(override=False); an
        # already-present empty value is NOT overridden, so the mutual-exclusion
        # guard sees only OPENAI_API_KEY set.
        wenv["ANTHROPIC_API_KEY"] = ""
        wlog = open(LOG / "cs_worker.log", "w")
        procs.append(subprocess.Popen(
            [str(VENV / "python"), "-m", "soctalk.runs_worker.main"],
            cwd=REPO, env=wenv, stdout=wlog, stderr=subprocess.STDOUT))

        # 10. watch the run trajectory ---------------------------------
        sh("watch run trajectory (up to 12 min)")
        t0 = time.time()
        last = None
        released_seen = False
        while time.time() - t0 < 720:
            row = db("SELECT status, attempts, last_error_category, not_before "
                     "FROM investigation_runs WHERE id=%s", (run_id,))[0]
            if row != last:
                el = int(time.time() - t0)
                print(f"  [t+{el:3}s] status={row[0]:9} attempts={row[1]} "
                      f"err_cat={row[2]} not_before={row[3]}", flush=True)
                last = row
                if row[2] == "serverless_unavailable" or (row[1] and row[1] > 0):
                    released_seen = True
            if row[0] in ("completed", "failed"):
                break
            time.sleep(4)
        final = db("SELECT status, attempts, max_attempts, last_error_category, last_error "
                   "FROM investigation_runs WHERE id=%s", (run_id,))[0]

        # 11. verdict --------------------------------------------------
        sh("RESULT")
        print(f"final: status={final[0]} attempts={final[1]}/{final[2]} "
              f"err_cat={final[3]} last_error={str(final[4])[:120]}")
        wl = (LOG / "cs_worker.log").read_text()
        rel = [ln for ln in wl.splitlines() if any(x in ln.lower()
               for x in ("serverless_unavailable", "release", "retry", "transient"))]
        print("worker release/transient log lines:")
        for ln in rel[-10:]:
            print("   ", ln[:180])
        survived = released_seen and final[0] == "completed"
        retried_not_terminal = released_seen and final[0] != "failed"
        print(f"\nSURVIVED (retried cold-start AND completed): {survived}")
        print(f"cold-start did NOT terminalize the run: {retried_not_terminal}")
        return 0 if survived else (2 if retried_not_terminal else 1)
    finally:
        sh("teardown")
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()


if __name__ == "__main__":
    raise SystemExit(main())
