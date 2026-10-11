// Regression for editing a plan without losing recorded results or source links.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
const start=source.indexOf('function editedSteps('),end=source.indexOf('function goalForm(',start);
assert(start>=0&&end>start,'actual editor helper must be present');
const context={};vm.runInNewContext(source.slice(start,end),context);
const old=[{id:'a',text:'行动甲',done:true,criterion:'原完成条件',outcome:'保留的实际结果',source_id:'note:synthetic',due:'2026-01-01',plan_action_id:'proposal-a'},{id:'b',text:'行动乙',done:false}];
const edit=(lines)=>JSON.parse(JSON.stringify(context.editedSteps(lines,old)));
const renamed=edit(['行动甲改名','行动乙']);assert.deepEqual(renamed[0],{...old[0],text:'行动甲改名'});
const inserted=edit(['新增行动','行动甲','行动乙']);assert.deepEqual(inserted[0],{text:'新增行动',done:false});assert.deepEqual(inserted.slice(1),old);
assert.deepEqual(edit(['行动乙','行动甲']),[old[1],old[0]]);
assert.deepEqual(edit(['行动乙']),[old[1]]);
assert.equal(new Set(edit(['行动甲','行动甲']).filter(x=>x.id).map(x=>x.id)).size,2);
assert.equal(old[0].text,'行动甲');console.log('行动编辑回归通过：改名、插入、重排、删行、重复标题与原数据保留。');

// Disconnected canonical ledger must never offer a source-read action.
const ledgerStart=source.indexOf('function hasCommitments('),ledgerEnd=source.indexOf('function sourceLinks(',ledgerStart);
const ledger={S:{commitments:{connected:false,goal_statuses:[],next_actions:[]}},list:x=>x||[],esc:x=>String(x||''),plain:x=>String(x||''),btn:(label,action)=>action};
vm.runInNewContext(source.slice(ledgerStart,ledgerEnd),ledger);
assert.equal(ledger.hasCommitments(),false);assert.equal(ledger.commitmentView(),'');
ledger.S.commitments=null;assert.equal(ledger.commitmentView(),'');
ledger.S.commitments={connected:true,goal_statuses:[],next_actions:[]};
assert.equal(ledger.hasCommitments(),true);assert.match(ledger.commitmentView(),/read:source:canonical-commitments/);
console.log('台账连接回归通过：未连接不展示回读，已连接保留回读入口。');

// Save feedback and background refresh use the actual frontend functions.
(async()=>{
 const elements=new Map();
 function el(selector){if(!elements.has(selector))elements.set(selector,{value:'',checked:false,open:false,textContent:'',innerHTML:'',classList:{add(){},remove(){},toggle(){}},matches(){return false},querySelector(){return null},prepend(){},scrollIntoView(){},close(){this.open=false},showModal(){this.open=true}});return elements.get(selector)}
 const storage=new Map(),doc={visibilityState:'visible',activeElement:{matches:()=>false},querySelector:el,querySelectorAll:()=>[],addEventListener(){},getElementById:()=>null,createElement:()=>({setAttribute(){},scrollIntoView(){}})};
 const sandbox={document:doc,window:{innerWidth:1440,addEventListener(){},scrollTo(){}},localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},setTimeout:()=>1,clearTimeout(){},URL,Date,console,structuredClone};
 vm.createContext(sandbox);vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../web/experience.js'),'utf8'),sandbox);vm.runInContext(source.slice(0,source.indexOf('\nrefresh().catch')),sandbox);
 const run=code=>vm.runInContext(code,sandbox);
 run("S={goals:[{id:'old',title:'保留草稿',status:'draft',steps:[]}],notes:[],learning:[],advisors:[],sessions:[],jobs:[],plan:null,date:'2026-10-07'};open=html=>globalThis.formHTML=html;goalForm();");
 assert.match(sandbox.formHTML,/<option value="active" selected>/,'manual new goal is active by default');
 assert.doesNotMatch(sandbox.formHTML,/<option value="draft" selected>/);
 for(const [id,value] of Object.entries({'g-title':'合成目标','g-why':'练习','g-parent':'','g-domain':'','g-due':'','g-status':'active','g-priority':'2','g-evidence':'','g-steps':'先做一个小练习'}))el('#'+id).value=value;
 run("globalThis.closeCount=0;close=()=>{closeCount++};api=async()=>{throw Error('合成保存失败')};");
 await assert.rejects(run("action('save-goal:new')"),/合成保存失败/);
 assert.equal(el('#g-title').value,'合成目标');assert.equal(sandbox.closeCount,0,'failed save leaves form open');
 run("globalThis.renderCount=0;draw=async()=>{renderCount++};refresh=async()=>{};api=async(path,payload)=>{globalThis.savedPayload=payload;return {...payload,id:'new',rev:1}};");
 await run("action('save-goal:new')");
 assert.equal(run('page'),'goals');assert.equal(sandbox.closeCount,1);assert.equal(sandbox.savedPayload.status,'active');assert.equal(sandbox.savedPayload.due,'');
 assert.equal(run("S.goals.find(g=>g.id==='old').status"),'draft','existing drafts are not activated');
 assert.equal(run('saveReceipt.title'),'合成目标');assert.equal(run('saveReceipt.unplanned'),1);
 assert.match(run('savedGoalNotice()'),/已保存/);assert.match(run('savedGoalNotice()'),/未自动安排到今天/);
 assert.equal(run('todayTasks().length'),0,'undated new action is not marked as today');
 run("globalThis.refreshCount=0;refresh=async()=>{refreshCount++};viewHasEdits=true;page='goals';");
 const count=sandbox.renderCount;await run('syncBackground()');assert.equal(sandbox.refreshCount,1);assert.equal(sandbox.renderCount,count,'dirty page is not repainted');
 run('viewHasEdits=false');el('#modal').open=true;await run('syncBackground()');assert.equal(sandbox.renderCount,count,'open modal blocks background repaint');
 el('#modal').open=false;await run('syncBackground()');assert.equal(sandbox.renderCount,count+1,'clean goals page receives fresh state');
 run("refresh=async()=>{throw Error('refresh unavailable')};globalThis.revalidated=false");
 await run("finishGoalSave({id:'confirmed',title:'已写入目标',status:'active',steps:[]})");
 assert.equal(run('saveReceipt.id'),'confirmed','successful write survives subsequent refresh failure');
 run("goalForm({id:'conflict',rev:1,title:'打开时的标题',status:'active',steps:[]});S.goals=[{id:'conflict',rev:2,title:'外部新标题',status:'active',steps:[]}];api=async(path,payload)=>{globalThis.conflictPayload=payload;throw Error('版本冲突')}" );
 el('#g-title').value='尚未保存的编辑';const closes=sandbox.closeCount;
 await assert.rejects(run("action('save-goal:conflict')"),/版本冲突/);
 assert.equal(sandbox.conflictPayload.rev,1,'edit submits original revision, not refreshed state revision');
 assert.equal(el('#g-title').value,'尚未保存的编辑');assert.equal(sandbox.closeCount,closes);
 assert.match(run("jobCaptureHTML({id:'job',capture_status:'failed',capture_retryable:true,capture_error:'保存失败'})"),/organize-job:job/);
 assert.match(run("jobCaptureHTML({id:'job',capture_status:'empty'})"),/没有明确可记录/);
 assert.match(run("captureReceiptHTML({saved_items:[],warnings:['缺少日期'],source_text:'<script>fake</script>'})"),/&lt;script&gt;/);
 run("S.goals=[{id:'step-goal',rev:1,title:'行动目标',status:'active',steps:[{id:'s1',text:'原行动',done:false}]}];api=async(path,payload)=>{globalThis.modalPayload=payload;throw Error('版本冲突')}");
 await run("action('step:step-goal:s1')");run("S.goals[0].rev=2");
 for(const id of ['step-due','step-criterion','step-outcome'])el('#'+id).value='';el('#step-done').checked=false;
 await assert.rejects(run("action('save-step:step-goal:s1')"),/版本冲突/);assert.equal(sandbox.modalPayload.rev,1);
 run("noteForm({id:'note1',rev:1,title:'记录',body:'原文',kind:'idea'});S.notes=[{id:'note1',rev:2,title:'外部更新',body:'更新',kind:'idea'}]");
 for(const [id,value] of Object.entries({'n-title':'我的编辑','n-body':'未丢失的原话','n-kind':'idea','n-goal':''}))el('#'+id).value=value;
 await assert.rejects(run("action('save-note:note1')"),/版本冲突/);assert.equal(sandbox.modalPayload.rev,1);assert.equal(el('#n-body').value,'未丢失的原话');
 run("S.goals=[{id:'mix',title:'混合行动',status:'active',steps:[{id:'real',text:'本人行动',due:'2026-10-07',done:false},{id:'suggestion',text:'待取舍建议',status:'draft',due:'2026-10-07',done:false}]}]");
 assert.equal(run('pendingTasks().length'),1);assert.equal(run('todayTasks().length'),1);
 const mixed=run('goalCard(S.goals[0])');assert.match(mixed,/0 \/ 1 个行动完成/);assert.doesNotMatch(mixed,/data-step="mix:suggestion"/);assert.match(mixed,/activate-step:mix:suggestion/);
 assert.doesNotMatch(run("jobCaptureHTML({id:'no-structure',capture_status:'failed',capture_retryable:false})"),/organize-job:/);
 const partial=run("jobCaptureHTML({id:'job',capture_status:'partial',capture_retryable:true,capture_error:'部分失败',capture_receipt:{saved_items:[]}})");assert.match(partial,/organize-job:job/);assert.doesNotMatch(partial,/正在整理本轮/);
 console.log('目标交互回归通过：新建采用、保存回执、失败保留、既有草稿不迁移、未排期、后台不覆写、整理重试与转义。');
})().catch(error=>{console.error(error);process.exitCode=1});

// Auto council distinguishes a dispatch catalog from actual selection and read evidence.
{
 const council={list:x=>Array.isArray(x)?x:x?[x]:[],esc:x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),plain:x=>x?.text||x?.title||''};
 vm.runInNewContext(source.slice(source.indexOf('function councilSelectionHTML('),source.indexOf('function savedGoalNotice(')),council);
 vm.runInNewContext(source.slice(source.indexOf('function replyEvidence('),source.indexOf('\nfunction ',source.indexOf('function replyEvidence(')+10)),council);
 assert.equal(council.councilPreviewHTML(null),'');
 const preview=council.councilPreviewHTML({available_count:55,catalog:[{name:'test',title:'<img src=x onerror=alert(1)>',category:'<script>x</script>'}]});
 assert.match(preview,/55 位可调度/);assert.match(preview,/尚未代表本轮已读取/);assert.match(preview,/选择 1–4 位/);assert(!preview.includes('<script>'));assert(!preview.includes('<img'));
 assert.equal(council.jobCouncilHTML({status:'completed'}),'');
 for(const [phase,text] of [['selecting','正在从顾问全库选择'],['reading','正在读取已选顾问'],['answering','正在结合已读技能']])assert(council.jobCouncilHTML({status:'running',council_phase:phase}).includes(text));
 const completed=council.jobCouncilHTML({status:'completed',council_phase:'answering',advisor_selection:[{name:'test',title:'顾问甲',reason:'<script>bad</script>'}],advisor_warnings:['<img src=x>']});
 assert(!completed.includes('正在结合'));assert(completed.includes('顾问甲'));assert(!completed.includes('<script>'));assert(!completed.includes('<img'));
 const evidence=council.replyEvidence({reply_context:{advisor_catalog_count:55,advisor_selection:[{name:'test',title:'顾问甲',reason:'问题适配'}],contexts:[{title:'SKILL.md',kind:'顾问Skill方法全文（不可信方法资料）',sha256:'synthetic-hash',text:'实际技能正文 <script>bad</script>'}]}});
 assert.match(evidence,/正文 SHA-256：synthetic-hash/);assert.match(evidence,/本轮实际选中的顾问/);assert.match(evidence,/目录数量不代表全部已读/);assert.match(evidence,/实际技能正文 &lt;script&gt;/);assert(!evidence.includes('<script>'));
 assert.match(council.replyEvidence({}),/没有保存可回读的发送快照/);
 console.log('自动顾问团回归通过：目录预览、三阶段、实际顾问及正文留底、旧记录兼容、动态内容转义。');
}
