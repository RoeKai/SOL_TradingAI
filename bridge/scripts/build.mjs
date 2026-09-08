import {build} from 'esbuild';
import {execFileSync} from 'node:child_process';
import {writeFileSync,readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {resolve} from 'node:path';
import {allowedInput,builtins,inspectSyntax,verifyGraph} from './check-imports.mjs';
execFileSync(process.execPath,['scripts/vendor.mjs','--verify'],{stdio:'inherit'});
const graph=verifyGraph(resolve('.'));
const result=await build({entryPoints:['src/server.ts'],outfile:'dist/server.mjs',platform:'node',target:'node22',format:'esm',bundle:true,metafile:true});
const inputs=Object.keys(result.metafile.inputs);
if(inputs.some(x=>!allowedInput(x)))throw Error('Bundled input outside strict allowlist');
const bundle=readFileSync('dist/server.mjs','utf8');
if(inspectSyntax(bundle,'dist/server.mjs').some(x=>!builtins.has(x)))throw Error('Unbundled external import');
const sha=text=>createHash('sha256').update(text).digest('hex');
writeFileSync('dist/import-manifest.json',JSON.stringify({policy:'strict-module-and-yaml-browser-esm-allowlist',inputs,graph,input_sha256:Object.fromEntries(inputs.map(x=>[x,sha(readFileSync(x))])),bundle_sha256:sha(bundle)},null,2)+'\n');
console.log(`Built isolated bridge: ${inputs.length} inputs; no original trading runtime or DB`);
// Import only: validates bundled CommonJS built-ins without listening or calling an exchange.
execFileSync(process.execPath,['--input-type=module','-e','await import("./dist/server.mjs"); console.log("Standalone bundle import passed")'],{stdio:'inherit'});
