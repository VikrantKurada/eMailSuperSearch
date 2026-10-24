"""Project database: connection helper and schema migrations."""

from emailsupersearch.db.connection import MissingSqliteFeature, connect
from emailsupersearch.db.schema import (
    MIGRATIONS,
    SCHEMA_VERSION,
    SchemaTooNew,
    migrate,
    schema_version,
)

__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "MissingSqliteFeature", "SchemaTooNew", "connect", "migrate", "schema_version"]
