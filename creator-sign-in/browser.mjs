import {mkdtemp,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {chromium} from 'playwright';

// Chromium's crash handler also needs a writable config directory. Keep it
// per-browser and temporary, without passing the service credential to Chrome.
export async function launchSignInBrowser(proxyUrl,{headless=false,chromiumSandbox=true,executablePath}={}){
  const directory=await mkdtemp(join(tmpdir(),'twin-sign-in-'));
  const cleanup=()=>rm(directory,{recursive:true,force:true}).catch(()=>{});
  const env={};
  for(const key of ['PATH','DISPLAY','XAUTHORITY','LANG','LC_ALL','TZ','SYSTEMROOT','WINDIR','TEMP','TMP','PATHEXT','COMSPEC','USERPROFILE','LOCALAPPDATA'])if(process.env[key])env[key]=process.env[key];
  env.XDG_CONFIG_HOME=join(directory,'config');env.XDG_CACHE_HOME=join(directory,'cache');
  try{
    let proxy;
    if(proxyUrl){const u=new URL(proxyUrl);proxy={server:u.protocol+'//'+u.host,...(u.username?{username:decodeURIComponent(u.username)}:{}),...(u.password?{password:decodeURIComponent(u.password)}:{})};}
    const browser=await chromium.launch({headless,chromiumSandbox,env,proxy,...(executablePath?{executablePath}:{})});
    browser.once('disconnected',()=>void cleanup());
    const close=browser.close.bind(browser);
    browser.close=async(...args)=>{try{await close(...args);}finally{await cleanup();}};
    return browser;
  }catch(error){await cleanup();throw error;}
}
