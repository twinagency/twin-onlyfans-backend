"""Provider profile observations update display fields, never recipient identity or mode."""
import re,json,hashlib
from urllib.parse import urlsplit
from db.models import Fan,OfCreatorBinding

def profile(raw):
    if type(raw) is not dict:return {}
    result={}
    name=raw.get('name');handle=raw.get('username');avatar=raw.get('avatar')
    if type(name) is str and 0<len(name.strip())<=256:result['name']=name.strip()
    if type(handle) is str and re.fullmatch(r'[A-Za-z0-9._-]{1,64}',handle):result['handle']=handle
    if type(avatar) is str and len(avatar)<=2048:
        try:
            u=urlsplit(avatar)
            if u.scheme=='https' and u.hostname and not u.username and not u.password and not u.port and any(u.hostname==d or u.hostname.endswith('.'+d) for d in ('onlyfans.com','ofusercontent.com','onlyfansusercontent.com')):
                result['avatar']=avatar
        except ValueError:pass
    return result

def apply_profiles(s,runtime,fan_ids,profiles,creator=None):
    if type(profiles) is not dict or any(type(fan) is not int or fan not in fan_ids for fan in profiles):return
    for fan,raw in profiles.items():
        data=profile(raw)
        if not data:continue
        row=s.get(Fan,(runtime.account_id,fan))
        if row is None:row=Fan(account_id=runtime.account_id,fan_id=fan);s.add(row)
        if 'name' in data:row.of_display_name=data['name']
        if 'handle' in data:row.of_username=data['handle']
        if 'avatar' in data:row.avatar_url=data['avatar']
    data=profile(creator)
    if data:
        binding=s.get(OfCreatorBinding,runtime.binding_id)
        if binding and 'name' in data and ('@' in binding.creator_name or not binding.creator_name.strip()):binding.creator_name=data['name']
        if binding and 'handle' in data:binding.handle=data['handle']
        # Reuse immutable import-command evidence for this small display-only
        # observation. No credentials, recipient identity or automation changes.
        from .launch_operations_models import OfLaunchCommand
        from .store import utcnow
        encoded=json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=False)
        key=hashlib.sha256(('provider_profile:'+runtime.binding_id+':'+encoded).encode()).hexdigest()
        if s.get(OfLaunchCommand,(runtime.account_id,key)) is None:
            s.add(OfLaunchCommand(account_id=runtime.account_id,command_key=key,binding_id=runtime.binding_id,
                kind='provider_profile_observation',request_hash=key,actor='worker-import:'+key,
                reason='Verified account owner profile from provider inbox sync',before_json='{}',
                result_json=encoded,created_at=utcnow()))
