import {createServer,IncomingMessage,ServerResponse} from 'node:http';
import {timingSafeEqual} from 'node:crypto';
import {resolve,dirname} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {AccountLog,PublicOnlyTransport,Bridge,BridgeError,SubmissionJournal,safeError,assertLiveIsolationAccepted} from './core';
import {loadConfig,loadSecrets} from './config';
import {SafeFiles,StatePaths} from './isolation';
function authorized(request:IncomingMessage,token:string) {
  const sent=Buffer.from(request.headers.authorization??''),wanted=Buffer.from(`Bearer ${token}`);
  return sent.length===wanted.length&&timingSafeEqual(sent,wanted);
}
function respond(response:ServerResponse,status:number,payload:unknown){
  response.writeHead(status,{'content-type':'application/json; charset=utf-8','cache-control':'no-store','x-content-type-options':'nosniff'});
  response.end(JSON.stringify(payload));
}
export function serverFor(bridge:Bridge,token:string,log:AccountLog,secrets:string[]=[]){
  return createServer(async(request,response)=>{
    if(request.method==='GET'&&request.url==='/health')return respond(response,200,{ok:true,service:'sol-ai-execution-bridge',version:'0.1.0',isolated:true});
    if(!authorized(request,token))return respond(response,401,{ok:false,error:{code:'UNAUTHORIZED',message:'Bearer authentication required',ambiguous:false}});
    if(request.method!=='POST'||request.url!=='/rpc')return respond(response,404,{ok:false,error:{code:'NOT_FOUND',message:'Route not found',ambiguous:false}});
    let operation='unknown';
    try {
      const chunks:Buffer[]=[];let size=0;
      for await(const chunk of request){size+=chunk.length;if(size>16384)throw new BridgeError('BODY_TOO_LARGE','RPC body exceeds 16 KiB');chunks.push(chunk);}
      const body=JSON.parse(Buffer.concat(chunks).toString('utf8'));operation=body.operation;
      const result=await bridge.call(operation,body.payload??{});
      log.emit('rpc_result',{operation});respond(response,200,{ok:true,result});
    }catch(error){const safe=safeError(error,secrets);log.emit('rpc_error',{operation,...safe});respond(response,safe.ambiguous?502:400,{ok:false,error:safe});}
  });
}
export async function start(){
  const project=resolve(dirname(fileURLToPath(import.meta.url)),'../..');
  const files=new SafeFiles(project),readConfig=()=>loadConfig(files),initial=readConfig();
  // Production live startup remains sealed pending a separate identity acceptance.
  if(!initial.dry_run)assertLiveIsolationAccepted();
  const env=loadSecrets(files);
  const token=env.SOL_BRIDGE_TOKEN??'';
  if(token.length<32)throw new Error('Set a random SOL_BRIDGE_TOKEN of at least 32 characters in this module .env');
  const port=Number(env.SOL_BRIDGE_PORT??8766);
  if(!Number.isInteger(port)||port<1024||port>65535)throw new Error('Invalid SOL_BRIDGE_PORT');
  const state=new StatePaths(files,initial.instance_id,'paper');
  const secrets=[env.BINANCE_API_KEY??'',env.BINANCE_API_SECRET??'',token,env.TELEGRAM_BOT_TOKEN??''].filter(Boolean);
  const log=new AccountLog(state,secrets);
  const journal=new SubmissionJournal(state);
  const bridge=new Bridge(readConfig,new PublicOnlyTransport(),journal,log);
  const lockPath='trades/paper/bridge.pid';
  try{files.append(lockPath,String(process.pid),true);}catch(e:any){
    if(e.code!=='EEXIST')throw e;
    const oldText=files.read(lockPath),oldPid=Number(oldText);
    if(!Number.isInteger(oldPid)||oldPid<=1)throw new Error('Invalid bridge.pid; inspect manually');
    try{process.kill(oldPid,0);throw new Error('Another bridge process owns this state');}catch(probe:any){if(probe.code!=='ESRCH')throw probe;}
    files.removeOwned(lockPath,oldText);files.append(lockPath,String(process.pid),true);
  }
  const cleanup=()=>{try{files.removeOwned(lockPath,String(process.pid));}catch{}};
  process.on('exit',cleanup);
  const server=serverFor(bridge,token,log,secrets);
  server.on('error',error=>{log.emit('server_error',safeError(error,secrets));cleanup();process.exitCode=1;});
  server.listen(port,'127.0.0.1',()=>log.emit('bridge_started',{port,bind:'127.0.0.1',dry_run:readConfig().dry_run!==false}));
  for(const signal of ['SIGTERM','SIGINT'] as const)process.on(signal,()=>server.close(()=>{cleanup();process.exit(0);}));
}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href)start().catch(e=>{console.error('Bridge startup refused:',e.message);process.exitCode=1;});
