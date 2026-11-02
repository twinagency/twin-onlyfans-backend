// Execute only the pure label function extracted from the actual running bundle.
const fs=require('fs'),crypto=require('crypto'),assert=require('assert');let result;
function walk(p){for(const f of fs.readdirSync(p,{withFileTypes:true})){const q=p+'/'+f.name;if(f.isDirectory())walk(q);else if(f.name.endsWith('.js')){
 const s=fs.readFileSync(q,'utf8'),i=s.indexOf('Last import complete');if(i<0)continue;
 const start=s.lastIndexOf('function ',i),end=s.indexOf('}function',i)+1;
 const pure=s.slice(start,end).replace(/\\"/g,'"');
 assert(/^function \w+\(e,t\)\{return e\?/.test(pure));
 assert(!/require|fetch|process|global|eval/.test(pure));
 const label=Function('return ('+pure+')')();
 const complete={lastCompleteSyncAt:'2026-11-02T00:00:00Z',recoveryRequired:false,discoveryStatus:'complete',conversationsRemaining:0,pendingMessages:0,quarantinedMessages:0};
 assert.equal(label(complete,'connected'),'Last import complete');
 assert.equal(label({...complete,lastCompleteSyncAt:null,recoveryRequired:true},'connected'),'Importing history');
 result={file:q,sha256:crypto.createHash('sha256').update(s).digest('hex'),passed:2};
}}}walk('/app/.next/static');assert(result,'Actual dashboard label function unavailable');console.log(JSON.stringify(result));
