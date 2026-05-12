"""v0.16 + v0.17 — custom fields, scheduled reports, webhook DLQ, key rotation.

Revision ID: c1d2e3f456ab
Revises: b8e9fa028345
Create Date: 2026-05-12 09:00:00.000000+00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c1d2e3f456ab"
down_revision = "b8e9fa028345"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # v0.16: custom_field_defs ----------------------------------------
    op.create_table(
        "custom_field_defs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("options_json", sa.Text(), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "entity_type", "key",
                            name="uq_cfdef_team_entity_key"),
    )
    op.create_index("ix_cfdef_team_entity", "custom_field_defs",
                    ["team_id", "entity_type"])

    # v0.16: custom_field_values --------------------------------------
    op.create_table(
        "custom_field_values",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("def_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["def_id"], ["custom_field_defs.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("def_id", "entity_id", name="uq_cfval_def_entity"),
    )
    op.create_index("ix_cfval_team_entity", "custom_field_values",
                    ["team_id", "entity_type", "entity_id"])

    # v0.16: scheduled_reports ----------------------------------------
    op.create_table(
        "scheduled_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("cadence", sa.String(length=16), nullable=False),
        sa.Column("webhook_url", sa.String(length=500), nullable=False),
        sa.Column("secret", sa.String(length=120), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "name", name="uq_schedrep_team_name"),
    )
    op.create_index("ix_schedrep_team_due", "scheduled_reports",
                    ["team_id", "next_run_at"])

    # v0.16: scheduled_report_runs ------------------------------------
    op.create_table(
        "scheduled_report_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("matched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["report_id"], ["scheduled_reports.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_schedrep_run_report", "scheduled_report_runs",
                    ["report_id", "created_at"])

    # v0.17: api_keys.rotated_from_id + rotation_grace_until ----------
    with op.batch_alter_table("api_keys") as batch:
        batch.add_column(sa.Column("rotated_from_id", sa.Integer(),
                                   nullable=True))
        batch.add_column(sa.Column("rotation_grace_until", sa.DateTime(),
                                   nullable=True))
        # SQLite-friendly named FK in batch mode
        batch.create_foreign_key(
            "fk_api_keys_rotated_from", "api_keys",
            ["rotated_from_id"], ["id"], ondelete="SET NULL",
        )

    # v0.17: webhook_deliveries.dead_lettered_at ----------------------
    with op.batch_alter_table("webhook_deliveries") as batch:
        batch.add_column(sa.Column("dead_lettered_at", sa.DateTime(),
                                   nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("webhook_deliveries") as batch:
        batch.drop_column("dead_lettered_at")

    with op.batch_alter_table("api_keys") as batch:
        batch.drop_constraint("fk_api_keys_rotated_from", type_="foreignkey")
        batch.drop_column("rotation_grace_until")
        batch.drop_column("rotated_from_id")

    op.drop_index("ix_schedrep_run_report",
                  table_name="scheduled_report_runs")
    op.drop_table("scheduled_report_runs")

    op.drop_index("ix_schedrep_team_due", table_name="scheduled_reports")
    op.drop_table("scheduled_reports")

    op.drop_index("ix_cfval_team_entity", table_name="custom_field_values")
    op.drop_table("custom_field_values")

    op.drop_index("ix_cfdef_team_entity", table_name="custom_field_defs")
    op.drop_table("custom_field_defs")
