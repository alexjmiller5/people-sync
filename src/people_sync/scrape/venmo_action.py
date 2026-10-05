"""Strict English Venmo personal-profile UI adapter.

The public profile bundle binds Unfriend directly to RemoveFriend, with no
confirmation dialog. Never call its handler or API ourselves: the one guarded
menu-item click is the submission. Only synthetic tests exercise that click;
live validation is read-only. The caller owns human approval, attempt journaling
and a subsequent fresh ``inspect`` before recording an absent relationship.

Only selected identity/relationship fields leave __NEXT_DATA__; no response
capture, whole page state, credentials or payment data are retained.
"""

import json
import math
from types import SimpleNamespace

from people_sync.scrape.instagram_action import Refused
from people_sync.scrape.pace import CHALLENGE_MARKERS, LOGIN_MARKERS

DOM_JS = r"""(() => {
  const {actor,actor_id,handle,url,remote_id,expires_at,step,markers,
    previous_origin,document_origin}=ARGS;
  const fail=error=>({error});
  if(document.readyState!=='complete') return fail('document-not-ready');
  if(previous_origin!==null&&!(performance.timeOrigin>previous_origin))
    return fail('fresh-document-unverified');
  if(document_origin!==null&&performance.timeOrigin!==document_origin)
    return fail('document-changed');
  if(step!=='inspect'&&(!Number.isFinite(expires_at)||Date.now()/1000>=expires_at))
    return fail('approval-expired');
  const numeric=v=>typeof v==='string'&&/^[1-9][0-9]{0,29}$/.test(v)?v:
    Number.isSafeInteger(v)&&v>0?String(v):null;
  if(step!=='inspect'&&(typeof remote_id!=='string'||numeric(remote_id)===null))
    return fail('bound-identity-required');
  const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&
    e.checkVisibility({opacityProperty:true,visibilityProperty:true,contentVisibilityAuto:true});};
  const all=(root,selector)=>[...root.querySelectorAll(selector)].filter(visible);
  const text=e=>(e.innerText||'').trim();
  const disabled=e=>e.disabled||e.getAttribute('aria-disabled')==='true';
  const page=(document.title+'\n'+(document.body?.innerText||'')).toLowerCase();
  if(markers.some(m=>page.includes(m))) return fail('source-warning');
  if(typeof handle!=='string'||! /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}$/.test(handle)||
    handle.includes('..')||url!=='https://account.venmo.com/u/'+handle||location.href!==url)
    return fail('wrong-profile-url');
  const scripts=[...document.querySelectorAll('script[id="__NEXT_DATA__"]')];
  if(scripts.length!==1) return fail('profile-model-unavailable');
  let p;
  try {p=JSON.parse(scripts[0].textContent)?.props?.pageProps;}
  catch {return fail('profile-model-unavailable');}
  const current=p?.currentUser, other=p?.otherUser;
  const currentID=numeric(current?.id), targetID=numeric(other?.id);
  if(p?.authenticated!==true||typeof actor!=='string'||typeof current?.username!=='string'||
    current.username.toLowerCase()!==actor.toLowerCase()||currentID===null||
    (actor_id!==null&&currentID!==actor_id)) return fail('signed-in-account-unverified');
  if(typeof other?.username!=='string'||other.username.toLowerCase()!==handle.toLowerCase()||
    targetID===null||(remote_id!==null&&targetID!==remote_id)||
    currentID===targetID||actor.toLowerCase()===handle.toLowerCase())
    return fail('remote-identity-unverified');
  if(other.isBlocked===true||other.isActive===false||
    !['friend','not_friend'].includes(other.friendStatus)) return fail('unsupported-relationship-state');
  const profiles=all(document,'[class^="profile_profileWrapper__"]');
  if(profiles.length!==1) return fail('ambiguous-profile');
  const profile=profiles[0];
  const handles=all(profile,'[class^="profileInfo_handle__"]');
  if(handles.length!==1||text(handles[0]).toLowerCase()!=='@'+handle.toLowerCase())
    return fail('profile-control-identity-unverified');
  if(all(document,'[role=dialog]').length) return fail('unexpected-dialog');
  const more=all(profile,'button[aria-label="More options"]');
  const containers=all(profile,'[class^="profile_friendsContainer__"]');
  if(more.length!==1||disabled(more[0])||more[0].getAttribute('aria-haspopup')!=='true'||
    containers.length!==1) return fail('ambiguous-relationship-control');
  const controls=all(containers[0],'h6,button,[role=button]');
  if(controls.length!==1||disabled(controls[0])) return fail('ambiguous-relationship-control');
  const friend=other.friendStatus==='friend';
  if(friend?(controls[0].tagName!=='H6'||text(controls[0])!=='Friends'):
    (controls[0].tagName!=='BUTTON'||text(controls[0])!=='Add friend'))
    return fail('relationship-model-control-mismatch');
  const menus=all(document,'[role=menu]');
  let remove;
  if(menus.length) {
    const owners=all(document,'[id="option-menu"]');
    if(!friend||menus.length!==1||owners.length!==1||!owners[0].contains(menus[0])||
      more[0].getAttribute('aria-controls')!=='option-menu'||
      more[0].getAttribute('aria-expanded')!=='true') return fail('ambiguous-removal-menu');
    const items=all(menus[0],'[role=menuitem]');
    const removals=items.filter(e=>e.tagName==='LI'&&text(e)==='Unfriend');
    if(items.length!==2||removals.length!==1||
      items.filter(e=>e.tagName==='LI'&&text(e)==='Block').length!==1||
      items.some(disabled)) return fail('ambiguous-unfriend-control');
    remove=removals[0];
  } else if(more[0].getAttribute('aria-expanded')==='true') return fail('missing-removal-menu');
  const result=state=>({state,remote_id:targetID,actor_id:currentID,
    document_origin:performance.timeOrigin});
  if(step==='inspect') return result(friend?'following':'absent');
  if(!friend) return fail('relationship-changed');
  if(step==='open') {
    if(!menus.length) more[0].click();
    return result('following');
  }
  if(step!=='remove'||!remove) return fail('removal-menu-unverified');
  remove.click();
  return result('submitted');
})()"""


def script(plan, target, step, *, previous_origin=None, actor_id=None, document_origin=None):
    if step not in {"inspect", "open", "remove"}:
        raise Refused("unsupported adapter step")
    args = dict(
        actor=plan.actor,
        actor_id=actor_id if actor_id is not None else getattr(plan, "actor_id", None),
        handle=target.handle,
        url=target.url,
        remote_id=target.remote_id,
        expires_at=plan.expires_at,
        step=step,
        markers=list(LOGIN_MARKERS + CHALLENGE_MARKERS),
        previous_origin=previous_origin,
        document_origin=document_origin,
    )
    return DOM_JS.replace("ARGS", json.dumps(args))


def _evaluate(browser, plan, target, step, **kwargs):
    browser.check_stop()
    result = browser.eval(script(plan, target, step, **kwargs))
    browser.check_stop()
    if (
        not isinstance(result, dict)
        or result.get("error")
        or result.get("state") not in {"following", "absent", "submitted"}
        or not isinstance(result.get("remote_id"), str)
        or not isinstance(result.get("actor_id"), str)
        or type(result.get("document_origin")) not in {int, float}
        or not math.isfinite(result["document_origin"])
        or result["document_origin"] <= 0
    ):
        raise Refused("Venmo profile, account, warning or controls could not be verified")
    return result


def _inspect(browser, plan, target):
    browser.check_stop()
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
        "!!document.querySelector('[class^=\"profileInfo_handle__\"]')"
    )
    result = browser.navigate(target.url, wait_ms=10000, capture=[], ready_js=ready_js)
    if not isinstance(result, dict) or result.get("data_ready") is not True:
        raise Refused("fresh Venmo profile navigation did not complete")
    return _evaluate(browser, plan, target, "inspect", previous_origin=previous_origin)


def observe(browser, actor, handle, url):
    """Read-only preparation: resolve the target ID before freezing an approval plan."""
    plan = SimpleNamespace(actor=actor, expires_at=None)
    target = SimpleNamespace(handle=handle, url=url, remote_id=None)
    result = _inspect(browser, plan, target)
    return {"remote_id": result["remote_id"], "state": result["state"]}


def inspect(browser, plan, target):
    """Reload first, including on resume: optimistic old JSON is never evidence."""
    return _inspect(browser, plan, target)["state"]


def perform(browser, plan, target):
    """Submit once; caller must have approved and journaled this exact attempt."""
    opened = _evaluate(browser, plan, target, "open")
    if opened["state"] != "following":
        raise Refused("relationship changed before action")
    if not browser.wait_for("!!document.querySelector('[role=menu]')", timeout_s=5):
        raise Refused("Venmo removal menu unavailable; no removal submitted")
    result = _evaluate(
        browser,
        plan,
        target,
        "remove",
        actor_id=opened["actor_id"],
        document_origin=opened["document_origin"],
    )
    if result["state"] != "submitted":
        raise Refused("Venmo removal was not submitted")
