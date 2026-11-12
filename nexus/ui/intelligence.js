// Account 3 workspace: research, references, AI models, languages, publishing, analytics, growth.
// Loaded after index.html's main script; it only ADDS a menu group and pages (no edits to other pages).
(function(){
const ev=(list)=>(list||[]).map(e=>e.url?`<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.source)}</a>`:esc(e.source)).join(", ");
const conf=c=>c==null?"":tag(`confidence ${Math.round(c*100)}%`,c>=0.7?"ok":c>=0.4?"warn":"bad");
const unavailable=(m,title,e)=>{m.innerHTML=`<h2 class="page">${esc(title)}</h2><div class="note">${esc(e.message)}</div>`};
const A3=[["models","AI Models & Router","live"]];
MODULES.push(["Intelligence (Account 3)",A3]);

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
window.A3={ev,conf,unavailable,A3};
})();
