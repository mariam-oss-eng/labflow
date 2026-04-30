"""v0.8 workflows sprints acls share-links scopes

Revision ID: d2a3b55ce0a1
Revises: c1f2a44b9d10
Create Date: 2026-04-30 19:00:00.000000+00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d2a3b55ce0a1"
down_revision = "c1f2a44b9d10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- workflows ----------------------------------------------------------
    op.create_table(
        "workflows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "name", name="uq_workflow_team_name"),
    )
    op.create_index("ix_workflow_team", "workflows", ["team_id"])

    # ---- sprints ------------------------------------------------------------
    op.create_table(
        "sprints",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "slug", name="uq_sprint_team_slug"),
    )
    op.create_index("ix_sprint_team_active", "sprints", ["team_id", "active"])

    # ---- resource ACLs ------------------------------------------------------
    op.create_table(
        "resource_acls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("api_key_id", sa.Integer(), nullable=True),
        sa.Column("permission", sa.String(length=8), nullable=False, server_default="read"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "entity_type", "entity_id", "api_key_id",
                            name="uq_acl_unique"),
    )
    op.create_index("ix_acl_entity", "resource_acls",
                    ["team_id", "entity_type", "entity_id"])

    # ---- share links --------------------------------------------------------
    op.create_table(
        "share_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("passcode_hash", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_by_key_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_key_id"], ["api_keys.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_share_token_hash", "share_links", ["token_hash"], unique=True)
    op.create_index("ix_share_team_entity", "share_links",
                    ["team_id", "entity_type", "entity_id"])

    # ---- ApiKey.scopes column ----------------------------------------------
    with op.batch_alter_table("api_keys") as b:
        b.add_column(sa.Column("scopes", sa.String(length=255), nullable=True))

    # ---- Task.workflow_id / sprint_id / state / sla_breach_at --------------
    with op.batch_alter_table("tasks") as b:
        b.add_column(sa.Column("workflow_id", sa.Integer(), nullable=True))
        b.add_column(sa.Column("sprint_id", sa.Integer(), nullable=True))
        b.add_column(sa.Column("state", sa.String(length=32), nullable=True))
        b.add_column(sa.Column("sla_breach_at", sa.DateTime(), nullable=True))
        b.create_foreign_key("fk_tasks_workflow", "workflows",
                             ["workflow_id"], ["id"], ondelete="SET NULL")
        b.create_foreign_key("fk_tasks_sprint", "sprints",
                             ["sprint_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("tasks") as b:
        b.drop_constraint("fk_tasks_sprint", type_="foreignkey")
        b.drop_constraint("fk_tasks_workflow", type_="foreignkey")
        b.drop_column("sla_breach_at")
        b.drop_column("state")
        b.drop_column("sprint_id")
        b.drop_column("workflow_id")
    with op.batch_alter_table("api_keys") as b:
        b.drop_column("scopes")
    op.drop_index("ix_share_team_entity", table_name="share_links")
    op.drop_index("ix_share_token_hash", table_name="share_links")
    op.drop_table("share_links")
    op.drop_index("ix_acl_entity", table_name="resource_acls")
    op.drop_table("resource_acls")
    op.drop_index("ix_sprint_team_active", table_name="sprints")
    op.drop_table("sprints")
    op.drop_index("ix_workflow_team", table_name="workflows")
    op.drop_table("workflows")
