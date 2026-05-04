"""add lead_source_urls junction + source_count on leads

Revision ID: 5383effacce1
Revises: de428221dea3
Create Date: 2026-05-04 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5383effacce1'
down_revision: Union[str, None] = 'de428221dea3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Denormalized count of distinct source URLs that discovered each lead.
    op.add_column(
        "leads",
        sa.Column(
            "source_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )

    # Junction table holding the actual (lead, url) pairs so we can dedupe
    # and recompute source_count exactly. ON CONFLICT DO NOTHING in the
    # extractor lets us update source_count atomically only when a genuinely
    # new URL is recorded.
    op.create_table(
        "lead_source_urls",
        sa.Column("lead_id", sa.BigInteger(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "first_seen",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("lead_id", "url"),
    )


def downgrade() -> None:
    op.drop_table("lead_source_urls")
    op.drop_column("leads", "source_count")
