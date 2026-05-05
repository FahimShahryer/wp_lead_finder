"""add platform column to campaigns

Revision ID: afb542f2f12d
Revises: 5383effacce1
Create Date: 2026-05-05 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'afb542f2f12d'
down_revision: Union[str, None] = '5383effacce1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Single-platform-per-campaign — each campaign hunts for invites on
    # exactly one platform. Default 'whatsapp' so existing campaigns keep
    # their exact current behavior.
    op.add_column(
        "campaigns",
        sa.Column(
            "platform",
            sa.String(length=16),
            nullable=False,
            server_default="whatsapp",
        ),
    )
    op.create_index("ix_campaigns_platform", "campaigns", ["platform"])


def downgrade() -> None:
    op.drop_index("ix_campaigns_platform", table_name="campaigns")
    op.drop_column("campaigns", "platform")
