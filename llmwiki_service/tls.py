"""The TLS context and its reload without downtime.

Phase 2 of docs/plans/02-llmwiki-service/phase-02-tls.md owes reload
without downtime: a certificate renewed on disk gets served to new
connections without dropping an in-flight one, and a broken renewal
must not take the service down.

`asyncio` fixes the `SSLContext` object identity a server was started
with; there is no public way to hand a running server a different
object per connection. The standard trick, and the one used here, is
an SNI callback: the object passed to the server never changes, but
its callback swaps each new connection onto whatever `ContextHolder`
currently holds, before that connection's certificate is chosen. An
already established connection has already made that choice and is
unaffected. Proved standalone before being wired in here; see the
reload proof in the phase 2 report.

`reload` is the one function that builds a new context and publishes
it. Both the file watcher and the `SIGHUP` handler in `__main__.py`
call this same function, so there is exactly one reload path.
"""
from __future__ import annotations

import logging
import os
import ssl
import threading

MIN_VERSIONS = {"1.2": ssl.TLSVersion.TLSv1_2, "1.3": ssl.TLSVersion.TLSv1_3}
DEFAULT_MIN_VERSION = "1.2"  # matches the example in phase-02-tls.md


def build_context(cert: str, key: str, min_version: str | None) -> ssl.SSLContext:
    """A fresh server `SSLContext` for `cert` and `key`.

    `ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)` per the
    phase's rule against hand-written cipher lists: only the minimum
    version is set. Raises `ssl.SSLError`, `OSError`, or `ValueError`
    on a certificate or key that does not load; the caller decides
    whether that is a startup refusal or a logged, discarded reload.
    """
    version = min_version or DEFAULT_MIN_VERSION
    if version not in MIN_VERSIONS:
        raise ValueError(f"[tls] min_version is not 1.2 or 1.3: {version!r}")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = MIN_VERSIONS[version]
    context.load_cert_chain(cert, key)
    return context


class ContextHolder:
    """The one place the serving path reads the live `SSLContext`.

    `current` is read once per connection, by the SNI callback wired
    to this holder in `server_context`. `reload` is the one place a
    new context is published. Both are safe to call from any thread:
    the lock only guards the build-then-publish step against two
    reloads racing each other, since attribute assignment already
    hands out either the whole old context or the whole new one.
    """

    def __init__(self, context: ssl.SSLContext) -> None:
        self.current = context
        self._lock = threading.Lock()

    def reload(
        self, cert: str, key: str, min_version: str | None, logger: logging.Logger
    ) -> bool:
        """Build a context from `cert` and `key` and publish it.

        A context that fails to build is logged and the previous one
        stays live, per the phase's rule that a broken renewal must
        not take the service down. Returns whether the reload took
        effect, so a caller (the watcher, `SIGHUP`) can log the outcome
        once rather than each duplicating the message.
        """
        try:
            new_context = build_context(cert, key, min_version)
        except (ssl.SSLError, OSError, ValueError) as exc:
            logger.error(
                "certificate reload failed, keeping the previous context: %s", exc
            )
            return False
        with self._lock:
            self.current = new_context
        logger.info("certificate reloaded from %s", cert)
        return True


def server_context(holder: ContextHolder) -> ssl.SSLContext:
    """The one `SSLContext` object handed to the server at startup.

    Its identity never changes for the life of the process; only its
    SNI callback runs per connection, reading `holder.current` at that
    moment. `holder.current` itself starts as this same object, so a
    connection made before any reload still gets a real certificate
    even if, for any reason, the callback did not run.
    """
    context = holder.current

    def sni_callback(
        ssl_socket: ssl.SSLObject, server_name: str | None, ssl_context: ssl.SSLContext
    ) -> None:
        ssl_socket.context = holder.current

    context.set_servername_callback(sni_callback)
    return context


def _mtimes(cert: str, key: str) -> tuple[float, float] | None:
    """`(cert mtime, key mtime)`, or `None` when either path is gone.

    A momentarily missing file, mid-renewal, reads as unchanged rather
    than as a reload trigger: the next poll sees the finished write.
    """
    try:
        return os.stat(cert).st_mtime, os.stat(key).st_mtime
    except OSError:
        return None


def watch_certificates(
    holder: ContextHolder,
    cert: str,
    key: str,
    min_version: str | None,
    logger: logging.Logger,
    stop_event: threading.Event,
    interval_sec: float = 1.0,
) -> None:
    """Poll `cert` and `key` for a changed mtime and reload on change.

    Runs until `stop_event` is set. Meant to be the target of a daemon
    thread; `__main__.py` starts one before binding a socket.
    """
    last = _mtimes(cert, key)
    while not stop_event.wait(interval_sec):
        current = _mtimes(cert, key)
        if current is not None and current != last:
            holder.reload(cert, key, min_version, logger)
            last = current
