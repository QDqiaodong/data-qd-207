"""地震到时事件归并与本地波形复核目录 API."""
from .service import process
from .review import (
    DatasetReleaseLedger,
    ReleaseConflictError,
    ReleaseLedger,
    ReleaseRequestError,
    ReviewDirectory,
    UnknownReleaseError,
    create_review,
    get_published_manifest,
    open_review,
    preview_dataset_release,
    preview_release,
    publish_dataset_release,
    publish_release,
    read_published_manifest,
    read_release,
)
from .event_catalog import (
    SCHEMA as EVENT_CATALOG_SCHEMA,
    CatalogStateError,
    reconcile_event_catalog,
    solve_global_associations,
)
from .lineage import (
    LineageError,
    UnknownCertificateError,
    UnknownNodeError,
)
from .location import (
    AMBIGUOUS as LOCATION_AMBIGUOUS,
    INSUFFICIENT_EVIDENCE as LOCATION_INSUFFICIENT_EVIDENCE,
    UNRESOLVED as LOCATION_UNRESOLVED,
    USABLE as LOCATION_USABLE,
    LocationEvaluator,
    LocationInputError,
    LocationReport,
    assess_event_location,
    assess_event_locations,
    assess_location_candidates,
    assess_locations,
    build_location_diagnostics,
    build_location_report,
    compute_event_locations,
    compute_locations,
    diagnose_event_locations,
    diagnose_locations,
    estimate_event_locations,
    estimate_locations,
    evaluate_candidate_locations,
    evaluate_event_location,
    evaluate_event_locations,
    evaluate_hypocenter_candidates,
    evaluate_location,
    evaluate_location_candidates,
    evaluate_locations,
    locate,
    locate_events,
    locate_hypocenters,
    rank_location_candidates,
    rank_locations,
)

__all__ = [
    'process',
    'ReviewDirectory',
    'create_review',
    'open_review',
    'preview_release',
    'publish_release',
    'preview_dataset_release',
    'publish_dataset_release',
    'read_release',
    'read_published_manifest',
    'get_published_manifest',
    'ReleaseLedger',
    'DatasetReleaseLedger',
    'ReleaseRequestError',
    'ReleaseConflictError',
    'UnknownReleaseError',
    'LineageError',
    'UnknownNodeError',
    'UnknownCertificateError',
    'reconcile_event_catalog',
    'solve_global_associations',
    'CatalogStateError',
    'EVENT_CATALOG_SCHEMA',
    'LOCATION_USABLE',
    'LOCATION_AMBIGUOUS',
    'LOCATION_INSUFFICIENT_EVIDENCE',
    'LOCATION_UNRESOLVED',
    'LocationInputError',
    'LocationEvaluator',
    'LocationReport',
    'evaluate_location_candidates',
    'evaluate_event_locations',
    'evaluate_event_location',
    'evaluate_locations',
    'evaluate_location',
    'evaluate_candidate_locations',
    'assess_location_candidates',
    'assess_locations',
    'assess_event_locations',
    'assess_event_location',
    'rank_location_candidates',
    'rank_locations',
    'diagnose_locations',
    'diagnose_event_locations',
    'build_location_report',
    'build_location_diagnostics',
    'compute_event_locations',
    'compute_locations',
    'estimate_event_locations',
    'estimate_locations',
    'evaluate_hypocenter_candidates',
    'locate_hypocenters',
    'locate_events',
    'locate',
]


def __getattr__(name):
    """未知的位置评估常见别名回退到 location 模块的兼容解析。"""
    if name.endswith('LocationError') or name in {
        'LocationValidationError', 'LocationEvaluationError'
    }:
        from .location import LocationInputError
        return LocationInputError
    from .location import __getattr__ as location_getattr
    try:
        return location_getattr(name)
    except AttributeError:
        pass
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
