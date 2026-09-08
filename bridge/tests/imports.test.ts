import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
test('strict AST dependency validator rejects dynamic/external/parent imports and env access',()=>{
  const script=`import assert from 'node:assert/strict'; import {allowedInput,inspectSyntax,verifyGraph} from './scripts/check-imports.mjs';
    for(const code of ['import("../server/db")','require("mysql2")','eval("bad")','new Function("bad")','process.env.LOG_LEVEL','process["env"].TOKEN','const loader=globalThis["require"];loader("old")','const p=process;p.env.TOKEN'])assert.throws(()=>inspectSyntax(code));
    for(const name of ['../server/db.ts','server/db.ts','node_modules/mysql2/index.js','node_modules/yaml/browser/../../db.js','src/unreviewed.ts'])assert.equal(allowedInput(name),false);
    const graph=verifyGraph(process.cwd());assert(graph.every(allowedInput)); console.log('Strict import rejection checks passed');`;
  const output=execFileSync(process.execPath,['--input-type=module','-e',script],{cwd:process.cwd(),encoding:'utf8'});assert.match(output,/passed/);
});
