(() => {
  'use strict';
  if (typeof document === 'undefined') return;
  const root = document.querySelector('[data-clinical-notebook]');
  if (!root) return;
  const find = name => root.querySelector(`[data-cn-${name}]`);
  const endpoint = '/settings/clinical-notebook', lecture = root.dataset.lectureId;
  const message = find('message'), notebook = find('notebook');
  let configured = false, destination = null, busy = false, choosing = false, loaded = false, timer, closed = false;
  const checks = new Map();
  async function post(path, fields = {}) {
    const body = fields instanceof FormData ? fields : new FormData();
    if (!(fields instanceof FormData)) Object.entries(fields).forEach(([key,value]) => body.set(key,value));
    body.set('csrf_token',root.dataset.csrf);
    const response = await fetch(endpoint + path,{method:'POST',body,cache:'no-store'});
    let data;
    try { data = await response.json(); } catch { throw new Error('Hub response unavailable. Reload and sign in.'); }
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The Hub could not complete this request.');
    return data;
  }
  function availability() {
    root.querySelectorAll('button').forEach(button => {button.disabled = busy;});
    if (find('send')) find('send').disabled = busy || !configured || !destination || !['slides','transcript'].some(role => find(role)?.checked && !find(role)?.disabled);
    if (find('save')) find('save').disabled = busy || !notebook?.value;
    if (find('notebooks')) find('notebooks').disabled = busy || !configured;
    if (notebook) notebook.disabled = busy || notebook.options.length < 2;
  }
  async function action(work) {
    if (busy) return;
    busy = true; availability(); message.textContent = 'Working…';
    try { await work(); message.textContent = ''; }
    catch (error) { message.textContent = error.message; }
    finally { busy = false; availability(); }
  }
  function element(tag,text) {const item=document.createElement(tag);item.textContent=text;return item;}
  async function loadNotebooks() {
    const data=await post('/notebooks');
    if (notebook) {
      const first=element('option','Choose an existing notebook'); first.value='';
      notebook.replaceChildren(first);
      data.items.forEach(item=>{const option=element('option',item.title);option.value=item.id;notebook.append(option);});
    }
    if (find('notebook-list')) {
      find('notebook-list').replaceChildren(...data.items.map(item=>element('li',item.title)));
      if (!data.items.length) find('notebook-list').textContent='No notebooks are granted. Update this connection in Clinical Notebook Connected Apps.';
    }
    loaded=true; availability();
  }
  function render(jobs) {
    const list=find('jobs');list.replaceChildren();
    if (!jobs.length) {list.textContent='No deliveries yet.';return;}
    jobs.forEach(job=>{
      const card=element('article','');card.className='settings-subsection';
      const title=element('h3',`${job.notebook_title} · ${job.installation} · ${job.status.replaceAll('_',' ')}`);
      const files=element('ul','');
      job.files.forEach(file=>{
        const warnings=file.receipt?.warnings?.length ? ` · ${file.receipt.warnings.join('; ')}` : '';
        files.append(element('li',`${file.filename} — ${file.status.replaceAll('_',' ')} · ${file.message}${warnings}`));
      });
      card.append(title,files);
      if (job.can_retry) card.append(element('p','Open Clinical Notebook and check its connection, then retry missing files. Received files are retained.'));
      else if (job.status === 'pending') card.append(element('p','Clinical Notebook has not confirmed acceptance yet. Refresh readiness to check progress.'));
      const button=(text,operation)=>{
        const button=element('button',text);button.type='button';button.className='sh-btn sh-btn--secondary';
        button.addEventListener('click',()=>action(async()=>{await post(`/jobs/${encodeURIComponent(job.id)}/${operation}`);await refresh(false);}));
        card.append(button);
      };
      if(job.can_retry) button('Retry missing files','retry');
      if(job.files.some(file=>file.receipt)) button('Refresh readiness','check');
      list.append(card);
    });
  }
  async function refresh(poll=true) {
    if (closed) return;
    clearTimeout(timer);
    const response=await fetch(endpoint+'/status'+(lecture?`?lecture_id=${encodeURIComponent(lecture)}`:''),{cache:'no-store'});
    if(!response.ok) throw new Error('Hub sign-in is unavailable. Reload the page.');
    const data=await response.json();
    configured=data.configured;destination=data.destination||null;
    if(find('connection')) find('connection').textContent=configured?`Saved connection: ${data.installation} · ${data.endpoint}`:'Connect Clinical Notebook to send materials.';
    if(find('setup')) find('setup').hidden=!!configured;
    if(find('disconnect')) find('disconnect').hidden=!configured;
    if(find('destination')) find('destination').textContent=destination?`${destination.notebook_title} · ${data.installation}`:configured?`Choose an exam destination · ${data.installation}`:'';
    if(find('picker')) find('picker').hidden=!configured || (!!destination && !choosing);
    if(find('change')) find('change').hidden=!destination;
    render(data.jobs||[]); availability();
    if(configured && !loaded && (!lecture || !destination || choosing)) await loadNotebooks();
    let pending=false;
    if(configured && poll) for(const job of data.jobs||[]) {
      if(!job.files.some(file=>file.receipt && ['accepted','pending'].includes(file.status)) || (checks.get(job.id)||0)>=15) continue;
      checks.set(job.id,(checks.get(job.id)||0)+1);
      await post(`/jobs/${encodeURIComponent(job.id)}/check`);pending=true;
    }
    if(pending || (data.jobs||[]).some(job=>job.status==='sending')) timer=setTimeout(()=>refresh().catch(error=>{message.textContent=error.message;}),4000);
  }
  find('connect')?.addEventListener('submit',event=>{
    event.preventDefault();
    action(async()=>{await post('/connect',new FormData(event.target));event.target.reset();loaded=false;await refresh();});
  });
  find('disconnect')?.addEventListener('click',()=>action(async()=>{await post('/disconnect');loaded=false;await refresh(false);}));
  find('notebooks')?.addEventListener('click',()=>action(loadNotebooks));
  find('change')?.addEventListener('click',()=>action(async()=>{choosing=true;loaded=false;await refresh(false);}));
  find('save')?.addEventListener('click',()=>action(async()=>{await post(`/lectures/${lecture}/destination`,{notebook_id:notebook.value});choosing=false;await refresh(false);}));
  find('send')?.addEventListener('click',()=>action(async()=>{
    await post(`/lectures/${lecture}/send`,Object.fromEntries(['slides','transcript'].map(role=>[role,String(!!find(role)?.checked && !find(role)?.disabled)])));
    await refresh();
  }));
  notebook?.addEventListener('change',availability);
  ['slides','transcript'].forEach(role=>find(role)?.addEventListener('change',availability));
  window.addEventListener('pagehide',()=>{closed=true;clearTimeout(timer);});
  refresh().catch(error=>{message.textContent=error.message;});
})();
