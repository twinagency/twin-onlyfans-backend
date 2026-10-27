// Lifecycle checks with a fake browser (no Chromium needed): capacity, abandoned
// attempts, launch timeouts, restart limits and what a busy creator is told.
import test from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {createSignInService,networkFingerprint,SignInError} from './service.mjs';

const actor=c=>'creator-session:'+c.repeat(64);
const A={creatorId:'creator-a',actor:actor('a')},B={creatorId:'creator-b',actor:actor('b')};
const network={accountId:null,ownerId:null,proxyUrl:null,fingerprint:networkFingerprint(null)};
const start=(service,scope,net=network)=>service.start(scope,{startKey:randomUUID(),network:net});
const tick=()=>new Promise(r=>setImmediate(r));

function fakeBrowser(state){
  state.launched=(state.launched??0)+1;
  const browser={closed:false,on(){},once(){},async close(){this.closed=true;state.closed=(state.closed??0)+1;}};
  const page={async goto(){},async screenshot(){return Buffer.from([0xff,0xd8,0xff,0,0,0]);},async close(){}};
  const frame={parentFrame:()=>null,url:()=>'https://onlyfans.com/my/profile'};
  const context={on(){},async route(){},async exposeBinding(name,fn){state.observe=p=>fn({page,frame},p);},async addInitScript(){},async newPage(){return page;},
    async cookies(){return [{name:'auth_id',value:String(state.owner??123),domain:'.onlyfans.com',path:'/'},{name:'sess',value:'synthetic',domain:'.onlyfans.com',path:'/'}];},async close(){}};
  browser.newContext=async()=>context;
  return browser;
}
// Two signed requests from the page, as the real interceptor would report them.
async function capture(state,time,owner=123){
  state.owner=owner;
  for(const c of ['a','b']){
    await state.observe({url:'https://onlyfans.com/api2/v2/users/me',identity:{id:owner,username:'synthetic_creator'},rules:{static_param:'synthetic-static'},
      headers:{'user-id':String(owner),'user-agent':'synthetic-agent','x-bc':'synthetic-bc','x-of-rev':'synthetic-rev','x-hash':'synthetic-hash',sign:'s:'+c.repeat(40)+':1f:e',time:String(time.now)}});
  }
  for(let i=0;i<5;i++)await tick();
}
function make({clock,...options}={}){
  const state={},time={now:1_700_000_000_000};
  const service=createSignInService({now:()=>time.now,interceptor:'',maxActive:1,
    launch:async()=>{if(state.fail)throw Error('launch failed');if(state.hang)return new Promise(()=>{});return fakeBrowser(state);},...options});
  return {service,state,time};
}
const rejected=async(promise)=>{try{await promise;}catch(e){return e;}assert.fail('expected a refusal');};

test('abandoned attempt frees the only browser slot instead of blocking everyone for the full lifetime',async()=>{
  const {service,time}=make();
  const a=await start(service,A);await tick();await tick();
  assert.equal(service.status(A,a.id).phase,'waiting');
  // Creator A closes the tab: no more polling. Creator B asks 6 minutes later.
  time.now+=6*60_000;await service.collect();
  const b=await start(service,B);
  assert.equal(b.phase,'opening');
  assert.equal(service.status(A,a.id).phase,'expired');
  await service.close();
});

test('an attempt that is still being used keeps its slot',async()=>{
  const {service,time}=make();
  const a=await start(service,A);await tick();await tick();
  for(let i=0;i<10;i++){time.now+=60_000;service.status(A,a.id);await service.collect();}
  const e=await rejected(start(service,B));
  assert.equal(e.code,'capacity');assert.equal(e.status,429);assert(e.retryAfter>=1);
  await service.close();
});

test('a captured sign-in that is only waiting for confirmation holds no browser, so it does not block capacity',async()=>{
  const {service,state,time}=make();
  const a=await start(service,A);await tick();await tick();
  await capture(state,time);
  assert.equal(service.status(A,a.id).phase,'ready');assert.equal(state.closed,1);
  // The only browser is closed; the next creator must be able to sign in now.
  const b=await start(service,B);assert.equal(b.phase,'opening');
  assert.equal(service.status(A,a.id).phase,'ready','A can still confirm');
  await service.close();
});

test('browser launch that never returns is failed after a deadline and releases the slot',async()=>{
  const {service,state,time}=make({launchTimeout:50});
  state.hang=true;
  const a=await start(service,A);
  await new Promise(r=>setTimeout(r,120));
  assert.equal(service.status(A,a.id).phase,'failed');
  state.hang=false;
  const b=await start(service,B);assert.equal(b.phase,'opening');
  await service.close();void time;
});

test('failed launches do not consume the creator hourly start allowance',async()=>{
  const {service,state}=make();
  state.fail=true;
  for(let i=0;i<8;i++){const j=await start(service,A);await tick();await tick();assert.equal(service.status(A,j.id).phase,'failed');}
  state.fail=false;
  const ok=await start(service,A);assert.equal(ok.phase,'opening');
  await service.close();
});

test('start limit and capacity are different refusals with a wait time',async()=>{
  const {service,time}=make({maxActive:4});
  const other={...network,accountId:'acct-1'};
  for(let i=0;i<6;i++){await start(service,A,i%2?network:{...network,accountId:'acct-'+i,fingerprint:network.fingerprint});await tick();await tick();}
  const e=await rejected(start(service,A,other));
  assert.equal(e.code,'start_limit');assert.equal(e.status,429);assert(e.retryAfter>0&&e.retryAfter<=3600);
  void time;await service.close();
});

test('failure reason and challenge are reported without changing the attempt shape',async()=>{
  const {service,state}=make();
  state.fail=true;
  const a=await start(service,A);await tick();await tick();
  assert.deepEqual(Object.keys(service.status(A,a.id)).sort(),['enrollmentId','expiresAt','handle','height','id','keyboard','phase','width']);
  const d=service.detail(A,a.id);
  assert.equal(d.phase,'failed');assert.equal(d.reason,'browser_unavailable');assert.equal(d.challenge,false);
  await service.close();
});

test('cancel and repeated start are idempotent for one creator and never leak to another',async()=>{
  const {service}=make({maxActive:2});
  const a=await start(service,A);const again=await start(service,A);
  assert.equal(a.id,again.id);
  await assert.rejects(()=>Promise.resolve().then(()=>service.status(B,a.id)),e=>e.status===404);
  assert.equal((await service.cancel(A,a.id)).phase,'cancelled');
  assert.equal((await service.cancel(A,a.id)).phase,'cancelled');
  const next=await start(service,A);assert.notEqual(next.id,a.id);
  await service.close();
});

test('reconnecting an existing account for a different network cancels the stale attempt first',async()=>{
  const {service}=make({maxActive:2});
  const a=await start(service,A);
  const b=await start(service,A,{accountId:'acct-1',ownerId:5,proxyUrl:null,fingerprint:network.fingerprint});
  assert.notEqual(a.id,b.id);assert.equal(service.status(A,a.id).phase,'cancelled');
  await service.close();
});

test('an existing-account reconnect refuses a different owner and discards the capture',async()=>{
  const {service,state,time}=make();
  const a=await start(service,A,{accountId:'acct-1',ownerId:123,proxyUrl:null,fingerprint:network.fingerprint});await tick();await tick();
  await capture(state,time,456);
  assert.equal(service.status(A,a.id).phase,'wrong_account');
  assert.throws(()=>service.capture(A,a.id),e=>e.status===409);
  await service.close();
});

test('acknowledging the same capture twice keeps one enrollment and a different one is refused',async()=>{
  const {service,state,time}=make();
  const a=await start(service,A);await tick();await tick();await capture(state,time);
  const enrollment=randomUUID();
  assert.equal((await service.acknowledge(A,a.id,enrollment)).phase,'submitted');
  assert.equal((await service.acknowledge(A,a.id,enrollment)).enrollmentId,enrollment);
  await assert.rejects(()=>service.acknowledge(A,a.id,randomUUID()),e=>e.status===409);
  await service.close();
});
