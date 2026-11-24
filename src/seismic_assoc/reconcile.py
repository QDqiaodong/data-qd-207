"""模块名兼容别名：稳定事件目录身份 API 的实现位于 :mod:`event_catalog`。"""
from .event_catalog import *  # noqa: F401,F403
from .event_catalog import (  # noqa: F401
    SCHEMA,
    CatalogStateError,
    reconcile_event_catalog,
    solve_global_associations,
)
