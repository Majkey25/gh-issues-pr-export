import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../docs/script.js', import.meta.url), 'utf8');
const response = (body, status = 200) => new Response(JSON.stringify(body), { status });
const turn = () => new Promise(resolve => setImmediate(resolve));

// Only DOM plumbing is stubbed. Tests execute the shipped request/lifecycle functions.
function app(transport) {
  const nodes = new Map();
  function element(id) {
    if (!nodes.has(id)) nodes.set(id, {
      value: '', checked: false, disabled: false, textContent: '', children: [],
      classList: { toggle() {} },
      addEventListener() {},
      replaceChildren() { this.children = []; },
      prepend(child) { this.children.unshift(child); },
    });
    return nodes.get(id);
  }
  const context = vm.createContext({
    AbortController, URL,
    window: { matchMedia: () => ({ matches: false, addEventListener() {} }) },
    document: {
      querySelectorAll: () => [], getElementById: element, querySelector: element,
      createElement: () => ({ textContent: '' }),
    },
    fetch: async (url, options) => {
      const parsed = new URL(url);
      assert.equal(parsed.origin, 'https://api.github.com');
      return transport(parsed.pathname, options.signal);
    },
  });
  vm.runInContext(source, context, { filename: 'docs/script.js' });
  element('repo-input').value = 'fixture/example';
  return { run: () => context.runExport(false), element };
}

test('issue and PR list requests overlap after metadata validation', async () => {
  const issue = Promise.withResolvers();
  const seen = [];
  const instance = app(path => {
    seen.push(path);
    if (path.endsWith('/issues')) return issue.promise;
    return response(path.endsWith('/pulls') ? [] : { name: 'example' });
  });
  const running = instance.run();
  await turn();
  const beforeIssueCompletes = [...seen];
  issue.resolve(response([{ number: 1 }]));
  await running;
  assert.deepEqual(beforeIssueCompletes, [
    '/repos/fixture/example', '/repos/fixture/example/issues', '/repos/fixture/example/pulls',
  ]);
  assert.equal(instance.element('summary-issues').textContent, '1');
  assert.equal(instance.element('export-status').textContent, 'Preview ready.');
});

test('list failure cancels its sibling and a fresh retry succeeds', async () => {
  let fail = true;
  let aborted = 0;
  const signals = [];
  const instance = app((path, signal) => {
    signals.push(signal);
    if (path.endsWith('/issues')) return response(fail ? { message: 'Fixture failure' } : [{ number: 1 }], fail ? 500 : 200);
    if (path.endsWith('/pulls') && fail) return new Promise((_, reject) => {
      signal.addEventListener('abort', () => { aborted++; reject(signal.reason); }, { once: true });
    });
    return response(path.endsWith('/pulls') ? [] : { name: 'example' });
  });
  await instance.run();
  assert.equal(aborted, 1);
  assert.match(instance.element('export-status').textContent, /GitHub API 500: Fixture failure/);
  assert.equal(instance.element('summary-issues').textContent, '—');
  assert.equal(instance.element('preview-button').disabled, false);
  const previousSignal = signals[0];
  fail = false;
  await instance.run();
  assert.notEqual(signals.at(-1), previousSignal);
  assert.equal(instance.element('summary-issues').textContent, '1');
  assert.equal(instance.element('export-status').textContent, 'Preview ready.');
});

test('a comment failure aborts active workers without starting queued work', async () => {
  let commentRequests = 0, aborted = 0;
  const instance = app((path, signal) => {
    if (path.endsWith('/issues')) return response(Array.from({ length: 5 }, (_, index) => ({ number: index + 1 })));
    if (path.endsWith('/pulls')) return response([]);
    if (!path.endsWith('/comments')) return response({ name: 'example' });
    commentRequests++;
    if (path.endsWith('/issues/1/comments')) return response({ message: 'Comment failure' }, 500);
    return new Promise((_, reject) => {
      signal.addEventListener('abort', () => { aborted++; reject(signal.reason); }, { once: true });
    });
  });
  instance.element('comments-input').checked = true;
  await instance.run();
  assert.equal(commentRequests, 4);
  assert.equal(aborted, 3);
  assert.match(instance.element('export-status').textContent, /GitHub API 500: Comment failure/);
  assert.equal(instance.element('summary-comments').textContent, '—');
  assert.equal(instance.element('download-button').disabled, false);
});
