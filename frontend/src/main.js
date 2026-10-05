import { initializeApp } from 'firebase/app';
import {
  browserSessionPersistence,
  getAuth,
  GoogleAuthProvider,
  onAuthStateChanged,
  setPersistence,
  signInWithPopup,
  signOut,
} from 'firebase/auth';
import './style.css';
import './home-chat.css';
import { buildCloudPayload } from './project-input.js';
import { renderSidebar, renderWorkspace } from './workspace-screens.js';

const projectId = import.meta.env.VITE_FIREBASE_PROJECT_ID || 'niji-agent';
const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY || '',
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN || `${projectId}.firebaseapp.com`,
  projectId,
  appId: import.meta.env.VITE_FIREBASE_APP_ID || '',
  messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID || undefined,
};
const configReady = Boolean(firebaseConfig.apiKey && firebaseConfig.appId);
const firebaseApp = configReady ? initializeApp(firebaseConfig) : null;
const auth = firebaseApp ? getAuth(firebaseApp) : null;
const googleProvider = new GoogleAuthProvider();
googleProvider.setCustomParameters({ prompt: 'select_account' });

const state = {
  user: null, screen: 'overview', runs: [], activeRun: null, artifacts: [], fileArtifacts: [],
  filesLoading: false, capabilities: null, pollingRuns: new Set(), historyFilter: 'all',
  historyQuery: '', runSearch: '', lastPrompt: '', draftPrompt: '', selectedModel: '', error: '', notice: '', busy: false,
  newChatChosen: false, health: 'Checking',
};
const app = document.querySelector('#app');
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));
const timeLabel = (value) => {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '' : new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(date);
};
const resultText = (run) => {
  const result = run?.result;
  if (typeof result === 'string') return result;
  if (!result) return run?.error || '';
  return result.response || result.answer || result.output || result.text || result.message || JSON.stringify(result, null, 2);
};

function signInPage() {
  app.innerHTML = `<div class="auth-page"><div class="auth-brand"><div class="brandmark">✧</div><div><div class="brandname">NIJI AGENT</div><div class="brandtag">Your ideas, in motion · cloud workspace</div></div></div><div class="auth-card panel"><div class="eyebrow">PRIVATE CLOUD WORKSPACE</div><h1>Welcome back</h1><p>Sign in to continue to your Niji workspace. The layout and navigation follow the local Niji UI; cloud capabilities depend on the active deployment.</p>${configReady ? '<button class="primary auth-google" id="signin"><span>G</span>Continue with Google</button>' : '<div class="notice">Sign-in setup needed · Firebase web settings must be configured in Vercel.</div>'}${state.error ? `<div class="notice auth-error" role="alert">${escapeHtml(state.error)}</div>` : ''}<small>Your prompts and run history are private to your signed-in account.</small></div></div>`;
  app.querySelector('#signin')?.addEventListener('click', doSignIn);
}

function signedInShell() {
  const initial = (state.user?.displayName || state.user?.email || 'N').slice(0, 1).toUpperCase();
  app.innerHTML = `<div class="app ${['overview', 'home'].includes(state.screen) ? 'home-shell' : ''}"><aside class="sidebar"><div class="brand"><div class="brandmark">✧</div><div><div class="brandname">NIJI AGENT</div><div class="brandtag">Cloud workspace</div></div></div><button class="newthread" id="new-chat" type="button">＋ New chat <span class="subtle">Ctrl K</span></button><div class="navlabel">Workspace</div><nav id="workspace-nav" class="nav" aria-label="Main navigation"></nav><div class="sidefoot"><div class="avatar">${escapeHtml(initial)}</div><div class="side-user"><strong>Niji cloud agent</strong><small id="side-provider">Cloud · checking</small></div><span id="local-dot" class="localdot" title="API status"></span></div></aside><button class="scrim" id="menu-scrim" aria-label="Close menu"></button><main class="main"><header class="topbar"><button class="menubtn" id="menu-button" aria-label="Open menu">☰</button><div class="crumb"><span>✧</span><span>/</span><strong id="crumb-title">Home</strong></div><div class="topright"><span class="pill" id="health-pill"><span class="statusdot">●</span> ${escapeHtml(state.health)}</span><span class="pill" id="mode-pill">Cloud · ${escapeHtml(state.capabilities?.execution_mode || 'checking')}</span><button class="iconbtn" id="settings-shortcut" title="Open settings" aria-label="Open settings">⚙</button><button class="secondary signout-top" id="signout">Sign out</button></div></header><div class="content ${state.screen === 'overview' ? 'home-content' : ''}"><div id="flash-area"></div><div id="main-view"></div></div></main></div>`;
  app.querySelector('#workspace-nav').innerHTML = renderSidebar(state, renderRun);
  app.querySelector('#side-provider').textContent = `Cloud · ${state.capabilities?.execution_mode || 'checking'}`;
  app.querySelector('#local-dot')?.classList.toggle('offline', state.health !== 'API online');
  const view = app.querySelector('#main-view');
  view.innerHTML = renderWorkspace(state, { renderRun, renderProjectSourcePanel, resultText });
  const flash = app.querySelector('#flash-area');
  flash.innerHTML = `${state.notice ? `<div class="notice success-note" role="status">${escapeHtml(state.notice)}</div>` : ''}${state.error && !['chat', 'overview', 'home'].includes(state.screen) ? `<div class="notice auth-error" role="alert">${escapeHtml(state.error)}</div>` : ''}`;
  const title = ({ overview: 'Home', chat: 'Chat', files: 'Files', automations: 'Automations', history: 'Run history', settings: 'Settings', activity: 'Activity', tools: 'Tool catalog' })[state.screen] || 'Home';
  app.querySelector('#crumb-title').textContent = title;
  app.querySelector('#new-chat')?.addEventListener('click', () => startNewChat());
  app.querySelector('#header-new-chat')?.addEventListener('click', () => startNewChat());
  app.querySelector('#settings-shortcut')?.addEventListener('click', () => setScreen('settings'));
  app.querySelector('#signout')?.addEventListener('click', doSignOut);
  app.querySelector('#menu-button')?.addEventListener('click', () => document.body.classList.toggle('menu-open'));
  app.querySelector('#menu-scrim')?.addEventListener('click', () => document.body.classList.remove('menu-open'));
  app.querySelector('#refresh-runs-sidebar')?.addEventListener('click', loadRuns);
  app.querySelector('#run-search')?.addEventListener('input', (event) => {
    state.runSearch = event.target.value;
    const pos = event.target.selectionStart;
    signedInShell();
    const input = app.querySelector('#run-search'); input?.focus(); input?.setSelectionRange(pos, pos);
  });
  app.querySelectorAll('[data-screen]').forEach((button) => button.addEventListener('click', () => {
    if (button.dataset.settingsSection) return setScreen(button.dataset.settingsSection);
    if (button.dataset.screen === 'chat') return startNewChat();
    setScreen(button.dataset.screen);
  }));
  app.querySelectorAll('[data-prompt]').forEach((button) => button.addEventListener('click', () => {
    state.draftPrompt = button.dataset.prompt || '';
    startNewChat({ preserveDraft: true });
    const prompt = app.querySelector('#prompt'); prompt?.focus(); prompt?.setSelectionRange(prompt.value.length, prompt.value.length);
  }));
  app.querySelectorAll('[data-run]').forEach((button) => button.addEventListener('click', () => openRun(button.dataset.run)));
  app.querySelector('#form')?.addEventListener('submit', submitPrompt);
  app.querySelector('#model-select')?.addEventListener('change', (event) => {
    const model = state.capabilities?.models?.find((item) => item.id === event.target.value);
    if (!model) return;
    state.selectedModel = model.id;
    try { localStorage.setItem('niji-selected-model', model.id); } catch { /* Model selection still applies for this page. */ }
    render();
  });
  app.querySelectorAll('#prompt, #home-prompt').forEach((input) => input.addEventListener('input', (event) => { state.draftPrompt = event.target.value; }));
  app.querySelectorAll('[data-retry-run]').forEach((button) => button.addEventListener('click', () => {
    state.draftPrompt = state.activeRun?.ui_prompt || state.activeRun?.payload?.prompt || state.lastPrompt || '';
    state.activeRun = null; state.lastPrompt = ''; state.error = '';
    render();
    submitPrompt({ preventDefault() {} });
  }));
  for (const input of [app.querySelector('#prompt'), app.querySelector('#home-prompt')].filter(Boolean)) {
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submitPrompt(event); }
    });
  }
  app.querySelectorAll('[data-cancel-run]').forEach((button) => button.addEventListener('click', cancelActiveRun));
  app.querySelector('#attach-button')?.addEventListener('click', () => {
    const details = app.querySelector('.project-source');
    if (details) { details.open = true; details.querySelector('#project-files')?.click(); }
    else { state.error = 'Project files are not enabled in this cloud mode.'; signedInShell(); }
  });
  app.querySelector('#github-button')?.addEventListener('click', () => {
    const details = app.querySelector('.project-source');
    if (details) { details.open = true; details.querySelector('#repo-url')?.focus(); }
    else { state.error = 'GitHub import is not enabled in this cloud mode.'; signedInShell(); }
  });
  app.querySelector('#refresh-history')?.addEventListener('click', loadRuns);
  app.querySelector('#refresh-files')?.addEventListener('click', loadFileArtifacts);
  app.querySelector('#history-search')?.addEventListener('input', (event) => {
    state.historyQuery = event.target.value;
    const pos = event.target.selectionStart;
    signedInShell();
    const input = app.querySelector('#history-search'); input?.focus(); input?.setSelectionRange(pos, pos);
  });
  app.querySelectorAll('[data-filter]').forEach((button) => button.addEventListener('click', () => { state.historyFilter = button.dataset.filter; render(); }));
  app.querySelector('#cancel-run')?.addEventListener('click', cancelActiveRun);
  app.querySelector('#show-run-files')?.addEventListener('click', () => setScreen('files'));
  app.querySelectorAll('[data-artifact-id]').forEach((button) => button.addEventListener('click', () => downloadArtifact(button.dataset.runId || state.activeRun?.run_id, button.dataset.artifactId, button.dataset.filename)));
  app.querySelector('#delete-data')?.addEventListener('click', deleteCloudData);
  app.querySelector('#theme-toggle')?.addEventListener('click', toggleTheme);
  const prompt = app.querySelector('#prompt');
  if (prompt) prompt.value = state.draftPrompt || '';
  document.body.classList.toggle('light', localStorage.getItem('niji-theme') === 'light');
}

function render() {
  if (!state.user) return signInPage();
  signedInShell();
}

function setScreen(screen) {
  state.screen = screen;
  state.error = '';
  state.notice = '';
  document.body.classList.remove('menu-open');
  render();
  if (screen === 'history') loadRuns();
  if (screen === 'files') loadFileArtifacts();
}

function startNewChat({ preserveDraft = false } = {}) {
  state.newChatChosen = true;
  state.screen = 'overview';
  state.activeRun = null;
  state.artifacts = [];
  state.lastPrompt = '';
  if (!preserveDraft) state.draftPrompt = '';
  state.error = '';
  state.notice = '';
  document.body.classList.remove('menu-open');
  render();
  app.querySelector('#prompt')?.focus();
}

function renderProjectSourcePanel() {
  if (!state.capabilities?.features?.text_file_upload) return '';
  return `<details class="project-source"><summary>Attach project context <small>Optional · isolated cloud workspace</small></summary><div class="project-source-body"><label for="project-files">Upload a folder or text files</label><input id="project-files" type="file" multiple webkitdirectory /><small>Up to 100 text files, 64 KB each / 500 KB total. Secret-looking or binary files are rejected.</small><div class="source-divider"><span>or import public GitHub source</span></div><label for="repo-url">Public repository URL</label><input id="repo-url" type="url" placeholder="https://github.com/owner/repository" /><label for="repo-revision">Full commit SHA (40 characters)</label><input id="repo-revision" type="text" maxlength="40" placeholder="40-character immutable commit" /><small>Private repositories and branch names are not supported. Select files or repository—not both.</small></div></details>`;
}

function renderRun(run) {
  const title = run.result?.title || run.result?.summary || (typeof run.result?.text === 'string' ? run.result.text.slice(0, 70) : '') || `Run ${String(run.run_id).slice(0, 8)}`;
  return `<button class="threaditem" data-run="${escapeHtml(run.run_id)}" title="${escapeHtml(title)}">${escapeHtml(title)}<span class="threadtime"><i class="statusdot ${escapeHtml(run.status)}">●</i> ${escapeHtml(run.status)} · ${timeLabel(run.updated_at)}</span></button>`;
}

async function getToken() {
  if (!auth?.currentUser) throw new Error('Please sign in again.');
  return auth.currentUser.getIdToken();
}

async function api(path, options = {}) {
  const token = await getToken();
  const response = await fetch(`/api/${path.replace(/^\//, '')}`, {
    ...options,
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), Authorization: `Bearer ${token}`, ...(options.headers || {}) },
    cache: 'no-store',
  });
  let body = {};
  try { body = await response.json(); } catch { /* Keep a status fallback. */ }
  if (!response.ok) throw new Error(body.detail || body.error || `Request failed (${response.status})`);
  return body;
}

async function loadCapabilities() {
  if (!state.user) return;
  try {
    state.capabilities = await api('capabilities');
    const models = state.capabilities?.models || [];
    let saved = '';
    try { saved = localStorage.getItem('niji-selected-model') || ''; } catch { /* Optional preference. */ }
    state.selectedModel = models.find((model) => model.id === saved)?.id
      || models.find((model) => model.default)?.id || models[0]?.id || '';
    if (state.selectedModel && state.selectedModel !== saved) {
      try { localStorage.setItem('niji-selected-model', state.selectedModel); } catch { /* Optional preference. */ }
    }
  } catch { state.capabilities = null; }
  render();
  if (state.screen === 'files' && state.capabilities?.execution_mode === 'sandbox') loadFileArtifacts();
}

async function loadArtifacts(runId) {
  if (!state.user || state.activeRun?.run_id !== runId) return;
  try {
    const data = await api(`runs/${encodeURIComponent(runId)}/artifacts`);
    if (state.activeRun?.run_id === runId) { state.artifacts = data.artifacts || []; render(); }
  } catch { state.artifacts = []; render(); }
}

async function loadFileArtifacts() {
  if (!state.user) return;
  if (state.capabilities?.execution_mode !== 'sandbox') { state.fileArtifacts = []; state.filesLoading = false; render(); return; }
  state.filesLoading = true; render();
  const completed = state.runs.filter((run) => run.status === 'completed').slice(0, 20);
  const lists = await Promise.allSettled(completed.map(async (run) => {
    const data = await api(`runs/${encodeURIComponent(run.run_id)}/artifacts`);
    return (data.artifacts || []).map((item) => ({ ...item, runId: run.run_id }));
  }));
  state.fileArtifacts = lists.flatMap((result) => result.status === 'fulfilled' ? result.value : []);
  state.filesLoading = false; render();
}

async function downloadArtifact(runId, artifactId, filename) {
  if (!runId || !artifactId) return;
  try {
    const token = await getToken();
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}`, { headers: { Authorization: `Bearer ${token}` }, cache: 'no-store' });
    if (!response.ok) { let body = {}; try { body = await response.json(); } catch { /* fallback */ } throw new Error(body.detail || `Download failed (${response.status})`); }
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = filename || 'niji-artifact'; document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 30_000);
  } catch (error) { state.error = error.message; render(); }
}

async function deleteCloudData() {
  if (!window.confirm('Delete this account’s live Niji runs, activity and generated artifacts? This cannot be undone. Managed database backups expire separately; your Google sign-in account will remain.')) return;
  state.busy = true; state.error = ''; state.notice = ''; render();
  try {
    const result = await api('account/data', { method: 'DELETE', headers: { 'X-Confirm-Data-Deletion': 'delete' } });
    state.runs = []; state.activeRun = null; state.artifacts = []; state.fileArtifacts = [];
    state.notice = `Deleted ${result.deleted_runs ?? 0} stored run(s) and their generated files. Your Google sign-in account was not deleted.`;
  } catch (error) { state.error = error.message; }
  state.busy = false; render();
}

async function checkHealth() {
  try { const response = await fetch('/api/healthz', { cache: 'no-store' }); if (!response.ok) throw new Error(); state.health = 'API online'; }
  catch { state.health = 'API unavailable'; }
  if (state.user) render();
}

async function doSignIn() {
  state.error = ''; render();
  try { await setPersistence(auth, browserSessionPersistence); await signInWithPopup(auth, googleProvider); }
  catch (error) { state.error = friendlyAuthError(error); render(); }
}

function friendlyAuthError(error) {
  const code = error?.code || '';
  if (code.includes('unauthorized-domain')) return 'Add this Vercel domain to Firebase Authentication → Settings → Authorized domains, then try again.';
  if (code.includes('operation-not-allowed')) return 'Enable Google sign-in in Firebase Authentication → Sign-in method.';
  if (code.includes('api-key-not-valid')) return 'The Firebase web API key in Vercel is not valid for this project.';
  if (code.includes('popup-closed-by-user')) return 'Sign-in was cancelled. You can try again.';
  return error?.message || 'Sign-in failed. Check Firebase Authentication setup and try again.';
}

async function doSignOut() {
  await signOut(auth);
  state.user = null; state.runs = []; state.activeRun = null; state.artifacts = []; state.fileArtifacts = [];
  state.screen = 'overview'; state.capabilities = null; state.error = ''; state.notice = ''; state.lastPrompt = ''; state.draftPrompt = '';
  render();
}

async function loadRuns({ restoreLatest = false } = {}) {
  if (!state.user) return;
  try {
    const data = await api('runs?limit=20');
    state.runs = data.runs || [];
    const activeRun = state.runs.find((run) => ['queued', 'running', 'cancelling'].includes(run.status));
    if (state.activeRun) {
      const current = state.runs.find((run) => run.run_id === state.activeRun.run_id);
      if (current) state.activeRun = { ...current, ui_prompt: state.activeRun.ui_prompt || current.payload?.prompt || '' };
      else if (activeRun && ['completed', 'failed', 'cancelled', 'timed_out'].includes(state.activeRun.status)) {
        state.activeRun = { ...activeRun, ui_prompt: activeRun.payload?.prompt || '' };
      }
    } else if (activeRun) {
      // Rehydrate a real in-flight/queued run after page reload so Home keeps updating.
      state.activeRun = { ...activeRun, ui_prompt: activeRun.payload?.prompt || '' };
    } else if (restoreLatest && !state.newChatChosen && state.runs[0]) {
      // Keep the most recent completed answer visible after a page reload.
      const latestRun = state.runs[0];
      state.activeRun = { ...latestRun, ui_prompt: latestRun.payload?.prompt || '' };
    }
    state.error = '';
  } catch (error) { state.error = error.message; }
  render();
  if (state.activeRun && ['queued', 'running', 'cancelling'].includes(state.activeRun.status)) pollRun(state.activeRun.run_id);
  if (state.screen === 'files' && state.capabilities?.execution_mode === 'sandbox') loadFileArtifacts();
}

async function submitPrompt(event) {
  event?.preventDefault();
  if (state.busy) return;
  if (state.activeRun && ['queued', 'running', 'cancelling'].includes(state.activeRun.status)) return cancelActiveRun();
  const prompt = app.querySelector('#prompt')?.value || app.querySelector('#home-prompt')?.value || '';
  if (!prompt.trim()) return;
  const projectFiles = Array.from(app.querySelector('#project-files')?.files || []);
  const repositoryUrl = app.querySelector('#repo-url')?.value || '';
  const revision = app.querySelector('#repo-revision')?.value || '';
  state.lastPrompt = prompt; state.draftPrompt = ''; state.activeRun = null; state.busy = true; state.error = ''; state.notice = ''; state.artifacts = [];
  render();
  try {
    const payload = await buildCloudPayload({ prompt, files: projectFiles, repositoryUrl, revision });
    if (state.capabilities?.models?.some((model) => model.id === state.selectedModel)) payload.model = state.selectedModel;
    if ((projectFiles.length || repositoryUrl.trim() || revision.trim()) && !state.capabilities?.features?.text_file_upload) throw new Error('Project files and repository import are not enabled in the current cloud mode.');
    const run = await api('runs', { method: 'POST', body: JSON.stringify({ idempotency_key: crypto.randomUUID(), payload, timeout_seconds: 900 }) });
    state.activeRun = { ...run, ui_prompt: state.lastPrompt };
    state.busy = false;
    await loadRuns();
    if (state.activeRun) pollRun(state.activeRun.run_id);
  } catch (error) {
    state.busy = false;
    state.lastPrompt = '';
    state.draftPrompt = prompt;
    state.error = error.message;
    render();
  }
}

async function pollRun(runId) {
  if (state.pollingRuns.has(runId)) return;
  state.pollingRuns.add(runId);
  const finalStates = ['completed', 'failed', 'cancelled', 'timed_out'];
  try {
    while (state.activeRun?.run_id === runId && !finalStates.includes(state.activeRun.status)) {
      await new Promise((resolve) => setTimeout(resolve, 2200));
      const latest = await api(`runs/${encodeURIComponent(runId)}`);
      if (state.activeRun?.run_id !== runId) break;
      state.activeRun = { ...latest, ui_prompt: state.activeRun.ui_prompt };
      state.runs = [state.activeRun, ...state.runs.filter((run) => run.run_id !== runId)].slice(0, 20);
      render();
    }
    if (state.activeRun?.run_id === runId && state.activeRun.status === 'completed') await loadArtifacts(runId);
  } catch (error) { if (state.activeRun?.run_id === runId) state.error = error.message; render(); }
  finally { state.pollingRuns.delete(runId); }
}

async function openRun(runId) {
  state.screen = 'overview'; state.error = ''; state.artifacts = []; state.lastPrompt = '';
  try {
    state.activeRun = await api(`runs/${encodeURIComponent(runId)}`);
    render();
    if (state.activeRun.status === 'completed') await loadArtifacts(runId);
    else if (!['failed', 'cancelled', 'timed_out'].includes(state.activeRun.status)) pollRun(runId);
  } catch (error) { state.error = error.message; render(); }
}

async function cancelActiveRun() {
  if (!state.activeRun) return;
  try {
    state.activeRun = await api(`runs/${encodeURIComponent(state.activeRun.run_id)}/cancel`, { method: 'POST', body: '{}' });
    render();
    if (!['completed', 'failed', 'cancelled', 'timed_out'].includes(state.activeRun.status)) pollRun(state.activeRun.run_id);
  } catch (error) { state.error = error.message; render(); }
}

function toggleTheme() {
  const light = !document.body.classList.contains('light');
  document.body.classList.toggle('light', light); localStorage.setItem('niji-theme', light ? 'light' : 'dark');
}

function updateAuthState(user) {
  state.user = user; state.error = ''; state.notice = '';
  if (!user) { state.capabilities = null; state.artifacts = []; state.fileArtifacts = []; }
  render();
  if (user) { loadRuns({ restoreLatest: true }); loadCapabilities(); }
}

render();
checkHealth();
if (auth) onAuthStateChanged(auth, updateAuthState);
