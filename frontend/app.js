const $ = (id) => document.getElementById(id);
let activeProposal = null;

function pct(value) { return value == null ? '—' : `${Math.round(value * 100)}%`; }
function notify(title, message, error=false) {
  const el = $('notice'); el.className = `notice show${error ? ' error' : ''}`;
  el.querySelector('strong').textContent = title; el.querySelector('p').textContent = message;
}
async function api(path, options={}) {
  const credential = $('api-key')?.value.trim();
  const auth = credential ? {'X-API-Key': credential} : {};
  const response = await fetch(path, {headers:{'Content-Type':'application/json',...auth,...(options.headers||{})}, ...options});
  if (!response.ok) { const body = await response.json().catch(()=>({detail:response.statusText})); throw new Error(body.detail || response.statusText); }
  return response.json();
}
async function refresh() {
  const overview = await api('/api/v1/overview');
  $('runs').textContent = overview.pipeline_runs;
  $('quality').textContent = pct(overview.latest_quality_score);
  $('silent').textContent = overview.silent_failures;
  $('accuracy').textContent = pct(overview.rca_accuracy);
  $('pending').textContent = overview.pending_approvals;
}
function renderQuality(metrics) {
  $('quality-bars').innerHTML = metrics.map(m => `<div class="quality-row ${m.passed?'':'failed'}"><span>${m.dimension.replaceAll('_',' ')}</span><div class="bar"><i style="width:${m.score*100}%"></i></div><strong>${Math.round(m.score*100)}</strong></div>`).join('');
}
function renderResult(result, metrics) {
  $('pipeline-status').textContent = result.pipeline_status;
  $('pipeline-status').style.color = result.pipeline_status === 'SUCCESS' ? '#1f6b4f' : '#b94b43';
  $('states').innerHTML = result.workflow_states.map((state,i)=>`<li><span>${String(i+1).padStart(2,'0')}</span>${state.replaceAll('_',' ').toLowerCase()}</li>`).join('');
  $('row-delta').textContent = result.scenario === 'incorrect_join' ? '10 rows ← 8 source' : result.pipeline_status === 'SUCCESS' ? '8 rows ← 8 source' : 'not materialized';
  renderQuality(metrics);
  if (!result.rca) {
    $('root-cause').textContent = 'No anomaly detected'; $('confidence').textContent = 'PASS';
    $('evidence').innerHTML = '<p class="muted">All operational and logical quality gates passed.</p>'; activeProposal=null; resetProposal(); return;
  }
  $('root-cause').textContent = result.rca.probable_root_cause.replaceAll('_',' ');
  $('confidence').textContent = `${Math.round(result.rca.confidence*100)}%`;
  $('evidence').innerHTML = result.rca.evidence.map(e=>`<div class="evidence-item"><small>${e.kind} · ${e.source}</small><strong>${e.claim}</strong><div>${e.observed}</div></div>`).join('');
  if (result.rca.recommended_action) {
    const p=result.rca.recommended_action; activeProposal=p.proposal_id; $('risk').textContent=`${p.risk} RISK · APPROVAL REQUIRED`; $('fix-title').textContent=p.title; $('sql').textContent=p.sql;
    const v=result.validation; $('before').textContent=pct(v.before_score); $('after').textContent=pct(v.after_score); $('checks').innerHTML=Object.entries(v.checks).map(([k,ok])=>`<li>${ok?'✓':'×'} ${k.replaceAll('_',' ')}</li>`).join(''); $('approve').disabled=!v.passed;
  } else { activeProposal=null; resetProposal(); }
}
function resetProposal(){ $('risk').textContent='NO PROPOSAL'; $('fix-title').textContent='No bounded fix was generated for this incident.'; $('sql').textContent='-- no proposal'; $('before').textContent='—'; $('after').textContent='—'; $('checks').innerHTML='<li>No shadow validation</li>'; $('approve').disabled=true; }
async function runScenario(scenario) {
  document.querySelectorAll('button[data-scenario]').forEach(b=>b.disabled=true); notify('Workflow running', `Profiling ${scenario.replaceAll('_',' ')} data…`);
  try { const result=await api('/api/v1/demo/run',{method:'POST',body:JSON.stringify({scenario})}); const metrics=await api(`/api/v1/runs/${result.run_id}/quality`); renderResult(result,metrics); await refresh(); notify(result.incident_id?'Incident detected':'Quality gates passed', result.incident_id ? 'Evidence, RCA, and shadow validation are ready for review.' : 'Operational and logical checks agree.'); }
  catch(error){ notify('Workflow failed',error.message,true); }
  finally{ document.querySelectorAll('button[data-scenario]').forEach(b=>b.disabled=false); }
}
document.querySelectorAll('button[data-scenario]').forEach(button=>button.addEventListener('click',()=>runScenario(button.dataset.scenario)));
$('approve').addEventListener('click',async()=>{ if(!activeProposal)return; $('approve').disabled=true; try { await api(`/api/v1/proposals/${activeProposal}/approval`,{method:'POST',body:JSON.stringify({decision:'APPROVED',reason:'Exact artifact and isolated shadow evidence reviewed.'})}); const result=await api(`/api/v1/proposals/${activeProposal}/execute`,{method:'POST'}); notify('Approved deployment independently verified',`${result.message} Verified quality: ${pct(result.verified_quality_score)}`); $('approve').textContent='Deployed & verified'; await refresh(); } catch(error){notify('Approval failed',`${error.message}. Enter a configured approver/executor API key.`,true);$('approve').disabled=false;} });
$('api-key').addEventListener('change',()=>refresh().catch(error=>notify('Credential rejected',error.message,true)));
refresh().catch(error=>notify('API unavailable',error.message,true));
