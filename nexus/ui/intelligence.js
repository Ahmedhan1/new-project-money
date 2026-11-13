// Account 3 workspace: research, references, AI models, languages, publishing, analytics, growth.
// Loaded after index.html's main script; it only ADDS a menu group and pages (no edits to other pages).
(function(){
const ev=(list)=>(list||[]).map(e=>e.url?`<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.source)}</a>`:esc(e.source)).join(", ");
const conf=c=>c==null?"":tag(`confidence ${Math.round(c*100)}%`,c>=0.7?"ok":c>=0.4?"warn":"bad");
const unavailable=(m,title,e)=>{m.innerHTML=`<h2 class="page">${esc(title)}</h2><div class="note">${esc(e.message)}</div>`};
const A3=[["a3research","Content Research & Opportunities","live"],["a3refs","Creator References","live"],["a3strategy","Script Studio (original strategy)","live"],
 ["a3lang","Language Studio","live"],["a3publish","Publishing (approval queue)","live"],["a3analytics","Analytics & Growth","live"],["models","AI Models & Router","live"]];
// the old "planned" research/analytics entries are now real pages here
MODULES.forEach(g=>{g[1]=g[1].filter(x=>!["research","analytics"].includes(x[0]))});
MODULES.push(["Intelligence (Account 3)",A3]);
const jobsHere=m=>{m.insertAdjacentHTML("beforeend",`<div id="jobs"></div>`);poll($("#jobs"))};
const pct=x=>x==null?"unknown":Math.round(x*100)+"%";

PAGES.models=async function(m){
  let d,r;try{d=await api("/api/a3/ai/providers");r=await api("/api/a3/ai/routes?privacy="+(sel||"public"))}catch(e){return unavailable(m,"AI Models & Router",e)}
  const pr=sel||"public";
  m.innerHTML=`<h2 class="page">AI Models & Provider Router</h2><p class="lead">Every AI task picks the best eligible model: local first for private data, hosted free tiers only after you authorise them, never a paid endpoint. Nothing depends on one provider.</p>
  <div class="row"><label style="max-width:260px">Data class<select id="pv">${["public","internal","private"].map(x=>`<option${x===pr?" selected":""}>${x}</option>`).join("")}</select></label></div>
  <h3 class="sec">Models</h3>${table(d.models,[["Provider",x=>esc(x.provider)],["Model",x=>esc(x.model)],["Hosting",x=>tag(x.hosting)],["Max data class",x=>esc(x.privacy)],["Licence",x=>esc(x.licence||"")],
   ["Quota",x=>esc(Object.entries(x.quotas||{}).filter(([k])=>k!=="source").map(([k,v])=>k+" "+v).join(", "))],["Verified",x=>esc(x.verified||"")],["Now",x=>x.available?tag("available","ok"):`${tag("not available","warn")}<div style="color:var(--mute);font-size:12px">${esc(x.why)}</div>`]])}
  <h3 class="sec">Routing per task (${esc(pr)} data)</h3>${table(Object.entries(r.tasks),[["Task",x=>esc(x[0])],["Route (in order)",x=>x[1].route.length?esc(x[1].route.join(" → ")):tag("no provider - task waits / human review","bad")]])}
  <h3 class="sec">Provider health</h3>${table(d.health,[["Provider",x=>esc(x.provider)],["Requests",x=>x.requests],["Mean latency",x=>x.mean_latency_s+" s"],["Failures",x=>x.failures],["Circuit",x=>tag(x.circuit,x.circuit==="closed"?"ok":"bad")],["Used today",x=>x.today]])}
  <h3 class="sec">Try a task</h3><div class="row"><label style="max-width:240px">Task<select id="tk">${Object.keys(r.tasks).map(t=>`<option>${t}</option>`).join("")}</select></label><label>Prompt<input id="tp" placeholder="Suggest 3 titles for a video about solar storage"></label><button id="tg">Run</button></div><pre id="to"></pre>`;
  $("#pv").onchange=()=>{sel=$("#pv").value;go("models")};
  $("#tg").onclick=async()=>{$("#to").textContent="running…";try{const o=await api("/api/a3/ai/try",{task:$("#tk").value,prompt:$("#tp").value||"Say hello",privacy:pr});$("#to").textContent=`${o.provider}/${o.model} · ${o.latency_s}s${o.below_bar?`\nDRAFT QUALITY: best available model scores ${o.below_bar.used_quality} (${o.below_bar.quality_source}) below the bar ${o.below_bar.required} for this task - review before use`:""}\n\n${o.text}`}catch(e){$("#to").textContent=e.message}};
};

PAGES.a3research=async function(m,arg){
  if(arg){const d=await api("/api/a3/research/"+arg),o=d.opportunity,b=d.brief;
    m.innerHTML=`<h2 class="page">${esc(d.topic)} <small>${esc(d.niche)} · ${esc(d.language)} · ${esc(d.date)}</small></h2>
    <p class="lead">Opportunity score <b>${o.score??"—"}</b> ${conf(o.confidence)}. Missing data lowers confidence; nothing is imputed and no view counts are predicted.</p>
    <h3 class="sec">Factors</h3>${table(Object.entries(o.factors),[["Factor",x=>esc(x[0])],["Value",x=>x[1].value==null?tag("unknown","warn"):esc(JSON.stringify(x[1].value))],["Confidence",x=>conf(x[1].confidence)],["How",x=>esc(x[1].method||"")],["Evidence",x=>ev(x[1].evidence)]])}
    <h3 class="sec">Claims (${b.claims.length})</h3>${table(b.claims,[["Claim",x=>esc(x.text)],["Status",x=>tag(x.status,x.status==="verified"?"ok":"warn")],["Sources",x=>esc((x.supported_by||x.source_ids||[]).join(", "))]])}
    ${(b.conflicts||[]).length?`<h3 class="sec">Conflicts — do not state</h3><pre>${esc(JSON.stringify(b.conflicts,null,1))}</pre>`:""}
    <h3 class="sec">Open questions</h3><ul>${(b.open_questions||[]).map(q=>`<li>${esc(typeof q==="string"?q:JSON.stringify(q))}</li>`).join("")}</ul>
    <h3 class="sec">Sources</h3>${table(b.sources,[["#",x=>esc(x.id)],["Source",x=>x.url?`<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title||x.source)}</a>`:esc(x.title||x.source)],["Licence",x=>esc(x.licence||"")],["Date",x=>esc(x.date||"")]])}
    <h3 class="sec">Adapters</h3>${table(Object.entries(d.adapters).filter(x=>!Array.isArray(x[1])),[["Source",x=>esc(x[0])],["Status",x=>statusTag(x[1].status)],["Reason",x=>esc(x[1].reason||"")]])}
    <div class="row"><label style="max-width:200px">Format<select id="asp"><option value="16:9">16:9 video</option><option value="9:16">9:16 short</option></select></label>
    <label style="max-width:300px">Style<select id="sty"><option value="">none (default structure)</option></select></label>
    <label style="max-width:240px">Then<select id="sub"><option value="0">write script for review</option><option value="1">write + send to production if checks pass</option></select></label><button id="ws">Write original script</button></div>`;
    (await api("/api/a3/styles")).forEach(st=>$("#sty").insertAdjacentHTML("beforeend",`<option value="${esc(st.id)}">${esc(st.name)}</option>`));
    $("#ws").onclick=async()=>{await api("/api/a3/strategy",{report:arg,style:$("#sty").value||null,aspect:$("#asp").value,submit:$("#sub").value==="1"});go("a3strategy")};
    return jobsHere(m)}
  const [rows,niches]=await Promise.all([api("/api/a3/research"),api("/api/a3/niches")]);
  m.innerHTML=`<h2 class="page">Content Research & Opportunities</h2><p class="lead">Wikipedia facts + readership in several languages (demand), NASA/Openverse material, news and competition when their sources are available. Every factor shows its evidence and confidence; unknown stays unknown.</p>
  <div class="row"><label>Topic<input id="tp" placeholder="James Webb Space Telescope"></label><label style="max-width:220px">Niche<select id="ni">${Object.keys(niches.niches).map(k=>`<option>${esc(k)}</option>`).join("")}</select></label>
  <label style="max-width:120px">Language<select id="lg">${["en","ar","de","fr","hi","zh","ru"].map(x=>`<option>${x}</option>`).join("")}</select></label><button id="go">Research</button></div>
  <h3 class="sec">Reports</h3>${table(rows,[["Topic",x=>`<a href="#" onclick="go('a3research','${esc(x.id)}');return false">${esc(x.topic)}</a>`],["Niche",x=>esc(x.niche)],["Lang",x=>esc(x.language)],["Score",x=>x.score??"—"],["Confidence",x=>conf(x.confidence)],["Date",x=>esc(x.date)]])}`;
  $("#go").onclick=async()=>{if(!$("#tp").value.trim())return;await api("/api/a3/research",{topic:$("#tp").value,niche:$("#ni").value,lang:$("#lg").value});go("a3research")};
  jobsHere(m)};

PAGES.a3refs=async function(m){
  const [refs,lib,styles]=await Promise.all([api("/api/a3/references"),api("/api/a3/references/library"),api("/api/a3/styles")]);
  m.innerHTML=`<h2 class="page">Creator References</h2><p class="lead">Measure the structure of videos you have the right to use (your own, licensed, public domain, Creative Commons). Patterns, never copies: nothing is downloaded from platforms, and styles carry a do-not-copy list.</p>
  <div class="row"><label>Local file path<input id="rp" placeholder="/path/to/reference.mp4"></label><label style="max-width:180px">Rights<select id="rr">${["owned","licensed","public_domain","cc-by","cc-by-sa","cc0","authorised"].map(x=>`<option>${x}</option>`).join("")}</select></label>
  <label style="max-width:160px">Creator<input id="rc"></label><label style="max-width:160px">Category<input id="rk" placeholder="science"></label><button id="ra">Analyse</button></div>
  <h3 class="sec">Profiles</h3>${table(refs,[["",x=>`<input type="checkbox" class="pick" value="${esc(x.id)}">`],["Creator",x=>esc(x.creator)],["Category",x=>esc(x.category)],["Format",x=>esc(x.format)],["Length",x=>x.duration_s?Math.round(x.duration_s)+" s":""],["Hook",x=>esc(typeof x.hook==="object"?JSON.stringify(x.hook):x.hook||"")],["Confidence",x=>conf(x.confidence)]])}
  <div class="row"><button id="cmp">Compare selected</button><label style="max-width:240px">Style name<input id="sn" value="my reference style"></label><button id="mk">Make abstract style</button></div><pre id="out"></pre>
  <h3 class="sec">Styles</h3>${table(styles,[["Name",x=>esc(x.name)],["From",x=>esc((x.from_references||[]).length+" reference(s)")],["Do not copy",x=>esc((x.do_not_copy||[]).slice(0,4).join("; "))]])}
  <h3 class="sec">Pattern library (cited)</h3>${table(lib,[["Format",x=>esc(x.name)],["Hooks",x=>esc((x.hook_patterns||[]).map(h=>h.pattern||h).slice(0,3).join("; "))],["Sources",x=>esc((x.sources||[]).length+" source(s)")]])}`;
  const picks=()=>[...document.querySelectorAll(".pick:checked")].map(x=>x.value);
  $("#ra").onclick=async()=>{if(!$("#rp").value.trim())return;await api("/api/a3/references",{path:$("#rp").value,rights:$("#rr").value,creator:$("#rc").value||"unknown",category:$("#rk").value||"unknown"});go("a3refs")};
  $("#cmp").onclick=async()=>{try{const r=await api("/api/a3/references/compare",{ids:picks()});$("#out").textContent=JSON.stringify(r,null,1)}catch(e){$("#out").textContent=e.message}};
  $("#mk").onclick=async()=>{try{await api("/api/a3/styles",{ids:picks(),name:$("#sn").value});go("a3refs")}catch(e){$("#out").textContent=e.message}};
  jobsHere(m)};

PAGES.a3strategy=async function(m){
  const rows=await api("/api/a3/strategy");
  m.innerHTML=`<h2 class="page">Script Studio</h2><p class="lead">Original scripts built from a research report and (optionally) an abstract reference style. A script goes to production only if it passes the originality check (no do-not-copy phrases, ≤ 5 % shared 6-word sequences) and the grounding check (every number is in a sourced claim). Start one from a research report.</p>
  ${rows.map(x=>`<div class="card"><h3>${esc(x.title)} <small>${esc(x.language)} · ${x.shots} shots · ${x.target_s} s</small></h3>
   <p>${x.originality.ok?tag("original","ok"):tag("originality FAILED","bad")} ${x.originality.grounding.ok?tag("grounded","ok"):tag("grounding: "+JSON.stringify(x.originality.grounding).slice(0,80),"bad")} ${tag("written by "+x.originality.written_by)} ${x.submitted?tag("in production: job "+x.submitted.job,"ok"):""}</p>
   <ol>${x.scenes.map(s=>`<li><b>${esc(s.purpose||"")}</b> ${esc(s.narration)} <span style="color:var(--mute)">[${esc(s.visual||"")}]</span></li>`).join("")}</ol></div>`).join("")||'<p class="lead">No scripts yet.</p>'}`;
  jobsHere(m)};

PAGES.a3lang=async function(m){
  const langs=await api("/api/a3/languages");
  m.innerHTML=`<h2 class="page">Language Studio</h2><p class="lead">${langs.length} languages. A voice is offered for commercial output only when its licence is verified; Egyptian and Gulf Arabic are separate profiles.</p>
  ${table(langs,[["Tag",x=>esc(x.tag)],["Language",x=>esc(x.name)],["MT",x=>esc((x.mt||[]).map(v=>v.id||v).join(", "))],["Commercial voices",x=>x.voices_commercial.length?esc(x.voices_commercial.join(", ")):tag("none - subtitles only","warn")]])}
  <h3 class="sec">Translation review</h3><div class="row"><label>Source lines (one per line)<textarea id="src">The Aswan High Dam is 111 m high and was completed in 1970.</textarea></label><label>Translation<textarea id="tgt">Der Assuan-Staudamm ist 111 m hoch.</textarea></label>
  <label style="max-width:120px">Language<input id="tl" value="de"></label><button id="chk">Check</button></div><div id="qa"></div>
  <h3 class="sec">Arabic dialect</h3><div class="row"><label>Text<input id="dt" value="إزاي الواحد يعمل ده دلوقتي عشان يكسب وقت"></label><label style="max-width:180px">Adapt to<select id="da"><option value="">(classify only)</option><option>ar-EG</option><option>ar-Gulf</option></select></label><button id="dc">Analyse</button></div><pre id="dout"></pre>`;
  $("#chk").onclick=async()=>{const r=await api("/api/a3/language/check",{source:$("#src").value.split("\n").filter(Boolean),target:$("#tgt").value.split("\n").filter(Boolean),lang:$("#tl").value});
   $("#qa").innerHTML=table(r.lines,[["Source",x=>esc(x.source)],["Translation",x=>esc(x.target)],["Findings",x=>x.findings.length?x.findings.map(f=>`${tag(f.severity,f.severity==="major"?"bad":"warn")} ${esc(f.detail)}`).join("<br>"):tag("no findings","ok")],["Confidence",x=>conf(x.confidence?.score??x.confidence)]])};
  $("#dc").onclick=async()=>{$("#dout").textContent=JSON.stringify(await api("/api/a3/language/dialect",{text:$("#dt").value,adapt_to:$("#da").value||null}),null,1)};
};

PAGES.a3publish=async function(m){
  const [rows,plat]=await Promise.all([api("/api/a3/publish"),api("/api/a3/platforms")]);
  m.innerHTML=`<h2 class="page">Publishing</h2><p class="lead">Uploads are private or draft by default; unlisted or public needs your explicit approval here. Platforms whose API needs an approved app or your login show exactly what is missing. Nothing is uploaded twice.</p>
  <h3 class="sec">Queue</h3>${table(rows,[["Platform",x=>esc(x.platform)],["Title",x=>esc(x.metadata?.title||"")],["Visibility",x=>tag(x.visibility)],["Approval",x=>tag(x.approval,x.approval==="approved_by_owner"?"ok":"warn")],["Status",x=>statusTag(x.status)],
   ["Actions",x=>`<button class="sm" onclick="api('/api/a3/publish/${x.id}/approve',{}).then(()=>go('a3publish')).catch(e=>alert(e.message))">Approve</button> <button class="sm" onclick="api('/api/a3/publish/${x.id}/queue',{}).then(()=>go('a3publish')).catch(e=>alert(e.message))">Upload</button>`]])}
  <h3 class="sec">Package a video</h3><div class="row"><label style="max-width:200px">Platform<select id="pp">${Object.keys(plat).map(k=>`<option>${esc(k)}</option>`).join("")}</select></label><label>Transcript / summary<input id="pt"></label><label style="max-width:220px">Title idea<input id="ptt"></label><button id="pk">Draft metadata</button></div><pre id="pko"></pre>
  <h3 class="sec">Platform capabilities (first-party sources)</h3>${table(Object.values(plat),[["Platform",x=>esc(x.display||x.platform)],["Upload API",x=>esc(x.api?.upload?.summary||x.api?.upload_status||JSON.stringify(x.api?.upload||"").slice(0,90))],["Default visibility",x=>esc(x.api?.default_visibility||x.api?.unaudited_visibility||"")],["Analytics",x=>esc(x.api?.analytics?.summary||JSON.stringify(x.api?.analytics||"").slice(0,90))]])}`;
  $("#pk").onclick=async()=>{try{$("#pko").textContent=JSON.stringify(await api("/api/a3/package",{platform:$("#pp").value,transcript:$("#pt").value||"",title:$("#ptt").value||null}),null,1)}catch(e){$("#pko").textContent=e.message}};
  jobsHere(m)};

PAGES.a3analytics=async function(m){
  const [rec,exps,recs]=await Promise.all([api("/api/a3/analytics"),api("/api/a3/experiments"),api("/api/a3/recommendations")]);
  m.innerHTML=`<h2 class="page">Analytics & Growth</h2><p class="lead">Only your own first-party numbers (CSV exports or the YouTube Analytics API). Experiments need a minimum sample; "inconclusive" is a normal result; patterns are associations, not causes. No virality predictions.</p>
  <h3 class="sec">Import an export</h3><div class="row"><label style="max-width:220px">Export type<select id="ck">${["youtube_studio_table","youtube_studio_daily","tiktok","instagram","facebook"].map(x=>`<option>${x}</option>`).join("")}</select></label><label>CSV text<textarea id="ct"></textarea></label><label style="max-width:200px">Period<input id="cp" placeholder="2026-10-01..2026-10-31"></label><button id="ci">Import</button></div><pre id="cio"></pre>
  <h3 class="sec">Records (${rec.length})</h3>${table(rec.slice(-30),[["Platform",x=>esc(x.platform)],["Video",x=>esc(x.video_id)],["Date",x=>esc(x.date)],["Metrics",x=>esc(Object.entries(x.metrics).filter(([,v])=>v!=null).map(([k,v])=>k+"="+v).slice(0,5).join(", "))],["Source",x=>esc(x.source)]])}
  <h3 class="sec">Recommendations</h3>${recs.recommendations.length?table(recs.recommendations,[["Recommendation",x=>esc(x.text)],["Confidence",x=>conf(x.confidence)],["For",x=>esc(x.target)],["Evidence",x=>esc(JSON.stringify(x.evidence).slice(0,160))]]):`<p class="lead">${esc(recs.note||"No evidence-backed recommendation yet - more data needed.")}</p>`}
  <h3 class="sec">Experiments</h3>${table(exps,[["Hypothesis",x=>esc(x.hypothesis)],["Variable",x=>esc(x.variable)],["Metric",x=>esc(x.metric)],["Min samples",x=>x.min_samples],["Status",x=>statusTag(x.status)],["Result",x=>esc(x.results?.verdict||x.results?.summary||"")],["",x=>`<button class="sm" onclick="api('/api/a3/experiments/${x.id}/evaluate',{}).then(()=>go('a3analytics')).catch(e=>alert(e.message))">Evaluate</button>`]])}
  <div class="row"><label>Hypothesis<input id="eh" placeholder="A question hook keeps more viewers at 3 s"></label><label style="max-width:160px">Variable<input id="ev" value="hook"></label><label style="max-width:160px">Metric<input id="em" value="retention_3s"></label><label style="max-width:110px">Baseline<input id="eb" value="0.6"></label><label style="max-width:110px">Min effect<input id="ed" value="0.05"></label><button id="ec">Plan experiment</button></div>`;
  $("#ci").onclick=async()=>{try{const r=await api("/api/a3/analytics/import",{kind:$("#ck").value,text:$("#ct").value,period:$("#cp").value||null});$("#cio").textContent=JSON.stringify(r,null,1).slice(0,1500)}catch(e){$("#cio").textContent=e.message}};
  $("#ec").onclick=async()=>{try{await api("/api/a3/experiments",{hypothesis:$("#eh").value,variable:$("#ev").value,variants:[{id:"A",desc:"control"},{id:"B",desc:"variant"}],platform:"youtube",metric:$("#em").value,baseline:+$("#eb").value,mde:+$("#ed").value});go("a3analytics")}catch(e){alert(e.message)}};
};
window.A3={ev,conf,unavailable,A3};
})();
