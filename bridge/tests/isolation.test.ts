import test from 'node:test';
import assert from 'node:assert/strict';
import {chmodSync,linkSync,mkdirSync,mkdtempSync,readFileSync,readdirSync,realpathSync,symlinkSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {SafeFiles,StatePaths} from '../src/isolation';
import {loadConfig,loadSecrets,validateConfig} from '../src/config';
import {AccountLog,Bridge,BinanceTransport,SubmissionJournal,Transport} from '../src/core';
import {createLogger} from '../vendor/logger';
import {configureRequestRuntime,BINANCE_WEIGHT_LIMIT_1M} from '../vendor/safe-request';

const config=()=>({dry_run:false,instance_id:'isolated-test',live:{enabled:true,confirmation:'ENABLE_SMALL_CAPITAL_LIVE',dedicated_account_confirmed:true,max_account_equity_usdt:500},risk:{max_loss_per_trade:5,max_margin_ratio:.2,max_leverage:5,taker_fee_rate:.0005,slippage_bps:10},execution:{leverage:5}});
function fixture(mode:'paper'|'live'='live'){
  const root=realpathSync(mkdtempSync(join(tmpdir(),'sol-isolation-')));
  writeFileSync(join(root,'isolation-policy.json'),JSON.stringify({application:'sol-ai-trading-system',policy_version:1,live_runtime_allowed:false}),{mode:0o600});
  const files=new SafeFiles(root),state=new StatePaths(files,'isolated-test',mode);
  return {root,files,state};
}
class RecordingTransport implements Transport {
  readonly kind='mock' as const;calls:any[]=[];
  async signed(method:string,path:string,params:Record<string,string>){this.calls.push({method,path,params});return {orderId:123,clientOrderId:params.origClientOrderId};}
  async public(path:string){this.calls.push({path});return {};}
}
test('dry-run blocks every private RPC with zero transport calls',async()=>{
  const {state}=fixture('paper'),c={...config(),dry_run:true},t=new RecordingTransport(),b=new Bridge(()=>c,t,new SubmissionJournal(state),new AccountLog(state));
  for(const op of ['account','positions','position_mode','open_orders','open_stops','order','query_stop','trades','place_order','cancel_order','stop','cancel_stop','leverage'])await assert.rejects(()=>b.call(op,{client_id:'solforeign'}),(e:any)=>e.code==='PRIVATE_RPC_DISABLED');
  assert.deepEqual(t.calls,[]);
});
test('production private reads and writes stay sealed despite env/config claims',async()=>{
  const oldFetch=globalThis.fetch,old=process.env.LIVE_ISOLATION_ACCEPTED;let calls=0;
  process.env.LIVE_ISOLATION_ACCEPTED='true';globalThis.fetch=async()=>{calls++;throw new Error('must never reach network');};
  try{const t=new BinanceTransport({apiKey:'sentinel',secretKey:'sentinel'},{weightLimit1m:2400});for(const method of ['GET','POST','DELETE'])await assert.rejects(()=>t.signed(method,'/fapi/v1/order',{}),(e:any)=>e.code==='LIVE_ISOLATION_NOT_ACCEPTED');assert.equal(calls,0);}
  finally{globalThis.fetch=oldFetch;if(old===undefined)delete process.env.LIVE_ISOLATION_ACCEPTED;else process.env.LIVE_ISOLATION_ACCEPTED=old;}
});
test('strict gate rejects missing fields, string booleans, string numbers and invalid YAML',()=>{
  for(const c of [{}, {...config(),dry_run:'false'}, {...config(),live:{...config().live,enabled:'true'}},{...config(),live:{...config().live,dedicated_account_confirmed:1}},{...config(),risk:{...config().risk,max_leverage:'5'}},{...config(),risk:{...config().risk,taker_fee_rate:NaN}}])assert.throws(()=>validateConfig(c),/CONFIG_INVALID/);
  const {root,files}=fixture();writeFileSync(join(root,'config.yaml'),'dry_run: true\ndry_run: false\n',{mode:0o600});assert.throws(()=>loadConfig(files));
  writeFileSync(join(root,'config.yaml'),'[unterminated',{mode:0o600});assert.throws(()=>loadConfig(files));
});
test('parent env and inherited credentials are not sources; interpolation is forbidden',()=>{
  const {root,files}=fixture(),old=process.env.BINANCE_API_KEY;process.env.BINANCE_API_KEY='HOST_SENTINEL';
  try{
    assert.deepEqual(loadSecrets(files),{});
    writeFileSync(join(root,'.env'),'BINANCE_API_KEY=OWN_SENTINEL\n',{mode:0o600});assert.equal(loadSecrets(files).BINANCE_API_KEY,'OWN_SENTINEL');
    writeFileSync(join(root,'.env'),'BINANCE_API_KEY=${BINANCE_API_KEY}\n',{mode:0o600});assert.throws(()=>loadSecrets(files),/INTERPOLATION/);
    const c=config();(c as any).timezone='${HOST_TZ}';assert.throws(()=>validateConfig(c),/interpolation/);
  }finally{if(old===undefined)delete process.env.BINANCE_API_KEY;else process.env.BINANCE_API_KEY=old;}
});
test('config/env files cannot be symlinked or hardlinked to outside state',()=>{
  for(const name of ['config.yaml','.env'])for(const kind of ['symlink','hardlink']){
    const {root,files}=fixture(),outside=join(realpathSync(mkdtempSync(join(tmpdir(),'sol-outside-'))),'secret');writeFileSync(outside,'sentinel',{mode:0o600});
    if(kind==='symlink')symlinkSync(outside,join(root,name));else linkSync(outside,join(root,name));
    assert.throws(()=>files.read(name),/symlink|hard-linked/);assert.equal(readFileSync(outside,'utf8'),'sentinel');
  }
});
test('wrong deployment owner or permissive policy marker is rejected',()=>{
  const {root}=fixture(),original=process.getuid;
  try{(process as any).getuid=()=>original!()+1;assert.throws(()=>new SafeFiles(root),/owner/);}finally{(process as any).getuid=original;}
  writeFileSync(join(root,'isolation-policy.json'),JSON.stringify({application:'sol-ai-trading-system',policy_version:1,live_runtime_allowed:true}),{mode:0o600});
  assert.throws(()=>new SafeFiles(root),/unaccepted deployment/);
});
test('path traversal, unsafe permissions and external state directory refused before writes',()=>{
  const {root,files,state}=fixture();assert.throws(()=>files.append('../outside','bad'),/traversal/);assert.throws(()=>files.append('/tmp/outside','bad'),/traversal/);
  const logDir=join(root,'logs');mkdirSync(logDir,{mode:0o700});const outside=realpathSync(mkdtempSync(join(tmpdir(),'sol-ext-')));symlinkSync(outside,join(logDir,'live'));
  assert.throws(()=>new AccountLog(state).emit('event'),/symlink/);assert.deepEqual(readdirSync(outside),[]);
  chmodSync(root,0o777);assert.throws(()=>files.read('isolation-policy.json'),/owner|writable/);chmodSync(root,0o700);
});
test('existing journals/logs without a matching identity must never be adopted',()=>{
  const {root,files}=fixture('paper');mkdirSync(join(root,'trades/live'),{recursive:true,mode:0o700});writeFileSync(join(root,'trades/live/bridge-submissions.jsonl'),'foreign-state\n',{mode:0o600});
  const before=readdirSync(join(root,'trades/live'));assert.throws(()=>new StatePaths(files,'isolated-test','live'),/unclaimed/);assert.deepEqual(readdirSync(join(root,'trades/live')),before);
});
test('paper/live state stays separate and copied foreign mode/instance is rejected',()=>{
  const {root,files,state}=fixture('paper'),live=new StatePaths(files,'isolated-test','live');assert.notEqual(state.log,live.log);assert.notEqual(state.journal,live.journal);
  writeFileSync(join(root,live.identityPath),JSON.stringify({...live.identity,instance_id:'other-instance'}),{mode:0o600});assert.throws(()=>new StatePaths(files,'isolated-test','live'),/foreign instance/);
  assert.equal(files.exists(live.journal),false);assert.equal(files.exists(live.log),false);
});
test('journal and log hardlinks cannot modify a foreign file',()=>{
  for(const name of ['journal','log'] as const){const {root,state}=fixture(),target=join(root,name==='journal'?state.journal:state.log);mkdirSync(join(root,'logs/live'),{recursive:true,mode:0o700});const outside=join(root,'outside');writeFileSync(outside,'foreign',{mode:0o600});linkSync(outside,target);assert.throws(()=>name==='journal'?new SubmissionJournal(state):new AccountLog(state).emit('event'),/journal damaged|hard-linked/);assert.equal(readFileSync(outside,'utf8'),'foreign');}
});
test('same sol prefix does not confer ownership for query or cancellation',async()=>{
  const {state}=fixture(),t=new RecordingTransport(),journal=new SubmissionJournal(state),b=new Bridge(config,t,journal,new AccountLog(state));
  for(const op of ['order','query_stop','cancel_order','cancel_stop','trades'])await assert.rejects(()=>b.call(op,{symbol:'SOLUSDT',client_id:'solforeign'}),(e:any)=>e.code==='FOREIGN_ORDER_ID');
  assert.equal(t.calls.length,0);
  journal.reserve('solownorder','order','SOLUSDT');await b.call('order',{symbol:'SOLUSDT',client_id:'solownorder'});assert.equal(t.calls[0].params.origClientOrderId,'solownorder');
  await assert.rejects(()=>b.call('cancel_stop',{symbol:'SOLUSDT',client_id:'solownorder'}),(e:any)=>e.code==='FOREIGN_ORDER_ID');
  const reloaded=new SubmissionJournal(state);assert.equal(reloaded.assertOwned('solownorder','order','SOLUSDT'),'solownorder');assert.throws(()=>reloaded.reserve('solownorder','order','SOLUSDT'),/Query the original/);
});
test('vendor logging and quota are explicit, not host environment inheritance',()=>{
  const oldLevel=process.env.LOG_LEVEL,oldQuota=process.env.BINANCE_WEIGHT_LIMIT_1M,oldLog=console.log;let emitted=0;
  process.env.LOG_LEVEL='error';process.env.BINANCE_WEIGHT_LIMIT_1M='1';console.log=()=>{emitted++;};
  try{configureRequestRuntime({weightLimit1m:1200});assert.equal(BINANCE_WEIGHT_LIMIT_1M,1200);createLogger('test',{minLevel:'info'}).info('sentinel');assert.equal(emitted,1);}finally{console.log=oldLog;configureRequestRuntime({weightLimit1m:2400});if(oldLevel===undefined)delete process.env.LOG_LEVEL;else process.env.LOG_LEVEL=oldLevel;if(oldQuota===undefined)delete process.env.BINANCE_WEIGHT_LIMIT_1M;else process.env.BINANCE_WEIGHT_LIMIT_1M=oldQuota;}
});
