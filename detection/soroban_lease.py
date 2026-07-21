import logging
import os
import time

from config.settings import settings

logger = logging.getLogger("ledgerlens.soroban_lease")


def _load_kube_config(config) -> None:
    """Load in‑cluster config, falling back to the local kube‑config file."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def acquire_submission_lease(region_name: str, lease_duration_seconds: int = 30) -> bool:
    """Attempt to acquire the Soroban submission lease for the given region.

    The lease is represented by a ``Lease`` object in the ``coordination.k8s.io`` API.
    This function implements a simple optimistic‑concurrency acquisition:

    1. Load the current lease (or create it if missing).
    2. If the lease is unclaimed or the ``renewTime`` is older than the
       ``lease_duration_seconds`` we try to claim it by updating ``holderIdentity``.
    3. If the update succeeds, the caller holds the lease.
    4. If another region has a fresh lease, return ``False``.

    Returns ``True`` when this region successfully holds the lease, ``False`` otherwise.

    The ``kubernetes`` client library is imported lazily (only reached when
    lease handling is enabled) so that importing this module -- and anything
    that transitively imports it -- does not hard-require ``kubernetes`` to
    be installed for deployments that don't run multi-region Soroban
    submission (matching this codebase's existing lazy-import convention for
    optional heavy dependencies, e.g. ``dowhy`` in ``detection/causal_engine.py``).
    """
    # Short‑circuit if lease handling is disabled.
    if not getattr(settings, "soroban_submission_lease_enabled", True):
        return True

    from kubernetes import client, config
    from kubernetes.client.rest import ApiException

    _load_kube_config(config)
    api = client.CoordinationV1Api()
    lease_name = settings.soroban_submission_lease_name
    namespace = os.getenv("K8S_NAMESPACE", "default")

    try:
        lease = api.read_namespaced_lease(name=lease_name, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            # Lease does not exist – create it with this region as holder.
            body = client.V1Lease(
                metadata=client.V1ObjectMeta(name=lease_name),
                spec=client.V1LeaseSpec(
                    holder_identity=region_name,
                    lease_duration_seconds=lease_duration_seconds,
                ),
            )
            try:
                api.create_namespaced_lease(namespace=namespace, body=body)
                logger.info("Created lease %s for region %s", lease_name, region_name)
                return True
            except ApiException:
                logger.exception("Failed to create lease %s", lease_name)
                return False
        else:
            logger.exception("Error reading lease %s: %s", lease_name, exc)
            return False

    holder = lease.spec.holder_identity
    renew_time = lease.spec.renew_time
    now = time.time()
    # Convert renew_time (datetime) to timestamp if present.
    last_renew_ts = None
    if renew_time:
        try:
            last_renew_ts = renew_time.timestamp()
        except Exception:
            last_renew_ts = None

    # Determine if lease is stale.
    stale = False
    if not holder:
        stale = True
    elif last_renew_ts is not None and (now - last_renew_ts) > lease_duration_seconds:
        stale = True

    if not stale:
        # Lease is held by another region and still fresh.
        return holder == region_name

    # Attempt to claim/renew the lease.
    body = client.V1Lease(
        metadata=client.V1ObjectMeta(
            name=lease.metadata.name,
            resource_version=lease.metadata.resource_version,
        ),
        spec=client.V1LeaseSpec(
            holder_identity=region_name,
            lease_duration_seconds=lease_duration_seconds,
        ),
    )
    try:
        api.replace_namespaced_lease(name=lease.metadata.name, namespace=namespace, body=body)
        logger.info("Acquired lease %s for region %s", lease_name, region_name)
        return True
    except ApiException:
        logger.exception("Failed to acquire lease %s (conflict?)", lease_name)
        return False
