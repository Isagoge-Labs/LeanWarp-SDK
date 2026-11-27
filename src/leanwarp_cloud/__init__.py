"""Client for hosted LeanWarp; mathematical execution remains server-side."""

from ._version import __version__
from .aio import AsyncLeanWarpCloud
from .bulk import target_sha256
from .client import LeanWarpCloud
from .config import load_client
from .errors import (
    ConfigurationError,
    ConnectTimeout,
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
    "AsyncLeanWarpCloud",
    "ConfigurationError",
    "ConnectTimeout",
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
    "target_sha256",
]
