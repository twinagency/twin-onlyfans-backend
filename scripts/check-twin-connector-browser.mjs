// Real Chromium extension, synthetic signed requests and cookies only.
import assert from 'node:assert/strict';
import {mkdtempSync,readFileSync,writeFileSync,mkdirSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {resolve,join} from 'node:path';
import {pathToFileURL} from 'node:url';
const {chromium}=process.env.PLAYWRIGHT_MODULE?await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE)):await import('playwright');
const temp=mkdtempSync(join(tmpdir(),'twin-connect-browser-')),extension=join(temp,'extension');mkdirSync(extension);
for(const name of ['manifest.json','background.js','of-bridge.js','dashboard-bridge.js','confirm.html','confirm.css','confirm.js'])writeFileSync(join(extension,name),readFileSync(resolve('loginExtension/twin',name)));
writeFileSync(join(extension,'interceptor.js'),readFileSync('loginExtension/interceptor.js'));
const context=await chromium.launchPersistentContext(join(temp,'profile'),{headless:true,channel:'chromium',args:['--disable-extensions-except='+extension,'--load-extension='+extension]});
const submissions=[];
try{
 await context.addCookies([{name:'auth_id',value:'123',domain:'.onlyfans.com',path:'/',secure:true,httpOnly:true},{name:'sess',value:'synthetic-session',domain:'.onlyfans.com',path:'/',secure:true,httpOnly:true}]);
 await context.route('https://onlyfans.com/**',async route=>{
  if(route.request().url().includes('/api2/v2/'))return route.fulfill({contentType:'application/json',body:JSON.stringify({id:123,username:'synthetic_creator',email:'must-not-transfer'})});
  return route.fulfill({contentType:'text/html',body:`<!doctype html><h1>Synthetic OnlyFans login fixture</h1><script>
  const chunk=[[],{89668:m=>{m.exports=()=> 'a'.repeat(40)}}];self.webpackChunkof_vue.push(chunk);const mod={exports:null};chunk[1][89668](mod,{},null);mod.exports('synthetic-static\\n1\\n/api2/v2/users/me\\n123');
  async function run(){for(let i=0;i<2;i++){await fetch('/api2/v2/users/me',{headers:{'user-id':'123','x-bc':'synthetic-bc','x-of-rev':'synthetic-rev',sign:'s:'+(i?'b':'a').repeat(40)+':123:e',time:String(Date.now())}});await new Promise(r=>setTimeout(r,100));}}run();</script>`});
 });
 await context.route('https://dashboard.tgaiclone.xyz/**',async route=>{
  if(route.request().url().endsWith('/creator-auth/platforms/onlyfans/browser')){
   submissions.push(route.request().postDataJSON());
   return route.fulfill({contentType:'application/json',body:JSON.stringify({ok:true,enrollment:{id:'synthetic-enrollment'}})});
  }
  return route.fulfill({contentType:'text/html',body:`<!doctype html><h1>Synthetic creator dashboard</h1><button id="start">Connect OnlyFans</button><p id="status"></p><script>
  window.received=[];window.addEventListener('message',e=>{if(e.source===window&&e.data?.source==='twin-connector'){received.push(e.data);document.querySelector('#status').textContent=e.data.phase||e.data.error||'ready'}});
  document.querySelector('#start').onclick=()=>window.postMessage({source:'twin-dashboard',id:crypto.randomUUID(),type:'start',profileId:'canonical-creator'},location.origin);</script>`});
 });
 const page=await context.newPage();await page.goto('https://dashboard.tgaiclone.xyz/creator/platforms');
 await page.locator('#start').click();
 await page.waitForFunction(()=>received.some(r=>r.phase==='waiting'));
 const confirm=await new Promise((resolve,reject)=>{const existing=context.pages().find(p=>p.url().startsWith('chrome-extension://')&&p.url().endsWith('/confirm.html'));if(existing)return resolve(existing);const timer=setTimeout(()=>reject(Error('Confirmation tab did not open')),15000);context.on('page',async p=>{await p.waitForLoadState('domcontentloaded').catch(()=>{});if(p.url().startsWith('chrome-extension://')&&p.url().endsWith('/confirm.html')){clearTimeout(timer);resolve(p)}})});
 await confirm.getByText('@synthetic_creator',{exact:true}).waitFor();assert.equal(submissions.length,0,'No automatic session submission');
 await confirm.getByRole('button',{name:'Connect this account',exact:true}).click();await confirm.getByText('Submitted to Twin for verification.',{exact:true}).waitFor();
 assert.equal(submissions.length,1);assert.equal(submissions[0].profileId,'canonical-creator');assert.equal(submissions[0].handle,'synthetic_creator');assert.equal(submissions[0].session.ownerId,123);assert(submissions[0].session.browser.cookies.some(c=>c.name==='sess'));
 assert(!JSON.stringify(await page.evaluate(()=>received)).includes('synthetic-session'),'Page messages must not contain cookies');
 const worker=context.serviceWorkers()[0]||await context.waitForEvent('serviceworker');const stored=await worker.evaluate(()=>chrome.storage.session.get('job'));
 assert.equal(stored.job.phase,'submitted');assert.equal(stored.job.command,undefined);assert(!JSON.stringify(stored).includes('synthetic-session'));
 console.log('PASS real Chromium extension: installed manifest, main/isolated bridges, selected-tab HttpOnly cookies, actual account confirmation, original-document submission, metadata-only page messages, success cleanup.');
}catch(error){
 const worker=context.serviceWorkers()[0];if(worker)console.log('Connector stage:',await worker.evaluate(async()=>{const {job}=await chrome.storage.session.get('job');return job?{phase:job.phase,samples:job.samples?.length,hasIdentity:!!job.identity,hasCommand:!!job.command}:null}));
 console.log('Open test pages:',context.pages().map(p=>p.url()));throw error;
}finally{await context.close();rmSync(temp,{recursive:true,force:true});}
