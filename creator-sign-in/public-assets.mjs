// Only versioned, explicitly public immutable UI code/fonts. No pages, API,
// challenges, credentials, account media or authenticated responses are cached.
export function createPublicAssetCache({maxBytes=32*1024*1024,maxEntries=256,maxEntryBytes=4*1024*1024,maxTtl=3600_000,now=Date.now}={}){
  const entries=new Map();let bytes=0,hits=0,misses=0,closed=false,pending=0;
  const remove=key=>{const value=entries.get(key);if(value){bytes-=value.body.length;entries.delete(key);}};
  const keyFor=async request=>{
    if(closed||request.method()!=='GET')return null;
    let url;try{url=new URL(request.url());}catch{return null;}
    if(url.origin!=='https://static2.onlyfans.com'||url.search||url.hash||!/^\/static\/prod\/[A-Za-z0-9_-]+\/\d{12,16}-[a-f0-9]{8,64}\/[A-Za-z0-9_./-]+\.(?:js|css|woff2?|ttf)$/.test(url.pathname))return null;
    const h=await request.allHeaders();
    if(['authorization','user-id','x-bc','x-hash','sign','x-of-rev'].some(k=>h[k]))return null;
    // Anonymous cookies may accompany public assets; logged-in sessions bypass.
    const cookies=(h.cookie??'').split(';').map(v=>v.trim().split('='));
    if(cookies.some(([name,value])=>['sess','auth_hash'].includes(name.toLowerCase())&&value||name.toLowerCase()==='auth_id'&&value&&value!=='0'))return null;
    return url.href+'\n'+(h.origin??'')+'\n'+(h['accept-language']??'');
  };
  return {
    async serve(route){
      const key=await keyFor(route.request());if(!key)return false;
      const row=entries.get(key);
      if(!row||row.expires<=now()){remove(key);misses++;return false;}
      entries.delete(key);entries.set(key,row);hits++;
      await route.fulfill({status:200,headers:row.headers,body:row.body});return true;
    },
    async remember(response){
      if(closed||pending>=32||response.status()!==200)return;
      const key=await keyFor(response.request());if(!key||entries.has(key))return;
      const headers=await response.allHeaders(),cc=headers['cache-control']??'';
      if(!/(?:^|,)\s*public\s*(?:,|$)/i.test(cc)||!/(?:^|,)\s*immutable\s*(?:,|$)/i.test(cc)||/\b(?:private|no-store|no-cache)\b/i.test(cc)||headers['set-cookie'])return;
      const ttl=Math.min(maxTtl,Number(cc.match(/(?:^|,)\s*max-age=(\d+)/i)?.[1]??0)*1000);
      if(!ttl||!/^\s*(?:text\/(?:javascript|css)|application\/(?:javascript|x-javascript|font-woff)|font\/(?:woff2?|ttf))(?:\s*;|\s*$)/i.test(headers['content-type']??''))return;
      if((headers.vary??'').split(',').some(v=>v.trim()&&!['accept-encoding','origin','accept-language'].includes(v.trim().toLowerCase())))return;
      if(Number(headers['content-length']??0)>maxEntryBytes)return;
      if(closed||pending>=32)return;
      pending++;
      try{
        const body=await response.body();if(closed||!body.length||body.length>maxEntryBytes||body.length>maxBytes)return;
        const safe={...headers};for(const h of ['content-encoding','content-length','transfer-encoding','connection','set-cookie'])delete safe[h];
        remove(key);
        while(entries.size&&(entries.size>=maxEntries||bytes+body.length>maxBytes))remove(entries.keys().next().value);
        entries.set(key,{body,headers:safe,expires:now()+ttl});bytes+=body.length;
      }finally{pending--;}
    },
    stats:()=>({entries:entries.size,bytes,hits,misses}),
    close(){closed=true;entries.clear();bytes=0;},
  };
}
