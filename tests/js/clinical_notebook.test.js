const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function page(status, {settings=false, notebooks=[{id:'notebook:exam3',title:'Cardio Exam 3'}]} = {}) {
  class Element {
    constructor() { this.children=[]; this.handlers={}; this.value=''; this.checked=true; this.hidden=false; }
    get options() { return this.children; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children=items; }
    addEventListener(event,fn) { this.handlers[event]=fn; }
    reset() { this.value=''; }
  }
  const names=['message','connection','jobs','setup','destination','send','change','picker','notebook','save','slides','transcript',...(settings?['notebooks','notebook-list','connect','disconnect']:[])];
  const nodes=Object.fromEntries(names.map(name=>[name,new Element()]));
  const root={dataset:{csrf:'csrf',...(settings?{}:{lectureId:'7'})},querySelector:selector=>nodes[selector.slice(9,-1)] || null,querySelectorAll:()=>[]};
  const calls=[], timers=[];
  vm.runInNewContext(fs.readFileSync(require.resolve('../../src/oms_hub/web/static/clinical_notebook.js'),'utf8'),{
    document:{querySelector:()=>root,createElement:()=>new Element()},FormData,
    window:{addEventListener:()=>{}},setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout:()=>{},
    fetch:async (url,options={})=>{calls.push({url,options});return {ok:true,json:async()=>url.includes('/status')?status:url.endsWith('/notebooks')?{items:notebooks}:{saved:true,job_id:'job1'}};},
  });
  return {nodes,calls,timers};
}
const saved={configured:true,installation:'My Mac',endpoint:'https://clinical.test/api/external/v1',destination:{notebook_id:'notebook:exam3',notebook_title:'Cardio Exam 3'},jobs:[]};

test('saved exam sends selected materials with the displayed installation',async()=>{
  const {nodes,calls}=page(saved);
  await new Promise(setImmediate);
  assert.equal(nodes.send.disabled,false);
  assert.match(nodes.destination.textContent,/My Mac/);
  assert.match(nodes.destination.textContent,/Cardio Exam 3/);
  nodes.transcript.checked=false;
  await nodes.send.handlers.click();
  const send=calls.find(x=>x.url.endsWith('/lectures/7/send'));
  assert.equal(send.options.body.get('slides'),'true');
  assert.equal(send.options.body.get('transcript'),'false');
  assert.equal(send.options.body.get('csrf_token'),'csrf');
});

test('first use loads explicit notebook choices without sending',async()=>{
  const {nodes,calls}=page({...saved,destination:null});
  await new Promise(setImmediate);
  assert.equal(nodes.send.disabled,true);
  assert.equal(nodes.picker.hidden,false);
  assert.equal(nodes.notebook.options[1].textContent,'Cardio Exam 3');
  assert.equal(calls.filter(x=>x.url.endsWith('/send')).length,0);
  nodes.notebook.value='notebook:exam3';
  await nodes.save.handlers.click();
  assert.equal(calls.find(x=>x.url.endsWith('/destination')).options.body.get('notebook_id'),'notebook:exam3');
});

const duplicateNotebooks=[
  {id:'notebook:cardio-a',title:'Cardiology Exam 1'},
  {id:'notebook:cardio-b',title:'Cardiology Exam 1'},
  {id:'notebook:renal',title:'Renal Exam 1'},
];
const duplicateLabels=[
  'Cardiology Exam 1 — notebook:cardio-a',
  'Cardiology Exam 1 — notebook:cardio-b',
  'Renal Exam 1',
];

test('duplicate notebook choices show stable IDs and save the selected ID',async()=>{
  const {nodes,calls}=page({...saved,destination:null},{notebooks:duplicateNotebooks});
  await new Promise(setImmediate);
  assert.deepEqual(nodes.notebook.options.slice(1).map(option=>option.textContent),duplicateLabels);
  assert.deepEqual(nodes.notebook.options.slice(1).map(option=>option.value),duplicateNotebooks.map(item=>item.id));
  for (const id of ['notebook:cardio-b','notebook:cardio-a']) {
    nodes.notebook.value=id;
    nodes.notebook.handlers.change();
    assert.equal(nodes.save.disabled,false);
    await nodes.save.handlers.click();
    assert.equal(calls.filter(call=>call.url.endsWith('/destination')).at(-1).options.body.get('notebook_id'),id);
  }
  assert.equal(calls.filter(call=>call.url.endsWith('/send')).length,0);
});

test('settings notebook list disambiguates duplicates and keeps unique titles unchanged',async()=>{
  const {nodes}=page({...saved,destination:null},{settings:true,notebooks:duplicateNotebooks});
  await new Promise(setImmediate);
  assert.deepEqual(nodes['notebook-list'].children.map(item=>item.textContent),duplicateLabels);
});

test('offline or partial receipts never trigger automatic retransmission',async()=>{
  const job={id:'job1',notebook_title:'Exam 3',installation:'My Mac',status:'partial',can_retry:true,files:[{filename:'slides.pdf',status:'accepted',message:'Received',receipt:{status:'accepted',warnings:[]}},{filename:'transcript.txt',status:'pending',message:'Open Clinical Notebook and Retry',receipt:null}]};
  const {calls,timers,nodes}=page({...saved,jobs:[job]});
  await new Promise(setImmediate);
  for(let n=0;n<25 && timers.length;n++) {await timers.shift()();await new Promise(setImmediate);}
  assert.equal(timers.length,0);
  assert.ok(calls.filter(x=>x.url.endsWith('/check')).length>0);
  assert.equal(calls.filter(x=>/\/(retry|send)$/.test(x.url)).length,0);
  assert.match(nodes.jobs.children[0].children[0].textContent,/partial/);
});

test('unconfigured lecture directs setup and makes no provider request',async()=>{
  const {nodes,calls}=page({configured:false,jobs:[]});
  await new Promise(setImmediate);
  assert.equal(nodes.setup.hidden,false);
  assert.equal(nodes.send.disabled,true);
  assert.equal(calls.length,1);
  assert.match(calls[0].url,/status\?lecture_id=7$/);
});
