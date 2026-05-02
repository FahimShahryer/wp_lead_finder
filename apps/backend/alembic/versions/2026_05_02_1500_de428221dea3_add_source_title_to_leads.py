"""add source_title to leads

Revision ID: de428221dea3
Revises: 2ba678758236
Create Date: 2026-05-02 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'de428221dea3'
down_revision: Union[str, None] = '2ba678758236'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('leads', sa.Column('source_title', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('leads', 'source_title')
