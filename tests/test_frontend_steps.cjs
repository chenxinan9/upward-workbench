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
const ledgerStart=source.indexOf('function hasCommitments('),ledgerEnd=source.indexOf('function domainCard(',ledgerStart);
const ledger={S:{commitments:{connected:false,goal_statuses:[],next_actions:[]}},list:x=>x||[],esc:x=>String(x||''),plain:x=>String(x||''),btn:(label,action)=>action};
vm.runInNewContext(source.slice(ledgerStart,ledgerEnd),ledger);
assert.equal(ledger.hasCommitments(),false);assert.equal(ledger.commitmentView(),'');
ledger.S.commitments=null;assert.equal(ledger.commitmentView(),'');
ledger.S.commitments={connected:true,goal_statuses:[],next_actions:[]};
assert.equal(ledger.hasCommitments(),true);assert.match(ledger.commitmentView(),/read:source:canonical-commitments/);
console.log('台账连接回归通过：未连接不展示回读，已连接保留回读入口。');
