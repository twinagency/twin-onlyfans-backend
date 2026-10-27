// Test the actual sign-in service after scrolling, with local page content only.
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {launchSignInBrowser} from './browser.mjs';
import {createSignInService,networkFingerprint} from './service.mjs';
const scope={creatorId:'scroll-fixture',actor:'creator-session:'+'a'.repeat(64)};
const network={accountId:null,ownerId:null,proxyUrl:null,fingerprint:networkFingerprint(null)};
let browser;
const service=createSignInService({interceptor:'',launch:async()=>{browser=await launchSignInBrowser(null,{headless:process.env.TWIN_SIGNIN_TEST_HEADED!=='1',chromiumSandbox:process.env.TWIN_SIGNIN_TEST_SANDBOX==='1'});return browser;},transport:route=>route.fulfill({contentType:'text/html',body:'<style>body{margin:0}div{height:800px}</style><div style="background:red"></div><div style="background:blue"><input type="email" style="margin-top:100px;height:40px;width:300px"></div><div style="background:green"></div>'})});
async function until(fn){for(let n=0;n<100;n++){if(await fn())return;await new Promise(r=>setTimeout(r,30));}throw Error('Scroll fixture not ready');}
try{
 const a=await service.start(scope,{startKey:randomUUID(),network});await until(()=>service.status(scope,a.id).phase==='waiting');
 const page=browser.contexts()[0].pages()[0];await service.input(scope,a.id,{type:'scroll',delta:850,x:.5,y:.5});await until(async()=>await page.evaluate(()=>scrollY)>=800);
 const shot=await service.frame(scope,a.id,{format:'png'});assert.equal(shot.readUInt32BE(16),864);assert.equal(shot.readUInt32BE(20),1440);
 const viewer=await(await browser.newContext()).newPage();
 const rgb=await viewer.evaluate(async data=>{const image=new Image();image.src='data:image/png;base64,'+data;await image.decode();const canvas=document.createElement('canvas');canvas.width=image.width;canvas.height=image.height;const ctx=canvas.getContext('2d');ctx.drawImage(image,0,0);return [...ctx.getImageData(20,20,1,1).data];},shot.toString('base64'));
 assert(rgb[2]>200&&rgb[0]<50,'The frame must show the current blue viewport after scrolling');
 await service.input(scope,a.id,{type:'click',x:100/432,y:70/720});assert.equal(service.status(scope,a.id).keyboard,'email','Input coordinates must map to the visible scrolled field');
 console.log(JSON.stringify({scrollY:await page.evaluate(()=>scrollY),topPixel:rgb,pixels:[864,1440],inputMapping:true}));
 console.log('PASS actual sign-in service: scrolled viewport pixels and input coordinates remain aligned');
}finally{await service.close();}
