"""Venmo actions against synthetic documents; no live relationship mutations."""

import asyncio
import importlib
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from people_sync.scrape import cdp

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node required for DOM predicates")

DOM = r"""
let clicks=[];
function el(tag, text='', attrs={}, children=[]) {
  const e={tagName:tag.toUpperCase(),innerText:text,attrs,children,disabled:false,
    getAttribute(k){return this.attrs[k]??null;},
    get id(){return this.attrs.id||'';},
    getBoundingClientRect(){return {width:this.zero?0:100,height:30};},
    checkVisibility(o){return o.opacityProperty&&o.visibilityProperty&&!this.hidden;},
    contains(other){return this===other||this.children.some(c=>c.contains(other));},
    querySelectorAll(selector){
      const matches=(n,s)=>{
        if(s==='[role=dialog]')return n.attrs.role==='dialog';
        if(s==='[role=menu]')return n.attrs.role==='menu';
        if(s==='[role=menuitem]')return n.attrs.role==='menuitem';
        if(s==='[aria-label="More options"]')return n.attrs['aria-label']==='More options';
        if(s==='button[aria-label="More options"]')return n.tagName==='BUTTON'&&n.attrs['aria-label']==='More options';
        if(s==='[role=button]')return n.attrs.role==='button';
        if(s==='[id="option-menu"]')return n.id==='option-menu';
        if(s==='script[id="__NEXT_DATA__"]')return n.tagName==='SCRIPT'&&n.id==='__NEXT_DATA__';
        const prefix=s.match(/^\[class\^="([^"]+)"\]$/);
        if(prefix)return (n.attrs.class||'').startsWith(prefix[1]);
        return n.tagName===s.toUpperCase();
      };
      const found=[];
      const walk=n=>{for(const c of n.children){
        if(selector.split(',').some(s=>matches(c,s)))found.push(c);walk(c);
      }};
      walk(this);return found;
    },
    querySelector(s){return this.querySelectorAll(s)[0]||null;},
    click(){clicks.push(this.innerText||this.attrs['aria-label']);}
  };
  return e;
}
let props={authenticated:true,
  currentUser:{id:'800001',username:'example_operator'},
  otherUser:{id:'900002',username:'example_target',friendStatus:'friend',isBlocked:false,isActive:true},
  csrfToken:'synthetic-secret',paymentHistory:[{note:'synthetic-private-payment'}]};
let next=el('script','',{id:'__NEXT_DATA__'});
Object.defineProperty(next,'textContent',{get(){return JSON.stringify({props:{pageProps:props}});},configurable:true});
let handle=el('span','@example_target',{class:'profileInfo_handle__synthetic'});
let friends=el('h6','Friends'), add=el('button','Add friend');
let controls=el('div','',{class:'profile_friendsContainer__synthetic'},[friends]);
let more=el('button','',{'aria-label':'More options','aria-haspopup':'true','aria-expanded':'false'});
let profile=el('div','',{class:'profile_profileWrapper__synthetic'},[more,handle,controls]);
let unfriend=el('li','Unfriend',{role:'menuitem'}), block=el('li','Block',{role:'menuitem'});
let menu=el('ul','',{role:'menu'},[unfriend,block]);
let popup=el('div','',{id:'option-menu',role:'presentation'},[menu]);
globalThis.document=el('document','',{},[next,profile]);
document.title='Venmo';document.body={innerText:''};document.readyState='complete';
document.getElementById=id=>document.querySelectorAll('[id="option-menu"]').find(e=>e.id===id)||null;
globalThis.location={href:'https://account.venmo.com/u/example_target'};
Object.defineProperty(globalThis,'performance',{value:{timeOrigin:2000},configurable:true});
function openMenu(){
  if(!document.children.includes(popup))document.children.push(popup);
  more.attrs['aria-controls']='option-menu';more.attrs['aria-expanded']='true';
}
more.click=()=>{clicks.push('More options');openMenu();};
function absent(){props.otherUser.friendStatus='not_friend';controls.children=[add];}
"""


@pytest.fixture
def adapter():
    return importlib.import_module("people_sync.scrape.venmo_action")


def plan(**kwargs):
    return SimpleNamespace(actor="example_operator", expires_at=4102444800, **kwargs)


def target(remote_id="900002"):
    return SimpleNamespace(
        handle="example_target",
        url="https://account.venmo.com/u/example_target",
        remote_id=remote_id,
    )


def evaluate(js, setup=""):
    output = subprocess.run(
        [NODE, "-"],
        input=DOM
        + setup
        + f"\nconst result={js};\nconsole.log(JSON.stringify({{result,clicks}}));",
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(output.stdout)


def run(adapter, setup="", step="inspect", remote_id="900002", **kwargs):
    return evaluate(adapter.script(plan(), target(remote_id), step, **kwargs), setup)


def test_friend_observation_and_only_exact_unfriend_is_clicked():
    assert importlib.util.find_spec("people_sync.scrape.venmo_action"), "Venmo adapter missing"
    adapter = importlib.import_module("people_sync.scrape.venmo_action")
    result = run(adapter)
    assert result["result"]["state"] == "following"
    assert result["result"]["remote_id"] == "900002"
    assert result["clicks"] == []
    assert run(adapter, step="open")["clicks"] == ["More options"]
    assert run(adapter, "openMenu();", "open")["clicks"] == []
    assert run(adapter, "openMenu();", "remove")["clicks"] == ["Unfriend"]


def test_absence_requires_both_server_model_and_positive_add_control(adapter):
    assert run(adapter, "absent();")["result"]["state"] == "absent"
    for setup in [
        "props.otherUser.friendStatus='not_friend';",
        "controls.children=[add];",
        "controls.children=[];",
        "absent();controls.children=[];",
        "absent();add.hidden=true;",
        "absent();controls.children.push(friends);",
        "absent();openMenu();",
    ]:
        result = run(adapter, setup)
        assert result["result"].get("error"), setup
        assert result["clicks"] == []


@pytest.mark.parametrize("step", ["inspect", "open", "remove"])
@pytest.mark.parametrize(
    "setup",
    [
        "location.href+='?redirect=1';",
        "location.href='https://account.venmo.com/u/other_target';",
        "props.authenticated=false;",
        "props.authenticated='true';",
        "props.currentUser.username='other_operator';",
        "props.currentUser.id='900002';",
        "delete props.currentUser;",
        "props.currentUser.id='invalid';",
        "props.otherUser.username='other_target';",
        "props.otherUser.id='900003';",
        "delete props.otherUser.id;",
        "props.otherUser.id=9007199254740993;",
        "props.otherUser.friendStatus='request_sent_by_you';",
        "props.otherUser.friendStatus='request_received_by_you';",
        "props.otherUser.isBlocked=true;",
        "props.otherUser.isActive=false;",
        "Object.defineProperty(next,'textContent',{value:'not JSON'});",
        "document.children.push(next);",
        "document.children=document.children.filter(e=>e!==next);",
        "document.children.push(profile);",
        "handle.innerText='@other_target';",
        "handle.hidden=true;",
        "profile.children.push(handle);",
        "controls.children.push(el('h6','Friends'));",
        "controls.children.push(add);",
        "friends.hidden=true;",
        "friends.zero=true;",
        "more.disabled=true;",
        "more.attrs['aria-disabled']='true';",
        "more.hidden=true;",
        "profile.children.push(el('button','',{'aria-label':'More options'}));",
        "document.children.push(el('div','',{role:'dialog'}));",
        "document.readyState='loading';",
        "document.body.innerText='Try again later';",
        "document.body.innerText='Sign in to continue';",
    ],
)
def test_identity_warning_and_control_guards_fail_closed(adapter, setup, step):
    result = run(adapter, ("openMenu();" if step == "remove" else "") + setup, step)
    assert result["result"].get("error")
    assert result["clicks"] == []


@pytest.mark.parametrize(
    "setup",
    [
        "document.children=document.children.filter(e=>e!==popup);",
        "document.children.push(el('ul','',{role:'menu'},[unfriend]));",
        "popup.attrs.id='other-menu';",
        "more.attrs['aria-controls']='other-menu';",
        "more.attrs['aria-expanded']='false';",
        "menu.children=[block];",
        "menu.children.push(el('li','Unfriend',{role:'menuitem'}));",
        "unfriend.innerText='Remove friend';",
        "unfriend.hidden=true;",
        "unfriend.zero=true;",
        "unfriend.disabled=true;",
        "unfriend.attrs['aria-disabled']='true';",
        "menu.children.push(el('li','Unexpected',{role:'menuitem'}));",
        "block.innerText='Unblock';",
        "Date.now=()=>4102444800000;",
    ],
)
def test_final_click_rechecks_exact_menu_owner_and_controls(adapter, setup):
    result = run(adapter, "openMenu();" + setup, "remove")
    assert result["result"].get("error")
    assert result["clicks"] == []


@pytest.mark.parametrize("step", ["open", "remove"])
def test_no_click_without_stable_id_or_valid_expiry(adapter, step):
    setup = "openMenu();" if step == "remove" else ""
    assert run(adapter, setup, step, remote_id=None)["result"].get("error")
    assert run(adapter, setup, step, remote_id=None)["clicks"] == []
    for expiry in [None, "4102444800", 1]:
        p = plan()
        p.expires_at = expiry
        result = evaluate(adapter.script(p, target(), step), setup)
        assert result["result"].get("error")
        assert result["clicks"] == []


def test_bound_actor_id_and_same_document_are_required_for_final_click(adapter):
    js = adapter.script(plan(actor_id="800009"), target(), "remove")
    assert evaluate(js, "openMenu();")["clicks"] == []
    result = run(adapter, "openMenu();", "remove", actor_id="800009", document_origin=2000)
    assert result["result"].get("error")
    assert result["clicks"] == []
    result = run(adapter, "openMenu();", "remove", actor_id="800001", document_origin=1000)
    assert result["result"].get("error")
    assert result["clicks"] == []


def test_observation_output_never_contains_next_data_secrets_or_payments(adapter):
    result = run(adapter, remote_id=None)
    assert result["result"]["remote_id"] == "900002"
    assert "synthetic-secret" not in json.dumps(result)
    assert "synthetic-private-payment" not in json.dumps(result)


class DocumentBrowser(cdp.Browser):
    """Exercise the real readiness loop, replacing only CDP transport and DOM."""

    def __init__(self, *, next_origin=2000, setup="", after_open="", revert=False):
        self.origin = 1000
        self.next_origin = next_origin
        self.setup = setup
        self.after_open = after_open
        self.revert = revert
        self.menu_open = False
        self.clicks = []
        self._session_id = "synthetic"
        self._block_callback = None
        self._stop_event = None
        self._capture_tasks = []

    def eval(self, js):
        setup = self.setup + f"performance.timeOrigin={self.origin};"
        if self.menu_open:
            setup += "openMenu();" + self.after_open
        result = evaluate(js, setup)
        self.clicks.extend(result["clicks"])
        if "More options" in result["clicks"]:
            self.menu_open = True
        return result["result"]

    async def _eval_async(self, js):
        return self.eval(js)

    async def _send(self, method, params, *, session_id):
        assert method == "Page.navigate"
        assert params == {"url": "https://account.venmo.com/u/example_target"}
        self.origin = self.next_origin
        self.menu_open = False
        return {"frameId": "synthetic", "loaderId": "synthetic-loader"}

    def navigate(self, url, wait_ms, capture, ready_js):
        assert capture == []
        result = asyncio.run(self._navigate_async(url, 0, capture, ready_js))
        if self.revert:
            self.origin = 1000
        return result


@pytest.mark.parametrize("setup,expected", [("", "following"), ("absent();", "absent")])
def test_observe_and_inspect_are_fresh_and_read_only(adapter, setup, expected):
    browser = DocumentBrowser(setup=setup)
    assert adapter.inspect(browser, plan(), target()) == expected
    assert browser.clicks == []
    browser = DocumentBrowser(setup=setup)
    assert adapter.observe(browser, "example_operator", "example_target", target().url) == {
        "remote_id": "900002",
        "state": expected,
    }
    assert browser.clicks == []


@pytest.mark.parametrize(
    "options",
    [
        {"next_origin": 1000},
        {"next_origin": 900},
        {"setup": "document.readyState='loading';"},
        {"revert": True},
    ],
)
def test_old_or_incomplete_document_cannot_prove_absence(adapter, options):
    options["setup"] = "absent();" + options.get("setup", "")
    browser = DocumentBrowser(**options)
    with pytest.raises(adapter.Refused):
        adapter.inspect(browser, plan(), target())
    assert browser.clicks == []


def test_perform_opens_menu_then_submits_once_but_does_not_claim_absence(adapter):
    browser = DocumentBrowser()
    assert adapter.perform(browser, plan(), target()) is None
    assert browser.clicks == ["More options", "Unfriend"]
    assert adapter.inspect(browser, plan(), target()) == "following"


@pytest.mark.parametrize(
    "after_open",
    [
        "props.currentUser.id='800009';",
        "props.currentUser.username='other_operator';",
        "props.otherUser.id='900003';",
        "performance.timeOrigin=3000;",
        "document.body.innerText='Your account has been restricted';",
        "Date.now=()=>4102444800000;",
        "absent();",
    ],
)
def test_perform_revalidates_between_menu_open_and_submission(adapter, after_open):
    browser = DocumentBrowser(after_open=after_open)
    with pytest.raises(adapter.Refused):
        adapter.perform(browser, plan(), target())
    assert browser.clicks == ["More options"]


def test_perform_never_opens_menu_for_unbound_or_absent_target(adapter):
    for browser, selected in [
        (DocumentBrowser(), target(None)),
        (DocumentBrowser(setup="absent();"), target()),
    ]:
        with pytest.raises(adapter.Refused):
            adapter.perform(browser, plan(), selected)
        assert browser.clicks == []
