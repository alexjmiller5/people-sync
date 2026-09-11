// Opt-in check on the controller's synthetic /review-smoke.html page only:
// node tests/review_browser.mjs <cdp-eval.mjs> <port> <owned-target-id>
// Batch Smoke: Example (two clusters), Next (one cluster), Unresolved (no clusters).
import {execFileSync} from 'node:child_process';
import assert from 'node:assert/strict';
const [bridge,port,target]=process.argv.slice(2);
assert(bridge&&port&&target,'Pass the bridge, port and synthetic-page target.');
const evaluate=expression=>JSON.parse(execFileSync(process.execPath,
  [bridge,port,'-','--target',target],{
    input:`JSON.stringify((()=>{if(location.pathname!=="/review-smoke.html")throw new Error("Synthetic page only");return (${expression});})())`,
    encoding:'utf8',timeout:15000
  }));
const act=code=>evaluate(`(()=>{${code};return true})()`);
const waitFor=async expression=>{
  for(let i=0;i<50;i++){
    try{if(evaluate(expression))return;}catch{ /* Navigation replaces the execution context. */ }
    await new Promise(resolve=>setTimeout(resolve,100));
  }
  assert.fail('Timed out: '+expression);
};
const reload=async()=>{
  act('window.smokeReloadToken=true;location.reload()');
  await waitFor('!window.smokeReloadToken&&typeof exportReview==="function"&&document.querySelector("#main h1")');
};
const go=name=>act(`$('review-filter').value='';$('group-search').value='';$('batch').value='';renderGroups();[...document.querySelectorAll('.group-link')].find(b=>b.firstElementChild.textContent===${JSON.stringify(name)}).click()`);
const type=content=>act(`$('correction').value=${JSON.stringify(content)};$('correction').dispatchEvent(new Event('input',{bubbles:true}))`);
const changeFilter=value=>act(`$('review-filter').value=${JSON.stringify(value)};$('review-filter').dispatchEvent(new Event('change'))`);

assert.deepEqual(evaluate('DATA.groups.map(g=>g.name)'),['Example','Next','Unresolved']);
assert(evaluate('DATA.groups.every(g=>g.batch==="Batch Smoke")'));
const backup=evaluate('({v1:localStorage.getItem(legacyKey),v2:localStorage.getItem(storageKey)})');
try{
  act(`localStorage.removeItem(storageKey);
    const g=DATA.groups.find(g=>g.name==='Example');
    const prior={decisions:{[JSON.stringify(['person-1','google_contacts:example'])]:{person_id:'person-1',record_id:'google_contacts:example',source:'google_contacts',choice:'unsure'}},notes:{[g.key]:'Legacy note: keep both circles'},reviewed:{[g.key]:true}};
    localStorage.setItem(legacyKey,' '+JSON.stringify(prior)+' ')`);
  await reload();
  const legacy=evaluate('localStorage.getItem(legacyKey)');
  go('Example');
  assert.equal(evaluate('document.querySelectorAll(".cluster").length'),2);
  assert.equal(evaluate('document.querySelectorAll("#main select,.person-tab,.decision").length'),0);
  assert.deepEqual(evaluate('[...document.querySelectorAll(".cluster .full-evidence")].map(c=>({people:[...c.querySelectorAll("[data-person-id]")].map(p=>p.dataset.personId),records:[...c.querySelectorAll("[data-record-id]")].map(p=>p.dataset.recordId)}))'),[
    {people:['person-1'],records:['google_contacts:example','instagram:example']},
    {people:['person-2'],records:['spotify:example']}
  ]);
  assert.deepEqual(evaluate('[...document.querySelectorAll(".cluster-overview .tag")].map(x=>x.textContent)'),['Circle A','Circle B']);
  assert(evaluate('[...document.querySelectorAll(".full-evidence")].every(d=>!d.open&&d.querySelector("summary").textContent==="See full account details")'));
  assert.equal(evaluate('document.querySelectorAll(".cluster-overview .photo").length'),1);
  assert(evaluate('document.querySelector(".cluster-overview").textContent.includes("instagram")'));
  assert.equal(evaluate('window.sourceExecuted||false'),false);
  assert.equal(evaluate('[...document.querySelectorAll("a")].some(a=>a.protocol==="javascript:")'),false);
  assert(evaluate('document.getElementById("main").textContent.includes("</script><script>window.sourceExecuted=true</script>")'));
  assert(evaluate('$("legacy-context").textContent.includes("Legacy note: keep both circles")&&$("legacy-context").textContent.includes("unsure")'));
  assert.equal(evaluate('isReviewed(group())'),false,'legacy reviewed flag is not proposal approval');

  // Repeated cache entries all stay with the assigned record; prior links stay visible.
  act(`const p=group().profiles[0];group().profiles.push({...p,status:'matched',person_id:'person-outside'});render()`);
  assert.equal(evaluate('document.querySelector(".cluster").querySelectorAll(".profile-card").length'),2);
  assert(evaluate('document.querySelector(".profile-grid").textContent.includes("Already linked in People DB")'));
  act('group().profiles.pop();render()');

  act('document.querySelector(".photo").focus();document.activeElement.click()');
  assert.equal(evaluate('$("lightbox").open'),true);
  await waitFor('$("large-photo").naturalWidth>0');
  act('$("close-photo").click()');
  assert.equal(evaluate('$("lightbox").open'),false);
  assert.equal(evaluate('document.documentElement.scrollWidth>innerWidth'),false);
  act("$('group-search').value='Next';$('group-search').dispatchEvent(new Event('input'))");
  assert.equal(evaluate('group().name'),'Next');
  go('Example');
  act('$("skip-next").click()');
  assert.equal(evaluate('group().name'),'Next');
  assert.equal(evaluate('isReviewed(DATA.groups[0])'),false,'skip leaves response pending');
  go('Example');
  changeFilter('pending');
  act('$("approve-next").click()');
  assert.equal(evaluate('group().name'),'Next','approval advances to the immediate pending group');
  assert.equal(evaluate('isReviewed(DATA.groups[0])'),true);
  type('Keep both people and every circle');
  assert.equal(evaluate('$("approve-next").disabled'),true);
  assert.equal(evaluate('isReviewed(group())'),false,'typing is an autosaved draft');
  act('$("correction-next").click()');
  assert.equal(evaluate('group().name'),'Unresolved','correction advances under pending filter');
  assert.equal(evaluate('$("approve-next").disabled'),true);
  assert.equal(evaluate('$("correction-next").disabled'),true);
  assert(evaluate('$("main").textContent.includes("Who is this?")'));
  assert(evaluate('$("unassigned").querySelector("[data-person-id=person-unknown]")!==null'));
  const before=evaluate('exportReview()');
  assert.equal(before.schema_version,2);
  assert.equal(before.legacy_v1_raw,legacy);
  assert.deepEqual(before.legacy_v1,JSON.parse(legacy));
  assert.deepEqual(before.groups[2].unresolved,{person_ids:['person-unknown'],record_ids:[]});
  assert.equal(before.groups[0].response.proposal_id,before.groups[0].proposal_id);
  assert.equal(before.groups[0].response.type,'approval');
  assert.equal(before.groups[1].response.type,'correction');
  await reload();
  assert.deepEqual(evaluate('exportReview().groups'),before.groups);
  assert.equal(evaluate('localStorage.getItem(legacyKey)'),legacy);

  // Simulate saved responses to an earlier proposal; the current page's digest stays authoritative.
  act(`const s=JSON.parse(localStorage.getItem(storageKey));s.responses[DATA.groups[0].key].proposal_id='previous-proposal';localStorage.setItem(storageKey,JSON.stringify(s))`);
  await reload();
  go('Example');
  assert.equal(evaluate('isReviewed(group())'),false,'changed digest invalidates approval');
  assert(evaluate('$("previous-response").textContent.includes("Proposal changed")'));
  assert.equal(evaluate('isReviewed(DATA.groups[1])'),true,'unrelated correction stays reviewed');
  act('$("approve-next").click()');
  act(`const s=JSON.parse(localStorage.getItem(storageKey));s.responses[DATA.groups[1].key].proposal_id='previous-proposal';localStorage.setItem(storageKey,JSON.stringify(s))`);
  await reload();
  go('Next');
  assert.equal(evaluate('isReviewed(group())'),false);
  assert.equal(evaluate('$("correction").value'),'Keep both people and every circle');
  assert(evaluate('$("previous-response").textContent.includes("Keep both people and every circle")'));
  assert.equal(evaluate('isReviewed(DATA.groups[0])'),true,'unrelated approval stays reviewed');
  type('Keep both people and every circle; check school dates');
  act('$("correction-next").click()');
  assert(evaluate('exportReview().groups[1].previous_responses.some(r=>r.text==="Keep both people and every circle")'));
  go('Example');
  type('Correction draft after approval');
  assert.equal(evaluate('isReviewed(group())'),false,'typing immediately invalidates approval');
  assert.equal(evaluate('$("approve-next").disabled'),true);
  await reload();
  go('Example');
  assert.equal(evaluate('$("correction").value'),'Correction draft after approval');
  assert.equal(evaluate('isReviewed(group())'),false);
  type('');
  assert.equal(evaluate('$("correction-next").disabled'),true);
  assert.equal(evaluate('$("approve-next").disabled'),false);
  assert.equal(evaluate('isReviewed(group())'),false,'clearing draft does not restore approval');
  act('$("approve-next").click()');
  go('Unresolved');
  type('Keep this person pending until identified');
  assert.equal(evaluate('$("approve-next").disabled'),true,'question-only group cannot approve');
  changeFilter('pending');
  act('$("correction-next").click()');
  assert.equal(evaluate('active'),-1,'last pending response reaches the empty queue');
  assert.equal(evaluate('document.querySelectorAll(".group-link").length'),0);
  assert.equal(evaluate('exportReview().groups[2].response.type'),'correction');
  assert.equal(evaluate('localStorage.getItem(legacyKey)'),legacy);
  go('Example');
  assert.equal(evaluate('document.documentElement.scrollWidth>innerWidth'),false);
  console.log('PASS: grouped cards, pending navigation, approval/correction drafts and reload, digest invalidation, legacy preservation/export, safe rendering, photo enlargement, search and no overflow.');
}finally{
  act(`for(const [key,value]of [[legacyKey,${JSON.stringify(backup.v1)}],[storageKey,${JSON.stringify(backup.v2)}]]){if(value===null)localStorage.removeItem(key);else localStorage.setItem(key,value)}`);
  await reload();
}
