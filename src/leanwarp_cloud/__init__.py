"""Client for hosted LeanWarp; mathematical execution remains server-side."""

from ._version import __version__
from .client import LeanWarpCloud
from .config import load_client
from .errors import (
    ConfigurationError,
    EditError,
    LeanWarpCloudError,
    LeanWarpError,
    LeanWarpTransportError,
    OperationTimeout,
    ProjectError,
    SessionError,
    UsageError,
    WaitError,
)
from .outcome import OperationOutcome
from .project import UploadPlan, collect_lean_sources, plan_upload
from .session import ProjectSession, check_project

__all__ = [
    "ConfigurationError",
    "EditError",
    "LeanWarpCloud",
    "LeanWarpCloudError",
    "LeanWarpError",
    "LeanWarpTransportError",
    "OperationOutcome",
    "OperationTimeout",
    "ProjectError",
    "ProjectSession",
    "SessionError",
    "UploadPlan",
    "UsageError",
    "WaitError",
    "__version__",
    "check_project",
    "collect_lean_sources",
    "load_client",
    "plan_upload",
]
