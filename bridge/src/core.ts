import {request, floorBinanceQuantityToStep, roundBinanceStopPriceToTick} from '../vendor/binance-transport';
import {calculateSymbolMaxLossAmount} from '../vendor/symbol-loss-policy';
import {createLogger} from '../vendor/logger';
import {redactCredentialError} from '../vendor/credential-redaction';
import {configureRequestRuntime} from '../vendor/safe-request';
import {StatePaths} from './isolation';
import {BridgeConfig,validateConfig} from './config';
export type {BridgeConfig} from './config';

export type Payload=Record<string,any>;
export interface Credentials {apiKey:string;secretKey:string}
export interface Transport {readonly kind:'mock'|'production';signed(method:string,path:string,params:Record<string,string>,beforeWrite?:()=>Promise<void>):Promise<any>;public(path:string):Promise<any>}
export class BridgeError extends Error {constructor(public code:string, message:string, public ambiguous=false){super(message);}}
export function safeError(error:unknown, secrets:string[]=[]):{code:string;message:string;ambiguous:boolean} {
  const e=error as any;
  const rawCode=e?.code??e?.binanceCode??'BRIDGE_ERROR';
  let message=redactCredentialError(e,secrets);
  message=message.replace(/(signature|api[_-]?key|secret|token)=([^&\s]+)/gi,'$1=[REDACTED]');
  return {code:String(rawCode),message,ambiguous:e?.ambiguous===true};
}
export class AccountLog {
  private logger=createLogger('SolAiBridge',{minLevel:'info'});
  constructor(private state:StatePaths,private secrets:string[]=[]){state.assertIdentity();}
  emit(event:string,meta:Payload={}) {
    const safe=JSON.parse(redactCredentialError(JSON.stringify(meta),this.secrets));
    const record={timestamp:new Date().toISOString(),account_id:'sol-ai',event,...safe};
    this.state.append(this.state.log,record);
    this.logger.info(event,record);
  }
}
/** Persistent at-most-once submission guard. UNKNOWN is never reset automatically. */
export class SubmissionJournal {
  private seen=new Map<string,{kind:'order'|'stop';symbol:string}>();
  constructor(readonly state:StatePaths){
    try {
      const content=state.readJournal();
      for(const line of content.split('\n').filter(Boolean)) {
        const record=JSON.parse(line);state.assertRecord(record);
        if(!['order','stop'].includes(record.kind)||record.symbol!=='SOLUSDT'||record.state!=='UNKNOWN'||typeof record.key!=='string'||this.seen.has(record.key))throw new Error('invalid ownership record');
        clientId(record.key);this.seen.set(record.key,{kind:record.kind,symbol:record.symbol});
      }
    } catch(e:any){if(e.code!=='ENOENT')throw new Error('Submission journal damaged; refusing bridge startup');}
  }
  has(key:string){this.state.assertIdentity();return this.seen.has(key);}
  assertOwned(key:string,kind:'order'|'stop',symbol:string){this.state.assertIdentity();const own=this.seen.get(clientId(key));if(!own||own.kind!==kind||own.symbol!==symbol)throw new BridgeError('FOREIGN_ORDER_ID','Only journal-owned original IDs may be queried or cancelled');return key;}
  reserve(key:string,kind:'order'|'stop',symbol:string){
    if(this.seen.has(key))throw new BridgeError('ALREADY_SUBMITTED','Client ID has a durable submission. Query the original ID; never replay.',true);
    clientId(key);if(symbol!=='SOLUSDT')throw new BridgeError('SYMBOL_NOT_ALLOWED','Unexpected journal symbol');
    this.state.append(this.state.journal,{key,kind,symbol,state:'UNKNOWN',at:new Date().toISOString()});
    this.seen.set(key,{kind,symbol});
  }
}
export class BinanceTransport implements Transport {
  readonly kind='production' as const;
  constructor(private credentials:Credentials,options:{weightLimit1m:number}){configureRequestRuntime(options);}
  async signed(method:string,path:string,params:Record<string,string>,beforeWrite?:()=>Promise<void>){
    assertLiveIsolationAccepted();
    if(!this.credentials.apiKey||!this.credentials.secretKey)throw new BridgeError('NO_CREDENTIALS','Set Binance credentials in this module .env');
    let attempts=0;
    return request(this.credentials,method,path,params,{retries:0,beforeWriteAttempt:async()=>{
      if(attempts++>0)throw new BridgeError('UNKNOWN','Signed request will not be replayed; reconcile original client ID',true);
      await beforeWrite?.();
    }});
  }
  async public(path:string){
    const response=await fetch('https://fapi.binance.com'+path,{signal:AbortSignal.timeout(8000)});
    if(!response.ok)throw new BridgeError('PUBLIC_API_ERROR',`Public Binance API HTTP ${response.status}`);
    return response.json();
  }
}
export class PublicOnlyTransport implements Transport {
  readonly kind='production' as const;
  async signed():Promise<never>{throw new BridgeError('PRIVATE_RPC_DISABLED','This transport has no private account capability');}
  async public(path:string){
    if(path!=='/fapi/v1/exchangeInfo')throw new BridgeError('PUBLIC_PATH_NOT_ALLOWED','Public-only bridge allows exchange rules only');
    const response=await fetch('https://fapi.binance.com'+path,{signal:AbortSignal.timeout(8000)});
    if(!response.ok)throw new BridgeError('PUBLIC_API_ERROR',`Public Binance API HTTP ${response.status}`);
    return response.json();
  }
}
/** Release blocker, intentionally not controllable by config, environment or RPC. */
export function assertLiveIsolationAccepted():never {throw new BridgeError('LIVE_ISOLATION_NOT_ACCEPTED','Production private exchange access is sealed until independent account identity and deployment isolation acceptance is completed');}
function positive(x:unknown,name:string):number {const n=Number(x);if(!Number.isFinite(n)||n<=0)throw new BridgeError('INVALID_INPUT',`${name} must be positive`);return n;}
function clientId(x:unknown):string {if(typeof x!=='string'||!/^sol[.A-Z:/a-z0-9_-]{1,33}$/.test(x))throw new BridgeError('INVALID_CLIENT_ID','A deterministic sol-prefixed client_id (4–36 chars) is mandatory');return x;}
function side(x:unknown):string {if(x!=='BUY'&&x!=='SELL')throw new BridgeError('INVALID_SIDE','side must be BUY or SELL');return x;}
const WRITES=new Set(['place_order','cancel_order','stop','cancel_stop','leverage']);
export function assertWriteEnabled(config:BridgeConfig) {
  validateConfig(config);
  if(config.dry_run!==false||config.live?.enabled!==true||config.live?.confirmation!=='ENABLE_SMALL_CAPITAL_LIVE'||config.live?.dedicated_account_confirmed!==true) {
    throw new BridgeError('LIVE_DISABLED','Exchange writes require explicit dry_run=false, live.enabled, live.confirmation and dedicated_account_confirmed in config.yaml');
  }
}
export class Bridge {
  private rulesCache:{at:number;data:any}|undefined;
  private queue=Promise.resolve();
  constructor(private config:()=>BridgeConfig,private transport:Transport,private journal:SubmissionJournal,private log:AccountLog){}
  private privateGate(){
    const c=validateConfig(this.config());
    if(c.dry_run)throw new BridgeError('PRIVATE_RPC_DISABLED','Paper mode forbids all private exchange RPCs, including account reads');
    assertWriteEnabled(c);
    if(this.transport.kind!=='mock')assertLiveIsolationAccepted();
    const identity=this.journal.state.identity;
    if(identity.instance_id!==c.instance_id||identity.mode!=='live')throw new BridgeError('STATE_IDENTITY_MISMATCH','Private RPC requires the same live instance state');
    this.journal.state.assertIdentity();
  }
  /** One writer per independent module; no concurrent entry race at this boundary. */
  call(operation:string,payload:Payload={}):Promise<any> {
    const task=this.queue.then(()=>this.dispatch(operation,payload));
    this.queue=task.then(()=>undefined,()=>undefined);return task;
  }
  private async rules(symbol='SOLUSDT') {
    if(!this.rulesCache||Date.now()-this.rulesCache.at>600_000)this.rulesCache={at:Date.now(),data:await this.transport.public('/fapi/v1/exchangeInfo')};
    const info=this.rulesCache.data.symbols?.find((s:any)=>s.symbol===symbol);
    if(!info||info.status!=='TRADING'||info.contractType!=='PERPETUAL')throw new BridgeError('UNSUPPORTED_SYMBOL','Active perpetual exchange rules required');
    return info;
  }
  private async write(operation:string,path:string,method:string,params:Record<string,string>,key?:string) {
    this.privateGate();
    if(key)this.journal.reserve(key,operation==='stop'?'stop':'order',params.symbol);
    this.log.emit('exchange_write_intent',{operation,client_id:key,symbol:params.symbol,type:params.type,quantity:params.quantity});
    let attempted=false;
    try {
      const result=await this.transport.signed(method,path,params,async()=>{this.privateGate();attempted=true;});
      if((operation==='place_order'&&!result?.orderId)||(operation==='stop'&&!result?.algoId))throw new BridgeError('UNKNOWN','Exchange did not return an authoritative order ID',true);
      this.log.emit('exchange_write_result',{operation,client_id:key,result});return result;
    }catch(e:any){
      const code=Number(e?.binanceCode);
      const explicitReject=Number.isFinite(code)&&code<0&&![-1000,-1001,-1007].includes(code);
      if(attempted&&!explicitReject&&!(e instanceof BridgeError))throw new BridgeError('UNKNOWN',String(e?.message||e),true);
      throw e;
    }
  }
  private async dispatch(operation:string,p:Payload) {
    if(!p||typeof p!=='object'||Array.isArray(p))throw new BridgeError('INVALID_INPUT','payload must be an object');
    validateConfig(this.config());
    if(operation!=='rules')this.privateGate();
    const symbol=p.symbol??'SOLUSDT';
    if(!['SOLUSDT','BTCUSDT','ETHUSDT'].includes(symbol)||(WRITES.has(operation)&&symbol!=='SOLUSDT'))throw new BridgeError('SYMBOL_NOT_ALLOWED','Only SOLUSDT trading is enabled');
    switch(operation){
      case 'rules':return this.rules(symbol);
      case 'account':return this.transport.signed('GET','/fapi/v3/account',{});
      case 'positions':return this.transport.signed('GET','/fapi/v3/positionRisk',{});
      case 'position_mode':return this.transport.signed('GET','/fapi/v1/positionSide/dual',{});
      case 'open_orders':return this.transport.signed('GET','/fapi/v1/openOrders',{});
      case 'open_stops':return this.transport.signed('GET','/fapi/v1/openAlgoOrders',{});
      case 'order':return this.transport.signed('GET','/fapi/v1/order',{symbol,origClientOrderId:this.journal.assertOwned(p.client_id,'order',symbol)});
      case 'query_stop':return this.transport.signed('GET','/fapi/v1/algoOrder',{clientAlgoId:this.journal.assertOwned(p.client_id,'stop',symbol)});
      case 'trades': {
        const owned=this.journal.assertOwned(p.client_id,'order',symbol);
        const order=await this.transport.signed('GET','/fapi/v1/order',{symbol,origClientOrderId:owned});
        if(!order?.orderId)throw new BridgeError('UNKNOWN','Original order is not yet confirmed',true);
        return this.transport.signed('GET','/fapi/v1/userTrades',{symbol,limit:'1000',orderId:String(order.orderId)});
      }
      case 'cancel_order':return this.write(operation,'/fapi/v1/order','DELETE',{symbol,origClientOrderId:this.journal.assertOwned(p.client_id,'order',symbol)});
      case 'cancel_stop':return this.write(operation,'/fapi/v1/algoOrder','DELETE',{clientAlgoId:this.journal.assertOwned(p.client_id,'stop',symbol)});
      case 'leverage': {
        const leverage=positive(p.leverage,'leverage'),max=positive(this.config().risk?.max_leverage??5,'max_leverage');
        if(!Number.isInteger(leverage)||leverage>max||leverage>5)throw new BridgeError('LEVERAGE_CAP','Leverage exceeds live cap');
        return this.write(operation,'/fapi/v1/leverage','POST',{symbol,leverage:String(leverage)});
      }
      case 'stop': {
        const info=await this.rules(symbol),q=positive(p.quantity,'quantity'),direction=side(p.side);
        const filter=info.filters.find((f:any)=>f.filterType==='LOT_SIZE'),priceFilter=info.filters.find((f:any)=>f.filterType==='PRICE_FILTER');
        const quantity=floorBinanceQuantityToStep(q,filter.stepSize);
        if(Number(quantity)!==q)throw new BridgeError('QUANTITY_STEP','Stop quantity must exactly match exchange step; never uplift');
        const positions=await this.transport.signed('GET','/fapi/v3/positionRisk',{});
        const position=positions.find((v:any)=>v.symbol===symbol&&v.positionSide==='BOTH');
        const amount=Number(position?.positionAmt??0);
        if((direction==='SELL'?amount<=0:amount>=0)||q>Math.abs(amount)+1e-10)throw new BridgeError('EXIT_SCOPE','Stop exceeds or opposes actual dedicated position');
        const trigger=roundBinanceStopPriceToTick(positive(p.stop_price,'stop_price'),priceFilter.tickSize,direction==='SELL'?'long':'short');
        const mark=positive(position?.markPrice,'markPrice');
        if(direction==='SELL'?Number(trigger)>=mark:Number(trigger)<=mark)throw new BridgeError('STOP_ALREADY_CROSSED','Stop is already crossed; close immediately');
        return this.write(operation,'/fapi/v1/algoOrder','POST',{symbol,side:direction,type:'STOP_MARKET',algoType:'CONDITIONAL',quantity,reduceOnly:'true',positionSide:'BOTH',triggerPrice:trigger,workingType:'MARK_PRICE',clientAlgoId:clientId(p.client_id)},clientId(p.client_id));
      }
      case 'place_order':return this.place(p,symbol);
      default:throw new BridgeError('OPERATION_NOT_ALLOWED','RPC operation is not allowlisted');
    }
  }
  private async place(p:Payload,symbol:string) {
    const id=clientId(p.client_id),direction=side(p.side),quantity=positive(p.quantity,'quantity');
    if(this.journal.has(id))throw new BridgeError('ALREADY_SUBMITTED','Reconcile original client_id; no automatic resubmission',true);
    if(p.type!=='MARKET'&&p.type!=='LIMIT')throw new BridgeError('ORDER_TYPE','Only MARKET and LIMIT entries/exits are supported');
    const config=this.config(),risk=config.risk??{},info=await this.rules(symbol);
    const lot=info.filters.find((f:any)=>f.filterType===(p.type==='MARKET'?'MARKET_LOT_SIZE':'LOT_SIZE'))??info.filters.find((f:any)=>f.filterType==='LOT_SIZE');
    const rounded=floorBinanceQuantityToStep(quantity,lot.stepSize);
    if(Number(rounded)!==quantity||quantity>Number(lot.maxQty))throw new BridgeError('QUANTITY_STEP','Quantity violates exact exchange step/max; no upward adjustment');
    const reduced=p.reduce_only===true;
    const positions=await this.transport.signed('GET','/fapi/v3/positionRisk',{});
    const active=positions.filter((x:any)=>Math.abs(Number(x.positionAmt))>0);
    if(reduced){
      const current=active.find((x:any)=>x.symbol===symbol&&x.positionSide==='BOTH');
      const q=Number(current?.positionAmt??0);
      if(!current||(direction==='SELL'?q<=0:q>=0)||quantity>Math.abs(q)+1e-10)throw new BridgeError('EXIT_SCOPE','reduceOnly exit must not exceed or oppose actual position');
      // Minimum notional is an ENTRY rule; do not increase an exit to meet it.
    } else {
      if(active.length)throw new BridgeError('DUPLICATE_POSITION','No pyramiding, Martingale or averaging down; account must be flat');
      const [mode,orders,stops,account,symbolConfig,markSnapshot]=await Promise.all([
        this.transport.signed('GET','/fapi/v1/positionSide/dual',{}),
        this.transport.signed('GET','/fapi/v1/openOrders',{}),
        this.transport.signed('GET','/fapi/v1/openAlgoOrders',{}),
        this.transport.signed('GET','/fapi/v3/account',{}),
        this.transport.signed('GET','/fapi/v1/symbolConfig',{symbol}),
        this.transport.public('/fapi/v1/premiumIndex?symbol='+symbol),
      ]);
      if(mode.dualSidePosition!==false)throw new BridgeError('ACCOUNT_MODE','Dedicated account must already be in one-way mode');
      if(orders.length||(Array.isArray(stops)?stops:stops.orders??[]).length)throw new BridgeError('PENDING_ORDERS','Pending or foreign orders block new entries');
      const rc=p.risk_context??{},entry=positive(rc.entry_price??p.price,'risk_context.entry_price'),stop=positive(rc.stop_price,'risk_context.stop_price');
      const mark=positive(markSnapshot.markPrice,'exchange mark price');
      if(p.type==='LIMIT'&&Number(p.price)!==entry)throw new BridgeError('PRICE_PROOF','Limit price must match risk-approved entry price');
      const equity=positive(account.totalMarginBalance,'exchange equity'),proofEquity=positive(rc.equity,'risk_context.equity');
      const maxEquity=positive(config.live?.max_account_equity_usdt??500,'max_account_equity_usdt');
      if(equity>maxEquity||proofEquity>maxEquity)throw new BridgeError('CAPITAL_CAP','Dedicated account exceeds configured small-capital limit');
      const leverage=positive(rc.leverage,'risk_context.leverage'),limit=Math.min(positive(risk.max_leverage??5,'max_leverage'),5);
      if(!Number.isInteger(leverage)||leverage>limit)throw new BridgeError('LEVERAGE_CAP','Leverage exceeds configured maximum');
      const actualLeverage=Number(symbolConfig.find((v:any)=>v.symbol===symbol)?.leverage);
      if(actualLeverage!==leverage)throw new BridgeError('LEVERAGE_NOT_CONFIRMED','Exchange leverage differs from risk-approved leverage');
      const priceFilter=info.filters.find((f:any)=>f.filterType==='PRICE_FILTER');
      const normalizedStop=Number(roundBinanceStopPriceToTick(stop,priceFilter.tickSize,direction==='BUY'?'long':'short'));
      if(direction==='BUY'?normalizedStop>=entry:normalizedStop<=entry)throw new BridgeError('STOP_DIRECTION','Mandatory stop must be on loss side of entry');
      if(direction==='BUY'?normalizedStop>=mark:normalizedStop<=mark)throw new BridgeError('STOP_ALREADY_CROSSED','Current exchange mark has already crossed the mandatory stop');
      const maximum=Math.min(positive(risk.max_loss_per_trade??5,'max_loss_per_trade'),positive(rc.max_loss_usdt,'risk_context.max_loss_usdt'));
      const fee=Number(risk.taker_fee_rate??0.0005),slip=Number(risk.slippage_bps??10)/10000;
      if(!Number.isFinite(fee)||!Number.isFinite(slip)||fee<0||slip<0)throw new BridgeError('INVALID_CONFIG','Invalid fee/slippage buffer');
      // Reuse legacy fixed-equity loss budget math; new module owns its policy/state.
      const budget=calculateSymbolMaxLossAmount(equity,maximum/equity*100);
      const notionalPrice=Math.max(entry,mark);
      const lossDistance=p.type==='MARKET'?Math.max(Math.abs(entry-normalizedStop),Math.abs(mark-normalizedStop)):Math.abs(entry-normalizedStop);
      const projected=quantity*(lossDistance+notionalPrice*(2*fee+slip));
      if(projected>budget+1e-9)throw new BridgeError('LOSS_CAP','Stop loss plus fees/slippage exceeds per-trade risk budget');
      const margin=quantity*notionalPrice/leverage,maxMargin=Math.min(Number(risk.max_margin_ratio??0.2),0.2)*Math.min(equity,proofEquity);
      if(!Number.isFinite(maxMargin)||maxMargin<=0||margin>maxMargin||margin>Number(account.availableBalance))throw new BridgeError('MARGIN_CAP','Margin exceeds 20%/available balance cap');
      const minimum=Number(info.filters.find((f:any)=>f.filterType==='MIN_NOTIONAL')?.notional??0);
      if(quantity<Number(lot.minQty)||quantity*Math.min(entry,mark)<minimum)throw new BridgeError('ENTRY_MINIMUM','Entry below exchange minimum; never enlarge risk');
    }
    const params:Record<string,string>={symbol,side:direction,type:p.type,quantity:rounded,newClientOrderId:id,newOrderRespType:'RESULT',positionSide:'BOTH'};
    if(reduced)params.reduceOnly='true';
    if(p.type==='LIMIT'){
      const price=positive(p.price,'price'),tick=Number(info.filters.find((f:any)=>f.filterType==='PRICE_FILTER').tickSize);
      if(Math.abs(price/tick-Math.round(price/tick))>1e-8)throw new BridgeError('PRICE_TICK','Limit price violates tick');
      params.price=String(price);params.timeInForce='GTC';
    }
    return this.write('place_order','/fapi/v1/order','POST',params,id);
  }
}
