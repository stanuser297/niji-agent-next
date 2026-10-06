import base64
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import unittest
import tempfile
from pathlib import Path
import urllib.error
import urllib.request
from unittest.mock import patch

from niji.webui import NijiWebUI
from niji import setup_wizard  # Ensure its lazy provider tester is patchable in tests.
from niji.webui_frontend import PAGE
from niji.planning import save_plan as REAL_SAVE_PLAN


class FakeAgent:
    def __init__(self, require_approval=False):
        self.provider_cfg = {"provider": "test", "model": "demo-model", "api_key": "private-test-secret"}
        self.provider_name = "test"
        self.model = "demo-model"
        self.client = object()
        self.session_id = "test-session"
        self.approval = "ask"
        self.approval_callback = None
        self.activity_callback = None
        self.activity = [{"time": "12:00:00", "level": "READY", "message": "Ready"}]
        self._activity_lock = threading.RLock()
        self.usage = {"turns": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.tool_usage = {}
        self.tool_schemas = [{"function": {"name": "read_file"}}]
        self.messages = [
            {"role": "system", "content": "private system instructions"},
            {"role": "user", "content": "[Environment: test]"},
        ]
        self.max_turns = 10
        self.max_tool_calls = 30
        self.max_tool_calls_per_turn = 6
        self.require_approval = require_approval
        self.tool_policies = {}
        self.last_plan_only = False
        self.last_images = []
        self.approved_plan_seen = None
        self.leave_plan_incomplete = False
        self.corrupt_completion_evidence = False
        self.todos = {"items": []}
        self.plan_callback = None
        self.pause_stream = False
        self.stream_ready = threading.Event()
        self.finish_stream = threading.Event()
        self.cancel_requested = threading.Event()
        self._cancel_event = threading.Event()
        self.pause_requested = threading.Event()
        self.resume_gate = threading.Event()
        self.resume_gate.set()
        self.after_pause_resume = None

    def cancel(self):
        self.cancel_requested.set()
        self._cancel_event.set()
        self.finish_stream.set()
        self.resume_gate.set()

    def request_pause(self):
        if self.cancel_requested.is_set():
            return False
        self.resume_gate.clear()
        self.pause_requested.set()
        return True

    def request_resume(self):
        self.pause_requested.clear()
        self.resume_gate.set()
        return not self.cancel_requested.is_set()

    def _pause_at_boundary(self):
        if not self.pause_requested.is_set():
            return not self.cancel_requested.is_set()
        if self.activity_callback:
            self.activity_callback({"time": "12:02:01", "level": "PAUSED", "message": "Paused safely"})
        while self.pause_requested.is_set() and not self.cancel_requested.is_set():
            self.resume_gate.wait(0.05)
        if not self.cancel_requested.is_set() and self.activity_callback:
            self.activity_callback({"time": "12:02:02", "level": "RESUMED", "message": "Resuming safely"})
        if callable(self.after_pause_resume):
            self.after_pause_resume()
        return not self.cancel_requested.is_set()

    def _pause_at_safe_boundary(self):
        return self._pause_at_boundary()

    def resume(self, messages):
        self.messages = list(messages)

    def _record_activity(self, level, message):
        self.activity.append({"time": "12:01:00", "level": level, "message": message})

    def chat(self, message, image_attachments=None):
        self.last_images = list(image_attachments or [])
        self.last_plan_only = bool(getattr(self, "plan_only", False))
        approved_plan = getattr(self, "approved_plan", None)
        self.approved_plan_seen = list(approved_plan) if approved_plan is not None else None
        if self.activity_callback:
            self.activity_callback({"time": "12:02:00", "level": "THINKING", "message": "Thinking on it"})
        self.messages.append({"role": "user", "content": message})
        if approved_plan is not None and not self.leave_plan_incomplete:
            from niji.tools import dispatch
            for index in range(len(approved_plan)):
                active = [dict(item) for item in self.todos["items"]]
                active[index]["status"] = "in_progress"
                started = dispatch("todo_write", {"todos": active, "activeForm": "Working"},
                                   {"agent": self, "todos": self.todos})
                if str(started).startswith("[error]"):
                    raise AssertionError(started)
                completed = [dict(item) for item in self.todos["items"]]
                completed[index]["status"] = "completed"
                completed[index]["evidence"] = "Fixture observed a successful step result."
                result = dispatch("todo_write", {"todos": completed, "activeForm": "Verified"},
                                  {"agent": self, "todos": self.todos})
                if str(result).startswith("[error]"):
                    raise AssertionError(result)
            if self.corrupt_completion_evidence and self.todos["items"]:
                self.todos["items"][-1]["evidence"] = "ok"
        if self.require_approval:
            approved = self.approval_callback("write_file", {"path": "notes.txt"})
            answer = "approved" if approved else "denied"
        elif self.last_plan_only:
            answer = "1. Inspect the project\\n2. Run the tests"
        else:
            answer = "Hello from Niji: " + message
        if self.stream_callback:
            self.stream_callback(answer[:10])
            self.stream_ready.set()
            if self.pause_stream:
                self.finish_stream.wait(2)
            if not self.cancel_requested.is_set():
                self.stream_callback(answer[10:])
        self._pause_at_boundary()
        if self.cancel_requested.is_set():
            return "[Stopped by user]"
        self.messages.append({"role": "assistant", "content": answer})
        self.usage["turns"] += 1
        return answer


class WebUITests(unittest.TestCase):
    def test_cli_dispatches_ui_subcommand(self):
        from niji.cli import main
        with patch("niji.cli.sys.argv", ["niji", "ui", "--port", "0"]):
            with patch("niji.cli._cmd_ui") as cmd:
                main()
        cmd.assert_called_once_with(["--port", "0"])

    def setUp(self):
        self.agent = FakeAgent()
        self._run_temp = tempfile.TemporaryDirectory()
        self._runs_patch = patch("niji.webui.RUNS_DIR", Path(self._run_temp.name) / "runs")
        self._runs_patch.start()
        self._plan_load_patch = patch("niji.webui.load_plan", return_value=[])
        self._plan_save_patch = patch("niji.webui.save_plan")
        self._stateful_plan_save_patch = patch("niji.planning.save_plan")
        self._plan_load_patch.start()
        self._plan_save_patch.start()
        self._stateful_plan_save_patch.start()
        self.ui = NijiWebUI(self.agent, port=0)
        self.thread = threading.Thread(target=self.ui.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.ui.httpd.server_port}"

    def tearDown(self):
        self.ui.close()
        self.thread.join(timeout=2)
        self._plan_load_patch.stop()
        self._plan_save_patch.stop()
        self._stateful_plan_save_patch.stop()
        self._runs_patch.stop()
        self._run_temp.cleanup()

    def request(self, path, data=None, token=None):
        body = json.dumps(data).encode() if data is not None else None
        headers = {}
        if token is not None:
            headers["X-Niji-Token"] = token
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=body, headers=headers)
        return urllib.request.urlopen(req, timeout=3)

    def wait_for_job_status(self, job_id, expected, timeout=3):
        deadline = time.time() + timeout
        job = None
        while time.time() < deadline:
            job = json.loads(self.request(f"/api/jobs/{job_id}", token=self.ui.token).read())
            if job["status"] == expected:
                return job
            time.sleep(0.02)
        self.fail(f"Job {job_id} did not reach {expected}; last status was {job and job.get('status')}")

    def start_paused_stream_job(self, message="pause this job"):
        self.agent.pause_stream = True
        started = json.loads(self.request("/api/chat", {"message": message}, self.ui.token).read())
        job_id = started["id"]
        self.assertTrue(self.agent.stream_ready.wait(2))
        response = self.request(f"/api/jobs/{job_id}/pause", {}, self.ui.token)
        self.assertEqual(response.status, 202)
        self.agent.finish_stream.set()
        self.wait_for_job_status(job_id, "paused")
        return job_id

    def test_plan_editor_exposes_optional_completion_criteria(self):
        self.assertIn("Optional completion criteria", PAGE)
        self.assertIn("acceptance_criteria:x.acceptance_criteria.trim()", PAGE)
        self.assertIn("maxLength=400", PAGE)

    def test_completed_runs_keep_action_history_and_plan_previews(self):
        self.assertIn("cleanupRunArtifacts(placeholder,j.plan_only&&j.status==='completed'&&!j.plan_approved)", PAGE)
        self.assertIn("cleanupRunArtifacts(placeholder,false)", PAGE)
        self.assertIn("activity.open=false", PAGE)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        start = PAGE.index("function cleanupRunArtifacts(")
        end = PAGE.index("\nfunction renderAttachments", start)
        helper = PAGE[start:end]
        script = helper + """
function node(){return {removed:false,remove(){this.removed=true}}}
const plan=node(),events=node(),activity={removed:false,remove(){this.removed=true}},placeholder={querySelector(selector){return selector==='.plan-progress'?plan:selector==='.run-activity'?activity:events}};
cleanupRunArtifacts(placeholder,false);
if(!plan.removed||events.removed||activity.removed)throw new Error('finished run did not retain its action history');
const saved=node(),savedEvents=node(),preview={querySelector(selector){return selector==='.plan-progress'?saved:savedEvents}};
cleanupRunArtifacts(preview,true);
if(saved.removed||savedEvents.removed)throw new Error('plan-only preview was removed');
"""
        subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)

    def test_frontend_image_upload_controls_are_present(self):
        self.assertIn("image/png,image/jpeg,image/webp", PAGE)
        self.assertIn("async function uploadImage(file,mime)", PAGE)
        self.assertIn("api('/api/uploads'", PAGE)
        self.assertIn("image_ids:imageIds", PAGE)
        self.assertIn("vision-capable model", PAGE)
        self.assertIn("attachment-preview", PAGE)

    def test_frontend_exposes_pause_resume_and_paused_status(self):
        self.assertIn('id="pause-resume"', PAGE)
        self.assertIn("async function togglePauseJob()", PAGE)
        self.assertIn("j.status==='paused'", PAGE)
        self.assertIn("Paused safely", PAGE)

    def test_live_status_stays_minimal_and_single_line(self):
        live_css = PAGE[PAGE.index("/* Minimal live status"):PAGE.index("/* Niji home refresh")]
        self.assertIn("display:flex;align-items:center;gap:9px", live_css)
        self.assertIn("white-space:nowrap;overflow:hidden;text-overflow:ellipsis", live_css)
        self.assertIn(".message.live-status.has-stream{display:flex;flex-wrap:wrap}", live_css)
        self.assertIn(".message.live-status .run-activity{display:none!important}", live_css)
        self.assertIn(".message.live-status .plan-progress", live_css)
        self.assertNotIn("border-radius:15px", live_css)
        self.assertNotIn("padding-top:7px;border-top", live_css)
        self.assertIn("activity.open=false", PAGE)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for frontend logic tests")
    def test_live_run_elapsed_and_plan_progress_logic(self):
        self.assertIn("updateRunElapsed(j.created,placeholder)", PAGE)
        self.assertIn("role','progressbar'", PAGE)
        self.assertIn("aria-valuenow", PAGE)
        self.assertIn("Task plan · ${done}/${items.length} complete · ${percent}%", PAGE)
        self.assertIn(".message.live-status .workmeta{display:block;width:100%;max-width:100%;padding:0;margin:0;border:0;color:var(--muted);font-size:14px", PAGE)
        self.assertIn(".message.live-status.has-stream .worktext{display:block;flex:1;min-width:0;overflow:hidden}", PAGE)
        script = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
        def function_source(start, end):
            return script[script.index(start):script.index(end, script.index(start))]
        helpers = function_source("function summarizeLiveTask(", "function addWorkingBubble(")
        helpers += function_source("function taskCompletionPercent(", "function formatRunElapsed(")
        helpers += function_source("function formatRunElapsed(", "function updateRunElapsed(")
        helpers += function_source("function updateProgress(", "function renderFileChanges(")
        helpers += function_source("function visibleJobEvents(", "function renderJobEvents(")
        helpers += function_source("function currentJobActivityLabel(", "async function watchJob(")
        probe = helpers + """
function node(){return {dataset:{},style:{},children:[],hidden:false,replacements:0,append(...items){this.children.push(...items)},replaceChildren(){this.children=[];this.replacements++},setAttribute(){}}}
function buildTaskPlanList(items){return node()}
global.document={createElement(){return node()}};
const planRoot=node(),planPlaceholder={querySelector(s){return s==='.plan-progress'?planRoot:null}},planItems=[{status:'in_progress',content:'Build safely'}];
renderJobPlan(planItems,planPlaceholder);renderJobPlan(planItems,planPlaceholder);const stablePlanRenders=planRoot.replacements;renderJobPlan([{status:'completed',content:'Build safely'}],planPlaceholder);const changedPlanRenders=planRoot.replacements;
const now=Date.UTC(2026,0,1);
const longTask='Review the cloud execution architecture and verify the isolated worker lifecycle across the project';
const meta={textContent:'',title:'',attrs:{},setAttribute(k,v){this.attrs[k]=v},getAttribute(k){return this.attrs[k]}};
const detailEl={textContent:''};
const placeholder={dataset:{},querySelector(s){return s==='.workmeta'?meta:(s==='.workdetail'?detailEl:null)}};
updateProgress({label:'Using a tool',detail:'Tool call: run_tests · Running project tests.'},true,placeholder);
const toolAction=meta.textContent;
updateProgress({label:'Using a tool',detail:'Tool call: slack.search.messages · Searching Slack messages.'},true,placeholder);const connectorAction=meta.textContent;
const recentEvents=visibleJobEvents([{level:'THINKING',message:'Thinking through the next step'},{level:'PLAN',message:'Executing 1 of 1 requested tool call(s)'},{level:'TOOL',message:'Tool call: list_files · Listing workspace files'}]);const manyEvents=visibleJobEvents(Array.from({length:40},(_,index)=>({level:'TOOL',message:'Step '+index})));const longEvent=formatJobEventMessage({message:'x'.repeat(160)});
updateProgress({label:'Using a tool',detail:'Tool call: bash · still running (12s)'},true,placeholder);
const timedAction=meta.textContent;
const liveAction=currentJobActivityLabel({status:'running',progress:'Preparing the next step',activity:{level:'THINKING',message:'Thinking through the next step'}});const markdownAction=currentJobActivityLabel({status:'running',progress:'Working',activity:{level:'PLAN',message:'*Evidence*: Record that the step has no further work'}});const pausedAction=currentJobActivityLabel({status:'paused',progress:'Paused safely',activity:{level:'TOOL',message:'Old action'}});
setLiveTaskLabel(placeholder,'Running tests');meta.textContent='Paused safely';setLiveTaskLabel(placeholder,'Running tests');const recoveredStatus=meta.textContent;setLiveTaskLabel(placeholder,'Paused safely');const pausedAria=meta.getAttribute('aria-label');setLiveTaskLabel(placeholder,'Pausing after the current action…');const pausingAria=meta.getAttribute('aria-label');
console.log(JSON.stringify({
  tasks:[summarizeLiveTask('  Fix\\n  the bug  '),summarizeLiveTask(''),summarizeLiveTask(null),summarizeLiveTask(longTask)],
  toolAction,connectorAction,timedAction,recentEvents:recentEvents.map(e=>e.level),manyEventCount:manyEvents.length,longEvent,
  liveLabels:[liveAction,markdownAction,pausedAction,currentJobActivityLabel({status:'running',progress:'Writing the response',activity:{level:'TOOL_DONE',message:'Old tool action'}})],
  planRenderCounts:[stablePlanRenders,changedPlanRenders],
  activityLabels:[formatJobEventMessage({message:'Tool call: list_files · Listing workspace files'}),formatJobEventMessage({message:'run_tests completed'}),formatJobEventMessage({message:'Tool call: bash · still running (9s)'}),formatJobEventMessage({message:'Tool call: slack.search.messages · Searching Slack messages'})],
  recoveredStatus,pausedAria,pausingAria,
  times:[formatRunElapsed(now/1000-75,now),formatRunElapsed(now/1000-3661,now),formatRunElapsed(true,now),formatRunElapsed([123],now),formatRunElapsed('bad',now),formatRunElapsed(now/1000+30,now)],
  progress:[taskCompletionPercent([{status:'pending'},{status:'completed'}]),taskCompletionPercent([{status:'completed'},{status:'completed'}]),taskCompletionPercent([{status:'pending'}]),taskCompletionPercent([]),taskCompletionPercent(null)]
}));
"""
        result = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["tasks"][:3], ["Fix the bug", "Working", "Working"])
        self.assertEqual(data["tasks"][3], "Review the cloud execution architecture and verify the isolated worker lifecycle across the project")
        self.assertEqual(data["toolAction"], "Running project tests.")
        self.assertEqual(data["connectorAction"], "Searching Slack messages.")
        self.assertEqual(data["recentEvents"], ["THINKING", "PLAN", "TOOL"])
        self.assertEqual(data["manyEventCount"], 40)
        self.assertEqual(data["longEvent"], "x" * 160)
        self.assertEqual(data["liveLabels"], ["Thinking through the next step", "Evidence: Record that the step has no further work", "Paused safely", "Writing response…"])
        self.assertEqual(data["planRenderCounts"], [1, 2])
        self.assertEqual(data["activityLabels"], ["Listing workspace files", "Finished run tests", "Still running · 9s", "Searching Slack messages"])
        self.assertEqual(data["recoveredStatus"], "Running tests")
        self.assertEqual(data["pausedAria"], "Current action: Paused safely")
        self.assertEqual(data["pausingAria"], "Current action: Pausing after the current action…")
        self.assertEqual(data["timedAction"], "Running a command… · 12s")
        
        self.assertEqual(data["times"], ["Total elapsed · 01:15", "Total elapsed · 01:01:01",
                                          "Total elapsed · —", "Total elapsed · —",
                                          "Total elapsed · —", "Total elapsed · 00:00"])
        self.assertEqual(data["progress"], [50, 100, 0, None, None])

    def test_frontend_expired_job_status_is_terminal_and_http_status_is_preserved(self):
        self.assertIn("err.status=r.status", PAGE)
        self.assertIn("if(e.status===404)", PAGE)
        self.assertIn("Run history expired or this tab is out of date", PAGE)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        start = PAGE.index("async function api(")
        end = PAGE.index("\nfunction ", start)
        api_function = PAGE[start:end]
        script = "const token='test-token';\n" + api_function + "\n" + r'''
fetch = async () => ({ok:false,status:404,json:async()=>({error:'Unknown job'})});
api('/api/jobs/expired').then(()=>{throw new Error('expected 404 rejection')}).catch(error=>{
  if(error.status!==404)throw new Error('HTTP status was not preserved');
  if(error.message!=='Unknown job')throw new Error('server error message was not preserved');
});
'''
        subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)

    def test_plan_editor_reordering_preserves_prerequisite_order(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        start = PAGE.index("function canMovePlanStep(")
        end = PAGE.index("\nfunction openPlanEditor", start)
        helper = PAGE[start:end]
        script = helper + """
const rows = [
  {id:'inspect', depends_on:[]},
  {id:'implement', depends_on:['inspect']},
  {id:'verify', depends_on:['implement']},
  {id:'docs', depends_on:[]}
];
if (canMovePlanStep(rows, 1, 0)) throw new Error('dependent step moved before prerequisite');
if (canMovePlanStep(rows, 0, 1)) throw new Error('prerequisite moved after dependent');
if (!canMovePlanStep(rows, 3, 2)) throw new Error('independent step could not be reordered');
"""
        subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)

    def test_plan_renderer_shows_dependency_waiting_state_safely(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        start = PAGE.index("function buildTaskPlanList(")
        end = PAGE.index("\nfunction renderJobPlan", start)
        renderer = PAGE[start:end]
        script = renderer + """
class FakeNode {
  constructor(tag){this.tagName=tag;this.children=[];this.attributes={};this.className='';this.textContent=''}
  append(...nodes){this.children.push(...nodes)}
  setAttribute(key,value){this.attributes[key]=value}
}
global.document={createElement:(tag)=>new FakeNode(tag)};
const tree=buildTaskPlanList([
  {id:'inspect',content:'Inspect <source>',status:'pending',acceptance_criteria:'Only current files are reviewed.'},
  {id:'build',content:'Build safely',status:'pending',depends_on:['inspect'],evidence:'Build completed without errors.'}
]);
function walk(node){return [node,...node.children.flatMap(walk)]}
const nodes=walk(tree);
if(!nodes.some(n=>n.className==='task-plan-deps waiting' && n.textContent==='Waiting for: Inspect <source>')) throw new Error('waiting dependency label missing');
if(!nodes.some(n=>n.className==='task-plan-state pending')) throw new Error('explicit pending status missing');
if(!nodes.some(n=>n.className==='task-plan-criteria' && n.textContent==='Check: Only current files are reviewed.')) throw new Error('acceptance criteria missing');
if(!nodes.some(n=>n.className==='task-plan-evidence' && n.textContent.includes('Build completed without errors.'))) throw new Error('completion evidence missing');
if(nodes.some(n=>n.innerHTML)) throw new Error('renderer used unsafe HTML');
"""
        subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)

    def test_assistant_markdown_formats_common_text_safely(self):
        self.assertIn("function displayMessageText(text)", PAGE)
        self.assertIn("function renderAssistantMarkdown(target,text)", PAGE)
        self.assertIn("async function downloadArtifactByPath(path)", PAGE)
        self.assertIn("niji-artifact:", PAGE)
        self.assertIn("if(role==='assistant')renderAssistantMarkdown(body,text)", PAGE)
        self.assertIn("renderAssistantMarkdown(body,j.streamed||'')", PAGE)
        self.assertIn("renderAssistantMarkdown(body,j.response||j.streamed", PAGE)
        self.assertIn(".msgbody ul,.msgbody ol", PAGE)
        self.assertIn(".md-table-wrap{max-width:100%;overflow-x:auto", PAGE)
        self.assertIn(".md-table th,.md-table td", PAGE)
        self.assertIn(".msgbody pre code", PAGE)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        start = PAGE.index("function displayMessageText(")
        end = PAGE.index("\nfunction addBubble", start)
        helper = PAGE[start:end]
        script = helper + r"""
class TestNode{constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.style={};this.dataset={};this._text=''}append(...nodes){this.children.push(...nodes)}replaceChildren(...nodes){this.children=[...nodes];this._text=''}set textContent(value){this._text=String(value);this.children=[]}get textContent(){return this._text+this.children.map(node=>node.textContent).join('')}}
global.document={createElement:tag=>new TestNode(tag),createTextNode:text=>{const node=new TestNode('#text');node._text=String(text);return node}};
const slash=String.fromCharCode(92);
const nl=String.fromCharCode(10);const bold=slash+'*'+slash+'*मैं ठीक हूँ, धन्यवाद!'+slash+'*'+slash+'*';const italic=slash+'*मैं कहां से हूँ?'+slash+'*';const codeItem=slash+'- '+slash+'`nvidia'+slash+'`';const input=[bold,'',slash+'- '+italic,codeItem,'','<script>alert(1)</script>'].join(nl);
const target=new TestNode('div');renderAssistantMarkdown(target,input);
function walk(node){return [node,...node.children.flatMap(walk)]}
const nodes=walk(target);
if(!nodes.some(node=>node.tagName==='STRONG'&&node.textContent==='मैं ठीक हूँ, धन्यवाद!'))throw new Error('escaped bold text was not rendered');
if(!nodes.some(node=>node.tagName==='EM'&&node.textContent==='मैं कहां से हूँ?'))throw new Error('escaped italic text was not rendered');
if(nodes.filter(node=>node.tagName==='LI').length!==2)throw new Error('escaped bullets were not rendered as a list');
if(!nodes.some(node=>node.tagName==='CODE'&&node.textContent==='nvidia'))throw new Error('inline code was not rendered');
const table=new TestNode('div');const tableInput=['| **क्षेत्र** | क्या कर सकता है | उदाहरण |','| :--- | :---: | ---: |','| **कोडिंग** | लेख और `code` | Python |','| _सुरक्षा_ | <script>alert(1)</script> | `--ask` |'].join(nl);renderAssistantMarkdown(table,tableInput);const tableNodes=walk(table);const tableElement=tableNodes.find(node=>node.tagName==='TABLE');if(!tableElement)throw new Error('Markdown table was not rendered');if(tableNodes.filter(node=>node.tagName==='TH').length!==3||tableNodes.filter(node=>node.tagName==='TD').length!==6)throw new Error('table cells are missing');if(tableNodes.filter(node=>node.tagName==='TH')[1].style.textAlign!=='center'||tableNodes.filter(node=>node.tagName==='TH')[2].style.textAlign!=='right')throw new Error('table column alignment was ignored');if(tableElement.textContent.includes('|'))throw new Error('table delimiter pipes leaked into display');if(tableNodes.some(node=>node.tagName==='SCRIPT'))throw new Error('HTML inside a table became executable');
const escapedTable=new TestNode('div');renderAssistantMarkdown(escapedTable,['| Name | Note |','| --- | --- |','| A | left '+slash+'| right |'].join(nl));const escapedCells=walk(escapedTable).filter(node=>node.tagName==='TD');if(escapedCells.length!==2||!escapedCells[1].textContent.includes('left | right'))throw new Error('escaped table separator was split');
const numbered=new TestNode('div');renderAssistantMarkdown(numbered,['1. first','2) second'].join(nl));if(walk(numbered).filter(node=>node.tagName==='LI').length!==2)throw new Error('ordered list markers were not rendered');
if(nodes.some(node=>node.tagName==='SCRIPT'||node.tagName==='IMG'))throw new Error('untrusted HTML became an element');
if(!target.textContent.includes('<script>alert(1)</script>'))throw new Error('HTML input was not preserved as safe text');
const unsafe=new TestNode('div');renderAssistantMarkdown(unsafe,'[bad](javascript:alert(1))');
if(walk(unsafe).some(node=>node.tagName==='A'))throw new Error('unsafe link protocol was allowed');
const artifact=new TestNode('div');renderAssistantMarkdown(artifact,'[Download report](niji-artifact://reports/report%20one.zip)');const artifactAnchor=walk(artifact).find(node=>node.tagName==='A');if(!artifactAnchor||artifactAnchor.dataset.artifactPath!=='reports/report one.zip'||typeof artifactAnchor.onclick!=='function')throw new Error('safe local artifact link was not rendered');
const emoji=new TestNode('div');renderAssistantMarkdown(emoji,'✅ '+slash+'*done'+slash+'* 😂');
if(emoji.textContent!=='✅ done 😂')throw new Error('emoji or escaped punctuation was corrupted: '+emoji.textContent);
"""
        subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)

    def test_page_requires_private_one_time_token(self):
        with self.assertRaises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(self.base + "/", timeout=3)
        self.assertEqual(missing.exception.code, 403)
        response = urllib.request.urlopen(self.ui.url, timeout=3)
        page = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn("NIJI AGENT", page)
        self.assertNotIn("\\nfunction renderSessions", page)
        self.assertIn(".chatcard{background:transparent;border:0;border-radius:0;box-shadow:none}", page)
        self.assertIn(".message{width:fit-content;max-width:min(88%,840px);border:0;border-radius:0;background:transparent;padding:0;", page)
        self.assertIn(".message.user{align-self:flex-end;width:fit-content;max-width:min(52%,680px);border:0;border-radius:0;background:transparent;padding:0", page)
        self.assertIn(".message.user .msgbody{text-align:right;unicode-bidi:plaintext}", page)
        self.assertIn("body.light .message.user .msglabel{color:#584a89}", page)
        self.assertNotIn("body.light .message.user{background:", page)
        self.assertIn('"Apple Color Emoji"', page)
        self.assertIn("body.light .chatcard{background:transparent}", page)
        self.assertIn(".chathead{display:none}", page)
        self.assertIn('id="attach-button"', page)
        self.assertIn('id="github-button"', page)
        self.assertIn('id="reasoning-level"', page)
        self.assertIn('id="mic-button"', page)
        self.assertIn('id="file-input"', page)
        self.assertIn("el('mic-button').onclick=toggleDictation", page)
        self.assertIn("addAttachments(files)", page)
        self.assertIn("Public GitHub repository reference", page)
        self.assertIn("window.SpeechRecognition", page)
        self.assertIn("File is over 64 KB", page)
        self.assertIn("X-Niji-Token", page)
        self.assertIn("RECENT THREADS", page)
        self.assertIn('id="model-provider"', page)
        self.assertIn('id="fetch-models"', page)
        self.assertIn('id="apply-model"', page)
        self.assertIn("async function togglePinnedSession", page)
        self.assertIn('id="view-overview"', page)
        self.assertIn('id="view-tools"', page)
        self.assertIn('id="view-settings"', page)
        self.assertIn('id="settings-detail-slot"', page)
        self.assertIn('id="settings-chat-preferences"', page)
        self.assertIn('id="settings-tools-slot"', page)
        self.assertIn('data-view="overview"', page)
        self.assertIn('class="navbtn active" data-view="overview"', page)
        self.assertIn('class="view active" id="view-overview"', page)
        self.assertIn("currentView='overview'", page)
        self.assertIn('id="home-live"', page)
        self.assertIn('id="home-run-progress-wrap"', page)
        self.assertIn('function renderHomeStatus(s)', page)
        self.assertNotIn('data-view="tools"', page)
        self.assertNotIn('data-view="activity"', page)
        self.assertIn('id="enter-send-toggle"', page)
        self.assertIn("moveSettingsSections", page)
        self.assertIn("niji-enter-to-send", page)
        self.assertIn("niji-plan-first", page)
        self.assertIn("e.key===','", page)
        self.assertIn("Thinking on it", page)
        self.assertIn("Mapping it out", page)
        self.assertIn('id="plan-only"', page)
        self.assertIn('id="send"', page)
        self.assertIn("Stop ■", page)
        self.assertIn("setChatControls", page)
        self.assertIn("stop-send", page)
        self.assertIn("Send ↗", page)
        self.assertNotIn('id="stop-job"', page)
        self.assertNotIn('id="live-progress"', page)
        self.assertNotIn(".live-progress", page)
        self.assertIn("#settings-chat-preferences .composerfoot", page)
        self.assertNotIn(".settings-chat-preferences .composerfoot", page)
        self.assertNotIn('id="working-status"', page)
        self.assertNotIn('id="working-text"', page)
        self.assertIn("placeholder?.querySelector('.workmeta')", page)
        self.assertIn("className='workmeta'", page)
        self.assertIn("box.classList.add('working','live-status')", page)
        self.assertIn(".message.live-status .msglabel{display:none}", page)
        self.assertIn(".message.live-status .msgbody{display:none}", page)
        self.assertIn(".message.live-status:before,.message.live-status.has-stream:before{content:'';display:block", page)
        self.assertIn(".message.live-status .workmeta{display:block;width:100%", page)
        self.assertIn(".message.live-status .run-activity{display:none!important}", page)
        self.assertIn("activity.open=false", page)
        self.assertIn("activity.className='run-activity'", page)
        self.assertIn("Activity · ${visible.length}", page)
        self.assertIn("root.dataset.signature===signature", page)
        self.assertIn("white-space:normal;overflow-wrap:anywhere", page)
        self.assertIn(".message.live-status .workdetail,.message.live-status .run-elapsed", page)
        self.assertIn("function summarizeLiveTask(value)", page)
        self.assertIn("addWorkingBubble(planOnly)", page)
        self.assertIn("const detailAction=detail.replace", page)
        self.assertIn("function visibleJobEvents(events)", page)
        self.assertIn("Listing workspace files…", page)
        self.assertIn("function currentJobActivityLabel(job)", page)
        self.assertIn("const task=currentJobActivityLabel(job)||s.progress?.label||job?.progress||''", page)
        self.assertIn("const liveAction=currentJobActivityLabel(j);if(liveAction)setLiveTaskLabel(placeholder,liveAction)", page)
        self.assertNotIn("setLiveTaskLabel(placeholder,activePlanItem.activeForm||activePlanItem.content)", page)
        self.assertIn("setLiveTaskLabel(placeholder,'Reconnecting…')", page)
        self.assertIn("root.dataset?.signature===signature", page)
        self.assertNotIn("meta.setAttribute('aria-label','Current action: '+currentAction)", page)
        self.assertIn("color:var(--text);font-size:14px", page)
        self.assertIn("@keyframes statuspulse", page)
        self.assertIn("prefers-reduced-motion:reduce", page)
        self.assertNotIn("Thinking…", page)
        self.assertIn("Running a command…", page)
        self.assertIn("still running", page)
        self.assertIn("duration=detail.match", page)
        self.assertIn("Planning…", page)
        self.assertIn("Checking your request and deciding what action is needed.", page)
        self.assertIn("Running the requested command.", page)
        self.assertIn("Still running · ${duration}s.", page)
        self.assertIn("workdetail", page)
        self.assertIn("Checking your request and deciding what action is needed.", page)
        self.assertIn("Running the requested command.", page)
        self.assertIn("Still running · ${duration}s.", page)
        self.assertIn("toolDescriptions", page)
        self.assertIn("Planning…", page)
        self.assertIn("Searching the web…", page)
        self.assertIn("Running tests…", page)
        self.assertIn("renderAssistantMarkdown(body,j.streamed||'')", page)
        self.assertIn("placeholder.classList.toggle('has-stream',!!j.streamed)", page)
        self.assertIn("pollFailures++", page)
        self.assertIn("Math.min(4000,250*Math.pow(2", page)
        self.assertIn("retrying its status check", page)
        self.assertIn("setTimeout(()=>watchJob(id,false,planOnly),1000)", page)
        self.assertIn(".message.live-status.has-stream .msgbody{display:block", page)
        self.assertNotIn("provider}/${model} · turn", page)
        self.assertNotIn("j.streamed||j.progress_detail", page)
        self.assertIn('id="file-changes"', page)
        self.assertIn('id="connector-settings"', page)
        self.assertIn('id="connector-api-key"', page)
        self.assertIn('id="add-connector"', page)
        self.assertIn("renderJobEvents(j.events,placeholder)", page)
        self.assertIn("className='execution-steps'", page)
        self.assertIn("function renderJobPlan(items", page)
        self.assertIn("function buildTaskPlanList(items)", page)
        self.assertIn("task-plan-deps", page)
        self.assertIn("Waiting for: ", page)
        self.assertIn("function openPlanEditor(actions,run,box,sourceJobId,items,setItems)", page)
        self.assertIn("function canMovePlanStep(rows,from,to)", page)
        self.assertIn("depends_on:[...x.depends_on]", page)
        self.assertIn("aria-describedby',hint.id", page)
        self.assertIn("removing a step also removes it from prerequisite lists", page)
        self.assertIn("function renderSavedPlan(items", page)
        self.assertIn("Approve & run plan", page)
        self.assertIn("/approve-plan", page)
        self.assertIn("/edit-plan", page)
        self.assertIn("function attachPlanAction(box,sourceJobId,items=[])", page)
        self.assertIn("choose prerequisites", page)
        self.assertIn("+ Add step", page)
        self.assertIn("Step ${index+1} depends on", page)
        self.assertNotIn("Execute the approved numbered plan above", page)
        self.assertIn('id="auto-compact-toggle"', page)
        self.assertIn('id="compaction-threshold"', page)
        self.assertIn("413 emergency recovery is still enabled", page)
        self.assertIn("Ask every time", page)

    def test_browser_model_picker_and_thread_pinning_controls_are_present(self):
        page = urllib.request.urlopen(self.ui.url, timeout=3).read().decode()
        for marker in ('id="model-provider"', 'id="model-catalog"', 'id="model-manual"',
                       'id="fetch-models"', 'id="apply-model"', 'pin-thread',
                       'async function fetchModelCatalog', 'async function applyModel',
                       'async function togglePinnedSession', 'PINNED'):
            self.assertIn(marker, page)

    def test_browser_run_history_and_manual_retry_controls_are_present(self):
        page = urllib.request.urlopen(self.ui.url, timeout=3).read().decode()
        for marker in ('data-view="history"', 'id="view-history"', 'id="run-history-list"',
                       'id="run-detail"', 'async function loadRunHistory',
                       'async function loadRunDetail', 'async function retrySelectedRun',
                       "Previous actions may run again", "never replayed automatically",
                       "Partial output:", "Origin context"):
            self.assertIn(marker, page)

    def test_browser_automation_manager_controls_are_present(self):
        page = urllib.request.urlopen(self.ui.url, timeout=3).read().decode()
        for marker in ('data-view="automations"', 'id="view-automations"',
                       'id="automation-form"', 'id="automation-run-at"',
                       'id="automation-plan-only"', 'async function loadAutomations',
                       'function renderAutomations', 'async function automationAction'):
            self.assertIn(marker, page)
        self.assertIn("local Niji workspace", page)
        self.assertIn("Plan-only by default", page)

    def test_automation_creation_persists_private_file_and_can_pause_delete(self):
        from datetime import datetime, timedelta, timezone
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            with (patch("niji.webui.CONFIG_DIR", config),
                  patch("niji.webui._AUTOMATION_FILE", config / "automations.json")):
                run_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
                result = json.loads(self.request("/api/automations", {
                    "action": "create", "name": "Daily review", "prompt": "Review the latest changes",
                    "run_at": run_at, "mode": "interval", "interval_minutes": 1440,
                    "plan_only": True,
                }, self.ui.token).read())
                item = result["automations"][0]
                self.assertEqual(item["name"], "Daily review")
                self.assertTrue(item["enabled"])
                self.assertTrue(item["plan_only"])
                saved = config / "automations.json"
                self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
                result = json.loads(self.request("/api/automations", {
                    "action": "toggle", "id": item["id"], "enabled": False,
                }, self.ui.token).read())
                self.assertFalse(result["automations"][0]["enabled"])
                result = json.loads(self.request("/api/automations", {
                    "action": "delete", "id": item["id"],
                }, self.ui.token).read())
                self.assertEqual(result["automations"], [])

    def test_automation_rejects_invalid_schedule_and_task(self):
        from datetime import datetime, timedelta, timezone
        with tempfile.TemporaryDirectory() as tmp, \
             patch("niji.webui._AUTOMATION_FILE", Path(tmp) / "automations.json"), \
             patch("niji.webui.CONFIG_DIR", Path(tmp)):
            invalid = [
                {"action": "create", "name": "x", "prompt": "task", "run_at": "soon", "mode": "once", "plan_only": True},
                {"action": "create", "name": "x", "prompt": "task", "run_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(), "mode": "interval", "interval_minutes": 2, "plan_only": True},
            ]
            for body in invalid:
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    self.request("/api/automations", body, self.ui.token)
                self.assertEqual(failure.exception.code, 400)

    def test_due_automation_runs_as_job_and_records_completion(self):
        from datetime import datetime, timedelta, timezone
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            file = config / "automations.json"
            with (patch("niji.webui.CONFIG_DIR", config),
                  patch("niji.webui._AUTOMATION_FILE", file)):
                run_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
                created = json.loads(self.request("/api/automations", {
                    "action": "create", "name": "Scheduled check", "prompt": "Review staged changes",
                    "run_at": run_at, "mode": "once", "plan_only": True,
                }, self.ui.token).read())["automations"][0]
                stored = json.loads(file.read_text())
                stored[0]["next_run"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
                file.write_text(json.dumps(stored))
                self.assertTrue(self.ui._dispatch_due_automations())
                self.ui._job_thread.join(timeout=3)
                result = json.loads(self.request("/api/automations", token=self.ui.token).read())
                item = next(item for item in result["automations"] if item["id"] == created["id"])
                self.assertEqual(item["last_status"], "completed")
                self.assertFalse(item["enabled"])
                self.assertTrue(self.agent.last_plan_only)
                self.assertIn("Review staged changes", [m["content"] for m in self.agent.messages])

    def test_browser_files_results_view_controls_are_present(self):
        page = urllib.request.urlopen(self.ui.url, timeout=3).read().decode()
        for marker in ('data-view="files"', 'id="view-files"', 'id="artifact-list"',
                       'id="artifact-refresh"', 'async function loadArtifacts',
                       'async function downloadArtifact', 'Preview diff'):
            self.assertIn(marker, page)

    def test_artifact_list_and_download_are_workspace_scoped(self):
        old_cwd = Path.cwd()
        self.addCleanup(os.chdir, old_cwd)
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            os.chdir(root)
            artifact = root / "report.txt"
            artifact.write_text("generated result\n")
            external = Path(outside) / "secret.txt"
            external.write_text("outside workspace\n")
            link = root / "external-link.txt"
            link.symlink_to(external)
            self.agent.file_change_history = [
                {"path": str(artifact), "before": b"", "operation": "write"},
                {"path": str(external), "before": b"", "operation": "write"},
                {"path": str(link), "before": b"", "operation": "write"},
            ]
            listed = json.loads(self.request("/api/artifacts", token=self.ui.token).read())["artifacts"]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["path"], "report.txt")
            response = self.request("/api/artifacts/0", token=self.ui.token)
            self.assertEqual(response.read(), b"generated result\n")
            self.assertIn("attachment", response.headers.get("Content-Disposition", ""))
            with self.assertRaises(urllib.error.HTTPError) as unsafe:
                self.request("/api/artifacts/1", token=self.ui.token)
            self.assertEqual(unsafe.exception.code, 400)

    def test_generated_zip_artifacts_get_safe_download_links_and_reject_outside_paths(self):
        old_cwd = Path.cwd()
        self.addCleanup(os.chdir, old_cwd)
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(workspace_dir).resolve()
            output = root / "bundle.zip"
            output.write_bytes(b"zip-data")
            external = Path(outside_dir) / "secret.zip"
            external.write_bytes(b"outside")
            self.agent.workspace = root
            self.agent.file_change_history = []
            self.agent.generated_artifacts = [
                {"id": "a" * 16, "path": str(output), "operation": "create_zip"},
                {"id": "b" * 16, "path": str(external), "operation": "create_zip"},
            ]
            os.chdir(root)
            listed = json.loads(self.request("/api/artifacts", token=self.ui.token).read())["artifacts"]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["index"], "g" + "a" * 16)
            self.assertEqual(listed[0]["path"], "bundle.zip")
            self.assertFalse(listed[0]["diffable"])
            response = self.request("/api/artifacts/g" + "a" * 16, token=self.ui.token)
            self.assertEqual(response.read(), b"zip-data")
            with self.assertRaises(urllib.error.HTTPError) as unsafe:
                self.request("/api/artifacts/g" + "b" * 16, token=self.ui.token)
            self.assertEqual(unsafe.exception.code, 400)

    def test_workspace_state_and_artifacts_follow_agent_workspace_not_process_cwd(self):
        old_cwd = Path.cwd()
        self.addCleanup(os.chdir, old_cwd)
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as cwd_dir:
            workspace = Path(workspace_dir).resolve()
            process_cwd = Path(cwd_dir).resolve()
            (workspace / "AGENTS.md").write_text("workspace instructions")
            artifact = workspace / "result.txt"
            artifact.write_text("from active workspace")
            decoy = process_cwd / "decoy.txt"
            decoy.write_text("not active")
            self.agent.workspace = workspace
            self.agent.file_change_history = [
                {"path": str(artifact), "operation": "write"},
                {"path": str(decoy), "operation": "write"},
            ]
            os.chdir(process_cwd)
            state = json.loads(self.request("/api/state", token=self.ui.token).read())
            self.assertEqual(state["runtime"]["workspace_path"], str(workspace))
            self.assertTrue(state["runtime"]["project_guidance"])
            listed = json.loads(self.request("/api/artifacts", token=self.ui.token).read())["artifacts"]
            self.assertEqual([(item["name"], item["path"]) for item in listed],
                             [("result.txt", "result.txt")])
            response = self.request(f"/api/artifacts/{listed[0]['index']}", token=self.ui.token)
            self.assertEqual(response.read(), b"from active workspace")

    def test_model_state_does_not_expose_credentials(self):
        with (patch("niji.webui.load_config", return_value={"api_keys": {"test": "private-test-secret"}}),
              patch("niji.webui.provider_names", return_value=["test", "openai"]),
              patch("niji.webui.provider_is_configured", side_effect=lambda name, cfg=None: name == "test")):
            state = json.loads(self.request("/api/models", token=self.ui.token).read())
        self.assertEqual(state["provider"], "test")
        self.assertEqual(state["model"], "demo-model")
        self.assertTrue(next(p for p in state["providers"] if p["name"] == "test")["configured"])
        self.assertNotIn("private-test-secret", json.dumps(state))

    def test_keyless_provider_sentinel_is_not_redacted_as_a_secret(self):
        self.agent.provider_cfg = {"provider": "ollama", "api_key": "ollama"}
        visible = self.ui._redact_visible({"provider": "ollama"})
        self.assertEqual(visible["provider"], "ollama")

    def test_provider_add_tests_builtin_endpoint_before_saving_and_never_returns_key(self):
        secret = "provider-secret-never-return-this"
        with (patch("niji.webui.load_config", return_value={}),
              patch("niji.setup_wizard.test_connection", return_value=(True, "ok")) as test,
              patch("niji.webui.save_config") as save):
            result = json.loads(self.request("/api/models", {
                "action": "add-provider", "provider": "openai", "api_key": secret,
                "model": "gpt-test-model",
            }, self.ui.token).read())
        self.assertEqual(result["provider"], "openai")
        self.assertNotIn(secret, json.dumps(result))
        test.assert_called_once_with({"provider": "openai", "base_url": "https://api.openai.com/v1",
                                      "api_key": secret, "model": "gpt-test-model"})
        saved = save.call_args.args[0]
        self.assertEqual(saved["api_keys"]["openai"], secret)
        self.assertEqual(saved["models"]["openai"], "gpt-test-model")

    def test_provider_add_rejects_invalid_custom_endpoint_and_fields(self):
        with patch("niji.webui.save_config") as save:
            for payload in (
                {"action": "add-provider", "provider": "custom", "api_key": "secret", "model": "m"},
                {"action": "add-provider", "provider": "bad/name", "base_url": "https://api.example.com/v1", "api_key": "secret", "model": "m"},
                {"action": "add-provider", "provider": "openai", "api_key": "secret", "model": ""},
                {"action": "add-provider", "provider": "xai", "base_url": "http://api.x.ai/v1", "api_key": "secret", "model": "m"},
                {"action": "add-provider", "provider": "xai", "base_url": "https://user:pass@api.x.ai/v1", "api_key": "secret", "model": "m"},
                {"action": "add-provider", "provider": "openai", "base_url": "https://custom.example/v1", "api_key": "secret", "model": "m"},
            ):
                with self.subTest(payload=payload), self.assertRaises(urllib.error.HTTPError) as invalid:
                    self.request("/api/models", payload, self.ui.token)
                self.assertEqual(invalid.exception.code, 400)
        save.assert_not_called()

    def test_provider_add_accepts_verified_custom_compatible_endpoint_and_keeps_key_private(self):
        secret = "xai-secret-never-return-this"
        payload = {"action": "add-provider", "provider": "xai",
                   "base_url": "https://api.x.ai/v1/", "api_key": secret,
                   "model": "grok-test-model"}
        with (patch("niji.webui.load_config", return_value={}),
              patch("niji.setup_wizard.test_connection", return_value=(True, "ok")) as test,
              patch("niji.webui.save_config") as save):
            result = json.loads(self.request("/api/models", payload, self.ui.token).read())
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "xai")
        self.assertNotIn(secret, json.dumps(result))
        test.assert_called_once_with({"provider": "xai", "base_url": "https://api.x.ai/v1",
                                      "api_key": secret, "model": "grok-test-model"})
        stored = save.call_args.args[0]["custom_providers"]["xai"]
        self.assertEqual(stored, {"base_url": "https://api.x.ai/v1",
                                  "model": "grok-test-model", "api_key": secret})

    def test_saved_keyless_custom_endpoint_is_configured(self):
        from niji.model_catalog import provider_is_configured
        config = {"custom_providers": {"local_gateway": {
            "base_url": "http://localhost:1234/v1", "model": "local-model"}}}
        self.assertTrue(provider_is_configured("local_gateway", config))

    def test_provider_add_fails_closed_and_local_ollama_needs_no_key(self):
        with (patch("niji.webui.load_config", return_value={}),
              patch("niji.setup_wizard.test_connection", return_value=(False, "secret details")),
              patch("niji.webui.save_config") as save):
            with self.assertRaises(urllib.error.HTTPError) as failed:
                self.request("/api/models", {"action": "add-provider", "provider": "openai",
                    "api_key": "secret", "model": "m"}, self.ui.token)
            self.assertEqual(failed.exception.code, 400)
            self.assertNotIn("secret details", failed.exception.read().decode())
        save.assert_not_called()
        with (patch("niji.webui.load_config", return_value={}),
              patch("niji.setup_wizard.test_connection", return_value=(True, "ok")),
              patch("niji.webui.save_config") as save_local):
            result = json.loads(self.request("/api/models", {"action": "add-provider", "provider": "ollama",
                "model": "qwen2.5"}, self.ui.token).read())
        self.assertTrue(result["ok"])
        self.assertNotIn("ollama", save_local.call_args.args[0].get("api_keys", {}))

    def test_provider_state_lists_presets_and_exposes_saved_keyless_ollama(self):
        secret = "provider-secret-never-return-this"
        cfg = {"api_keys": {"openai": secret}, "models": {"ollama": "qwen2.5"}}
        with patch("niji.webui.load_config", return_value=cfg):
            state = json.loads(self.request("/api/models", token=self.ui.token).read())
        self.assertTrue(any(item["name"] == "openai" for item in state["available_providers"]))
        ollama = next(item for item in state["providers"] if item["name"] == "ollama")
        self.assertTrue(ollama["configured"])
        self.assertTrue(ollama["removable"])
        self.assertNotIn(secret, json.dumps(state))

    def test_provider_settings_removal_rejects_active_and_clears_saved_key_and_model(self):
        cfg = {"api_keys": {"openai": "saved-secret"},
               "models": {"openai": "old-model", "anthropic": "keep-model"}}
        with patch("niji.webui.load_config", return_value=cfg), patch("niji.webui.save_config") as save:
            with self.assertRaises(urllib.error.HTTPError) as active:
                self.request("/api/models", {"action": "remove-provider", "provider": "test"}, self.ui.token)
            self.assertEqual(active.exception.code, 409)
            result = json.loads(self.request("/api/models", {"action": "remove-provider", "provider": "openai"}, self.ui.token).read())
        self.assertTrue(result["ok"])
        self.assertNotIn("openai", cfg["api_keys"])
        self.assertNotIn("openai", cfg["models"])
        self.assertEqual(cfg["models"]["anthropic"], "keep-model")
        save.assert_called_once_with(cfg)

    def test_provider_removal_clears_keyless_ollama_and_environment_provider_models(self):
        cfg = {"models": {"ollama": "qwen2.5", "openai": "gpt-env"}}
        with patch("niji.webui.load_config", return_value=cfg), patch("niji.webui.save_config") as save:
            ollama = json.loads(self.request("/api/models", {
                "action": "remove-provider", "provider": "ollama",
            }, self.ui.token).read())
            self.assertTrue(ollama["ok"])
            self.assertNotIn("ollama", cfg["models"])
            openai = json.loads(self.request("/api/models", {
                "action": "remove-provider", "provider": "openai",
            }, self.ui.token).read())
        self.assertTrue(openai["ok"])
        self.assertNotIn("openai", cfg.get("models", {}))
        self.assertNotIn("models", cfg)
        self.assertEqual(save.call_count, 2)

    def test_simultaneous_provider_additions_are_serialized_without_lost_config(self):
        cfg = {}
        first_started = threading.Event()
        release_first = threading.Event()
        first_result = {}

        def delayed_test(_provider_cfg):
            first_started.set()
            self.assertTrue(release_first.wait(3))
            return True, "ok"

        def add_first():
            try:
                first_result["body"] = self.request("/api/models", {
                    "action": "add-provider", "provider": "openai",
                    "api_key": "openai-secret", "model": "gpt-test",
                }, self.ui.token).read()
            except Exception as exc:
                first_result["error"] = exc

        with (patch("niji.webui.load_config", return_value=cfg),
              patch("niji.setup_wizard.test_connection", side_effect=delayed_test)):
            worker = threading.Thread(target=add_first)
            worker.start()
            self.assertTrue(first_started.wait(2), "first provider test did not start")
            with self.assertRaises(urllib.error.HTTPError) as competing:
                self.request("/api/models", {
                    "action": "add-provider", "provider": "anthropic",
                    "api_key": "anthropic-secret", "model": "claude-test",
                }, self.ui.token)
            self.assertEqual(competing.exception.code, 409)
            release_first.set()
            worker.join(timeout=4)
            self.assertFalse(worker.is_alive())
            self.assertNotIn("error", first_result)
            self.assertTrue(json.loads(first_result["body"])["ok"])
            second = json.loads(self.request("/api/models", {
                "action": "add-provider", "provider": "anthropic",
                "api_key": "anthropic-secret", "model": "claude-test",
            }, self.ui.token).read())
        self.assertTrue(second["ok"])
        self.assertEqual(cfg["models"], {"openai": "gpt-test", "anthropic": "claude-test"})
        self.assertEqual(cfg["api_keys"], {"openai": "openai-secret", "anthropic": "anthropic-secret"})

    def test_provider_manager_ui_and_sent_prompt_anchor_are_available(self):
        self.assertIn('id="provider-manager"', PAGE)
        self.assertIn('id="provider-api-key" type="password"', PAGE)
        self.assertIn('id="provider-name" aria-label="Provider to connect"', PAGE)
        self.assertIn('id="custom-provider-name"', PAGE)
        self.assertIn('id="custom-provider-base-url"', PAGE)
        self.assertIn("Custom OpenAI-compatible endpoint…", PAGE)
        self.assertIn("function toggleCustomProviderFields()", PAGE)
        self.assertIn("action:'add-provider'", PAGE)
        self.assertIn("action:'remove-provider'", PAGE)
        self.assertIn("requestAnimationFrame(()=>anchorSentMessage(sentMessage))", PAGE)
        self.assertIn("function anchorSentMessage(message)", PAGE)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        start = PAGE.index("function anchorSentMessage(")
        end = PAGE.index("\nfunction summarizeLiveTask(", start)
        helper = PAGE[start:end]
        probe = helper + """
const target={getBoundingClientRect(){return {top:510}}};
const container={scrollTop:200,scrollTo(value){this.next=value.top},getBoundingClientRect(){return {top:300}}};
global.el=id=>id==='messages'?container:null;
anchorSentMessage(target);
if(container.next!==400)throw new Error('sent message not anchored near the top of the chat viewport: '+container.next);
"""
        subprocess.run(["node", "-e", probe], check=True, capture_output=True, text=True)

    def test_model_catalog_returns_models_for_configured_provider(self):
        with (patch("niji.webui.load_config", return_value={}),
              patch("niji.webui.provider_names", return_value=["demo"]),
              patch("niji.webui.provider_is_configured", return_value=True),
              patch("niji.webui.resolve_catalog_provider", return_value=({"provider": "demo"}, None)),
              patch("niji.webui.fetch_provider_models", return_value=(["demo-fast", "demo-pro"], ""))):
            result = json.loads(self.request("/api/models", {
                "action": "catalog", "provider": "demo"
            }, self.ui.token).read())
        self.assertEqual(result["models"], ["demo-fast", "demo-pro"])

    def test_saved_model_only_ollama_can_fetch_catalog_and_switch(self):
        cfg = {"models": {"ollama": "qwen2.5"}, "api_keys": {}, "custom_providers": {}}
        provider_cfg = {"provider": "ollama", "base_url": "http://localhost:11434/v1",
                        "api_key": "ollama", "model": "qwen2.5"}
        with (patch("niji.webui.load_config", return_value=cfg),
              patch("niji.model_catalog.load_config", return_value=cfg),
              patch("niji.webui.resolve_catalog_provider", return_value=(provider_cfg, None)),
              patch("niji.webui.fetch_provider_models", return_value=(["qwen2.5"], ""))):
            catalog = json.loads(self.request("/api/models", {
                "action": "catalog", "provider": "ollama",
            }, self.ui.token).read())
        self.assertEqual(catalog["models"], ["qwen2.5"])
        with (patch("niji.webui.load_config", return_value=cfg),
              patch("niji.model_catalog.load_config", return_value=cfg),
              patch("niji.webui.resolve_provider", return_value=dict(provider_cfg)),
              patch("niji.setup_wizard.test_connection", return_value=(True, "ok")),
              patch("niji.webui.save_config"),
              patch("openai.OpenAI", return_value=object())):
            switched = json.loads(self.request("/api/models", {
                "action": "switch", "provider": "ollama", "model": "qwen2.5",
            }, self.ui.token).read())
        self.assertTrue(switched["ok"])
        self.assertEqual(switched["provider"], "ollama")
        self.assertEqual(self.agent.provider_name, "ollama")

    def test_model_switch_requires_successful_chat_test_before_persisting(self):
        cfg = {"api_key": "key-for-test", "base_url": "https://example.invalid/v1",
               "provider": "demo", "model": "demo-pro"}
        with (patch("niji.webui.load_config", return_value={}),
              patch("niji.webui.provider_names", return_value=["demo"]),
              patch("niji.webui.provider_is_configured", return_value=True),
              patch("niji.webui.resolve_provider", return_value=dict(cfg)),
              patch("niji.setup_wizard.test_connection", return_value=(True, "ok")),
              patch("niji.webui.save_config") as save,
              patch("openai.OpenAI", return_value=object())):
            result = json.loads(self.request("/api/models", {
                "action": "switch", "provider": "demo", "model": "demo-pro"
            }, self.ui.token).read())
        self.assertTrue(result["ok"])
        self.assertEqual(self.agent.provider_name, "demo")
        self.assertEqual(self.agent.model, "demo-pro")
        save.assert_called_once()
        self.assertFalse(self.ui._model_mutating)

    def test_failed_model_chat_test_does_not_change_active_model_or_config(self):
        with (patch("niji.webui.load_config", return_value={}),
              patch("niji.webui.provider_names", return_value=["demo"]),
              patch("niji.webui.provider_is_configured", return_value=True),
              patch("niji.webui.resolve_provider", return_value={"api_key": "secret", "base_url": "https://example.invalid/v1", "model": "bad"}),
              patch("niji.setup_wizard.test_connection", return_value=(False, "secret response")),
              patch("niji.webui.save_config") as save):
            with self.assertRaises(urllib.error.HTTPError) as failed:
                self.request("/api/models", {"action": "switch", "provider": "demo", "model": "bad"}, self.ui.token)
        self.assertEqual(failed.exception.code, 400)
        self.assertEqual(self.agent.model, "demo-model")
        save.assert_not_called()
        self.assertFalse(self.ui._model_mutating)

    def test_pinned_threads_persist_and_appear_in_session_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions, config = root / "sessions", root / "config"
            sessions.mkdir(); config.mkdir()
            (sessions / "thread-1.json").write_text(json.dumps([
                {"role": "user", "content": "Review this release"},
                {"role": "assistant", "content": "I will review it."},
            ]))
            with (patch("niji.webui.SESSION_DIR", sessions),
                  patch("niji.webui.CONFIG_DIR", config),
                  patch("niji.webui._PIN_FILE", config / "pinned_sessions.json")):
                result = json.loads(self.request("/api/sessions/thread-1/pin", {
                    "pinned": True
                }, self.ui.token).read())
                self.assertTrue(result["sessions"][0]["pinned"])
                self.assertEqual(json.loads((config / "pinned_sessions.json").read_text()), ["thread-1"])
                self.assertEqual((config / "pinned_sessions.json").stat().st_mode & 0o777, 0o600)
                result = json.loads(self.request("/api/sessions/thread-1/pin", {
                    "pinned": False
                }, self.ui.token).read())
                self.assertFalse(result["sessions"][0]["pinned"])

    def test_pin_endpoint_rejects_unsaved_threads_and_bad_values(self):
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.request("/api/sessions/not-saved/pin", {"pinned": True}, self.ui.token)
        self.assertEqual(missing.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as invalid:
            self.request("/api/sessions/bad/pin", {"pinned": "yes"}, self.ui.token)
        self.assertEqual(invalid.exception.code, 400)

    def test_state_endpoint_requires_token_and_never_returns_api_key(self):
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.request("/api/state")
        self.assertEqual(missing.exception.code, 403)
        state = json.loads(self.request("/api/state", token=self.ui.token).read())
        self.assertEqual(state["model"], "demo-model")
        self.assertEqual(state["tools"][0]["name"], "read_file")
        self.assertEqual(state["tools"][0]["access"], "Read-only")
        self.assertIn("workspace", state["runtime"])
        self.assertIn("transcript", state)
        self.assertNotIn("private-test-secret", json.dumps(state))
        self.assertNotIn("private system instructions", json.dumps(state))

    def test_chat_runs_as_background_job_and_returns_response(self):
        started = json.loads(self.request("/api/chat", {"message": "hello"}, self.ui.token).read())
        deadline = time.time() + 3
        job = None
        while time.time() < deadline:
            job = json.loads(self.request("/api/jobs/" + started["id"], token=self.ui.token).read())
            if job["status"] != "running":
                break
            time.sleep(0.03)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["response"], "Hello from Niji: hello")
        self.assertEqual(job["streamed"], "Hello from Niji: hello")
        self.assertTrue(job["plan_only"] is False)

    def test_run_history_is_durable_redacted_and_requires_explicit_retry(self):
        started = json.loads(self.request(
            "/api/chat", {"message": "show private-test-secret safely"}, self.ui.token).read())
        completed = self.wait_for_job_status(started["id"], "completed")
        history = json.loads(self.request("/api/jobs", token=self.ui.token).read())
        self.assertEqual(history["jobs"][0]["id"], started["id"])
        self.assertEqual(history["jobs"][0]["status"], "completed")
        self.assertNotIn("private-test-secret", json.dumps(history))
        record_path = self.ui._run_store.directory / f"{started['id']}.json"
        persisted_text = record_path.read_text(encoding="utf-8")
        self.assertNotIn("private-test-secret", persisted_text)
        self.assertIn("[redacted]", persisted_text)
        detail = json.loads(self.request(f"/api/jobs/{started['id']}", token=self.ui.token).read())
        self.assertEqual(detail["status"], "completed")
        self.assertNotIn("private-test-secret", json.dumps(detail))
        # The server—not only the browser—requires acknowledgement before repeating side effects.
        with self.assertRaises(urllib.error.HTTPError) as repeat_not_confirmed:
            self.request(f"/api/jobs/{started['id']}/retry", {}, self.ui.token)
        self.assertEqual(repeat_not_confirmed.exception.code, 409)
        self.assertTrue(json.loads(repeat_not_confirmed.exception.read())["repeat_confirmation_required"])
        retried = json.loads(self.request(
            f"/api/jobs/{started['id']}/retry", {"confirm_repeat": True}, self.ui.token).read())
        self.assertEqual(retried["source_job_id"], started["id"])
        second = self.wait_for_job_status(retried["id"], "completed")
        self.assertNotIn("private-test-secret", second["original_message"])

    def test_restart_marks_runs_interrupted_without_automatic_replay(self):
        self.ui._run_store.save({"id": "crash-run", "status": "running",
                                 "original_message": "perform a safe task",
                                 "created": time.time(), "events": []})
        restored_agent = FakeAgent()
        restored = NijiWebUI(restored_agent, port=0)
        restored_thread = threading.Thread(target=restored.httpd.serve_forever, daemon=True)
        restored_thread.start()
        try:
            self.assertFalse(restored._busy)
            self.assertIsNone(restored._active_job)
            self.assertEqual(restored_agent.usage["turns"], 0)
            detail = json.loads(urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{restored.httpd.server_port}/api/jobs/crash-run",
                    headers={"X-Niji-Token": restored.token}), timeout=3).read())
            self.assertEqual(detail["status"], "interrupted")
            self.assertIn("No action was replayed", detail["progress_detail"])
            history = restored._job_history()
            self.assertIn("crash-run", [item["id"] for item in history["jobs"]])
        finally:
            restored.close()
            restored_thread.join(timeout=2)

    def test_failed_run_history_preserves_partial_output_and_error(self):
        def partial_then_fail(message):
            self.agent.stream_callback("partial result before failure")
            raise RuntimeError("provider interrupted")
        with patch.object(self.agent, "chat", side_effect=partial_then_fail):
            status, started = self.ui._start_job("collect partial result")
        self.assertEqual(status, 202)
        job = self.wait_for_job_status(started["id"], "error")
        self.assertEqual(job["streamed"], "partial result before failure")
        self.assertIn("provider interrupted", job["error"])
        persisted = json.loads((self.ui._run_store.directory / f"{started['id']}.json").read_text())
        self.assertEqual(persisted["streamed"], "partial result before failure")
        self.assertIn("provider interrupted", persisted["error"])

    def test_retry_requires_explicit_confirmation_when_session_changed(self):
        started = json.loads(self.request(
            "/api/chat", {"message": "safe context retry"}, self.ui.token).read())
        self.wait_for_job_status(started["id"], "completed")
        self.agent.session_id = "another-thread"
        with self.assertRaises(urllib.error.HTTPError) as needs_confirmation:
            self.request(f"/api/jobs/{started['id']}/retry", {}, self.ui.token)
        self.assertEqual(needs_confirmation.exception.code, 409)
        challenge = json.loads(needs_confirmation.exception.read())
        self.assertTrue(challenge["context_confirmation_required"])
        # A context switch invalidates the first token; approval must bind to the new target.
        self.agent.session_id = "third-thread"
        with self.assertRaises(urllib.error.HTTPError) as stale_confirmation:
            self.request(f"/api/jobs/{started['id']}/retry", {
                "confirm_repeat": True,
                "context_confirmation_token": challenge["context_confirmation_token"],
            }, self.ui.token)
        fresh = json.loads(stale_confirmation.exception.read())
        self.assertEqual(fresh["context_summary"]["target_session_id"], "third-thread")
        self.assertIsNone(self.ui._active_job)
        # The old token remains revoked even if its original target context returns.
        self.agent.session_id = "another-thread"
        with self.assertRaises(urllib.error.HTTPError) as replayed_old:
            self.request(f"/api/jobs/{started['id']}/retry", {
                "confirm_repeat": True,
                "context_confirmation_token": challenge["context_confirmation_token"],
            }, self.ui.token)
        replacement = json.loads(replayed_old.exception.read())
        self.assertEqual(replacement["context_summary"]["target_session_id"], "another-thread")
        retried = json.loads(self.request(
            f"/api/jobs/{started['id']}/retry", {
                "confirm_repeat": True,
                "context_confirmation_token": replacement["context_confirmation_token"],
            }, self.ui.token).read())
        self.assertEqual(retried["source_job_id"], started["id"])
        self.assertEqual(self.wait_for_job_status(retried["id"], "completed")["session_id"], "another-thread")

    def test_retry_with_missing_origin_metadata_requires_exact_context_confirmation(self):
        run_id = "legacy-context"
        with self.ui._lock:
            self.ui._jobs[run_id] = {
                "id": run_id, "status": "completed", "original_message": "inspect safely",
                "workspace_path": "/private-test-secret/project", "plan_only": False,
            }
        with self.assertRaises(urllib.error.HTTPError) as needs_confirmation:
            self.request(f"/api/jobs/{run_id}/retry", {"confirm_repeat": True}, self.ui.token)
        self.assertEqual(needs_confirmation.exception.code, 409)
        challenge = json.loads(needs_confirmation.exception.read())
        summary = challenge["context_summary"]
        self.assertEqual(summary["source_session_id"], "unknown")
        self.assertEqual(summary["source_workspace"], "/[redacted]/project")
        self.assertNotIn("private-test-secret", json.dumps(challenge))
        self.assertIsNone(self.ui._active_job)
        body = {"confirm_repeat": True,
                "context_confirmation_token": challenge["context_confirmation_token"]}
        retried = json.loads(self.request(f"/api/jobs/{run_id}/retry", body, self.ui.token).read())
        self.assertEqual(retried["source_job_id"], run_id)
        self.assertEqual(self.wait_for_job_status(retried["id"], "completed")["original_message"],
                         "inspect safely")

    def test_run_cache_evicts_oldest_timestamp_not_newest_restored_entry(self):
        with self.ui._lock:
            self.ui._jobs.clear()
            # Startup loads newest-first; a newly finished run is newest of all.
            for index in reversed(range(100)):
                run_id = f"restored-{index}"
                self.ui._jobs[run_id] = {"id": run_id, "status": "completed",
                                         "created": index, "updated": index}
            self.ui._jobs["new-run"] = {"id": "new-run", "status": "completed",
                                         "created": 100, "updated": 100}
            self.ui._prune_job_cache_locked()
        self.assertEqual(len(self.ui._jobs), 100)
        self.assertIn("new-run", self.ui._jobs)
        self.assertIn("restored-99", self.ui._jobs)
        self.assertNotIn("restored-0", self.ui._jobs)

    def test_mcp_secrets_are_redacted_from_streams_jobs_errors_and_exports(self):
        secret = "stdio-output-secret"
        self.agent.mcp_clients = [type("MCP", (), {"_secrets": [secret]})()]

        def chat_with_secret(message):
            self.agent.stream_callback("streamed stdio-output-")
            self.agent.stream_callback("secret")
            self.agent.messages.append({"role": "assistant", "content": "transcript " + secret})
            return "final " + secret

        with patch.object(self.agent, "chat", side_effect=chat_with_secret):
            status, started = self.ui._start_job("show result")
        self.assertEqual(status, 202)
        job = self.wait_for_job_status(started["id"], "completed")
        self.assertEqual(job["streamed"], "streamed [redacted]")
        self.assertEqual(job["response"], "final [redacted]")
        persisted = (self.ui._run_store.directory / f"{started['id']}.json").read_text(encoding="utf-8")
        self.assertNotIn(secret, persisted)
        # Removing a connector must not make credentials already observed by this UI visible again.
        self.agent.mcp_clients = []
        state = json.loads(self.request("/api/state", token=self.ui.token).read())
        serialized_state = json.dumps(state)
        self.assertNotIn(secret, serialized_state)
        exported = self.ui._export_session(self.agent.session_id)
        self.assertTrue(any("transcript [redacted]" in item["content"] for item in exported))
        self.assertNotIn(secret, json.dumps(exported))

        with patch.object(self.agent, "chat", side_effect=RuntimeError("connector failed: " + secret)):
            status, failed = self.ui._start_job("cause safe failure")
        self.assertEqual(status, 202)
        failure_job = self.wait_for_job_status(failed["id"], "error")
        self.assertNotIn(secret, failure_job["error"])

    def test_job_execution_timeline_is_request_scoped_and_redacts_secrets(self):
        started = json.loads(self.request("/api/chat", {"message": "show progress"}, self.ui.token).read())
        deadline = time.time() + 3
        job = None
        while time.time() < deadline:
            job = json.loads(self.request("/api/jobs/" + started["id"], token=self.ui.token).read())
            if job["status"] != "running":
                break
            time.sleep(0.03)
        self.assertTrue(job["events"])
        self.assertEqual(job["events"][0]["level"], "THINKING")
        self.assertEqual(job["events"][0]["message"], "Thinking through the next step")
        self.assertNotIn("private-test-secret", json.dumps(job["events"]))
        self.assertNotIn("demo-model", json.dumps(job["events"]))

    def test_settings_persist_automatic_compaction_preferences(self):
        with (patch("niji.webui.load_config", return_value={}),
              patch("niji.webui.save_config") as save):
            state = json.loads(self.request("/api/settings", {
                "auto_compact": False, "compaction_threshold": 40000
            }, self.ui.token).read())
        self.assertFalse(self.agent.auto_compact)
        self.assertEqual(self.agent.compaction_threshold, 40000)
        self.assertFalse(state["auto_compact"])
        self.assertEqual(state["compaction_threshold"], 40000)
        saved = save.call_args.args[0]
        self.assertEqual(saved, {"auto_compact": False, "compaction_threshold": 40000})
        with self.assertRaises(urllib.error.HTTPError) as invalid:
            self.request("/api/settings", {"compaction_threshold": 2000}, self.ui.token).read()
        self.assertEqual(invalid.exception.code, 400)

    def test_live_tool_activity_and_elapsed_time_are_exposed_to_ui(self):
        job_id = "live-progress-case"
        self.ui._jobs[job_id] = {
            "id": job_id, "status": "running", "response": "", "error": "",
            "streamed": "", "progress": "Preparing the next step",
            "progress_detail": "Preparing the model request", "activity": None,
            "plan_only": False, "original_message": "run tests",
            "cancel_requested": False,
        }
        self.ui._active_job = job_id
        self.ui._busy = True
        self.ui._record_activity({
            "time": "12:03:00", "level": "TOOL",
            "message": "Tool call: run_tests · Running project tests",
        })
        started = json.loads(self.request(
            "/api/jobs/" + job_id, token=self.ui.token).read())
        self.assertEqual(started["progress"], "Using a tool")
        self.assertIn("Tool call: run_tests", started["progress_detail"])
        self.ui._record_activity({
            "time": "12:03:09", "level": "TOOL_PROGRESS",
            "message": "Tool call: run_tests · still running (9s)",
        })
        progress = json.loads(self.request(
            "/api/jobs/" + job_id, token=self.ui.token).read())
        self.assertEqual(progress["activity"]["level"], "TOOL_PROGRESS")
        self.assertIn("still running (9s)", progress["progress_detail"])
        self.ui._active_job = None
        self.ui._busy = False

    def test_cancelled_provider_exception_is_reported_as_cancelled(self):
        job_id = "cancelled-error-case"
        self.ui._jobs[job_id] = {
            "id": job_id, "status": "running", "response": "", "error": "",
            "streamed": "partial output", "cancel_requested": True,
        }
        self.ui._active_job = job_id
        self.ui._busy = True
        def raise_after_cancel(message):
            raise RuntimeError("provider stream closed during cancellation")
        self.agent.chat = raise_after_cancel
        self.ui._run_job(job_id, "stop this")
        job = self.ui._jobs[job_id]
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["response"], "partial output")
        self.assertEqual(job["error"], "")

    def test_unfinished_secret_prefix_is_not_exposed_in_live_state(self):
        entered = threading.Event()
        release = threading.Event()
        def emit_partial_secret(message):
            self.agent.stream_callback("visible text private-test-")
            entered.set()
            release.wait(2)
            return "done"
        with patch.object(self.agent, "chat", side_effect=emit_partial_secret):
            status, started = self.ui._start_job("wait for stream redaction")
            self.assertEqual(status, 202)
            self.assertTrue(entered.wait(2))
            state = json.loads(self.request("/api/state", token=self.ui.token).read())
            serialized = json.dumps(state)
            self.assertNotIn("private-test-", serialized)
            self.assertNotIn("_stream_redaction_pending", serialized)
            self.assertIn("visible text", state["active_job"]["streamed"])
            release.set()
            completed = self.wait_for_job_status(started["id"], "completed")
            self.assertEqual(completed["response"], "done")
            self.assertEqual(completed["streamed"], "visible text [redacted]")

    def test_streamed_text_is_available_before_completion_and_stop_is_cooperative(self):
        self.agent.pause_stream = True
        started = json.loads(self.request("/api/chat", {"message": "slow response"}, self.ui.token).read())
        self.assertTrue(self.agent.stream_ready.wait(2))
        job = json.loads(self.request("/api/jobs/" + started["id"], token=self.ui.token).read())
        self.assertEqual(job["status"], "running")
        self.assertTrue(job["streamed"])
        self.assertEqual(job["progress"], "Writing the response")
        response = self.request("/api/jobs/" + started["id"] + "/cancel", {}, self.ui.token)
        self.assertEqual(response.status, 202)
        deadline = time.time() + 3
        while time.time() < deadline:
            job = json.loads(self.request("/api/jobs/" + started["id"], token=self.ui.token).read())
            if job["status"] != "running":
                break
            time.sleep(0.03)
        self.assertEqual(job["status"], "cancelled")

    def test_pause_resume_keeps_job_busy_and_resumes_after_safe_boundary(self):
        self.agent.pause_stream = True
        started = json.loads(self.request("/api/chat", {"message": "slow task"}, self.ui.token).read())
        job_id = started["id"]
        self.assertTrue(self.agent.stream_ready.wait(2))
        paused_request = self.request(f"/api/jobs/{job_id}/pause", {}, self.ui.token)
        self.assertEqual(paused_request.status, 202)
        self.assertEqual(json.loads(paused_request.read())["status"], "pause_requested")
        self.agent.finish_stream.set()
        job = self.wait_for_job_status(job_id, "paused")
        self.assertEqual(job["progress"], "Paused safely")
        state = json.loads(self.request("/api/state", token=self.ui.token).read())
        self.assertTrue(state["busy"])
        self.assertEqual(state["active_job"]["id"], job_id)
        self.assertEqual(state["active_job"]["status"], "paused")
        with self.assertRaises(urllib.error.HTTPError) as busy:
            self.request("/api/chat", {"message": "duplicate"}, self.ui.token)
        self.assertEqual(busy.exception.code, 409)
        resumed = self.request(f"/api/jobs/{job_id}/resume", {}, self.ui.token)
        self.assertEqual(resumed.status, 202)
        self.assertEqual(json.loads(resumed.read())["status"], "running")
        completed = self.wait_for_job_status(job_id, "completed")
        self.assertEqual(completed["response"], "Hello from Niji: slow task")
        self.assertEqual([event["level"] for event in completed["events"] if event["level"] in ("PAUSED", "RESUMED")],
                         ["PAUSED", "RESUMED"])

    def test_pause_requested_during_approval_holds_side_effect_until_resume(self):
        self.agent.require_approval = True
        started = json.loads(self.request("/api/chat", {"message": "approve carefully"}, self.ui.token).read())
        job_id = started["id"]
        deadline = time.time() + 2
        pending = None
        while time.time() < deadline:
            state = json.loads(self.request("/api/state", token=self.ui.token).read())
            if state["pending_approvals"]:
                pending = state["pending_approvals"][0]
                break
            time.sleep(0.02)
        self.assertIsNotNone(pending)
        self.request(f"/api/jobs/{job_id}/pause", {}, self.ui.token)
        approved = self.request(f"/api/approvals/{pending['id']}", {"approved": True}, self.ui.token)
        self.assertEqual(approved.status, 200)
        paused = self.wait_for_job_status(job_id, "paused")
        self.assertEqual(paused["response"], "")
        resumed = self.request(f"/api/jobs/{job_id}/resume", {}, self.ui.token)
        self.assertEqual(resumed.status, 202)
        complete = self.wait_for_job_status(job_id, "completed")
        self.assertEqual(complete["response"], "approved")

    def test_cancel_while_paused_wakes_job_and_prevents_resume(self):
        self.agent.pause_stream = True
        started = json.loads(self.request("/api/chat", {"message": "pause then stop"}, self.ui.token).read())
        job_id = started["id"]
        self.assertTrue(self.agent.stream_ready.wait(2))
        self.request(f"/api/jobs/{job_id}/pause", {}, self.ui.token)
        self.agent.finish_stream.set()
        self.wait_for_job_status(job_id, "paused")
        response = self.request(f"/api/jobs/{job_id}/cancel", {}, self.ui.token)
        self.assertEqual(response.status, 202)
        cancelled = self.wait_for_job_status(job_id, "cancelled")
        self.assertEqual(cancelled["response"], "[Stopped by user]")
        with self.assertRaises(urllib.error.HTTPError) as stale_resume:
            self.request(f"/api/jobs/{job_id}/resume", {}, self.ui.token)
        self.assertEqual(stale_resume.exception.code, 409)

    def test_concurrent_chat_submissions_cannot_replace_a_paused_job(self):
        job_id = self.start_paused_stream_job("hold a single active run")
        barrier = threading.Barrier(3)
        statuses = []

        def submit(message):
            barrier.wait(timeout=2)
            try:
                response = self.request("/api/chat", {"message": message}, self.ui.token)
                statuses.append(response.status)
                response.read()
            except urllib.error.HTTPError as exc:
                statuses.append(exc.code)
                exc.read()

        workers = [threading.Thread(target=submit, args=(f"duplicate {i}",)) for i in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=2)
        for worker in workers:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sorted(statuses), [409, 409])
        state = json.loads(self.request("/api/state", token=self.ui.token).read())
        self.assertTrue(state["busy"])
        self.assertEqual(state["active_job"]["id"], job_id)
        self.assertEqual(state["active_job"]["status"], "paused")
        self.assertEqual(len(self.ui._jobs), 1)
        self.request(f"/api/jobs/{job_id}/cancel", {}, self.ui.token)
        self.wait_for_job_status(job_id, "cancelled")

    def test_concurrent_resume_and_cancel_finishes_cancelled_without_new_actions(self):
        resumed_boundary = threading.Event()
        release_after_resume = threading.Event()
        self.agent.after_pause_resume = lambda: (resumed_boundary.set(), release_after_resume.wait(3))
        job_id = self.start_paused_stream_job("race stop and resume")
        barrier = threading.Barrier(3)
        statuses = {}

        def post(name, suffix):
            barrier.wait(timeout=2)
            try:
                response = self.request(f"/api/jobs/{job_id}/{suffix}", {}, self.ui.token)
                statuses[name] = response.status
                response.read()
            except urllib.error.HTTPError as exc:
                statuses[name] = exc.code
                exc.read()

        workers = [threading.Thread(target=post, args=("resume", "resume")),
                   threading.Thread(target=post, args=("cancel", "cancel"))]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=2)
        for worker in workers:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(statuses.get("cancel"), 202)
        self.assertIn(statuses.get("resume"), (202, 409))
        if statuses.get("resume") == 202:
            self.assertTrue(resumed_boundary.wait(2))
        release_after_resume.set()
        cancelled = self.wait_for_job_status(job_id, "cancelled")
        self.assertEqual(cancelled["response"], "[Stopped by user]")
        self.assertTrue(self.agent.cancel_requested.is_set())

    def test_cancel_signal_is_serialized_before_next_job_generation(self):
        old_job_id = "old-generation"
        timeline = []

        class RecordedEvent:
            def __init__(self):
                self.event = threading.Event()
            def set(self):
                timeline.append("cancel-delivered")
                self.event.set()
            def clear(self):
                timeline.append("generation-started")
                self.event.clear()
            def is_set(self):
                return self.event.is_set()

        finalization_attempted = threading.Event()
        admission_attempted = threading.Event()
        allow_worker_lock = threading.Event()
        allow_admission_lock = threading.Event()
        original_lock = self.ui._lock

        class ObservedRLock:
            def __init__(self, inner):
                self.inner = inner
            def __enter__(self):
                name = threading.current_thread().name
                if name == "old-generation-worker":
                    finalization_attempted.set()
                    allow_worker_lock.wait(2)
                elif name == "next-generation-admission":
                    admission_attempted.set()
                    allow_admission_lock.wait(2)
                self.inner.acquire()
                return self
            def __exit__(self, *exc):
                self.inner.release()

        with original_lock:
            self.ui._jobs[old_job_id] = {
                "id": old_job_id, "status": "running", "cancel_requested": False,
                "response": "", "error": "", "streamed": "", "events": [],
                "automation_id": "", "plan_only": False,
            }
            self.ui._active_job = old_job_id
            self.ui._busy = True
        self.ui._lock = ObservedRLock(original_lock)
        self.agent._cancel_event = RecordedEvent()

        cancel_entered = threading.Event()
        allow_cancel = threading.Event()
        old_cancel = self.agent.cancel
        original_chat = self.agent.chat
        release_old_chat = threading.Event()
        chat_entered = threading.Event()
        old_run_finished = threading.Event()
        next_start_finished = threading.Event()
        cancel_result = {}
        next_result = {}

        def blocked_cancel():
            cancel_entered.set()
            allow_cancel.wait(2)
            old_cancel()

        def blocking_old_chat(message):
            chat_entered.set()
            release_old_chat.wait(2)
            return "old generation complete"

        def send_cancel():
            response = self.request(f"/api/jobs/{old_job_id}/cancel", {}, self.ui.token)
            cancel_result["status"] = response.status
            response.read()

        def run_old_generation():
            self.ui._run_job(old_job_id, "old request")
            old_run_finished.set()

        def try_next_generation():
            next_result["result"] = self.ui._start_job("fresh generation")
            next_start_finished.set()

        self.agent.cancel = blocked_cancel
        self.agent.chat = blocking_old_chat
        cancelling = threading.Thread(target=send_cancel, name="cancel-request")
        finishing = threading.Thread(target=run_old_generation, name="old-generation-worker")
        admitting = threading.Thread(target=try_next_generation, name="next-generation-admission")
        try:
            finishing.start()
            self.assertTrue(chat_entered.wait(2))
            cancelling.start()
            self.assertTrue(cancel_entered.wait(2))
            release_old_chat.set()
            self.assertTrue(finalization_attempted.wait(2))
            admitting.start()
            self.assertTrue(admission_attempted.wait(2))
            # First let the real completion path contend with cancel. In an unsafe
            # implementation it would finish and make the next generation admissible.
            allow_worker_lock.set()
            self.assertFalse(old_run_finished.wait(0.1))
            allow_admission_lock.set()
            self.assertFalse(next_start_finished.wait(0.1))
            allow_cancel.set()
            cancelling.join(2)
            finishing.join(2)
            admitting.join(2)
            self.assertFalse(cancelling.is_alive())
            self.assertFalse(finishing.is_alive())
            self.assertFalse(admitting.is_alive())
            self.assertEqual(cancel_result.get("status"), 202)
            self.assertTrue(old_run_finished.is_set())
            self.assertTrue(next_start_finished.is_set())

            status, started = next_result["result"]
            self.assertIn("cancel-delivered", timeline)
            if status == 202:
                self.assertLess(timeline.index("cancel-delivered"),
                                timeline.index("generation-started"))
                self.assertFalse(self.agent._cancel_event.is_set())
            else:
                self.assertTrue(self.agent._cancel_event.is_set())
                status, started = self.ui._start_job("fresh generation retry")
            self.assertEqual(status, 202)
            self.assertFalse(self.agent._cancel_event.is_set(),
                             "a new generation must clear only an already-delivered old signal")
            self.agent.chat = original_chat
            self.agent.cancel = old_cancel
            self.agent.cancel_requested.clear()
            self.wait_for_job_status(started["id"], "completed")
        finally:
            allow_worker_lock.set()
            allow_admission_lock.set()
            allow_cancel.set()
            release_old_chat.set()
            for worker in (cancelling, finishing, admitting):
                if worker.ident is not None:
                    worker.join(2)
            self.agent.chat = original_chat
            self.agent.cancel = old_cancel
            self.ui._lock = original_lock

    def test_cancel_while_approval_is_pending_releases_worker_and_denies_action(self):
        self.agent.require_approval = True
        started = json.loads(self.request("/api/chat", {"message": "wait for approval"}, self.ui.token).read())
        job_id = started["id"]
        deadline = time.time() + 2
        while time.time() < deadline:
            state = json.loads(self.request("/api/state", token=self.ui.token).read())
            if state["pending_approvals"]:
                break
            time.sleep(0.02)
        self.assertTrue(state["pending_approvals"])
        response = self.request(f"/api/jobs/{job_id}/cancel", {}, self.ui.token)
        self.assertEqual(response.status, 202)
        cancelled = self.wait_for_job_status(job_id, "cancelled")
        self.assertEqual(cancelled["response"], "[Stopped by user]")
        self.assertEqual(json.loads(self.request("/api/state", token=self.ui.token).read())["pending_approvals"], [])

    def test_approval_preview_redacts_provider_mcp_and_credential_field_values(self):
        class FakeMcpClient:
            _secrets = ["nango-secret-value", "Bearer nango-secret-value",
                        "provider-config-secret", "connection-id-secret", "xy"]

        from niji.mcp import MCPServer
        local_mcp = MCPServer("local", {"command": "fake-server", "env": {
            "GITHUB_TOKEN": "stdio-mcp-secret", "SAFE_LABEL": "ok"}})
        self.agent.mcp_clients = [FakeMcpClient(), local_mcp]
        result = []
        args = {
            "api_key": "unregistered-api-key-value",
            "key": "generic-key-secret",
            "token": "session-token-secret",
            "connection_id": "connection-id-secret",
            "nested": {"authorization": "Bearer nango-secret-value",
                       "bearer_token": "bearer-token-secret"},
            "description": "query contains provider-config-secret, private-test-secret, "
                           "stdio-mcp-secret, and standalone xy.",
        }
        worker = threading.Thread(target=lambda: result.append(
            self.ui._request_approval("connector_call", args)))
        worker.start()
        deadline = time.time() + 2
        state = {}
        while time.time() < deadline:
            state = json.loads(self.request("/api/state", token=self.ui.token).read())
            if state["pending_approvals"]:
                break
            time.sleep(0.02)
        self.assertTrue(state["pending_approvals"])
        preview = state["pending_approvals"][0]["preview"]
        for secret in (*FakeMcpClient._secrets, "private-test-secret", "unregistered-api-key-value",
                       "generic-key-secret", "session-token-secret", "bearer-token-secret",
                       "stdio-mcp-secret"):
            self.assertNotIn(secret, preview)
        self.assertIn("[redacted]", preview)
        self.assertEqual(local_mcp._secrets, ["stdio-mcp-secret"])
        approval_id = state["pending_approvals"][0]["id"]
        self.request(f"/api/approvals/{approval_id}", {"approved": False}, self.ui.token)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [False])

    def test_pause_resume_routes_reject_malformed_or_inactive_requests(self):
        with self.assertRaises(urllib.error.HTTPError) as malformed:
            self.request("/api/jobs/nope/pause", {"unexpected": True}, self.ui.token)
        self.assertEqual(malformed.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as inactive:
            self.request("/api/jobs/nope/resume", {}, self.ui.token)
        self.assertEqual(inactive.exception.code, 409)

    def test_settings_can_add_and_remove_nango_without_exposing_credentials(self):
        saved = {}

        class MockHttpMCP:
            def __init__(self, name, cfg):
                self.name, self.cfg = name, cfg
                self.tools = [{"name": "issue_search"}]
                self.stopped = False
            def start(self, timeout=20):
                self.timeout = timeout
            def stop(self):
                self.stopped = True

        def save(servers):
            saved.clear()
            saved.update(servers)

        with (patch("niji.webui.load_mcp_servers", side_effect=lambda: dict(saved)),
              patch("niji.webui.save_mcp_servers", side_effect=save),
              patch("niji.mcp.HttpMCPServer", MockHttpMCP)):
            result = json.loads(self.request("/api/connectors", {
                "action": "add_nango", "name": "nango_github",
                "api_key": "nango-private-api-key", "provider_config_key": "github-prod",
                "connection_id": "connection-private-id",
            }, self.ui.token).read())
            self.assertTrue(result["ok"])
            self.assertEqual(result["connectors"][0]["tools"], 1)
            self.assertTrue(result["connectors"][0]["connected"])
            self.assertNotIn("nango-private-api-key", json.dumps(result))
            self.assertNotIn("connection-private-id", json.dumps(result))
            state = json.loads(self.request("/api/state", token=self.ui.token).read())
            self.assertNotIn("nango-private-api-key", json.dumps(state))
            removed = json.loads(self.request("/api/connectors", {
                "action": "remove", "name": "nango_github"
            }, self.ui.token).read())
        self.assertTrue(removed["ok"])
        self.assertEqual(saved, {})
        self.assertEqual(self.agent.mcp_clients, [])

    def test_authenticated_image_upload_is_validated_and_consumed_once_by_chat(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/B7sAAAAASUVORK5CYII=")
        image = {"name": "diagram.png", "mime": "image/png",
                 "data": base64.b64encode(png).decode("ascii")}
        with self.assertRaises(urllib.error.HTTPError) as unauthorized:
            self.request("/api/uploads", image).read()
        self.assertEqual(unauthorized.exception.code, 403)
        saved_response = self.request("/api/uploads", image, self.ui.token)
        self.assertEqual(saved_response.status, 201)
        saved = json.loads(saved_response.read())
        self.assertEqual(saved["name"], "diagram.png")
        started = json.loads(self.request("/api/chat", {
            "message": "Describe this image", "image_ids": [saved["id"]]
        }, self.ui.token).read())
        job = self.wait_for_job_status(started["id"], "completed")
        self.assertEqual(self.agent.last_images[0]["name"], "diagram.png")
        self.assertEqual(self.agent.last_images[0]["mime"], "image/png")
        self.assertEqual(self.agent.last_images[0]["data"], png)
        self.assertEqual(self.agent.messages[-2]["content"], "Describe this image")
        self.assertNotIn(image["data"], json.dumps(job))
        self.assertEqual(self.ui._uploads, {})
        with self.assertRaises(urllib.error.HTTPError) as reused:
            self.request("/api/chat", {"message": "try again", "image_ids": [saved["id"]]},
                         self.ui.token).read()
        self.assertEqual(reused.exception.code, 400)

    def test_image_upload_rejects_spoofed_mime_secret_name_and_unknown_fields(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/B7sAAAAASUVORK5CYII=")
        encoded = base64.b64encode(png).decode("ascii")
        for data in (
            {"name": "fake.png", "mime": "image/png", "data": base64.b64encode(b"not an image").decode()},
            {"name": "secret-photo.png", "mime": "image/png", "data": encoded},
            {"name": "photo.png", "mime": "image/png", "data": encoded, "path": "/tmp/x"},
        ):
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                self.request("/api/uploads", data, self.ui.token).read()
            self.assertIn(rejected.exception.code, (400, 415))

    def test_chat_rejects_malformed_and_duplicate_image_ids(self):
        for ids in (["bad", "bad"], [1], ["x"] * 6):
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                self.request("/api/chat", {"message": "look", "image_ids": ids},
                             self.ui.token).read()
            self.assertEqual(rejected.exception.code, 400)

    def test_only_a_completed_unapproved_plan_only_run_restores_saved_plan_card(self):
        self.assertFalse(self.ui._state()["saved_plan_preview"])
        self.ui._jobs["plan-run"] = {
            "id": "plan-run", "session_id": self.agent.session_id,
            "status": "completed", "plan_only": True, "plan_approved": False,
            "created": 100,
        }
        self.assertTrue(self.ui._state()["saved_plan_preview"])
        self.ui._jobs["plan-run"].update(plan_approved=True)
        self.assertFalse(self.ui._state()["saved_plan_preview"])
        self.ui._jobs["plan-run"].update(plan_approved=False, status="error")
        self.assertFalse(self.ui._state()["saved_plan_preview"])

    def test_plan_only_does_not_execute_tools_and_is_visible_in_job(self):
        started = json.loads(self.request("/api/chat", {"message": "inspect project", "plan_only": True}, self.ui.token).read())
        deadline = time.time() + 3
        while time.time() < deadline:
            job = json.loads(self.request("/api/jobs/" + started["id"], token=self.ui.token).read())
            if job["status"] != "running":
                break
            time.sleep(0.03)
        self.assertEqual(job["status"], "completed")
        self.assertTrue(job["plan_only"])
        self.assertTrue(self.agent.last_plan_only)
        self.assertEqual([item["content"] for item in job["plan"]],
                         ["Inspect the project", "Run the tests"])
        self.assertIn("plan", json.loads(self.request("/api/state", token=self.ui.token).read()))
        self.assertIn("Inspect the project", job["response"])

    def _wait_for_job(self, job_id, timeout=3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = json.loads(self.request("/api/jobs/" + job_id, token=self.ui.token).read())
            if job["status"] != "running":
                return job
            time.sleep(0.03)
        self.fail("job did not finish before timeout")

    def test_plan_preview_can_be_edited_then_approved_exactly(self):
        preview = json.loads(self.request("/api/chat", {
            "message": "inspect project", "plan_only": True,
        }, self.ui.token).read())
        plan_job = self._wait_for_job(preview["id"])
        original = list(plan_job["plan"])
        steps = [
            {"id": "inspect", "content": "Inspect the source tree",
             "acceptance_criteria": "Only the active project tree is examined.", "depends_on": []}, 
            {"id": "tests", "content": "Run the full test suite", "depends_on": ["inspect"]},
            {"id": "review", "content": "Review the diff", "depends_on": ["tests"]},
        ]
        with patch("niji.webui.load_plan", return_value=original):
            edited = json.loads(self.request(
                f"/api/jobs/{preview['id']}/edit-plan", {"steps": steps}, self.ui.token).read())
        self.assertTrue(edited["ok"])
        self.assertEqual([item["content"] for item in edited["plan"]],
                         [item["content"] for item in steps])
        self.assertEqual(edited["plan"][0]["acceptance_criteria"],
                         "Only the active project tree is examined.")
        self.assertEqual(edited["plan"][1]["depends_on"], ["inspect"])
        self.assertEqual(edited["plan"][2]["depends_on"], ["tests"])
        self.assertTrue(all(item["status"] == "pending" for item in edited["plan"]))
        source = json.loads(self.request(f"/api/jobs/{preview['id']}", token=self.ui.token).read())
        self.assertEqual(source["plan"], edited["plan"])
        with patch("niji.webui.load_plan", return_value=edited["plan"]):
            submitted = json.loads(self.request(
                f"/api/jobs/{preview['id']}/approve-plan", {}, self.ui.token).read())
        execution = self._wait_for_job(submitted["id"])
        self.assertEqual(execution["status"], "completed")
        self.assertIn('"approved_steps":["Inspect the source tree","Run the full test suite","Review the diff"]',
                      execution["response"])
        self.assertIn('"id":"tests","content":"Run the full test suite","acceptance_criteria":"","depends_on":["inspect"]',
                      execution["response"])
        with patch("niji.webui.load_plan", return_value=edited["plan"]):
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                self.request(f"/api/jobs/{preview['id']}/edit-plan", {"steps": ["late edit"]}, self.ui.token)
        self.assertEqual(rejected.exception.code, 409)

    def test_plan_edits_persist_on_disk_and_are_the_plan_that_runs(self):
        from niji.planning import load_plan as persisted_load
        with tempfile.TemporaryDirectory() as tmp:
            with patch("niji.planning.save_plan",
                       side_effect=lambda sid, items: REAL_SAVE_PLAN(sid, items, root=tmp)), \
                 patch("niji.webui.load_plan",
                       side_effect=lambda sid: persisted_load(sid, root=tmp)), \
                 patch("niji.webui.save_plan",
                       side_effect=lambda sid, items: REAL_SAVE_PLAN(sid, items, root=tmp)):
                preview = json.loads(self.request("/api/chat", {
                    "message": "inspect project", "plan_only": True,
                }, self.ui.token).read())
                plan_job = self._wait_for_job(preview["id"])
                self.assertTrue(plan_job["plan"])
                edited = json.loads(self.request(
                    f"/api/jobs/{preview['id']}/edit-plan",
                    {"steps": ["Inspect only the active workspace", "Run its tests"]},
                    self.ui.token).read())
                self.assertEqual(persisted_load("test-session", root=tmp), edited["plan"])
                submitted = json.loads(self.request(
                    f"/api/jobs/{preview['id']}/approve-plan", {}, self.ui.token).read())
                execution = self._wait_for_job(submitted["id"])
                self.assertIn('"approved_steps":["Inspect only the active workspace","Run its tests"]',
                              execution["response"])

    def test_empty_new_preview_clears_stale_steps_and_can_be_repaired_manually(self):
        self.agent.todos = {"items": [{"content": "steps from an older request", "status": "pending"}]}
        with patch("niji.webui.extract_plan_steps", return_value=[]):
            preview = json.loads(self.request("/api/chat", {
                "message": "new request", "plan_only": True,
            }, self.ui.token).read())
            plan_job = self._wait_for_job(preview["id"])
        self.assertEqual(plan_job["plan"], [])
        self.assertEqual(self.agent.todos["items"], [])
        with patch("niji.webui.load_plan", return_value=[]):
            with self.assertRaises(urllib.error.HTTPError) as empty_approval:
                self.request(f"/api/jobs/{preview['id']}/approve-plan", {}, self.ui.token)
        self.assertEqual(empty_approval.exception.code, 409)
        with patch("niji.webui.load_plan", return_value=[]):
            edited = json.loads(self.request(f"/api/jobs/{preview['id']}/edit-plan",
                                             {"steps": ["Inspect the requested scope"]},
                                             self.ui.token).read())
        self.assertEqual(edited["plan"][0]["content"], "Inspect the requested scope")

    def test_plan_edit_rejects_malformed_empty_oversized_and_stale_updates(self):
        preview = json.loads(self.request("/api/chat", {
            "message": "inspect project", "plan_only": True,
        }, self.ui.token).read())
        plan_job = self._wait_for_job(preview["id"])
        path = f"/api/jobs/{preview['id']}/edit-plan"
        for payload in ({"steps": []}, {"steps": ["ok", 7]},
                        {"steps": ["step"] * 61},
                        {"steps": ["ok"], "status": "completed"}):
            with self.assertRaises(urllib.error.HTTPError) as bad:
                self.request(path, payload, self.ui.token)
            self.assertEqual(bad.exception.code, 400)
        invalid_graphs = [
            [{"id": "a", "content": "A", "depends_on": ["missing"]}],
            [{"id": "a", "content": "A", "depends_on": ["a"]}],
            [{"id": "a", "content": "A", "depends_on": ["b"]},
             {"id": "b", "content": "B", "depends_on": ["a"]}],
            [{"id": "first", "content": "First", "depends_on": ["later"]},
             {"id": "later", "content": "Later"}],
            [{"id": "a", "content": "A", "status": "completed"}],
        ]
        with patch("niji.webui.load_plan", return_value=plan_job["plan"]):
            for graph in invalid_graphs:
                with self.subTest(graph=graph), self.assertRaises(urllib.error.HTTPError) as invalid:
                    self.request(path, {"steps": graph}, self.ui.token)
                self.assertEqual(invalid.exception.code, 400)
        with patch("niji.webui.load_plan", return_value=[{"content": "stale plan"}]):
            with self.assertRaises(urllib.error.HTTPError) as stale:
                self.request(path, {"steps": ["replace stale plan"]}, self.ui.token)
        self.assertEqual(stale.exception.code, 409)
        with patch("niji.webui.load_plan", return_value=plan_job["plan"]):
            self.agent.session_id = "different-thread"
            with self.assertRaises(urllib.error.HTTPError) as wrong_thread:
                self.request(path, {"steps": ["cross-thread edit"]}, self.ui.token)
        self.assertEqual(wrong_thread.exception.code, 409)
        self.agent.session_id = "test-session"
        with patch("niji.webui.load_plan", return_value=plan_job["plan"]), \
             patch("niji.webui.save_plan", side_effect=OSError("disk full")):
            with self.assertRaises(urllib.error.HTTPError) as storage_error:
                self.request(path, {"steps": ["would not persist"]}, self.ui.token)
        self.assertEqual(storage_error.exception.code, 500)
        self.assertEqual(json.loads(self.request(
            f"/api/jobs/{preview['id']}", token=self.ui.token).read())["plan"], plan_job["plan"])

    def test_approved_plan_runs_from_unchanged_server_saved_plan_once(self):
        preview = json.loads(self.request("/api/chat", {
            "message": "inspect project", "plan_only": True,
        }, self.ui.token).read())
        plan_job = self._wait_for_job(preview["id"])
        self.assertEqual(plan_job["status"], "completed")
        saved_plan = list(plan_job["plan"])
        with patch("niji.webui.load_plan", return_value=saved_plan):
            submitted = json.loads(self.request(
                f"/api/jobs/{preview['id']}/approve-plan", {}, self.ui.token).read())
        self.assertTrue(submitted["ok"])
        run_job = self._wait_for_job(submitted["id"], timeout=5)
        self.assertEqual(run_job["status"], "completed")
        self.assertFalse(run_job["plan_only"])
        self.assertEqual(self.agent.approved_plan_seen, saved_plan)
        self.assertIsNone(self.agent.approved_plan)
        self.assertEqual([item["status"] for item in run_job["plan"]],
                         ["completed", "completed"])
        self.assertIn('"original_request":"inspect project"', run_job["response"])
        self.assertIn('"approved_steps":["Inspect the project","Run the tests"]', run_job["response"])
        updated_source = json.loads(self.request(
            f"/api/jobs/{preview['id']}", token=self.ui.token).read())
        self.assertTrue(updated_source["plan_approved"])
        with patch("niji.webui.load_plan", return_value=saved_plan):
            with self.assertRaises(urllib.error.HTTPError) as duplicate:
                self.request(f"/api/jobs/{preview['id']}/approve-plan", {}, self.ui.token)
        self.assertEqual(duplicate.exception.code, 409)

    def test_final_success_guard_revalidates_evidence_after_in_memory_mutation(self):
        preview = json.loads(self.request("/api/chat", {
            "message": "inspect project", "plan_only": True,
        }, self.ui.token).read())
        plan_job = self._wait_for_job(preview["id"])
        self.agent.corrupt_completion_evidence = True
        with patch("niji.webui.load_plan", return_value=plan_job["plan"]):
            submitted = json.loads(self.request(
                f"/api/jobs/{preview['id']}/approve-plan", {}, self.ui.token).read())
        result = self._wait_for_job(submitted["id"])
        self.assertEqual(result["status"], "error")
        self.assertIn("invalid completion evidence", result["error"])
        self.agent.corrupt_completion_evidence = False

    def test_approved_plan_cannot_report_success_when_steps_remain_incomplete(self):
        preview = json.loads(self.request("/api/chat", {
            "message": "inspect project", "plan_only": True,
        }, self.ui.token).read())
        plan_job = self._wait_for_job(preview["id"])
        self.agent.leave_plan_incomplete = True
        with patch("niji.webui.load_plan", return_value=plan_job["plan"]):
            submitted = json.loads(self.request(
                f"/api/jobs/{preview['id']}/approve-plan", {}, self.ui.token).read())
        result = self._wait_for_job(submitted["id"])
        self.assertEqual(result["status"], "error")
        self.assertIn("unfinished steps", result["error"])

    def test_plan_approval_rejects_stale_saved_plan_and_wrong_thread(self):
        preview = json.loads(self.request("/api/chat", {
            "message": "inspect project", "plan_only": True,
        }, self.ui.token).read())
        plan_job = self._wait_for_job(preview["id"])
        with patch("niji.webui.load_plan", return_value=[{"content": "changed after preview"}]):
            with self.assertRaises(urllib.error.HTTPError) as stale:
                self.request(f"/api/jobs/{preview['id']}/approve-plan", {}, self.ui.token)
        self.assertEqual(stale.exception.code, 409)
        self.assertFalse(json.loads(self.request(
            f"/api/jobs/{preview['id']}", token=self.ui.token).read())["plan_approved"])
        saved_plan = list(plan_job["plan"])
        self.agent.session_id = "another-thread"
        with patch("niji.webui.load_plan", return_value=saved_plan):
            with self.assertRaises(urllib.error.HTTPError) as wrong_thread:
                self.request(f"/api/jobs/{preview['id']}/approve-plan", {}, self.ui.token)
        self.assertEqual(wrong_thread.exception.code, 409)

    def test_session_tool_policy_can_be_changed(self):
        state = json.loads(self.request("/api/tool-policy", {"name": "read_file", "policy": "block"}, self.ui.token).read())
        self.assertEqual(state["tool_policies"]["read_file"], "block")
        with self.assertRaises(urllib.error.HTTPError) as invalid:
            self.request("/api/tool-policy", {"name": "not_a_tool", "policy": "allow"}, self.ui.token).read()
        self.assertEqual(invalid.exception.code, 400)

    def test_workspace_profiles_switch_folder_and_reload_project_guidance(self):
        old_cwd = Path.cwd()
        self.addCleanup(os.chdir, old_cwd)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = base / "project"
            workspace.mkdir()
            (workspace / "AGENTS.md").write_text("Use unittest for this project.")
            with patch("niji.webui.CONFIG_DIR", base / "config"), \
                 patch("niji.webui._PROFILE_FILE", base / "config" / "project_profiles.json"):
                saved = json.loads(self.request("/api/profiles", {
                    "action": "save", "name": "demo", "path": str(workspace)}, self.ui.token).read())
                self.assertEqual(saved["profiles"][0]["name"], "demo")
                active = json.loads(self.request("/api/profiles", {
                    "action": "activate", "name": "demo"}, self.ui.token).read())
                self.assertEqual(active["workspace"], str(workspace.resolve()))
                state = json.loads(self.request("/api/state", token=self.ui.token).read())
                self.assertTrue(state["runtime"]["project_guidance"])
                self.assertIn("Use unittest", self.agent.messages[0]["content"])
        os.chdir(old_cwd)

    def test_workspace_profile_and_artifact_paths_redact_known_secrets(self):
        secret = "private-test-secret"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / secret / "project"
            root.mkdir(parents=True)
            artifact = root / f"{secret}-notes.txt"
            artifact.write_text("private artifact content")
            self.agent.workspace = root
            self.agent.file_change_history = [{
                "path": str(artifact), "before": b"old\\n", "operation": "write",
            }]
            state = json.loads(self.request("/api/state", token=self.ui.token).read())
            self.assertNotIn(secret, json.dumps(state))
            with patch.object(self.ui, "_load_profiles", return_value=[
                    {"name": "profile", "path": str(root)}]):
                profiles = json.loads(self.request("/api/profiles", token=self.ui.token).read())
            self.assertNotIn(secret, json.dumps(profiles))
            artifacts_response = self.request("/api/artifacts", token=self.ui.token)
            artifacts = json.loads(artifacts_response.read())
            self.assertNotIn(secret, json.dumps(artifacts))
            download = self.request("/api/artifacts/0", token=self.ui.token)
            self.assertNotIn(secret, download.headers.get("Content-Disposition", ""))

    def test_file_diff_endpoint_returns_unified_diff_and_redacts_provider_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.txt"
            path.write_text("new private-test-secret value\n")
            self.agent.file_change_history = [{
                "path": str(path), "before": b"old value\n",
                "after_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "operation": "write",
            }]
            result = json.loads(self.request("/api/changes/0", token=self.ui.token).read())
            self.assertIn("-old value", result["diff"])
            self.assertIn("+new [redacted] value", result["diff"])
            self.assertNotIn("private-test-secret", result["diff"])

    def test_file_diff_redacts_rotated_provider_and_removed_mcp_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider_secret = "rotated-provider-secret"
            mcp_secret = "removed-mcp-secret"
            path = Path(tmp) / "notes.txt"
            path.write_text(f"provider={provider_secret} mcp={mcp_secret} short=xk\\n")
            self.agent.provider_cfg["api_key"] = provider_secret
            self.agent.mcp_clients = [type("MCP", (), {"_secrets": [mcp_secret, "xk"]})()]
            self.ui._known_secrets()
            self.agent.provider_cfg["api_key"] = "new-provider-secret"
            self.agent.mcp_clients = []
            self.agent.file_change_history = [{
                "path": str(path), "before": b"old values\\n",
                "after_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "operation": "write",
            }]
            diff = self.ui._change_diff(0)
            for secret in (provider_secret, mcp_secret, "xk"):
                self.assertNotIn(secret, diff)
            self.assertGreaterEqual(diff.count("[redacted]"), 3)

    def test_file_diff_endpoint_rejects_symlink_and_bad_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.txt"
            target.write_text("content")
            link = Path(tmp) / "link.txt"
            link.symlink_to(target)
            self.agent.file_change_history = [{"path": str(link), "before": b"", "operation": "write"}]
            with self.assertRaises(urllib.error.HTTPError) as unsafe:
                self.request("/api/changes/0", token=self.ui.token).read()
            self.assertEqual(unsafe.exception.code, 400)
            with self.assertRaises(urllib.error.HTTPError) as invalid:
                self.request("/api/changes/abc", token=self.ui.token).read()
            self.assertEqual(invalid.exception.code, 400)

    def test_profile_storage_error_returns_json_not_dropped_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = base / "project"
            workspace.mkdir()
            blocked = base / "not-a-directory"
            blocked.write_text("block")
            with patch("niji.webui.CONFIG_DIR", blocked), \
                 patch("niji.webui._PROFILE_FILE", blocked / "profiles.json"):
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    self.request("/api/profiles", {
                        "action": "save", "name": "demo", "path": str(workspace)}, self.ui.token).read()
                self.assertEqual(failure.exception.code, 400)
                payload = json.loads(failure.exception.read())
                self.assertIn("Could not save workspace profiles", payload["error"])

    def test_browser_memory_controls_read_replace_and_clear_private_notes(self):
        with tempfile.TemporaryDirectory() as tmp, patch("niji.webui.MEMORY_FILE", Path(tmp) / "MEMORY.md"):
            initial = json.loads(self.request("/api/memory", token=self.ui.token).read())
            self.assertEqual(initial["content"], "")
            saved = self.request("/api/memory", {"action": "replace", "content": "Use pytest."}, self.ui.token).read()
            self.assertIn(b"Use pytest", saved)
            self.assertEqual((Path(tmp) / "MEMORY.md").read_text(), "Use pytest.")
            self.request("/api/memory", {"action": "clear", "content": ""}, self.ui.token).read()
            self.assertFalse((Path(tmp) / "MEMORY.md").exists())

    def test_agent_mutation_gate_blocks_jobs_and_state_mutation_endpoints(self):
        self.ui._jobs["gate-plan"] = {
            "id": "gate-plan", "status": "completed", "plan_only": True,
            "plan_approved": False, "plan": [], "session_id": self.agent.session_id,
        }
        with self.ui._agent_mutation("test state"):
            status, result = self.ui._start_job("must wait for state update")
            self.assertEqual(status, 409)
            self.assertIn("workspace operation", result["error"])
            self.assertEqual(self.ui._edit_plan("gate-plan", ["new plan step"])[0], 409)
            self.assertFalse(self.ui._dispatch_due_automations())
            for path, body in (
                ("/api/compact", {}),
                ("/api/undo", {}),
                ("/api/profiles", {"action": "delete", "name": "missing"}),
                ("/api/session/new", {}),
                ("/api/settings", {"approval": "auto"}),
                ("/api/tool-policy", {"name": "read_file", "policy": "block"}),
                ("/api/memory", {"action": "replace", "content": "memory update"}),
            ):
                with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as rejected:
                    self.request(path, body, self.ui.token)
                self.assertEqual(rejected.exception.code, 409)
        for flag in ("_connector_mutating", "_model_mutating"):
            with self.ui._lock:
                setattr(self.ui, flag, True)
            try:
                with self.subTest(flag=flag), self.assertRaises(urllib.error.HTTPError) as rejected:
                    self.request("/api/tool-policy", {"name": "read_file", "policy": "block"}, self.ui.token)
                self.assertEqual(rejected.exception.code, 409)
            finally:
                with self.ui._lock:
                    setattr(self.ui, flag, False)
        status, started = self.ui._start_job("starts after state update")
        self.assertEqual(status, 202)
        self.assertEqual(self.wait_for_job_status(started["id"], "completed")["status"], "completed")

    def test_session_switch_waits_for_model_change(self):
        with tempfile.TemporaryDirectory() as tmp, patch("niji.webui.SESSION_DIR", Path(tmp)):
            (Path(tmp) / "saved.json").write_text(json.dumps([
                {"role": "system", "content": "system"},
                {"role": "user", "content": "saved"},
            ]))
            with self.ui._lock:
                self.ui._model_mutating = True
            try:
                with self.assertRaises(RuntimeError):
                    self.ui._new_session()
                with self.assertRaises(RuntimeError):
                    self.ui._open_session("saved")
            finally:
                with self.ui._lock:
                    self.ui._model_mutating = False
        self.assertEqual(self.agent.session_id, "test-session")

    def test_browser_context_compaction_keeps_the_latest_request(self):
        self.agent.messages.extend([
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer " * 500},
            {"role": "user", "content": "current question"},
        ])
        result = json.loads(self.request("/api/compact", {}, self.ui.token).read())
        self.assertTrue(result["changed"])
        self.assertEqual(self.agent.messages[-1]["content"], "current question")
        self.assertLess(result["after"], result["before"])

    def test_current_session_export_omits_internal_messages(self):
        self.agent.messages.extend([
            {"role": "user", "content": "export this"},
            {"role": "assistant", "content": "visible reply"},
            {"role": "tool", "content": "private tool output"},
        ])
        data = json.loads(self.request("/api/sessions/test-session/export", token=self.ui.token).read())
        self.assertEqual([m["role"] for m in data["messages"]], ["user", "assistant"])
        self.assertNotIn("private system instructions", json.dumps(data))
        self.assertNotIn("private tool output", json.dumps(data))

    def test_tool_approval_is_delivered_to_the_browser(self):
        self.agent.require_approval = True
        started = json.loads(self.request("/api/chat", {"message": "write a note"}, self.ui.token).read())
        deadline = time.time() + 3
        state = None
        while time.time() < deadline:
            state = json.loads(self.request("/api/state", token=self.ui.token).read())
            if state["pending_approvals"]:
                break
            time.sleep(0.03)
        self.assertTrue(state["pending_approvals"])
        approval = state["pending_approvals"][0]
        self.request("/api/approvals/" + approval["id"], {"approved": True}, self.ui.token).read()
        deadline = time.time() + 3
        job = None
        while time.time() < deadline:
            job = json.loads(self.request("/api/jobs/" + started["id"], token=self.ui.token).read())
            if job["status"] != "running":
                break
            time.sleep(0.03)
        self.assertEqual(job["response"], "approved")

    def test_new_thread_resets_session_usage_and_transcript(self):
        self.agent.messages.extend([
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
        ])
        self.agent.usage["turns"] = 4
        state = json.loads(self.request("/api/session/new", {}, self.ui.token).read())
        self.assertNotEqual(state["session_id"], "test-session")
        self.assertEqual(state["usage"]["turns"], 0)
        self.assertEqual(state["transcript"], [])

    def test_saved_sessions_can_be_listed_and_opened(self):
        with tempfile.TemporaryDirectory() as tmp, patch("niji.webui.SESSION_DIR", Path(tmp)):
            session = Path(tmp) / "saved-session.json"
            session.write_text(json.dumps([
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "Find the latest release"},
                {"role": "assistant", "content": "Here is the release"},
            ]))
            listed = json.loads(self.request("/api/sessions", token=self.ui.token).read())
            self.assertEqual(listed["sessions"][0]["id"], "saved-session")
            self.assertEqual(listed["sessions"][0]["title"], "Find the latest release")
            state = json.loads(self.request("/api/sessions/saved-session", {}, self.ui.token).read())
            self.assertEqual(state["session_id"], "saved-session")
            self.assertEqual([m["role"] for m in state["transcript"]], ["user", "assistant"])

    def test_approval_mode_can_be_changed_for_the_ui_session(self):
        state = json.loads(self.request("/api/settings", {"approval": "auto"}, self.ui.token).read())
        self.assertEqual(state["approval"], "auto")
        with self.assertRaises(urllib.error.HTTPError) as invalid:
            self.request("/api/settings", {"approval": "unsafe"}, self.ui.token).read()
        self.assertEqual(invalid.exception.code, 400)

    def test_listener_refuses_network_binding(self):
        with self.assertRaises(ValueError):
            NijiWebUI(FakeAgent(), host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()
