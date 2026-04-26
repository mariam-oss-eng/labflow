"""v0.6 v0.7 collaboration realtime

Revision ID: c1f2a44b9d10
Revises: a9a8b26bb922
Create Date: 2026-04-26 11:05:00.000000+00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c1f2a44b9d10"
down_revision = "a9a8b26bb922"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- v0.6 collaboration -------------------------------------------------
    op.create_table(
        "comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("actor_key_id", sa.Integer(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("edited_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["comments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_key_id"], ["api_keys.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_comments_team_entity", "comments",
                    ["team_id", "entity_type", "entity_id"])
    op.create_index("ix_comments_parent", "comments", ["parent_id"])

    op.create_table(
        "reactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("emoji", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("actor_key_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_key_id"], ["api_keys.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "entity_type", "entity_id", "emoji",
                            "actor", name="uq_reactions_unique"),
    )
    op.create_index("ix_reactions_entity", "reactions",
                    ["team_id", "entity_type", "entity_id"])

    op.create_table(
        "saved_searches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("alpha", sa.Float(), nullable=True),
        sa.Column("filters", sa.Text(), nullable=True),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "slug", name="uq_saved_search_team_slug"),
    )
    op.create_index("ix_saved_search_team", "saved_searches", ["team_id"])

    op.create_table(
        "notification_prefs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("api_key_id", sa.Integer(), nullable=False),
        sa.Column("digest_cadence", sa.String(length=16), nullable=False,
                  server_default="weekly"),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("muted_events", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_key_id", name="uq_notif_pref_key"),
    )

    # ---- v0.7 worker locks --------------------------------------------------
    op.create_table(
        "worker_locks",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("acquired_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("worker_locks")
    op.drop_table("notification_prefs")
    op.drop_index("ix_saved_search_team", table_name="saved_searches")
    op.drop_table("saved_searches")
    op.drop_index("ix_reactions_entity", table_name="reactions")
    op.drop_table("reactions")
    op.drop_index("ix_comments_parent", table_name="comments")
    op.drop_index("ix_comments_team_entity", table_name="comments")
    op.drop_table("comments")
