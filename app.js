import { MotionEngine, AXES } from './lib/motion.js';
import { RobotView } from './lib/robot-view.js';
import { HeadingController } from './lib/heading.js';

const $ = id => document.getElementById(id);
const video = $('face-video');
const heading = new HeadingController();
const eventLog = [];
const sourceNames = {simulator:'模拟回执',local:'手动操作',injected:'本地注入',replay:'回放',board:'板端信号',cloud:'云端信号',pc:'PC 信号',bridge:'本地桥接'};
const eventNames = {action_started:'动作开始',stage_started:'阶段开始',simulated_arrival:'模拟到位',stage_completed:'阶段完成',action_completed:'模拟动作完成',interrupted:'动作被中断',paused:'七轴暂停',resumed:'七轴继续',stopped:'七轴停止',reset_started:'七轴归中',reset_completed:'模拟归中完成',pose_started:'手动关节目标',pose_completed:'模拟姿态到位'};
let catalog, engine, view, kind='actions', selected, currentAction=null;
let faceOwner='idle', faceGeneration=0, pausedVideo=false, resumeOnVisible=false, connected=false, knownConnection=false;
let lastFrame=performance.now(), lastUI=0, uiStageKey='';
let logFilter='all', activeInterimRow=null, activeAssistantRow=null, activeAssistantTs=0, lastUserCaptionBadge=null, boardVoiceActive=false, poseSource='local';
const systemStatus={};
let serviceCpu={};
let lastBoardOnline=null;
function formatShare(pct){return (pct<10?pct.toFixed(1):Math.round(pct))+'%';}
function formatUptime(s){if(s<60)return s+' 秒';if(s<3600)return Math.floor(s/60)+' 分';return Math.floor(s/3600)+' 小时 '+Math.floor(s%3600/60)+' 分';}
// A slider being dragged or awaiting the board's answer is not overwritten by status pushes.
const levelBusy={volume:false,brightness:false,fan:false};

function showError(message, {startup=false}={}) { $('error').hidden=false; $('error').textContent=message; $('startup-recovery').hidden=!startup; }
function clearError() { $('error').hidden=true; $('startup-recovery').hidden=true; }

// Collapsed side panels count what arrived while hidden; expanding clears the count.
const unread={};
function bumpUnread(cardId) {
  if (!$(cardId)?.classList.contains('collapsed')) return;
  unread[cardId]=(unread[cardId]||0)+1;
  const badge=$(cardId+'-unread');
  if (badge) { badge.hidden=false; badge.textContent=unread[cardId]>99?'99+':String(unread[cardId]); }
}
function clearUnread(cardId) {
  unread[cardId]=0;
  const badge=$(cardId+'-unread'); if (badge) badge.hidden=true;
}

function shouldShowRow(source) {
  if (logFilter === 'all') return true;
  if (logFilter === 'board') return source !== 'simulator';
  if (logFilter === 'simulator') return source === 'simulator';
  return true;
}

function log(event, description) {
  const entry={...event,logged_at:new Date().toISOString(),description};
  eventLog.push(entry); if(eventLog.length>300) eventLog.shift();
  const list = $('event-list');
  if (!list) return;
  if(list.querySelector('.empty')) list.replaceChildren();
  const row=document.createElement('div');
  const isSim = event.source === 'simulator';
  row.className='event-row' + (isSim ? ' simulator-row' : '');
  row.dataset.source = event.source || 'local';
  if (!shouldShowRow(row.dataset.source)) row.hidden = true;
  const stamp=document.createElement('time'); stamp.textContent=new Date(entry.logged_at).toLocaleTimeString('zh-CN',{hour12:false});
  const source=document.createElement('span');
  source.className='event-source '+(isSim?'simulator':(event.source==='board'?'board':''));
  source.textContent=sourceNames[event.source]||event.source||'本地';
  const text=document.createElement('span');text.className='event-description';text.textContent=description;
  row.append(stamp,source,text);list.prepend(row);bumpUnread('log-card');
  while(list.children.length>120) list.lastElementChild.remove();
}

function appendCaption(event) {
  const list = $('caption-list');
  if (!list) return;
  if (list.querySelector('.empty')) list.replaceChildren();
  const isInterim = event.kind === 'user_interim';
  const isUser = event.kind === 'user_interim' || event.kind === 'user_final';

  if (isUser && activeInterimRow) {
    activeAssistantRow = null;
    const textEl = activeInterimRow.querySelector('.caption-text');
    if (textEl) textEl.textContent = event.text;
    if (!isInterim) {
      activeInterimRow.classList.remove('interim');
      const roleEl = activeInterimRow.querySelector('.caption-role');
      if (roleEl) roleEl.textContent = '用户语音 (ASR Final)';
      activeInterimRow = null;
    }
    return;
  }

  // Merge streaming assistant delta tokens into the current assistant bubble
  const nowMs = Date.now();
  if (!isUser && activeAssistantRow && (nowMs - activeAssistantTs) < 8000) {
    activeAssistantTs = nowMs;
    const textEl = activeAssistantRow.querySelector('.caption-text');
    if (textEl) {
      const prev = textEl.textContent || '';
      const incomingText = event.text || '';
      textEl.textContent = incomingText.startsWith(prev) ? incomingText : (prev + incomingText);
    }
    return;
  }

  const row = document.createElement('div');
  row.className = 'caption-item ' + (isUser ? 'user' : 'assistant') + (isInterim ? ' interim' : '');
  const meta = document.createElement('div');
  meta.className = 'caption-meta';
  const role = document.createElement('span');
  role.className = 'caption-role';
  role.textContent = event.detail === 'translation_source' ? '同传 · 原文' : event.detail === 'translation' ? '同传 · 译文'
    : isUser ? (isInterim ? '用户正在说话 (ASR Interim)…' : '用户语音 (ASR Final)') : '机器人回复 (Assistant)';
  const timeEl = document.createElement('time');
  timeEl.textContent = new Date().toLocaleTimeString('zh-CN', {hour12: false});
  const badge = document.createElement('span');
  badge.className = 'caption-emotion-tag';
  badge.hidden = true;
  if (event.emotion || event.action_id) {
    badge.hidden = false;
    badge.textContent = [event.emotion, event.action_id].filter(Boolean).join(' → ');
  }
  meta.append(role, timeEl, badge);
  const body = document.createElement('div');
  body.className = 'caption-text';
  body.textContent = event.text || '';
  row.append(meta, body);
  list.prepend(row);
  bumpUnread('captions-card');
  while (list.children.length > 50) list.lastElementChild.remove();

  if (isUser) {
    activeAssistantRow = null;
    lastUserCaptionBadge = badge;
    if (isInterim) activeInterimRow = row;
  } else {
    activeInterimRow = null;
    activeAssistantRow = row;
    activeAssistantTs = nowMs;
  }
}

// ---- Board switcher: scan results, manual IP, and the password prompt ----
let boardListing={current:null,boards:[],scanned_at:null}, authHost=null;
function boardLabel(b){return [b.name, (b.model||'').replace(/^Rockchip\s+/,'')].filter(Boolean).join(' · ');}
function renderBoards(listing) {
  boardListing=listing;
  const list=$('board-list');list.replaceChildren();
  const rows=[...listing.boards];
  if(listing.current && !rows.some(b=>b.host===listing.current)) rows.unshift({host:listing.current,state:'current'});
  if(!rows.length){const p=document.createElement('p');p.className='empty';p.textContent=listing.scanned_at?'没有发现机器人，可在下方手动输入 IP':'扫描中…';list.append(p);return;}
  for(const b of rows){
    const item=document.createElement('button');item.type='button';item.className='board-item'+(b.host===listing.current?' current':'');
    const host=document.createElement('strong');host.textContent=b.host;
    const meta=document.createElement('span');meta.className='board-meta';meta.textContent=boardLabel(b)||(b.state==='current'?'当前选择（本次扫描未发现）':'');
    const badge=document.createElement('span');badge.className='board-badge '+b.state;
    badge.textContent={robot:'机器人',auth:'需要密码',current:'当前'}[b.state]||b.state;
    item.append(host,badge,meta);item.onclick=()=>selectBoard(b.host);list.append(item);
  }
}
async function loadBoards(rescan=false) {
  if(rescan){$('board-list').replaceChildren(Object.assign(document.createElement('p'),{className:'empty',textContent:'扫描局域网中…'}));}
  try{
    let listing=await (await fetch('/api/boards')).json();renderBoards(listing);
    if(rescan || !listing.scanned_at || Date.now()/1000-listing.scanned_at>60){$('board-rescan').disabled=true;listing=await (await fetch('/api/boards/scan')).json();renderBoards(listing);}
  }catch(error){menuError('扫描失败：'+error.message);}
  finally{$('board-rescan').disabled=false;}
}
function menuError(text){$('board-menu-error').hidden=!text;$('board-menu-error').textContent=text||'';}
function toggleBoardMenu(open=$('board-menu').hidden){
  $('board-menu').hidden=!open;$('board-host-chip').setAttribute('aria-expanded',String(open));
  if(open){menuError('');loadBoards();}
}
function openAuth(host, message='') {
  authHost=host;$('board-auth-target').textContent='root@'+host;
  $('board-auth-error').hidden=!message;$('board-auth-error').textContent=message;
  $('board-auth-password').value='';
  if(!$('board-auth').open) $('board-auth').showModal();
  $('board-auth-password').focus();
}
function resetBoardView() {
  for(const key of Object.keys(systemStatus)) delete systemStatus[key];
  serviceCpu={};
  for(const id of ['board-services','board-turn-phase','board-system','board-people']) $(id).textContent='-';
  $('board-system').classList.remove('warn');
  activeInterimRow=null;activeAssistantRow=null;$('caption-list').replaceChildren();
  perfTurns.clear();renderPerf();
  resetAll();
}
async function selectBoard(host, password) {
  const previous=boardListing.current;
  menuError('');$('board-host').textContent=host+' · 连接中';
  try{
    const resp=await fetch('/api/board/select',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(password===undefined?{host}:{host,password})});
    const data=await resp.json();
    if(resp.status===401){
      $('board-host').textContent=(previous||'主板')+' · 未切换';
      openAuth(host, password===undefined?'':'密码不对，请重新输入');
      return;
    }
    if(!resp.ok) throw new Error(data.error||resp.status);
    if($('board-auth').open) $('board-auth').close();
    toggleBoardMenu(false);
    if(data.board) boardListing=data.board;
    if(host!==previous){resetBoardView();log({type:'board_switch',source:'bridge'},'已切换到主板 '+host+(data.board_online?'':'（暂未连通，持续重试）'));}
    updateBoardStatusUI(data);
  }catch(error){
    $('board-host').textContent=(previous||'主板')+' · 未切换';menuError('无法切换：'+error.message);
  }
}

// ---- Performance: per-turn latency from voiced's own log lines (see board_bridge._turn_metrics) ----
const perfTurns=new Map();
const PERF_COLUMNS=[['e2e_ms','首词'],['server_first_ms','服务端'],['intent_ms','意图'],['doa_ms','DOA']];
function quantile(values,q){const v=[...values].sort((a,b)=>a-b);if(!v.length)return null;const i=(v.length-1)*q,lo=Math.floor(i);return v[lo]+(v[Math.ceil(i)]-v[lo])*(i-lo);}
function fmtMs(v){return v==null?'-':(Math.round(v*100)/100)+' ms';}
// Keep time and the four metrics compact in the side panel.
const tightMs=v=>fmtMs(v).replace(' ','');
function perfValidity(metrics) {
  const first=metrics.e2e_ms,server=metrics.server_first_ms;
  if(!Number.isFinite(first)||!Number.isFinite(server)) return 'pending';
  return first<server?'invalid':'valid';
}
function renderPerf() {
  const turns=[...perfTurns.values()];
  const validTurns=turns.filter(t=>perfValidity(t.metrics)==='valid');
  const summary=$('perf-summary');summary.replaceChildren();
  for(const [key,label] of PERF_COLUMNS){
    const vals=validTurns.map(t=>t.metrics[key]).filter(v=>typeof v==='number');
    const cell=document.createElement('div');cell.className='perf-stat';
    cell.title='仅统计首词与服务端耗时齐全，且首词 ≥ 服务端耗时的有效轮次';
    const name=document.createElement('span');name.textContent=label+' 中位';
    const value=document.createElement('strong');value.textContent=fmtMs(quantile(vals,0.5));
    const sub=document.createElement('small');sub.textContent='P90 '+tightMs(quantile(vals,0.9));
    const count=document.createElement('small');count.textContent=vals.length+' 轮';
    cell.append(name,value,sub,count);summary.append(cell);
  }
  const rows=$('perf-rows');rows.replaceChildren();
  if(!turns.length){rows.innerHTML='<tr><td colspan="5" class="empty">等待对话…</td></tr>';return;}
  for(const t of turns.slice(-60).reverse()){
    const tr=document.createElement('tr');
    const cells=[t.time,...PERF_COLUMNS.map(([k])=>tightMs(t.metrics[k]))];
    for(const c of cells){const td=document.createElement('td');td.textContent=c;tr.append(td);}
    // First-response latency comes directly from voiced's e2e_ms log.
    if(t.woken){const tag=document.createElement('span');tag.className='perf-woken';tag.textContent='唤醒';tr.children[0].append(document.createTextNode(' '),tag);
      tr.children[0].title='唤醒后的第一句：休眠时缓存的语音需先补发，首词可能包含这段等待';}
    if(t.note&&t.metrics.e2e_ms==null){tr.children[1].title='未测到：'+t.note;tr.children[1].className='unmeasured';}
    if(t.metrics.doa_ms==null) tr.children[4].title='未测到同轮说话开始通知或 DOA 方向';
    const validity=perfValidity(t.metrics);
    if(validity!=='valid') {
      tr.className='perf-'+validity;
      const reason=validity==='invalid'?`首词 ${tightMs(t.metrics.e2e_ms)} < 服务端 ${tightMs(t.metrics.server_first_ms)}，整轮作废，不计入任何统计`:'首词或服务端耗时未到齐，暂不计入统计';
      tr.title=reason;
      const tag=document.createElement('span');tag.className='perf-validity';
      tag.textContent=validity==='invalid'?'无效':'待校验';
      tag.title=reason;tr.children[0].append(document.createTextNode(' '),tag);
    }
    rows.append(tr);
  }
}
function recordMetric(event) {
  const prev=perfTurns.get(event.turn_id);
  perfTurns.set(event.turn_id,{time:prev?.time||new Date().toLocaleTimeString('zh-CN',{hour12:false}),metrics:{...(prev?.metrics||{}),...event.metrics},note:event.note||'',woken:Boolean(event.woken)});
  while(perfTurns.size>200) perfTurns.delete(perfTurns.keys().next().value);
  if(!prev) bumpUnread('perf-card');
  renderPerf();
}

function updateBoardStatusUI(st) {
  if (st.auth_required && st.board_ip) { $('board-host').textContent = st.board_ip + ' · 需要密码'; openAuth(st.board_ip); return; }
  if (st.board_ip) boardListing.current = st.board_ip;
  if ('board_online' in st && $('board-host')) {
    const ip = st.board_ip || '主板';
    $('board-host').textContent = ip + (st.board_online ? ' · 在线' : ' · 离线');
    $('board-host-chip')?.classList.toggle('online', Boolean(st.board_online));
  }
  if (st.board_online === false && $('board-turn-phase')) $('board-turn-phase').textContent = '离线';
  if (st.services && $('board-services')) {
    const names = ['robotd', 'voiced', 'visiond', 'waked', 'translated', 'mediad'];
    if (st.service_cpu) serviceCpu = st.service_cpu;
    const line = n => st.services[n] !== 'active' ? '✗'
      : typeof serviceCpu[n] === 'number' ? formatShare(serviceCpu[n]) : '✓';
    $('board-services').replaceChildren(...(Object.keys(st.services).length ? names.map(n => {
      const cell = document.createElement('span'), value = document.createElement('b');
      cell.textContent = n + ' '; value.textContent = line(n);
      cell.classList.toggle('down', st.services[n] !== 'active');
      cell.append(value);
      return cell;
    }) : [document.createTextNode('-')]));
    $('board-services').title = names.map(n => n + ': ' + (st.services[n] || '未知')
      + (st.services[n] === 'active' && typeof serviceCpu[n] === 'number' ? '，CPU ' + formatShare(serviceCpu[n]) : '')).join('\n')
      + '\nCPU 是占全部 8 个核的比例，和「系统」里的 CPU 同一单位，取最近几秒';
  }
  if ('voice_active' in st && $('board-toggle-btn')) {
    boardVoiceActive = Boolean(st.voice_active);
    $('board-toggle-btn').textContent = boardVoiceActive ? '停止语音' : '启动语音';
    $('board-toggle-btn').classList.toggle('danger', boardVoiceActive);
  }
  if (st.turn_phase && $('board-turn-phase')) $('board-turn-phase').textContent = st.turn_phase;
  if ('board_online' in st) for (const key of ['volume', 'brightness', 'fan']) $('board-' + key).disabled = !st.board_online;
  if (typeof st.volume_raw === 'number') $('board-volume').title = '功放衰减 ' + (st.volume_raw / 8).toFixed(1) + ' dB（原始值 ' + st.volume_raw + '）';
  for (const key of ['volume', 'brightness', 'fan']) {
    const value = st[key + '_pct'];
    if (typeof value !== 'number' || levelBusy[key]) continue;
    $('board-' + key).value = value; $('board-' + key).disabled = false;
    $('board-' + key + '-value').textContent = value + '%';
  }
  for (const key of ['cpu_temp_c', 'cpu_pct', 'mem_pct', 'npu_pct', 'cpu_mhz', 'cpu_mhz_full', 'load_1m', 'uptime_s']) if (typeof st[key] === 'number') systemStatus[key] = st[key];
  if ($('board-system') && Object.keys(systemStatus).length) {
    const s = systemStatus, parts = [];
    if ('cpu_temp_c' in s) parts.push(Math.round(s.cpu_temp_c) + '°C');
    // Busy share of all eight cores; boards that only report the load average still show that.
    if ('cpu_pct' in s) parts.push('CPU ' + s.cpu_pct + '%');
    else if ('load_1m' in s) parts.push('负载 ' + s.load_1m.toFixed(1));
    if ('mem_pct' in s) parts.push('MEM ' + s.mem_pct + '%');
    if ('npu_pct' in s) parts.push('NPU ' + s.npu_pct + '%');
    const throttled = s.cpu_mhz < s.cpu_mhz_full;
    if (throttled) parts.push('降频 ' + s.cpu_mhz + ' MHz');
    if ('uptime_s' in s) parts.push('开机 ' + formatUptime(s.uptime_s));
    // Hot, busy, or booted in the last three minutes (these boards have been resetting on their own).
    const hot = s.cpu_temp_c >= 80, busy = s.cpu_pct >= 90, tight = s.mem_pct >= 90, fresh = s.uptime_s < 180;
    $('board-system').textContent = parts.join(' · ') || '-';
    $('board-system').classList.toggle('warn', hot || busy || tight || fresh || throttled);
    $('board-system').title = (hot ? 'CPU 温度偏高。' : '') + (busy ? 'CPU 占用偏高。' : '') + (tight ? '内存快用完了。' : '') + (throttled ? 'CPU 被过热降频到 ' + s.cpu_mhz + ' MHz（满频 ' + s.cpu_mhz_full + ' MHz），这时的 CPU % 是相对降频后的速度。' : '') + (fresh ? '板子刚重启过（开机不到 3 分钟）。' : '')
      + '温度取各温区最高值；CPU 为 8 个核合计的忙碌比例，取最近几秒；内存不含可回收的缓存；NPU 为各核负载的平均，读的是一瞬间'
      + ('load_1m' in s ? '；1 分钟平均负载 ' + s.load_1m.toFixed(2) + '（8 核满载为 8）' : '') + '。';
  }
  if ('people' in st && $('board-people')) $('board-people').textContent = st.people + ' 人';
}

function motionEvent(event) {
  if(poseSource==='board' && (event.type==='pose_started'||event.type==='pose_completed')) return;
  const meta=catalog.actions.find(x=>x.protocol_id===event.actionId);
  const stage=event.stageIndex===0?'中位初始化':meta?.stages[event.stageIndex-1]?.name;
  const suffix=event.type==='stage_started'||event.type==='simulated_arrival'||event.type==='stage_completed' ? stage : meta?.name;
  log(event,[eventNames[event.type]||event.type,suffix].filter(Boolean).join(' · '));
  if(event.type==='action_completed' && faceOwner==='action') idleFace();
  if(performing?.action && !performing.motionDone && event.actionId===performing.action.protocol_id
     && ['action_completed','interrupted','stopped'].includes(event.type)){performing.motionDone=true;settlePerformance();}
  uiStageKey='';
}

function findFace(id) { return [...catalog.expressions,...catalog.states].find(item=>item.id===id); }
function idleFace() { const idle=findFace('D01');if(idle) playFace(idle,'loop','idle'); }
// `onPass` fires once the clip has played through the first time (a loop clip included), after the
// screen has moved on to whatever follows it on its own.
function playFace(item, mode='full', owner='manual', onPass=null) {
  if(!item) throw new Error('找不到表情资源');
  faceOwner=owner; const generation=++faceGeneration;
  const isLoop=mode==='loop';video.loop=isLoop&&!onPass;video.onended=null;
  $('face-name').textContent=item.id+' · '+item.name;
  $('face-mode').textContent=isLoop?'循环':'完整';
  video.src=isLoop?(item.loop||item.full):item.full;video.load();
  video.playbackRate=Number($('speed').value);
  const tryPlay=()=>{
    if(document.hidden){resumeOnVisible=true;return;}
    resumeOnVisible=false;
    return video.play().then(()=>{if(generation===faceGeneration)$('face-resume').hidden=true;}).catch(error=>{
    if(generation!==faceGeneration || error.name==='AbortError') return;
    $('face-resume').hidden=false;
    if(error.name!=='NotAllowedError') showError('表情播放失败：'+item.name+'。请检查素材路径。');
    });
  };
  tryPlay();
  // Full expression transitions to its supplied loop; `once` (the board's mediad) returns to idle.
  // Actions release it to idle on completion.
  const passed=()=>{ if(onPass) onPass(); };
  if(mode==='once') video.onended=()=>{ if(generation===faceGeneration){ idleFace(); passed(); } };
  else if(isLoop && onPass) video.onended=()=>{
    if(generation!==faceGeneration) return;
    video.onended=null; video.loop=true; video.play().catch(()=>{}); passed();
  };
  else if(!isLoop) video.onended=()=>{
    if(generation!==faceGeneration) return;
    if(item.loop) playFace(item,'loop',owner); else video.onended=null;
    passed();
  };
  log({type:'screen_changed',source:'simulator',simulated:true,expression_id:item.id,mode},'屏幕 · '+item.id+' '+item.name+' / '+(isLoop?'循环保持':mode==='once'?'播放一次后回待机':'完整播放'));
}

// Signalled performances (an action, or an expression that plays through) run one at a time: the app
// side sends them faster than they play, so a new one waits for the current face and motion to finish
// instead of cutting them short. Holding screens (states, loop faces with no motion) don't queue: the
// latest one is put up once the queue has drained.
const QUEUE_MAX=6, FACE_GRACE_S=20;
let performing=null, performQueue=[], heldScreen=null;

function toPerformance(event) {
  if(event.type==='action'){
    const action=catalog.actions.find(x=>x.id===event.action_id);
    if(!action)throw new Error('未知动作 '+event.action_id);
    return {key:'A:'+action.id, label:action.id+' '+action.name, face:findFace(action.screen_id), mode:'full', action,
            idleAfter:true, live:(event.emotion?event.emotion+' → ':'')+action.id+' '+action.name};
  }
  const face=findFace(event.expression_id);
  if(!face)throw new Error('未知表情');
  const mode=event.mode||'full', action=event.action_id&&catalog.actions.find(x=>x.id===event.action_id)||null;
  // Paired body motion (Fnn ↔ Ann) on the virtual robot only; the face keeps the event's mode.
  return {key:'E:'+face.id+':'+mode+':'+(action?.id||''), label:face.id+' '+face.name+(action?' + '+action.id:''),
          face, mode, action, live:face.id+' · '+face.name};
}
function renderQueue() {
  $('live-queue').textContent=performing?'播放中 '+performing.label+(performQueue.length?' · 排队 '+performQueue.length:'')
    :heldScreen?'待显示 '+heldScreen.face.id:'队列空';
}
function sequenceLog(text){log({type:'sequence',source:'simulator',simulated:true},'队列 · '+text);}
function enqueue(p) {
  if(!performing){runPerformance(p);return;}
  const last=performQueue.at(-1)||performing;
  if(last.key===p.key){sequenceLog(p.label+' 与'+(last===performing?'正在播放的':'队尾')+'相同，合并');return;}
  if(performQueue.length>=QUEUE_MAX)sequenceLog('已满，丢弃最早排队的 '+performQueue.shift().label);
  performQueue.push(p);sequenceLog(p.label+' 排在第 '+performQueue.length+' 位，等 '+performing.label+' 播完');
  renderQueue();
}
function runPerformance(p) {
  performing=p;p.faceDone=!p.face;p.motionDone=!p.action;p.waited=0;
  $('live-emotion').textContent=p.live;
  if(p.face)playFace(p.face,p.mode,'perform',()=>{if(performing===p){p.faceDone=true;settlePerformance();}});
  if(p.action){currentAction=p.action;engine.start(p.action.protocol_id);uiStageKey='';renderStages();}
  renderQueue();
}
function settlePerformance() { if(performing?.faceDone && performing.motionDone) finishPerformance(); }
function finishPerformance() {
  const done=performing;performing=null;
  const next=performQueue.shift();
  if(next){runPerformance(next);return;}
  if(heldScreen){const held=heldScreen;heldScreen=null;showHeld(held);}
  // An action's own face goes back to idle with the motion, as before; an expression keeps its loop.
  else if(done.idleAfter && faceOwner==='perform')idleFace();
  renderQueue();
}
function holdScreen(face, live) {
  if(!performing){showHeld({face,live});return;}
  if(heldScreen&&heldScreen.face.id!==face.id)sequenceLog('待显示画面 '+heldScreen.face.id+' 换成 '+face.id);
  else if(!heldScreen)sequenceLog(face.id+' '+face.name+' 等队列播完再显示');
  heldScreen={face,live};renderQueue();
}
function showHeld(held){$('live-emotion').textContent=held.live;playFace(held.face,'loop','signal');}
function dropQueue(reason) {
  const dropped=(performing?1:0)+performQueue.length;
  performing=null;performQueue=[];heldScreen=null;renderQueue();
  if(dropped)sequenceLog(reason+'，清空 '+dropped+' 个');
}

function startAction(item) {
  if(!item) return;
  dropQueue('手动播放');clearError();currentAction=item;
  playFace(findFace(item.screen_id),'full','action');
  engine.start(item.protocol_id);uiStageKey='';renderStages();
}
function stopAll(reason='停止预览') {
  dropQueue(reason);engine.stop();heading.stop();video.pause();resumeOnVisible=false;video.onended=null;++faceGeneration;faceOwner='stopped';
  $('doa').value=Math.round(heading.target)%360;$('base-target').textContent='目标 '+Math.round(heading.target)%360+'°';
  log({type:'preview_stopped',source:'local',simulated:true},reason);
}
function resetAll() {view?.resetCamera('front');dropQueue('归中');engine.reset();heading.setTarget(0);$('doa').value=0;$('base-target').textContent='目标 0°';idleFace();clearError();}

function renderLibrary() {
  const query=$('search').value.toLowerCase().trim();
  const items=catalog[kind].filter(item=>(item.id+' '+item.name+' '+(item.family||'')).toLowerCase().includes(query));
  $('library-list').replaceChildren();
  for(const item of items){
    const button=document.createElement('button');button.className='library-item'+(selected?.id===item.id?' selected':'');button.setAttribute('aria-label',item.id+' '+item.name);button.setAttribute('aria-pressed',String(selected?.id===item.id));
    const id=document.createElement('span');id.className='id';id.textContent=item.id;
    const name=document.createElement('span');name.className='name';name.textContent=item.name;button.append(id,name);
    button.onclick=()=>selectItem(item);$('library-list').append(button);
  }
  if(!items.length){const empty=document.createElement('p');empty.className='empty';empty.textContent='没有匹配项';$('library-list').append(empty);}
}
function selectItem(item) {
  selected=item;renderLibrary();
  $('selection-id').textContent=item.id;$('selection-name').textContent=item.name;
  $('selection-description').textContent=item.description||'';
  const action=kind==='actions';$('face-buttons').hidden=action;$('action-note').hidden=!action;
  $('play').replaceChildren(document.createTextNode(action?'▶  播放动作':'▶  播放表情'));
  $('face-full').textContent=kind==='states'?'播放状态':'完整播放';
  if(engine.status!=='running' && engine.status!=='paused') renderStages();
}
function renderStages() {
  const running=engine.mode==='action' && ['running','paused'].includes(engine.status);
  const action=running?currentAction:(kind==='actions'?selected:currentAction);
  const key=action?.id+':'+engine.stageIndex+':'+engine.status+':'+running;
  if(uiStageKey===key) return;uiStageKey=key;$('stage-track').replaceChildren();
  if(!action)return;
  ['中位初始化',...action.stages.map(s=>s.name)].forEach((name,index)=>{
    const item=document.createElement('div');item.className='stage-step';
    if(engine.mode==='action' && currentAction?.id===action.id){
      if(index===engine.stageIndex && ['running','paused'].includes(engine.status))item.classList.add('active');
      if(index<engine.stageIndex||engine.status==='completed')item.classList.add('done');
    }
    item.textContent=(index===0?'○':String(index).padStart(2,'0'))+' '+name;item.title=name;$('stage-track').append(item);
  });
}

const directionLabels={head_pitch_down:['仰头 −30°','低头 +12°'],head_roll:['右歪 −10°','左歪 +10°'],head_yaw:['右转 −60°','左转 +60°'],arm_r_out:['内收 −6°','外张 +35°'],arm_l_out:['内收 −6°','外张 +35°'],arm_r_forward:['后摆 −20°','前抬 +180°'],arm_l_forward:['后摆 −20°','前抬 +180°']};
function buildJoints() {
  const liveGrid = $('live-joints-grid');
  for(const axis of AXES){
    const row=document.createElement('div');row.className='joint';
    const heading=document.createElement('div');heading.className='joint-heading';
    const label=document.createElement('label');label.htmlFor='joint-'+axis.key;label.textContent=axis.label;
    const value=document.createElement('span');value.className='joint-value';value.id='value-'+axis.key;value.textContent='0° → 0°';heading.append(label,value);
    const slider=document.createElement('input');slider.type='range';slider.min=axis.min;slider.max=axis.max;slider.step=1;slider.value=0;slider.id='joint-'+axis.key;
    slider.oninput=()=>{dropQueue('手动调关节');poseSource='local';engine.setPose({[axis.key]:Number(slider.value)});if(faceOwner==='action')idleFace();};
    const labels=document.createElement('div');labels.className='range-labels';for(const text of directionLabels[axis.key]){const span=document.createElement('span');span.textContent=text;labels.append(span);}
    row.append(heading,slider,labels);$('joint-controls').append(row);

    if (liveGrid) {
      const cell = document.createElement('div');
      cell.className = 'telemetry-cell';
      cell.id = 'tcell-' + axis.key;
      const tName = document.createElement('span');
      tName.className = 't-name';
      tName.textContent = axis.label;
      const tVal = document.createElement('span');
      tVal.className = 't-val';
      tVal.id = 'tval-' + axis.key;
      tVal.textContent = '0°';
      cell.append(tName, tVal);
      liveGrid.append(cell);
    }
  }
}
function updateUI() {
  const snap=engine.snapshot();
  const phase={idle:'准备就绪',moving:'运动中',holding:'到位后停留',completed:'模拟执行完成',stopped:'已停止'};
  $('playback-title').textContent=snap.paused?'已暂停':phase[snap.phase]||snap.phase;
  const meta=catalog.actions.find(x=>x.protocol_id===snap.actionId);
  const detail=meta?(snap.stageIndex===0?'中位初始化':meta.stages[snap.stageIndex-1]?.name):snap.mode==='manual'?'手动关节目标':snap.mode==='reset'?'七轴归中':'等待板端触发动作';
  $('playback-detail').textContent=(meta?meta.id+' '+meta.name+' · ':'')+detail+(snap.phase==='holding'?' · 剩余 '+snap.holdRemaining.toFixed(2)+' s':'');
  $('elapsed').textContent=snap.elapsed.toFixed(1)+' s';$('phase-dot').classList.toggle('running',snap.status==='running');
  $('elapsed').classList.toggle('quiet',snap.status!=='running');$('playback-detail').classList.toggle('quiet',snap.status!=='running'&&!snap.paused);
  $('live-queue').classList.toggle('quiet',$('live-queue').textContent==='队列空');
  $('base-target').classList.toggle('quiet',Math.round(heading.current-heading.target)===0);
  $('pause').disabled=!['running','paused'].includes(snap.status);$('pause').textContent=snap.paused?'继续':'暂停';
  $('base-angle').textContent=(Math.round(heading.current)%360)+'°';
  for(const axis of AXES){
    const cur = snap.pose[axis.key].toFixed(0);
    const tgt = snap.target[axis.key].toFixed(0);
    $('value-'+axis.key).textContent=cur+'° → '+tgt+'°';
    const slider=$('joint-'+axis.key);if(document.activeElement!==slider)slider.value=snap.target[axis.key];
    const tVal = $('tval-' + axis.key);
    const tCell = $('tcell-' + axis.key);
    if (tVal) tVal.textContent = cur === tgt ? cur + '°' : cur + '° → ' + tgt + '°';
    if (tCell) tCell.classList.toggle('moving', cur !== tgt);
  }
  renderStages();
}

const examples={action:{type:'action',source:'injected',action_id:'A02'},doa:{type:'doa',source:'injected',angle_deg:120,frame:'world'},state:{type:'state',source:'injected',state_id:'T03'},expression:{type:'expression',source:'injected',expression_id:'F19',mode:'loop'},stop:{type:'stop',source:'injected'}};
function updateExample(){ $('event-json').value=JSON.stringify(examples[$('event-example').value],null,2); }
function incoming(event) {
  if (event.type === 'caption') {
    const wasMergingAssistant = (event.kind === 'assistant' && activeAssistantRow && (Date.now() - activeAssistantTs) < 8000);
    appendCaption(event);
    if (event.kind === 'user_final') {
      log(event, '字幕 [用户] ' + event.text);
    } else if (event.kind === 'assistant') {
      const mergedText = activeAssistantRow?.querySelector('.caption-text')?.textContent || event.text;
      if (wasMergingAssistant && $('event-list')?.firstElementChild?.dataset?.captionAssistant === '1') {
        const desc = $('event-list').firstElementChild.querySelector('.event-description');
        if (desc) desc.textContent = '字幕 [回复] ' + mergedText;
      } else {
        log(event, '字幕 [回复] ' + mergedText);
        if ($('event-list')?.firstElementChild) {
          $('event-list').firstElementChild.dataset.captionAssistant = '1';
        }
      }
    }
    return;
  }
  if (event.type === 'board_status') {
    updateBoardStatusUI(event);
    // Log the edge, not every retry: the bridge re-reports offline every few seconds.
    if ('board_online' in event) {
      if (event.board_online === false && lastBoardOnline !== false) log(event, '板端连接断开，桥接重连中');
      if (event.board_online === true && lastBoardOnline === false) log(event, '板端已重新连上');
      lastBoardOnline = event.board_online;
    }
    if (event.detail) log(event, '板端状态 · ' + event.detail);
    return;
  }
  if (event.type === 'board_log') {
    log(event, '[' + (event.logger || 'board').replace(/^voiced::/, '') + '] ' + event.message);
    return;
  }
  if (event.type === 'metric') { recordMetric(event); return; }
  // Where visiond sees faces, a couple of times a second: only the camera window wants it.
  if (event.type === 'faces') {
    if (typeof CustomEvent === 'function') window.dispatchEvent(new CustomEvent('board-faces', {detail: event.faces}));
    return;
  }
  if (event.type === 'pose') {
    // Board head pose: drives the preview joints; uncalibrated, so clamped rather than refused.
    poseSource = 'board';
    if (engine.mode === 'action' && engine.status === 'running') return; // don't cut a paired action short
    engine.setPose(event.joints, {clamp: true});
    return;
  }
  $('last-source').textContent=(sourceNames[event.source]||event.source)+' · '+new Date(event.received_at||Date.now()).toLocaleTimeString('zh-CN',{hour12:false});
  const emotionSuffix = event.emotion ? ' (' + event.emotion + ')' : '';
  log(event,'收到 '+event.type+' · '+(event.action_id||event.expression_id||event.state_id||(event.type==='doa'?event.angle_deg+'°':'')||'') + emotionSuffix);
  switch(event.type){
    case 'action': {
      const p=toPerformance(event);
      if (lastUserCaptionBadge) { lastUserCaptionBadge.hidden = false; lastUserCaptionBadge.textContent = p.live; }
      clearError();enqueue(p);
      break;
    }
    case 'expression': {
      const p=toPerformance(event);
      // A loop face with no motion is a holding screen, like a state: it never finishes on its own.
      if(p.mode==='loop' && !p.action) holdScreen(p.face,p.live); else enqueue(p);
      break;
    }
    case 'state': {
      const item=catalog.states.find(x=>x.id===event.state_id);
      if(!item)throw new Error('未知状态');
      holdScreen(item,'业务状态 · ' + item.id + ' ' + item.name);
      break;
    }
    case 'doa': if(event.frame!=='world')throw new Error('DOA 必须先转换为 world 绝对朝向');heading.setTarget(event.angle_deg);$('doa').value=event.angle_deg;$('base-target').textContent='目标 '+event.angle_deg+'°';break;
    case 'reset':resetAll();break;
    case 'stop':stopAll('外部停止事件');break;
    default:throw new Error('不支持的事件类型');
  }
}
function connectBridge() {
  const stream=new EventSource('/api/events');
  stream.onopen=()=>{
    connected=true;$('connection').classList.add('online');$('connection').lastChild.textContent='在线';
    log({type:'connected',source:'bridge'},knownConnection?'桥接已重连 · 等待新的状态，不补播旧命令':'本地桥接已连接 · 等待板端信号');
    knownConnection=true;
  };
  stream.onmessage=message=>{try{incoming(JSON.parse(message.data));}catch(error){showError('事件未执行：'+error.message);log({type:'event_error',source:'bridge'},error.message);}};
  stream.addEventListener('gap',()=>{stopAll('接收队列出现缺口，预览已停止');showError('事件接收不连续，请发送最新状态后再继续。');});
  stream.onerror=()=>{
    $('connection').classList.remove('online');$('connection').lastChild.textContent='重连中';
    if(connected){connected=false;stopAll('桥接断开，预览已停止');showError('本地桥接已断开。恢复后请重发当前状态，旧命令不会自动补播。');}
  };
  window.addEventListener('pagehide',()=>stream.close(),{once:true});
}

async function callBoardControl(payload) {
  try {
    const resp = await fetch('/api/board/control', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if ('board_online' in data) updateBoardStatusUI(data);
    if (!data.ok) log({type: 'control_error', source: 'bridge'}, '板端操作失败：' + (data.error || resp.status));
  } catch (err) {
    log({type: 'control_error', source: 'bridge'}, '板端操作失败：' + err.message);
  }
}

function wireControls() {
  document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=()=>{
    document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('active',b===button));
    for(const id of ['library','joints','signals'])$('panel-'+id).hidden=id!==button.dataset.tab;
  });
  document.querySelectorAll('[data-kind]').forEach(button=>button.onclick=()=>{
    kind=button.dataset.kind;$('search').value='';document.querySelectorAll('[data-kind]').forEach(b=>b.classList.toggle('active',b===button));selectItem(catalog[kind][0]);
  });
  document.querySelectorAll('[data-logfilter]').forEach(button=>button.onclick=()=>{
    logFilter = button.dataset.logfilter;
    document.querySelectorAll('[data-logfilter]').forEach(b=>b.classList.toggle('active',b===button));
    for (const child of $('event-list').children) {
      if (child.dataset?.source) child.hidden = !shouldShowRow(child.dataset.source);
    }
  });
  document.querySelectorAll('[data-collapse]').forEach(button=>{
    const card=$(button.dataset.collapse), key='robotlab.collapsed.'+button.dataset.collapse;
    const apply=collapsed=>{
      card.classList.toggle('collapsed',collapsed);
      button.setAttribute('aria-expanded',String(!collapsed));
      if(!collapsed) clearUnread(card.id);
      try{localStorage.setItem(key,collapsed?'1':'0');}catch{}
    };
    let saved=null;try{saved=localStorage.getItem(key);}catch{}
    if(saved==='1') apply(true);
    button.onclick=()=>apply(!card.classList.contains('collapsed'));
  });
  $('search').oninput=renderLibrary;
  const manualFace=mode=>{dropQueue('手动播放');playFace(selected,mode,'manual');};
  $('play').onclick=()=>{if(kind==='actions')startAction(selected);else manualFace(kind==='states'?'loop':'full');};
  $('face-full').onclick=()=>manualFace(kind==='states'?'loop':'full');$('face-loop').onclick=()=>manualFace('loop');
  $('face-resume').onclick=()=>video.play().then(()=>{$('face-resume').hidden=true;}).catch(()=>showError('表情未能播放，请检查浏览器的媒体播放设置。'));
  video.onerror=()=>{showError('屏幕视频加载失败，请检查本地服务和素材路径。');};
  $('pause').onclick=()=>{if(engine.paused){engine.resume();if(pausedVideo)video.play().catch(()=>{$('face-resume').hidden=false;});}else{pausedVideo=!video.paused;engine.pause();video.pause();resumeOnVisible=false;}};
  $('stop').onclick=()=>stopAll();$('neutral').onclick=()=>{dropQueue('归中');engine.reset();idleFace();};$('reset-all').onclick=resetAll;
  $('doa').oninput=()=>{const degrees=Number($('doa').value);heading.setTarget(degrees);$('base-target').textContent='目标 '+degrees+'°';};
  $('doa').onchange=()=>log({type:'doa',source:'local',angle_deg:heading.target,frame:'world'},'DOA 目标 '+heading.target+'° · 世界坐标，左转为正');
  $('speed').onchange=()=>{video.playbackRate=Number($('speed').value);log({type:'preview_speed',source:'local'},'预览速度 '+$('speed').value+'×');};
  $('event-example').onchange=updateExample;updateExample();
  $('send-event').onclick=async()=>{
    $('send-event').disabled=true;
    try{
      const event=JSON.parse($('event-json').value);
      event.source='injected';
      const response=await fetch('/api/events',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(event)});
      const result=await response.json();if(!response.ok)throw new Error(result.error||'事件被拒绝');
      $('event-result').textContent='已提交事件 #'+result.event.seq+'；在时间线查看接收和执行。';
    }catch(error){$('event-result').textContent='未发送：'+error.message;}
    finally{$('send-event').disabled=false;}
  };
  $('clear-events').onclick=()=>{eventLog.length=0;$('event-list').replaceChildren();};
  if ($('clear-captions')) $('clear-captions').onclick=()=>{activeInterimRow=null;activeAssistantRow=null;$('caption-list').replaceChildren();};
  if ($('board-toggle-btn')) {
    $('board-toggle-btn').onclick = async () => {
      $('board-toggle-btn').disabled = true;
      log({type: 'voice_toggle', source: 'board'}, (boardVoiceActive ? '正在停止' : '正在启动') + '板端 voiced…');
      await callBoardControl({cmd: boardVoiceActive ? 'stop' : 'start'});
      $('board-toggle-btn').disabled = false;
    };
  }
  for (const key of ['volume', 'brightness', 'fan']) {
    const slider = $('board-' + key), label = $('board-' + key + '-value');
    slider.addEventListener('pointerdown', () => { levelBusy[key] = true; });
    slider.oninput = () => { levelBusy[key] = true; label.textContent = slider.value + '%'; };
    slider.onchange = async () => {
      const percent = Number(slider.value);
      await callBoardControl({cmd: key, percent});
      log({type: key, source: 'board'}, ({volume: '音量', brightness: '屏幕亮度', fan: '风扇转速'})[key] + ' → ' + percent + '%');
      levelBusy[key] = false;
    };
  }
  $('board-host-chip').onclick=event=>{event.stopPropagation();toggleBoardMenu();};
  $('board-menu').onclick=event=>event.stopPropagation();
  document.addEventListener('click',()=>{if(!$('board-menu').hidden)toggleBoardMenu(false);});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!$('board-menu').hidden){toggleBoardMenu(false);$('board-host-chip').focus();}});
  $('board-rescan').onclick=()=>loadBoards(true);
  $('clear-perf').onclick=()=>{perfTurns.clear();renderPerf();};
  $('board-add').onsubmit=event=>{event.preventDefault();const host=$('board-add-input').value.trim();if(host){$('board-add-input').value='';selectBoard(host);}};
  $('board-auth-form').onsubmit=event=>{event.preventDefault();const pw=$('board-auth-password').value;if(pw&&authHost)selectBoard(authHost,pw);};
  $('board-auth-cancel').onclick=()=>{$('board-auth').close();authHost=null;fetch('/api/board/status').then(r=>r.json()).then(st=>{if(!st.auth_required)updateBoardStatusUI(st);}).catch(()=>{});};
  $('export-events').onclick=()=>{
    const blob=new Blob([eventLog.map(e=>JSON.stringify(e)).join('\n')+'\n'],{type:'application/x-ndjson'});
    const url=URL.createObjectURL(blob),anchor=document.createElement('a');anchor.href=url;anchor.download='robot-events-'+new Date().toISOString().replaceAll(':','-')+'.jsonl';anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  document.addEventListener('visibilitychange',()=>{
    lastFrame=performance.now();
    if(document.hidden){resumeOnVisible=!video.paused;video.pause();}
    else if(resumeOnVisible && faceOwner!=='stopped'){
      resumeOnVisible=false;video.play().catch(()=>{$('face-resume').hidden=false;});
    }
  });
}

async function boot() {
  const getJSON=async path=>{const response=await fetch(path);if(!response.ok)throw new Error('资源加载失败：'+path);return response.json();};
  const [assets,actions]=await Promise.all([getJSON('/data/catalog.json'),getJSON('/data/ServoActionList.json')]);
  catalog=assets;engine=new MotionEngine(actions,{onEvent:motionEvent});view=new RobotView($('viewport'));
  await view.load();$('loading').hidden=true;view.setFace(video);buildJoints();wireControls();
  selectItem(catalog.actions.find(x=>x.id==='A02')||catalog.actions[0]);idleFace();connectBridge();
  log({type:'ready',source:'simulator',simulated:true},'STP 模型就绪 · 7 个表达关节 + 1 个 DOA 底座 · 实板联调监控台已启动');
  if (window.location?.hostname) {
    fetch('/api/board/status').then(r => r.json()).then(updateBoardStatusUI).catch(() => {});
  }
  lastFrame=performance.now();
  const frame=now=>{
    const dt=document.hidden?0:Math.max(0,(now-lastFrame)/1000)*Number($('speed').value);lastFrame=now;
    engine.tick(dt);heading.tick(dt);
    if(performing?.motionDone && !performing.faceDone && !engine.paused && (performing.waited+=dt)>FACE_GRACE_S){
      sequenceLog(performing.label+' 的表情 '+FACE_GRACE_S+' 秒没播完，跳过');performing.faceDone=true;settlePerformance();
    }view.setPose(engine.pose);view.setBaseHeading(heading.current);view.render();
    if(now-lastUI>80){updateUI();lastUI=now;}
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}
boot().catch(error=>{
  $('loading').hidden=true;
  const message=error.message==='Failed to fetch'?'本地服务不可用或预览资源未能加载。':'预览启动失败：'+error.message;
  showError(message,{startup:true});$('play').disabled=true;
  $('reload-preview').onclick=()=>window.location.reload();
});
