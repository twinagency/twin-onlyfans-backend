"""Scoped encrypted provider credentials and non-secret lifecycle evidence."""
from sqlalchemy import BigInteger,Boolean,CheckConstraint,Column,DateTime,ForeignKey,Integer,String,Text,false
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
