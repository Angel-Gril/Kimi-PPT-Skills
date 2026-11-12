import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = await readFile(new URL('../skills/open-kimi-ppt/scripts/export_host.html', import.meta.url), 'utf8');
const source = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1]
  .replace(/import\s*\{[\s\S]*?\}\s*from\s*"[^"]+";/, '');

async function host(search, artifactStatus = 200) {
  let onLoad;
  let methods;
  const calls = [];
  const remote = {
    async setSlideConfig(config) { calls.push(['config', config]); },
    async setPPTD() {},
    async setEditable(value) { calls.push(['editable', value]); },
    async getSlideStatus() { return {}; },
  };
  const frame = { allow: 'fullscreen', contentWindow: {}, addEventListener(_event, fn) { onLoad = fn; } };
  const document = { documentElement: { dataset: {} }, querySelector: id => id === '#editor' ? frame : {} };
  const payload = { id: 'test', pages: [{}], manifestContent: 'version: v2', manifestPath: 'deck.pptd' };
  const context = {
    document, URL, URLSearchParams, window: {}, location: { search, href: 'http://127.0.0.1/export_host.html' + search },
    WindowMessenger: class {},
    FileReader: class {
      readAsDataURL() { this.result = 'data:application/octet-stream;base64,YXJ0aWZhY3Q='; this.onload(); }
    },
    connect(options) { methods = options.methods; return { promise: Promise.resolve(remote) }; },
    fetch: async url => url !== './payload.json'
      ? { ok: artifactStatus === 200, status: artifactStatus, blob: async () => ({ size: 4911 }) }
      : { ok: true, json: async () => payload },
  };
  await runInNewContext(`(async () => {${source}})()`, context);
  const editorUrl = new URL(frame.src);
  await onLoad();
  assert.equal(document.documentElement.dataset.deckStatus, 'ready');
  return { editorUrl, calls, methods, allow: frame.allow };
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
