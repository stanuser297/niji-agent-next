import { proxy } from '../../../lib/proxy.js';

export default function handler(req, res) {
  const runId = req.query?.runId;
  if (typeof runId !== 'string' || !/^[A-Za-z0-9_-]{1,100}$/.test(runId)) {
    res.status(404).json({ detail: 'Run not found' });
    return;
  }
  return proxy(req, res, `runs/${runId}/artifacts`);
}
