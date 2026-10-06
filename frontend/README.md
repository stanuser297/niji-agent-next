# Niji Cloud Frontend

Vite app for the Niji Cloud Runs API. Vercel serves the static interface and an allowlisted set of same-origin `/api/*` serverless routes forwards requests to the Railway API. Firebase ID tokens are sent in the `Authorization` header; provider keys and database credentials stay on Railway.

The interface reports the server's effective execution mode rather than implying local-agent parity. In prompt mode it accepts ordinary prompt/response tasks only. In sandbox mode it also supports bounded text project files and public GitHub repositories pinned to a full commit SHA. Completed sandbox artifacts can be downloaded, and signed-in users can permanently delete their stored cloud run data. Sandboxed shell/file tools are still a limited subset of Niji's local tools; local browser automation, MCP connectors, persistent local sessions and memory are not hosted by this UI.

## Local run and tests

1. Copy `.env.example` to `.env.local` and fill the Firebase **web app** configuration from Firebase Console → Project settings → General → Your apps. These are client configuration values, not the Firebase Admin service-account private key.
2. Install and run: `npm install && npm run dev`.
3. Local Vite does not run Vercel's serverless routes; use Vercel's local development command (`npx vercel dev`) to test the UI with `/api/*` routing.
4. Run frontend unit tests and production build with `npm test && npm run build`.

## Vercel setup

- Import the `stanuser297/niji-agent-next` repository after linking the GitHub account in Vercel.
- Set the project root to `frontend`.
- Build command: `npm run build`; output directory: `dist`.
- Set these **Production and Preview** environment variables:
  - `VITE_FIREBASE_API_KEY`
  - `VITE_FIREBASE_AUTH_DOMAIN`
  - `VITE_FIREBASE_PROJECT_ID` (`niji-agent`)
  - `VITE_FIREBASE_APP_ID`
  - `VITE_FIREBASE_MESSAGING_SENDER_ID` (if shown in the Firebase web config)
  - `NIJI_API_BASE_URL=https://your-api.example.com`
- In Firebase Authentication, enable Google sign-in and add the deployed Vercel hostname under Authorized domains. Firebase ID tokens are verified by the Railway API against the project issuer/audience; the web configuration must refer to the same Firebase project.
- The frontend exposes only explicit allowlisted routes: health, capabilities, run create/list/detail/cancel, tenant artifact listing/download, and confirmed tenant-data deletion. Artifact downloads remain authenticated and are streamed as bytes; deletion requires the explicit `X-Confirm-Data-Deletion: delete` header.

No Firebase Admin credentials, model-provider keys, E2B keys, or Railway database secrets belong in Vercel or in this repository. Do not switch the worker to sandbox mode until live E2B network-denial, resource-limit, cancellation, teardown, artifact, and provider-spend tests have passed. The USD token budget does not cap sandbox-compute charges.
