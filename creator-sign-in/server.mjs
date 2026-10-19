import {createServer} from 'node:http';
import {timingSafeEqual} from 'node:crypto';
import {chromium} from 'playwright';
import {createSignInService,SignInError} from './service.mjs';

const token=process.env.ONLYFANS_SIGNIN_TOKEN??'';
if(token.length<32)throw Error('A separate private sign-in credential is required');
const service=createSignInService({maxActive:Number(process.env.ONLYFANS_SIGNIN_MAX_BROWSERS??2),launch:async proxyUrl=>{
  let proxy;
  if(proxyUrl){const u=new URL(proxyUrl);proxy={server:u.protocol+'//'+u.host,...(u.username?{username:decodeURIComponent(u.username)}:{}),...(u.password?{password:decodeURIComponent(u.password)}:{})};}
  return chromium.launch({headless:false,chromiumSandbox:true,proxy});
}});
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
    if(!action&&req.method==='GET')return json({ok:true,attempt:service.status(scope,identity)});
    if(action==='frame'&&req.method==='GET'){const frame=await service.frame(scope,identity);res.setHeader('Content-Type','image/jpeg');return res.end(frame);}
    if(action==='capture'&&req.method==='GET')return json({ok:true,capture:service.capture(scope,identity)});
    if(action==='input'&&req.method==='POST')return json({ok:true,attempt:await service.input(scope,identity,await body(req))});
    if(action==='cancel'&&req.method==='POST'){const b=await body(req);if(Object.keys(b).length)throw new SignInError('invalid_request',400);return json({ok:true,attempt:await service.cancel(scope,identity)});}
    if(action==='acknowledge'&&req.method==='POST'){const b=await body(req);if(Object.keys(b).join(',')!=='enrollmentId')throw new SignInError('invalid_request',400);return json({ok:true,attempt:await service.acknowledge(scope,identity,b.enrollmentId)});}
    return json({ok:false,error:'not_found'},404);
  }catch(error){json({ok:false,error:error instanceof SignInError?error.code:'connection_unavailable'},error instanceof SignInError?error.status:503);}
});
server.requestTimeout=15000;server.headersTimeout=10000;
const timer=setInterval(()=>void service.collect(),10000);timer.unref();
server.listen(Number(process.env.PORT??8479),'0.0.0.0',()=>console.log('Private creator sign-in ready'));
for(const signal of ['SIGTERM','SIGINT'])process.on(signal,()=>{clearInterval(timer);server.close();void service.close().finally(()=>process.exit(0));});
