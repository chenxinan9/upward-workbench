'use strict';

// Local recovery contains document drafts, unsent input and opaque undo receipts only.
function createWorkspaceDraftStore(storage, scope) {
  const prefix='upward.workspace.recovery.v1:'+encodeURIComponent(scope)+':';
  const key=(kind,id)=>prefix+kind+':'+encodeURIComponent(id||'new');
  function read(kind,id){try{const raw=storage?.getItem(key(kind,id));return raw?JSON.parse(raw):null;}catch{return null;}}
  function write(kind,id,value){try{if(!storage)return false;if(value===null)storage.removeItem(key(kind,id));else storage.setItem(key(kind,id),JSON.stringify(value));return true;}catch{return false;}}
  return {
    document(id){const v=read('document',id);return v&&v.id===id&&typeof v.text==='string'&&v.text.length<=600000&&typeof v.base_sha==='string'&&v.base_sha.length<=128?v:null;},
    saveDocument(id,base_sha,text){if(typeof text!=='string'||text.length>600000||typeof base_sha!=='string'||base_sha.length>128)return false;return write('document',id,{id,base_sha,text,updated_at:Date.now()});},
    clearDocument(id){return write('document',id,null);},
    chat(id){const v=read('chat',id);return v&&typeof v.text==='string'&&v.text.length<=100000?v.text:'';},
    saveChat(id,text){if(typeof text!=='string'||text.length>100000)return false;return write('chat',id,text?{text,updated_at:Date.now()}:null);},
    undo(id){const v=read('undo',id);return v&&typeof v.id==='string'&&typeof v.sha==='string'?v:null;},
    saveUndo(id,value){return write('undo',id,value?{id:value.id,sha:value.sha}:null);},
    isUndone(id){return read('undone',id)?.done===true;},
    markUndone(id){return write('undone',id,{done:true});}
  };
}
function workspaceDraftRecovery(record,file){
  if(!record||record.id!==file.id)return {kind:'none'};
  if(record.text===file.text)return {kind:'saved'};
  return {kind:record.base_sha===file.sha?'restored':'conflict',text:record.text,base_sha:record.base_sha};
}
function mergeWorkspaceMessages(current,incoming,prepend=false){
  const latest=new Map([...current,...incoming].map(item=>[item.id,item]));
  const order=prepend?[...incoming,...current]:[...current,...incoming],seen=new Set();
  return order.filter(item=>item.id&&!seen.has(item.id)&&seen.add(item.id)).map(item=>latest.get(item.id));
}
function workspacePollDelay(active,failures){return failures?Math.min(30000,3000*2**Math.min(failures-1,4)):active?1200:10000;}
(() => {
  const $ = id => document.getElementById(id);
  const list = value => Array.isArray(value) ? value : [];
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const state = {workspaceView:'knowledge',growthContext:{purpose:'general',scope:'personal',days:7,advisor_names:[]},storageScope:'local',token:'', roots:[], file:null, dirty:false, editing:false, attached:true, session:'', messages:[], jobs:[], sending:false, searchVersion:0, readVersion:0, undo:null, selectedModel:'', selection:'', filePolling:false, chatSignature:'', navigation:new Map(), draftBase:'', draftConflict:false, documentDraftSafe:false, fileMutation:false, chatEpoch:0, linkEpoch:0, chatRequest:null, chatFailures:0, sessions:[], sessionsPage:null, sessionsVersion:0, sessionsExpanded:false, messagesPage:null, historyExpanded:false, historyLoading:false,undoneReceipts:new Set()};
  let toastTimer, searchTimer, pendingLeave=null, draftTimer, chatTimer,growth=null;
  let recovery=createWorkspaceDraftStore(null,'unavailable');
  const selectionKey=name=>'upward.workspace.'+encodeURIComponent(state.storageScope)+'.'+name;
  function toast(text) { $('toast').textContent=text; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>{$('toast').hidden=true;},4500); }
  function notice(text,error=false) { $('chat-notice').textContent=text; $('chat-notice').classList.toggle('error',error); }
  async function request(url, data, extra={}) {
    const options={cache:'no-store',headers:{}},controller=data===undefined?new AbortController():null;let timedOut=false,timer;
    const abort=()=>controller?.abort();if(controller){options.signal=controller.signal;if(extra.signal?.aborted)controller.abort();else extra.signal?.addEventListener('abort',abort,{once:true});timer=setTimeout(()=>{timedOut=true;controller.abort();},15000);}
    if(data!==undefined){options.method='POST';options.headers={'Content-Type':'application/json','X-Desk-Token':state.token};options.body=JSON.stringify(data);}
    try{const response=await fetch(url,options);let body;try{body=await response.json();}catch(error){if(error.name==='AbortError')throw error;throw new Error('本地服务未返回有效数据，请稍后重试');}
      if(!response.ok||body.error){const err=new Error(typeof body.error==='string'?body.error:'请求未完成');err.status=response.status;err.data=body;throw err;}return body;
    }catch(error){if(timedOut)throw new Error('本地读取超时，请稍后重试');throw error;}finally{clearTimeout(timer);extra.signal?.removeEventListener('abort',abort);}
  }
  const query = (path, params) => path+'?'+new URLSearchParams(params);
  function inline(text){
    const slots=[];
    const stash=html=>{const n=slots.length;slots.push(html);return '\u0000'+n+'\u0000';};
    let out=escape(text).replace(/`([^`]+)`/g,(_,s)=>stash('<code>'+s+'</code>'));
    out=out.replace(/\[\[([^\]\n]+)\]\]/g,(_,raw)=>{const parts=raw.split('|');return stash('<button class="wiki-link" data-link="'+parts[0]+'">'+(parts[1]||parts[0])+'</button>');});
    out=out.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g,(_,label,url)=>{
      if(/^vault:/i.test(url)&&url.length>6)return stash('<button class="wiki-link" data-open="'+url.slice(6)+'">'+label+'</button>');
      if(/^https?:\/\//i.test(url)) return stash('<a href="'+url+'" target="_blank" rel="noopener noreferrer">'+label+' ↗</a>');
      if(!/^[a-z][a-z0-9+.-]*:/i.test(url) && !url.startsWith('//'))return stash('<button class="wiki-link" data-link="'+url+'">'+label+'</button>');
      return label;
    });
    out=out.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/__([^_]+)__/g,'<strong>$1</strong>').replace(/(?<!\*)\*([^*]+)\*(?!\*)/g,'<em>$1</em>');
    return out.replace(/\u0000(\d+)\u0000/g,(_,n)=>slots[Number(n)]||'');
  }
  function markdown(text){
    const lines=String(text||'').replace(/\r/g,'').split('\n'), out=[];let i=0,lineMarker=1;const emit=html=>out.push(html.replace(/^<([a-z0-9]+)/i,'<$1 data-line="'+lineMarker+'"'));
    if(lines[0]==='---'){const end=lines.indexOf('---',1);if(end>0&&end<100){emit('<details><summary>文档属性</summary><pre>'+escape(lines.slice(1,end).join('\n'))+'</pre></details>');i=end+1;}}
    while(i<lines.length){let line=lines[i];lineMarker=i+1;
      if(!line.trim()){i++;continue;}
      if(/^\s*(```|~~~)/.test(line)){const fence=line.trim().slice(0,3),code=[];i++;while(i<lines.length&&!lines[i].trim().startsWith(fence)){code.push(lines[i++]);}i++;emit('<pre><code>'+escape(code.join('\n'))+'</code></pre>');continue;}
      const heading=line.match(/^(#{1,6})\s+(.+)$/);if(heading){emit('<h'+heading[1].length+'>'+inline(heading[2])+'</h'+heading[1].length+'>');i++;continue;}
      if(/^\s*(---+|\*\*\*+)\s*$/.test(line)){emit('<hr>');i++;continue;}
      if(/^>\s?/.test(line)){const quote=[];while(i<lines.length&&/^>/.test(lines[i]))quote.push(lines[i++].replace(/^>\s?/,''));emit('<blockquote>'+markdown(quote.join('\n'))+'</blockquote>');continue;}
      if(/^\s*([-*+] |\d+\. )/.test(line)){const ordered=/^\s*\d+\./.test(line),items=[];while(i<lines.length&&/^\s*([-*+] |\d+\. )/.test(lines[i])){let value=lines[i++].replace(/^\s*([-*+] |\d+\. )/,'');value=value.replace(/^\[ \] /,'☐ ').replace(/^\[[xX]\] /,'☑ ');items.push('<li>'+inline(value)+'</li>');}emit('<'+(ordered?'ol':'ul')+'>'+items.join('')+'</'+(ordered?'ol':'ul')+'>');continue;}
      if(i+1<lines.length&&line.includes('|')&&/^\s*\|?\s*:?-{3,}/.test(lines[i+1])){const cells=v=>v.trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim());const head=cells(line);i+=2;const rows=[];while(i<lines.length&&lines[i].includes('|')&&lines[i].trim())rows.push(cells(lines[i++]));emit('<div class="table-scroll"><table><thead><tr>'+head.map(c=>'<th>'+inline(c)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(c=>'<td>'+inline(c)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>');continue;}
      const para=[line];i++;while(i<lines.length&&lines[i].trim()&&!/^(#{1,6}\s|\s*([-*+] |\d+\. )|>|```|~~~|---+$)/.test(lines[i]))para.push(lines[i++]);emit('<p>'+para.map(inline).join('<br>')+'</p>');
    }
    return out.join('');
  }
  function rootSection(root){
    const section=document.createElement('section');section.className='tree-root';section.dataset.root=root.id;
    const button=document.createElement('button');button.className='root-button';button.setAttribute('aria-expanded','false');button.innerHTML='<span class="chevron">▸</span><span class="root-icon">◇</span><span>'+escape(root.label)+'</span>'+(root.writable===false?'<span class="root-count">只读</span>':'');
    const body=document.createElement('div');body.hidden=true;body.className='root-content';
    button.addEventListener('click',async()=>{body.hidden=!body.hidden;button.setAttribute('aria-expanded',String(!body.hidden));button.querySelector('.chevron').textContent=body.hidden?'▸':'▾';if(!body.hidden&&!body.dataset.loaded)await loadEntries(root.id,'',body,0);});
    section.append(button,body);state.navigation.set(root.id,{button,body});return section;
  }
  async function loadRoots(){
    const data=await request('/api/vault/roots');state.roots=list(data.roots);state.navigation.clear();$('tree').replaceChildren();
    const groups=new Map();for(const root of state.roots){const group=root.group||'我的知识库';if(!groups.has(group))groups.set(group,[]);groups.get(group).push(root);}
    for(const [group,roots] of groups){
      if(group==='我的知识库' || !group){for(const root of roots)$('tree').append(rootSection(root));}
      else{const section=document.createElement('section');section.className='tree-root';const button=document.createElement('button');button.className='root-button';button.setAttribute('aria-expanded','false');button.innerHTML='<span class="chevron">▸</span><span class="root-icon">✧</span><span>'+escape(group)+'</span><span class="root-count">'+roots.length+'</span>';const body=document.createElement('div');body.hidden=true;body.style.paddingLeft='9px';for(const root of roots)body.append(rootSection(root));button.addEventListener('click',()=>{body.hidden=!body.hidden;button.setAttribute('aria-expanded',String(!body.hidden));button.querySelector('.chevron').textContent=body.hidden?'▸':'▾';});section.append(button,body);$('tree').append(section);for(const root of roots)state.navigation.get(root.id).groupButton=button;}
    }
    $('root-shortcuts').replaceChildren();for(const root of state.roots.filter(r=>!r.group||r.group==='我的知识库').slice(0,6)){const button=document.createElement('button');button.innerHTML=escape(root.label)+'<span>↗</span>';button.addEventListener('click',()=>openRoot(root.id));$('root-shortcuts').append(button);}
    const writable=state.roots.filter(r=>r.writable);$('new-note-root').replaceChildren();for(const root of writable){const option=document.createElement('option');option.value=root.id;option.textContent=root.label;$('new-note-root').append(option);} $('new-note').disabled=!writable.length;
    $('connection-label').textContent='本地知识库已连接';$('connection-dot').classList.add('online');
  }
  function openRoot(id){const root=state.navigation.get(id);if(!root)return;if(root.groupButton?.getAttribute('aria-expanded')==='false')root.groupButton.click();if(root.body.hidden)root.button.click();document.body.classList.remove('nav-hidden');if(innerWidth<=900)document.body.classList.add('nav-open');root.button.scrollIntoView({block:'nearest'});}
  async function loadEntries(rootId,path,body,depth){
    body.innerHTML='<p class="tree-empty">读取目录…</p>';
    try{const data=await request(query('/api/vault/tree',{root_id:rootId,path}));body.replaceChildren();body.dataset.loaded='true';const entries=list(data.entries);if(!entries.length){body.innerHTML='<p class="tree-empty">这里还没有文档</p>';return;}
      for(const entry of entries){const directory=entry.type==='directory'||entry.kind==='directory';const button=document.createElement('button');button.className='tree-entry'+(directory?' directory':'');button.style.paddingLeft=(14+depth*12)+'px';button.innerHTML='<span class="file-symbol">'+(directory?'▸':'▤')+'</span><span>'+escape(entry.name||entry.title||entry.path)+'</span>';button.title=entry.path||entry.name||'';
        if(directory){button.setAttribute('aria-expanded','false');const child=document.createElement('div');child.hidden=true;button.addEventListener('click',async()=>{child.hidden=!child.hidden;button.setAttribute('aria-expanded',String(!child.hidden));button.querySelector('.file-symbol').textContent=child.hidden?'▸':'▾';if(!child.hidden&&!child.dataset.loaded)await loadEntries(entry.root_id||rootId,entry.path,child,depth+1);});body.append(button,child);}
        else{button.dataset.fileId=entry.id||entry.file_id;button.classList.toggle('active',button.dataset.fileId===state.file?.id);button.addEventListener('click',()=>openFile(button.dataset.fileId));body.append(button);}}
    }catch(error){body.innerHTML='<p class="tree-error">'+escape(error.message)+'</p>';const retry=document.createElement('button');retry.className='text-button';retry.textContent='重新读取';retry.addEventListener('click',()=>loadEntries(rootId,path,body,depth));body.append(retry);}
  }
  function markActive(){document.querySelectorAll('[data-file-id]').forEach(button=>button.classList.toggle('active',button.dataset.fileId===state.file?.id));}
  function persistDocumentDraft(){
    clearTimeout(draftTimer);if(!state.file)return true;
    const ok=state.dirty?recovery.saveDocument(state.file.id,state.draftBase||state.file.sha,$('editor').value):recovery.clearDocument(state.file.id);
    state.documentDraftSafe=ok;if(state.dirty)updateFileState();if(!ok){$('document-notice').hidden=false;$('document-notice').textContent='本机草稿保护未能写入（空间不足或文档过大）。请保存到磁盘或复制内容后再关闭。';}return ok;
  }
  function persistChatDraft(){const ok=recovery.saveChat(state.session,$('message').value);if(!ok){$('chat-draft-state').hidden=false;$('chat-draft-state').textContent='输入草稿未能保存在本机，请先复制内容。';}return ok;}
  function restoreChatDraft(){const text=recovery.chat(state.session);$('message').value=text;$('chat-draft-state').hidden=!text;$('chat-draft-state').textContent=text?'已恢复此对话的未发送输入 · 不会自动发送':'';}
  function discardDocumentDraft(){
    if(!state.file)return;clearTimeout(draftTimer);recovery.clearDocument(state.file.id);state.dirty=false;state.documentDraftSafe=false;state.draftBase=state.file.sha;state.draftConflict=false;$('editor').value=state.file.text||'';$('document-notice').hidden=true;renderFile();
  }
  async function canLeave(){
    if(state.fileMutation){toast('正在保存或撤销，请稍候再切换文档');return false;}
    if(!state.dirty)return true;if(pendingLeave)return pendingLeave;
    persistDocumentDraft();const dialog=$('unsaved-dialog');dialog.returnValue='cancel';
    pendingLeave=new Promise(resolve=>{dialog.addEventListener('close',()=>{const discard=dialog.returnValue==='discard';if(discard)discardDocumentDraft();pendingLeave=null;resolve(discard);},{once:true});});
    dialog.showModal();return pendingLeave;
  }
  async function openFile(id,force=false,line=0){
    if(!id)return;if(!force&&state.file?.id===id){growth?.setView('knowledge');if(line)locateLine(line);return;}if(!force&&!await canLeave())return;
    ++state.linkEpoch;const version=++state.readVersion,editorAtStart=$('editor').value;$('file-state').textContent='读取本地文档…';
    try{const file=await request(query('/api/vault/read',{id}));if(version!==state.readVersion)return;if(state.dirty&&$('editor').value!==editorAtStart){toast('编辑内容发生变化，已保留草稿，本次未切换文档');return;}state.selection='';state.file=file;state.draftBase=file.sha;state.draftConflict=false;state.documentDraftSafe=false;state.dirty=false;state.editing=false;state.attached=true;$('allow-write').checked=false;state.undo=recovery.undo(file.id);if(state.undo?.sha!==file.sha){state.undo=null;recovery.saveUndo(file.id,null);}$('editor').value=file.text||'';$('document-notice').hidden=true;
      const recovered=workspaceDraftRecovery(recovery.document(file.id),file);
      if(recovered.kind==='saved')recovery.clearDocument(file.id);
      if(['restored','conflict'].includes(recovered.kind)){$('editor').value=recovered.text;state.draftBase=recovered.base_sha;state.dirty=true;state.documentDraftSafe=true;state.editing=true;state.draftConflict=recovered.kind==='conflict';$('document-notice').hidden=false;$('document-notice').textContent=state.draftConflict?'已恢复本机草稿，但磁盘版本已变化。草稿未覆盖磁盘；请复制草稿，再从磁盘重读后合并。':'已恢复上次未保存的本机草稿。保存后写入知识库；放弃修改会删除此草稿。';}
      $('undo-save').hidden=!state.undo;renderFile();growth?.setView('knowledge');markActive();try{localStorage.setItem(selectionKey('lastFile'),file.id);}catch{}if(innerWidth<=900)document.body.classList.remove('nav-open');$('document-scroll').scrollTop=0;if(line)locateLine(line);
    }catch(error){if(version!==state.readVersion)return;$('file-state').textContent='文档未打开';toast(error.message);}
  }
  function locateLine(line){
    line=Math.max(1,Number(line)||1);if(state.editing){const text=$('editor').value,position=text.split('\n').slice(0,line-1).join('\n').length+(line>1?1:0);$('editor').focus();$('editor').setSelectionRange(position,position);$('editor').scrollTop=Math.max(0,(line-4)*24);return;}
    const blocks=[...$('reader').querySelectorAll('[data-line]')],target=blocks.filter(el=>Number(el.dataset.line)<=line).sort((a,b)=>Number(b.dataset.line)-Number(a.dataset.line))[0]||blocks[0];if(target){target.scrollIntoView({block:'center'});target.classList.add('line-highlight');setTimeout(()=>target.classList.remove('line-highlight'),1800);}updateFileState('已定位到来源第 '+line+' 行附近');
  }
  function renderFile(){
    const file=state.file;if(!file)return;$('editor').readOnly=!file.writable||state.fileMutation==='undo';$('welcome').hidden=true;$('reader').hidden=state.editing;$('editor').hidden=!state.editing;
    $('reader').innerHTML=markdown(state.dirty?$('editor').value:file.text);$('breadcrumb').textContent=file.path||file.title;$('breadcrumb').title=file.path||file.title;
    $('read-mode').disabled=false;$('edit-mode').disabled=!file.writable;$('read-mode').classList.toggle('active',!state.editing);$('edit-mode').classList.toggle('active',state.editing);$('edit-mode').title=file.writable?'编辑 Markdown':'此资料只读';$('save-note').disabled=!file.writable||!state.dirty||state.draftConflict||!!state.fileMutation;
    $('reload-note').hidden=false;renderConnections();updateFileState();updateContext();
  }
  function updateFileState(receipt){const file=state.file;if(!file)return;$('word-count').textContent=String(($('editor').value||'').replace(/\s/g,'').length)+' 字';$('file-state').textContent=receipt||(state.dirty?(state.draftConflict?'草稿与磁盘版本冲突 · 尚未保存':(state.documentDraftSafe?'有未保存的修改 · 本机草稿已保护':'有未保存的修改 · 尚未完成草稿保护')):file.writable?'本地文件 · 修改后保存到 Obsidian 知识库':'本地参考资料 · 只读');$('save-note').disabled=!file.writable||!state.dirty||state.draftConflict||!!state.fileMutation;}
  function connectionLink(link,text='',useAlias=true){
    const object=typeof link==='object'&&link,raw=String(object?(link.target||link.path||link.title||link.name||''):link||''),id=object&&(link.id||link.file_id);
    const normalize=value=>{let result=String(value||'').trim().replace(/^<|>$/g,'').split(' "',1)[0].split('#',1)[0];try{result=decodeURIComponent(result);}catch{}return result;};
    const wiki=raw.match(/^\[\[([^\]|]+)(?:\|([^\]]+))?\]\]$/),target=normalize(wiki?wiki[1]:raw);let alias=object&&(link.alias||link.label)||wiki?.[2]||'';
    if(useAlias&&!alias){const clean=String(text).replace(/```[\s\S]*?```|~~~[\s\S]*?~~~/g,'');for(const match of clean.matchAll(/!?\[\[([^\]\n]+)\]\]|(?<!!)\[([^\]\n]*)\]\(([^)\n]+)\)/g)){const parts=match[1]?.split('|'),source=normalize(parts?parts[0]:match[3]),label=parts?parts.slice(1).join('|'):match[2];if(source===target&&label){alias=label;break;}}}
    let fallback=String(object?(link.title||link.name||target):target);try{fallback=decodeURIComponent(fallback);}catch{}fallback=fallback.split('/').pop().replace(/\.md$/i,'').replace(/--[0-9a-f]{8,64}$/i,'');
    return {id,target,label:alias||fallback||'关联文档'};
  }
  function renderConnections(){const file=state.file;const links=list(file.links),backlinks=list(file.backlinks);$('connections').hidden=state.editing||(!links.length&&!backlinks.length);$('connections').replaceChildren();for(const [title,items,useAlias] of [['文档链接',links,true],['提到此文档',backlinks,false]]){if(!items.length)continue;const h=document.createElement('h3');h.textContent=title;$('connections').append(h);for(const link of items){const button=document.createElement('button'),display=connectionLink(link,file.text,useAlias);button.textContent=display.label;button.addEventListener('click',()=>display.id?openFile(display.id):findLink(display.target));$('connections').append(button);}}}

  function documentAttached(){return !!(state.file&&state.attached&&state.workspaceView==='knowledge'&&state.growthContext.scope!=='advisors');}
  function updateContext(){const file=state.file,attached=documentAttached();$('context-chip').hidden=!attached;$('context-title').textContent=attached?(file.title||file.path)+(state.selection?' · 选区 '+state.selection.length+' 字':''):'';$('clear-selection').hidden=!attached||!state.selection;$('context-title').title=attached?file.path||file.title:'';$('attach-context').hidden=!file||state.attached||state.workspaceView!=='knowledge'||state.growthContext.scope==='advisors';$('allow-write-label').hidden=!attached;$('allow-write').disabled=!attached||!file.writable;if($('allow-write').disabled)$('allow-write').checked=false;}
  async function saveNote(){
    if(!state.file||!state.dirty||state.fileMutation)return;
    if(state.draftConflict){toast('磁盘版本已变化，请先复制草稿并重读合并，不能直接覆盖');return;}
    const id=state.file.id,text=$('editor').value,sha=state.draftBase||state.file.sha,version=state.readVersion;state.fileMutation='save';persistDocumentDraft();updateFileState();
    try{const result=await request('/api/vault/save',{id,text,expected_sha:sha});if(state.file?.id!==id||version!==state.readVersion)return;
      state.file={...state.file,...result,text:result.text??text};state.draftBase=state.file.sha;state.draftConflict=false;if(state.selection&&!state.file.text.includes(state.selection))state.selection='';state.undo=result.undo_id?{id:result.undo_id,sha:state.file.sha}:null;recovery.saveUndo(id,state.undo);state.dirty=$('editor').value!==text;$('document-notice').hidden=true;persistDocumentDraft();$('undo-save').hidden=!state.undo;renderFile();updateFileState(state.dirty?(state.documentDraftSafe?'已保存此版本 · 新修改仍有本机草稿保护':'已保存此版本 · 新修改尚未完成草稿保护'):'已写入磁盘 · '+new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'})+(state.file.sha?' · '+state.file.sha.slice(0,8):''));toast('已保存到本地知识库');
    }catch(error){if(state.file?.id!==id||version!==state.readVersion)return;if(error.status===409){state.draftConflict=true;updateFileState();}$('document-notice').hidden=false;$('document-notice').textContent=(error.status===409?'磁盘文档已被其他应用修改。你的草稿仍保留，请复制草稿，再从磁盘重读合并。':error.message);}
    finally{state.fileMutation=false;if(state.file?.id===id)$('save-note').disabled=!state.file.writable||!state.dirty||state.draftConflict;}
  }
  async function undoSave(){
    if(!state.undo||!await canLeave()||state.fileMutation)return;const undo=state.undo,id=state.file.id,version=state.readVersion,editorAtStart=$('editor').value;state.fileMutation='undo';$('undo-save').disabled=true;$('editor').readOnly=true;
    try{const result=await request('/api/vault/undo',{undo_id:undo.id,expected_sha:undo.sha});recovery.saveUndo(id,null);recovery.markUndone(undo.id);if(state.file?.id===id&&version===state.readVersion){state.undo=null;$('undo-save').hidden=true;if($('editor').value!==editorAtStart||state.dirty){state.dirty=true;state.draftConflict=true;persistDocumentDraft();$('document-notice').hidden=false;$('document-notice').textContent='磁盘已撤销，上次请求期间的新输入仍保留。请复制草稿，再从磁盘重读合并。';}else{recovery.clearDocument(id);await openFile(result.id||id,true);}}toast('已撤销上次保存，磁盘内容已恢复');}
    catch(error){toast(error.message);}finally{state.fileMutation=false;$('undo-save').disabled=false;$('editor').readOnly=!state.file?.writable;updateFileState();}
  }
  async function checkFileChange(){
    if(!state.file||state.filePolling||state.fileMutation||document.hidden)return;state.filePolling=true;const id=state.file.id,sha=state.file.sha;
    try{const stat=await request(query('/api/vault/stat',{id}));if(state.file?.id!==id||state.file.sha!==sha||stat.sha===sha)return;
      if(state.dirty){state.draftConflict=true;updateFileState();$('document-notice').hidden=false;$('document-notice').textContent='外部文件已更新，草稿已保留。请复制你的修改，再从磁盘重读后合并。';return;}
      const next=await request(query('/api/vault/read',{id}));if(state.file?.id!==id||state.file.sha!==sha||state.dirty)return;
      const scroll=$('document-scroll').scrollTop,caret=$('editor').selectionStart;state.file=next;state.draftBase=next.sha;state.draftConflict=false;state.selection='';state.undo=null;recovery.saveUndo(id,null);$('undo-save').hidden=true;$('editor').value=next.text||'';renderFile();$('document-scroll').scrollTop=scroll;if(state.editing)$('editor').setSelectionRange(Math.min(caret,$('editor').value.length),Math.min(caret,$('editor').value.length));$('document-notice').hidden=true;updateFileState('已同步外部修改 · '+new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}));
    }catch{/* A transient stat failure must not replace an open draft. */}finally{state.filePolling=false;}
  }
  function captureSelection(){
    if(!state.file)return;let text='';if(state.editing){const editor=$('editor');text=editor.value.slice(editor.selectionStart,editor.selectionEnd);}else{const sel=window.getSelection();if(sel?.anchorNode&&$('reader').contains(sel.anchorNode)&&$('reader').contains(sel.focusNode))text=sel.toString();}
    if(!text||text.length>20000)return;
    if(!state.file.text.includes(text)){if(!state.dirty)toast('此选区跨越格式标记，请在编辑模式选择 Markdown 原文');return;}state.selection=text;state.attached=true;updateContext();
  }
  async function searchLibrary(){const q=$('search').value.trim(),version=++state.searchVersion;$('tree').hidden=!!q;$('search-results').hidden=!q;if(!q){$('search-status').textContent='';return;}$('search-status').textContent='搜索本地内容…';
    try{const data=await request(query('/api/vault/search',{q}));if(version!==state.searchVersion)return;const results=list(data.results);$('search-results').replaceChildren();$('search-status').textContent=(results.length?'找到 '+results.length+' 条内容':'没有找到匹配内容')+(data.scan?.complete===false?' · 已扫描 '+(data.scan.scanned_files||0)+' 份，尚未覆盖全部资料':'' );for(const result of results){const button=document.createElement('button');button.className='search-result';button.innerHTML='<strong>'+escape(result.title||result.path)+'</strong><small>'+escape(result.path)+(result.line?' · 第 '+Number(result.line)+' 行':'')+'</small><p>'+escape(result.excerpt||'')+'</p>';button.addEventListener('click',()=>openFile(result.id||result.file_id,false,result.line));$('search-results').append(button);}if(data.scan?.has_more){const more=document.createElement('button');more.className='continue-search';more.textContent='继续搜索剩余资料';more.addEventListener('click',()=>{more.disabled=true;searchLibrary();});$('search-results').append(more);}else if(data.scan?.complete===false){const hint=document.createElement('p');hint.className='tree-error';hint.textContent=data.scan.reason||'部分文档未能读取，结果不代表全部资料';$('search-results').append(hint);}}
    catch(error){if(version===state.searchVersion)$('search-status').textContent=error.message;}
  }
  async function findLink(raw){
    const linkVersion=++state.linkEpoch,documentVersion=state.readVersion;const stillCurrent=()=>linkVersion===state.linkEpoch&&documentVersion===state.readVersion;
    let relative=String(raw||'').split('#')[0];try{relative=decodeURIComponent(relative);}catch{}
    if(state.file&&/\.(md|markdown|txt)$/i.test(relative)&&!relative.includes(':')){
      const parts=relative.startsWith('/')?[]:String(state.file.path||'').split('/').slice(0,-1);let valid=true;
      for(const part of relative.split('/')){if(!part||part==='.')continue;if(part==='..'){if(!parts.length){valid=false;break;}parts.pop();}else parts.push(part);}
      if(valid){const id=state.file.root_id+':'+parts.map(encodeURIComponent).join('/');try{await request(query('/api/vault/stat',{id}));if(stillCurrent())await openFile(id);return;}catch{/* Fall through to title lookup for wiki-style paths. */}}
    }
    let q=String(raw||'').split('#')[0];try{q=decodeURIComponent(q);}catch{}q=q.replace(/\.md$/i,'');if(!q){toast('此链接指向当前文档内的标题');return;}
    try{const data=await request(query('/api/vault/search',{q}));if(!stillCurrent())return;const matches=list(data.results);const exact=matches.find(r=>r.path===raw||r.title===q||r.title===q.split('/').pop());if(exact||matches.length===1)await openFile((exact||matches[0]).id||(exact||matches[0]).file_id);else{$('search').value=q;await searchLibrary();document.body.classList.remove('nav-hidden');if(innerWidth<=900)document.body.classList.add('nav-open');if(!matches.length)toast('还没有找到这个链接对应的文档');}}
    catch(error){toast(error.message);}
  }
  async function loadModels(){try{const data=await request('/api/models');const models=list(data.models).filter(m=>m.verified===true);$('model-select').replaceChildren();for(const model of models){const option=document.createElement('option');option.value=model.id;option.textContent=model.label||model.id;option.title='实际验证：'+(model.verified_at||'已验证');$('model-select').append(option);}if(models.length){state.selectedModel=models.some(m=>m.id===data.default_id)?data.default_id:models[0].id;$('model-select').value=state.selectedModel;notice('可直接说“记下这个想法”或“保存这次复盘” · 检索片段会提供给所选模型');}else{const option=document.createElement('option');option.value='';option.textContent='暂无已验证模型';$('model-select').append(option);notice('尚无已验证模型，请在连接设置完成验证后刷新。',true);}updateChatControls();}catch(error){notice(error.message,true);}}
  async function loadSessions(more=false){
    const version=++state.sessionsVersion,cursor=more?state.sessionsPage?.next_cursor:null;$('more-sessions').disabled=true;
    try{const data=await request(query('/api/workspace/sessions',cursor?{before:cursor}:{}));if(version!==state.sessionsVersion)return;
      state.sessions=mergeWorkspaceMessages(state.sessions,list(data.sessions),!more);if(more||!state.sessionsExpanded)state.sessionsPage=data.pagination||null;if(more)state.sessionsExpanded=true;
      const select=$('session-select');select.replaceChildren();const blank=document.createElement('option');blank.value='';blank.textContent='新对话';select.append(blank);for(const session of state.sessions){const option=document.createElement('option');option.value=session.id||session.session_id;option.textContent=session.title||session.name||'未命名对话';select.append(option);}select.value=state.session;if(state.session&&select.value!==state.session){const option=document.createElement('option');option.value=state.session;option.textContent='当前对话';select.append(option);select.value=state.session;}
      $('more-sessions').hidden=!state.sessionsPage?.has_more;
    }catch(error){if(version===state.sessionsVersion)notice('历史对话暂未读取：'+error.message,true);}finally{if(version===state.sessionsVersion)$('more-sessions').disabled=false;}
  }
  const activeJob=()=>state.jobs.find(j=>['queued','running','pending','selecting','streaming','cancelling'].includes(j.status));
  function updateChatControls(){const busy=state.sending||!!activeJob();$('send').disabled=busy||!state.selectedModel||!$('message').value.trim();$('stop-chat').hidden=!activeJob();$('model-select').disabled=busy;$('session-select').disabled=state.sending;$('new-chat').disabled=state.sending;}
  function referenceHTML(data){
    const context=data.reply_context||data.answer_context||data,advisors=list(data.advisor_selection||context.advisor_selection),seen=new Set();
    const refs=list(data.references||context.references||context.contexts).filter(r=>{const key=[r.id||r.file_id||r.source_id||r.path,r.line,r.sha].join('|');if(seen.has(key))return false;seen.add(key);return true;});let html='';
    if(advisors.length)html+='<div class="advisors">本轮顾问 · '+advisors.map(a=>'<span title="'+escape(a.reason||'')+'">'+escape(a.title||a.name)+'</span>').join(' · ')+'</div>';
    const reads=list(data.advisor_reads||context.advisor_reads);if(reads.length)html+='<details><summary>实际读取的方法 · '+reads.length+'</summary>'+reads.map(a=>'<p>'+escape(a.title||a.name)+' · '+escape((a.sha||'').slice(0,8))+(a.skill_id?' <button class="text-button" data-open="'+escape(a.skill_id)+'">打开方法</button>':'')+'</p>').join('')+'</details>';
    if(advisors.some(a=>a.reason))html+='<details><summary>顾问采用理由</summary>'+advisors.map(a=>'<p><strong>'+escape(a.title||a.name)+'</strong> · '+escape(a.reason||'')+'</p>').join('')+'</details>';
    if(refs.length)html+='<div class="evidence-chips">'+refs.map(r=>{const id=r.id||r.file_id||r.source_id;return id?'<button data-open="'+escape(id)+'" data-line="'+Number(r.line||1)+'" title="'+escape(r.path||r.title||'')+'">▤ '+escape(r.title||r.path||'资料')+'</button>':'<span>'+escape(r.title||r.path||'资料')+'</span>';}).join('')+'</div>';
    const receipts=list(data.write_receipts||context.write_receipts);
    if(receipts.length)html+='<div class="write-receipts">'+receipts.map(r=>{const verified=r.status==='verified',undone=r.undo_id&&(state.undoneReceipts.has(r.undo_id)||recovery.isUndone(r.undo_id)),label=undone?'已撤销写入':verified?'已写入并回读核对':r.status==='cancelled'?'写入已取消':'写入结果待核实';return '<div><span>'+label+' '+escape(r.title||r.id||'文档')+'</span> '+(r.id?'<button data-open="'+escape(r.id)+'">查看</button> ':'')+(verified&&r.undo_id&&r.sha?'<button data-undo="'+escape(r.undo_id)+'" data-sha="'+escape(r.sha)+'"'+(undone?' disabled':'')+'>'+(undone?'已撤销':'撤销')+'</button>':'')+'</div>';}).join('')+'</div>';
    return html;
  }
  async function undoReceipt(button){
    button.disabled=true;try{const receipt=await request('/api/vault/undo',{undo_id:button.dataset.undo,expected_sha:button.dataset.sha});state.undoneReceipts.add(button.dataset.undo);recovery.markUndone(button.dataset.undo);button.textContent='已撤销';state.chatSignature='';renderConversation();await checkFileChange();toast('已撤销此次 AI 写入');}catch(error){button.disabled=false;toast('未覆盖现有文件：'+error.message);}
  }
  function renderConversation(){
    const signature=JSON.stringify([state.messages,state.jobs,state.messagesPage,state.historyLoading]);if(signature===state.chatSignature)return;const selection=window.getSelection();if(selection&&!selection.isCollapsed&&$('conversation').contains(selection.anchorNode))return;state.chatSignature=signature;const el=$('conversation'),detailState=new Map([...el.querySelectorAll('details')].map(d=>[d.querySelector('summary')?.textContent,d.open])),nearBottom=el.scrollHeight-el.scrollTop-el.clientHeight<100,oldTop=el.scrollTop;
    if(!state.messages.length&&!state.jobs.length){el.innerHTML='<div class="chat-welcome"><span class="small-label">从一个真实问题开始</span><h3>你想一起想清楚什么？</h3><p>讨论一段内容，梳理最近的想法，<br>或把一个方向拆成下一步。</p><div class="prompt-list"><button data-prompt="请结合我的方向、近期记录与当前目标，帮我想清楚今天应该优先推进什么。">确定今天的重点 <span>↗</span></button><button data-prompt="请结合我的近期记录，帮我做一次阶段复盘。">复盘最近的进展 <span>↗</span></button><button data-prompt="我有一个新想法，想和顾问一起推敲：">一起推敲一个想法 <span>↗</span></button></div></div>';return;}
    let html=(state.messagesPage?.has_more?'<button class="text-button earlier-messages" data-earlier-messages'+(state.historyLoading?' disabled':'')+'>'+(state.historyLoading?'正在读取更早消息…':'查看更早消息')+'</button>':'')+state.messages.map(m=>{if(m.role==='user')return '<article class="message user">'+escape(m.text||m.content||'')+'</article>';const context=m.reply_context||m.answer_context||m,updates=list(context.analysis_updates).filter(u=>['summary','commentary'].includes(u.kind)&&u.text);return '<article class="message assistant"><div class="message-label"><span class="mini-avatar">✳</span>Codex'+(context.actual_model?' · '+escape(context.actual_model):'')+'</div>'+referenceHTML(m)+'<div class="markdown">'+markdown(m.text||m.content||'')+'</div>'+(updates.length?'<details><summary>本轮公开分析摘要</summary>'+updates.map(u=>'<p>'+escape(u.text)+'</p>').join('')+'</details>':'')+'</article>';}).join('');
    for(const job of state.jobs){const live=['queued','running','pending','selecting','streaming','cancelling'].includes(job.status);const failed=['failed','error','cancelled','canceled','interrupted'].includes(job.status);if(!live&&!failed)continue;const updates=list(job.analysis_updates).filter(u=>['summary','commentary'].includes(u.kind)&&u.text),events=list(job.progress_events);html+='<section class="live-process"><strong>'+escape(live?(job.status==='cancelling'?'正在停止本轮任务':'Codex 正在处理'):job.status==='failed'||job.status==='error'?'本轮未完成':'本轮已停止')+'</strong>'+referenceHTML(job)+(events.length?'<p class="progress-line">'+escape(events[events.length-1].text||'')+'</p>':'')+(updates.length?'<details open><summary>实时分析摘要</summary><div class="analysis-summary">'+updates.slice(-6).map(u=>'<p>'+escape(u.text)+'</p>').join('')+'</div></details>':live?'<p>等待模型返回公开摘要或执行动态。</p>':'')+(job.error?'<p class="error">'+escape(typeof job.error==='string'?job.error:JSON.stringify(job.error))+'</p>':'')+'</section>';if(job.response_preview)html+='<article class="message assistant"><div class="stream-label">'+(live?'正在生成回答':'停止前的部分回答')+'</div><div class="markdown">'+markdown(job.response_preview)+'</div></article>';}
    el.innerHTML=html;for(const d of el.querySelectorAll('details')){const key=d.querySelector('summary')?.textContent;if(detailState.has(key))d.open=detailState.get(key);}if(nearBottom)el.scrollTop=el.scrollHeight;else el.scrollTop=oldTop;
  }
  function scheduleChatPoll(){
    clearTimeout(chatTimer);if(!state.session)return;
    chatTimer=setTimeout(()=>{if(document.hidden)scheduleChatPoll();else pollChat();},workspacePollDelay(!!activeJob(),state.chatFailures));
  }
  async function pollChat(){
    if(!state.session)return;const session=state.session,epoch=state.chatEpoch;if(state.chatRequest?.epoch===epoch)return;
    clearTimeout(chatTimer);const ticket={epoch,controller:new AbortController()};state.chatRequest=ticket;
    try{const data=await request(query('/api/workspace/status',{session_id:session}),undefined,{signal:ticket.controller.signal});if(state.session!==session||state.chatEpoch!==epoch)return;
      const hadActive=!!activeJob();state.messages=mergeWorkspaceMessages(state.messages,list(data.messages));state.jobs=list(data.jobs);if(!state.historyExpanded)state.messagesPage=data.messages_pagination||null;state.chatFailures=0;renderConversation();updateChatControls();if(hadActive&&!activeJob()){checkFileChange();growth?.load();}
    }catch(error){if(state.session===session&&state.chatEpoch===epoch&&error.name!=='AbortError'){state.chatFailures++;notice('暂时无法同步对话，稍后自动重试：'+error.message,true);}}
    finally{if(state.chatRequest===ticket){state.chatRequest=null;scheduleChatPoll();}}
  }
  async function loadEarlierMessages(){
    if(state.historyLoading||!state.messagesPage?.has_more)return;const session=state.session,epoch=state.chatEpoch,cursor=state.messagesPage.next_cursor;state.historyLoading=true;renderConversation();
    const el=$('conversation'),height=el.scrollHeight,top=el.scrollTop;
    try{const data=await request(query('/api/workspace/status',{session_id:session,messages_before:cursor}));if(state.session!==session||state.chatEpoch!==epoch)return;state.messages=mergeWorkspaceMessages(state.messages,list(data.messages),true);state.messagesPage=data.messages_pagination||null;state.historyExpanded=true;state.historyLoading=false;renderConversation();el.scrollTop=top+el.scrollHeight-height;}
    catch(error){if(state.chatEpoch===epoch)notice('更早消息读取失败：'+error.message,true);}finally{if(state.chatEpoch===epoch){state.historyLoading=false;renderConversation();}}
  }
  async function selectSession(id,initial=false){
    if(state.sending)return;if(!initial&&!persistChatDraft()&&$('message').value){$('session-select').value=state.session;notice('未发送输入尚未保护，请先复制或清空后切换对话。',true);return;}clearTimeout(chatTimer);state.chatRequest?.controller.abort();state.chatRequest=null;state.chatEpoch++;state.session=id;state.messages=[];state.jobs=[];state.messagesPage=null;state.historyExpanded=false;state.historyLoading=false;state.chatFailures=0;state.chatSignature='';$('session-select').value=id;$('allow-write').checked=false;restoreChatDraft();try{localStorage.setItem(selectionKey('session'),id);}catch{}renderConversation();updateChatControls();if(id)await pollChat();
  }
  async function sendMessage(event){
    event.preventDefault();const message=$('message').value.trim();if(!message||state.sending||activeJob()||!state.selectedModel)return;if(documentAttached()&&state.dirty){notice('当前文档有未保存的修改，请先保存，或移除文档后发送。',true);return;}if(state.growthContext.scope==='document'&&!documentAttached()){notice('本轮范围是当前文档，请先打开并附上文档，或更改资料范围。',true);return;}state.sending=true;persistChatDraft();updateChatControls();notice('正在提交问题…');const originalSession=state.session,epoch=state.chatEpoch;
    const payload={session_id:state.session||undefined,message,model:state.selectedModel,...growthRequestContext(state.growthContext,{attached:documentAttached(),id:state.file?.id,selection:state.selection,allow_write:$('allow-write').checked})};
    try{const data=await request('/api/workspace/send',payload);const id=typeof data.session==='string'?data.session:data.session?.id||data.session?.session_id||data.session_id||data.job?.session_id;if(!id)throw new Error('发送未返回会话标识，请刷新确认是否已提交');if(state.chatEpoch!==epoch)return;
      recovery.saveChat(originalSession,'');state.session=id;state.jobs=data.job?[data.job]:[];if($('message').value.trim()===message)$('message').value='';persistChatDraft();$('chat-draft-state').hidden=!$('message').value;$('chat-draft-state').textContent='继续输入的内容已留作下一条草稿';$('allow-write').checked=false;try{localStorage.setItem(selectionKey('session'),id);}catch{}await loadSessions();await pollChat();notice('已发送 · 实际采用的顾问与资料会在回复中显示');$('conversation').scrollTop=$('conversation').scrollHeight;
    }catch(error){if(state.chatEpoch===epoch)notice(error.message+'；输入仍保留，若连接中断请先核对会话是否已收到。',true);}finally{state.sending=false;updateChatControls();}
  }
  function setupDivider(id,property,min,max,reverse){const bar=$(id);let start=0,initial=0;const clamp=v=>Math.max(min,Math.min(max,v));bar.addEventListener('pointerdown',event=>{if(innerWidth<=900&&id==='left-divider')return;start=event.clientX;initial=parseFloat(getComputedStyle(document.documentElement).getPropertyValue(property));bar.setPointerCapture(event.pointerId);bar.classList.add('dragging');document.body.style.userSelect='none';});bar.addEventListener('pointermove',event=>{if(!bar.classList.contains('dragging'))return;const value=clamp(initial+(event.clientX-start)*(reverse?-1:1));document.documentElement.style.setProperty(property,value+'px');});const stop=()=>{bar.classList.remove('dragging');document.body.style.userSelect='';};bar.addEventListener('pointerup',stop);bar.addEventListener('pointercancel',stop);bar.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight'].includes(event.key))return;event.preventDefault();const current=parseFloat(getComputedStyle(document.documentElement).getPropertyValue(property)),delta=(event.key==='ArrowRight'?15:-15)*(reverse?-1:1);document.documentElement.style.setProperty(property,clamp(current+delta)+'px');});}
  $('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(searchLibrary,200);});
  $('connection-settings').addEventListener('click',()=>{$('connection-dialog').showModal();});
  $('close-connection').addEventListener('click',()=>{$('connection-dialog').close();});
  $('refresh-models').addEventListener('click',async()=>{$('refresh-models').disabled=true;try{await loadModels();toast('已刷新保存的模型验证状态，没有发起模型调用');}finally{$('refresh-models').disabled=false;}});
  $('refresh-library').addEventListener('click',()=>loadRoots().catch(error=>toast(error.message)));
  $('read-mode').addEventListener('click',()=>{state.editing=false;$('reader').innerHTML=markdown(state.dirty?$('editor').value:state.file.text);$('reader').hidden=false;$('editor').hidden=true;$('read-mode').classList.add('active');$('edit-mode').classList.remove('active');renderConnections();});
  $('edit-mode').addEventListener('click',()=>{if(!state.file?.writable)return;state.editing=true;renderFile();$('editor').focus();});
  $('editor').addEventListener('input',()=>{state.dirty=$('editor').value!==state.file?.text;state.documentDraftSafe=false;if(!state.dirty){state.draftConflict=false;state.draftBase=state.file.sha;}updateFileState();clearTimeout(draftTimer);draftTimer=setTimeout(persistDocumentDraft,180);});
  $('save-note').addEventListener('click',saveNote);$('undo-save').addEventListener('click',undoSave);
  $('reload-note').addEventListener('click',async()=>{if(await canLeave())await openFile(state.file.id,true);});
  $('clear-selection').addEventListener('click',()=>{state.selection='';updateContext();});$('reader').addEventListener('mouseup',captureSelection);$('editor').addEventListener('mouseup',captureSelection);$('editor').addEventListener('keyup',event=>{if(event.shiftKey)captureSelection();});$('remove-context').addEventListener('click',()=>{state.attached=false;updateContext();});$('attach-context').addEventListener('click',()=>{state.attached=true;updateContext();});
  $('model-select').addEventListener('change',()=>{state.selectedModel=$('model-select').value;updateChatControls();});$('message').addEventListener('input',()=>{const protectedInput=persistChatDraft();if(protectedInput){$('chat-draft-state').hidden=!$('message').value;$('chat-draft-state').textContent='未发送输入已保存在本机';}updateChatControls();});$('chat-form').addEventListener('submit',sendMessage);
  $('more-sessions').addEventListener('click',()=>loadSessions(true));$('new-chat').addEventListener('click',()=>selectSession(''));$('session-select').addEventListener('change',event=>selectSession(event.target.value));
  $('stop-chat').addEventListener('click',async()=>{const job=activeJob();if(!job)return;$('stop-chat').disabled=true;try{await request('/api/workspace/cancel',{id:job.id});await pollChat();notice('已请求停止，已返回的内容会保留');}catch(error){notice(error.message,true);}finally{$('stop-chat').disabled=false;}});
  $('new-note').addEventListener('click',()=>{$('new-note-error').textContent='';$('new-note-dialog').showModal();$('new-note-path').focus();});$('close-new-note').addEventListener('click',()=>$('new-note-dialog').close());
  async function createNote(event){event.preventDefault();if($('create-note').disabled||!await canLeave()||$('create-note').disabled)return;const version=state.readVersion,editorAtStart=$('editor').value;let path=$('new-note-path').value.trim();if(!path)return;if(!path.endsWith('.md'))path+='.md';$('create-note').disabled=true;try{const title=path.split('/').pop().replace(/\.md$/,'');const data=await request('/api/vault/create',{root_id:$('new-note-root').value,path,text:'# '+title+'\n\n'});$('new-note-dialog').close();$('new-note-path').value='';await loadRoots();if(version===state.readVersion&&!state.dirty&&$('editor').value===editorAtStart){const id=data.id||data.file_id;await openFile(id);if(state.file?.id===id){state.editing=true;renderFile();$('editor').focus();}}toast('笔记已创建到本地，可从左侧目录打开');}catch(error){$('new-note-error').textContent=error.message;}finally{$('create-note').disabled=false;}}
  $('new-note-form').addEventListener('submit',createNote);
  $('toggle-nav').addEventListener('click',()=>{if(innerWidth<=900)document.body.classList.toggle('nav-open');else document.body.classList.toggle('nav-hidden');});$('toggle-chat').addEventListener('click',()=>document.body.classList.toggle('chat-hidden'));
  document.addEventListener('click',event=>{const earlier=event.target.closest('[data-earlier-messages]');if(earlier){loadEarlierMessages();return;}const undo=event.target.closest('[data-undo]');if(undo){undoReceipt(undo);return;}const link=event.target.closest('[data-link]');if(link){event.preventDefault();findLink(link.dataset.link);}const ref=event.target.closest('[data-open]');if(ref)openFile(ref.dataset.open,false,ref.dataset.line);const prompt=event.target.closest('[data-prompt]');if(prompt){$('message').value=prompt.dataset.prompt;persistChatDraft();$('message').focus();updateChatControls();}});
  document.addEventListener('keydown',event=>{if(!(event.metaKey||event.ctrlKey))return;if(event.key.toLowerCase()==='k'){event.preventDefault();document.body.classList.remove('nav-hidden');if(innerWidth<=900)document.body.classList.add('nav-open');$('search').focus();}if(event.key.toLowerCase()==='s'&&state.workspaceView==='knowledge'){event.preventDefault();saveNote();}if(event.key==='Enter'&&document.activeElement===$('message')){event.preventDefault();$('chat-form').requestSubmit();}});
  window.addEventListener('pagehide',()=>{persistDocumentDraft();persistChatDraft();});
  window.addEventListener('beforeunload',event=>{persistChatDraft();if(state.dirty&&!persistDocumentDraft()){event.preventDefault();event.returnValue='';}});
  document.addEventListener('visibilitychange',()=>{if(document.hidden){persistDocumentDraft();persistChatDraft();clearTimeout(chatTimer);}else{checkFileChange();pollChat();}});
  setupDivider('left-divider','--nav-width',180,360,false);setupDivider('right-divider','--chat-width',310,650,true);
  if(innerWidth<=640)document.body.classList.add('chat-hidden');
  async function start(){$('message').disabled=true;try{
    growth=createGrowthWorkspace({request,viewChanged(view){if(state.dirty)persistDocumentDraft();state.workspaceView=view;updateContext();},contextChanged(context){state.growthContext=context;updateContext();},prefill(text){const current=$('message').value.trim();$('message').value=current?current+'\n\n'+text:text;persistChatDraft();updateChatControls();$('message').focus();if(innerWidth<=640)document.body.classList.remove('chat-hidden');}});
    const app=await request('/api/bootstrap');state.token=app.token;let storage=null;try{storage=localStorage;}catch{}state.storageScope=app.data_id||'local';recovery=createWorkspaceDraftStore(storage,state.storageScope);
    await Promise.all([loadRoots(),loadModels(),loadSessions()]);let lastFile='',session='';try{lastFile=localStorage.getItem(selectionKey('lastFile'))||'';session=localStorage.getItem(selectionKey('session'))||'';}catch{}
    if(lastFile)await openFile(lastFile);else if(state.roots.some(root=>root.id==='upward'))await openFile('upward:'+encodeURIComponent('首页.md'));await selectSession(session,true);if(session)await loadSessions();growth.setView(state.dirty?'knowledge':'daily');
  }catch(error){$('connection-label').textContent='连接未完成';notice(error.message,true);toast(error.message);}finally{$('message').disabled=false;}}
  start();setInterval(checkFileChange,3000);
})();
