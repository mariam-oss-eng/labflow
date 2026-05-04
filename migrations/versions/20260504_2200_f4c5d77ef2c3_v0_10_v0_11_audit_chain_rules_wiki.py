"""v0.10 + v0.11 — audit hash chain, automation rules, dashboards, wiki, watchers, entity_links.

Revision ID: f4c5d77ef2c3
Revises: e3b4c66de1b2
Create Date: 2026-05-04 22:00:00.000000+00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f4c5d77ef2c3"
down_revision = "e3b4c66de1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # v0.10: audit hash chain columns ----------------------------------
    with op.batch_alter_table("audit_events") as batch:
        batch.add_column(sa.Column("prev_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("entry_hash", sa.String(length=64), nullable=True))

    # v0.10: automation rules -----------------------------------------
    op.create_table(
        "automation_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("trigger_event", sa.String(length=64), nullable=False),
        sa.Column("condition_json", sa.Text(), nullable=True),
        sa.Column("actions_json", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("fires", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("last_fired_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "name", name="uq_rule_team_name"),
    )
    op.create_index("ix_rule_team_event", "automation_rules",
                    ["team_id", "trigger_event"])

    # v0.10: dashboards ------------------------------------------------
    op.create_table(
        "dashboards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("owner_key_id", sa.Integer(), nullable=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("layout_json", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_key_id"], ["api_keys.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "owner_key_id", "slug",
                            name="uq_dashboard_team_owner_slug"),
    )
    op.create_index("ix_dashboard_team", "dashboards", ["team_id"])

    # v0.11: wiki pages + revisions -----------------------------------
    op.create_table(
        "wiki_pages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.String(length=512), nullable=True),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("current_revision_id", sa.Integer(), nullable=True),
        sa.Column("deleted", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "slug", name="uq_wiki_team_slug"),
    )
    op.create_index("ix_wiki_team", "wiki_pages", ["team_id"])

    op.create_table(
        "wiki_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("page_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author", sa.String(length=128), nullable=False,
                  server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["page_id"], ["wiki_pages.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_wiki_rev_page", "wiki_revisions",
                    ["page_id", "created_at"])

    # v0.11: entity links + watchers ----------------------------------
    op.create_table(
        "entity_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "source_type", "source_id",
                            "target_type", "target_id", "kind",
                            name="uq_entity_link"),
    )
    op.create_index("ix_link_source", "entity_links",
                    ["team_id", "source_type", "source_id"])
    op.create_index("ix_link_target", "entity_links",
                    ["team_id", "target_type", "target_id"])

    op.create_table(
        "watchers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("api_key_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("delivery", sa.String(length=16), nullable=False,
                  server_default="feed"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "api_key_id", "entity_type", "entity_id",
                            name="uq_watcher"),
    )
    op.create_index("ix_watcher_entity", "watchers",
                    ["team_id", "entity_type", "entity_id"])


def downgrade() -> None:
    op.drop_index("ix_watcher_entity", table_name="watchers")
    op.drop_table("watchers")
    op.drop_index("ix_link_target", table_name="entity_links")
    op.drop_index("ix_link_source", table_name="entity_links")
    op.drop_table("entity_links")
    op.drop_index("ix_wiki_rev_page", table_name="wiki_revisions")
    op.drop_table("wiki_revisions")
    op.drop_index("ix_wiki_team", table_name="wiki_pages")
    op.drop_table("wiki_pages")
    op.drop_index("ix_dashboard_team", table_name="dashboards")
    op.drop_table("dashboards")
    op.drop_index("ix_rule_team_event", table_name="automation_rules")
    op.drop_table("automation_rules")
    with op.batch_alter_table("audit_events") as batch:
        batch.drop_column("entry_hash")
        batch.drop_column("prev_hash")
