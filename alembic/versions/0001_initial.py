"""Initial schema: all 20 tables + PG-only artifacts.

Tables are created from the SQLAlchemy metadata so the migration can never
drift from app/core/models.py. PG-only artifacts (the append-only trigger on
audit_events per FR-026, and the GIN index on sam_notices.naics per the
section-5 index strategy) are applied only on PostgreSQL.

Downgrade is a supported one-revision rollback: CHECK constraints and the
trigger drop cleanly (VARCHAR+CHECK, never PG enums).
"""

from __future__ import annotations

from alembic import op

from app.core.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION prevent_audit_modification()
RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_events is append-only';
END;
$$ LANGUAGE plpgsql;
"""

APPEND_ONLY_TRIGGER = """
CREATE TRIGGER audit_events_no_update_delete
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();
"""


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind)
    if bind.dialect.name == "postgresql":
        op.execute(APPEND_ONLY_FUNCTION)
        op.execute(APPEND_ONLY_TRIGGER)
        op.execute("CREATE INDEX ix_sam_notices_naics ON sam_notices USING GIN (naics)")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_events_no_update_delete ON audit_events")
        op.execute("DROP FUNCTION IF EXISTS prevent_audit_modification()")
        op.execute("DROP INDEX IF EXISTS ix_sam_notices_naics")
    Base.metadata.drop_all(bind)
