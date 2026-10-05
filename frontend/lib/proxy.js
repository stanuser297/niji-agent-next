const DEFAULT_API = 'https://niji-cloud-api-production.up.railway.app';
const isRunRoute = (route) => /^runs\/[A-Za-z0-9_-]{1,100}$/.test(route);
const isCancelRoute = (route) => /^runs\/[A-Za-z0-9_-]{1,100}\/cancel$/.test(route);
const isArtifactListRoute = (route) => /^runs\/[A-Za-z0-9_-]{1,100}\/artifacts$/.test(route);
const isArtifactDownloadRoute = (route) => /^runs\/[A-Za-z0-9_-]{1,100}\/artifacts\/[A-Za-z0-9._-]{1,120}$/.test(route);

export async function proxy(req, res, route) {
  res.setHeader('Cache-Control', 'private, no-store, max-age=0');
  res.setHeader('X-Content-Type-Options', 'nosniff');

  const method = req.method || 'GET';
  const allowed = (route === 'healthz' && method === 'GET')
    || (route === 'capabilities' && method === 'GET')
    || (route === 'account/data' && method === 'DELETE')
    || (route === 'runs' && ['GET', 'POST'].includes(method))
    || (isRunRoute(route) && method === 'GET')
    || (isCancelRoute(route) && method === 'POST')
    || ((isArtifactListRoute(route) || isArtifactDownloadRoute(route)) && method === 'GET');

  if (!allowed) {
    res.setHeader('Allow', route === 'runs' ? 'GET, POST' : route === 'account/data' ? 'DELETE' : route.endsWith('/cancel') ? 'POST' : 'GET');
    res.status(404).json({ detail: 'Route not found' });
    return;
  }

  const base = (process.env.NIJI_API_BASE_URL || DEFAULT_API).replace(/\/+$/, '');
  const upstreamPath = route === 'healthz' ? '/healthz'
    : route === 'capabilities' ? '/v1/capabilities'
      : route === 'account/data' ? '/v1/account/data' : `/v1/${route}`;
  const url = new URL(`${base}${upstreamPath}`);
  if (route === 'runs' && method === 'GET') {
    const limit = Number(req.query?.limit ?? 20);
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
      res.status(400).json({ detail: 'Invalid limit' });
      return;
    }
    url.searchParams.set('limit', String(limit));
  }

  const headers = {};
  const authorization = req.headers?.authorization;
  if (typeof authorization === 'string' && authorization.length < 20_000) headers.Authorization = authorization;
  if (method === 'POST') headers['Content-Type'] = 'application/json';
  if (route === 'account/data' && method === 'DELETE') {
    const confirmation = req.headers?.['x-confirm-data-deletion'];
    if (typeof confirmation === 'string' && confirmation.toLowerCase() === 'delete') {
      headers['X-Confirm-Data-Deletion'] = 'delete';
    }
  }

  let body;
  if (method === 'POST') {
    body = typeof req.body === 'string' ? req.body : JSON.stringify(req.body ?? {});
    if (body.length > 1_050_000) {
      res.status(413).json({ detail: 'Request body is too large' });
      return;
    }
  }

  try {
    const upstream = await fetch(url, {
      method,
      headers,
      body: method === 'POST' ? body : undefined,
      redirect: 'error',
      signal: AbortSignal.timeout(25_000),
    });
    const contentType = upstream.headers.get('content-type');
    if (contentType) res.setHeader('Content-Type', contentType);
    for (const name of ['content-disposition', 'etag']) {
      const value = upstream.headers.get(name);
      if (value) res.setHeader(name, value);
    }
    const retryAfter = upstream.headers.get('retry-after');
    if (retryAfter) res.setHeader('Retry-After', retryAfter);
    const location = upstream.headers.get('location');
    if (location && location.startsWith('/v1/')) res.setHeader('Location', location.replace(/^\/v1\//, '/api/'));
    if (contentType && !contentType.toLowerCase().includes('application/json')) {
      res.status(upstream.status).send(Buffer.from(await upstream.arrayBuffer()));
    } else {
      res.status(upstream.status).send(await upstream.text());
    }
  } catch {
    res.status(502).json({ detail: 'Niji API is temporarily unavailable' });
  }
}
