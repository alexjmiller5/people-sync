// Opt-in browser check on a synthetic page, through the existing CDP bridge:
// node tests/review_browser.mjs <cdp-eval.mjs> <port> <owned-target-id>
import {execFileSync} from 'node:child_process';
import assert from 'node:assert/strict';
const [bridge,port,target]=process.argv.slice(2);
assert(bridge&&port&&target,'Pass the bridge, port and synthetic-page target.');
const evaluate=expression=>JSON.parse(execFileSync(process.execPath,
  [bridge,port,'-','--target',target],{input:`JSON.stringify(${expression})`,encoding:'utf8'}));
assert(evaluate('location.pathname.endsWith("/review-smoke.html")'),'Use only the synthetic page.');
evaluate('(()=>{localStorage.removeItem(storageKey);state={decisions:{},notes:{},reviewed:{}};render();return true})()');
assert.equal(evaluate('document.querySelectorAll(".person-tab").length'),2);
assert.equal(evaluate('window.sourceExecuted||false'),false);
assert.equal(evaluate('[...document.querySelectorAll("a")].some(a=>a.protocol==="javascript:")'),false);
evaluate('(()=>{const s=document.querySelector(".google-grid select");s.value="same";s.dispatchEvent(new Event("change"));document.querySelectorAll(".person-tab")[1].click();return true})()');
assert.equal(evaluate('document.querySelector(".google-grid select").value'),'');
evaluate('(()=>{const s=document.querySelector(".google-grid select");s.value="different";s.dispatchEvent(new Event("change"));const n=document.querySelector("textarea");n.value="Keep both people and every circle";n.dispatchEvent(new Event("input"));document.getElementById("mark-reviewed").click();return true})()');
const before=evaluate('exportReview()');
assert.deepEqual(before.decisions.map(d=>[d.person_id,d.choice]),[['person-1','same'],['person-2','different']]);
assert.equal(before.groups[0].reviewed,true);
evaluate('(()=>{location.reload();return true})()');
await new Promise(r=>setTimeout(r,800));
const after=evaluate('exportReview()');
assert.deepEqual(after.decisions,before.decisions);
assert.deepEqual(after.groups,before.groups);
assert.equal(evaluate('document.querySelector(".google-grid select").value'),'same');
evaluate('(()=>{document.querySelector(".photo").click();return true})()');
assert.equal(evaluate('document.getElementById("lightbox").open'),true);
assert.equal(evaluate('document.getElementById("large-photo").naturalWidth>0'),true);
evaluate('(()=>{document.getElementById("close-photo").click();document.getElementById("platform").value="instagram";document.getElementById("platform").dispatchEvent(new Event("change"));return true})()');
assert.equal(evaluate('document.querySelectorAll("#profiles .card").length'),1);
evaluate('(()=>{document.getElementById("profile-search").value="no such profile";document.getElementById("profile-search").dispatchEvent(new Event("input"));return true})()');
assert.equal(evaluate('document.querySelectorAll("#profiles .card").length'),0);
assert.equal(evaluate('document.documentElement.scrollWidth>innerWidth'),false);
evaluate('(()=>{localStorage.removeItem(storageKey);return true})()');
console.log('PASS: isolated choices, reload persistence, export, notes, photo enlargement, filters, safe source rendering.');
