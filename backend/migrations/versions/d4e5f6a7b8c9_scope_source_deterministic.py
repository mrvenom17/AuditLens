"""Add `deterministic` to the scope_source enum.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-01

Deliberately a migration of its own, containing exactly one statement.

Postgres refuses to use a newly-added enum value in the same transaction that
adds it. Alembic runs each migration in a transaction, so `ALTER TYPE ... ADD
VALUE` has to be isolated from anything that writes the new member — and here it
is isolated even from the rest of the schema change, so a failure at this step
cannot roll back the column additions in c3d4e5f6a7b8.

`autocommit_block()` takes the statement outside the migration transaction,
which is what makes the ADD VALUE legal at all.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE scope_source ADD VALUE IF NOT EXISTS 'deterministic'")


def downgrade() -> None:
    """Not supported.

    Postgres cannot drop a value from an enum type. Removing it would mean
    recreating the type and rewriting every column that uses it — for no benefit,
    since an unused enum member is inert.
    """
    raise NotImplementedError("Enum values cannot be removed from a Postgres enum type.")
