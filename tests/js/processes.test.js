const test = require('node:test');
const assert = require('node:assert/strict');
const processes = require('../../src/oms_hub/web/static/processes.js');
const helpers = require('../../src/oms_hub/web/static/study_generation.js');
test('process views keep failures visible and search lecture filenames', () => {
  const rows = [{title:'Lecture 1 transcript', state:'needs_review'}, {title:'Lecture 2 slides', state:'complete'}, {title:'Quiz', state:'paused'}];
  assert.deepEqual(processes.selectItems(rows, '', 'active'), [rows[0], rows[2]]);
  assert.deepEqual(processes.selectItems(rows, 'LECTURE', 'complete'), [rows[1]]);
  assert.deepEqual(processes.selectItems(rows, '  transcript ', 'all'), [rows[0]]);
  assert.deepEqual(processes.selectItems([{...rows[0], lecture_id:110}, {...rows[1], lecture_id:111}], '', 'all', '110'), [{...rows[0], lecture_id:110}]);
});
test('cards render untrusted titles as text and reject external detail links', () => {
  const document = {baseURI:'https://studyhub.example/processes', createElement(tag) { return {tag, dataset:{}, children:[], append(...els){this.children.push(...els);}, addEventListener(_,fn){this.click=fn;}}; }};
  let action;
  const item = {id:'ingestion:12',job_id:'12',family:'ingestion',title:'<img onerror=alert(1)>',state:'needs_review',detail_url:'https://untrusted.example',actions:{restart:{enabled:true},pause:{enabled:false,reason:'Finished'}}};
  const result = processes.card(document,item,(_,name)=>{action=name;},helpers);
  assert.equal(result.children[1].textContent,item.title);
  const controls=result.children.find(el=>el.className==='process-card-actions').children;
  assert.equal(controls.some(el=>el.tag==='a'),false);
  assert.equal(controls[0].disabled,true);
  assert.equal(controls[1].disabled,false);
  controls[1].click(); assert.equal(action,'restart');
});
