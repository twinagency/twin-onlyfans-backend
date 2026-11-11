"""Approved commercial terms for a catalogue item (Step 3, 0135).

CatalogItem stays the only catalogue owner. One immutable revision row freezes what the agency approved for it: product
kind, the manual standard price, an optional approved discount price, the exact paid/preview members and the price
source. Rows are evidence: only `status` may close (approved -> superseded|withdrawn); nothing else changes or is deleted.
"""
from sqlalchemy import (BigInteger, CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer,
                        String, Text, UniqueConstraint, text)
from .models import Base

REVISION = '0135_commercial_catalogue_terms'
INFO = {'onlyfans_since': REVISION}

KINDS = ('single_photo', 'single_video', 'bundle_photo', 'bundle_video', 'bundle_mixed')


class OfCatalogueTerms(Base):
    __tablename__ = 'of_catalogue_terms'
    __table_args__ = (
        UniqueConstraint('account_id', 'command_key', name='uq_of_catalogue_terms_command', info=INFO),
        CheckConstraint("status IN ('approved','superseded','withdrawn')", name='status', info=INFO),
        CheckConstraint("product_kind IN ('single_photo','single_video','bundle_photo','bundle_video','bundle_mixed')", name='kind', info=INFO),
        CheckConstraint('revision >= 1 AND standard_price_cents BETWEEN 300 AND 20000', name='standard', info=INFO),
        CheckConstraint('discount_price_cents IS NULL OR (discount_price_cents >= 300 AND discount_price_cents < standard_price_cents)',
                        name='discount', info=INFO),
        CheckConstraint("price_source IN ('agency_category','agency_bundle_total')", name='source', info=INFO),
        CheckConstraint("(product_kind IN ('single_photo','single_video') AND price_source = 'agency_category' AND category_folder_id IS NOT NULL"
                        " AND category_price_revision IS NOT NULL AND category_price_revision > 0)"
                        " OR (product_kind IN ('bundle_photo','bundle_video','bundle_mixed') AND price_source = 'agency_bundle_total')", name='source_kind', info=INFO),
        CheckConstraint('paid_photo_count >= 0 AND paid_video_count >= 0 AND preview_photo_count >= 0 AND preview_video_count >= 0'
                        ' AND media_count = paid_photo_count + paid_video_count + preview_photo_count + preview_video_count',
                        name='counts', info=INFO),
        CheckConstraint("(product_kind = 'single_photo' AND paid_photo_count = 1 AND paid_video_count = 0)"
                        " OR (product_kind = 'single_video' AND paid_photo_count = 0 AND paid_video_count = 1)"
                        " OR (product_kind = 'bundle_photo' AND paid_photo_count >= 2 AND paid_video_count = 0)"
                        " OR (product_kind = 'bundle_video' AND paid_photo_count = 0 AND paid_video_count >= 2)"
                        " OR (product_kind = 'bundle_mixed' AND paid_photo_count >= 1 AND paid_video_count >= 1"
                        " AND paid_photo_count + paid_video_count >= 2)", name='shape', info=INFO),
        Index('uq_of_catalogue_terms_live', 'account_id', 'item_id', unique=True,
              postgresql_where=text("status = 'approved'"), info=INFO),
        Index('ix_of_catalogue_terms_kind', 'account_id', 'product_kind', 'item_id',
              postgresql_where=text("status = 'approved'"), info=INFO),
        {'info': INFO},
    )
    account_id = Column(String, ForeignKey('of_runtime.account_id', ondelete='RESTRICT'), primary_key=True)
    item_id = Column(Integer, ForeignKey('catalog_items.id', ondelete='RESTRICT'), primary_key=True)
    revision = Column(Integer, primary_key=True)
    status = Column(String(12), nullable=False)
    product_kind = Column(String(16), nullable=False)
    standard_price_cents = Column(Integer, nullable=False)
    discount_price_cents = Column(Integer)
    price_source = Column(String(24), nullable=False)
    category_folder_id = Column(BigInteger)
    category_price_revision = Column(Integer)
    media_count = Column(Integer, nullable=False)
    paid_photo_count = Column(Integer, nullable=False)
    paid_video_count = Column(Integer, nullable=False)
    preview_photo_count = Column(Integer, nullable=False)
    preview_video_count = Column(Integer, nullable=False)
    approved_by = Column(String(128), nullable=False)
    approved_at = Column(DateTime, nullable=False)
    binding_id = Column(String, ForeignKey('of_creator_bindings.id', ondelete='RESTRICT'), nullable=False)
    command_key = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)
    snapshot_json = Column(Text, nullable=False)
    snapshot_sha256 = Column(String(64), nullable=False)


class OfCatalogueTermsMember(Base):
    __tablename__ = 'of_catalogue_terms_member'
    __table_args__ = (
        ForeignKeyConstraint(['account_id', 'item_id', 'revision'],
            ['of_catalogue_terms.account_id', 'of_catalogue_terms.item_id', 'of_catalogue_terms.revision'], ondelete='RESTRICT'),
        CheckConstraint("role IN ('paid','preview')", name='role', info=INFO),
        CheckConstraint("kind IN ('photo','video')", name='kind', info=INFO),
        Index('ix_terms_member_media', 'account_id', 'media_id', info=INFO),
        {'info': INFO},
    )
    account_id = Column(String, primary_key=True)
    item_id = Column(Integer, primary_key=True)
    revision = Column(Integer, primary_key=True)
    media_id = Column(BigInteger, primary_key=True)
    role = Column(String(8), nullable=False)
    kind = Column(String(8), nullable=False)
