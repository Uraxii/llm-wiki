"""The gate. One callable turns an Authorization header, an operation,
and a deployment's `[access]` policy into a principal or a refusal.

No web framework types appear here on purpose: whatever serves the
routes decides how a `Refusal` becomes a response, and phase 1 does not
assume what that is. The `[access]` mapping arrives already parsed,
because the deployment file has exactly one parser and it is not this
module.
"""
from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass

from llmwiki_service.tokens import TokenStore

# An operation names what a route does; a role names what a token may
# do. Roles rank, because a writer that cannot read is not a thing, and
# admin is honestly a superset: an admin can mint itself a writer token,
# so any narrower separation would be decorative.
ROLE_FOR_OPERATION = {"read": "reader", "write": "writer", "admin": "admin"}
ROLE_RANK = {"reader": 1, "writer": 2, "admin": 3}

OPEN = "open"
BEARER = "bearer"

MAX_FAILURES_PER_WINDOW = 10
FAILURE_WINDOW_SEC = 60
MAX_TRACKED_CLIENTS = 10_000


@dataclass(frozen=True)
class Principal:
    """A caller the service recognised. Carries the `<id>` segment and
    the label, which are what a log line may name, and never the token."""

    token_id: str
    label: str
    role: str


@dataclass(frozen=True)
class Refusal:
    """Why a caller got nothing. Every authentication failure returns
    the one `UNAUTHORIZED` value, so a caller cannot tell an unknown id
    from a wrong secret, a revoked row, or an expired one."""

    reason: str


ANONYMOUS = Principal(token_id="", label="anonymous", role="reader")
UNAUTHORIZED = Refusal("unauthorized")
RATE_LIMITED = Refusal("rate_limited")


class FailureLimiter:
    """A fixed window count of failed authentications per caller.

    Deliberately not a general rate limiter: it guards this one path,
    and `[limits]` in the deployment file is a separate budget for a
    separate purpose.

    ponytail: a fixed window lets a caller spend up to twice the budget
    across a window edge. A sliding window if that ever matters.
    """

    def __init__(
        self,
        limit: int = MAX_FAILURES_PER_WINDOW,
        window_sec: int = FAILURE_WINDOW_SEC,
    ) -> None:
        self.limit = limit
        self.window_sec = window_sec
        # Ordered oldest-window-first. A client is only ever appended
        # or moved to the end (on a fresh window), so under the real
        # monotonic clock the front is always the entry closest to
        # expiry, which is what makes `_forget_expired` below cheap.
        self._windows: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def blocked(self, client: str, now: float | None = None) -> bool:
        """True while `client` has spent its budget in the open window."""
        started, count = self._windows.get(client, (0.0, 0))
        moment = time.monotonic() if now is None else now
        return count >= self.limit and moment - started < self.window_sec

    def record_failure(self, client: str, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        existing = self._windows.get(client)
        if existing is not None:
            started, count = existing
            if moment - started >= self.window_sec:
                started, count = moment, 0
                self._windows.move_to_end(client)
            self._windows[client] = (started, count + 1)
            return
        self._forget_expired(moment)
        if len(self._windows) >= MAX_TRACKED_CLIENTS:
            # ponytail: the table is full of windows that are all still
            # open, so there is no closed window to reclaim. Refusing
            # to track this client (rather than evicting the oldest
            # live one) is the deliberate cut: plain LRU eviction here
            # would let a flood of cap-many distinct client strings
            # evict an unrelated client's active block, which is the
            # reset attack the cap exists to prevent. The cost is that
            # a client arriving while the table is this saturated goes
            # unrated for that stretch, since nothing records it.
            return
        self._windows[client] = (moment, 1)

    def tracked(self) -> list[str]:
        """The callers currently holding a window."""
        return list(self._windows)

    def _forget_expired(self, moment: float) -> None:
        """Drop closed windows from the front, oldest first, stopping at
        the first window still open. Each entry is popped at most once
        over its lifetime, so the total cost across every call is
        proportional to the number of clients ever seen: `record_failure`
        is amortized O(1) rather than the O(n) full-table sweep this
        replaces, and a table with no closed windows costs one peek."""
        while self._windows:
            started, _count = next(iter(self._windows.values()))
            if moment - started < self.window_sec:
                break
            self._windows.popitem(last=False)


SEARCH_WINDOW_SEC = 60
MAX_TRACKED_SEARCH_CLIENTS = 10_000
DEFAULT_SEARCH_PER_MINUTE = 30  # fallback only when [limits] is absent from
                                 # the deployment; phase 2's own example sets it


class SearchLimiter:
    """A sliding window count of accepted `search` calls per caller.

    Unlike `FailureLimiter`'s fixed window, this records every accepted
    call's own timestamp and only counts the ones still inside the last
    `window_sec`. A fixed window lets a caller spend up to twice its
    budget across a window edge; this one does not, which is what the
    phase 3 spec's own test drives: sixty requests spanning a minute
    boundary in two seconds must return 429.

    Same eviction discipline as `FailureLimiter`, and for the same
    reason: when the table is saturated with clients that are ALL still
    inside their window, there is nothing safely reclaimable, so a new
    client goes untracked (and unlimited) for that stretch rather than
    evicting a live client's window, which would be the reset attack
    `FailureLimiter` already refuses.
    """

    def __init__(self, limit: int, window_sec: int = SEARCH_WINDOW_SEC) -> None:
        self.limit = limit
        self.window_sec = window_sec
        self._calls: OrderedDict[str, deque] = OrderedDict()

    def check(self, client: str, now: float | None = None) -> int | None:
        """`None` when `client` may proceed (and this call is recorded
        against its window); otherwise the whole seconds, rounded up,
        until its oldest in-window call expires, the caller's
        `Retry-After` value."""
        moment = time.monotonic() if now is None else now
        timestamps = self._calls.get(client)
        if timestamps is not None:
            self._calls.move_to_end(client)
            self._prune(timestamps, moment)
        else:
            self._forget_expired(moment)
            if len(self._calls) >= MAX_TRACKED_SEARCH_CLIENTS:
                return None  # untracked while saturated, same cut as FailureLimiter
            timestamps = deque()
            self._calls[client] = timestamps
        if len(timestamps) >= self.limit:
            return max(1, math.ceil(self.window_sec - (moment - timestamps[0])))
        timestamps.append(moment)
        return None

    def _prune(self, timestamps: deque, moment: float) -> None:
        while timestamps and moment - timestamps[0] >= self.window_sec:
            timestamps.popleft()

    def _forget_expired(self, moment: float) -> None:
        while self._calls:
            _client, timestamps = next(iter(self._calls.items()))
            if not timestamps or moment - timestamps[-1] >= self.window_sec:
                self._calls.popitem(last=False)
                continue
            break


def bearer_token(header: str | None) -> str | None:
    """The token in an `Authorization: Bearer <token>` header, else
    `None`. RFC 7235 makes the scheme name case insensitive; the token
    is not."""
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != BEARER:
        return None
    return token.strip() or None


def authorize(
    header: str | None,
    operation: str,
    access: Mapping[str, str],
    store: TokenStore,
    limiter: FailureLimiter,
    client: str,
) -> Principal | Refusal:
    """Resolve `header` against the policy for `operation`.

    Only `read` can be open, and only on the exact word `open`: any
    other value, a missing key, and an operation no policy names are
    all closed. That is the restrictive default read literally, so a
    deployment file that slips past its own startup checks still
    cannot open a write.

    A caller that presents a token is judged on that token even where
    reads are open, because silently downgrading a bad token to an
    anonymous read hides the failure from whoever has to fix it.
    """
    required = ROLE_FOR_OPERATION.get(operation)
    if required is None:
        return UNAUTHORIZED
    if limiter.blocked(client):
        return RATE_LIMITED
    token = bearer_token(header)
    if token is None:
        if operation == "read" and access.get("read") == OPEN:
            return ANONYMOUS
        return _refuse(limiter, client)
    row = store.verify(token)
    if row is None or ROLE_RANK.get(row.role, 0) < ROLE_RANK[required]:
        return _refuse(limiter, client)
    return Principal(token_id=row.id, label=row.label, role=row.role)


def _refuse(limiter: FailureLimiter, client: str) -> Refusal:
    limiter.record_failure(client)
    return UNAUTHORIZED
