"""Contract: all processes must first run 0020-capable code before this release.

No old user columns, duplicate source of truth or legacy feature-role remains.
"""

import sqlalchemy as sa

from alembic import op

revision = "0021_interest_contract"
down_revision = "0020_user_interests"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER socrat_interests_bridge ON user_interests")
        op.execute("DROP TRIGGER socrat_users_bridge ON users")
        op.execute("DROP FUNCTION socrat_interest_legacy_bridge()")
    op.execute("UPDATE users SET role='user' WHERE role='vpn'")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("uses_vpn")
        batch.drop_column("alerts_enabled")


def downgrade():
    # Reconstitute the temporary legacy projection before rolling back code.
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "uses_vpn", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch.add_column(
            sa.Column(
                "alerts_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    op.execute(
        "UPDATE users SET uses_vpn=EXISTS (SELECT 1 FROM user_interests WHERE user_id=users.id AND topic='vpn' AND enabled)"
    )
    op.execute(
        "UPDATE users SET alerts_enabled=(alerts_approved AND EXISTS (SELECT 1 FROM user_interests WHERE user_id=users.id AND topic='alerts' AND enabled))"
    )
    if op.get_bind().dialect.name == "postgresql":
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
