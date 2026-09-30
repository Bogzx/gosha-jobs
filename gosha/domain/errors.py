"""Domain exceptions raised by services; interface layers translate them.

The web API maps these to HTTP envelopes (gosha/api/app.py); the Discord
bot maps them to friendly messages. Services never raise HTTP concepts.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base for expected business-rule failures."""

    code = "error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    code = "not_found"


class TierLimitError(DomainError):
    code = "tier_limit"


class QuotaExceededError(DomainError):
    code = "quota_exceeded"


class ValidationError(DomainError):
    code = "invalid_request"


class InvalidStatusError(DomainError):
    code = "invalid_status"


class NoCvError(DomainError):
    code = "no_cv"


class ConsentRequiredError(DomainError):
    code = "consent_required"


class FileTooLargeError(DomainError):
    code = "file_too_large"


class GenerationFailedError(DomainError):
    code = "generation_failed"
