import logging
import os
import time

from config.settings import settings

logger = logging.getLogger("ledgerlens.soroban_lease")


class LeaseRenewalError(RuntimeError):
    """Raised when a lease cannot be renewed and publication must be paused."""


def _load_kube_config(config) -> None:
    """Load in‑cluster config, falling back to the local kube‑config file."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def _lease_settings():
    """Resolve lease-renewal tuning knobs from settings with safe defaults."""
    return {
        "renew_lead_seconds": getattr(settings, "soroban_lease_renew_lead_seconds", 10),
        "warn_window_seconds": getattr(settings, "soroban_lease_warn_window_seconds", 15),
        "max_renew_attempts": getattr(settings, "soroban_lease_max_renew_attempts", 3),
        "backoff_base_seconds": getattr(settings, "soroban_lease_backoff_base_seconds", 0.5),
    }


def _alert(message: str, *args) -> None:
    """Emit a loud, greppable alert for lease problems."""
    logger.error("LEASE ALERT: " + message, *args)


def _renew_lease(api, client, lease, region_name: str, lease_duration_seconds: int) -> bool:
    """Attempt to renew/claim the lease with retry and exponential backoff."""
    from kubernetes.client.rest import ApiException

    namespace = os.getenv("K8S_NAMESPACE", "default")
    cfg = _lease_settings()
    attempts = max(1, int(cfg["max_renew_attempts"]))
    base = max(0.0, float(cfg["backoff_base_seconds"]))

    for attempt in range(1, attempts + 1):
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
            api.replace_namespaced_lease(
                name=lease.metadata.name, namespace=namespace, body=body
            )
            logger.info(
                "Renewed lease %s for region %s (attempt %d/%d)",
                lease.metadata.name, region_name, attempt, attempts,
            )
            return True
        except ApiException as exc:
            logger.warning(
                "Lease renewal attempt %d/%d for %s failed: %s",
                attempt, attempts, lease.metadata.name, exc,
            )
            if attempt < attempts:
                time.sleep(base * (2 ** (attempt - 1)))
    return False


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
    If lease handling is disabled via ``settings.soroban_submission_lease_enabled``,
    returns ``True`` immediately without acquiring a lease.

    The ``kubernetes`` client library is imported lazily (only reached when
    lease handling is enabled) so that importing this module -- and anything
    that transitively imports it -- does not hard-require ``kubernetes`` to
    be installed for deployments that don't run multi-region Soroban
    submission (matching this codebase's existing lazy-import convention for
    optional heavy dependencies, e.g. ``dowhy`` in ``detection/causal_engine.py``).
    """
    # Short‑circuit if lease handling is disabled.
    lease_enabled = getattr(settings, "soroban_submission_lease_enabled", None)
    if lease_enabled is None:
        logger.warning("soroban_submission_lease_enabled not set in config; defaulting to enabled")
        lease_enabled = True
    if not lease_enabled:
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
            logger.error("Error reading lease %s: %s", lease_name, exc)
            return False

    holder = lease.spec.holder_identity
    renew_time = lease.spec.renew_time
    now = time.time()
    # Convert renew_time (datetime) to timestamp if present.
    last_renew_ts = None
    if renew_time:
        try:
            last_renew_ts = renew_time.timestamp()
        except (AttributeError, TypeError, ValueError) as e:
            logger.warning("Failed to parse renew_time: %s", e)
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
    if not _renew_lease(api, client, lease, region_name, lease_duration_seconds):
        _alert(
            "Failed to renew lease %s for region %s after retries; "
            "pausing publication to avoid publishing with an invalid lease",
            lease_name, region_name,
        )
        return False
    return True


def renew_submission_lease(region_name: str, lease_duration_seconds: int = 30) -> bool:
    """Proactively renew the submission lease well before it expires.

    This is the backpressure entry point: callers should invoke it on a
    cadence shorter than ``lease_duration_seconds`` so renewal happens ahead
    of expiry. It renews with retry/backoff, raises a loud alert when the
    lease is within the configurable warning window of lapsing unrenewed, and
    raises :class:`LeaseRenewalError` (a hard failure) so dependent
    publication is paused rather than proceeding with an invalid lease.
    """
    lease_enabled = getattr(settings, "soroban_submission_lease_enabled", None)
    if lease_enabled is None:
        lease_enabled = True
    if not lease_enabled:
        return True

    from kubernetes import client, config
    from kubernetes.client.rest import ApiException

    _load_kube_config(config)
    api = client.CoordinationV1Api()
    lease_name = settings.soroban_submission_lease_name
    namespace = os.getenv("K8S_NAMESPACE", "default")
    cfg = _lease_settings()

    try:
        lease = api.read_namespaced_lease(name=lease_name, namespace=namespace)
    except ApiException as exc:
        _alert("Unable to read lease %s for renewal: %s", lease_name, exc)
        raise LeaseRenewalError(f"cannot read lease {lease_name}: {exc}") from exc

    renew_time = lease.spec.renew_time
    last_renew_ts = None
    if renew_time:
        try:
            last_renew_ts = renew_time.timestamp()
        except (AttributeError, TypeError, ValueError) as e:
            logger.warning("Failed to parse renew_time: %s", e)

    now = time.time()
    remaining = None
    if last_renew_ts is not None:
        remaining = lease_duration_seconds - (now - last_renew_ts)

    # Alert when the lease is approaching expiry unrenewed.
    if remaining is not None and remaining <= cfg["warn_window_seconds"]:
        _alert(
            "Lease %s for region %s expires in %.1fs (<= warn window %ss); "
            "renewing now",
            lease_name, region_name, remaining, cfg["warn_window_seconds"],
        )

    # Renew proactively when within the lead window (or already stale).
    if remaining is None or remaining <= cfg["renew_lead_seconds"]:
        if not _renew_lease(api, client, lease, region_name, lease_duration_seconds):
            _alert(
                "Lease %s for region %s could not be renewed; "
                "pausing dependent publication",
                lease_name, region_name,
            )
            raise LeaseRenewalError(
                f"lease {lease_name} could not be renewed for region {region_name}"
            )
    return True
