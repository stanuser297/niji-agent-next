const MAX_PROMPT_BYTES = 100_000;
const MAX_FILES = 100;
const MAX_FILE_BYTES = 64_000;
const MAX_TOTAL_BYTES = 500_000;
const encoder = new TextEncoder();

export async function buildCloudPayload({ prompt, files = [], repositoryUrl = '', revision = '' }) {
  if (typeof prompt !== 'string' || !prompt.trim()) throw new Error('Write a task before starting.');
  const normalizedPrompt = prompt.trim();
  if (encoder.encode(normalizedPrompt).byteLength > MAX_PROMPT_BYTES) {
    throw new Error('Your task is larger than the 100 KB limit.');
  }

  const selectedFiles = Array.from(files || []);
  const urlText = String(repositoryUrl || '').trim();
  const revisionText = String(revision || '').trim();
  const hasRepositoryFields = Boolean(urlText || revisionText);
  if (selectedFiles.length && hasRepositoryFields) {
    throw new Error('Choose project files or a GitHub repository—not both.');
  }

  if (hasRepositoryFields) {
    if (!urlText || !revisionText) throw new Error('Enter both the public GitHub URL and the full commit SHA.');
    let parsed;
    try { parsed = new URL(urlText); } catch { throw new Error('Enter a valid HTTPS GitHub repository URL.'); }
    const parts = parsed.pathname.split('/').filter(Boolean);
    if (parsed.protocol !== 'https:' || parsed.hostname.toLowerCase() !== 'github.com'
      || parsed.username || parsed.password || parsed.port || parsed.search || parsed.hash
      || parts.length !== 2 || !/^[A-Za-z0-9-]{1,39}$/.test(parts[0])
      || !/^[A-Za-z0-9._-]{1,100}(?:\.git)?$/.test(parts[1])) {
      throw new Error('Only a public https://github.com/owner/repository URL is supported.');
    }
    if (!/^[a-f0-9]{40}$/i.test(revisionText)) {
      throw new Error('Use the full 40-character commit SHA so the imported source is fixed and reviewable.');
    }
    return { prompt: normalizedPrompt, repository: { url: `https://github.com/${parts[0]}/${parts[1].replace(/\.git$/i, '')}`, revision: revisionText.toLowerCase() } };
  }

  if (!selectedFiles.length) return { prompt: normalizedPrompt };
  if (selectedFiles.length > MAX_FILES) throw new Error('Choose at most 100 project files.');
  const projectFiles = [];
  let totalBytes = 0;
  for (const file of selectedFiles) {
    const path = String(file.webkitRelativePath || file.name || '');
    if (!path || path.length > 240 || path.includes('\\') || path.includes('\0') || path.startsWith('/')) {
      throw new Error('A selected project file has an invalid path.');
    }
    if (file.size > MAX_FILE_BYTES) throw new Error(`Each project file must be 64 KB or smaller (${path}).`);
    const content = await file.text();
    const bytes = encoder.encode(content).byteLength;
    if (bytes > MAX_FILE_BYTES) throw new Error(`Each project file must be 64 KB or smaller (${path}).`);
    if (content.includes('\0')) throw new Error(`Binary files are not supported (${path}).`);
    totalBytes += bytes;
    if (totalBytes > MAX_TOTAL_BYTES) throw new Error('Project text files must total 500 KB or less.');
    projectFiles.push({ path, content });
  }
  return { prompt: normalizedPrompt, files: projectFiles };
}
