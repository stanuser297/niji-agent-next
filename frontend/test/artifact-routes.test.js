import test from 'node:test';
import assert from 'node:assert/strict';
import listArtifacts from '../api/runs/[runId]/artifacts.js';
import downloadArtifact from '../api/runs/[runId]/artifacts/[artifactId].js';

function responseRecorder() {
  return {
    statusCode: 200, body: undefined,
    status(code) { this.statusCode = code; return this; },
    json(value) { this.body = value; return this; },
  };
}

test('artifact routes reject unsafe and repeated dynamic parameters before proxying', () => {
  const cases = [
    [listArtifacts, { runId: '../other' }, 'Run not found'],
    [listArtifacts, { runId: ['safe-id', '../other'] }, 'Run not found'],
    [downloadArtifact, { runId: 'safe-id', artifactId: '../secret' }, 'Artifact not found'],
    [downloadArtifact, { runId: 'safe-id', artifactId: ['safe-id', '../secret'] }, 'Artifact not found'],
  ];
  for (const [handler, query, detail] of cases) {
    const res = responseRecorder();
    handler({ method: 'GET', query }, res);
    assert.equal(res.statusCode, 404);
    assert.equal(res.body.detail, detail);
  }
});
