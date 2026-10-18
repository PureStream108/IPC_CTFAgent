import fs from 'node:fs';
import path from 'node:path';

const root = process.argv[2] || process.cwd();
const html = fs
  .readFileSync(path.join(root, 'frontend/index.html'), 'utf8')
  .split('\r\n')
  .join('\n');

function extract(signature) {
  const marker = '\n      ' + signature + '{';
  const start = html.indexOf(marker);
  if (start < 0) throw new Error('could not find ' + signature);
  const bodyStart = start + marker.length;
  const multiLine = html.indexOf('\n      },', bodyStart);
  const lineEnd = html.indexOf('\n', bodyStart);
  const oneLine = html.lastIndexOf('},', lineEnd);
  const end = oneLine > bodyStart ? oneLine : multiLine;
  if (end < 0) throw new Error('could not find the end of ' + signature);
  return html.slice(bodyStart, end);
}

function check(condition, message) {
  if (!condition) throw new Error(message);
}


const renderMarkdown = new Function(
  'text', 'marked', 'DOMPurify', extract('renderMarkdown(text)')
);

new Function(fs.readFileSync(path.join(root, 'frontend/vendor/marked.min.js'), 'utf8'))
  .call(globalThis);
const marked = globalThis.marked;
check(marked && typeof marked.parse === 'function', 'vendored marked exposes no parse()');

let sanitizerConfig = null;
const DOMPurify = {
  sanitize(dirty, config) {
    sanitizerConfig = config;
    return dirty;
  },
};

const markdownSource = [
  '# Title', '', '**bold** and `code`', '', '- a', '- b', '',
  '| x | y |', '|---|---|', '| 1 | 2 |', '',
  '```python', 'print(1)', '```', '', '[l](https://e.invalid)',
].join('\n');
const rendered = renderMarkdown(markdownSource, marked, DOMPurify);
for (const needle of ['<h1', '<strong>', '<code>', '<ul>', '<table>', '<pre>', 'href="https://e.invalid"']) {
  check(rendered.includes(needle), 'markdown output is missing ' + needle);
}
check(sanitizerConfig !== null, 'DOMPurify.sanitize was never called');
for (const tag of ['script', 'iframe', 'object', 'embed', 'form', 'input']) {
  check(sanitizerConfig.FORBID_TAGS.includes(tag), 'tag not forbidden: ' + tag);
}
check(sanitizerConfig.ALLOW_DATA_ATTR === false, 'data attributes are still allowed');
check(sanitizerConfig.FORBID_ATTR.includes('style'), 'the style attribute is not forbidden');
for (const empty of ['', null, undefined]) {
  check(renderMarkdown(empty, marked, DOMPurify) === '', 'empty input produced markup');
}
const fallback = renderMarkdown('**b** <script>x</script>', undefined, undefined);
check(!fallback.includes('<script>'), 'the fallback path did not escape html');
check(fallback.includes('&lt;script&gt;'), 'the fallback path lost the escaped text');


const started = [];
const app = {
  opsInput: '',
  opsAttachments: [],
  opsBusy: false,
  opsStreaming: false,
  opsNextCalls: [],
  opsCfg: { configured: true },
  interruptCalls: 0,
  resetOpsInputHeight() {},
  async interruptOpsRun() { this.interruptCalls += 1; },
  async runOpsMessage(message) {
    started.push(message);
    this.opsBusy = true;
    this.opsStreaming = true;
  },
};
for (const [name, signature] of [
  ['sendOpsMessage', 'async sendOpsMessage()'],
  ['drainOpsNextCalls', 'async drainOpsNextCalls()'],
]) {
  app[name] = new Function('return async function(){' + extract(signature) + '};')().bind(app);
}
app.removeOpsNextCall = new Function(
  'id', extract('removeOpsNextCall(id)')
).bind(app);

app.opsInput = 'first';
await app.sendOpsMessage();
check(started.join() === 'first', 'the first message did not start: ' + started);
check(app.opsInput === '', 'the input box was not cleared');

app.opsInput = 'second';
await app.sendOpsMessage();
app.opsInput = 'third';
await app.sendOpsMessage();
check(app.interruptCalls === 0, 'queueing a message must never interrupt the live run');
check(started.length === 1, 'a queued message was sent while a run was in flight');
check(app.opsNextCalls.length === 2, 'queue length is ' + app.opsNextCalls.length);

await app.drainOpsNextCalls();
check(started.length === 1, 'the queue drained while a run was still in flight');

app.opsBusy = false;
app.opsStreaming = false;
await app.drainOpsNextCalls();
check(started.join() === 'first,second', 'wrong drain order: ' + started);
check(app.opsNextCalls.length === 1, 'more than one queued message was sent at once');

app.opsBusy = false;
app.opsStreaming = false;
app.removeOpsNextCall(app.opsNextCalls[0].id);
check(app.opsNextCalls.length === 0, 'a queued message was not removed');
await app.drainOpsNextCalls();
check(started.join() === 'first,second', 'a removed message was still sent');


const pentestFactory = new Function(fs.readFileSync(path.join(root, 'frontend/pentest.js'), 'utf8') + '\nreturn pentestWorkspace;')();
const sidebar = {
  ...pentestFactory(),
  projectStatusFilter: [],
  projects: [],
  projectStatusGroups: new Function(
    'return ' + html.match(/projectStatusGroups: (\[[\s\S]*?\]),/)[1]
  )(),
};
for (const [name, signature] of [
  ['projectStatusGroup', 'projectStatusGroup(status)'],
  ['projectStatusRank', 'projectStatusRank(status)'],
  ['projectStatusGroupCount', 'projectStatusGroupCount(key)'],
  ['toggleProjectStatusFilter', 'toggleProjectStatusFilter(key)'],
  ['filteredProjects', 'filteredProjects()'],
]) {
  const argument = signature.slice(signature.indexOf('(') + 1, signature.lastIndexOf(')'));
  sidebar[name] = new Function(argument, extract(signature)).bind(sidebar);
}

const groupKeys = sidebar.projectStatusGroups.map((group) => group.key);
check(groupKeys.join() === 'solved,running,created,stopped', 'unexpected groups: ' + groupKeys);
for (const [status, expected] of [
  ['solved', 'solved'], ['completed', 'solved'], ['flag_found', 'solved'], ['running', 'running'],
  ['created', 'created'], ['stopped', 'stopped'], ['timeout', 'stopped'],
  ['infra_error', 'stopped'], ['failed', 'stopped'],
]) {
  check(
    sidebar.projectStatusGroup(status) === expected,
    status + ' grouped as ' + sidebar.projectStatusGroup(status)
  );
}

sidebar.projects = [
  { id: 'proj_001', status: 'stopped' },
  { id: 'proj_002', status: 'created' },
  { id: 'proj_003', status: 'running' },
  { id: 'proj_004', status: 'solved' },
  { id: 'proj_005', status: 'flag_found' },
  { id: 'proj_006', status: 'failed' },
];
const ordered = [...sidebar.projects].sort(
  (a, b) => (sidebar.projectStatusRank(a.status) - sidebar.projectStatusRank(b.status))
    || (a.id < b.id ? 1 : -1)
);
check(
  ordered.map((p) => p.id).join() === 'proj_005,proj_004,proj_003,proj_002,proj_006,proj_001',
  'wrong project order: ' + ordered.map((p) => p.id + ':' + p.status).join()
);

check(sidebar.filteredProjects().length === 6, 'no filter must show every project');
sidebar.toggleProjectStatusFilter('solved');
check(sidebar.filteredProjects().map((p) => p.id).join() === 'proj_004,proj_005', 'solved filter wrong');
sidebar.toggleProjectStatusFilter('running');
check(sidebar.filteredProjects().length === 3, 'two selected groups should show three projects');
sidebar.toggleProjectStatusFilter('solved');
check(sidebar.filteredProjects().map((p) => p.id).join() === 'proj_003', 'deselecting a group failed');
check(sidebar.projectStatusGroupCount('stopped') === 2, 'stopped count should include failed');

sidebar.projectStatusFilter = [];
sidebar.projects.push({id: 'pentest-1', category: 'pentest', status: 'completed'});
check(!sidebar.filteredProjects().some(p=>p.category==='pentest'), 'CTF workspace leaked pentest task');
sidebar.workspaceMode = 'pentest';
check(sidebar.filteredProjects().map(p=>p.id).join() === 'pentest-1', 'Pentest workspace leaked CTF task');
check(sidebar.projectStatusGroupCount('solved') === 1, 'completed pentest not counted');
console.log('ok');
