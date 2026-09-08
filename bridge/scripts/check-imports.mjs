import {readFileSync,realpathSync,lstatSync,existsSync} from 'node:fs';
import {resolve,dirname,relative} from 'node:path';
import ts from 'typescript';
const sources=new Set(['src/server.ts','src/core.ts','src/config.ts','src/isolation.ts']);
const vendors=new Set(['vendor/binance-transport.ts','vendor/safe-request.ts','vendor/symbol-loss-policy.ts','vendor/logger.ts','vendor/credential-redaction.ts','vendor/full-margin-stop.ts']);
export const builtins=new Set(['node:fs','node:path','node:url','node:http','node:crypto']);
export function allowedInput(name){return sources.has(name)||vendors.has(name)||(!name.split('/').includes('..')&&/^node_modules\/yaml\/browser\/(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_-]+\.js$/.test(name));}
export function inspectSyntax(text,label='source'){
  const ast=ts.createSourceFile(label,text,ts.ScriptTarget.Latest,true,label.endsWith('.ts')?ts.ScriptKind.TS:ts.ScriptKind.JS),imports=[];
  function walk(node){
    if(ts.isIdentifier(node)&&['globalThis','require','eval','Function','createRequire'].includes(node.text))throw Error(`Dynamic loader capability forbidden in ${label}`);
    if(ts.isIdentifier(node)&&node.text==='process'){
      const parent=node.parent;
      if(!ts.isPropertyAccessExpression(parent)||parent.expression!==node||!['getuid','kill','pid','on','argv','exit','exitCode'].includes(parent.name.text))throw Error(`Implicit process capability forbidden in ${label}`);
    }
    if((ts.isImportDeclaration(node)||ts.isExportDeclaration(node))&&node.moduleSpecifier){
      if(!ts.isStringLiteral(node.moduleSpecifier))throw Error(`Nonliteral import in ${label}`);imports.push(node.moduleSpecifier.text);
    }
    if(ts.isCallExpression(node)){
      const e=node.expression;
      if(e.kind===ts.SyntaxKind.ImportKeyword||(ts.isIdentifier(e)&&['require','eval','Function','createRequire'].includes(e.text))||(ts.isPropertyAccessExpression(e)&&['require','eval'].includes(e.name.text)))throw Error(`Dynamic loader forbidden in ${label}`);
    }
    if(ts.isNewExpression(node)&&ts.isIdentifier(node.expression)&&node.expression.text==='Function')throw Error(`Dynamic code forbidden in ${label}`);
    if((ts.isPropertyAccessExpression(node)&&node.name.text==='env')||(ts.isElementAccessExpression(node)&&ts.isStringLiteral(node.argumentExpression)&&node.argumentExpression.text==='env'))throw Error(`Implicit environment access forbidden in ${label}`);
    ts.forEachChild(node,walk);
  }
  walk(ast);return imports;
}
export function verifyGraph(root,entry='src/server.ts'){
  const visited=new Set();
  function visit(name){
    if(!allowedInput(name))throw Error(`Import outside strict module allowlist: ${name}`);
    if(visited.has(name))return;visited.add(name);
    const path=resolve(root,name),st=lstatSync(path);
    if(st.isSymbolicLink()||st.nlink!==1||!st.isFile()||realpathSync(path)!==path)throw Error(`Unsafe source path: ${name}`);
    for(const spec of inspectSyntax(readFileSync(path,'utf8'),name)){
      if(builtins.has(spec))continue;
      if(!spec.startsWith('.'))throw Error(`External package/import not allowlisted: ${spec}`);
      let target=resolve(dirname(path),spec);if(!existsSync(target)){if(existsSync(target+'.ts'))target+='.ts';else if(existsSync(target+'.js'))target+='.js';}
      visit(relative(root,target).replaceAll('\\','/'));
    }
  }
  visit(entry);return [...visited].sort();
}
