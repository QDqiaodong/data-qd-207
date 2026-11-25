"""本地震级目录模块名兼容入口。"""
from .magnitude_catalog import *  # noqa: F401,F403
from .magnitude_catalog import (  # noqa: F401
    LocalMagnitudeCatalog,
    MagnitudeCatalog,
    MagnitudeCatalogError,
    MagnitudeVersionConflictError,
    create_local_magnitude_catalog,
    create_magnitude_catalog,
    open_local_magnitude_catalog,
    open_magnitude_catalog,
)
from .magnitude import apply_manifest_delta  # noqa: F401
