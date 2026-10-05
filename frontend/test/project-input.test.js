import test from 'node:test';
import assert from 'node:assert/strict';
import { buildCloudPayload } from '../src/project-input.js';

const file = (name, content, relative = name) => ({
  name, webkitRelativePath: relative, size: Buffer.byteLength(content),
  async text() { return content; },
});

test('serializes bounded UTF-8 project files with their relative paths', async () => {
  const payload = await buildCloudPayload({
    prompt: '  review this project  ',
    files: [file('main.py', 'print("नमस्ते")', 'demo/src/main.py')],
  });
  assert.deepEqual(payload, {
    prompt: 'review this project',
    files: [{ path: 'demo/src/main.py', content: 'print("नमस्ते")' }],
  });
});

test('accepts a public repository only when pinned to a full immutable commit', async () => {
  const payload = await buildCloudPayload({
    prompt: 'review this repository',
    repositoryUrl: 'https://github.com/example/project.git',
    revision: 'A'.repeat(40),
  });
  assert.deepEqual(payload, {
    prompt: 'review this repository',
    repository: { url: 'https://github.com/example/project', revision: 'a'.repeat(40) },
  });
  await assert.rejects(() => buildCloudPayload({
    prompt: 'review', repositoryUrl: 'https://github.com/example/project', revision: 'main',
  }), /full 40-character commit SHA/);
});

test('rejects mixed sources, excessive file count/size, and binary text', async () => {
  await assert.rejects(() => buildCloudPayload({
    prompt: 'review', files: [file('a.txt', 'a')],
    repositoryUrl: 'https://github.com/example/project', revision: 'a'.repeat(40),
  }), /Choose project files or a GitHub repository/);
  await assert.rejects(() => buildCloudPayload({
    prompt: 'review', files: Array.from({ length: 101 }, (_, i) => file(`${i}.txt`, 'a')),
  }), /at most 100/);
  await assert.rejects(() => buildCloudPayload({
    prompt: 'review', files: [file('large.txt', 'a'.repeat(64_001))],
  }), /64 KB or smaller/);
  await assert.rejects(() => buildCloudPayload({
    prompt: 'review', files: Array.from({ length: 9 }, (_, i) => file(`${i}.txt`, 'a'.repeat(60_000))),
  }), /500 KB or less/);
  await assert.rejects(() => buildCloudPayload({
    prompt: 'x'.repeat(100_001),
  }), /100 KB limit/);
  await assert.rejects(() => buildCloudPayload({
    prompt: 'review', files: [file('image.png', 'a\0b')],
  }), /Binary files are not supported/);
});
