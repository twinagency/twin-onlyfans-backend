"""One durable Moxy owner; disabling never hands the account to another engine."""
from sqlalchemy import (Column, String, Text, Integer, BigInteger, Boolean, DateTime,
    ForeignKey, ForeignKeyConstraint, CheckConstraint, UniqueConstraint, Index, text)
from .models import Base

REVISION='0111_moxy_owner'
INFO={'onlyfans_since':REVISION}


class MoxyAccount(Base):
    __tablename__='of_moxy_account'
    __table_args__=(CheckConstraint("mode IN ('off','preview','on')",name='mode'),{'info':INFO})
    account_id=Column(String,ForeignKey('of_runtime.account_id',ondelete='RESTRICT'),primary_key=True)
    binding_id=Column(String,ForeignKey('of_creator_bindings.id',ondelete='RESTRICT'),nullable=False)
    mode=Column(String(8),nullable=False)
    revision=Column(Integer,nullable=False)
    settings_json=Column(Text,nullable=False)
    readiness_json=Column(Text,nullable=False)
    updated_at=Column(DateTime,nullable=False)
    actor=Column(String(128),nullable=False)


class MoxyFan(Base):
    __tablename__='of_moxy_fan'
    __table_args__=(ForeignKeyConstraint(['account_id','fan_id'],['fans.account_id','fans.fan_id'],ondelete='RESTRICT'),{'info':INFO})
    account_id=Column(String,primary_key=True)
    fan_id=Column(BigInteger,primary_key=True)
    revision=Column(Integer,nullable=False,default=0)
    facts_json=Column(Text,nullable=False,default='{}')
    state_json=Column(Text,nullable=False,default='{}')
    updated_at=Column(DateTime,nullable=False)


class MoxyEvent(Base):
    __tablename__='of_moxy_event'
    __table_args__=(
        ForeignKeyConstraint(['account_id','fan_id'],['fans.account_id','fans.fan_id'],ondelete='RESTRICT'),
        CheckConstraint("kind IN ('turn','welcome','invitation','scheduled','campaign','purchase','refund','chargeback','fulfilment')",name='kind'),
        CheckConstraint("status IN ('pending','generating','ready','queued','done','preview','held','unknown','stale')",name='status'),
        UniqueConstraint('account_id','event_key',name='uq_moxy_event_key'),
        Index('ix_moxy_event_due','account_id','status','created_at'),{'info':INFO})
    account_id=Column(String,ForeignKey('of_moxy_account.account_id',ondelete='RESTRICT'),primary_key=True)
    event_id=Column(String(36),primary_key=True)
    fan_id=Column(BigInteger,nullable=False)
    event_key=Column(String(64),nullable=False)
    kind=Column(String(16),nullable=False)
    payload_json=Column(Text,nullable=False)
    status=Column(String(16),nullable=False)
    reason=Column(String(128))
    snapshot_json=Column(Text)
    snapshot_hash=Column(String(64))
    decision_json=Column(Text)
    intent_id=Column(String(36))
    model_call_id=Column(Integer,ForeignKey('grok_calls.id',ondelete='RESTRICT'))
    reserved_millicents=Column(Integer)
    charged_millicents=Column(Integer)
    budget_day=Column(String(10))
    not_before=Column(DateTime)
    lease_until=Column(DateTime)
    created_at=Column(DateTime,nullable=False)
    updated_at=Column(DateTime,nullable=False)


class MoxyAudit(Base):
    __tablename__='of_moxy_audit'
    __table_args__=({'info':INFO},)
    audit_id=Column(String(36),primary_key=True)
    account_id=Column(String,ForeignKey('of_moxy_account.account_id',ondelete='RESTRICT'),nullable=False)
    actor=Column(String(128),nullable=False)
    before_json=Column(Text,nullable=False)
    after_json=Column(Text,nullable=False)
    created_at=Column(DateTime,nullable=False)
