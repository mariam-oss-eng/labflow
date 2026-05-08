"""v0.12 + v0.13 — recurring tasks, API key quotas, digest hour, invites, smart lists.

Revision ID: a7d8e91f1234
Revises: f4c5d77ef2c3
Create Date: 2026-05-08 14:30:00.000000+00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a7d8e91f1234"
down_revision = "f4c5d77ef2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # v0.12: tasks.priority -------------------------------------------
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("priority", sa.String(length=16), nullable=True))

    # v0.12: recurring tasks ------------------------------------------
    op.create_table(
        "recurring_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("cadence", sa.String(length=16), nullable=False),
        sa.Column("interval", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("day_of_week", sa.Integer(), nullable=True),
        sa.Column("day_of_month", sa.Integer(), nullable=True),
        sa.Column("template_title", sa.String(length=255), nullable=False),
        sa.Column("template_owner_id", sa.Integer(), nullable=True),
        sa.Column("template_priority", sa.String(length=16), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["template_owner_id"], ["owners.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "slug", name="uq_recurring_slug"),
    )
    op.create_index("ix_recurring_team_active_due", "recurring_tasks",
                    ["team_id", "active", "next_run_at"])

    # v0.12: api key quotas + usage -----------------------------------
    op.create_table(
        "api_key_quotas",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("api_key_id", sa.Integer(), nullable=False),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_key_id", name="uq_quota_key"),
    )
    op.create_table(
        "api_key_usage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("api_key_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_key_id", "day", name="uq_usage_key_day"),
    )
    op.create_index("ix_usage_team_day", "api_key_usage", ["team_id", "day"])

    # v0.12: notification_prefs.digest_hour_utc -----------------------
    with op.batch_alter_table("notification_prefs") as batch:
        batch.add_column(sa.Column("digest_hour_utc", sa.Integer(), nullable=True))

    # v0.13: invites --------------------------------------------------
    op.create_table(
        "invites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="viewer"),
        sa.Column("scopes", sa.String(length=255), nullable=True),
        sa.Column("acl_entries_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("created_by_key_id", sa.Integer(), nullable=True),
        sa.Column("accepted_key_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_key_id"], ["api_keys.id"],
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["accepted_key_id"], ["api_keys.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_invite_token_hash", "invites", ["token_hash"], unique=True)
    op.create_index("ix_invite_team_status", "invites", ["team_id", "status"])

    # v0.13: smart lists ----------------------------------------------
    op.create_table(
        "smart_lists",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("filter_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "slug", name="uq_smart_list_slug"),
    )


def downgrade() -> None:
    op.drop_table("smart_lists")
    op.drop_index("ix_invite_team_status", table_name="invites")
    op.drop_index("ix_invite_token_hash", table_name="invites")
    op.drop_table("invites")
    with op.batch_alter_table("notification_prefs") as batch:
        batch.drop_column("digest_hour_utc")
    op.drop_index("ix_usage_team_day", table_name="api_key_usage")
    op.drop_table("api_key_usage")
    op.drop_table("api_key_quotas")
    op.drop_index("ix_recurring_team_active_due", table_name="recurring_tasks")
    op.drop_table("recurring_tasks")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("priority")
