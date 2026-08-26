import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import ip_address
from time import monotonic


@dataclass(frozen=True, slots=True)
class RatePolicy:
    capacity: float
    refill_per_second: float


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float
    last_seen: float


_GENERAL_CLIENT = RatePolicy(240, 4)
_GENERAL_GLOBAL = RatePolicy(2400, 40)
_OUTLINE_CLIENT = RatePolicy(20, 1 / 3)
_OUTLINE_GLOBAL = RatePolicy(200, 10 / 3)


class PublicQuizRateLimiter:
    def __init__(
        self,
        *,
        general_client: RatePolicy = _GENERAL_CLIENT,
        general_global: RatePolicy = _GENERAL_GLOBAL,
        outline_client: RatePolicy = _OUTLINE_CLIENT,
        outline_global: RatePolicy = _OUTLINE_GLOBAL,
        max_client_buckets: int = 4096,
        idle_seconds: float = 3600,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_client_buckets < 1:
            raise ValueError("max_client_buckets must be positive")
        self._client_policies = {
            "general": general_client,
            "outline": outline_client,
        }
        self._global_policies = {
            "general": general_global,
            "outline": outline_global,
        }
        self._max_client_buckets = max_client_buckets
        self._idle_seconds = idle_seconds
        self._clock = clock
        self._clients: dict[tuple[str, str], _Bucket] = {}
        self._globals: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(
        self,
        client_id: str,
        category: str = "general",
    ) -> RateLimitDecision:
        client_policy = self._client_policies[category]
        global_policy = self._global_policies[category]
        now = self._clock()
        with self._lock:
            self._prune(now)
            client_key = (category, client_id)
            if client_key not in self._clients:
                self._make_room()
            client = self._bucket(
                self._clients,
                client_key,
                client_policy,
                now,
            )
            global_bucket = self._bucket(
                self._globals,
                category,
                global_policy,
                now,
            )
            waits = (
                self._wait_seconds(client, client_policy),
                self._wait_seconds(global_bucket, global_policy),
            )
            retry_after = max(waits)
            if retry_after > 0:
                return RateLimitDecision(False, max(1, math.ceil(retry_after)))
            client.tokens -= 1
            global_bucket.tokens -= 1
            return RateLimitDecision(True, 0)

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, bucket in self._clients.items()
            if now - bucket.last_seen > self._idle_seconds
        ]
        for key in expired:
            self._clients.pop(key, None)

    def _make_room(self) -> None:
        if len(self._clients) < self._max_client_buckets:
            return
        oldest = min(
            self._clients,
            key=lambda key: self._clients[key].last_seen,
        )
        del self._clients[oldest]

    @staticmethod
    def _bucket[K](
        buckets: dict[K, _Bucket],
        key: K,
        policy: RatePolicy,
        now: float,
    ) -> _Bucket:
        bucket = buckets.get(key)
        if bucket is None:
            bucket = _Bucket(policy.capacity, now, now)
            buckets[key] = bucket
            return bucket
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(
            policy.capacity,
            bucket.tokens + elapsed * policy.refill_per_second,
        )
        bucket.updated_at = now
        bucket.last_seen = now
        return bucket

    @staticmethod
    def _wait_seconds(bucket: _Bucket, policy: RatePolicy) -> float:
        if bucket.tokens >= 1:
            return 0
        if policy.refill_per_second <= 0:
            return 60
        return (1 - bucket.tokens) / policy.refill_per_second


def public_client_identifier(
    forwarded_address: str | None,
    peer_address: str | None,
    *,
    trusted_proxy_addresses: frozenset[str] = frozenset({"127.0.0.1", "::1"}),
) -> str:
    peer = peer_address.strip() if peer_address else None
    try:
        normalized_peer = str(ip_address(peer)) if peer else None
    except ValueError:
        normalized_peer = peer
    if (
        normalized_peer in trusted_proxy_addresses
        and forwarded_address
        and "," not in forwarded_address
    ):
        candidate = forwarded_address.strip()
        try:
            return str(ip_address(candidate))
        except ValueError:
            pass
    if normalized_peer:
        return normalized_peer
    return "unknown"
