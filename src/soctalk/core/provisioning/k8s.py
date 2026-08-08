"""Thin K8s API wrapper used by the TenantController.

Uses the official Python kubernetes client (``kubernetes`` PyPI). Runs
in-cluster with the SocTalk controller ServiceAccount token.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import structlog

logger = structlog.get_logger()


def _ensure_client_loaded():
    """Load in-cluster config, or fall back to KUBECONFIG for dev."""
    from kubernetes import config as k8s_config

    try:
        k8s_config.load_incluster_config()
    except Exception:  # noqa: BLE001
        # Dev / test path.
        k8s_config.load_kube_config()


class K8sClient:
    """Async-friendly wrapper around the ``kubernetes`` sync client.

    Calls are offloaded to a thread pool. Scope is limited to what the
    TenantController needs: namespace, secret, and helm release bookkeeping
    (Helm writes its own Secrets to track release state).
    """

    def __init__(self) -> None:
        _ensure_client_loaded()
        from kubernetes import client as k8s

        self._core = k8s.CoreV1Api()

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def ensure_namespace(
        self, name: str, labels: dict[str, str]
    ) -> None:
        """Create the namespace if missing; verify labels match if present.

        Applies required V1 labels (``tenant=true``, ``managed-by=soctalk``,
        plus MSSP/install/tenant IDs). If namespace exists with different
        labels, raises (operator should reconcile manually).
        """
        from kubernetes import client as k8s
        from kubernetes.client.exceptions import ApiException

        body = k8s.V1Namespace(
            metadata=k8s.V1ObjectMeta(name=name, labels=labels)
        )
        try:
            await self._run(self._core.create_namespace, body)
            logger.info("namespace_created", name=name, labels=labels)
        except ApiException as e:
            if e.status == 409:
                # Exists: patch labels if different.
                existing = await self._run(self._core.read_namespace, name)
                existing_labels = existing.metadata.labels or {}
                if not all(existing_labels.get(k) == v for k, v in labels.items()):
                    # Patch missing/wrong labels.
                    await self._run(
                        self._core.patch_namespace,
                        name,
                        {"metadata": {"labels": labels}},
                    )
                    logger.info("namespace_relabeled", name=name)
            else:
                raise

    async def delete_namespace(self, name: str) -> None:
        from kubernetes.client.exceptions import ApiException

        try:
            await self._run(self._core.delete_namespace, name)
            logger.info("namespace_deleted", name=name)
        except ApiException as e:
            if e.status != 404:
                raise

    async def namespace_exists(self, name: str) -> bool:
        """Definitively report whether a namespace is present.

        ``read_pods`` cannot answer this: a 404 (namespace gone) and a
        transient API error both surface as exceptions, so drift detection
        needs a call that maps *only* the 404 to a boolean and re-raises
        everything else (so a blip is never mistaken for a deleted tenant).
        """
        from kubernetes.client.exceptions import ApiException

        try:
            await self._run(self._core.read_namespace, name)
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    async def put_secret(
        self,
        namespace: str,
        name: str,
        data: dict[str, str],
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Create or update an Opaque Secret. Values are string (not bytes)."""
        from base64 import b64encode

        from kubernetes import client as k8s
        from kubernetes.client.exceptions import ApiException

        encoded = {k: b64encode(v.encode()).decode() for k, v in data.items()}
        body = k8s.V1Secret(
            metadata=k8s.V1ObjectMeta(name=name, labels=labels or {}),
            type="Opaque",
            data=encoded,
        )
        try:
            await self._run(self._core.create_namespaced_secret, namespace, body)
        except ApiException as e:
            if e.status == 409:
                await self._run(
                    self._core.patch_namespaced_secret, name, namespace, body
                )
            else:
                raise

    async def get_secret(
        self, namespace: str, name: str
    ) -> dict[str, Any]:
        """Read an Opaque Secret. Returns ``{"name", "namespace", "data"}``
        with ``data`` already base64-decoded into a ``{key: str}`` dict.
        """
        from base64 import b64decode

        sec = await self._run(
            self._core.read_namespaced_secret, name, namespace
        )
        decoded = {
            k: b64decode(v).decode() for k, v in (sec.data or {}).items()
        }
        return {"name": name, "namespace": namespace, "data": decoded}

    async def check_reachable(self) -> None:
        """Cheap ping: list namespaces (read verb the SA already needs)."""
        await self._run(self._core.list_namespace, limit=1)

    async def storage_class_exists(self, name: str) -> bool:
        """Return True if a StorageClass with ``name`` is registered."""
        from kubernetes import client as k8s
        from kubernetes.client.exceptions import ApiException

        storage = k8s.StorageV1Api()
        try:
            await self._run(storage.read_storage_class, name)
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    async def list_storage_classes(self) -> list[str]:
        """Return the names of every registered StorageClass.

        Only used to enrich the preflight failure message -- the
        controller never picks a class on the operator's behalf. The
        controller ServiceAccount already holds ``list`` on
        ``storage.k8s.io/storageclasses``.
        """
        from kubernetes import client as k8s

        storage = k8s.StorageV1Api()
        result = await self._run(storage.list_storage_class)
        return [sc.metadata.name for sc in result.items]

    async def read_pods(self, namespace: str) -> list[dict[str, Any]]:
        """Return a lightweight summary of pods in a namespace.

        A pod with no container statuses yet (freshly scheduled / Pending) is
        NOT ready: ``all([])`` is ``True`` and would otherwise report an
        un-started pod as ready, so require at least one status.
        """
        result = await self._run(self._core.list_namespaced_pod, namespace)
        pods = []
        for p in result.items:
            statuses = p.status.container_statuses or []
            pods.append(
                {
                    "name": p.metadata.name,
                    "phase": p.status.phase,
                    "ready": bool(statuses) and all(c.ready for c in statuses),
                }
            )
        return pods

    async def rollout_restart_deployment(
        self, namespace: str, name: str
    ) -> None:
        """Trigger a rolling restart of a Deployment.

        Mirrors ``kubectl rollout restart``: patches the pod template
        with ``kubectl.kubernetes.io/restartedAt: <ISO8601>``, which
        kube-controller-manager treats as a template change and rolls
        new pods. Safe to call when the Deployment is missing — 404
        is silently swallowed so callers in best-effort paths
        (post-rotate LLM key) don't have to special-case it.

        Used by the LLM key rotation/clear path: env ``secretKeyRef``
        does not refresh on Secret update, so without a restart the
        runs-worker would hold the stale credential.
        """
        from datetime import datetime, timezone

        from kubernetes import client as k8s
        from kubernetes.client.exceptions import ApiException

        apps = k8s.AppsV1Api()
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": datetime.now(
                                timezone.utc
                            ).isoformat(),
                        }
                    }
                }
            }
        }
        try:
            await self._run(
                apps.patch_namespaced_deployment, name, namespace, body
            )
        except ApiException as e:
            if e.status == 404:
                return
            raise

    async def patch_deployment(
        self, namespace: str, name: str, patch: dict[str, Any]
    ) -> None:
        """Apply an arbitrary strategic-merge patch to a Deployment.

        Thin wrapper over ``apps_v1.patch_namespaced_deployment``. Used by
        the external-SIEM PATCH endpoint to bump a pod-template annotation
        (``soctalk.io/restartedAt``) — the same mechanism ``kubectl rollout
        restart`` uses under the hood — so the long-lived adapter pod cycles
        against the freshly-written Secret. A 404 (no Deployment in this
        cluster — e.g. cross-cluster deploy) is swallowed so best-effort
        callers don't have to special-case it; other errors propagate to the
        caller, which logs and continues.
        """
        from kubernetes import client as k8s
        from kubernetes.client.exceptions import ApiException

        apps = k8s.AppsV1Api()
        try:
            await self._run(
                apps.patch_namespaced_deployment, name, namespace, patch
            )
        except ApiException as e:
            if e.status == 404:
                return
            raise


def new_k8s_client() -> K8sClient:
    """Factory helper; tests substitute a fake."""
    return K8sClient()
