"""beoordeling: status (klad/definitief) en werkwijze (blind/overgenomen)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-17 14:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Bestaande beoordelingen zijn definitief; hun werkwijze is onbekend (leeg).
    with op.batch_alter_table('beoordelingen', schema=None) as batch_op:
        batch_op.add_column(sa.Column('status', sa.String(length=12), server_default='definitief', nullable=False))
        batch_op.add_column(sa.Column('werkwijze', sa.String(length=12), server_default='', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('beoordelingen', schema=None) as batch_op:
        batch_op.drop_column('werkwijze')
        batch_op.drop_column('status')
