const fs = require("node:fs"), path = require("node:path");
const ts = require("/workspace/node_modules/typescript");
const root = "/workspace", out = process.env.TSC_SHARD_OUTPUT || "/evidence/tsc-shards";
if (fs.existsSync(out) && fs.readdirSync(out).length) throw new Error("Refusing to overwrite existing shard evidence");
fs.mkdirSync(out, { recursive: true });
const paths = [];
function walk(dir) {
  for (const item of fs.readdirSync(dir, { withFileTypes: true })) {
    if (item.isDirectory() && ["node_modules", ".git", ".next"].includes(item.name)) continue;
    const file = path.join(dir, item.name);
    if (item.isDirectory()) walk(file);
    else if (item.isFile() && /\.(?:ts|tsx)$/.test(item.name)) paths.push(file);
  }
}
walk(root); paths.sort();
const globals = [], modules = [];
for (const file of paths) {
  const text = fs.readFileSync(file, "utf8");
  const kind = file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, false, kind);
  if (file.endsWith(".d.ts") || !ts.isExternalModule(source) || /\bdeclare\s+global\b/.test(text)) globals.push(file);
  else modules.push(file);
}
const size = Number(process.env.TSC_SHARD_SIZE || "100"), groups = [];
if (!Number.isInteger(size) || size < 1 || size > 200) throw new Error("Invalid shard size");
for (let i=0; i<modules.length; i+=size) {
  const files = [...globals, ...modules.slice(i,i+size), "/evidence/dashboard-css-modules.d.ts", "/evidence/dashboard-next-env.d.ts"];
  const name = `part-${String(groups.length).padStart(2,"0")}.json`;
  fs.writeFileSync(path.join(out,name), JSON.stringify({ extends:"/workspace/tsconfig.json", compilerOptions:{noEmit:true,incremental:false}, files, include:[] },null,2));
  groups.push({name, moduleRoots:Math.min(size,modules.length-i), files:files.length});
}
fs.writeFileSync(path.join(out,"manifest.json"), JSON.stringify({sourceRoots:paths.length, sharedGlobals:globals.length, moduleRoots:modules.length, shardSize:size, groups},null,2));
console.log(`Prepared ${groups.length} bounded checks for ${paths.length} source roots, including ${globals.length} shared global declarations.`);
