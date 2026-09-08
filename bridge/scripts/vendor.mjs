/** Mechanical, audited extraction. Never load TradingSkill's daemon or database. */
import {readFileSync, writeFileSync, mkdirSync} from 'node:fs';
import {resolve, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import ts from 'typescript';
const bridge = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const root = resolve(bridge, '../..');
const sha = s => createHash('sha256').update(s).digest('hex');
const verify = process.argv.includes('--verify');
const manifestPath = resolve(bridge, 'vendor/manifest.json');
const patchText=readFileSync(resolve(bridge,'vendor/patches.json'),'utf8');
const patches=JSON.parse(patchText).patches;
if (verify) {
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  if(manifest.patch_manifest_sha256!==sha(patchText))throw Error('Patch provenance drift');
  for (const file of manifest.files) {
    if (sha(readFileSync(resolve(bridge, 'vendor', file.output))) !== file.output_sha256) throw Error(`Vendor drift: ${file.output}`);
  }
  console.log(`Verified ${manifest.files.length} isolated vendor snapshots`);
  process.exit(0);
}
mkdirSync(resolve(bridge, 'vendor'), {recursive:true});
const files = [];
function save(source, output, content, declarations = null) {
  const original = readFileSync(resolve(root, source));
  const applied=patches.filter(p=>p.file===output);
  for(const patch of applied){if(content.split(patch.before).length!==2)throw Error(`Patch base mismatch ${output}`);content=content.replace(patch.before,patch.after);}
  writeFileSync(resolve(bridge, 'vendor', output), content);
  files.push({source, output, source_sha256:sha(original), output_sha256:sha(content), declarations,patches:applied.length});
}
for (const source of ['server/exchange/safe-request.ts','server/full-margin-stop.ts','server/symbol-loss-policy.ts','server/logger.ts','server/credential-redaction.ts']) {
  save(source, source.split('/').at(-1), readFileSync(resolve(root,source),'utf8'));
}
const source='server/exchange/binance-adapter.ts';
const text=readFileSync(resolve(root,source),'utf8');
const ast=ts.createSourceFile(source,text,ts.ScriptTarget.Latest,true,ts.ScriptKind.TS);
const names = new Set(['BINANCE_FUTURES_URL','binanceIpBanUntilMs','binanceServerTimeOffsetMs','binanceServerTimeOffsetValidUntilMs','binanceTimeSyncPromise','resetBinanceTimeOffsetForTests','parseBinanceBanUntil','sign','buildSignedUrl','syncBinanceServerTime','getHeaders','request','decimalsFromIncrement','floorBinanceQuantityToStep','roundBinanceStopPriceToTick']);
const extracted=[];
for (const node of ast.statements) {
  const name=node.name?.text ?? (ts.isVariableStatement(node) ? node.declarationList.declarations[0]?.name?.text : undefined);
  if (names.has(name)) {extracted.push(node.getText(ast)); names.delete(name);}
}
if(names.size) throw Error(`Missing audited declarations: ${[...names]}`);
const header='// Generated exact declaration bodies from TradingSkill; see manifest.json.\nimport crypto from "node:crypto";\nimport {fetchWithRetry,withRetry} from "./safe-request";\ninterface ExchangeCredentials {apiKey:string;secretKey:string}\n';
save(source,'binance-transport.ts',header+extracted.join('\n\n')+'\n',extracted.map(s=>s.match(/(?:function|const|let)\s+(\w+)/)?.[1]));
writeFileSync(manifestPath,JSON.stringify({format:2,policy:'Pure source snapshots plus audited isolation-only explicit-injection patches; no original DB/server imports; refresh is explicit.',patch_manifest_sha256:sha(patchText),files},null,2)+'\n');
console.log(`Extracted ${files.length} isolated vendor snapshots`);
