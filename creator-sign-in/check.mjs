// Real isolated Chromium and the existing interceptor; all provider requests
// are fulfilled locally. This does not claim a real OnlyFans login.
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {chromium} from 'playwright';
import {createSignInService,networkFingerprint,allowedRequest} from './service.mjs';

const scope={creatorId:'synthetic-creator',actor:'creator-session:'+'a'.repeat(64)};
const other={creatorId:'another-creator',actor:'creator-session:'+'b'.repeat(64)};
const network={accountId:null,ownerId:null,proxyUrl:null,fingerprint:networkFingerprint(null)};
let clock=Date.now(),launches=0,closed=0,loginCalls=0;
const page=`<!doctype html><style>input,button{position:absolute;left:20px;width:300px;height:40px}#email{top:80px}#password{top:140px}button{top:200px}</style><h1>Synthetic provider sign-in</h1><form><input id="email" type="email"><input id="password" type="password"><button>Sign in</button></form><script>
document.querySelector('form').onsubmit=async e=>{e.preventDefault();const r=await fetch('/api2/v2/users/login',{method:'POST',body:JSON.stringify({email:document.querySelector('#email').value,password:document.querySelector('#password').value})});if(!r.ok)return;
const chunk=[[],{89668:m=>{m.exports=()=> 'a'.repeat(40)}}];self.webpackChunkof_vue.push(chunk);const mod={exports:null};chunk[1][89668](mod,{},null);mod.exports('synthetic-static\\n1\\n/api2/v2/users/me\\n123');
for(let i=0;i<2;i++){await fetch('/api2/v2/users/me',{headers:{'user-id':'123','x-bc':'synthetic-bc','x-of-rev':'synthetic-rev',sign:'s:'+(i?'b':'a').repeat(40)+':123:e',time:String(Date.now())}});await new Promise(r=>setTimeout(r,50));}};</script>`;
const service=createSignInService({now:()=>clock,maxActive:1,launch:async proxy=>{
  assert.equal(proxy,null);launches++;
  const browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});browser.on('disconnected',()=>closed++);return browser;
},transport:async(route,context)=>{
  const request=route.request(),u=new URL(request.url());
  if(u.pathname==='/api2/v2/users/login'){
    const credentials=request.postDataJSON();assert.equal(credentials.email,'fixture@example.invalid');assert.equal(credentials.password,'synthetic-password');loginCalls++;
    await context.addCookies([{name:'auth_id',value:'123',domain:'.onlyfans.com',path:'/',httpOnly:true,secure:true},{name:'sess',value:'synthetic-session',domain:'.onlyfans.com',path:'/',httpOnly:true,secure:true}]);
    return route.fulfill({contentType:'application/json',body:'{}'});
  }
  if(u.pathname==='/api2/v2/users/me')return route.fulfill({contentType:'application/json',body:JSON.stringify({id:123,username:'synthetic_creator',email:'must-not-transfer'})});
  if(u.origin==='https://onlyfans.com'&&u.pathname==='/my/profile')return route.fulfill({contentType:'text/html',body:page});
  return route.fulfill({status:404,body:''});
}});
async function until(predicate){for(let i=0;i<120;i++){if(await predicate())return;await new Promise(r=>setTimeout(r,50));}throw Error('Synthetic sign-in did not reach the expected state');}
try{
  const started=await service.start(scope,{startKey:randomUUID(),network});
  const resumed=await service.start(scope,{startKey:randomUUID(),network});assert.equal(started.id,resumed.id);
  await until(()=>service.status(scope,started.id).phase==='waiting');
  await assert.rejects(()=>service.start(other,{startKey:randomUUID(),network}),e=>e.status===429);
  assert.throws(()=>service.status(other,started.id),e=>e.status===404);
  const frame=await service.frame(scope,started.id);assert.equal(frame[0],0xff);assert.equal(frame[1],0xd8);
  await service.input(scope,started.id,{type:'click',x:100/432,y:100/720});
  assert.equal(service.status(scope,started.id).keyboard,'email');
  await service.input(scope,started.id,{type:'text',text:'fixture@example.invalid'});
  await service.input(scope,started.id,{type:'key',key:'Tab'});
  assert.equal(service.status(scope,started.id).keyboard,'password');
  await service.input(scope,started.id,{type:'text',text:'synthetic-password'});
  await service.input(scope,started.id,{type:'key',key:'Enter'});
  await until(()=>service.status(scope,started.id).phase==='ready');
  const captured=service.capture(scope,started.id);assert.equal(captured.profileId,scope.creatorId);assert.equal(captured.session.ownerId,123);assert.equal(captured.session.networkFingerprint,network.fingerprint);
  assert.equal(captured.handle,'synthetic_creator');assert.equal(captured.session.browser.samples.length,2);
  assert.deepEqual(service.capture(scope,started.id),captured,'A lost native response must retry the exact capture');
  assert(!JSON.stringify(service.status(scope,started.id)).includes('synthetic-session'));
  assert(!JSON.stringify(captured).includes('must-not-transfer'));
  await assert.rejects(()=>service.input(scope,started.id,{type:'text',text:'must not send'}));
  await assert.rejects(()=>service.frame(scope,started.id));
  const enrollment=randomUUID();assert.equal((await service.acknowledge(scope,started.id,enrollment)).phase,'submitted');
  assert.equal((await service.acknowledge(scope,started.id,enrollment)).phase,'submitted');assert.throws(()=>service.capture(scope,started.id));
  await until(()=>closed===1);assert.equal(launches,1);assert.equal(loginCalls,1);
  const second=await service.start(scope,{startKey:randomUUID(),network});assert.notEqual(second.id,started.id);
  await service.cancel(scope,second.id);assert.equal(service.status(scope,second.id).phase,'cancelled');
  const third=await service.start(scope,{startKey:randomUUID(),network});clock+=16*60_000;await service.collect();assert.throws(()=>service.status(scope,third.id),e=>e.status===404);
  for(const [url,method,expected]of [['https://onlyfans.com/api2/v2/chats/123/messages','POST',false],['https://onlyfans.com/api2/v2/payments','POST',false],['https://onlyfans.com/api2/v2/users/login','POST',true],['http://127.0.0.1/private','GET',false],['https://foreign.example/','GET',false]]){
    assert.equal(allowedRequest({url:()=>url,method:()=>method,isNavigationRequest:()=>false}),expected);
  }
  console.log('PASS self-hosted sign-in: actual browser input and capture, one start/resume, isolated creator scope, capacity, frozen retry, minimal metadata, native handoff acknowledgement, cleanup, expiry, no fan sends or purchases.');
}finally{await service.close();}
