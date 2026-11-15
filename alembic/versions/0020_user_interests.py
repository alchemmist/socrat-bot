"""Expand to independent interests; bridge legacy processes during one rollout."""

import sqlalchemy as sa

from alembic import op

revision = "0020_user_interests"
down_revision = "0019_graph_source"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_interests",
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("topic", sa.String(64), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
    )
    # Old delivery treated enabled as authoritative and repaired approval on use.
    op.execute("UPDATE users SET alerts_approved=true WHERE alerts_enabled")
    # Exactly the old active opt-ins, not role/panel-link/approval history.
    op.execute(
        "INSERT INTO user_interests (user_id,topic,enabled) SELECT id,'vpn',uses_vpn FROM users"
    )
    op.execute(
        "INSERT INTO user_interests (user_id,topic,enabled) SELECT id,'alerts',alerts_enabled FROM users"
    )
    op.execute("UPDATE users SET role='user' WHERE role='vpn'")
    if op.get_bind().dialect.name == "postgresql":
        # Temporary, transaction-local dual-write compatibility with the old pods.
        # Never leave the old delivery flag true for an unapproved selection.
        op.execute("""
        CREATE OR REPLACE FUNCTION socrat_interest_legacy_bridge() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF pg_trigger_depth() > 1 THEN RETURN NEW; END IF;
          IF TG_TABLE_NAME = 'user_interests' THEN
            IF NEW.topic = 'vpn' THEN
              UPDATE users SET uses_vpn=NEW.enabled WHERE id=NEW.user_id;
            ELSIF NEW.topic = 'alerts' THEN
              UPDATE users SET alerts_enabled=(NEW.enabled AND alerts_approved) WHERE id=NEW.user_id;
            END IF;
          ELSE
            IF (TG_OP = 'INSERT' AND NEW.uses_vpn) OR (TG_OP = 'UPDATE' AND NEW.uses_vpn IS DISTINCT FROM OLD.uses_vpn) THEN
              INSERT INTO user_interests VALUES (NEW.id,'vpn',NEW.uses_vpn)
              ON CONFLICT (user_id,topic) DO UPDATE SET enabled=EXCLUDED.enabled;
            END IF;
            IF (TG_OP = 'INSERT' AND NEW.alerts_enabled) OR (TG_OP = 'UPDATE' AND NEW.alerts_enabled IS DISTINCT FROM OLD.alerts_enabled) THEN
              INSERT INTO user_interests VALUES (NEW.id,'alerts',NEW.alerts_enabled)
              ON CONFLICT (user_id,topic) DO UPDATE SET enabled=EXCLUDED.enabled;
            ELSIF TG_OP = 'UPDATE' AND NEW.alerts_approved IS DISTINCT FROM OLD.alerts_approved THEN
              UPDATE users SET alerts_enabled=(NEW.alerts_approved AND EXISTS
                (SELECT 1 FROM user_interests WHERE user_id=NEW.id AND topic='alerts' AND enabled)) WHERE id=NEW.id;
            END IF;
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER socrat_interests_bridge AFTER INSERT OR UPDATE ON user_interests
          FOR EACH ROW EXECUTE FUNCTION socrat_interest_legacy_bridge();
        CREATE TRIGGER socrat_users_bridge AFTER INSERT OR UPDATE OF uses_vpn,alerts_enabled,alerts_approved ON users
          FOR EACH ROW EXECUTE FUNCTION socrat_interest_legacy_bridge();
        """)
    # Defaults allow new code to insert users while old NOT NULL columns coexist.
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "uses_vpn", existing_type=sa.Boolean(), server_default=sa.false()
        )
        batch.alter_column(
            "alerts_enabled", existing_type=sa.Boolean(), server_default=sa.false()
        )


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER socrat_interests_bridge ON user_interests")
        op.execute("DROP TRIGGER socrat_users_bridge ON users")
        op.execute("DROP FUNCTION socrat_interest_legacy_bridge()")
    op.execute(
        "UPDATE users SET uses_vpn=EXISTS (SELECT 1 FROM user_interests WHERE user_id=users.id AND topic='vpn' AND enabled)"
    )
    op.execute(
        "UPDATE users SET alerts_enabled=(alerts_approved AND EXISTS (SELECT 1 FROM user_interests WHERE user_id=users.id AND topic='alerts' AND enabled))"
    )
    op.execute(
        "UPDATE users SET role='vpn' WHERE role='user' AND (uses_vpn OR panel_client_id IS NOT NULL)"
    )
    op.drop_table("user_interests")
    with op.batch_alter_table("users") as batch:
        batch.alter_column("uses_vpn", existing_type=sa.Boolean(), server_default=None)
        batch.alter_column(
            "alerts_enabled", existing_type=sa.Boolean(), server_default=None
        )
