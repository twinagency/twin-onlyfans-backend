"""Prepare source-backed catalogue bindings. Does not enable products or accounts."""
import argparse,json,os,hashlib
from onlyfans_runtime.store import Store
from onlyfans_runtime.offer_import import moxy_candidates,moxy_vault_candidates,materialize_moxy_products

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tenant',required=True);p.add_argument('--account',required=True)
    p.add_argument('--creator',required=True);p.add_argument('--classifications')
    p.add_argument('--after-observation');p.add_argument('--after-media',type=int,default=0)
    p.add_argument('--limit',type=int,default=10)
    args=p.parse_args(argv)
    if os.environ.get('ONLYFANS_PROCESS_ROLE')!='worker':p.error('worker role required')
    store=Store(os.environ['DATABASE_URL'])
    try:
        if args.classifications:
            values=json.loads(open(args.classifications).read())
            actor='worker-import:'+hashlib.sha256(
                (args.tenant+':'+args.account+':moxy-source-catalogue').encode()).hexdigest()
            result=materialize_moxy_products(store,args.tenant,args.account,creator_key=args.creator,
                classifications=values,actor=actor)
        else:
            result={'history':moxy_candidates(store,args.tenant,args.account,creator_key=args.creator,
                after=args.after_observation,limit=args.limit),
                'vault':moxy_vault_candidates(store,args.tenant,args.account,creator_key=args.creator,
                after=args.after_media,limit=args.limit)}
        print(json.dumps(result,default=str))
    finally:store.close()
if __name__=='__main__':main()
