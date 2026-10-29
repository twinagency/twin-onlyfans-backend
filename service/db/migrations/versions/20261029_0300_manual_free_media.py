"""Permit explicitly selected free media in manual delivery intents."""
from alembic import op

revision = "0100_manual_free_media"
down_revision = "0099_fan_identity_sync"
branch_labels = None
depends_on = None

def upgrade():
    # Existing text and paid intent shapes are unchanged. Free media is manual-only.
    op.execute("""ALTER TABLE of_text_intent DROP CONSTRAINT ck_of_text_intent_media_shape""")
    op.execute("""ALTER TABLE of_text_intent ADD CONSTRAINT ck_of_text_intent_media_shape CHECK (
        (catalog_item_id IS NULL AND media_json = '[]' AND preview_json = '[]'
         AND price_cents = 0 AND catalog_snapshot_json IS NULL AND offer_id IS NULL)
        OR (catalog_item_id IS NOT NULL AND media_json <> '[]' AND catalog_snapshot_json IS NOT NULL
            AND price_cents >= 300 AND price_cents <= 20000)
        OR (origin = 'manual' AND catalog_item_id IS NOT NULL AND media_json <> '[]'
            AND preview_json = '[]' AND catalog_snapshot_json IS NOT NULL AND price_cents = 0
            AND offer_id IS NULL AND catalog_snapshot_json::jsonb @> '{"manual_vault": true}'::jsonb)
    )""")

def downgrade():
    raise RuntimeError("Preserve free-media delivery receipts; use a compatible forward release.")
