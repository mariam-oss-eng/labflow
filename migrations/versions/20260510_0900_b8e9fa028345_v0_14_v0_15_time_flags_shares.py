"""v0.14 + v0.15 — time tracking, feature flags, smart-list subs, public shares.

Revision ID: b8e9fa028345
Revises: a7d8e91f1234
Create Date: 2026-05-10 09:00:00.000000+00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "b8e9fa028345"
down_revision = "a7d8e91f1234"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # v0.14: tasks.effort_hours --------------------------------------
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("effort_hours", sa.Float(), nullable=True))

    # v0.14: time_entries --------------------------------------------
    op.create_table(
        "time_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False,
                  server_default="manual"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["owners.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_time_entries_task", "time_entries", ["task_id"])
    op.create_index("ix_time_entries_team_owner", "time_entries",
                    ["team_id", "owner_id"])
    op.create_index("ix_time_entries_open", "time_entries",
                    ["team_id", "ended_at"])

    # v0.14: feature_flags -------------------------------------------
    op.create_table(
        "feature_flags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "key", name="uq_feature_flag_key"),
    )

    # v0.14: smart_list_subscriptions --------------------------------
    op.create_table(
        "smart_list_subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("smart_list_id", sa.Integer(), nullable=False),
        sa.Column("webhook_url", sa.String(length=500), nullable=False),
        sa.Column("secret", sa.String(length=120), nullable=True),
        sa.Column("last_digest", sa.String(length=64), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["smart_list_id"], ["smart_lists.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("smart_list_id", "webhook_url",
                            name="uq_smart_list_sub_url"),
    )
    op.create_index("ix_smart_list_sub_team",
                    "smart_list_subscriptions", ["team_id"])

    # v0.15: public_shares -------------------------------------------
    op.create_table(
        "public_shares",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False,
                  server_default="system"),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_public_share_token", "public_shares",
                    ["token_hash"], unique=True)
    op.create_index("ix_public_share_team_entity", "public_shares",
                    ["team_id", "entity_type", "entity_id"])


def downgrade() -> None:
    op.drop_index("ix_public_share_team_entity", table_name="public_shares")
    op.drop_index("ix_public_share_token", table_name="public_shares")
    op.drop_table("public_shares")

    op.drop_index("ix_smart_list_sub_team",
                  table_name="smart_list_subscriptions")
    op.drop_table("smart_list_subscriptions")

    op.drop_table("feature_flags")

    op.drop_index("ix_time_entries_open", table_name="time_entries")
    op.drop_index("ix_time_entries_team_owner", table_name="time_entries")
    op.drop_index("ix_time_entries_task", table_name="time_entries")
    op.drop_table("time_entries")

    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("effort_hours")
