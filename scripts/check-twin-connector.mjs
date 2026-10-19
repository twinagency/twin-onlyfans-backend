import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {webcrypto,createHash} from 'node:crypto';
const root=process.cwd(),read=p=>readFileSync(root+'/'+p,'utf8');
let listener,alarm,removed,state={},nextTab=40,submitted=[],outcome={ok:true};
const tabs=new Map([[1,{id:1,url:'https://dashboard.tgaiclone.xyz/creator/platforms'}]]),created=[];
const chrome={runtime:{id:'synthetic-extension',getURL:p=>'chrome-extension://synthetic-extension/'+p,onMessage:{addListener:f=>listener=f}},
 storage:{session:{async get(k){return {[k]:structuredClone(state[k])}},async set(v){state=structuredClone({...state,...v})},async remove(k){delete state[k]}}},
 tabs:{async create(v){const tab={id:++nextTab,...v};tabs.set(tab.id,tab);created.push(tab);return tab},async get(id){return tabs.get(id)},async update(id,v){Object.assign(tabs.get(id),v)},
 async sendMessage(tab,m,options){submitted.push(structuredClone({tab,m,options}));return outcome},onRemoved:{addListener:f=>removed=f}},
 cookies:{async getAllCookieStores(){return [{id:'default',tabIds:[41,43,45,47,49,51]}]},async getAll(){return [{name:'auth_id',value:'123',domain:'.onlyfans.com',path:'/'},{name:'sess',value:'synthetic-secret',domain:'.onlyfans.com',path:'/'}]}},
 alarms:{create(){},onAlarm:{addListener:f=>alarm=f}}};
vm.runInNewContext(read('loginExtension/twin/background.js'),{chrome,crypto:webcrypto,URL,Date,console});
const dashboard={id:chrome.runtime.id,frameId:0,url:tabs.get(1).url,documentId:'original-document',tab:{id:1}};
const ui={id:chrome.runtime.id,url:chrome.runtime.getURL('confirm.html')};
const send=(m,s=dashboard)=>new Promise(resolve=>listener(m,s,resolve));
assert.equal((await send({type:'ping'})).version,'1.0.0');
for(const sender of [{...dashboard,url:'https://evil.example/creator/platforms'},{...dashboard,frameId:2},{...dashboard,url:'https://dashboard.tgaiclone.xyz/creator/platforms/forged'},{...dashboard,id:'different-extension'}])assert.equal((await send({type:'start',profileId:'creator'},sender)).ok,false);
assert.equal((await send({type:'getCookies'})).ok,false);
let started=await send({type:'start',profileId:'canonical-creator'});assert.equal(started.phase,'waiting');assert.equal(created[0].url,'https://onlyfans.com/my/profile');
const ofSender={id:chrome.runtime.id,frameId:0,url:'https://onlyfans.com/my/profile',documentId:'of-document',tab:{id:41}};
function capture(i){return {type:'capture',payload:{url:'https://onlyfans.com/api2/v2/users/me',headers:{'user-id':'123','user-agent':'Synthetic agent','x-bc':'synthetic-bc','x-of-rev':'synthetic-rev',sign:'prefix:'+String(i).repeat(40)+':123:end',time:String(Date.now())},rules:{static_param:'synthetic-static'},identity:{id:123,username:'synthetic_creator'}}};}
await send(capture(1),{...ofSender,tab:{id:999}});assert.equal(state.job.samples.length,0);
await send(capture(1),ofSender);assert.equal(state.job.samples.length,1);assert.equal(state.job.phase,'waiting');
await send(capture(2),ofSender);assert.equal(state.job.phase,'ready');assert.equal(submitted.length,0,'Capture must not submit without confirmation');
assert.equal((await send({type:'confirm'})).ok,false,'Web pages cannot confirm');
assert(!JSON.stringify(await send({type:'status'})).includes('synthetic-secret'),'Status contains metadata only');
outcome={ok:false,retry:true,error:'Synthetic unconfirmed outcome'};
assert.equal((await send({type:'confirm'},ui)).phase,'retry');const first=submitted[0];
assert.equal(first.m.command.profileId,'canonical-creator');assert.equal(first.m.command.handle,'synthetic_creator');assert.equal(first.options.documentId,'original-document');
outcome={ok:true};assert.equal((await send({type:'confirm'},ui)).phase,'submitted');assert.deepEqual(submitted[1],first,'Retries must preserve the exact request');
assert.equal(state.job.command,undefined);assert.equal(state.job.samples,undefined);assert(!JSON.stringify(state).includes('synthetic-secret'));
await send({type:'start',profileId:'canonical-creator'});state.job.deadline=0;await send({type:'status'});assert.equal(state.job,undefined);
await send({type:'start',profileId:'canonical-creator'});await send({type:'cancel'},{...dashboard,documentId:'changed-document'});assert(state.job);
await send({type:'cancel'});assert.equal(state.job,undefined);

// Exercise the real page-world interceptor: a complete request never borrows
// headers from an unrelated request, and only /users/me identity is retained.
const posted=[],events={};
class XHR {open(){}setRequestHeader(){}send(){}addEventListener(n,f){this[n]=f}}
const win={postMessage:m=>posted.push(m),fetch:()=>Promise.resolve({ok:false})};
const context={window:win,self:{},location:{origin:'https://onlyfans.com'},navigator:{userAgent:'Synthetic agent'},XMLHttpRequest:XHR,Headers,URL,Date,console};
vm.runInNewContext(read('loginExtension/interceptor.js'),context);
const module={exports:null},chunk=[[],{89668:m=>{m.exports=input=>createHash('sha1').update(input).digest('hex')}}];
context.self.webpackChunkof_vue.push(chunk);chunk[1][89668](module,{},null);module.exports('synthetic-static\n1\n/api2/v2/users/me\n123');
const xhr=new XHR();xhr.open('GET','/api2/v2/users/me');for(const [k,v]of Object.entries(capture(1).payload.headers))xhr.setRequestHeader(k,v);xhr.send();
xhr.status=200;xhr.responseType='json';xhr.response={id:123,username:'synthetic_creator',email:'must-not-transfer'};xhr.load();
assert(posted.some(m=>m.payload.identity?.username==='synthetic_creator'));assert(!JSON.stringify(posted).includes('must-not-transfer'));
const before=posted.length,other=new XHR();other.open('GET','https://evil.example/api2/v2/users/me');other.setRequestHeader('x-bc','foreign');other.send();assert.equal(posted.length,before);
const incomplete=new XHR();incomplete.open('GET','/api2/v2/users/me');incomplete.setRequestHeader('user-id','999');incomplete.send();assert.equal(posted.length,before);
const manifest=JSON.parse(read('loginExtension/twin/manifest.json'));assert.deepEqual(manifest.permissions,['storage','tabs','cookies','alarms']);assert(!manifest.host_permissions.includes('<all_urls>'));
console.log('PASS Twin Connect: exact origin/tab/document scope, explicit confirmation, metadata-only status, exact retry, secret cleanup, expiry, cancellation, atomic request capture, minimal identity.');
