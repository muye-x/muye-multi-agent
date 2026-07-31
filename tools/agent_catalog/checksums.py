"""兼容导出 Catalog checksum；实现位于无包名冲突的 `contracts.catalog`。"""

from contracts.catalog import (
    CATALOG_SCHEMA_VERSION,
    build_catalog_snapshot,
    capabilities_checksum_from_response,
    capabilities_identity_checksum,
    capabilities_identity_projection,
    validate_catalog_snapshot_checksum,
)

__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "build_catalog_snapshot",
    "capabilities_checksum_from_response",
    "capabilities_identity_checksum",
    "capabilities_identity_projection",
    "validate_catalog_snapshot_checksum",
]
