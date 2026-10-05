import { proxy } from '../../../../lib/proxy.js';

export default function handler(req, res) {
  const runId = req.query?.runId;
  const artifactId = req.query?.artifactId;
  if (typeof runId !== 'string' || !/^[A-Za-z0-9_-]{1,100}$/.test(runId)
    || typeof artifactId !== 'string' || !/^[A-Za-z0-9._-]{1,120}$/.test(artifactId)) {
    res.status(404).json({ detail: 'Artifact not found' });
    return;
  }
  return proxy(req, res, `runs/${runId}/artifacts/${artifactId}`);
}
