TEMPLATE = r'''<!doctype html><html><head><meta charset="utf-8"><title>Trace report</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;font:13px/1.45 system-ui,sans-serif;background:#0e1116;color:#e6edf3}
header{padding:10px 16px;background:#161b22;border-bottom:1px solid #30363d}
header b{color:#58a6ff}.sub{color:#8b949e;font-size:12px;margin-top:3px}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px}
.toolbar label{color:#8b949e;font-size:12px}
nav{display:flex;gap:2px;background:#161b22;padding:0 10px;border-bottom:1px solid #30363d;flex-wrap:wrap}
nav button{background:none;border:none;color:#8b949e;padding:10px 14px;cursor:pointer;font-size:13px;border-bottom:2px solid transparent}
nav button.on{color:#58a6ff;border-bottom-color:#58a6ff}nav button:hover{color:#e6edf3}
.tab{display:none;padding:16px}.tab.on{display:block}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 16px;min-width:150px}
.card .n{font-size:22px;font-weight:600}.card .l{color:#8b949e;font-size:12px}
.risk{color:#f85149}.warn{color:#db6d28}.ok{color:#3fb950}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #21262d}
th{color:#8b949e;cursor:pointer;position:sticky;top:0;background:#0e1116}
td.mono,.mono{font-family:ui-monospace,monospace}tr:hover{background:#161b22}
a{color:#58a6ff}a.addr{text-decoration:none}a.addr:hover{text-decoration:underline}
input,select,button.act{background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:6px 8px;border-radius:6px}
button.act{cursor:pointer}
svg{background:#0e1116;width:100%}
.badge{display:inline-block;padding:1px 6px;border-radius:10px;font-size:10px;font-weight:600}
.b-exchange{background:#1f6feb33;color:#58a6ff}.b-mixer{background:#db6d2833;color:#db6d28}.b-sanctioned{background:#f8514933;color:#f85149}
.r-high{background:#f8514933;color:#f85149}.r-med{background:#db6d2833;color:#db6d28}.r-low{background:#3fb95033;color:#3fb950}
#wrap{display:flex;height:66vh}#side{width:310px;padding:12px;background:#161b22;border-left:1px solid #30363d;overflow:auto;font-size:12px}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;vertical-align:middle;margin-right:4px}
line.edge{stroke:#3d4551}line.loop{stroke:#f85149;stroke-width:2}line.hl{stroke:#f0b72f !important;stroke-width:2.5 !important;stroke-opacity:1 !important}
circle{cursor:pointer;stroke:#0e1116;stroke-width:1.5}text.lbl{fill:#8b949e;font-size:9px;pointer-events:none}
h3{color:#58a6ff;margin:14px 0 6px}.muted{color:#8b949e}
.note{background:#161b22;border:1px solid #30363d;border-left:3px solid #58a6ff;border-radius:6px;padding:10px 12px;margin:8px 0}
#printview{display:none}
@media print{
 body{background:#fff;color:#000}nav,#wrap,.toolbar,header .sub{display:none}
 .tab{display:none !important}#printview{display:block !important;padding:0}
 #printview h1{font-size:18px}#printview h2{font-size:14px;color:#000;border-bottom:1px solid #999;margin-top:18px}
 #printview table,#printview th,#printview td{border:1px solid #999;color:#000}#printview .badge{border:1px solid #999}
 .card{border:1px solid #999}
}
</style></head><body>
<header>
 <div><b id="rtitle"></b> — origin <b class="mono" id="orig"></b></div>
 <div class="sub">chain tip <span id="tip"></span> · <span id="gen"></span></div>
 <div class="toolbar">
  <label>direction</label>
  <select id="dir"><option value="out">outgoing (where it went)</option><option value="in">incoming (where it came from)</option><option value="both">both</option></select>
  <label><input type="checkbox" id="collapse"> collapse entities</label>
  <label><input type="checkbox" id="links" checked> explorer links</label>
  <label><input type="checkbox" id="conf" checked> confidence shading</label>
  <button class="act" onclick="doPrint()">🖨 print / save PDF</button>
 </div>
</header>
<nav id="nav"></nav><div id="tabs"></div>
<div id="printview"></div>
<script>
const DATA=__DATA__;
const STATE={dir:'out',collapse:false,links:true,conf:true};
const RTITLE=DATA.title||'Bitcoin address trace report';
document.getElementById('rtitle').textContent=RTITLE;
document.title=RTITLE;
document.getElementById('orig').textContent=DATA.origin;
document.getElementById('tip').textContent=DATA.tip;
document.getElementById('gen').textContent=DATA.generated;
const fmt=x=>Number(x).toLocaleString(undefined,{maximumFractionDigits:6});
const short=s=>s.startsWith('ent:')?s.slice(4):s.slice(0,10)+'…'+s.slice(-4);
const exurl=a=>DATA.explorer+'/address/'+a;
const alink=a=>STATE.links&&!a.startsWith('ent:')?`<a class="addr mono" href="${exurl(a)}" target="_blank">${short(a)}</a>`:`<span class="mono">${short(a)}</span>`;

// ---------- data shaping: direction filter + entity collapse + confidence + risk ----------
function shaped(){
 let nodes=DATA.nodes.filter(n=>STATE.dir==='both'||n.side==='origin'||n.side===STATE.dir);
 let ids=new Set(nodes.map(n=>n.id));
 let edges=DATA.edges.filter(e=>(STATE.dir==='both'||e.dir===STATE.dir)&&ids.has(e.s)&&ids.has(e.t));
 // confidence: decays with hop distance from origin along flow direction
 edges.forEach(e=>{const src=DATA.nodes.find(n=>n.id===e.s);const depthRef=STATE.dir==='in'? (DATA.nodes.find(n=>n.id===e.t)||{}).depth : (src||{}).depth;
   e.conf=Math.max(0.15,Math.pow(0.8,(depthRef||0)));});
 if(STATE.collapse){
   const map={};DATA.nodes.forEach(n=>map[n.id]=n.entity?'ent:'+n.entity:n.id);
   const nn={};nodes.forEach(n=>{const id=map[n.id];if(!nn[id])nn[id]={...n,id,recv:0,sent:0,members:[]};nn[id].recv+=n.recv;nn[id].sent+=n.sent;nn[id].members.push(n.id);nn[id].origin=nn[id].origin||n.origin;});
   const em={};edges.forEach(e=>{const s=map[e.s],t=map[e.t];if(s===t)return;const k=s+'>'+t;if(!em[k])em[k]={...e,s,t,v:0,n:0};em[k].v+=e.v;em[k].n+=e.n;});
   nodes=Object.values(nn);edges=Object.values(em);
 }
 // risk per node: worst entity type on the path back to origin
 const inc={};edges.forEach(e=>(inc[e.t]=inc[e.t]||[]).push(e.s));
 const rank={sanctioned:3,mixer:2,exchange:1};
 nodes.forEach(n=>{let seen=new Set(),cur=[n.id],g=0,worst=0,wt=null;
   while(cur.length&&g++<40){const nx=[];for(const c of cur){if(seen.has(c))continue;seen.add(c);const nd=nodes.find(x=>x.id===c);if(nd&&nd.etype&&rank[nd.etype]>worst){worst=rank[nd.etype];wt=nd.etype;}(inc[c]||[]).forEach(p=>nx.push(p));}cur=nx;}
   n.risk=worst;n.risktype=wt;});
 // signed level: origin 0, outgoing +depth, incoming -depth
 nodes.forEach(n=>{n.level=n.origin?0:(n.side==='in'?-Math.abs(n.depth):Math.abs(n.depth));});
 return {nodes,edges};
}
function sgn(v){return (v>0?'+':'')+v;}
function riskBadge(n){if(!n.risk)return'';const m={3:['r-high','HIGH'],2:['r-med','MED'],1:['r-low','LOW']}[n.risk];return `<span class="badge ${m[0]}">${m[1]} risk</span>`;}
function entExposure(g,type){return g.edges.filter(e=>{const t=g.nodes.find(n=>n.id===e.t);return t&&t.etype===type;}).reduce((a,e)=>a+e.v,0);}

// ---------- tabs ----------
const TABS=[['Overview',overview],['Network graph',graph],['Flow (Sankey)',sankey],['Flows table',table],['Entities & risk',entities],['Timeline',timeline],['Methodology',method]];
const nav=document.getElementById('nav'),tabs=document.getElementById('tabs');
TABS.forEach(([name],i)=>{const b=document.createElement('button');b.textContent=name;b.onclick=()=>sel(i);nav.append(b);
 const d=document.createElement('div');d.className='tab';d.id='tab'+i;tabs.append(d);});
let cur=0;
function sel(i){cur=i;[...nav.children].forEach((b,j)=>b.classList.toggle('on',j===i));[...tabs.children].forEach((d,j)=>d.classList.toggle('on',j===i));
 const el=document.getElementById('tab'+i);el.innerHTML='';TABS[i][1](el);}
function rerender(){sel(cur);}
['dir','collapse','links','conf'].forEach(id=>{const e=document.getElementById(id);
 e.addEventListener('change',()=>{STATE.dir=document.getElementById('dir').value;STATE.collapse=document.getElementById('collapse').checked;STATE.links=document.getElementById('links').checked;STATE.conf=document.getElementById('conf').checked;rerender();});});

function overview(el){const g=shaped();const ex=entExposure(g,'exchange'),mx=entExposure(g,'mixer'),sn=entExposure(g,'sanctioned');
 const origin=g.nodes.find(n=>n.origin)||{recv:0,sent:0};const loops=g.edges.filter(e=>e.loop).length;const own=g.nodes.filter(n=>n.owner).length;
 const top=[...g.edges].sort((a,b)=>b.v-a.v).slice(0,6);
 el.innerHTML=`<div class="cards">
  <div class="card"><div class="n">${fmt(origin.recv)}</div><div class="l">BTC received</div></div>
  <div class="card"><div class="n">${fmt(origin.sent)}</div><div class="l">BTC sent</div></div>
  <div class="card"><div class="n">${g.nodes.length}</div><div class="l">addresses (${STATE.dir})</div></div>
  <div class="card"><div class="n">${g.edges.length}</div><div class="l">flows</div></div>
  <div class="card"><div class="n ${loops?'warn':'ok'}">${loops}</div><div class="l">round-trips to owner</div></div><div class="card"><div class="n ${own?'warn':'ok'}">${own}</div><div class="l">related wallets (same owner)</div></div></div>
  <h3>Entity exposure</h3><div class="cards">
   <div class="card"><div class="n">${fmt(ex)}</div><div class="badge b-exchange">EXCHANGE</div><div class="muted" style="margin-top:4px">subpoena target</div></div>
   <div class="card"><div class="n warn">${fmt(mx)}</div><div class="badge b-mixer">MIXER</div><div class="muted" style="margin-top:4px">trace boundary</div></div>
   <div class="card"><div class="n risk">${fmt(sn)}</div><div class="badge b-sanctioned">SANCTIONED</div><div class="muted" style="margin-top:4px">OFAC / reportable</div></div></div>
  <div class="note">Exchange exposure is the practical endpoint — those flows reach a regulated VASP that can be served a disclosure order for KYC records.</div>
  <h3>Largest flows</h3><table><tr><th>from</th><th>to</th><th>BTC</th><th>conf.</th><th>entity</th></tr>
  ${top.map(e=>{const t=g.nodes.find(n=>n.id===e.t)||{};return `<tr><td>${alink(e.s)}</td><td>${alink(e.t)}</td><td>${fmt(e.v)}</td><td class=muted>${Math.round((e.conf||1)*100)}%</td><td>${t.entity?`<span class="badge b-${t.etype}">${t.entity}</span>`:''}</td></tr>`;}).join('')}</table>`;
}
function graph(el){el.innerHTML=`<div style="margin-bottom:8px"><input id="q" placeholder="search address / entity…" style="width:220px">
  <select id="layout"><option value="free">free-flow</option><option value="target">fixed rings (target)</option></select>
  <button class="act" onclick="fitG()">reset view</button>
  <span style="margin-left:10px;font-size:12px"><span class="dot" style="background:#f0b72f"></span>origin
  <span class="dot" style="background:#58a6ff"></span>d1 <span class="dot" style="background:#3fb950"></span>d2 <span class="dot" style="background:#a371f7"></span>d3
  <span class="dot" style="background:#db6d28"></span>mixer <span class="dot" style="background:#f85149"></span>sanctioned · red edge=round-trip to owner · white ring=entity · gold ring=same owner · fade=confidence</span></div>
  <div id="wrap"><svg id="g"></svg><div id="side" class="muted">Click a node (path from origin highlights). Click an edge for value + confidence. Addresses link to the block explorer.</div></div>`;
 initGraph(shaped());
}
function sankey(el){const g=shaped();const cols={};g.nodes.forEach(n=>{const d=STATE.dir==='in'?-Math.abs(n.depth):n.depth;(cols[d]=cols[d]||[]).push(n);});
 const keys=Object.keys(cols).map(Number).sort((a,b)=>a-b);const W=el.clientWidth||1000,H=560,pad=46,span=(keys[keys.length-1]-keys[0])||1;
 const colX=d=>pad+((d-keys[0])/span)*(W-2*pad);const pos={};
 keys.forEach(d=>{let y=pad;const list=cols[d].sort((a,b)=>b.recv-a.recv);const tot=list.reduce((a,n)=>a+n.recv,0)||1;
   list.forEach(n=>{const h=Math.max(6,(n.recv/tot)*(H-2*pad));pos[n.id]={x:colX(d),y,h};y+=h+3;});});
 let s=`<svg viewBox="0 0 ${W} ${H}" height="${H}">`;
 g.edges.forEach(e=>{const a=pos[e.s],b=pos[e.t];if(!a||!b)return;const t=g.nodes.find(n=>n.id===e.t)||{};
   const col=e.loop?'#f85149':t.etype==='sanctioned'?'#f85149':t.etype==='mixer'?'#db6d28':'#58a6ff';
   const op=STATE.conf?Math.max(.12,(e.conf||1)*.5):.35;
   s+=`<path d="M${a.x+8},${a.y+a.h/2} C${(a.x+b.x)/2},${a.y+a.h/2} ${(a.x+b.x)/2},${b.y+b.h/2} ${b.x},${b.y+b.h/2}" fill="none" stroke="${col}" stroke-opacity="${op}" stroke-width="${Math.max(1,Math.sqrt(e.v)*3)}"/>`;});
 g.nodes.forEach(n=>{const p=pos[n.id];if(!p)return;const c=n.origin?'#f0b72f':n.etype==='sanctioned'?'#f85149':n.etype==='mixer'?'#db6d28':['#58a6ff','#58a6ff','#3fb950','#a371f7'][Math.min(Math.abs(n.depth),3)];
   s+=`<rect x="${p.x}" y="${p.y}" width="9" height="${p.h}" fill="${c}"><title>${n.id}\n${fmt(n.recv)} BTC${n.entity?' · '+n.entity:''}</title></rect>`;
   if(p.h>12)s+=`<text x="${p.x+12}" y="${p.y+p.h/2+3}" fill="#8b949e" font-size="9">${n.entity||short(n.id)}</text>`;});
 s+='</svg>';
 el.innerHTML=`<div class="muted" style="margin-bottom:6px">Value by hop (${STATE.dir}). Bar height ∝ BTC; ribbon width ∝ flow; opacity ∝ confidence.</div>`+s;
}
function table(el){const g=shaped();el.innerHTML=`<div style="margin-bottom:8px"><input id="tf" placeholder="filter…" style="width:280px"><button class="act" onclick="dlCSV()">⬇ CSV</button></div><div id="tw"></div>`;
 const rows=g.edges.map(e=>{const s=g.nodes.find(n=>n.id===e.s)||{},t=g.nodes.find(n=>n.id===e.t)||{};
   const level=e.dir==='in'?(s.level||0):(t.level||0);
   return{from:e.s,to:e.t,level,btc:e.v,txs:e.n,conf:Math.round((e.conf||1)*100),entity:t.entity||'',etype:t.etype||'',risk:t.risk||0,when:e.when||'',loop:e.loop?'↩':''};});
 let sk='btc',dir=-1;
 window.TS=k=>{if(sk===k)dir=-dir;else{sk=k;dir=-1;}render();};
 window.dlCSV=()=>{const csv='from,to,level,btc,txs,confidence,entity,risk,when,loop\n'+rows.map(x=>[x.from,x.to,x.level,x.btc,x.txs,x.conf+'%',x.entity,x.risk,x.when,x.loop].join(',')).join('\n');const a=document.createElement('a');a.href='data:text/csv,'+encodeURIComponent(csv);a.download='flows.csv';a.click();};
 function render(){const f=(document.getElementById('tf').value||'').toLowerCase();
  const r=rows.filter(x=>!f||x.from.includes(f)||x.to.includes(f)||x.entity.toLowerCase().includes(f)).sort((a,b)=>(a[sk]>b[sk]?1:-1)*dir);
  document.getElementById('tw').innerHTML='<table><tr>'+['from','to','level','btc','txs','conf','entity','risk','when','loop'].map(k=>`<th onclick="TS('${k}')">${k==='level'?'level (hops)':k}</th>`).join('')+'</tr>'+
   r.map(x=>`<tr><td>${alink(x.from)}</td><td>${alink(x.to)}</td><td class=mono>${sgn(x.level)}</td><td>${fmt(x.btc)}</td><td>${x.txs}</td><td class=muted>${x.conf}%</td><td>${x.entity?`<span class="badge b-${x.etype}">${x.entity}</span>`:''}</td><td>${x.risk?`<span class="badge ${['','r-low','r-med','r-high'][x.risk]}">${['','LOW','MED','HIGH'][x.risk]}</span>`:''}</td><td>${x.when}</td><td class=warn>${x.loop}</td></tr>`).join('')+'</table>';}
 document.getElementById('tf').oninput=render;render();
}
function entities(el){const g=shaped();const flagged=g.nodes.filter(n=>n.entity);const byType={};flagged.forEach(n=>(byType[n.etype]=byType[n.etype]||[]).push(n));
 el.innerHTML='<div class="note">Addresses matched against a known-entity list (exchanges, mixers, sanctioned/OFAC). Unflagged = unlabelled, not necessarily clean. Risk = worst entity type on the path from origin.</div>'+
  (Object.keys(byType).length?Object.entries(byType).map(([t,list])=>`<h3><span class="badge b-${t}">${t.toUpperCase()}</span></h3><table><tr><th>entity</th><th>address(es)</th><th>BTC in</th></tr>`+
   list.map(n=>{const v=g.edges.filter(e=>e.t===n.id).reduce((a,e)=>a+e.v,0);return `<tr><td>${n.entity}${n.members&&n.members.length>1?` <span class=muted>(${n.members.length} addrs)</span>`:''}</td><td>${alink(n.id)}</td><td>${fmt(v)}</td></tr>`;}).join('')+'</table>').join(''):'<p class=muted>No flagged entities in this direction.</p>');
}
function timeline(el){const g=shaped();const byM={};g.edges.forEach(e=>{if(e.when)byM[e.when]=(byM[e.when]||0)+e.v;});
 const months=Object.keys(byM).sort();if(!months.length){el.innerHTML='<p class=muted>no dated flows</p>';return;}
 const W=el.clientWidth||1000,H=320,pad=40,max=Math.max(...Object.values(byM)),bw=(W-2*pad)/months.length;
 let s=`<svg viewBox="0 0 ${W} ${H}" height="${H}">`;
 months.forEach((m,i)=>{const h=(byM[m]/max)*(H-2*pad);s+=`<rect x="${pad+i*bw}" y="${H-pad-h}" width="${bw-6}" height="${h}" fill="#58a6ff"><title>${m}: ${fmt(byM[m])} BTC</title></rect><text x="${pad+i*bw+bw/2-6}" y="${H-pad+14}" fill="#8b949e" font-size="10">${m.slice(2)}</text>`;});
 el.innerHTML='<div class="muted" style="margin-bottom:6px">Flow value by month ('+STATE.dir+') — spot bursts (cash-outs, layering).</div>'+s+'</svg>';
}
function method(el){el.innerHTML=`<h3>Fact vs. inference</h3><div class="note"><b>Fact:</b> that a coin moved and into which transaction (on-chain spend graph); amounts, addresses, times.<br><b>Inference:</b> payment-vs-change and intra-transaction value split — estimated, with uncertainty.</div>
 <h3>Value attribution</h3><p class="muted">Per-edge BTC uses the <b>haircut</b> model. <b>Confidence</b> shading fades with hop distance as haircut dilution and change-ambiguity compound; treat deep, faint edges as weaker.</p>
 <h3>Risk scoring</h3><p class="muted">Each address's risk = worst entity type on its path from the origin: <span class="badge r-high">HIGH</span> sanctioned, <span class="badge r-med">MED</span> mixer, <span class="badge r-low">LOW</span> exchange.</p>
 <h3>Related wallets (same owner)</h3><p class="muted">Gold-ringed addresses are likely controlled by the same entity as the origin (common-input-ownership: co-spent as inputs in one transaction). A red edge is value returning to that cluster — a round-trip. Bounded single-round; may miss wallets and can be defeated by CoinJoin.</p><h3>Bounds</h3><p class="muted">Fixed depth, per-node fan-out cap; hub/exchange/mixer addresses are flagged and not expanded (value becomes unattributable past them).</p>
 <h3>Provenance</h3><p class="muted">Full Bitcoin Core node at chain tip <b>${DATA.tip}</b>. Reproducible from the source database — keep this stamp for evidentiary integrity.</p>
 <p class="muted" style="margin-top:14px;font-size:11px">Estimates for investigative use; verify before evidentiary reliance.</p>`;
}
// ---------- force graph ----------
let G={};
function initGraph(g){const svg=document.getElementById('g'),NS='http://www.w3.org/2000/svg';const Wd=()=>svg.clientWidth,Hd=()=>svg.clientHeight;
 const COL=['#f0b72f','#58a6ff','#3fb950','#a371f7'];
 const color=n=>n.etype==='sanctioned'?'#f85149':n.etype==='mixer'?'#db6d28':n.origin?COL[0]:COL[Math.min(Math.abs(n.depth),3)];
 const rad=n=>n.origin?12:Math.max(4,6+Math.log10((n.recv||0)+1)*3);
 const N=g.nodes.map(n=>({...n,x:Wd()/2+(Math.random()-.5)*400,y:Hd()/2+(Math.random()-.5)*400,vx:0,vy:0}));const I={};N.forEach(n=>I[n.id]=n);
 const L=g.edges.filter(e=>I[e.s]&&I[e.t]).map(e=>({...e,a:I[e.s],b:I[e.t]}));
 const mk=(t,at)=>{const e=document.createElementNS(NS,t);for(const k in at)e.setAttribute(k,at[k]);return e;};
 svg.innerHTML='';const gR=mk('g'),gE=mk('g'),gN=mk('g'),gT=mk('g');svg.append(gR,gE,gN,gT);
 const les=L.map(l=>{const e=mk('line',{class:l.loop?'loop':'edge'});e.setAttribute('stroke-opacity',STATE.conf?Math.max(.12,(l.conf||1)):.6);
   e.addEventListener('click',()=>info(`<b>flow</b><br>${alink(l.s)} → ${alink(l.t)}<br>${fmt(l.v)} BTC · ${l.n} tx · conf ${Math.round((l.conf||1)*100)}%${l.loop?'<br><b class=warn>round-trip to owner</b>':''}`));gE.append(e);return e;});
 const nes=N.map(n=>{const c=mk('circle',{r:rad(n),fill:color(n)});if(n.entity){c.setAttribute('stroke','#fff');c.setAttribute('stroke-width','2.5');}else if(n.owner){c.setAttribute('stroke','#f0b72f');c.setAttribute('stroke-width','2.5');c.setAttribute('stroke-dasharray','2 2');}
   c.addEventListener('click',()=>{info(`<div>${alink(n.id)}</div><br>level ${sgn(n.level||0)} (hops from origin) · ${riskBadge(n)}${n.owner?' <b style="color:#f0b72f">⚠ same owner</b>':''}<br>received ${fmt(n.recv)} BTC<br>sent ${fmt(n.sent)} BTC${n.entity?`<br><span class="badge b-${n.etype}">${n.entity}</span>`:''}${n.members&&n.members.length>1?`<br><span class=muted>${n.members.length} addresses collapsed</span>`:''}<br><br><button class=act onclick="navigator.clipboard.writeText('${n.id}')">copy</button> ${STATE.links&&!n.id.startsWith('ent:')?`<a class=act href="${exurl(n.id)}" target=_blank>explorer ↗</a>`:''}`);hlPath(n,les,L);});
   c.addEventListener('mousedown',e=>{G.drag=n;e.stopPropagation();});gN.append(c);return c;});
 const tes=N.map(n=>{const t=mk('text',{class:'lbl'});t.textContent=(n.origin?'★ ':'')+(n.entity||short(n.id));gT.append(t);return t;});
 G={svg,N,L,les,nes,tes,rad,vb:{x:0,y:0,k:1},gR,gE,gN,gT,layout:'free',mk};
 computeTargets();drawRings();
 document.getElementById('layout').addEventListener('change',e=>{G.layout=e.target.value;computeTargets();drawRings();});
 document.getElementById('q').oninput=e=>{const q=e.target.value.toLowerCase();nes.forEach((c,i)=>c.setAttribute('opacity',!q||N[i].id.toLowerCase().includes(q)||(N[i].entity||'').toLowerCase().includes(q)?1:.12));};
 svg.addEventListener('mousedown',e=>{if(e.target===svg){G.pan=1;G.px=e.clientX-G.vb.x;G.py=e.clientY-G.vb.y;}});
 window.onmousemove=e=>{if(G.drag){const r=svg.getBoundingClientRect();G.drag.x=(e.clientX-r.left-G.vb.x)/G.vb.k;G.drag.y=(e.clientY-r.top-G.vb.y)/G.vb.k;G.drag.vx=G.drag.vy=0;}else if(G.pan){G.vb.x=e.clientX-G.px;G.vb.y=e.clientY-G.py;applyV();}};
 window.onmouseup=()=>{G.drag=null;G.pan=0;};
 svg.onwheel=e=>{e.preventDefault();G.vb.k*=e.deltaY<0?1.1:.9;applyV();};
 stepG();
}
function computeTargets(){const {N,L,svg}=G;const cx=svg.clientWidth/2,cy=svg.clientHeight/2;
 const colGap=175,rowGap=34;const byId={};N.forEach(n=>{n._y=undefined;byId[n.id]=n;});
 // parent -> children along the flow direction (incoming edges point origin-ward)
 const kids={};N.forEach(n=>kids[n.id]=[]);const ids=new Set(N.map(n=>n.id));
 L.forEach(e=>{const p=e.dir==='in'?e.t:e.s,c=e.dir==='in'?e.s:e.t;if(ids.has(p)&&ids.has(c)&&p!==c&&!kids[p].includes(c))kids[p].push(c);});
 // tidy-tree y: leaves get sequential slots, parents centre over their children
 let leaf=0;const seen=new Set();
 function dfs(id){if(seen.has(id))return byId[id]._y;seen.add(id);
  const ch=kids[id].filter(c=>!seen.has(c));
  if(!ch.length){byId[id]._y=leaf++;return byId[id]._y;}
  const ys=ch.map(dfs);byId[id]._y=(Math.min(...ys)+Math.max(...ys))/2;return byId[id]._y;}
 dfs(DATA.origin);N.forEach(n=>{if(n._y===undefined)n._y=leaf++;});
 const mid=(leaf-1)/2;
 N.forEach(n=>{n.tx=cx+(n.level||0)*colGap;n.ty=cy+(n._y-mid)*rowGap;});
 if(byId[DATA.origin]){byId[DATA.origin].tx=cx;byId[DATA.origin].ty=cy;}
 G.cols=[...new Set(N.map(n=>n.level||0))].filter(l=>l!==0).sort((a,b)=>a-b).map(l=>({L:l,x:cx+l*colGap}));}
function drawRings(){const {gR,svg,mk}=G;gR.innerHTML='';if(G.layout!=='target')return;
 const H=svg.clientHeight,cx=svg.clientWidth/2;
 (G.cols||[]).forEach(({L,x})=>{const ln=mk('line',{x1:x,y1:20,x2:x,y2:H-20,stroke:'#21262d','stroke-dasharray':'3 6'});gR.append(ln);
   const t=mk('text',{x,y:16,fill:'#6e7681','font-size':11,'text-anchor':'middle'});t.textContent=(L>0?'+':'')+L;gR.append(t);});
 const o=mk('text',{x:cx,y:16,fill:'#f0b72f','font-size':11,'text-anchor':'middle'});o.textContent='origin (0)';gR.append(o);}
function applyV(){const t=`translate(${G.vb.x},${G.vb.y}) scale(${G.vb.k})`;G.gR.setAttribute('transform',t);G.gE.setAttribute('transform',t);G.gN.setAttribute('transform',t);G.gT.setAttribute('transform',t);}
function fitG(){G.vb={x:0,y:0,k:1};applyV();}
function info(h){document.getElementById('side').innerHTML=h;}
function hlPath(n,les,L){les.forEach((e,i)=>e.setAttribute('class',L[i].loop?'loop':'edge'));const inc={};L.forEach((l,i)=>(inc[l.t]=inc[l.t]||[]).push(i));let c=n.id,g=0;
 while(c&&c!==DATA.origin&&g++<25){const es=inc[c];if(!es)break;les[es[0]].setAttribute('class','hl');c=L[es[0]].s;}}
function stepG(){const {N,L,les,nes,tes,rad,svg}=G;const Wd=svg.clientWidth,Hd=svg.clientHeight;
 if(G.layout==='target'){
  for(const n of N){if(n===G.drag)continue;n.x+=((n.tx||Wd/2)-n.x)*.12;n.y+=((n.ty||Hd/2)-n.y)*.12;}
 }else{
  for(const n of N){if(n===G.drag)continue;for(const m of N){if(n===m)continue;let dx=n.x-m.x,dy=n.y-m.y,d2=dx*dx+dy*dy+.01;if(d2<90000){const f=1200/d2/Math.sqrt(d2);n.vx+=dx*f;n.vy+=dy*f;}}}
  for(const l of L){let dx=l.b.x-l.a.x,dy=l.b.y-l.a.y,d=Math.sqrt(dx*dx+dy*dy)+.01,f=(d-100)*.01;if(l.a!==G.drag){l.a.vx+=dx/d*f;l.a.vy+=dy/d*f;}if(l.b!==G.drag){l.b.vx-=dx/d*f;l.b.vy-=dy/d*f;}}
  for(const n of N){if(n===G.drag)continue;n.vx+=(Wd/2-n.x)*.001;n.vy+=(Hd/2-n.y)*.001;n.x+=n.vx*=.85;n.y+=n.vy*=.85;}
 }
 L.forEach((l,i)=>{les[i].setAttribute('x1',l.a.x);les[i].setAttribute('y1',l.a.y);les[i].setAttribute('x2',l.b.x);les[i].setAttribute('y2',l.b.y);});
 N.forEach((n,i)=>{nes[i].setAttribute('cx',n.x);nes[i].setAttribute('cy',n.y);tes[i].setAttribute('x',n.x+rad(n)+2);tes[i].setAttribute('y',n.y+3);});
 requestAnimationFrame(stepG);}
// ---------- print ----------
function doPrint(){const g=shaped();const origin=g.nodes.find(n=>n.origin)||{recv:0,sent:0};
 const ex=entExposure(g,'exchange'),mx=entExposure(g,'mixer'),sn=entExposure(g,'sanctioned');
 const top=[...g.edges].sort((a,b)=>b.v-a.v).slice(0,15);
 document.getElementById('printview').innerHTML=`<h1>${RTITLE}</h1>
  <p><b>Origin:</b> ${DATA.origin}<br><b>Direction:</b> ${STATE.dir}<br><b>Chain tip:</b> ${DATA.tip}<br><b>Generated:</b> ${DATA.generated}</p>
  <h2>Summary</h2><p>Received ${fmt(origin.recv)} BTC · Sent ${fmt(origin.sent)} BTC · ${g.nodes.length} addresses · ${g.edges.length} flows.</p>
  <h2>Entity exposure</h2><p>Exchange: ${fmt(ex)} BTC · Mixer: ${fmt(mx)} BTC · Sanctioned: ${fmt(sn)} BTC</p>
  <h2>Largest flows</h2><table><tr><th>from</th><th>to</th><th>BTC</th><th>conf</th><th>entity</th></tr>
  ${top.map(e=>{const t=g.nodes.find(n=>n.id===e.t)||{};return `<tr><td>${short(e.s)}</td><td>${short(e.t)}</td><td>${fmt(e.v)}</td><td>${Math.round((e.conf||1)*100)}%</td><td>${t.entity||''}</td></tr>`;}).join('')}</table>
  <h2>Methodology</h2><p>Value attribution: haircut model; confidence decays with hop distance. Fact = on-chain spend graph; inference = payment/change split. Hubs/exchanges flagged and not expanded. Estimates for investigative use; verify before evidentiary reliance.</p>`;
 window.print();
}
sel(0);
</script></body></html>'''
