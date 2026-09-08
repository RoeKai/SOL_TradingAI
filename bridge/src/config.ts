// The browser ESM parser has no Node/process.env debug or configuration hooks.
import {parse} from '../node_modules/yaml/browser/index.js';
import {SafeFiles} from './isolation';
export type ConfigMap=Record<string,any>;
export interface BridgeConfig {dry_run:boolean;instance_id:string;live:ConfigMap;risk:ConfigMap;execution:ConfigMap;[key:string]:any}
export function validateConfig(value:unknown):BridgeConfig{
  if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('CONFIG_INVALID: mapping required');
  const c=value as ConfigMap;
  if(typeof c.dry_run!=='boolean'||typeof c.instance_id!=='string'||!/^[a-z][a-z0-9_-]{2,47}$/.test(c.instance_id))throw new Error('CONFIG_INVALID: explicit boolean dry_run and instance_id required');
  for(const group of ['live','risk','execution'])if(!c[group]||typeof c[group]!=='object'||Array.isArray(c[group]))throw new Error(`CONFIG_INVALID: ${group} mapping required`);
  if(typeof c.live.enabled!=='boolean'||typeof c.live.dedicated_account_confirmed!=='boolean'||typeof c.live.confirmation!=='string')throw new Error('CONFIG_INVALID: strict live gate types required');
  for(const [obj,name,min,max] of [[c.live,'max_account_equity_usdt',0,500],[c.risk,'max_loss_per_trade',0,500],[c.risk,'max_margin_ratio',0,.2],[c.risk,'max_leverage',0,5],[c.risk,'taker_fee_rate',-Number.EPSILON,.01],[c.risk,'slippage_bps',-Number.EPSILON,100],[c.execution,'leverage',0,5]] as const){
    const v=obj[name];if(typeof v!=='number'||!Number.isFinite(v)||v<=min||v>max)throw new Error(`CONFIG_INVALID: ${name} must be a bounded numeric value`);
  }
  if(!Number.isInteger(c.risk.max_leverage)||!Number.isInteger(c.execution.leverage))throw new Error('CONFIG_INVALID: leverage integer required');
  if(JSON.stringify(c).includes('${'))throw new Error('CONFIG_INVALID: environment interpolation forbidden');
  return c as BridgeConfig;
}
export function loadConfig(files:SafeFiles):BridgeConfig{return validateConfig(parse(files.read('config.yaml'),{maxAliasCount:0,uniqueKeys:true}));}
export function loadSecrets(files:SafeFiles):Record<string,string>{
  if(!files.exists('.env'))return {};
  const raw=files.read('.env',true);if(raw.includes('${'))throw new Error('SECRET_INTERPOLATION_FORBIDDEN');
  const allowed=new Set(['BINANCE_API_KEY','BINANCE_API_SECRET','SOL_BRIDGE_TOKEN','SOL_BRIDGE_PORT','SOL_DASHBOARD_TOKEN','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID']);
  const result:Record<string,string>={};
  for(const original of raw.split(/\r?\n/)){
    const line=original.trim();if(!line||line.startsWith('#'))continue;
    const match=line.match(/^([A-Z_]+)\s*=\s*(.*)$/);if(!match||!allowed.has(match[1])||Object.hasOwn(result,match[1]))throw new Error('INVALID_MODULE_ENV');
    let value=match[2].trim();if(value.startsWith('"')||value.startsWith("'")){if(value.at(-1)!==value[0])throw new Error('INVALID_MODULE_ENV');value=value.slice(1,-1);}else value=value.replace(/\s+#.*$/,'');
    result[match[1]]=value;
  }
  return result;
}
