"""Every failure the SDK raises derives from LeanWarpError."""

from __future__ import annotations

from typing import Any, cast

#: Responses that mean "not now" rather than "no".
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
# Raised for a response the SDK could not trust; sending it again would not help.
_RESPONSE_FAULTS = frozenset({"invalid_response", "invalid_encoding", "response_too_large"})


def _restore(kind: type[LeanWarpError], message: str, fields: dict[str, Any]) -> LeanWarpError:
    error = kind.__new__(kind)
    Exception.__init__(error, message)
    error.__dict__.update(fields)
    return error


class LeanWarpError(Exception):
    """A LeanWarp failure; ``str(error)`` is ``message``.

    ``code`` is stable, so branch on it. ``status`` is the HTTP status, or None if
    no response decided the failure. ``retryable`` says the same request may
    succeed if sent again; ``retry_after`` is how many seconds the service asked to
    wait first. ``idempotency_key`` is the key a write was sent with: resending the
    same request with it can never run the request twice.
    """

    default_code = "local_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        self.idempotency_key = idempotency_key

    def __reduce__(self) -> tuple[Any, ...]:
        # Training jobs pass failures between processes; a subclass's __init__
        # signature differs from Exception's, so rebuild from the fields.
        return _restore, (type(self), self.message, dict(self.__dict__))


class LeanWarpCloudError(LeanWarpError):
    """The service refused the request, or sent a response the SDK could not trust."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retry_after: float | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            status=status,
            retryable=status in RETRYABLE_STATUSES and code not in _RESPONSE_FAULTS,
            retry_after=retry_after,
            idempotency_key=idempotency_key,
        )

    @property
    def status_code(self) -> int:
        """The HTTP status, as ``status``; the name earlier releases used."""
        return cast(int, self.status)


class LeanWarpTransportError(LeanWarpError):
    """No usable response arrived, so a write may or may not have taken effect."""

    default_code = "transport_error"

    def __init__(
        self, message: str, *, timed_out: bool = False, idempotency_key: str | None = None
    ) -> None:
        super().__init__(message, retryable=True, idempotency_key=idempotency_key)
        self.timed_out = timed_out


class OperationTimeout(LeanWarpError, TimeoutError):
    """Waiting expired; the operation remains addressable and may still be running."""

    default_code = "poll_timeout"

    def __init__(self, operation_id: str, operation: dict[str, Any] | None = None) -> None:
        super().__init__(
            f"operation {operation_id} is still pending; poll again or cancel it", retryable=True
        )
        self.operation_id = operation_id
        # The last non-terminal observation, when polling read one.
        self.operation = operation


class SessionError(LeanWarpError):
    """Local recovery or project reconciliation is required before another write."""


class UsageError(LeanWarpError, ValueError):
    """An argument outside its supported range; nothing was sent."""


class WaitError(UsageError):
    """An inline wait outside its supported range; nothing was submitted."""


class ProjectError(LeanWarpError, ValueError):
    """An actionable project error that contains no file contents or credentials."""


class ConfigurationError(LeanWarpError, ValueError):
    """An actionable configuration failure containing no credential material."""


class EditError(LeanWarpError, ValueError):
    """A result that cannot be applied to this source."""
