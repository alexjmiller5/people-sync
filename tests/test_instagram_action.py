"""Execute the adapter JavaScript against synthetic DOMs, not substring mocks."""

import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from people_sync.scrape import cdp, instagram_action

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node required for DOM predicates")

DOM = r"""
let clicks=[];
function el(text='', options={}) {
  return Object.assign({innerText:text, disabled:false,
    checkVisibility(o) {return o.opacityProperty && o.visibilityProperty && !this.hidden;},
    getBoundingClientRect() {return {width:100,height:30};},
    getAttribute(k) {return this[k] || null;},
    querySelector() {return null;}, querySelectorAll() {return [];},
    closest() {return null;}, click() {clicks.push(this.innerText);}
  }, options);
}
let following=el('Following'), follow=el('Follow'), unfollow=el('Unfollow');
let heading=el('example_target');
let avatar=el('', {alt:"example_target's profile picture"});
let actor=el('', {href:'https://www.instagram.com/example_operator/',
  querySelector() {return el('', {alt:"example_operator's profile picture"});}});
let header=el('', {querySelectorAll(s) {
  if(s==='h1,h2') return [heading];
  if(s==='button,[role=button]') return this.controls || [following];
  if(s==='img') return [avatar];
  return [];
}});
let dialog=el('', {innerText:'example_target\nUnfollow\nCancel',
  querySelectorAll(s) {
    if(s==='button,[role=button]') return this.controls || [unfollow];
    if(s==='img') return [avatar];
    if(s==='h1,h2,h3') return [heading];
    if(s==='a[href]') return [];
    return [];
}});
let dialogs=[];
let viewer={id:'800001',username:'example_operator'};
let profileProps={userID:'900001',username:'example_target',viewer:{...viewer}};
header.__reactFiber$synthetic={memoizedProps:{children:[]},return:{
  memoizedProps:profileProps,alternate:{memoizedProps:{...profileProps}},return:{
    type:'main',memoizedProps:{children:[]},return:null
  }
}};
let viewerModule=['PolarisViewer',[],{id:'800001',data:viewer},7];
let modules=[viewerModule];
let unrelatedUser={username:'example_target',id:'900001',is_private:false};
globalThis.location={href:'https://www.instagram.com/example_target/'};
globalThis.document={title:'Instagram',body:{innerText:''},
  querySelectorAll(s) {
    if(s==='header') return [header];
    if(s==='a[href]') return [actor];
    if(s==='[role=dialog]') return dialogs;
    if(s==='link[rel=canonical]') return [];
    if(s==='script[type="application/json"]')
      return [{textContent:JSON.stringify({data:{user:unrelatedUser},require:[['SyntheticLoader',[],[{__bbox:{define:modules}}]]]} )}];
    return [];
  }
};
"""


def run(setup="", step="inspect", remote_id="900001"):
    plan = SimpleNamespace(actor="example_operator", expires_at=4102444800)
    target = SimpleNamespace(
        handle="example_target",
        url="https://www.instagram.com/example_target/",
        remote_id=remote_id,
    )
    js = instagram_action.script(plan, target, step)
    output = subprocess.run(
        [NODE, "-"],
        input=DOM + setup + f"\nlet result={js};\nconsole.log(JSON.stringify({{result,clicks}}));",
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(output.stdout)


def test_following_state_and_guarded_click():
    assert run()["result"] == {"state": "following"}
    assert run(step="open")["clicks"] == ["Following"]
    assert run("dialogs=[dialog];", "remove")["clicks"] == ["Unfollow"]


def test_absence_requires_exact_positive_follow_control():
    assert run("header.controls=[follow];")["result"] == {"state": "absent"}
    assert run("header.controls=[];")["result"].get("error")
    assert run("header.controls=[el('Requested')];")["result"].get("error")


@pytest.mark.parametrize(
    "setup",
    [
        "heading.innerText='other_target';",
        "location.href='https://www.instagram.com/other_target/';",
        "actor.href='https://www.instagram.com/other_operator/';",
        "actor.querySelector=()=>null;",
        "header.controls=[following,el('Following')];",
        "header.controls=[following,follow];",
        "header.controls=[el('Following more')];",
        "header.controls=[el('Following',{hidden:true})];",
        "header.controls=[el('Following',{disabled:true})];",
        "dialogs=[dialog];",
        "document.body.innerText='Try again later';",
        "document.body.innerText='Log in to continue';",
    ],
)
@pytest.mark.parametrize("step", ["inspect", "open"])
def test_wrong_identity_ambiguous_dom_or_warning_never_clicks(setup, step):
    result = run(setup, step)
    assert result["result"].get("error")
    assert result["clicks"] == []


@pytest.mark.parametrize(
    "setup",
    [
        "dialogs=[];",
        "dialogs=[dialog,dialog];",
        "heading.innerText='other_target';",
        "avatar.alt='another profile';dialog.innerText='Unfollow';dialog.querySelectorAll=(s)=>s==='button,[role=button]'?[unfollow]:[];",
        "dialog.controls=[unfollow,el('Unfollow')];",
        "dialog.controls=[el('Remove follower')];",
        "document.body.innerText='We restrict certain activity';",
        "actor.href='https://www.instagram.com/other_operator/';",
    ],
)
def test_remove_requires_identity_bound_unique_dialog(setup):
    result = run("dialogs=[dialog];" + setup, "remove")
    assert result["result"].get("error")
    assert result["clicks"] == []


def test_warning_after_menu_opens_blocks_final_click():
    result = run(
        "dialogs=[dialog];document.body.innerText='Your account has been restricted';", "remove"
    )
    assert result["clicks"] == []


def test_bound_remote_identity_refuses_reassigned_handle():
    setup = "profileProps.userID='900002';header.__reactFiber$synthetic.return.alternate.memoizedProps.userID='900002';"
    assert run(setup, "open", "900001")["clicks"] == []
    assert run(setup, "open", "900001")["result"].get("error")
    assert run(setup, "open", "900002")["clicks"] == ["Following"]
    assert run("delete header.__reactFiber$synthetic;", "open", "900001")["clicks"] == []


def test_expiration_after_opening_menu_blocks_final_click():
    result = run("dialogs=[dialog];Date.now=()=>9999999999000;", "remove")
    assert result["result"].get("error")
    assert result["clicks"] == []


@pytest.mark.parametrize("step", ["open", "remove"])
def test_handle_only_plan_never_clicks(step):
    result = run("dialogs=[dialog];" if step == "remove" else "", step, remote_id=None)
    assert result["result"].get("error")
    assert result["clicks"] == []


@pytest.mark.parametrize("step", ["inspect", "open", "remove", "observe"])
@pytest.mark.parametrize(
    "setup",
    [
        "modules=[];",
        "viewerModule[0]='UnrelatedViewer';",
        "viewer.username='other_operator';",
        "viewer.username='other_operator';profileProps.viewer.username='other_operator';",
        "viewerModule[2].id='800002';",
        "modules.push(['PolarisViewer',[],{id:'800002',data:{id:'800002',username:'other_operator'}},8]);",
        "profileProps.viewer={id:'800002',username:'other_operator'};",
        "delete profileProps.viewer.id;",
        "profileProps.userID='800001';",
        "profileProps.username='other_target';",
        "header.__reactFiber$synthetic.return.alternate.memoizedProps.userID='900002';",
    ],
)
def test_authenticated_identity_required_even_with_matching_sidebar_link(setup, step):
    if step == "remove":
        setup += "dialogs=[dialog];"
    result = run(setup, step, remote_id=None if step == "observe" else "900001")
    assert result["result"].get("error")
    assert result["clicks"] == []


def test_observe_returns_only_verified_identity_and_state_without_clicking():
    result = run(step="observe", remote_id=None)
    assert result == {"result": {"remote_id": "900001", "state": "following"}, "clicks": []}
    result = run("header.controls=[follow];", step="observe", remote_id=None)
    assert result == {"result": {"remote_id": "900001", "state": "absent"}, "clicks": []}


def test_unbound_inspection_cannot_report_absence():
    result = run("header.controls=[follow];", remote_id=None)
    assert result["result"].get("error")


class DocumentBrowser(cdp.Browser):
    """Real CDP readiness loop; only transport and the browser document are synthetic."""

    def __init__(self, next_origin, ready_state="complete", revert_after_navigation=False):
        self.origin = 1000
        self.next_origin = next_origin
        self.ready_state = ready_state
        self.revert_after_navigation = revert_after_navigation
        self._session_id = "synthetic"
        self._block_callback = None
        self._stop_event = None
        self._capture_tasks = []

    def eval(self, script):
        setup = (
            "Object.defineProperty(globalThis,'performance',{value:{timeOrigin:"
            + json.dumps(self.origin)
            + "}});document.readyState="
            + json.dumps(self.ready_state)
            + ";header.controls=[follow];document.querySelector=(s)=>heading;"
        )
        output = subprocess.run(
            [NODE, "-"],
            input=DOM + setup + f"\nconsole.log(JSON.stringify({script}));",
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(output.stdout)

    async def _eval_async(self, script):
        return self.eval(script)

    async def _send(self, method, params, *, session_id):
        assert method == "Page.navigate"
        self.origin = self.next_origin
        return {"frameId": "synthetic", "loaderId": "synthetic-loader"}

    def navigate(self, url, wait_ms, capture, ready_js):
        # A zero timeout still evaluates readiness once, without real-time sleeps.
        result = asyncio.run(self._navigate_async(url, 0, capture, ready_js))
        if self.revert_after_navigation:
            self.origin = 1000
        return result


@pytest.mark.parametrize(
    ("next_origin", "ready_state", "revert"),
    [
        (1000, "complete", False),
        (900, "complete", False),
        (2000, "loading", False),
        (2000, "complete", True),
    ],
)
def test_inspection_never_accepts_old_or_incomplete_document(next_origin, ready_state, revert):
    browser = DocumentBrowser(next_origin, ready_state, revert)
    plan = SimpleNamespace(actor="example_operator", expires_at=4102444800)
    target = SimpleNamespace(
        handle="example_target", url="https://www.instagram.com/example_target/", remote_id="900001"
    )
    with pytest.raises(instagram_action.Refused):
        instagram_action.inspect(browser, plan, target)


def test_inspection_accepts_loaded_new_document():
    browser = DocumentBrowser(2000)
    plan = SimpleNamespace(actor="example_operator", expires_at=4102444800)
    target = SimpleNamespace(
        handle="example_target", url="https://www.instagram.com/example_target/", remote_id="900001"
    )
    assert instagram_action.inspect(browser, plan, target) == "absent"


def test_observe_public_api_reads_fresh_profile_without_expected_id():
    browser = DocumentBrowser(2000)
    assert instagram_action.observe(
        browser, "example_operator", "example_target", "https://www.instagram.com/example_target/"
    ) == {"remote_id": "900001", "state": "absent"}


@pytest.mark.parametrize("remote_id", [None, "", "0", 900001])
def test_inspect_requires_plan_identity_before_navigation(remote_id):
    browser = DocumentBrowser(2000)
    plan = SimpleNamespace(actor="example_operator", expires_at=4102444800)
    target = SimpleNamespace(
        handle="example_target",
        url="https://www.instagram.com/example_target/",
        remote_id=remote_id,
    )
    with pytest.raises(instagram_action.Refused):
        instagram_action.inspect(browser, plan, target)
    assert browser.origin == 1000


@pytest.mark.parametrize("origin", [None, False, 0, -1, "1000"])
def test_invalid_original_document_identity_cannot_authorize_inspection(origin):
    browser = DocumentBrowser(2000)
    browser.origin = origin
    plan = SimpleNamespace(actor="example_operator", expires_at=4102444800)
    target = SimpleNamespace(
        handle="example_target", url="https://www.instagram.com/example_target/", remote_id="900001"
    )
    with pytest.raises(instagram_action.Refused):
        instagram_action.inspect(browser, plan, target)
    assert browser.origin == origin
