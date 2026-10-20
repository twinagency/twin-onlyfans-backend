// Private, short-lived browser jobs. Canonical identity and enrollment remain
// owned by Twin's existing dashboard and native worker.
import {randomUUID,createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';

const OF='https://onlyfans.com', WIDTH=432, HEIGHT=720;
const id=v=>typeof v==='string'&&/^[A-Za-z0-9._:-]{1,64}$/.test(v);
const actor=v=>typeof v==='string'&&/^creator-session:[a-f0-9]{64}$/.test(v);
const header=v=>typeof v==='string'&&/^[\x20-\x7e]{1,4096}$/.test(v);
const uuid=v=>typeof v==='string'&&/^[a-f0-9-]{36}$/.test(v);
export const networkFingerprint=proxy=>createHash('sha256').update('twin-capture-network.v1\0'+(proxy??'')).digest('hex');
export class SignInError extends Error {constructor(code,status=409){super(code);this.code=code;this.status=status;}}
const fail=(code,status)=>{throw new SignInError(code,status);};

// The viewer permits sign-in, not arbitrary account operations or fan sends.
export function allowedRequest(request){
  let u;try{u=new URL(request.url());}catch{return false;}
  if(u.protocol!=='https:')return false;
  const of=u.hostname==='onlyfans.com'||u.hostname.endsWith('.onlyfans.com');
  const challenge=(u.hostname==='challenges.cloudflare.com'||u.hostname.endsWith('.challenges.cloudflare.com'))||u.hostname==='hcaptcha.com'||u.hostname.endsWith('.hcaptcha.com');
  const recaptcha=['www.google.com','www.gstatic.com','recaptcha.net','www.recaptcha.net'].includes(u.hostname)&&u.pathname.startsWith('/recaptcha/');
  const loginAsset=['GET','HEAD'].includes(request.method())&&((['abs-0.twimg.com','abs.twimg.com'].includes(u.hostname)&&u.pathname.startsWith('/login/'))||(u.hostname==='pbs.twimg.com'&&u.pathname.startsWith('/app_img/')));
  if(!of&&!challenge&&!recaptcha&&!loginAsset)return false;
  if(request.isNavigationRequest()&&request.frame().parentFrame()===null&&u.origin!==OF)return false;
  if(of&&!['GET','HEAD','OPTIONS'].includes(request.method())){
    let p;try{p=decodeURIComponent(u.pathname);}catch{return false;}
    if(p!=='/api2/v2/webauthn/login/options'&&!p.startsWith('/cdn-cgi/challenge-platform/')&&!/^\/api2\/v2\/(?:users\/(?:login|logout|otp|auth|2fa|two-factor)|auth|login|otp|captcha|challenge)(?:\/|$)/.test(p))return false;
  }
  return true;
}

export function createSignInService({launch,transport,now=Date.now,ttl=15*60_000,maxActive=2,interceptor=readFileSync(new URL('../loginExtension/interceptor.js',import.meta.url),'utf8')}={}){
  if(typeof launch!=='function')throw Error('A private browser launcher is required');
  if(!Number.isInteger(maxActive)||maxActive<1||maxActive>4||!Number.isFinite(ttl)||ttl<60_000||ttl>15*60_000)throw Error('Invalid sign-in capacity or lifetime');
  const jobs=new Map(),scopes=new Map(),starts=new Map();let allocation=Promise.resolve();
  const scopeKey=s=>{if(!s||!id(s.creatorId)||!actor(s.actor))fail('invalid_scope',400);return s.actor+':'+s.creatorId;};
  const view=j=>({id:j.id,phase:j.phase,handle:j.handle??null,expiresAt:new Date(j.deadline).toISOString(),enrollmentId:j.enrollmentId??null,width:WIDTH,height:HEIGHT,keyboard:j.keyboard??null});
  async function discard(j,phase){
    j.closed=true;j.phase=phase;delete j.capture;delete j.samples;delete j.identity;delete j.captureScope;
    const b=j.browser;j.browser=null;j.page=null;if(b)await b.close().catch(()=>{});
  }
  function lookup(s,identity){
    const key=scopeKey(s),j=jobs.get(identity);
    if(!j||j.scopeKey!==key)fail('attempt_unavailable',404);
    if(j.deadline<=now()){void discard(j,'expired');fail('attempt_expired',410);}
    return j;
  }
  async function observe(j,source,p){
    if(j.closed||j.deadline<=now()||!['opening','waiting'].includes(j.phase)||source.page!==j.page||source.frame.parentFrame()!==null)return;
    try{if(new URL(source.frame.url()).origin!==OF)return;}catch{return;}
    if(!p||Buffer.byteLength(JSON.stringify(p))>24000)return;
    const h=p.headers,r=p.rules;
    if(!h||!r||!['user-id','user-agent','x-bc','x-of-rev','sign','time'].every(k=>header(h[k]))||!header(r.static_param))return;
    const owner=Number(h['user-id']);if(!Number.isSafeInteger(owner)||owner<=0||String(owner)!==h['user-id']||!/^[A-Za-z0-9_-]{1,128}$/.test(h['x-of-rev']))return;
    let u;try{u=new URL(p.url,OF);}catch{return;}
    const parts=h.sign.split(':');
    if(u.origin!==OF||!u.pathname.startsWith('/api2/v2/')||u.href.length>2048||u.hash||parts.length!==4||!parts[0]||!parts[3]||!/^\d{13}$/.test(h.time)||Math.abs(now()-Number(h.time))>60_000||!/^[a-f0-9]{40}$/.test(parts[1])||!/^[a-f0-9]{1,8}$/.test(parts[2]))return;
    const key=[owner,h['x-of-rev'],h['x-bc'],r.static_param,parts[0],parts[3]].join('|');
    if(j.captureScope!==key){j.captureScope=key;j.samples=[];j.identity=null;}
    if(!j.samples.some(s=>s.sign.split(':')[1]===parts[1]))j.samples.push({url:u.href,sign:h.sign,time:h.time});
    j.samples=j.samples.slice(-4);
    if(p.identity?.id===owner&&typeof p.identity.username==='string'&&/^[A-Za-z0-9_.-]{1,80}$/.test(p.identity.username))j.identity={id:owner,username:p.identity.username};
    if(j.samples.length<2||!j.identity)return;
    if(j.network.ownerId!==null&&owner!==j.network.ownerId){await discard(j,'wrong_account');return;}
    const rows=await j.context.cookies(OF+'/');
    const cookies=rows.filter(c=>['onlyfans.com','.onlyfans.com'].includes(c.domain)&&c.path==='/'&&!c.partitionKey).map(({name,value,domain,path})=>({name,value,domain,path}));
    if(!cookies.length||cookies.length>64||new Set(cookies.map(c=>c.name)).size!==cookies.length||cookies.find(c=>c.name==='auth_id')?.value!==String(owner))return;
    if(cookies.some(c=>!header(c.value)||/[;,"\\\s]/.test(c.value)))return;
    const command={profileId:j.creatorId,handle:j.identity.username,session:{ownerId:owner,expiresAt:new Date(now()+6*3600_000).toISOString(),networkFingerprint:j.network.fingerprint,browser:{schemaVersion:'of-browser.v1',headers:{user_id:String(owner),user_agent:h['user-agent'],x_bc:h['x-bc'],x_of_rev:h['x-of-rev'],x_hash:header(h['x-hash'])?h['x-hash']:null},cookies,staticParam:r.static_param,samples:j.samples}}};
    if(Buffer.byteLength(JSON.stringify(command))>110000){await discard(j,'failed');return;}
    if(j.closed||j.deadline<=now())return;
    j.capture=command;j.handle=j.identity.username;j.phase='ready';
    // Capture has finished. The browser cannot be used to browse or message.
    const browser=j.browser;j.browser=null;j.page=null;await browser?.close().catch(()=>{});
    delete j.samples;delete j.identity;delete j.captureScope;
  }
  async function boot(j){
    try{
      const browser=await launch(j.network.proxyUrl);
      if(j.closed||j.deadline<=now()){await browser.close();return;}
      j.browser=browser;
      browser.on('disconnected',()=>{if(!j.closed&&!['ready','submitted'].includes(j.phase))void discard(j,'failed');});
      j.context=await browser.newContext({viewport:{width:WIDTH,height:HEIGHT},deviceScaleFactor:2,acceptDownloads:false,serviceWorkers:'block'});
      j.context.on('page',p=>{if(j.page&&p!==j.page)void p.close().catch(()=>{});});
      await j.context.route('**/*',async route=>{
        if(!allowedRequest(route.request()))return route.abort('blockedbyclient');
        return transport?transport(route,j.context):route.continue();
      });
      j.observations=Promise.resolve();
      await j.context.exposeBinding('__twinSignInObservation',(source,p)=>{
        j.observations=j.observations.then(()=>observe(j,source,p)).catch(()=>discard(j,'failed'));
        // Do not await browser.close from a callback running in that browser.
      });
      const bridge=`window.addEventListener('message',e=>{if(e.source===window&&e.origin===location.origin&&e.data?.__chatterly_capture===true)void window.__twinSignInObservation(e.data.payload);});\n`;
      await j.context.addInitScript({content:bridge+interceptor});
      j.page=await j.context.newPage();
      if(j.closed){await browser.close();return;}
      await j.page.goto(OF+'/my/profile',{waitUntil:'domcontentloaded',timeout:30000});if(j.phase==='opening')j.phase='waiting';
    }catch{if(!j.closed&&!['ready','submitted'].includes(j.phase))await discard(j,'failed');}
  }
  function sequential(j,fn){const p=(j.operation??Promise.resolve()).then(fn);j.operation=p.catch(()=>{});return p;}
  const api={
    async start(s,{startKey,network}){
      const operation=allocation.then(async()=>{
        const key=scopeKey(s);
        if(!uuid(startKey)||!network||Object.keys(network).sort().join(',')!=='accountId,fingerprint,ownerId,proxyUrl'||!(network.accountId===null||id(network.accountId))||!(network.ownerId===null||Number.isSafeInteger(network.ownerId)&&network.ownerId>0)||!(network.proxyUrl===null||header(network.proxyUrl))||network.fingerprint!==networkFingerprint(network.proxyUrl))fail('invalid_start',400);
        if(network.proxyUrl!==null){let u;try{u=new URL(network.proxyUrl);}catch{fail('invalid_network',400);}if(!['http:','https:','socks5:'].includes(u.protocol)||!u.hostname||u.hash)fail('invalid_network',400);}
        const prior=jobs.get(scopes.get(key));
        if(prior&&prior.deadline>now()&&prior.network.accountId===network.accountId&&prior.network.fingerprint===network.fingerprint&&(['opening','waiting','ready'].includes(prior.phase)||prior.phase==='submitted'&&prior.startKey===startKey))return view(prior);
        if(prior&&!prior.closed)await discard(prior,'cancelled');
        const recent=(starts.get(key)??[]).filter(t=>t>now()-3600_000);
        if(recent.length>=6)fail('start_limit',429);
        if([...jobs.values()].filter(j=>!j.closed&&j.deadline>now()&&['opening','waiting','ready'].includes(j.phase)).length>=maxActive)fail('capacity',429);
        const j={...s,scopeKey:key,id:randomUUID(),startKey,network:structuredClone(network),deadline:now()+ttl,phase:'opening',closed:false};
        jobs.set(j.id,j);scopes.set(key,j.id);starts.set(key,[...recent,now()]);
        j.launching=boot(j);return view(j);
      });
      allocation=operation.catch(()=>{});return operation;
    },
    status(s,identity){return view(lookup(s,identity));},
    async frame(s,identity,{format='jpeg'}={}){if(!['jpeg','png'].includes(format))fail('invalid_frame_format',400);const j=lookup(s,identity);return sequential(j,async()=>{
      if(j.closed||j.phase!=='waiting'||!j.page)fail('frame_unavailable',409);
      if(now()-(j.lastFrame??0)<350)fail('frame_limit',429);j.lastFrame=now();
      const frame=await j.page.screenshot({type:format,...(format==='jpeg'?{quality:95}:{}),timeout:3000});
      if(frame.length>2*1024*1024)fail('frame_unavailable',409);return frame;
    });},
    async input(s,identity,event){const j=lookup(s,identity);return sequential(j,async()=>{
      if(j.closed||j.phase!=='waiting'||!j.page)fail('input_unavailable',409);
      const keys=Object.keys(event??{}).sort().join(',');
      if(event?.type==='click'&&keys==='type,x,y'&&[event.x,event.y].every(n=>typeof n==='number'&&Number.isFinite(n)&&n>=0&&n<=1))await j.page.mouse.click(event.x*WIDTH,event.y*HEIGHT);
      else if(event?.type==='login'&&keys==='email,password,type'&&typeof event.email==='string'&&typeof event.password==='string'&&/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(event.email)&&Buffer.byteLength(event.email)<=320&&event.password.length>0&&Buffer.byteLength(event.password)<=4096&&!/[\x00-\x1f]/.test(event.email+event.password)){
        // Values live only in this temporary provider page; never in job/capture metadata.
        const email=j.page.locator('input[name="email"],input[type="email"]').first();
        const password=j.page.locator('input[name="password"],input[type="password"]').first();
        await email.fill(event.email);await password.fill(event.password);await password.press('Enter');
      }
      else if(event?.type==='text'&&keys==='text,type'&&typeof event.text==='string'&&event.text.length>0&&Buffer.byteLength(event.text)<=512&&!/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(event.text))await j.page.keyboard.insertText(event.text);
      else if(event?.type==='key'&&keys==='key,type'&&['Enter','Tab','Backspace','Delete','Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(event.key))await j.page.keyboard.press(event.key);
      else if(event?.type==='scroll'&&['delta,type','delta,type,x,y'].includes(keys)&&typeof event.delta==='number'&&Number.isFinite(event.delta)&&Math.abs(event.delta)<=1000&&(keys==='delta,type'||[event.x,event.y].every(n=>typeof n==='number'&&Number.isFinite(n)&&n>=0&&n<=1))){if(keys==='delta,type,x,y')await j.page.mouse.move(event.x*WIDTH,event.y*HEIGHT);await j.page.mouse.wheel(0,event.delta);}
      else fail('invalid_input',400);
      if(j.page&&j.phase==='waiting')j.keyboard=await j.page.evaluate(()=>{const el=document.activeElement;if(!el||!['INPUT','TEXTAREA'].includes(el.tagName))return null;return el.getAttribute('type')==='password'?'password':el.getAttribute('type')==='email'?'email':'text';}).catch(()=>null);
      return view(j);
    });},
    capture(s,identity){const j=lookup(s,identity);if(j.closed||j.phase!=='ready'||!j.capture)fail('capture_unavailable',409);return structuredClone(j.capture);},
    async acknowledge(s,identity,enrollmentId){const j=lookup(s,identity);if(!uuid(enrollmentId))fail('invalid_enrollment',400);if(j.phase==='submitted'&&j.enrollmentId===enrollmentId)return view(j);if(j.phase!=='ready'||!j.capture)fail('capture_unavailable',409);j.enrollmentId=enrollmentId;await discard(j,'submitted');return view(j);},
    async cancel(s,identity){const j=lookup(s,identity);await discard(j,'cancelled');return view(j);},
    async collect(){for(const [identity,j]of jobs){if(j.deadline<=now()){await discard(j,'expired');jobs.delete(identity);if(scopes.get(j.scopeKey)===identity)scopes.delete(j.scopeKey);}}for(const [key,times]of starts){const recent=times.filter(t=>t>now()-3600_000);if(recent.length)starts.set(key,recent);else starts.delete(key);}},
    async close(){await Promise.allSettled([...jobs.values()].map(async j=>{await discard(j,'cancelled');await j.launching;await discard(j,'cancelled');}));jobs.clear();scopes.clear();}
  };
  return api;
}
