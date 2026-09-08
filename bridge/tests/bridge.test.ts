import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,readFileSync,writeFileSync,realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {AccountLog,BinanceTransport,Bridge,BridgeConfig,SubmissionJournal,assertWriteEnabled,safeError,Transport} from '../src/core';
import {serverFor} from '../src/server';
import {SafeFiles,StatePaths} from '../src/isolation';

const live:BridgeConfig={dry_run:false,instance_id:'sol-test',execution:{leverage:5},live:{enabled:true,confirmation:'ENABLE_SMALL_CAPITAL_LIVE',dedicated_account_confirmed:true,max_account_equity_usdt:500},risk:{max_loss_per_trade:5,max_leverage:5,max_margin_ratio:.2,taker_fee_rate:.0005,slippage_bps:10}};
const info={symbols:[{symbol:'SOLUSDT',status:'TRADING',contractType:'PERPETUAL',filters:[{filterType:'LOT_SIZE',minQty:'0.001',maxQty:'10000',stepSize:'0.001'},{filterType:'MARKET_LOT_SIZE',minQty:'0.001',maxQty:'10000',stepSize:'0.001'},{filterType:'MIN_NOTIONAL',notional:'5'},{filterType:'PRICE_FILTER',tickSize:'0.01'}]}]};
class FakeTransport implements Transport {
  readonly kind='mock' as const;
  writes:any[]=[];position:any[]=[];failure:Error|undefined;equity=100;orders:any[]=[];stopOrders:any[]=[];
  async public(path:string){return path.includes('premiumIndex')?{markPrice:'100'}:info;}
  async signed(method:string,path:string,params:Record<string,string>,beforeWrite?:()=>Promise<void>){
    if(method!=='GET'){await beforeWrite?.();this.writes.push({method,path,params});if(this.failure)throw this.failure;return path.includes('algo')?{algoId:2,algoStatus:'NEW'}:{orderId:1,status:'FILLED'};}
    if(path.includes('positionRisk'))return this.position;
    if(path.includes('positionSide'))return {dualSidePosition:false};
    if(path.endsWith('openOrders'))return this.orders;
    if(path.endsWith('openAlgoOrders'))return this.stopOrders;
    if(path.endsWith('account'))return {totalMarginBalance:String(this.equity),availableBalance:String(this.equity)};
    if(path.endsWith('symbolConfig'))return [{symbol:'SOLUSDT',leverage:5}];
    return {orderId:1,status:'FILLED'};
  }
}
function setup(config:any=live){const root=realpathSync(mkdtempSync(join(tmpdir(),'sol-bridge-test-')));writeFileSync(join(root,'isolation-policy.json'),JSON.stringify({application:'sol-ai-trading-system',policy_version:1,live_runtime_allowed:false}),{mode:0o600});const state=new StatePaths(new SafeFiles(root),config.instance_id??'sol-test',config.dry_run===false?'live':'paper');const transport=new FakeTransport(),journal=new SubmissionJournal(state),log=new AccountLog(state,['secret-value']);return {bridge:new Bridge(()=>config,transport,journal,log),transport,journal,root,state};}
const entry=()=>({symbol:'SOLUSDT',side:'BUY',type:'MARKET',quantity:0.1,client_id:'solentry001',risk_context:{entry_price:100,stop_price:95,max_loss_usdt:5,equity:100,leverage:5}});
test('default and partial live config cannot write',async()=>{
  for(const config of [{},{dry_run:true},{dry_run:false}, {...live,dry_run:true},{...live,live:{...live.live,dedicated_account_confirmed:false}}]){
    assert.throws(()=>assertWriteEnabled(config as BridgeConfig));
    const {bridge,transport}=setup(config);await assert.rejects(()=>bridge.call('place_order',entry()));assert.equal(transport.writes.length,0);
  }
});
test('mandatory stop, risk caps and rounding reject before POST',async()=>{
  const cases=[{risk_context:{...entry().risk_context,stop_price:0}},{risk_context:{...entry().risk_context,stop_price:101}},{quantity:10},{quantity:.1001},{risk_context:{...entry().risk_context,leverage:10}},{quantity:.001}];
  for(const delta of cases){const {bridge,transport}=setup();await assert.rejects(()=>bridge.call('place_order',{...entry(),...delta}));assert.equal(transport.writes.length,0);}
});
test('valid entry reuses transport and journal prevents repeated POST across restart',async()=>{
  const {bridge,transport,state}=setup();assert.equal((await bridge.call('place_order',entry())).orderId,1);
  await assert.rejects(()=>bridge.call('place_order',entry()),/Reconcile/);
  const reloaded=new SubmissionJournal(state);assert.equal(reloaded.has(entry().client_id),true);assert.equal(transport.writes.length,1);
});
test('ambiguous entry is UNKNOWN, never blindly retried',async()=>{
  const {bridge,transport}=setup();transport.failure=new Error('network fetch failed');
  await assert.rejects(()=>bridge.call('place_order',entry()),(e:any)=>e.ambiguous===true&&e.code==='UNKNOWN');
  await assert.rejects(()=>bridge.call('place_order',entry()));assert.equal(transport.writes.length,1);
  assert.equal((await bridge.call('order',{symbol:'SOLUSDT',client_id:entry().client_id})).orderId,1);
});
test('exact dust reduceOnly exit bypasses entry minimum without uplift',async()=>{
  const {bridge,transport}=setup();transport.position=[{symbol:'SOLUSDT',positionSide:'BOTH',positionAmt:'.001',markPrice:'100'}];
  await bridge.call('place_order',{symbol:'SOLUSDT',side:'SELL',type:'MARKET',quantity:.001,client_id:'soldust001',reduce_only:true});
  assert.equal(transport.writes[0].params.quantity,'0.001');assert.equal(transport.writes[0].params.reduceOnly,'true');
});
test('exits cannot touch opposite positions or increase exposure',async()=>{
  const {bridge,transport}=setup();transport.position=[{symbol:'SOLUSDT',positionSide:'BOTH',positionAmt:'.001'}];
  await assert.rejects(()=>bridge.call('place_order',{...entry(),side:'SELL',quantity:.002,reduce_only:true}),/exceed/);
  await assert.rejects(()=>bridge.call('place_order',{...entry(),quantity:.001,reduce_only:true}),/oppose/);assert.equal(transport.writes.length,0);
});
test('existing position, pending order and capital cap independently block entry',async()=>{
  for(const mode of ['position','orders','equity']){
    const {bridge,transport}=setup();if(mode==='position')transport.position=[{symbol:'BTCUSDT',positionAmt:'1'}];if(mode==='orders')transport.orders=[{orderId:99}];if(mode==='equity')transport.equity=501;
    await assert.rejects(()=>bridge.call('place_order',entry()));assert.equal(transport.writes.length,0);
  }
});
test('stop uses algo ID, explicit reduceOnly quantity and inward tick rounding',async()=>{
  const {bridge,transport}=setup();transport.position=[{symbol:'SOLUSDT',positionSide:'BOTH',positionAmt:'.1',markPrice:'100'}];
  await bridge.call('stop',{symbol:'SOLUSDT',side:'SELL',quantity:.1,stop_price:95.001,client_id:'solstop001'});
  const sent=transport.writes[0];assert.equal(sent.path,'/fapi/v1/algoOrder');assert.equal(sent.params.triggerPrice,'95.01');assert.equal(sent.params.reduceOnly,'true');assert.equal(sent.params.closePosition,undefined);
});
test('arbitrary endpoints or non-SOL write symbols denied',async()=>{
  const {bridge,transport}=setup();await assert.rejects(()=>bridge.call('/fapi/v1/order',{}),/allowlisted/);await assert.rejects(()=>bridge.call('place_order',{...entry(),symbol:'BTCUSDT'}),/SOLUSDT/);assert.equal(transport.writes.length,0);
});
test('account logs and HTTP errors redact secrets',()=>{
  const {root,state}=setup();const log=new AccountLog(state,['secret-value']);log.emit('error',{message:'request secret-value failed'});
  assert.equal(readFileSync(join(root,state.log),'utf8').includes('secret-value'),false);
  const error=safeError(new Error('signature=abcd& apiKey=secret-value failed'),['secret-value']);assert.equal(error.message.includes('abcd'),false);assert.equal(error.message.includes('secret-value'),false);
});
test('production transport sealed until identity acceptance, including direct calls',async()=>{
  const oldFetch=globalThis.fetch;let writes=0;
  globalThis.fetch=async()=>{writes++;return new Response(JSON.stringify({code:-1021,msg:'timestamp rejected'}),{status:400});};
  try {await assert.rejects(()=>new BinanceTransport({apiKey:'fake-key',secretKey:'fake-secret'},{weightLimit1m:2400}).signed('POST','/fapi/v1/order',{}),(e:any)=>e.code==='LIVE_ISOLATION_NOT_ACCEPTED');assert.equal(writes,0);}finally{globalThis.fetch=oldFetch;}
});
test('configuration is checked again after transport queue delay',async()=>{
  const config=structuredClone(live),{bridge,transport}=setup(config),base=transport.signed.bind(transport);
  transport.signed=async(method,path,params,guard)=>{if(method!=='GET')config.dry_run=true;return base(method,path,params,guard);};
  await assert.rejects(()=>bridge.call('place_order',entry()),/Paper mode/);assert.equal(transport.writes.length,0);
});
test('corrupt configuration introduced immediately before POST prevents the write',async()=>{
  const config=structuredClone(live),{bridge,transport}=setup(config),base=transport.signed.bind(transport);
  transport.signed=async(method,path,params,guard)=>{if(method!=='GET')(config.live as any).enabled='true';return base(method,path,params,guard);};
  await assert.rejects(()=>bridge.call('place_order',entry()),/CONFIG_INVALID/);assert.equal(transport.writes.length,0);
});
test('HTTP health is public, account routes require bearer and errors are safe',async()=>{
  const {bridge,state}=setup({...live,dry_run:true}),token='test-token-with-at-least-32-characters';
  const server=serverFor(bridge,token,new AccountLog(state));
  await new Promise<void>(resolve=>server.listen(0,'127.0.0.1',resolve));
  const address=server.address() as any,base=`http://127.0.0.1:${address.port}`;
  try{
    assert.equal((await fetch(base+'/health')).status,200);
    assert.equal((await fetch(base+'/rpc',{method:'POST',body:'{}'})).status,401);
    const response=await fetch(base+'/rpc',{method:'POST',headers:{authorization:`Bearer ${token}`},body:JSON.stringify({operation:'place_order',payload:entry()})});
    const body=await response.json() as any;assert.equal(body.ok,false);assert.equal(body.error.code,'PRIVATE_RPC_DISABLED');assert.equal(body.error.ambiguous,false);
  }finally{await new Promise<void>(resolve=>server.close(()=>resolve()));}
});
