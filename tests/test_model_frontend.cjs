'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
const experience=fs.readFileSync(path.join(__dirname,'../web/experience.js'),'utf8'),source=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
const elements=new Map(),storage=new Map(),listeners={};
function element(key){if(!elements.has(key))elements.set(key,{value:'',checked:true,innerHTML:'',textContent:'',children:[],dataset:{},scrollHeight:0,scrollTop:0,clientHeight:0,querySelector(){return null},querySelectorAll(){return []},classList:{add(){},remove(){},toggle(){}},matches(){return false},focus(){},append(n){this.children.push(n)},close(){},scrollIntoView(){}});return elements.get(key)}
const box={Date,URL,Intl,crypto,console,structuredClone,document:{visibilityState:'visible',querySelector:element,querySelectorAll:()=>[],addEventListener:(k,fn)=>{listeners[k]=fn},createElement:()=>({dataset:{},querySelectorAll:()=>[],remove(){}})},window:{addEventListener(){},scrollTo(){}},localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,String(v)),removeItem:k=>storage.delete(k)},setTimeout:()=>1,clearTimeout(){}};
vm.createContext(box);vm.runInContext(experience,box);vm.runInContext(source.slice(0,source.indexOf('\nrefresh().catch')),box);const run=code=>vm.runInContext(code,box),views=box.GrowthExperience;
(async()=>{
 run("dataScope='model-test';page='chat';S={goals:[],sessions:[],jobs:[],notes:[]};globalThis.requests=[];api=async(path,payload)=>{requests.push({path,payload});return {default_model:'model-a',models:[{id:'model-a',label:'模型甲',provider:'本地路由'},{id:'model-b',label:'模型乙',description:'第二个模型'}],status:'available'}};");
 await Promise.all([run('loadModels()'),run('loadModels()')]);assert.equal(run('requests.length'),1,'concurrent model reads share request');
 await run('loadModels(true)');assert.equal(run('requests.at(-1).path'),'/api/models','status polling reuses backend directory cache');
 await run("action('models-refresh')");assert.equal(run('requests.at(-1).path'),'/api/models?refresh=1','explicit legacy refresh bypasses backend cache');
 assert.equal(run('requests.filter(r=>r.payload).length'),0,'directory refresh never starts generation');
 assert.match(run('modelSelectorHTML()'),/跟随本机默认 · 模型甲/);assert.match(run('modelSelectorHTML()'),/value="model-b"/);assert.match(run('modelConnectionHTML()'),/当前目录提供的型号可直接选择/);
 run("persistChatState({text:'保留这句未发送的话',model:'model-b'})");element('#chat-text').value='保留这句未发送的话';element('#chat-model').value='model-b';
 assert.equal(run('chatSnapshot().model'),'model-b');assert.equal(run('chatSnapshot().message'),'保留这句未发送的话');
 run("globalThis.firstKey=chatKey();newDraft();persistChatState({text:'另一个草稿',model:'model-a'});globalThis.secondKey=chatKey();draftKey=firstKey");
 assert.equal(run('currentDraft().model'),'model-b','model preference is draft-scoped');assert.equal(run('currentDraft().text'),'保留这句未发送的话');
 element('#chat-model').value='';await listeners.change({target:{id:'chat-model',value:'',dataset:{}}});assert.equal(run('chatSnapshot().model'),null,'default selection explicitly sends null');
 run("persistChatState({model:'retired-model'});api=async()=>{throw Error('offline')}");await run('loadModels(true)');assert.equal(run('currentDraft().model'),'retired-model');assert.match(run('modelSelectorHTML()'),/retired-model · 待核实/);assert.match(run('modelSelectorHTML()'),/当前选择已保留/);assert.equal(run('currentDraft().text'),'保留这句未发送的话');
 run("modelCatalog=null;modelCatalogError='offline'");assert.doesNotMatch(run('modelSelectorHTML()'),/value="model-a"/,'do not manufacture fallback models');assert.match(run('modelSelectorHTML()'),/跟随本机默认/);
 const rendered=views.processSummary({council_phase:'answering',effective_model:'model-a',analysis_updates:[{kind:'summary',at:'2026-10-07T09:00:00Z',text:'先核对目标与实际记录。'},{kind:'reasoning',text:'HIDDEN_RAW_REASONING'},{kind:'commentary',text:'已读到你这周的记录。'}],response_preview:'这是正在出现的回答。<script>bad</script>',progress_events:[{text:'真实回执',at:'2026-10-07T09:00:01Z'}]},{live:true});
 assert.match(rendered,/分析摘要/);assert.match(rendered,/先核对目标与实际记录/);assert.match(rendered,/已读到你这周的记录/);assert.match(rendered,/model-a/);assert.match(rendered,/正在生成回答/);assert.match(rendered,/&lt;script&gt;/);assert.doesNotMatch(rendered,/<script>|HIDDEN_RAW_REASONING/);
 const waiting=views.processSummary({council_phase:'selecting',analysis_updates:[],response_preview:''},{live:true});assert.match(waiting,/模型尚未返回可展示的摘要/);assert.doesNotMatch(waiting,/class="analysis-updates"|正在生成回答/,'absence of summary must not invent progress');
 const archived=views.processSummary({analysis_updates:[{kind:'summary',text:'当轮公开摘要'}],effective_model:'model-a'});assert.match(archived,/<details class="completed-analysis">/);assert.match(archived,/查看当轮分析摘要/);assert.match(archived,/当轮公开摘要/);
 const ended=views.processSummary({effective_model:'model-a',response_preview:'STALE_PARTIAL'});assert.match(ended,/model-a/);assert.doesNotMatch(ended,/STALE_PARTIAL/,'completed messages use canonical body, never stale stream');
 run("page='chat';modelCatalog={default_model:'model-a',models:[{id:'model-a'}]};modelCatalogError=''");box.document.activeElement={id:'chat-model'};element('#model-control').innerHTML='OPEN_SELECT_SENTINEL';run('repaintModels()');assert.equal(element('#model-control').innerHTML,'OPEN_SELECT_SENTINEL','background refresh must not replace a focused selector');box.document.activeElement=null;run('repaintModels()');assert.notEqual(element('#model-control').innerHTML,'OPEN_SELECT_SENTINEL');
 console.log('模型与动态前端通过：目录去重读取、默认/显式选型、草稿隔离、不丢输入、离线保留、真实摘要白名单、转义与流式正文边界。');
})().catch(error=>{console.error(error);process.exitCode=1});
