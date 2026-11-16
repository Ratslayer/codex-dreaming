// Synthetic interaction checks; no live KB or network is used.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'skills/agent-kb-review/assets/review-checklist.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const exportsBox={exports:{}};
vm.runInNewContext(script,{module:exportsBox});
const {makeSubmission,makePrompt}=exportsBox.exports;
function fixture(preview=false) {
  const entry=(id,edit)=>({id,title:'Rule '+id,text:'Exact original rule '+id,lesson_ids:['lesson-'+id],lesson_versions:{['lesson-'+id]:'b'.repeat(64)},rationale:'Recorded correction.',evidence:[{quote:'Supporting source passage.',source_path:'/tmp/kb/.aggregator/sources/source.md'}],...(edit?{requested_text:'Displayed narrower wording.'}:{})});
  return {schema_version:2,review_id:'review-1',kb_root:'/tmp/kb',preview,groups:[
    {project:'First',proposal_id:'proposal-first',proposal_hash:'a'.repeat(64),assessment_versions:{session:'c'.repeat(64)},entries:[entry('one',true),entry('two')]},
    {project:'Second',proposal_id:'proposal-second',proposal_hash:'d'.repeat(64),entries:[entry('three')]}
  ]};
}
class El {
  constructor(tag='div'){this.tag=tag;this.children=[];this.dataset={};this.events={};this.checked=false;this.open=false;this.hidden=false;this.disabled=false;this.textContent='';}
  append(...children){this.children.push(...children);}
  addEventListener(name,fn){this.events[name]=fn;}
}
function walk(node){return [node,...node.children.flatMap(walk)];}
function mount(data,host) {
  const form=new El('form'),groups=new El(),button=new El('button'),count=new El(),status=new El(),error=new El(),preview=new El(),help=new El();
  error.hidden=true;preview.hidden=true;
  const controls={'form':form,'[data-groups]':groups,'[data-publish]':button,'[data-count]':count,'[data-status]':status,'[data-error]':error,'[data-preview]':preview,'[data-help]':help};
  const root={querySelector(key){assert(controls[key],key);return controls[key];}};
  const window={openai:host,events:{},addEventListener(k,fn){this.events[k]=fn;}};
  const document={createElement(tag){return new El(tag);},getElementById(id){if(id==='agent-kb-review-live')return root;if(id==='agent-kb-review-data')return {textContent:JSON.stringify(data)};throw Error('Unknown element '+id);}};
  vm.runInNewContext(script,{document,window,setTimeout(fn){fn();},crypto:{randomUUID(){return 'submission-test';}}});
  const elements=walk(groups),boxes=elements.filter(e=>e.tag==='input'),folds=elements.filter(e=>e.tag==='details');
  return {...controls,form,groups,button,count,status,error,preview,help,boxes,folds,elements,window};
}
function check(ui,index){ui.boxes[index].checked=true;ui.boxes[index].events.change();}
function request(message){return JSON.parse(message.prompt.split('\n\n').slice(1).join('\n\n'));}

(async()=>{
  const data=fixture(),pub=makeSubmission(data,['one'],'submission-1');
  assert.equal(pub.action,'publish_selected_kb_rules');
  assert.equal(pub.choices.length,1);
  assert.deepEqual(Array.from(pub.choices[0].selected_entry_ids),['one']);
  assert.deepEqual(Array.from(pub.choices[0].unchecked_entry_ids),['two']);
  assert.equal(pub.choices[0].selected_entries[0].text,data.groups[0].entries[0].text);
  assert.equal(pub.choices[0].edits.one,'Displayed narrower wording.');
  assert.equal(pub.choices[0].reviewed_hash,'a'.repeat(64));
  assert(makePrompt(pub).includes('without a second approval'));
  assert.throws(()=>makeSubmission(data,[],'s'));
  assert.throws(()=>makeSubmission(data,['one','one'],'s'));
  assert.throws(()=>makeSubmission(data,['unknown'],'s'));
  assert.throws(()=>makeSubmission(fixture(true),['one'],'s'));
  const mergeData=fixture();
  const originals=mergeData.groups[0].entries.map(e=>({id:e.id,text:e.text,lesson_ids:e.lesson_ids,lesson_versions:e.lesson_versions}));
  const merged={id:'merge-one',title:'Combined rule',text:'Equivalent combined guidance.',lesson_ids:originals.flatMap(e=>e.lesson_ids),lesson_versions:Object.assign({},...originals.map(e=>e.lesson_versions)),replaces:originals,merge_conditions:'Same condition; retain all exceptions.',rationale:'Equivalent guidance.',evidence:mergeData.groups[0].entries[0].evidence};
  mergeData.groups[0].entries.push(merged);
  const mergeRequest=makeSubmission(mergeData,['merge-one'],'merge-submit');
  assert.equal(mergeRequest.choices[0].selected_entries[0].replaces.length,2);
  assert(mergeRequest.authorization.includes('listed originals atomically'));
  assert.throws(()=>makeSubmission(mergeData,['merge-one','one'],'merge-conflict'),/originals/);
  const mergeUI=mount(mergeData,{sendFollowUpMessage(){return Promise.resolve();}});
  assert(mergeUI.elements.some(e=>e.tag==='summary'&&e.textContent.startsWith('Merge ·')));
  assert(mergeUI.elements.some(e=>e.tag==='dt'&&e.textContent==='Replaces 2 entries'));
  assert(mergeUI.elements.some(e=>e.tag==='dd'&&e.textContent.includes('Exact original rule one')&&e.textContent.includes('Exact original rule two')));

  const sent=[],saved=[];
  let complete;
  const host={setWidgetState(state){saved.push(state);return Promise.resolve();},sendFollowUpMessage(message){sent.push(message);return new Promise(resolve=>{complete=resolve;});}};
  const ui=mount(data,host);
  assert(ui.button.disabled);assert.equal(saved.length,0);assert.equal(sent.length,0);
  assert(ui.folds.every(f=>!f.open));
  check(ui,0);assert(!ui.button.disabled);assert(ui.folds.every(f=>!f.open));
  ui.folds[1].open=true;ui.folds[1].children[0].events.click();assert(!ui.boxes[1].checked);
  assert.equal(saved.at(-1).modelContent.intent,'selection_preview_only');
  const inFlight=ui.form.events.submit({preventDefault(){}});
  await ui.form.events.submit({preventDefault(){}});
  assert.equal(sent.length,1);assert(ui.boxes.every(b=>b.disabled));
  assert.equal(request(sent[0]).choices[0].edits.one,'Displayed narrower wording.');
  complete();await inFlight;
  assert(ui.status.textContent.includes('Publication requested'));
  await ui.form.events.submit({preventDefault(){}});assert.equal(sent.length,1);
  const restored=mount(data,{...host,widgetState:saved.at(-1)});
  assert(restored.boxes[0].checked);assert.equal(sent.length,1,'Restoring choices must not send a publication request');
  const legacy=mount(data,{...host,widgetState:{schema_version:1,intent:'selection_only_not_approval',selected_entry_ids:['one']}});
  assert(legacy.boxes.every(b=>!b.checked));
  const stale=mount({...data,review_id:'new-review'},{...host,widgetState:saved.at(-1)});
  assert(stale.boxes.every(b=>!b.checked));
  const previewUI=mount(fixture(true),host);check(previewUI,0);
  await previewUI.form.events.submit({preventDefault(){}});
  assert.equal(sent.length,1);assert(previewUI.status.textContent.includes('nothing published'));
  const offline=mount(data,{});check(offline,0);await offline.form.events.submit({preventDefault(){}});
  assert(offline.error.textContent.includes('Nothing was submitted'));
  const failures=[];
  const retryUI=mount(data,{setWidgetState(){return Promise.resolve();},sendFollowUpMessage(message){failures.push(message);return Promise.reject(Error('unknown outcome'));}});
  check(retryUI,0);
  await retryUI.form.events.submit({preventDefault(){}});
  assert(retryUI.error.textContent.includes('Could not confirm'));
  await retryUI.form.events.submit({preventDefault(){}});
  assert.equal(request(failures[0]).submission_id,request(failures[1]).submission_id);
  const sourceMessages=[];
  const sourceUI=mount(data,{sendFollowUpMessage(message){sourceMessages.push(message);return Promise.resolve();}});
  const sourceButton=sourceUI.elements.find(e=>e.tag==='button'&&e.textContent==='View source');
  await sourceButton.events.click();
  assert.equal(request(sourceMessages[0]).action,'view_kb_source');
  assert(!sourceMessages[0].prompt.includes('publish_selected_kb_rules'));
  console.log('PASS: scoped publication payload, edits, zero/duplicate selections, independent foldouts, restored/legacy/stale state, local preview, duplicate submit, stable retries, and read-only evidence action.');
})().catch(error=>{console.error(error);process.exitCode=1;});
