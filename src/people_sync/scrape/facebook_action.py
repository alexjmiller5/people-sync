"""Conservative English Facebook friendship adapter.

The engine owns exact-batch approval, intent journaling and postcondition calls.
Here ``following``/``absent`` mean friend/not friend, never subscription state.
Every click rechecks identity and expiry in the same JavaScript turn. A
name-only confirmation is refused: a visible canonical target link is required.
That confirmation layout is synthetic-tested, not live-validated.
"""

import json
import math
import re
from types import SimpleNamespace

from people_sync.scrape.instagram_action import Refused
from people_sync.scrape.pace import CHALLENGE_MARKERS, LOGIN_MARKERS

DOM_JS = r"""(() => {
  const {actor,handle,url,remote_id,expires_at,step,markers,previous_origin}=ARGS;
  const fail=error=>({error});
  const numeric=v=>typeof v==='string'&&/^[1-9][0-9]{0,29}$/.test(v);
  const reading=step==='inspect'||step==='observe';
  const expired=()=>!Number.isFinite(expires_at)||Date.now()/1000>=expires_at;
  if(!reading&&expired()) return fail('approval-expired');
  if(document.readyState!=='complete'||(previous_origin!==null&&
    !(performance.timeOrigin>previous_origin))) return fail('fresh-document-unverified');
  if(!numeric(actor)||(!numeric(remote_id)&&!(step==='observe'&&remote_id===null)))
    return fail('numeric-identity-required');
  const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&
    e.checkVisibility({opacityProperty:true,visibilityProperty:true,contentVisibilityAuto:true});};
  const all=(root,selector)=>[...root.querySelectorAll(selector)].filter(visible);
  const text=e=>(e.innerText||'').trim();
  const enabled=e=>!e.disabled&&e.getAttribute('aria-disabled')!=='true';
  const page=(document.title+'\n'+(document.body?.innerText||'')).toLowerCase();
  if(markers.some(m=>page.includes(m))) return fail('source-warning');
  if(typeof handle!=='string'||!(/^[A-Za-z0-9][A-Za-z0-9.]*$/.test(handle)||
    /^profile\.php\?id=[1-9][0-9]*$/.test(handle))||
    /^(me|friends|groups|settings|login|checkpoint|feed)$/i.test(handle)||
    url!=='https://www.facebook.com/'+handle||location.href!==url)
    return fail('wrong-profile-url');
  const canon=[...document.querySelectorAll('link[rel=canonical]')];
  if(canon.length>1||(canon.length===1&&canon[0].href!==url)) return fail('wrong-canonical-url');

  // Read only the named actor module and the profile-header user component.
  // Never return or retain bootstrap data, cookies, requests, names or tokens.
  const actors=new Set(),profiles=[];
  let malformed=false;
  const visit=(node,depth=0)=>{
    if(!node||typeof node!=='object'||depth>40) return;
    if(Array.isArray(node)&&node[0]==='CurrentUserInitialData') {
      if(!numeric(node[2]?.USER_ID)) malformed=true;
      else actors.add(node[2].USER_ID);
    }
    const user=node.data?.user?.profile_header_renderer?.user;
    if(user) profiles.push({id:user.id,url:user.url,name:user.name,friend:user.is_viewer_friend});
    for(const value of Object.values(node)) visit(value,depth+1);
  };
  for(const s of document.querySelectorAll('script[type="application/json"]')) {
    try {visit(JSON.parse(s.textContent));} catch { /* not a structured profile */ }
  }
  if(malformed||actors.size!==1||!actors.has(actor)) return fail('signed-in-account-unverified');
  const p=profiles[0];
  if(!p||!numeric(p.id)||p.id===actor||p.url!==url||typeof p.name!=='string'||!p.name.trim()||
    typeof p.friend!=='boolean'||(remote_id!==null&&p.id!==remote_id)||
    profiles.some(other=>other.id!==p.id||other.url!==url||other.friend!==p.friend||other.name!==p.name))
    return fail('profile-identity-unverified');
  if(handle.startsWith('profile.php?id=')&&handle!=='profile.php?id='+p.id)
    return fail('profile-identity-unverified');
  if(/^[0-9]+$/.test(handle)&&handle!==p.id) return fail('profile-identity-unverified');
  const mains=all(document,'[role=main]');
  if(mains.length!==1) return fail('ambiguous-main');
  const controls=all(mains[0],'button,[role=button]').filter(e=>
    ['Friends','Add friend'].includes(e.getAttribute('aria-label'))||
    ['Friends','Add friend'].includes(text(e)));
  if(controls.length!==1||!enabled(controls[0])) return fail('ambiguous-relationship-control');
  const control=controls[0],label=control.getAttribute('aria-label');
  if(text(control)!==label||label!==(p.friend?'Friends':'Add friend'))
    return fail('relationship-unverified');
  if(p.friend&&control.getAttribute('aria-haspopup')!=='dialog') return fail('unknown-friends-control');
  const dialogs=all(document,'[role=dialog]'),menus=all(document,'[role=menu]');
  const result=state=>({remote_id:p.id,state});
  // This check is deliberately adjacent to click, after potentially slow DOM work.
  const click=(element,state)=>{
    if(expired()) return fail('approval-expired');
    element.click();return result(state);
  };
  if(reading||step==='open') {
    if(dialogs.length||menus.length) return fail('unexpected-overlay');
    if(!p.friend) return result('absent');
    if(control.getAttribute('aria-expanded')!=='false') return fail('unexpected-friends-menu');
    return step==='open'?click(control,'following'):result('following');
  }
  if(!p.friend) return fail('relationship-changed');
  if(step==='preview') {
    if(dialogs.length||menus.length!==1||
      menus[0].getAttribute('aria-label')!=='Friend management options'||
      control.getAttribute('aria-expanded')!=='true') return fail('ambiguous-friends-menu');
    const items=all(menus[0],'[role=menuitem]').filter(e=>text(e)==='Unfriend');
    if(items.length!==1||!enabled(items[0])) return fail('ambiguous-unfriend-menuitem');
    return click(items[0],'following');
  }
  if(step!=='remove'||menus.length||dialogs.length!==1) return fail('ambiguous-removal-dialog');
  const d=dialogs[0],title='Unfriend '+p.name;
  const headings=all(d,'h2');
  if(d.getAttribute('aria-label')!==title||headings.length!==1||text(headings[0])!==title)
    return fail('dialog-title-unverified');
  // Display names, substrings and pictures alone cannot bind a destructive dialog.
  const links=all(d,'a[href]');
  if(links.length!==1||links[0].href!==url) return fail('dialog-identity-unverified');
  const buttons=all(d,'button,[role=button]');
  const confirms=buttons.filter(e=>e.getAttribute('aria-label')==='Confirm'||text(e)==='Confirm');
  const cancels=buttons.filter(e=>e.getAttribute('aria-label')==='Cancel'||text(e)==='Cancel');
  if(confirms.length!==1||cancels.length!==1||!enabled(confirms[0])||!enabled(cancels[0])||
    confirms[0].getAttribute('aria-label')!=='Confirm'||text(confirms[0])!=='Confirm'||
    cancels[0].getAttribute('aria-label')!=='Cancel'||text(cancels[0])!=='Cancel')
    return fail('ambiguous-confirmation-controls');
  return click(confirms[0],'submitted');
})()"""


def script(plan, target, step, *, previous_origin=None):
    if step not in {"observe", "inspect", "open", "preview", "remove"}:
        raise Refused("unsupported adapter step")
    return DOM_JS.replace(
        "ARGS",
        json.dumps(
            dict(
                actor=plan.actor,
                handle=target.handle,
                url=target.url,
                remote_id=target.remote_id,
                expires_at=plan.expires_at,
                step=step,
                markers=list(LOGIN_MARKERS + CHALLENGE_MARKERS),
                previous_origin=previous_origin,
            )
        ),
    )


def _evaluate(browser, plan, target, step, *, previous_origin=None):
    browser.check_stop()
    result = browser.eval(script(plan, target, step, previous_origin=previous_origin))
    browser.check_stop()
    states = {"submitted"} if step == "remove" else {"following", "absent"}
    if (
        not isinstance(result, dict)
        or result.get("error")
        or result.get("state") not in states
        or not isinstance(result.get("remote_id"), str)
        or not re.fullmatch(r"[1-9][0-9]{0,29}", result["remote_id"])
        or (step != "observe" and result["remote_id"] != target.remote_id)
    ):
        raise Refused("profile, account, warning or controls could not be verified")
    return {"remote_id": result["remote_id"], "state": result["state"]}


def _read(browser, plan, target, step):
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
        "!!document.querySelector('[role=main]')"
    )
    result = browser.navigate(target.url, wait_ms=10000, capture=[], ready_js=ready_js)
    if not isinstance(result, dict) or result.get("data_ready") is not True:
        raise Refused("fresh profile navigation did not complete")
    return _evaluate(browser, plan, target, step, previous_origin=previous_origin)


def observe(browser, actor, handle, url):
    """Discover a numeric identity and friendship state without any UI clicks."""
    plan = SimpleNamespace(actor=actor, expires_at=0)
    target = SimpleNamespace(handle=handle, url=url, remote_id=None)
    return _read(browser, plan, target, "observe")


def inspect(browser, plan, target):
    """Verify the bound friendship on a new, loaded document, including recovery."""
    return _read(browser, plan, target, "inspect")["state"]


def perform(browser, plan, target):
    """Submit an approved unfriend; the caller must inspect its postcondition."""
    if _evaluate(browser, plan, target, "open")["state"] != "following":
        raise Refused("friendship changed before action")
    if not browser.wait_for(
        "!!document.querySelector('[role=menu][aria-label=\"Friend management options\"]')",
        timeout_s=5,
    ):
        raise Refused("friend management menu unavailable; outcome unknown")
    if _evaluate(browser, plan, target, "preview")["state"] != "following":
        raise Refused("unfriend preview unavailable; outcome unknown")
    if not browser.wait_for(
        "!!document.querySelector('[role=dialog][aria-label^=\"Unfriend \"]')", timeout_s=5
    ):
        raise Refused("unfriend dialog unavailable; outcome unknown")
    if _evaluate(browser, plan, target, "remove")["state"] != "submitted":
        raise Refused("unfriend was not submitted")
