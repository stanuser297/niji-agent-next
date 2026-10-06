import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { renderSidebar, renderWorkspace, WORKSPACE_NAVIGATION } from '../src/workspace-screens.js';

const components = {
  renderRun: (run) => `<button class="run-item" data-run="${run.run_id}">${run.status}</button>`,
  renderRuntimePanel: () => '<section class="runtime-panel">runtime-status</section>',
  renderProjectSourcePanel: () => '<details class="project-source">project-input</details>',
  renderActiveRun: () => '<section class="active-run">active-run</section>',
  resultText: (run) => run?.result?.text || '',
};
const baseState = {
  screen: 'overview', user: { email: 'person@example.com' }, runs: [], health: 'API online', capabilities: { execution_mode: 'prompt', sandbox_tools: [], features: {}, models: [{ id: 'nemotron-3.5-lightning', name: 'Nemotron 3.5 Lightning', default: true }, { id: 'nemotron-3-super-120b', name: 'Nemotron 3 Super 120B', default: false }] }, selectedModel: 'nemotron-3.5-lightning',
  activeRun: null, artifacts: [], fileArtifacts: [], filesLoading: false, busy: false, historyFilter: 'all', historyQuery: '', runSearch: '', error: '', notice: '',
};

test('sidebar follows the local Niji navigation and includes recent cloud runs', () => {
  assert.deepEqual(WORKSPACE_NAVIGATION.map((item) => item.id), ['overview', 'chat', 'files', 'automations', 'history', 'settings']);
  const html = renderSidebar({ ...baseState, runs: [{ run_id: 'r1', status: 'completed' }] }, components.renderRun);
  for (const screen of ['overview', 'chat', 'files', 'automations', 'history', 'settings']) assert.match(html, new RegExp(`data-screen="${screen}"`));
  assert.match(html, /RECENT RUNS/);
  assert.match(html, /run-search/);
  assert.match(html, /data-run="r1"/);
});

test('home is a simple chat-first screen with the composer and secure cloud route in view', () => {
  const html = renderWorkspace(baseState, components);
  assert.match(html, /home-chat-view/);
  assert.match(html, /id="form"/);
  assert.match(html, /id="prompt"/);
  assert.match(html, /Message Niji/);
  assert.match(html, /Niji is online/);
  assert.match(html, /What can I help with\?/);
  assert.match(html, /id="model-select"/);
  assert.match(html, /Nemotron 3\.5 Lightning/);
  assert.match(html, /Nemotron 3 Super 120B/);
  assert.doesNotMatch(html, /NVIDIA|OpenAI|API key|base URL/i);
  assert.doesNotMatch(html, /Good to see you|LIVE STATUS|Recent runs|Quick start|worker|provider|execution mode/i);
  assert.doesNotMatch(html, /integrate\.api\.nvidia\.com|provider_api_key/i);
});

test('home renders discovered provider models by model name only', () => {
  const models = [
    { id: 'deepseek-ai/deepseek-v3.2', name: 'DeepSeek V3.2', default: true },
    { id: 'qwen/qwen3.5-397b-a17b', name: 'Qwen 3.5 397B A17B', default: false },
  ];
  const html = renderWorkspace({ ...baseState, selectedModel: models[1].id, capabilities: { ...baseState.capabilities, models } }, components);
  assert.match(html, /value="deepseek-ai\/deepseek-v3\.2"/);
  assert.match(html, /DeepSeek V3\.2/);
  assert.match(html, /Qwen 3\.5 397B A17B/);
  assert.doesNotMatch(html, /provider|NVIDIA|OpenAI|API key|base URL/i);
});

test('home shows the user request and a simple queued status without infrastructure jargon', () => {
  const old = new Date(Date.now() - 120_000).toISOString();
  const run = { run_id: 'r1', status: 'queued', created_at: old, updated_at: old, payload: { prompt: 'hi' }, events: [{ detail: 'Run accepted' }] };
  const html = renderWorkspace({ ...baseState, lastPrompt: 'hi', activeRun: run }, components);
  assert.match(html, /hi/);
  assert.match(html, /Still waiting to start · 2m/);
  assert.match(html, /class="live-status-line is-queued"/);
  assert.match(html, /id="cancel-run"/);
  assert.doesNotMatch(html, /worker|provider|cloud execution/i);
  const queuedWhilePolling = renderWorkspace({ ...baseState, lastPrompt: 'hi', activeRun: run, busy: true }, components);
  assert.match(queuedWhilePolling, /class="live-status-line is-queued"/);
  assert.doesNotMatch(queuedWhilePolling, /live-status-line is-queued is-active/);
});

test('running home chat shows one real phase line with a shimmer-ready active state', () => {
  const run = { run_id: 'r2', status: 'running', payload: { prompt: 'build this' }, events: [{ status: 'running', detail: 'Agent is planning' }] };
  const html = renderWorkspace({ ...baseState, lastPrompt: 'build this', activeRun: run }, components);
  assert.match(html, /class="live-status-line is-active"/);
  assert.match(html, /Planning the next step…/);
  assert.match(html, /id="cancel-run"/);
  assert.doesNotMatch(html, /status-tag running|worktext|Agent is planning/);
});

test('live chat activity never echoes arbitrary event text into the compact status', () => {
  const run = { run_id: 'r3', status: 'running', payload: { prompt: 'help' }, events: [{ detail: 'private /home/person/secret.txt' }] };
  const html = renderWorkspace({ ...baseState, lastPrompt: 'help', activeRun: run }, components);
  assert.match(html, /Working on your request…/);
  assert.doesNotMatch(html, /secret\.txt|private \/home/);
});

test('home shows a completed response and preserves the conversation title for the restored run', () => {
  const run = { run_id: 'r1', status: 'completed', payload: { prompt: 'hi' }, result: { text: 'OK' }, updated_at: new Date().toISOString() };
  const html = renderWorkspace({ ...baseState, activeRun: run }, components);
  assert.match(html, /<h1 title="hi">hi<\/h1>/);
  assert.match(html, /class="message user"/);
  assert.match(html, /OK/);
  assert.match(html, /status-tag completed/);
  assert.match(html, /header-new-chat/);
});

test('chat screen keeps the cloud prompt composer, project controls and actual result output', () => {
  const html = renderWorkspace({ ...baseState, screen: 'chat', activeRun: { run_id: 'r1', status: 'completed', result: { text: 'task output' } } }, components);
  assert.match(html, /id="prompt"/);
  assert.match(html, /id="submit"/);
  assert.match(html, /project-input/);
  assert.match(html, /task output/);
  assert.match(html, /Session details/);
});

test('full chat uses the same single-line active status and avoids a duplicate header status', () => {
  const run = { run_id: 'r4', status: 'running', payload: { prompt: 'research this' }, events: [{ detail: 'Agent is working' }] };
  const html = renderWorkspace({ ...baseState, screen: 'chat', lastPrompt: 'research this', activeRun: run }, components);
  assert.match(html, /class="live-status-line is-active"/);
  assert.match(html, /Working on your task…/);
  assert.match(html, /id="cancel-run"/);
  assert.doesNotMatch(html, /id="chat-status"|worktext/);
});

test('history provides filtering over loaded cloud runs and discloses its limit', () => {
  const html = renderWorkspace({ ...baseState, screen: 'history', runs: [{ run_id: 'r1', status: 'failed' }] }, components);
  assert.match(html, /id="history-search"/);
  assert.match(html, /data-filter="failed"/);
  assert.match(html, /most recent 20 cloud runs/);
});

test('files screen explains prompt-only mode instead of inventing file access', () => {
  const html = renderWorkspace({ ...baseState, screen: 'files' }, components);
  assert.match(html, /not enabled in this deployment/i);
  assert.match(html, /Prompt-only mode/);
});

test('files screen lists artifacts with run-scoped download identity in sandbox mode', () => {
  const html = renderWorkspace({ ...baseState, screen: 'files', capabilities: { execution_mode: 'sandbox' }, fileArtifacts: [{ runId: 'run-123', artifact_id: 'file-1', path: 'report.txt', size_bytes: 1200 }] }, components);
  assert.match(html, /report\.txt/);
  assert.match(html, /data-run-id="run-123"/);
  assert.match(html, /data-artifact-id="file-1"/);
});

test('automation, activity and tool screens do not claim missing local features are live', () => {
  const automation = renderWorkspace({ ...baseState, screen: 'automations' }, components);
  assert.match(automation, /Scheduler not configured/);
  assert.match(automation, /intentionally inactive/);
  const activity = renderWorkspace({ ...baseState, screen: 'activity' }, components);
  assert.match(activity, /Tool-level activity is not available/);
  const tools = renderWorkspace({ ...baseState, screen: 'tools' }, components);
  assert.match(tools, /full local tool catalog.*not been migrated/i);
});

test('settings screen escapes account text and contains explicit data controls', () => {
  const html = renderWorkspace({ ...baseState, screen: 'settings', user: { email: '<img src=x onerror=alert(1)>' } }, components);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
  assert.match(html, /id="delete-data"/);
  assert.match(html, /id="signout"/);
  assert.match(html, /Persistent cloud threads and multi-turn history are not enabled/);
});

test('style keeps the restrained graphite and lime palette with a mobile-safe fixed composer', () => {
  const css = readFileSync(new URL('../src/style.css', import.meta.url), 'utf8');
  assert.match(css, /--bg:#0b1017/);
  assert.match(css, /--cyan:#c1f27a/);
  assert.match(css, /\.home-chat-view/);
  assert.match(css, /\.home-composer/);
  assert.match(css, /@media\(max-width:520px\).*\.home-chat-view/s);
  const homeCss = readFileSync(new URL('../src/home-chat.css', import.meta.url), 'utf8');
  assert.match(homeCss, /\.home-chat-view \.message[\s\S]*?border:\s*0/);
  assert.match(homeCss, /background:\s*transparent/);
  assert.match(homeCss, /\.home-chat-view \.message \.status-tag[\s\S]*?border:\s*0/);
  assert.match(homeCss, /\.app\.home-shell > \.main > \.topbar\s*\{\s*display:\s*none/);
  assert.match(homeCss, /\.home-chat-view \.message\.user[\s\S]*?align-self:\s*flex-start/);
  assert.match(homeCss, /\.live-status-text[\s\S]*?white-space:\s*nowrap/);
  assert.match(homeCss, /\.live-status-line\.is-active \.live-status-text/);
  assert.match(homeCss, /linear-gradient\(100deg,[^;]*#fff 50%/);
  assert.match(homeCss, /prefers-reduced-motion:\s*reduce/);
  assert.match(homeCss, /niji-status-shimmer/);
});

test('production rehydrates queued/running jobs after page reload and resumes status polling', () => {
  const main = readFileSync(new URL('../src/main.js', import.meta.url), 'utf8');
  assert.match(main, /Rehydrate a real in-flight\/queued run/);
  assert.match(main, /Keep the most recent completed answer visible after a page reload/);
  assert.match(main, /loadRuns\(\{ restoreLatest: true \}\)/);
  assert.match(main, /restoreLatest && !state\.newChatChosen && state\.runs\[0\]/);
  assert.match(main, /state\.newChatChosen = true/);
  assert.match(main, /pollRun\(state\.activeRun\.run_id\)/);
  assert.match(main, /#form/);
  assert.match(main, /data-retry-run/);
  assert.match(main, /state\.draftPrompt = event\.target\.value/);
  assert.match(main, /api\('runs', \{ method: 'POST'/);
  assert.match(main, /Authorization: `Bearer \$\{token\}`/);
  assert.match(main, /payload\.model = state\.selectedModel/);
  assert.match(main, /state\.draftPrompt = prompt/);
  assert.match(main, /niji-selected-model/);
  assert.doesNotMatch(main, /fetch\(['\"]https:\/\/integrate\.api\.nvidia\.com/);
});
