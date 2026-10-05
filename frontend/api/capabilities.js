import { proxy } from '../lib/proxy.js';

export default function handler(req, res) {
  return proxy(req, res, 'capabilities');
}
