"""Synthetic Facebook UI only; execute the actual predicates in Node."""

import asyncio
import importlib
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from people_sync.scrape import cdp, instagram_action

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node required for DOM predicates")


def adapter():
    assert importlib.util.find_spec("people_sync.scrape.facebook_action"), "adapter missing"
    return importlib.import_module("people_sync.scrape.facebook_action")


def plan_target(remote_id="900002", actor="900001", expires_at=4102444800):
    return (
        SimpleNamespace(actor=actor, expires_at=expires_at),
        SimpleNamespace(
            handle="example.target",
            url="https://www.facebook.com/example.target",
            remote_id=remote_id,
        ),
    )


DOM = r"""
let clicks=[];
function el(tag, attrs={}, content='', children=[]) {
  const e={tagName:tag.toUpperCase(),attrs,innerText:content,children,disabled:false,
    getAttribute(k){return this.attrs[k]??null;},
    get href(){return this.attrs.href;},
    get textContent(){return this.innerText;},
    getBoundingClientRect(){return {width:this.zeroWidth?0:100,height:30};},
    checkVisibility(o){return o.opacityProperty && o.visibilityProperty && !this.hidden;},
    matches(s){
      const tag=s.match(/^[a-z0-9]+/i)?.[0];
      if(tag && tag.toUpperCase()!==this.tagName)return false;
      return [...s.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)].every(
        ([,k,v])=>k in this.attrs && (v===undefined || this.attrs[k]===v));
    },
    querySelectorAll(s){const out=[];const walk=n=>{for(const c of n.children){
      if(s.split(',').some(selector=>c.matches(selector.trim())))out.push(c);walk(c);
    }};walk(this);return out;},
    querySelector(s){return this.querySelectorAll(s)[0]??null;},
    click(){clicks.push(this.getAttribute('aria-label')||this.innerText);}
  };return e;
}
let profile={id:'900002',url:'https://www.facebook.com/example.target',
  name:'Example Person',is_viewer_friend:true};
let current={USER_ID:'900001',NAME:'Example Operator'};
let moduleName='CurrentUserInitialData';
let extraData=[];
let friends=el('div',{'role':'button','aria-label':'Friends','aria-haspopup':'dialog',
  'aria-expanded':'false'},'Friends');
let add=el('div',{'role':'button','aria-label':'Add friend'},'Add friend');
let main=el('div',{role:'main'},'', [friends]);
let menuItem=el('div',{role:'menuitem'},'Unfriend');
let menu=el('div',{role:'menu','aria-label':'Friend management options'},'',[
  el('div',{role:'menuitem'},'Unfollow'), menuItem]);
let link=el('a',{href:profile.url},'Example Person');
let confirm=el('div',{role:'button','aria-label':'Confirm'},'Confirm');
let cancel=el('div',{role:'button','aria-label':'Cancel'},'Cancel');
let title=el('h2',{},'Unfriend Example Person');
let dialog=el('div',{role:'dialog','aria-label':'Unfriend Example Person'},'',[
  title,link,confirm,cancel]);
let dialogs=[],menus=[],mains=[main],canonical=[];
friends.click=()=>{clicks.push('Friends');menus=[menu];friends.attrs['aria-expanded']='true';};
menuItem.click=()=>{clicks.push('Unfriend');menus=[];dialogs=[dialog];
  friends.attrs['aria-expanded']='false';};
confirm.click=()=>{clicks.push('Confirm');dialogs=[];profile.is_viewer_friend=false;
  main.children=[add];};
globalThis.location={href:profile.url};
Object.defineProperty(globalThis,'performance',{value:{timeOrigin:2000}});
globalThis.document={title:'Facebook',body:{innerText:''},readyState:'complete',
  querySelectorAll(s){
    if(s==='script[type="application/json"]')return [
      {textContent:JSON.stringify({require:[[moduleName,[],current,1]],
        data:{user:{profile_header_renderer:{user:profile}}},extraData})}];
    return el('root',{},'', [...mains,...menus,...dialogs,...canonical]).querySelectorAll(s);
  },querySelector(s){return this.querySelectorAll(s)[0]??null;}
};
"""


def execute_js(js, setup=""):
    output = subprocess.run(
        [NODE, "-"],
        input=DOM + setup + f"\nlet result={js};console.log(JSON.stringify({{result,clicks}}));",
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(output.stdout)


def run(setup="", step="inspect", remote_id="900002", actor="900001", **kwargs):
    plan, target = plan_target(remote_id, actor)
    return execute_js(adapter().script(plan, target, step, **kwargs), setup)


STAGES = {
    "inspect": "",
    "open": "",
    "preview": "menus=[menu];friends.attrs['aria-expanded']='true';",
    "remove": "dialogs=[dialog];",
}


def test_inspection_returns_only_selected_identity_and_state():
    assert run() == {"result": {"remote_id": "900002", "state": "following"}, "clicks": []}
    assert run("profile.is_viewer_friend=false;main.children=[add];")["result"] == {
        "remote_id": "900002",
        "state": "absent",
    }


@pytest.mark.parametrize(
    "step,label", [("open", "Friends"), ("preview", "Unfriend"), ("remove", "Confirm")]
)
def test_only_expected_scoped_action_clicks(step, label):
    assert run(STAGES[step], step)["clicks"] == [label]


@pytest.mark.parametrize("step", STAGES)
@pytest.mark.parametrize(
    "setup",
    [
        "current.USER_ID='900003';",
        "delete current.USER_ID;",
        "current.USER_ID=900001;",
        "moduleName='UnrelatedModule';",
        "extraData.push(['CurrentUserInitialData',[],{USER_ID:'900003'},1]);",
        "profile.id='900004';",
        "delete profile.id;",
        "profile.id=900002;",
        "profile.url='https://www.facebook.com/other.target';",
        "location.href+='?tracking=1';",
        "profile.url+='?tracking=1';",
        "extraData.push({data:{user:{profile_header_renderer:{user:{...profile,id:'900004'}}}}});",
        "mains=[];",
        "mains=[main,main];",
        "main.children=[friends,add];",
        "main.children=[friends,friends];",
        "main.children=[];",
        "friends.hidden=true;",
        "friends.zeroWidth=true;",
        "friends.disabled=true;",
        "friends.attrs['aria-disabled']='true';",
        "friends.innerText='Friends of friends';",
        "friends.attrs['aria-label']='Unfollow';",
        "profile.is_viewer_friend='true';",
        "delete profile.is_viewer_friend;",
        "profile.is_viewer_friend=false;",
        "document.body.innerText='Try again later';",
        "document.body.innerText='Log in to continue';",
        "document.title='Your account has been restricted';",
        "document.readyState='loading';",
        "canonical=[el('link',{rel:'canonical',href:'https://www.facebook.com/other.target'})];",
    ],
)
def test_each_step_revalidates_actor_target_warning_and_unique_controls(setup, step):
    result = run(STAGES[step] + setup, step)
    assert result["result"].get("error")
    assert result["clicks"] == []


@pytest.mark.parametrize(
    "setup",
    [
        "main.children=[];",
        "main.children=[el('div',{role:'button','aria-label':'Message'},'Message')];",
        "main.children=[add,friends];",
        "profile.is_viewer_friend=true;main.children=[add];",
        "profile.is_viewer_friend=null;main.children=[add];",
    ],
)
def test_absence_requires_both_false_friend_flag_and_exact_add_friend(setup):
    assert run(setup)["result"].get("error")


@pytest.mark.parametrize("step", ["open", "preview", "remove"])
@pytest.mark.parametrize("remote_id", [None, "", "0", "nan", 900002, True])
def test_missing_or_malformed_bound_identity_never_clicks(step, remote_id):
    result = run(STAGES[step], step, remote_id=remote_id)
    assert result["result"].get("error")
    assert result["clicks"] == []


def test_unbound_identity_is_allowed_only_for_read_only_observation():
    assert run(remote_id=None)["result"].get("error")
    assert run(step="observe", remote_id=None) == {
        "result": {"remote_id": "900002", "state": "following"},
        "clicks": [],
    }


@pytest.mark.parametrize("step", ["open", "preview", "remove"])
def test_expiry_checked_at_click_after_identity_and_dom_work(step):
    result = run(STAGES[step] + "let now=0;Date.now=()=>++now===1?1:4102444800000;", step)
    assert result["result"].get("error")
    assert result["clicks"] == []


@pytest.mark.parametrize("step", ["open", "preview", "remove"])
def test_expired_plan_never_clicks(step):
    result = run(STAGES[step] + "Date.now=()=>4102444800000;", step)
    assert result["clicks"] == []
    assert result["result"].get("error")


@pytest.mark.parametrize(
    "setup",
    [
        "menus=[menu,menu];",
        "menus=[];",
        "dialogs=[dialog];",
        "menu.attrs['aria-label']='Other menu';",
        "friends.attrs['aria-expanded']='false';",
        "menu.children=[menuItem,menuItem];",
        "menuItem.hidden=true;",
        "menuItem.attrs['aria-disabled']='true';",
        "menuItem.innerText='Unfollow';",
    ],
)
def test_preview_requires_unique_friend_management_menu(setup):
    result = run(STAGES["preview"] + setup, "preview")
    assert result["result"].get("error")
    assert result["clicks"] == []


@pytest.mark.parametrize(
    "setup",
    [
        "dialogs=[];",
        "dialogs=[dialog,dialog];",
        "menus=[menu];",
        "dialog.children=[title,confirm,cancel];",  # live observed layout: name only
        "link.attrs.href='https://www.facebook.com/other.target';",
        "link.hidden=true;",
        "link.attrs.href+='?tracking=1';",
        "dialog.children.push(el('a',{href:'https://www.facebook.com/other.target'},'Example Person'));",
        "title.innerText='Unfriend Other Person';",
        "dialog.attrs['aria-label']='Unfriend Other Person';",
        "dialog.children.push(confirm);",
        "confirm.hidden=true;",
        "confirm.disabled=true;",
        "confirm.attrs['aria-disabled']='true';",
        "confirm.attrs['aria-label']='Unfollow';",
        "confirm.innerText='Unfriend';",
        "cancel.hidden=true;",
    ],
)
def test_final_confirmation_requires_stable_dialog_target_and_exact_controls(setup):
    result = run(STAGES["remove"] + setup, "remove")
    assert result["result"].get("error")
    assert result["clicks"] == []


class DocumentBrowser(cdp.Browser):
    """Exercise real CDP navigation readiness, replacing only transport and DOM."""

    def __init__(self, *, next_origin=2000, ready_state="complete", revert=False, setup=""):
        self.origin = 1000
        self.next_origin = next_origin
        self.ready_state = ready_state
        self.revert = revert
        self.setup = setup
        self._session_id = "synthetic"
        self._block_callback = None
        self._stop_event = None
        self._capture_tasks = []

    def eval(self, script):
        setup = self.setup + (
            f"performance.timeOrigin={json.dumps(self.origin)};"
            f"document.readyState={json.dumps(self.ready_state)};"
        )
        return execute_js(script, setup)["result"]

    async def _eval_async(self, script):
        return self.eval(script)

    async def _send(self, method, params, *, session_id):
        assert method == "Page.navigate"
        self.origin = self.next_origin
        return {"frameId": "synthetic", "loaderId": "synthetic-loader"}

    def navigate(self, url, wait_ms, capture, ready_js):
        assert capture == []
        result = asyncio.run(self._navigate_async(url, 0, capture, ready_js))
        if self.revert:
            self.origin = 1000
        return result


@pytest.mark.parametrize("operation", ["inspect", "observe"])
@pytest.mark.parametrize(
    "options",
    [
        {"next_origin": 1000},
        {"next_origin": 900},
        {"ready_state": "loading"},
        {"revert": True},
    ],
)
def test_readers_refuse_old_incomplete_or_reverted_documents(operation, options):
    browser = DocumentBrowser(**options)
    plan, target = plan_target()
    with pytest.raises(instagram_action.Refused):
        if operation == "inspect":
            adapter().inspect(browser, plan, target)
        else:
            adapter().observe(browser, plan.actor, target.handle, target.url)


def test_observe_discovers_id_but_inspect_requires_plan_binding():
    plan, target = plan_target(remote_id=None)
    assert adapter().observe(DocumentBrowser(), plan.actor, target.handle, target.url) == {
        "remote_id": "900002",
        "state": "following",
    }
    with pytest.raises(instagram_action.Refused):
        adapter().inspect(DocumentBrowser(), plan, target)


def test_inspection_accepts_absence_only_on_fresh_document():
    plan, target = plan_target()
    assert (
        adapter().inspect(
            DocumentBrowser(setup="profile.is_viewer_friend=false;main.children=[add];"),
            plan,
            target,
        )
        == "absent"
    )


def test_observation_checks_actor_and_url():
    plan, target = plan_target()
    with pytest.raises(instagram_action.Refused):
        adapter().observe(DocumentBrowser(), "900099", target.handle, target.url)
    with pytest.raises(instagram_action.Refused):
        adapter().observe(DocumentBrowser(), plan.actor, target.handle, target.url + "?bad=1")


class ActionBrowser:
    def __init__(self, drift="", drift_stage=1):
        self.stage = 0
        self.clicks = []
        self.drift = drift
        self.drift_stage = drift_stage

    def check_stop(self):
        pass

    def eval(self, script):
        setup = ["", STAGES["preview"], STAGES["remove"]][self.stage]
        if self.stage == self.drift_stage:
            setup += self.drift
        result = execute_js(script, setup)
        self.clicks.extend(result["clicks"])
        if result["clicks"]:
            self.stage += 1
        return result["result"]

    def wait_for(self, predicate, timeout_s):
        return bool(self.eval(predicate))


def test_perform_submits_only_once_without_claiming_absence():
    browser = ActionBrowser()
    assert adapter().perform(browser, *plan_target()) is None
    assert browser.clicks == ["Friends", "Unfriend", "Confirm"]


@pytest.mark.parametrize("stage", [1, 2])
@pytest.mark.parametrize(
    "drift",
    [
        "current.USER_ID='900099';",
        "profile.id='900099';",
        "document.body.innerText='Try again later';",
        "Date.now=()=>4102444800000;",
    ],
)
def test_perform_never_clicks_after_between_step_drift(stage, drift):
    browser = ActionBrowser(drift, stage)
    with pytest.raises(instagram_action.Refused):
        adapter().perform(browser, *plan_target())
    assert browser.clicks == ["Friends", "Unfriend"][:stage]


def test_perform_refuses_live_observed_name_only_confirmation():
    browser = ActionBrowser("dialog.children=[title,confirm,cancel];", 2)
    with pytest.raises(instagram_action.Refused):
        adapter().perform(browser, *plan_target())
    assert browser.clicks == ["Friends", "Unfriend"]


@pytest.mark.parametrize("handle", ["900002", "profile.php?id=900002"])
def test_numeric_profile_urls_bind_the_observed_target_id(handle):
    plan, target = plan_target()
    target.handle = handle
    target.url = "https://www.facebook.com/" + handle
    setup = f"profile.url={json.dumps(target.url)};location.href=profile.url;"
    assert execute_js(adapter().script(plan, target, "observe"), setup)["result"] == {
        "remote_id": "900002",
        "state": "following",
    }
    setup += "profile.id='900004';"
    target.remote_id = None
    assert execute_js(adapter().script(plan, target, "observe"), setup)["result"].get("error")


@pytest.mark.parametrize(
    "setup",
    [
        "profile.id='900001';",
        "delete profile.name;",
        "profile.name='';",
        "extraData.push(['CurrentUserInitialData',[],{},1]);",
        "friends.attrs['aria-haspopup']='menu';",
        "menus=[menu];",
        "dialogs=[dialog];",
    ],
)
def test_observation_refuses_self_missing_identity_and_overlays(setup):
    result = run(setup, "observe", remote_id=None)
    assert result["result"].get("error")
    assert result["clicks"] == []


@pytest.mark.parametrize("origin", [None, False, 0, -1, "1000", float("inf"), float("nan")])
def test_missing_document_identity_refuses_before_navigation(origin):
    class Browser:
        def check_stop(self):
            pass

        def eval(self, script):
            return origin

        def navigate(self, *args, **kwargs):
            pytest.fail("must refuse before navigating")

    with pytest.raises(instagram_action.Refused):
        adapter().inspect(Browser(), *plan_target())


@pytest.mark.parametrize(
    "state,remote_id",
    [
        ("submitted", "900002"),
        ("following", "page-controlled-name"),
        ("following", "900099"),
        ("unknown", "900002"),
        ("absent", None),
    ],
)
def test_invalid_browser_results_never_escape_the_read_boundary(state, remote_id):
    class Browser(DocumentBrowser):
        def eval(self, script):
            if script == "performance.timeOrigin":
                return 1000
            return {"state": state, "remote_id": remote_id, "extra": "discard me"}

        def navigate(self, *args, **kwargs):
            return {"data_ready": True}

    with pytest.raises(instagram_action.Refused) as error:
        adapter().inspect(Browser(), *plan_target())
    assert "page-controlled" not in str(error.value)


def test_shared_stop_prevents_any_browser_evaluation():
    class Browser:
        def check_stop(self):
            raise RuntimeError("stopped")

        def eval(self, script):
            pytest.fail("must stop before inspecting or clicking")

    for operation in (adapter().inspect, adapter().perform):
        with pytest.raises(RuntimeError, match="stopped"):
            operation(Browser(), *plan_target())
