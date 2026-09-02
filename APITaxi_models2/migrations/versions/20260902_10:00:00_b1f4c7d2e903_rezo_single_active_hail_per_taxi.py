"""Rezo D16: at most one non-terminal hail per taxi

Upstream le.taxi reads `VehicleDescription.status == 'free'` in the hail
creation view, then writes `'answering'` some sixty lines later, with no row
lock in between. Under READ COMMITTED two simultaneous requests can both read
`free` and both retain the same taxi, leaving two customers with one cab.

A partial unique index makes that state unrepresentable, whichever code path
tries to reach it. It is the only place the guarantee can live: neither the
Next.js facade nor the mobile application can enforce it.

Revision ID: b1f4c7d2e903
Revises: 8d6592987ce1
Create Date: 2026-09-02 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b1f4c7d2e903'
down_revision = '8d6592987ce1'
branch_labels = None
depends_on = None


INDEX_NAME = 'uq_rezo_single_active_hail_per_taxi'

# Kept in sync with APITaxi_models2.hail.HAIL_TERMINAL_STATUS. Spelled out here
# rather than interpolated: a partial index predicate is stored in the
# catalogue, so it must not silently change meaning when the Python constant
# is edited. A mismatch is caught by the model test.
TERMINAL_STATUS = (
    'failure',
    'declined_by_taxi',
    'incident_taxi',
    'timeout_taxi',
    'declined_by_customer',
    'incident_customer',
    'timeout_customer',
    'timeout_accepted_by_customer',
    'finished',
)


def _terminal_status_sql():
    return ', '.join("'%s'" % status for status in TERMINAL_STATUS)


def upgrade():
    # Existing rows may already violate the rule: the pilot ran without this
    # guarantee. Close the oldest duplicates rather than fail the migration —
    # a hail left non-terminal on a taxi that has a newer one is stale by
    # definition, and refusing to migrate would leave the race open.
    op.execute(
        """
        UPDATE hail AS stale
        SET status = 'failure',
            last_status_change = NOW()
        WHERE stale.status NOT IN (%(terminal)s)
          AND EXISTS (
              SELECT 1
              FROM hail AS newer
              WHERE newer.taxi_id = stale.taxi_id
                AND newer.status NOT IN (%(terminal)s)
                AND (newer.added_at, newer.id) > (stale.added_at, stale.id)
          )
        """ % {'terminal': _terminal_status_sql()}
    )

    op.execute(
        """
        CREATE UNIQUE INDEX %(name)s
        ON hail (taxi_id)
        WHERE status NOT IN (%(terminal)s)
        """ % {'name': INDEX_NAME, 'terminal': _terminal_status_sql()}
    )


def downgrade():
    op.drop_index(INDEX_NAME, table_name='hail')
