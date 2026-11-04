"""Render before/after transcript JSON (tests/test_moxy_conversation_repair_pg.py dumps) as plain text."""
import json,sys,glob,os
def render(path):
    d=json.load(open(path));out=[d['id']+': '+d['title']+' ['+d['creator']+']']
    for t in d['turns']:
        out.append('  FAN: '+t['fan'])
        for a in t['attempts']:
            out.append('    %s draft (%s%s): %s'%(a['kind'],a['draft_action'],' product=%s'%a['draft_product'] if a['draft_product'] else '',' | '.join(a['draft'])))
            out.append('      checked -> %s%s'%(a['status'],' (%s)'%a['reason'] if a['reason'] else ''))
            r=a.get('rejected')
            if r:out.append('      correction brief: kind=%s product_id=%s keep=%s'%(r.get('kind'),r.get('product_id'),r.get('keep')))
            p=a['prompt']
            out.append('      writer saw: fan_name=%s, offer=%s, reads_as=%s'%(p.get('fan_name'),p.get('first_offer'),p.get('situation')))
        out.append('    DELIVERED (%s): %s'%(t['final_status'],' || '.join('%s%s'%(x['body'],' [CARD $%.2f]'%(x['price_cents']/100) if x['price_cents'] else '') for x in t['sent']) or '(nothing)'))
    return '\n'.join(out)
if __name__=='__main__':
    base=sys.argv[1]
    for sub in ('before','after'):
        print('=== %s ==='%sub.upper())
        for f in sorted(glob.glob(os.path.join(base,sub,'*.json'))):print(render(f));print()
