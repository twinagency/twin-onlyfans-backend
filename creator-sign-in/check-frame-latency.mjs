// Actual Chromium with a deliberately unfinished font request. No external login.
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {createSignInService,networkFingerprint} from './service.mjs';
import {launchSignInBrowser} from './browser.mjs';
const scope={creatorId:'frame-fixture',actor:'creator-session:'+'a'.repeat(64)};
const network={accountId:null,ownerId:null,proxyUrl:null,fingerprint:networkFingerprint(null)};
let fontRequested=false,releaseFont;const font=new Promise(r=>{releaseFont=r;});
const html=`<!doctype html><style>@font-face{font-family:slow;src:url('https://static2.onlyfans.com/static/prod/f/202610231500-abcdef123456/fixture.woff2')}body{font-family:slow,Arial}input{position:absolute;top:80px;left:20px;width:300px;height:40px}</style><h1>Font loading fixture</h1><input type="email">`;
const service=createSignInService({interceptor:'',launch:proxy=>launchSignInBrowser(proxy,{headless:process.env.TWIN_SIGNIN_TEST_HEADED!=='1',chromiumSandbox:process.env.TWIN_SIGNIN_TEST_SANDBOX==='1'}),transport:async route=>{
 if(route.request().url().endsWith('.woff2')){fontRequested=true;await font;return route.fulfill({status:204,body:''});}
 return route.fulfill({contentType:'text/html',body:html});
}});
async function until(check){for(let n=0;n<100;n++){if(check())return;await new Promise(r=>setTimeout(r,50));}throw Error('Fixture not ready');}
try{
 const a=await service.start(scope,{startKey:randomUUID(),network});await until(()=>service.status(scope,a.id).phase==='waiting'&&fontRequested);
 const started=performance.now(),image=await service.frame(scope,a.id,{format:'png'}),ms=Math.round(performance.now()-started);
 assert.equal(image.readUInt32BE(16),864);assert.equal(image.readUInt32BE(20),1440);assert(ms<1500,'An unfinished font must not delay the verification screen');
 await service.input(scope,a.id,{type:'click',x:100/432,y:100/720});assert.equal(service.status(scope,a.id).keyboard,'email');
 console.log(JSON.stringify({unfinishedFont:true,frameMs:ms,pixels:[864,1440],inputMapping:true}));
 console.log('PASS screen capture renders before remote fonts finish and preserves the full-resolution input grid');
}finally{releaseFont();await service.close();}
