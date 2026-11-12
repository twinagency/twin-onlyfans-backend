import {createServer} from 'node:http';
import {timingSafeEqual} from 'node:crypto';
import {createSignInService,SignInError} from './service.mjs';
import {launchSignInBrowser} from './browser.mjs';

const token=process.env.ONLYFANS_SIGNIN_TOKEN??'';
if(token.length<32)throw Error('A separate private sign-in credential is required');
async function beforeLaunch(network,scope){
  if(network.accountId!==null)return;
  const root=process.env.ONLYFANS_MANAGED_PROXY_BASE_URL,credential=process.env.ONLYFANS_MANAGED_PROXY_TOKEN;
  if(!root||!credential||credential.length<32)throw Error('network_guard_unavailable');
  const response=await fetch(root.replace(/\/+$/,'')+'/v1/begin-use',{
    method:'POST',headers:{Authorization:'Bearer '+credential,'Content-Type':'application/json'},
    body:JSON.stringify({...scope,proxyUrl:network.proxyUrl}),signal:AbortSignal.timeout(8000)});
  const result=await response.json();
  if(!response.ok||result.ok!==true)throw Error('network_reservation_changed');
}
const service=createSignInService({beforeLaunch,maxActive:Number(process.env.ONLYFANS_SIGNIN_MAX_BROWSERS??2),idleTtl:Number(process.env.ONLYFANS_SIGNIN_IDLE_SECONDS??300)*1000,launch:launchSignInBrowser});
const authorized=req=>{const supplied=Buffer.from(req.headers.authorization??''),expected=Buffer.from('Bearer '+token);return supplied.length===expected.length&&timingSafeEqual(supplied,expected);};
async function body(req){let value='';for await(const part of req){value+=part;if(Buffer.byteLength(value)>16384)throw new SignInError('request_too_large',413);}try{return JSON.parse(value);}catch{throw new SignInError('invalid_request',400);}}
const server=createServer(async(req,res)=>{
  res.setHeader('Cache-Control','private, no-store');res.setHeader('X-Content-Type-Options','nosniff');
  const json=(value,status=200)=>{res.statusCode=status;res.setHeader('Content-Type','application/json');res.end(JSON.stringify(value));};
  try{
    const url=new URL(req.url,'http://private');
    if(url.pathname==='/health'&&req.method==='GET')return json({ok:true});
    if(!authorized(req))return json({ok:false,error:'unauthorized'},401);
    const scope={creatorId:req.headers['x-twin-creator'],actor:req.headers['x-twin-actor']};
    if(url.pathname==='/v1/attempts'&&req.method==='POST'){const b=await body(req);if(Object.keys(b).sort().join(',')!=='network,startKey')throw new SignInError('invalid_request',400);return json({ok:true,attempt:await service.start(scope,b)});}
    const match=url.pathname.match(/^\/v1\/attempts\/([a-f0-9-]{36})(?:\/(frame|input|capture|acknowledge|cancel))?$/);
    if(!match)return json({ok:false,error:'not_found'},404);
    const [,identity,action]=match;
    if(!action&&req.method==='GET')return json({ok:true,attempt:service.status(scope,identity),detail:service.detail(scope,identity)});
    if(action==='frame'&&req.method==='GET'){const format=url.searchParams.get('format')??'jpeg',frame=await service.frame(scope,identity,{format});res.setHeader('Content-Type',format==='png'?'image/png':'image/jpeg');return res.end(frame);}
    if(action==='capture'&&req.method==='GET')return json({ok:true,capture:service.capture(scope,identity)});
    if(action==='input'&&req.method==='POST')return json({ok:true,attempt:await service.input(scope,identity,await body(req))});
    if(action==='cancel'&&req.method==='POST'){const b=await body(req);if(Object.keys(b).length)throw new SignInError('invalid_request',400);return json({ok:true,attempt:await service.cancel(scope,identity)});}
    if(action==='acknowledge'&&req.method==='POST'){const b=await body(req);if(Object.keys(b).join(',')!=='enrollmentId')throw new SignInError('invalid_request',400);return json({ok:true,attempt:await service.acknowledge(scope,identity,b.enrollmentId)});}
    return json({ok:false,error:'not_found'},404);
  }catch(error){const known=error instanceof SignInError;if(known&&error.retryAfter)res.setHeader('Retry-After',String(error.retryAfter));json({ok:false,error:known?error.code:'connection_unavailable',...(known&&error.retryAfter?{retryAfter:error.retryAfter}:{})},known?error.status:503);}
});
server.requestTimeout=15000;server.headersTimeout=10000;
const timer=setInterval(()=>void service.collect(),10000);timer.unref();
// Loopback unless HOST opts in (Docker: container-private network, compose `expose` only).
server.listen(Number(process.env.PORT??8479),process.env.HOST??'127.0.0.1',()=>console.log('Private creator sign-in ready'));
for(const signal of ['SIGTERM','SIGINT'])process.on(signal,()=>{clearInterval(timer);server.close();void service.close().finally(()=>process.exit(0));});
