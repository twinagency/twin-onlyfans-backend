// Build the small, dependency-free extension package from its single source.
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {dirname,resolve} from 'node:path';
const destination=process.argv[2];
if(!destination)throw new Error('Usage: node scripts/build-twin-connector.mjs OUTPUT.zip');
const files=['manifest.json','background.js','of-bridge.js','dashboard-bridge.js','confirm.html','confirm.js','confirm.css'];
const source=files.map(name=>[name,readFileSync(resolve('loginExtension/twin',name))]);
source.push(['interceptor.js',readFileSync('loginExtension/interceptor.js')]);
source.push(['README.txt',Buffer.from('Twin OnlyFans Connect 1.0.0\n\nExtract this ZIP. In desktop Chrome or Edge, open Extensions, turn on Developer mode, choose Load unpacked and select this folder. Refresh https://dashboard.tgaiclone.xyz/creator/platforms and select Connect OnlyFans. Sign in on OnlyFans and confirm the detected account in Twin Connect.\n\nThis private pilot is not a Chrome Web Store listing. It requires desktop installation; it does not support iPhone. No password is sent to Twin. The extension sends the chosen login session only after confirmation. Pending data is held in browser session memory for at most 15 minutes and cleared on success, cancellation, expiry, or browser restart. A connection never enables sending.\n')]);
function crc(buffer){let v=0xffffffff;for(const b of buffer){v^=b;for(let i=0;i<8;i++)v=(v>>>1)^((v&1)?0xedb88320:0);}return (v^0xffffffff)>>>0;}
const local=[],central=[];let offset=0;
for(const [name,body] of source){const filename=Buffer.from(name),sum=crc(body),h=Buffer.alloc(30);h.writeUInt32LE(0x04034b50);h.writeUInt16LE(20,4);h.writeUInt16LE(0x21,12);h.writeUInt32LE(sum,14);h.writeUInt32LE(body.length,18);h.writeUInt32LE(body.length,22);h.writeUInt16LE(filename.length,26);local.push(h,filename,body);
 const c=Buffer.alloc(46);c.writeUInt32LE(0x02014b50);c.writeUInt16LE(20,4);c.writeUInt16LE(20,6);c.writeUInt16LE(0x21,14);c.writeUInt32LE(sum,16);c.writeUInt32LE(body.length,20);c.writeUInt32LE(body.length,24);c.writeUInt16LE(filename.length,28);c.writeUInt32LE(offset,42);central.push(c,filename);offset+=h.length+filename.length+body.length;}
const directory=Buffer.concat(central),end=Buffer.alloc(22);end.writeUInt32LE(0x06054b50);end.writeUInt16LE(source.length,8);end.writeUInt16LE(source.length,10);end.writeUInt32LE(directory.length,12);end.writeUInt32LE(offset,16);
mkdirSync(dirname(resolve(destination)),{recursive:true});writeFileSync(destination,Buffer.concat([...local,directory,end]));console.log('Built Twin Connect 1.0.0: '+source.length+' files');
