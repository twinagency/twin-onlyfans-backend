import test from 'node:test';
import assert from 'node:assert/strict';
import {createPublicAssetCache} from './public-assets.mjs';
const url='https://static2.onlyfans.com/static/prod/f/202610231500-abcdef123456/chunk-vendors-12345678.js';
const request=(extra={})=>({url:()=>url,method:()=>'GET',allHeaders:async()=>({origin:'https://onlyfans.com',cookie:'lang=en; auth_id=0'}),...extra});
const headers={'cache-control':'public, max-age=604800, immutable','content-type':'text/javascript; charset=utf-8','content-encoding':'gzip',vary:'Accept-Encoding, Origin'};
const response=(req=request(),extra={})=>({request:()=>req,status:()=>200,allHeaders:async()=>headers,body:async()=>Buffer.from('window.fixture=1'),...extra});
async function hit(cache,req=request()){let value=null;const result=await cache.serve({request:()=>req,fulfill:async v=>{value=v;}});return {result,value};}
test('fresh anonymous logins reuse public versioned code and retain only decoded payload headers',async()=>{
 const c=createPublicAssetCache();await c.remember(response());const h=await hit(c);
 assert(h.result);assert.equal(h.value.body.toString(),'window.fixture=1');assert(!h.value.headers['content-encoding']);assert.equal(c.stats().entries,1);
 assert(!(await hit(c,request({url:()=>url.replace('abcdef123456','abcdef999999')}))).result,'A new provider build must use fresh code');c.close();
});
test('pages, API, media, challenge assets, signed queries and other origins cannot enter the cache',async()=>{
 for(const denied of ['https://onlyfans.com/','https://onlyfans.com/api2/v2/users/me','https://cdn2.onlyfans.com/media/fixture.js','https://challenges.cloudflare.com/static/prod/f/202610231500-abcdef123456/test.js',url+'?token=synthetic',url.replace('static2.onlyfans.com','attacker.example'),url.replace('/202610231500-abcdef123456/','/unversioned/')]){
  const c=createPublicAssetCache(),r=request({url:()=>denied});await c.remember(response(r));assert.equal(c.stats().entries,0);assert(!(await hit(c,r)).result);c.close();
 }
});
test('authenticated requests, non-GETs and session-setting or private responses always bypass',async()=>{
 const c=createPublicAssetCache();await c.remember(response());
 for(const h of [{cookie:'auth_id=123; sess=synthetic'},{cookie:'sess=synthetic'},{authorization:'Bearer synthetic'},{'user-id':'123'},{'x-bc':'synthetic'},{sign:'synthetic'}]){
  const r=request({allHeaders:async()=>h});assert(!(await hit(c,r)).result);const empty=createPublicAssetCache();await empty.remember(response(r));assert.equal(empty.stats().entries,0);empty.close();
 }
 assert(!(await hit(c,request({method:()=>'POST'}))).result);c.close();
 for(const h of [{'cache-control':'public, immutable, max-age=60, private'},{'cache-control':'public, immutable, max-age=60, no-store'},{'cache-control':'max-age=60, immutable'},{'cache-control':'public, max-age=60'},{'set-cookie':'sess=synthetic'},{vary:'Cookie'},{vary:'*'},{'content-type':'text/html'},{'content-type':'application/json'}]){
  const empty=createPublicAssetCache();await empty.remember(response(request(),{allHeaders:async()=>({...headers,...h})}));assert.equal(empty.stats().entries,0);empty.close();
 }
});
test('expiry, language/origin variants, entry limits and shutdown bound retained public bytes',async()=>{
 let time=0;const c=createPublicAssetCache({now:()=>time,maxEntries:1,maxBytes:100});await c.remember(response());
 assert(!(await hit(c,request({allHeaders:async()=>({origin:'https://onlyfans.com','accept-language':'fr'})}))).result);
 time=3600_001;assert(!(await hit(c)).result);assert.equal(c.stats().bytes,0);
 await c.remember(response());const other=request({url:()=>url.replace('12345678.js','99999999.js')});await c.remember(response(other));assert.equal(c.stats().entries,1);assert(!(await hit(c)).result);assert((await hit(c,other)).result);
 c.close();await c.remember(response());assert.equal(c.stats().entries,0);assert.equal(c.stats().bytes,0);
});
test('oversized public files and late responses after shutdown are not retained',async()=>{
 const c=createPublicAssetCache({maxEntryBytes:8});await c.remember(response());assert.equal(c.stats().entries,0);c.close();
 const late=createPublicAssetCache();let release;const held=new Promise(r=>{release=r;});
 const storing=late.remember(response(request(),{body:async()=>{await held;return Buffer.from('fixture');}}));await new Promise(r=>setImmediate(r));late.close();release();await storing;assert.equal(late.stats().bytes,0);
});
