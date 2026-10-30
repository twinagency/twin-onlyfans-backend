import asyncio
from unittest.mock import patch
from automations import pack_pricing as p
async def check(paid=0,misses=0,video=False,opening=None,n=20,unit=300):
    ctx=p.LadderCtx(paid,misses,10000)
    async def context(*a): return ctx
    async def moving(*a): return video
    async def values(account,ids): return {i:unit for i in ids}
    async def kinds(account,ids): return {i:("video" if video else "photo") for i in ids}
    with patch.object(p,"ladder_context",context),patch.object(p,"has_moving_media",moving),patch.object(p,"_values_for",values),patch.object(p.content_resolver,"kind_of",kinds):
        return await p.negotiate_pack("test",1,None,list(range(n)),{} if opening is None else {"fastt_pack_open_price_cents":opening})
async def main():
    cases=0
    for opening in (2500,2000):
        r=await check(opening=opening);assert r.price_cents==opening and len(r.media)>=3;r2=await check(paid=opening,opening=opening);assert r2.price_cents==2700
        r=await check(paid=opening,video=True,opening=opening);assert r.price_cents==3600
        assert (await check(misses=1,opening=opening)).price_cents==1000
        assert (await check(misses=2,opening=opening)).price_cents==670
        assert (await check(opening=opening,n=2)).refusal=="no_price"
        r=await check(opening=opening,unit=1000);assert len(r.media)>=3 and r.price_cents==(2500 if opening==2500 else 2700)
        cases+=7
    for opening in (None,True,"2000",0,1000,3600,-1):
        assert (await check(opening=opening)).price_cents==2700;cases+=1
    for paid,expected in ((1000,2700),(2700,3600),(5900,7200)):
        assert (await check(paid=paid,opening=2000)).price_cents==expected;cases+=1
    print("PASS",cases,"opening-price, purchase progression, video, walkdown, content and invalid-setting checks")
asyncio.run(main())
