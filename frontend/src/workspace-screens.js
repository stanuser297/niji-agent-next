const navigation = [
  { id: 'overview', label: 'Home', icon: '⌂' },
  { id: 'chat', label: 'Chat', icon: '◈' },
  { id: 'files', label: 'Files', icon: '▤' },
  { id: 'automations', label: 'Automations', icon: '◷' },
  { id: 'history', label: 'Run history', icon: '↻' },
  { id: 'settings', label: 'Settings', icon: '⚙' },
];

const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));
const dateLabel = (value) => {
  const raw = Number(value);
  const date = new Date(Number.isFinite(raw) ? (raw < 1_000_000_000_000 ? raw * 1000 : raw) : value);
  return Number.isNaN(date.valueOf()) ? 'Time unavailable' : new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium', timeStyle: 'short',
  }).format(date);
};
const shortTime = (value) => {
  const date = new Date(value || Date.now());
  return Number.isNaN(date.valueOf()) ? '' : new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(date);
};
const statusLabel = (value) => ({ queued: 'Starting', running: 'Thinking', completed: 'Complete', failed: 'Failed', cancelled: 'Stopped', timed_out: 'Timed out', cancelling: 'Stopping' }[value] || String(value || 'unknown').replaceAll('_', ' '));

function homeModelPicker(state) {
  const models = state.capabilities?.models || [];
  if (state.capabilities?.execution_mode !== 'prompt' || !models.length) return '';
  const selected = models.find((model) => model.id === state.selectedModel)?.id
    || models.find((model) => model.default)?.id || models[0].id;
  return `<label class="home-model-picker"><span>Model</span><select id="model-select" aria-label="Choose model">${models.map((model) => `<option value="${escapeHtml(model.id)}" ${model.id === selected ? 'selected' : ''}>${escapeHtml(model.name)}</option>`).join('')}</select></label>`;
}

function runTitle(run) {
  return run.result?.title || run.result?.summary || run.payload?.prompt?.slice(0, 60) || `Run ${String(run.run_id).slice(0, 8)}`;
}

function localPageHeader(eyebrow, title, description, action = '') {
  return `<div class="pagehead"><div><div class="eyebrow">${escapeHtml(eyebrow)}</div><h1>${escapeHtml(title)}</h1><p>${escapeHtml(description)}</p></div>${action}</div>`;
}

function ageSeconds(run) {
  const raw = run?.created_at || run?.createdAt || run?.updated_at || run?.updatedAt;
  const value = Number(raw);
  const stamp = Number.isFinite(value) ? (value < 1_000_000_000_000 ? value * 1000 : value) : Date.parse(raw || '');
  return Number.isFinite(stamp) ? Math.max(0, Math.floor((Date.now() - stamp) / 1000)) : 0;
}

function durationLabel(seconds) {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function liveStatusText(run, busy = false) {
  if (busy && !run) return 'Starting your request…';
  if (!run) return '';
  if (run.status === 'queued') {
    const age = ageSeconds(run);
    return age >= 30 ? `Still waiting to start · ${durationLabel(age)}` : 'Waiting for the agent…';
  }
  if (run.status === 'cancelling') return 'Stopping your request…';
  if (run.status !== 'running') return '';
  const phase = run.events?.at(-1)?.detail;
  return ({
    'Agent is thinking': 'Thinking through your request…',
    'Agent is planning': 'Planning the next step…',
    'Agent is working': 'Working on your task…',
    'Response ready': 'Writing the response…',
    'Run started': 'Working on your request…',
  })[phase] || 'Working on your request…';
}

function renderLiveStatus(run, { busy = false, showStop = false } = {}) {
  const label = liveStatusText(run, busy);
  if (!label) return '';
  const isActive = run ? run.status === 'running' : busy;
  const stateClass = run?.status === 'queued' ? 'is-queued' : run?.status === 'cancelling' ? 'is-stopping' : '';
  const classes = ['live-status-line', isActive && 'is-active', stateClass].filter(Boolean).join(' ');
  return `<div class="${classes}" role="status" aria-live="polite" aria-atomic="true"><span class="live-status-dot" aria-hidden="true"></span><span class="live-status-text" title="${escapeHtml(label)}">${escapeHtml(label)}</span>${showStop ? '<button class="text-button live-status-stop" id="cancel-run" type="button">Stop</button>' : ''}</div>`;
}

function statusPanel(state, run) {
  if (!run) return `<section class="home-live"><div class="home-live-top"><div><span class="eyebrow">LIVE STATUS</span><span class="status-tag ready">Ready</span></div><span class="status-note"><i class="status-dot ${state.health === 'API online' ? 'online' : ''}"></i>${escapeHtml(state.health || 'Checking API')}</span></div><h2>Ready for your next task</h2><p>Send a request here and follow Niji’s response as it arrives.</p><div class="home-live-foot"><span>Status updates refresh automatically.</span><button class="secondary" data-screen="chat">Open full chat →</button></div></section>`;
  const active = ['queued', 'running', 'cancelling'].includes(run.status);
  const age = ageSeconds(run);
  const notStartedLong = run.status === 'queued' && age >= 30;
  const detail = run.status === 'queued'
    ? (notStartedLong ? `Niji hasn't started this request yet. Please retry shortly.` : 'Message accepted. Niji is getting ready to reply.')
    : run.events?.at(-1)?.detail || (run.status === 'running' ? 'Niji is thinking…' : run.status === 'completed' ? 'Response ready.' : 'The run has finished.');
  const result = state.componentsResultText?.(run) || run.result?.text || run.result?.response || '';
  return `<section class="home-live ${active ? 'is-running' : ''} ${notStartedLong ? 'is-delayed' : ''}" aria-live="polite"><div class="home-live-top"><div><span class="eyebrow">LIVE STATUS</span><span class="status-tag ${escapeHtml(run.status)}">${run.status === 'running' ? '● ' : run.status === 'queued' ? '◷ ' : ''}${escapeHtml(statusLabel(run.status))}</span></div><span class="status-note"><i class="status-dot ${run.status === 'running' ? 'running' : run.status === 'queued' ? 'queued' : run.status}"></i>${run.status === 'queued' ? (notStartedLong ? 'Not started yet' : 'Getting ready') : run.status === 'running' ? 'Niji is thinking' : run.status === 'completed' ? 'Response ready' : escapeHtml(statusLabel(run.status))}</span></div><h2>${escapeHtml(runTitle(run))}</h2><p class="live-detail">${escapeHtml(detail)}</p>${result && run.status === 'completed' ? `<div class="home-answer">${escapeHtml(String(result).slice(0, 420))}</div>` : ''}<div class="home-live-foot"><span>${run.status === 'queued' ? `Accepted · ${durationLabel(age)}` : `Updated ${escapeHtml(shortTime(run.updated_at))}`}</span><div class="live-actions"><button class="secondary" data-run="${escapeHtml(run.run_id)}">Open run →</button>${active ? `<button class="text-button" data-cancel-run="${escapeHtml(run.run_id)}">Stop</button>` : ''}</div></div></section>`;
}

function homeScreen(state, components) {
  const activeRuns = state.runs.filter((run) => ['queued', 'running', 'cancelling'].includes(run.status));
  const currentRun = state.activeRun && ['queued', 'running', 'cancelling'].includes(state.activeRun.status) ? state.activeRun : activeRuns[0] || state.activeRun || state.runs[0];
  const active = activeRuns.length;
  const completed = state.runs.filter((run) => run.status === 'completed').length;
  const failed = state.runs.filter((run) => ['failed', 'timed_out'].includes(run.status)).length;
  return `<section class="view active" id="view-overview">
    ${localPageHeader('Niji workspace', 'Good to see you', 'Start a chat and get Niji’s response right here.', '<button class="secondary" data-screen="history">Run history →</button>')}
    ${statusPanel(state, currentRun)}
    <form class="home-compose panel" id="home-form"><label for="home-prompt">What can Niji help with?</label><div class="home-compose-row"><textarea id="home-prompt" maxlength="100000" placeholder="Ask a question, describe a task, or start with a quick prompt…" aria-label="Ask Niji" ${state.busy ? 'disabled' : ''}></textarea><button class="primary" id="home-submit" type="submit" ${state.busy ? 'disabled' : ''}>${state.busy ? 'Sending…' : 'Send to Niji ↗'}</button></div><small>Your reply will appear here as soon as it’s ready.</small></form>
    ${state.error && state.screen === 'overview' ? `<div class="notice error-note" role="alert">${escapeHtml(state.error)}</div>` : ''}
    <div class="stats"><article class="statcard"><div class="statlabel">Recent runs</div><div class="statnum">${state.runs.length}</div><div class="statmeta">Latest cloud activity</div></article><article class="statcard"><div class="statlabel">In progress</div><div class="statnum">${active}</div><div class="statmeta">Queued or running</div></article><article class="statcard"><div class="statlabel">Completed</div><div class="statnum">${completed}</div><div class="statmeta">Recent successful runs</div></article><article class="statcard"><div class="statlabel">Needs attention</div><div class="statnum">${failed}</div><div class="statmeta">Failed or timed out</div></article></div>
    <div class="overviewgrid"><section class="panel"><div class="panelhead"><h3>Quick start</h3><span class="subtle">One click to begin</span></div><div class="quickgrid"><button class="quick" data-prompt="Help me research this topic and summarize key findings."><b>Research a topic</b><span>Gather and organize useful information</span></button><button class="quick" data-prompt="Help me review this project and identify the next improvements."><b>Review a project</b><span>Get a focused project review</span></button><button class="quick" data-prompt="Help me plan a solution to this problem: "><b>Plan a solution</b><span>Break a problem into clear steps</span></button><button class="quick" data-prompt="Help me draft and refine this: "><b>Draft something</b><span>Start with a short description</span></button></div></section><section class="panel"><div class="panelhead"><h3>Cloud workspace</h3><span class="subtle">Current mode</span></div><div class="metric"><span>API</span><b>${escapeHtml(state.health || 'Checking')}</b></div><div class="metric"><span>Execution</span><b>${escapeHtml(state.capabilities?.execution_mode || 'Checking')}</b></div><div class="metric"><span>Tools</span><b>${(state.capabilities?.sandbox_tools || []).length ? escapeHtml((state.capabilities.sandbox_tools || []).join(', ')) : 'Prompt only'}</b></div><div class="metric"><span>Run status</span><b>${activeRuns.some((run) => run.status === 'queued' && ageSeconds(run) >= 30) ? 'Still starting' : 'Up to date'}</b></div></section></div>
  </section>`;
}

function homeChatScreen(state, components) {
  const run = state.activeRun;
  const terminal = run && ['completed', 'failed', 'cancelled', 'timed_out'].includes(run.status);
  const prompt = state.lastPrompt || run?.ui_prompt || run?.payload?.prompt || '';
  const title = prompt.replace(/\s+/g, ' ').trim().slice(0, 80) || 'New chat';
  const result = run ? components.resultText(run) : '';
  const conversation = prompt
    ? `<article class="message user"><div class="msglabel">You</div><div class="msgbody">${escapeHtml(prompt)}</div></article>`
    : `<div class="welcome"><div class="welcomeicon">✧</div><h2>What can I help with?</h2><p>Send a message and I’ll reply here.</p><div class="prompts"><button class="promptchip" data-prompt="Hi!">Say hi</button><button class="promptchip" data-prompt="Explain a topic simply: ">Explain something</button><button class="promptchip" data-prompt="Help me plan: ">Help me plan</button></div></div>`;
  let terminalMessage = '';
  if (run?.status === 'completed') terminalMessage = result || 'Niji finished but did not return a reply.';
  else if (run?.status === 'failed' || run?.status === 'timed_out') terminalMessage = run.error || 'Niji could not complete that reply.';
  else if (run?.status === 'cancelled') terminalMessage = 'This request was stopped.';
  const assistant = (run || state.busy) ? `<article class="message assistant ${terminal ? '' : 'live-status'}">${terminal ? `<div class="msglabel">Niji <span class="status-tag ${escapeHtml(run.status)}">${escapeHtml(statusLabel(run.status))}</span></div><div class="msgbody">${escapeHtml(terminalMessage)}</div>` : renderLiveStatus(run, { busy: state.busy, showStop: Boolean(run) && run.status !== 'cancelling' })}${run?.status === 'failed' || run?.status === 'timed_out' ? `<button class="secondary retry-message" data-retry-run="${escapeHtml(run.run_id)}" type="button">Try again</button>` : ''}</article>` : '';
  return `<section class="view active home-chat-view" id="view-overview"><header class="home-chat-header"><div class="home-chat-title"><div class="eyebrow">Niji Agent</div><h1 title="${escapeHtml(title)}">${escapeHtml(title)}</h1></div><div class="home-chat-actions"><button class="secondary" id="header-new-chat" type="button">＋ New chat</button><button class="secondary" data-screen="history" type="button">History</button></div></header><div class="home-messages messages" id="messages">${conversation}${assistant}${state.error ? `<div class="notice error-note" role="alert">${escapeHtml(state.error)}</div>` : ''}</div><div class="home-composer composer"><form id="form"><div class="composerbox"><textarea id="prompt" maxlength="100000" placeholder="Message Niji…" aria-label="Message Niji" ${state.busy ? 'disabled' : ''}></textarea><div class="composer-toolbar"><div class="composer-left"><span class="route-label"><i class="status-dot ${state.health === 'API online' ? 'online' : ''}"></i>${state.health === 'API online' ? 'Niji is online' : 'Connecting…'}</span>${homeModelPicker(state)}</div><div class="composer-right"><button class="sendbtn" id="submit" type="submit" ${state.busy ? 'disabled' : ''}>${state.busy ? 'Sending…' : 'Send ↗'}</button></div></div></div><div class="composerfoot"><span>Enter to send · Shift+Enter for a new line</span></div></form></div></section>`;
}

function chatScreen(state, components) {
  const run = state.activeRun;
  const terminal = run && ['completed', 'failed', 'cancelled', 'timed_out'].includes(run.status);
  const messageMarkup = state.lastPrompt ? `<article class="message user"><div class="msglabel">You</div><div class="msgbody">${escapeHtml(state.lastPrompt)}</div></article>` : `<div class="welcome"><div class="welcomeicon">✧</div><h2>Ready when you are.</h2><p>Ask Niji to research, help plan, or work with the project context this cloud deployment supports.</p><div class="prompts"><button class="promptchip" data-prompt="Research this and give me a concise summary: ">Research a topic</button><button class="promptchip" data-prompt="Help me plan a solution to: ">Plan a solution</button><button class="promptchip" data-prompt="Review this project context and suggest next steps.">Review project context</button></div></div>`;
  const result = run ? components.resultText(run) : '';
  const chatStatus = state.busy || (run && !terminal) ? '' : `<span class="live" id="chat-status">${run ? escapeHtml(statusLabel(run.status)) : 'Ready'}</span>`;
  const terminalMessage = run?.status === 'completed' ? (result || 'Niji finished but did not return a reply.')
    : run?.status === 'cancelled' ? 'This request was stopped.'
      : run?.status === 'failed' || run?.status === 'timed_out' ? (run.error || 'Niji could not complete that reply.') : '';
  const status = (run || state.busy) ? `<article class="message assistant ${terminal ? '' : 'live-status'}">${terminal ? `<div class="msglabel">Niji <span class="status-tag ${escapeHtml(run.status)}">${escapeHtml(statusLabel(run.status))}</span></div><div class="msgbody">${escapeHtml(terminalMessage)}</div>` : renderLiveStatus(run, { busy: state.busy, showStop: Boolean(run) && run.status !== 'cancelling' })}${run?.error ? `<div class="notice">${escapeHtml(run.error)}</div>` : ''}${terminal && run.status === 'completed' ? `<button class="secondary" id="show-run-files">View generated files</button>` : ''}</article>` : '';
  return `<section class="view active" id="view-chat"><div class="chatlayout"><div class="chatcard panel"><div class="chathead"><h2>◈ Chat</h2>${chatStatus}</div><div class="messages" id="messages">${messageMarkup}${status}</div><div class="composer"><div class="approval" id="approval-inline"></div><form id="form"><div class="composerbox"><textarea id="prompt" maxlength="100000" placeholder="Ask Niji anything…" aria-label="Message" ${state.busy ? 'disabled' : ''}></textarea>${components.renderProjectSourcePanel()}<div class="composer-toolbar"><div class="composer-left"><button class="composer-action" id="attach-button" type="button" title="Attach project files" ${state.capabilities?.features?.text_file_upload ? '' : 'disabled'}>＋</button><button class="composer-action" id="github-button" type="button" title="Import public GitHub context" ${state.capabilities?.features?.public_pinned_github_import ? '' : 'disabled'}>⌘</button></div><div class="composer-right"><span class="reasoning-lock" title="Model selection is managed by the cloud deployment">Cloud model</span><button class="sendbtn ${run && !terminal ? 'stop-send' : ''}" id="submit" type="submit" ${state.busy ? 'disabled' : ''}>${state.busy ? 'Sending…' : run && !terminal ? 'Stop ■' : 'Send ↗'}</button></div></div></div><div id="attachments" class="attachments"></div><div class="composerfoot"><span>Enter to send · Shift+Enter for a new line</span><span>Cloud run · ${escapeHtml(state.capabilities?.execution_mode || 'checking')}</span></div></form>${state.error ? `<div class="notice error-note" role="alert">${escapeHtml(state.error)}</div>` : ''}</div></div><aside class="details"><section class="panel"><div class="panelhead"><h3>Session details</h3><span class="subtle">Cloud</span></div><div class="metric"><span>Provider</span><b>Managed by deployment</b></div><div class="metric"><span>Model</span><b>Server configured</b></div><div class="metric"><span>Recent runs</span><b>${state.runs.length}</b></div><div class="metric"><span>Execution</span><b>${escapeHtml(state.capabilities?.execution_mode || 'checking')}</b></div><div class="metric"><span>Tools</span><b>${(state.capabilities?.sandbox_tools || []).length || 'None'}</b></div></section><section class="panel"><div class="panelhead"><h3>Available tools</h3><button class="text-button" data-settings-section="tools">View</button></div><div class="tools">${(state.capabilities?.sandbox_tools || []).length ? state.capabilities.sandbox_tools.map((tool) => `<span class="tool">${escapeHtml(tool)}</span>`).join('') : '<span class="subtle">Prompt-only · no tools enabled</span>'}</div></section><section class="panel"><div class="panelhead"><h3>Recent activity</h3><button class="text-button" data-screen="history">History</button></div><ul class="activity">${run?.events?.length ? run.events.slice(-6).reverse().map((event) => `<li><time>${escapeHtml(shortTime(event.timestamp * 1000))}</time>${escapeHtml(event.detail || statusLabel(event.status))}</li>`).join('') : '<li>No cloud run activity yet.</li>'}</ul></section></aside></div></section>`;
}

function historyScreen(state, components) {
  const filter = state.historyFilter || 'all';
  const query = (state.historyQuery || '').trim().toLocaleLowerCase();
  const runs = state.runs.filter((run) => (filter === 'all' || run.status === filter) && (!query || `${runTitle(run)} ${run.run_id} ${run.status}`.toLocaleLowerCase().includes(query)));
  const filters = ['all', 'queued', 'running', 'failed', 'completed', 'cancelled', 'timed_out'];
  return `<section class="view active" id="view-history">${localPageHeader('Cloud run journal', 'Run history', 'Review recent tasks and their current lifecycle state.', '<button class="secondary" id="refresh-history" type="button">↻ Refresh</button>')}<div class="notice">Only the most recent 20 cloud runs are currently loaded. Persistent local sessions, retry and saved plans are not available yet.</div><div class="filters"><input id="history-search" type="search" value="${escapeHtml(state.historyQuery || '')}" placeholder="Search recent runs…" aria-label="Search recent runs">${filters.map((item) => `<button class="filterbtn run-filter ${filter === item ? 'active' : ''}" data-filter="${item}">${item === 'all' ? 'All' : escapeHtml(statusLabel(item))}</button>`).join('')}</div><div class="run-list">${runs.length ? runs.map((run) => components.renderRun(run)).join('') : '<div class="empty">No matching runs.</div>'}</div></section>`;
}

function filesScreen(state) {
  const files = state.fileArtifacts || [];
  const enabled = state.capabilities?.execution_mode === 'sandbox';
  return `<section class="view active" id="view-files">${localPageHeader('Current cloud workspace', 'Files & results', 'Review and download artifacts generated by completed cloud runs.', `<span class="badge">${files.length} files</span>`)}${!enabled ? '<div class="notice">Cloud file generation is not enabled in this deployment. This page is ready for the sandbox artifacts when that capability is verified.</div>' : ''}<section class="panel"><div class="panelhead"><h3>Recent run outputs</h3><button class="secondary" id="refresh-files" type="button">↻ Refresh</button></div><p class="settingshint">${state.filesLoading ? 'Loading files…' : enabled ? `${files.length} artifact${files.length === 1 ? '' : 's'} from recent completed runs.` : 'Prompt-only mode · no generated file workspace.'}</p>${files.length ? `<ul class="files-list">${files.map((file) => `<li><span><b>${escapeHtml(file.path)}</b><small>Run ${escapeHtml(file.runId.slice(0, 8))} · ${Math.ceil((file.size_bytes || 0) / 1024)} KB</small></span><button class="secondary" data-artifact-id="${escapeHtml(file.artifact_id)}" data-run-id="${escapeHtml(file.runId)}" data-filename="${escapeHtml(file.path.split('/').pop())}">Download</button></li>`).join('')}</ul>` : '<div class="empty">Completed sandbox run artifacts will appear here.</div>'}</section><div class="overviewgrid"><section class="panel"><div class="panelhead"><h3>Changes & review</h3><span class="subtle">Not enabled in cloud</span></div><p class="settingshint">Diff preview and undo are available in the local workspace only; cloud runs currently expose final artifacts, not editable workspace diffs.</p></section><section class="panel"><div class="panelhead"><h3>Safety</h3></div><p class="safehint"><span class="safeicon">✓</span>Downloads are scoped to the run and signed-in account.</p></section></div></section>`;
}

function automationsScreen() {
  return `<section class="view active" id="view-automations">${localPageHeader('Cloud scheduler', 'Automations', 'Schedule one-off or repeating tasks for this cloud workspace.', '<span class="badge confirm">Scheduler not configured</span>')}<div class="hero automation-hero"><h2>Run tasks on a schedule</h2><p>The local UI supports scheduled tasks. Cloud scheduling needs tenant-safe ownership, timezone handling, retries and audit controls before it can run here.</p><div class="buttonrow"><button class="primary" disabled>Cloud scheduler unavailable</button></div></div><section class="panel automation-create"><div class="panelhead"><h3>New scheduled task</h3><span class="subtle">Unavailable in this deployment</span></div><div class="notice">This form is intentionally inactive; it won’t save a fake automation.</div><div class="settingrow"><strong>Task name</strong><input disabled placeholder="e.g. Weekly project review"></div><div class="settingrow"><strong>First run</strong><input disabled type="datetime-local"></div><div class="settingrow"><strong>Repeat</strong><select disabled><option>Run once</option><option>Repeat</option></select></div><label class="planbar"><input type="checkbox" disabled> Plan only · no tools run</label><button class="primary" disabled>Schedule task</button></section></section>`;
}

function activityScreen(state) {
  const events = state.runs.flatMap((run) => (run.events || []).map((event) => ({ ...event, runId: run.run_id, title: runTitle(run) }))).sort((a, b) => Number(b.timestamp) - Number(a.timestamp));
  return `<section class="view active" id="view-activity">${localPageHeader('Cloud run journal', 'Activity', 'Safe lifecycle updates from recent cloud runs.', '<button class="secondary" data-screen="history">Run history →</button>')}<div class="timeline">${events.length ? events.map((event) => `<article class="event"><time class="eventtime">${escapeHtml(dateLabel(event.timestamp))}</time><i class="eventdot"></i><div><div class="eventtext">${escapeHtml(event.detail || 'Run status changed')}</div><div class="eventlevel">${escapeHtml(statusLabel(event.status))} · ${escapeHtml(event.title)}</div></div></article>`).join('') : '<div class="empty">No run lifecycle events yet.</div>'}</div><div class="notice">Tool-level activity is not available in the current cloud runtime.</div></section>`;
}

function toolsScreen(state) {
  const tools = state.capabilities?.sandbox_tools || [];
  return `<section class="view active" id="view-tools">${localPageHeader('Capabilities', 'Tool catalog', 'See which cloud actions are currently available and what the deployment does not provide.') }<div class="notice">Tool permissions are managed by the cloud deployment; this screen does not grant local device access.</div><div class="filters"><input id="tool-search" placeholder="Search tools and descriptions…" disabled><button class="filterbtn active" disabled>All</button><button class="filterbtn" disabled>Read-only</button><button class="filterbtn" disabled>Confirmation</button></div><div class="catalogtools">${tools.length ? tools.map((tool) => `<article class="toolcard"><div class="tooltop"><span class="toolname">${escapeHtml(tool)}</span><span class="badge">Sandbox</span></div><p>Available only in the isolated cloud workspace.</p></article>`).join('') : '<article class="toolcard"><div class="tooltop"><span class="toolname">Prompt and response</span><span class="badge confirm">No tools</span></div><p>Prompt-only mode does not expose file, shell, browser, MCP or local-machine tools.</p></article>'}</div><div class="notice">The full local tool catalog and per-tool controls have not been migrated to cloud.</div></section>`;
}

function settingsScreen(state, components) {
  return `<section class="view active" id="view-settings">${localPageHeader('Preferences', 'Settings', 'Review this cloud session, capability policy and account controls.')}<div class="settingsgrid"><section class="panel"><div class="panelhead"><h3>Model & runtime</h3><span class="badge">Cloud managed</span></div><div class="metric"><span>Provider</span><b>Managed by deployment</b></div><div class="metric"><span>Active model</span><b>Server configured</b></div><div class="metric"><span>Execution mode</span><b>${escapeHtml(state.capabilities?.execution_mode || 'Checking')}</b></div><div class="metric"><span>Project context</span><b>${state.capabilities?.features?.text_file_upload ? 'Bounded files / public source' : 'Not enabled'}</b></div><div class="notice">Provider credentials and model switching are not user-editable in this hosted workspace.</div></section><section class="panel"><div class="panelhead"><h3>Chat & appearance</h3></div><div class="settingrow"><div><strong>Theme</strong><small>Match the local Niji workspace appearance.</small></div><button id="theme-toggle" class="toggle">Switch theme</button></div><div class="settingrow"><div><strong>Session conversations</strong><small>Persistent cloud threads and multi-turn history are not enabled.</small></div><span class="badge confirm">Not ready</span></div><button class="secondary" data-screen="tools">Open tool catalog →</button><button class="secondary" data-screen="activity">Open activity →</button></section></div><details class="panel settings-accordion" open><summary>Safety and cloud policy</summary><div class="notice">The cloud uses a fixed deployment-managed execution policy. Prompt-only runs have no tools; isolated sandbox mode is limited to its published allowlist.</div><div class="metric"><span>Approvals</span><b>Not available in cloud</b></div><div class="metric"><span>Browser / MCP / automations</span><b>Not hosted yet</b></div></details><section class="panel settings-extra"><div class="panelhead"><h3>Account & data</h3></div><div class="metric"><span>Signed in as</span><b>${escapeHtml(state.user?.email || state.user?.displayName || 'your account')}</b></div><div class="settings-actions"><button class="secondary" id="signout">Sign out</button><button class="exportbtn" id="delete-data" ${state.busy ? 'disabled' : ''}>Delete cloud data</button></div><p class="settingshint">Deletes stored runs and generated files for this account. Managed backups expire separately; your Google account remains.</p></section></section>`;
}

export function renderWorkspace(state, components) {
  const pages = { overview: () => homeChatScreen(state, components), home: () => homeChatScreen(state, components), chat: () => chatScreen(state, components), history: () => historyScreen(state, components), files: () => filesScreen(state), automations: () => automationsScreen(), settings: () => settingsScreen(state, components), activity: () => activityScreen(state), tools: () => toolsScreen(state) };
  return (pages[state.screen] || pages.overview)();
}

export function renderSidebar(state, renderRun) {
  const filter = (state.runSearch || '').trim().toLocaleLowerCase();
  const runs = state.runs.filter((run) => !filter || `${runTitle(run)} ${run.run_id} ${run.status}`.toLocaleLowerCase().includes(filter));
  return `${navigation.map((item) => `<button class="navbtn ${state.screen === item.id ? 'active' : ''}" data-screen="${item.id}" ${state.screen === item.id ? 'aria-current="page"' : ''}><span class="ico">${item.icon}</span>${item.label}</button>`).join('')}
    <div class="divider"></div><div class="threads"><div class="threadtitle"><span>RECENT RUNS</span><button class="iconbtn" id="refresh-runs-sidebar" type="button" title="Refresh runs" aria-label="Refresh runs">↻</button></div><label class="visually-hidden" for="run-search">Search recent runs</label><input id="run-search" class="searchbox" placeholder="Search recent runs…" value="${escapeHtml(state.runSearch || '')}" aria-label="Search recent runs"><div id="thread-list" class="threadlist">${runs.length ? runs.slice(0, 30).map((run) => `<div class="thread-row"><button class="threaditem ${state.activeRun?.run_id === run.run_id ? 'current' : ''}" data-run="${escapeHtml(run.run_id)}" title="${escapeHtml(runTitle(run))}">${escapeHtml(runTitle(run))}<span class="threadtime">${escapeHtml(statusLabel(run.status))} · ${escapeHtml(shortTime(run.updated_at))}</span></button></div>`).join('') : '<div class="empty">Your recent cloud runs will appear here.</div>'}</div></div>`;
}

export const WORKSPACE_NAVIGATION = navigation.map(({ id, label }) => ({ id, label }));
