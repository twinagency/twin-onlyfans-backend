"""Scoped encrypted provider credentials and non-secret lifecycle evidence."""
from sqlalchemy import BigInteger,Boolean,CheckConstraint,Column,DateTime,ForeignKey,Integer,String,Text,false,UniqueConstraint,Index
from .models import Base
INFO={"onlyfans_since":"0081_native_connection"}

class OfProviderConnection(Base):
    __tablename__="of_provider_connection"
    __table_args__=(CheckConstraint("status IN ('pending','verified','failed','expired','paused')",name="status"),{"info":INFO})
    account_id=Column(String,ForeignKey("of_runtime.account_id",ondelete="RESTRICT"),primary_key=True)
    binding_id=Column(String,ForeignKey("of_creator_bindings.id",ondelete="RESTRICT"),nullable=False)
    owner_id=Column(BigInteger,nullable=False)
    revision=Column(Integer,nullable=False)
    sealed_config=Column(Text,nullable=False)
    expires_at=Column(DateTime,nullable=False)
    status=Column(String(16),nullable=False)
    verified_at=Column(DateTime)
    last_error=Column(String(64))
    updated_at=Column(DateTime,nullable=False)
    automate_new_conversations=Column(Boolean,nullable=False,server_default=false())
    automation_starts_at=Column(DateTime)

class OfConnectionChange(Base):
    __tablename__="of_connection_change"
    __table_args__=({"info":INFO},)
    account_id=Column(String,ForeignKey("of_runtime.account_id",ondelete="RESTRICT"),primary_key=True)
    command_key=Column(String(64),primary_key=True)
    revision=Column(Integer,nullable=False)
    action=Column(String(32),nullable=False)
    request_hash=Column(String(64),nullable=False)
    actor=Column(String(128),nullable=False)
    created_at=Column(DateTime,nullable=False)


class OfConnectionEnrollment(Base):
    __tablename__="of_connection_enrollment"
    __table_args__=(
        CheckConstraint("status IN ('pending','checking','accepted','failed','expired')",name="status"),
        UniqueConstraint("tenant_id","command_key",name="uq_of_connection_enrollment_tenant_key"),
        Index("ix_of_connection_enrollment_pending","tenant_id","status","created_at"),
        Index("ix_of_connection_enrollment_creator","tenant_id","creator_id","created_at"),
        {"info":{"onlyfans_since":"0087_creator_enrollment"}})
    id=Column(String,primary_key=True)
    tenant_id=Column(String,nullable=False)
    creator_id=Column(String,nullable=False)
    creator_name=Column(Text,nullable=False)
    handle=Column(Text,nullable=False)
    owner_id=Column(BigInteger,nullable=False)
    actor=Column(String(128),nullable=False)
    command_key=Column(String(64),nullable=False)
    request_hash=Column(String(64),nullable=False)
    status=Column(String(16),nullable=False)
    sealed_config=Column(Text)
    expires_at=Column(DateTime,nullable=False)
    lease_until=Column(DateTime)
    generation=Column(Integer,nullable=False)
    expected_revision=Column(Integer)
    account_id=Column(String)
    last_error=Column(String(64))
    created_at=Column(DateTime,nullable=False)
    updated_at=Column(DateTime,nullable=False)

class OfProxyAssignment(Base):
    __tablename__="of_proxy_assignment"
    __table_args__=(
        UniqueConstraint("pool_id",name="uq_of_proxy_assignment_pool"),
        UniqueConstraint("proxy_fingerprint",name="uq_of_proxy_assignment_network"),
        {"info":{"onlyfans_since":"0092_proxy_assignments"}})
    tenant_id=Column(String,primary_key=True)
    creator_id=Column(String,primary_key=True)
    pool_id=Column(String(64),nullable=False)
    proxy_fingerprint=Column(String(64),nullable=False)
    sealed_proxy=Column(Text,nullable=False)
    created_at=Column(DateTime,nullable=False)
