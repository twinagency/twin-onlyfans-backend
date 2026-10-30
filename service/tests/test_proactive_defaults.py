import json
from types import SimpleNamespace
from onlyfans_runtime import connections
from onlyfans_runtime.contextual_mode import new_account_settings, mode
from db.models import AccountAiConfig,OfConversationState
from db.onlyfans_models import OfRuntime

def test_provisioning_saves_sales_defaults_but_leaves_sending_disabled(monkeypatch):
    added=[]
    class Session:
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def begin(self):return self
        def execute(self,*args,**kwargs):pass
        def get(self,*args):return None
        def add(self,row):added.append(row)
        def flush(self):pass
    monkeypatch.setattr(connections,"Session",Session)
    account=connections.provision_account(SimpleNamespace(engine=None),"agency-a",
        creator_id="creator-a",creator_name="Synthetic Creator",owner_id=12345,handle="synthetic",
        command_key="a"*64,actor="op-session:"+"b"*64)
    cfg=next(r for r in added if isinstance(r,AccountAiConfig))
    runtime=next(r for r in added if isinstance(r,OfRuntime))
    raw=json.loads(cfg.ai_chatter_config_json)
    assert account=="of-12345" and mode(raw)=="live"
    assert all(raw[k] is True for k in ("pack_send_enabled","pack_on_ask_enabled","qualification_gate_enabled","fastt_vault_mode"))
    assert raw["fastt_text_only"] is False and runtime.enabled is False
    assert not any(isinstance(r,OfConversationState) for r in added)
    assert "fastt_scope" not in raw and "automate_new_conversations" not in raw

def test_saved_operator_off_and_invalid_values_still_win():
    raw=new_account_settings();raw["fastt_contextual_sales_mode"]="off"
    assert mode(raw)=="off"
    raw["fastt_contextual_sales_mode"]="invalid"
    assert mode(raw)=="off"

def test_default_instances_are_independent():
    one=new_account_settings();one["pack_send_enabled"]=False
    assert new_account_settings()["pack_send_enabled"] is True
