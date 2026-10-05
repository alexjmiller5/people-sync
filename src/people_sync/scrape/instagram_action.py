"""Conservative English Instagram UI adapter.

No API mutations or guessed identities. Every UI click rechecks the actor,
profile, warning markers, visibility and uniqueness in the same JS turn.
Unknown layouts fail closed. Success requires a separately reloaded profile.
"""

import json
import math
import re
from types import SimpleNamespace

from people_sync.scrape.pace import CHALLENGE_MARKERS, LOGIN_MARKERS


class Refused(RuntimeError):
    """Fixed, non-sensitive explanation safe for the CLI to print."""


# Kept as one predicate so checks cannot drift between inspection and clicking.
DOM_JS = r"""(() => {
  const {actor,handle,url,remote_id,expires_at,step,markers,previous_origin}=ARGS;
  const fail=error=>({error});
  if(previous_origin!==null&&
    (!(performance.timeOrigin>previous_origin)||document.readyState!=='complete'))
    return fail('fresh-document-unverified');
  if((step==='open'||step==='remove')&&Date.now()/1000>=expires_at) return fail('approval-expired');
  const numericID=value=>typeof value==='string'&&/^[1-9][0-9]{0,29}$/.test(value);
  if(step!=='observe'&&!numericID(remote_id)) return fail('bound-identity-required');
  const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&
    e.checkVisibility({opacityProperty:true,visibilityProperty:true,contentVisibilityAuto:true});};
  const all=(root,selector)=>[...root.querySelectorAll(selector)].filter(visible);
  const text=e=>(e.innerText||'').trim();
  const page=(document.title+'\n'+(document.body?.innerText||'')).toLowerCase();
  if(markers.some(m=>page.includes(m))) return fail('source-warning');
  if(location.href!==url) return fail('wrong-profile-url');
  const headers=all(document,'header');
  if(headers.length!==1) return fail('ambiguous-profile-header');
  const header=headers[0];
  const headings=all(header,'h1,h2').filter(e=>text(e).toLowerCase()===handle.toLowerCase());
  if(headings.length!==1) return fail('wrong-profile-identity');
  const canon=[...document.querySelectorAll('link[rel=canonical]')];
  if(canon.length>1 || (canon.length===1&&canon[0].href!==url)) return fail('wrong-canonical-url');
  // PolarisViewer is the authenticated bootstrap module, not an arbitrary user
  // object or profile link. Read only its selected identity fields.
  const viewers=[];
  const visit=(node,depth=0)=>{
    if(!node||typeof node!=='object'||depth>40) return;
    if(Array.isArray(node.__bbox?.define)) {
      for(const tuple of node.__bbox.define) {
        if(Array.isArray(tuple)&&tuple[0]==='PolarisViewer') viewers.push(tuple[2]);
      }
    }
    for(const value of Object.values(node)) visit(value,depth+1);
  };
  for(const s of document.querySelectorAll('script[type="application/json"]')) {
    try {visit(JSON.parse(s.textContent));} catch { /* unavailable bootstrap */ }
  }
  if(viewers.length!==1) return fail('authenticated-viewer-unverified');
  const viewer=viewers[0]?.data;
  if(!viewer||!numericID(viewer.id)||viewers[0].id!==viewer.id||
    typeof viewer.username!=='string'||viewer.username.toLowerCase()!==actor.toLowerCase())
    return fail('authenticated-viewer-unverified');
  // The rendered header's profile component supplies userID/username and its
  // viewer. Stop at main; unrelated React trees never provide target identity.
  const fiberKeys=Object.keys(header).filter(k=>k.startsWith('__reactFiber$'));
  if(fiberKeys.length!==1) return fail('profile-identity-unverified');
  const ids=new Set();
  let profileViewer=false;
  let fiber=header[fiberKeys[0]];
  const seen=new Set();
  for(let depth=0;fiber&&fiber.type!=='main'&&depth<40;depth++,fiber=fiber.return) {
    if(seen.has(fiber)) return fail('profile-identity-unverified');
    seen.add(fiber);
    for(const props of [fiber.memoizedProps,fiber.alternate?.memoizedProps]) {
      if(!props||typeof props!=='object'||!('userID' in props)) continue;
      if(!numericID(props.userID)||typeof props.username!=='string'||
        props.username.toLowerCase()!==handle.toLowerCase()) return fail('profile-identity-unverified');
      ids.add(props.userID);
      if(props.viewer&&('id' in props.viewer||'username' in props.viewer)) {
        if(props.viewer.id!==viewer.id||props.viewer.username!==viewer.username)
          return fail('profile-viewer-unverified');
        profileViewer=true;
      }
    }
  }
  if(!fiber||fiber.type!=='main'||ids.size!==1||!profileViewer)
    return fail('profile-identity-unverified');
  const observedID=[...ids][0];
  if(observedID===viewer.id||(step!=='observe'&&observedID!==remote_id))
    return fail('remote-identity-unverified');
  const actorURL='https://www.instagram.com/'+actor+'/';
  const actorLinks=all(document,'a[href]').filter(a=>a.href===actorURL&&
    !a.closest('header,main,[role=main],[role=dialog]')&&
    a.querySelector('img')?.getAttribute('alt')===actor+"'s profile picture");
  if(actorLinks.length!==1) return fail('signed-in-account-unverified');
  const dialogs=all(document,'[role=dialog]');
  const controls=all(header,'button,[role=button]').filter(e=>
    ['Following','Follow','Follow Back','Requested'].includes(text(e)));
  if(controls.length!==1||controls[0].disabled||controls[0].getAttribute('aria-disabled')==='true')
    return fail('ambiguous-relationship-control');
  const state=text(controls[0]);
  const observed=state=>step==='observe'?{remote_id:observedID,state}:{state};
  if(step==='inspect'||step==='open'||step==='observe') {
    if(dialogs.length) return fail('unexpected-dialog');
    if(state==='Follow'||state==='Follow Back') return observed('absent');
    if(state!=='Following') return fail('unsupported-relationship-state');
    if(step==='open') controls[0].click();
    return observed('following');
  }
  if(step!=='remove'||state!=='Following'||dialogs.length!==1) return fail('ambiguous-removal-dialog');
  const d=dialogs[0];
  const identity=all(d,'img').some(e=>e.getAttribute('alt')===handle+"'s profile picture")||
    all(d,'h1,h2,h3').some(e=>text(e)===handle)||all(d,'a[href]').some(e=>e.href===url);
  if(!identity) return fail('dialog-identity-unverified');
  const buttons=all(d,'button,[role=button]').filter(e=>text(e)==='Unfollow');
  if(buttons.length!==1||buttons[0].disabled||buttons[0].getAttribute('aria-disabled')==='true')
    return fail('ambiguous-unfollow-control');
  buttons[0].click();
  return {state:'submitted'};
})()"""


def script(plan, target, step, *, previous_origin=None):
    if step not in {"inspect", "open", "remove", "observe"}:
        raise Refused("unsupported adapter step")
    args = dict(
        actor=plan.actor,
        handle=target.handle,
        url=target.url,
        remote_id=target.remote_id,
        step=step,
        markers=list(LOGIN_MARKERS + CHALLENGE_MARKERS),
        expires_at=plan.expires_at,
        previous_origin=previous_origin,
    )
    return DOM_JS.replace("ARGS", json.dumps(args))


def _evaluate(browser, plan, target, step, *, previous_origin=None):
    browser.check_stop()
    result = browser.eval(script(plan, target, step, previous_origin=previous_origin))
    browser.check_stop()
    if (
        not isinstance(result, dict)
        or result.get("error")
        or result.get("state")
        not in {
            "following",
            "absent",
            "submitted",
        }
    ):
        # Never echo page-controlled values or raw browser exceptions here.
        raise Refused("profile, account, warning or controls could not be verified")
    if step == "observe":
        remote_id = result.get("remote_id")
        if (
            not isinstance(remote_id, str)
            or re.fullmatch(r"[1-9][0-9]{0,29}", remote_id) is None
            or result["state"] not in {"following", "absent"}
        ):
            raise Refused("observed profile identity unavailable")
        return {"remote_id": remote_id, "state": result["state"]}
    return result["state"]


def _inspect(browser, plan, target, step):
    browser.check_stop()
    if step != "observe" and (
        not isinstance(target.remote_id, str)
        or re.fullmatch(r"[1-9][0-9]{0,29}", target.remote_id) is None
    ):
        raise Refused("approved numeric profile identity required")
    previous_origin = browser.eval("performance.timeOrigin")
    if (
        type(previous_origin) not in {int, float}
        or not math.isfinite(previous_origin)
        or previous_origin <= 0
    ):
        raise Refused("document identity unavailable")
    ready_js = (
        f"performance.timeOrigin>{json.dumps(previous_origin)}&&"
        "document.readyState==='complete'&&"
        f"location.href==={json.dumps(target.url)}&&"
        "!!document.querySelector('header h1,header h2')"
    )
    result = browser.navigate(target.url, wait_ms=10000, capture=[], ready_js=ready_js)
    if not isinstance(result, dict) or result.get("data_ready") is not True:
        raise Refused("fresh profile navigation did not complete")
    return _evaluate(browser, plan, target, step, previous_origin=previous_origin)


def inspect(browser, plan, target):
    return _inspect(browser, plan, target, "inspect")


def observe(browser, actor, handle, url):
    """Read a fresh profile for a future plan; never authorize a click."""
    if (
        any(
            not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.]{0,29}", value) is None
            or ".." in value
            for value in (actor, handle)
        )
        or actor.casefold() == handle.casefold()
        or url != f"https://www.instagram.com/{handle}/"
    ):
        raise Refused("invalid observation identity")
    plan = SimpleNamespace(actor=actor, expires_at=0)
    target = SimpleNamespace(handle=handle, url=url, remote_id=None)
    return _inspect(browser, plan, target, "observe")


def perform(browser, plan, target):
    if _evaluate(browser, plan, target, "open") != "following":
        raise Refused("relationship changed before action")
    if not browser.wait_for("!!document.querySelector('[role=dialog]')", timeout_s=5):
        raise Refused("removal dialog unavailable; outcome unknown")
    if _evaluate(browser, plan, target, "remove") != "submitted":
        raise Refused("removal was not submitted")
