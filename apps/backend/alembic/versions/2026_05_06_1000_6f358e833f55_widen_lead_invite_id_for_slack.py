"""widen lead.invite_id from 64 to 128 chars (slack support)

Revision ID: 6f358e833f55
Revises: afb542f2f12d
Create Date: 2026-05-06 10:00:00.000000

WhatsApp invite_ids are 22 chars and Discord codes top out at 32, so the
old 64-char column was generous for both. Slack invites are different:
each one is identified by a `<workspace>/<token>` pair where workspace
names are 10-30 chars and the new-format tokens add another 30-40, so
the worst case lands around 60-80 chars. 64 is borderline tight, 128
gives plenty of headroom for any future format change.

Forward-only: existing values fit in the wider column without conversion.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6f358e833f55'
down_revision: Union[str, None] = 'afb542f2f12d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "leads",
        "invite_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=128),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Slack-format invite_ids may exceed 64 chars; downgrading would silently
    # truncate. Block by raising — caller can manually delete Slack rows first
    # if they really want to revert.
    op.alter_column(
        "leads",
        "invite_id",
        existing_type=sa.String(length=128),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
