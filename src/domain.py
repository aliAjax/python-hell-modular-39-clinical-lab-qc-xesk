from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict


class DomainError(Exception):
    """Base class for domain failures."""


class ValidationError(DomainError):
    """Input does not satisfy a domain rule."""


class PermissionDenied(DomainError):
    """Actor is not allowed to perform the action."""


class NotFoundError(DomainError):
    """Requested record does not exist."""


class ConflictError(DomainError):
    """A uniqueness or version constraint was violated."""


class InvalidTransition(DomainError):
    """The requested state transition is not valid."""


class ReleaseBlocked(ConflictError):
    """Release preconditions failed; the result batch keeps its current status."""

    def __init__(self, reasons, context=None):
        self.reasons = [str(reason) for reason in reasons]
        self.context = dict(context or {})
        message = "; ".join(self.reasons)
        super().__init__(message)


class Role(str, Enum):
    viewer = "viewer"
    reporter = "reporter"
    operator = "operator"
    analyst = "analyst"
    coordinator = "coordinator"
    supervisor = "supervisor"
    auditor = "auditor"
    admin = "admin"


@dataclass
class Actor:
    user_id: str
    role: str

    @classmethod
    def from_headers(cls, headers):
        user_id = headers.get("X-User-Id", "anonymous")
        role = headers.get("X-Role", "viewer")
        if role not in {item.value for item in Role}:
            raise PermissionDenied("unknown role: " + role)
        return cls(user_id=user_id, role=role)


@dataclass
class Entity:
    id: str
    kind: str
    status: str
    version: int
    data: Dict[str, Any]
    created_by: str
    created_at: str
    updated_at: str
