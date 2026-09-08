import {constants,closeSync,fstatSync,fsyncSync,lstatSync,mkdirSync,openSync,readFileSync,realpathSync,unlinkSync,writeSync} from 'node:fs';
import {dirname,isAbsolute,relative,resolve,sep} from 'node:path';

export const APPLICATION='sol-ai-trading-system';
export type Mode='paper'|'live';
export interface Identity {application:typeof APPLICATION;schema_version:1;instance_id:string;mode:Mode}
export function fail(message:string):never {throw new Error(`ISOLATION_REJECTED: ${message}`);}
/** Deployment root is explicit and trusted; all descendants are checked before I/O. */
export class SafeFiles {
  readonly root:string;
  private uid=process.getuid?.();
  constructor(root:string){
    if(!isAbsolute(root)||resolve(root)!==root||realpathSync(root)!==root)fail('root must be canonical absolute deployment directory');
    this.root=root;this.check(root,'directory');
    const policy=JSON.parse(this.read('isolation-policy.json'));
    if(policy.application!==APPLICATION||policy.policy_version!==1||policy.live_runtime_allowed!==false)fail('unaccepted deployment isolation policy');
  }
  private check(path:string,kind:'directory'|'file'){
    const s=lstatSync(path);
    if(s.isSymbolicLink()||(kind==='directory'?!s.isDirectory():!s.isFile()))fail('symlink or invalid file type');
    if(this.uid===undefined||s.uid!==this.uid||(s.mode&0o022)!==0)fail('wrong owner or writable-by-others path');
    if(kind==='file'&&s.nlink!==1)fail('hard-linked files are forbidden');
    return s;
  }
  path(name:string,missing=false):string{
    if(isAbsolute(name)||name.split(/[\\/]/).includes('..')||name.includes('\0'))fail('absolute/traversal path forbidden');
    const target=resolve(this.root,name),rel=relative(this.root,target);
    if(!rel||rel==='..'||rel.startsWith('..'+sep))fail('path outside module');
    this.check(this.root,'directory');
    const parts=rel.split(sep);let at=this.root,absent=false;
    for(let i=0;i<parts.length;i++){
      at=resolve(at,parts[i]);
      if(absent)continue;
      try{this.check(at,i===parts.length-1?'file':'directory');}
      catch(e:any){if(missing&&e.code==='ENOENT'){absent=true;continue;}throw e;}
    }
    return target;
  }
  read(name:string,secret=false):string{
    const path=this.path(name),fd=openSync(path,constants.O_RDONLY|constants.O_NOFOLLOW);
    try{const s=fstatSync(fd);if(s.nlink!==1||s.uid!==this.uid||!s.isFile()||(secret&&(s.mode&0o077)!==0))fail('unsafe opened file');return readFileSync(fd,'utf8');}finally{closeSync(fd);}
  }
  exists(name:string){try{this.path(name);return true;}catch(e:any){if(e.code==='ENOENT')return false;throw e;}}
  append(name:string,text:string,exclusive=false){
    const path=this.path(name,true);this.ensureParents(dirname(path));
    this.path(name,true);
    const flags=constants.O_WRONLY|constants.O_CREAT|constants.O_NOFOLLOW|(exclusive?constants.O_EXCL:constants.O_APPEND);
    const fd=openSync(path,flags,0o600);
    try{const s=fstatSync(fd);if(s.nlink!==1||s.uid!==this.uid||!s.isFile()||(s.mode&0o077)!==0)fail('unsafe output file');writeSync(fd,text);fsyncSync(fd);}finally{closeSync(fd);}
  }
  removeOwned(name:string,expected:string){const path=this.path(name);if(this.read(name)!==expected)fail('changed owned lock');unlinkSync(path);}
  private ensureParents(parent:string){
    const rel=relative(this.root,parent);let at=this.root;
    for(const part of rel?rel.split(sep):[]){at=resolve(at,part);try{this.check(at,'directory');}catch(e:any){if(e.code!=='ENOENT')throw e;mkdirSync(at,{mode:0o700});this.check(at,'directory');}}
  }
}
export class StatePaths {
  readonly identity:Identity;
  readonly journal:string;
  readonly log:string;
  readonly identityPath:string;
  constructor(readonly files:SafeFiles,instanceId:string,mode:Mode){
    if(!/^[a-z][a-z0-9_-]{2,47}$/.test(instanceId)||!['paper','live'].includes(mode))fail('invalid state identity');
    this.identity={application:APPLICATION,schema_version:1,instance_id:instanceId,mode};
    this.journal=`trades/${mode}/bridge-submissions.jsonl`;this.log=`logs/${mode}/bridge.jsonl`;this.identityPath=`trades/${mode}/bridge-identity.json`;
    for(const path of [this.journal,this.log,this.identityPath])files.path(path,true);
    if(files.exists(this.identityPath))this.assertIdentity();
    else if(files.exists(this.journal)||files.exists(this.log))fail('unclaimed existing state must never be adopted');
    else files.append(this.identityPath,JSON.stringify(this.identity)+'\n',true);
    // Reject copied/foreign records before any subsequent log or journal write.
    for(const path of [this.journal,this.log])if(files.exists(path))for(const line of files.read(path).split('\n').filter(Boolean))this.assertRecord(JSON.parse(line));
  }
  assertRecord(record:any){for(const [k,v] of Object.entries(this.identity))if(record?.[k]!==v)fail('foreign instance, mode or schema state');}
  assertIdentity(){this.assertRecord(JSON.parse(this.files.read(this.identityPath)));}
  readJournal(){this.assertIdentity();return this.files.exists(this.journal)?this.files.read(this.journal):'';}
  append(path:string,record:Record<string,unknown>){this.assertIdentity();if(path!==this.log&&path!==this.journal)fail('unexpected state sink');this.files.append(path,JSON.stringify({...record,...this.identity})+'\n');}
}
