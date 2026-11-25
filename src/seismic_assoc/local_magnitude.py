"""局部震级 API 的兼容模块名。"""
from .magnitude import *  # noqa: F401,F403
from .magnitude import (  # noqa: F401
    CHANGE_PACKAGE_SCHEMA,
    MANIFEST_SCHEMA,
    SCHEMA,
    ManifestDeltaError,
    MagnitudeInputError,
    apply_change_package,
    apply_manifest_delta,
    apply_magnitude_delta,
    build_change_package,
    build_manifest,
    estimate_local_magnitudes,
    great_circle_distance_km,
    median_float,
)
from .magnitude_catalog import (  # noqa: F401
    LocalMagnitudeCatalog,
    MagnitudeCatalog,
    create_local_magnitude_catalog,
    create_magnitude_catalog,
    open_local_magnitude_catalog,
    open_magnitude_catalog,
)
