"""eerste schema

Revision ID: 0001
Revises: 
Create Date: 2026-11-17 11:51:50.954988
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('instellingen',
    sa.Column('sleutel', sa.String(length=60), nullable=False),
    sa.Column('waarde', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('sleutel')
    )
    op.create_table('modelkoppeling',
    sa.Column('model_id', sa.String(length=40), nullable=False),
    sa.Column('openwebui_id', sa.Text(), nullable=False),
    sa.Column('naam', sa.Text(), nullable=False),
    sa.Column('bevestigd_op', sa.Date(), nullable=True),
    sa.Column('bron', sa.String(length=40), nullable=False),
    sa.PrimaryKeyConstraint('model_id')
    )
    op.create_table('modellen',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('model_id', sa.String(length=40), nullable=False),
    sa.Column('naam', sa.Text(), nullable=False),
    sa.Column('rechtsgebied', sa.Text(), nullable=False),
    sa.Column('versie', sa.String(length=40), nullable=False),
    sa.Column('basismodel', sa.Text(), nullable=False),
    sa.Column('systeemprompt_versie', sa.Text(), nullable=False),
    sa.Column('kennisbank', sa.Text(), nullable=False),
    sa.Column('tools', sa.Text(), nullable=False),
    sa.Column('temperatuur', sa.String(length=20), nullable=False),
    sa.Column('eigenaar', sa.Text(), nullable=False),
    sa.Column('datum_versie', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=40), nullable=False),
    sa.Column('opmerkingen', sa.Text(), nullable=False),
    sa.Column('aangemaakt_op', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('gewijzigd_op', sa.DateTime(timezone=True), nullable=True),
    sa.Column('verwijderd_op', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('model_id', 'versie', name='uq_model_versie')
    )
    with op.batch_alter_table('modellen', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_modellen_model_id'), ['model_id'], unique=False)

    op.create_table('testcases',
    sa.Column('tc_id', sa.String(length=60), nullable=False),
    sa.Column('model_id', sa.String(length=40), nullable=False),
    sa.Column('rechtsgebied', sa.Text(), nullable=False),
    sa.Column('onderwerp', sa.Text(), nullable=False),
    sa.Column('testtype', sa.String(length=20), nullable=False),
    sa.Column('prompt', sa.Text(), nullable=False),
    sa.Column('context', sa.Text(), nullable=False),
    sa.Column('kernpunten', sa.Text(), nullable=False),
    sa.Column('verwacht_gedrag', sa.Text(), nullable=False),
    sa.Column('verwachte_bronnen', sa.Text(), nullable=False),
    sa.Column('regressieset', sa.String(length=1), nullable=False),
    sa.Column('herkomst', sa.Text(), nullable=False),
    sa.Column('opgesteld_door', sa.Text(), nullable=False),
    sa.Column('goedgekeurd_door', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('peildatum', sa.Date(), nullable=True),
    sa.Column('opmerkingen', sa.Text(), nullable=False),
    sa.Column('import_bron', sa.Text(), nullable=False),
    sa.Column('oorspronkelijk_id', sa.String(length=60), nullable=False),
    sa.Column('aangemaakt_op', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('gewijzigd_op', sa.DateTime(timezone=True), nullable=True),
    sa.Column('verwijderd_op', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('tc_id')
    )
    with op.batch_alter_table('testcases', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_testcases_model_id'), ['model_id'], unique=False)

    op.create_table('testruns',
    sa.Column('run_id', sa.String(length=40), nullable=False),
    sa.Column('tc_id', sa.String(length=60), nullable=False),
    sa.Column('model_id', sa.String(length=40), nullable=False),
    sa.Column('modelversie', sa.String(length=40), nullable=False),
    sa.Column('datum', sa.Date(), nullable=False),
    sa.Column('uitgevoerd_door', sa.Text(), nullable=False),
    sa.Column('uitvoerwijze', sa.String(length=20), nullable=False),
    sa.Column('herhaling', sa.Integer(), nullable=False),
    sa.Column('antwoord', sa.Text(), nullable=False),
    sa.Column('api_model', sa.Text(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=True),
    sa.Column('response', sa.JSON(), nullable=True),
    sa.Column('start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duur_s', sa.Float(), nullable=True),
    sa.Column('import_bron', sa.Text(), nullable=False),
    sa.Column('oorspronkelijk_id', sa.String(length=40), nullable=False),
    sa.Column('aangemaakt_op', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('gewijzigd_op', sa.DateTime(timezone=True), nullable=True),
    sa.Column('verwijderd_op', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['tc_id'], ['testcases.tc_id'], ),
    sa.PrimaryKeyConstraint('run_id')
    )
    with op.batch_alter_table('testruns', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_testruns_model_id'), ['model_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_testruns_modelversie'), ['modelversie'], unique=False)
        batch_op.create_index(batch_op.f('ix_testruns_tc_id'), ['tc_id'], unique=False)

    op.create_table('beoordelingen',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.String(length=40), nullable=False),
    sa.Column('type', sa.String(length=10), nullable=False),
    sa.Column('beoordelaar', sa.Text(), nullable=False),
    sa.Column('juistheid', sa.String(length=6), nullable=True),
    sa.Column('volledigheid', sa.String(length=6), nullable=True),
    sa.Column('bronnen', sa.String(length=6), nullable=True),
    sa.Column('bruikbaarheid', sa.String(length=6), nullable=True),
    sa.Column('gedrag', sa.String(length=6), nullable=True),
    sa.Column('ko', sa.String(length=1), nullable=False),
    sa.Column('ko_code', sa.String(length=10), nullable=False),
    sa.Column('ko_citaat', sa.Text(), nullable=False),
    sa.Column('toelichting', sa.Text(), nullable=False),
    sa.Column('te_verifieren', sa.JSON(), nullable=True),
    sa.Column('kernpunten', sa.JSON(), nullable=True),
    sa.Column('eindoordeel', sa.String(length=12), nullable=False),
    sa.Column('waarschuwingen', sa.JSON(), nullable=True),
    sa.Column('details', sa.JSON(), nullable=True),
    sa.Column('aangemaakt_op', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('gewijzigd_op', sa.DateTime(timezone=True), nullable=True),
    sa.Column('verwijderd_op', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['testruns.run_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('beoordelingen', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_beoordelingen_run_id'), ['run_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('beoordelingen', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_beoordelingen_run_id'))

    op.drop_table('beoordelingen')
    with op.batch_alter_table('testruns', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_testruns_tc_id'))
        batch_op.drop_index(batch_op.f('ix_testruns_modelversie'))
        batch_op.drop_index(batch_op.f('ix_testruns_model_id'))

    op.drop_table('testruns')
    with op.batch_alter_table('testcases', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_testcases_model_id'))

    op.drop_table('testcases')
    with op.batch_alter_table('modellen', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_modellen_model_id'))

    op.drop_table('modellen')
    op.drop_table('modelkoppeling')
    op.drop_table('instellingen')
