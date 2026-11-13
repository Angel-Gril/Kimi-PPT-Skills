import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = await readFile(new URL('../skills/open-kimi-ppt/scripts/export_host.html', import.meta.url), 'utf8');
const source = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1]
  .replace(/import\s*\{[\s\S]*?\}\s*from\s*"[^"]+";/, '');

async function host(search, artifactStatus = 200, livePageCount = '2') {
  let onLoad;
  let methods;
  const calls = [];
  const requests = [];
  const remote = {
    async setSlideConfig(config) { calls.push(['config', config]); },
    async setPPTD() {},
    async setEditable(value) { calls.push(['editable', value]); },
    async getSlideStatus() { return {}; },
  };
  const frame = { allow: 'fullscreen', contentWindow: {}, addEventListener(_event, fn) { onLoad = fn; } };
  const document = { documentElement: { dataset: {} }, querySelector: id => id === '#editor' ? frame : {} };
  const payload = {
    id: 'test', pages: [{ path: 'pages/01.page', content: 'elements: []' }],
    manifestContent: 'version: v2', manifestPath: 'deck.pptd', renderToken: 'test-session',
  };
  const context = {
    document, URL, URLSearchParams, window: {}, location: { search, href: 'http://127.0.0.1/export_host.html' + search },
    WindowMessenger: class {},
    FileReader: class {
      readAsDataURL() { this.result = 'data:application/octet-stream;base64,YXJ0aWZhY3Q='; this.onload(); }
    },
    connect(options) { methods = options.methods; return { promise: Promise.resolve(remote) }; },
    fetch: async (url, options) => {
      requests.push({ url, options });
      return url !== './payload.json'
        ? {
          ok: artifactStatus === 200, status: artifactStatus,
          blob: async () => ({ size: 4911 }),
          headers: { get: name => name === 'X-Render-Pages' ? livePageCount : null },
        }
        : { ok: true, json: async () => payload };
    },
  };
  await runInNewContext(`(async () => {${source}})()`, context);
  const editorUrl = new URL(frame.src);
  await onLoad();
  assert.equal(document.documentElement.dataset.deckStatus, 'ready');
  return { editorUrl, calls, methods, requests, payload, allow: frame.allow };
}

test('default export keeps the official editable path', async () => {
  const result = await host('');
  assert.equal(result.editorUrl.searchParams.has('sdkExportMode'), false);
  assert.equal('handleExport' in result.methods, false);
  assert.equal(result.allow, 'fullscreen');
  assert.equal(result.calls.find(call => call[0] === 'editable')[1], true);
});

test('optional host export serves a frozen artifact with actual size', async () => {
  const result = await host('?hostExport=1');
  assert.equal(result.editorUrl.searchParams.get('sdkExportMode'), 'external');
  assert.equal(result.allow, 'fullscreen');
  // Kimi disables its export button in read-only mode.
  assert.equal(result.calls.find(call => call[0] === 'config')[1].editable, true);
  assert.equal(result.calls.find(call => call[0] === 'editable')[1], true);
  const artifact = await result.methods.handleExport();
  assert.equal(artifact.downloadUrl, 'data:application/octet-stream;base64,YXJ0aWZhY3Q=');
  assert.equal(artifact.sizeBytes, 4911);
  assert.equal(artifact.pageCount, 1);
});

test('unused host artifact URL cannot break the default export', async () => {
  const result = await host('?pptx=http://[');
  assert.equal('handleExport' in result.methods, false);
});

test('missing host artifact rejects the RPC instead of reporting success', async () => {
  const result = await host('?hostExport=1', 404);
  await assert.rejects(result.methods.handleExport(), /host PPTX HTTP 404/);
});

test('live export sends the latest saved files with the session token and current page count', async () => {
  const result = await host('?hostExport=1&render=1');
  const saved = {
    fileContent: [
      { path: 'deck.pptd', content: 'version: v2\npages: [pages/01.page, pages/02.page]' },
      { path: 'pages/01.page', content: 'edited first page' },
      { path: 'pages/02.page', content: 'new second page' },
    ],
    title: 'Edited deck',
  };
  result.methods.onSave(saved);
  const artifact = await result.methods.handleExport();
  const request = result.requests.find(item => item.url === './render-pptx');
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.headers['X-Render-Token'], result.payload.renderToken);
  assert.deepEqual(JSON.parse(request.options.body), { files: saved.fileContent, title: saved.title });
  assert.equal(artifact.pageCount, 2);
  assert.equal(result.requests.some(item => item.url.includes('deck.pptx')), false);
});

test('live export renders the original files when no save has occurred', async () => {
  const result = await host('?hostExport=1&render=1', 200, '1');
  const artifact = await result.methods.handleExport();
  const request = result.requests.find(item => item.url === './render-pptx');
  const deck = JSON.parse(request.options.body);
  assert.equal(deck.files[0].path, result.payload.manifestPath);
  assert.equal(deck.files[0].content, result.payload.manifestContent);
  assert.deepEqual(deck.files.slice(1), result.payload.pages);
  assert.equal(artifact.pageCount, 1);
});

test('live export rejects invalid rendering metadata and HTTP failures', async () => {
  for (const count of [null, '0', '-1', '1.5', 'not a count']) {
    const result = await host('?hostExport=1&render=1', 200, count);
    await assert.rejects(result.methods.handleExport(), /invalid live render page count/);
  }
  const failed = await host('?hostExport=1&render=1', 400);
  await assert.rejects(failed.methods.handleExport(), /live render HTTP 400/);
});

test('static export ignores later editor saves and the live endpoint', async () => {
  const result = await host('?hostExport=1&render=0');
  result.methods.onSave({ fileContent: [{ path: 'deck.pptd', content: 'changed' }] });
  const artifact = await result.methods.handleExport();
  assert.equal(artifact.pageCount, 1);
  assert.equal(result.requests.some(item => item.url === './render-pptx'), false);
});
