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
  const lineEnd = html.indexOf('\n', bodyStart);
  const oneLine = html.lastIndexOf('},', lineEnd);
  const multiLine = html.indexOf('\n      },', bodyStart);
  const end = oneLine > bodyStart ? oneLine : multiLine;
  if (end < 0) throw new Error('could not find the end of ' + signature);
  return html.slice(bodyStart, end);
}

function check(condition, message) {
  if (!condition) throw new Error(message);
}

globalThis.marked = { parse() { return ''; } };
globalThis.DOMPurify = { sanitize() { return ''; } };
const storage = new Map();
globalThis.localStorage = {
  getItem(key) { return storage.has(key) ? storage.get(key) : null; },
  setItem(key, value) { storage.set(key, String(value)); },
};

const app = {
  wpSource: 'project',
  wpViewMode: 'preview',
  wpText: '# Hello',
  wpEtag: 'one',
  wpUpdatedAt: 'old',
  wpLiveStatus: 'live',
  wpUpdatedFlash: false,
  wpEventSource: null,
  wpPollTimer: null,
  wpReconnectTimer: null,
  wpFlashTimer: null,
  wpPollToken: 0,
  $refs: { wpScroll: { scrollTop: 20, scrollHeight: 220, clientHeight: 100 } },
  $nextTick(callback) { callback(); },
  renderMarkdown(text) { return '<h1>' + text.replace(/^# /, '') + '</h1>'; },
};

for (const [name, signature] of [
  ['setWPView', 'setWPView(mode)'],
  ['wpDisplayHTML', 'wpDisplayHTML()'],
  ['applyProjectWPUpdate', 'applyProjectWPUpdate(payload)'],
  ['stopProjectWPLive', 'stopProjectWPLive()'],
  ['closeProjectWPLive', 'closeProjectWPLive()'],
  ['wpLiveLabel', 'wpLiveLabel()'],
  ['wpLiveDotClass', 'wpLiveDotClass()'],
  ['wpLiveTextClass', 'wpLiveTextClass()'],
  ['connectProjectWPStream', 'connectProjectWPStream(projectId)'],
]) {
  const args = signature.slice(signature.indexOf('(') + 1, signature.lastIndexOf(')'));
  app[name] = new Function(args, extract(signature)).bind(app);
}

check(app.wpDisplayHTML().includes('<h1>Hello</h1>'), 'project preview did not render markdown');
app.setWPView('source');
check(app.wpViewMode === 'source', 'setWPView did not switch to source');
check(storage.get('ipc.wp.viewMode') === 'source', 'view mode was not persisted');
check(app.wpDisplayHTML() === '', 'source mode still produced preview markup');
app.setWPView('preview');
check(storage.get('ipc.wp.viewMode') === 'preview', 'preview mode was not persisted');

app.wpSource = 'global';
app.wpViewMode = 'preview';
app.setWPView('source');
check(app.wpViewMode === 'preview', 'global WP must not accept a view mode change');

app.wpSource = 'project';
app.wpEtag = 'one';
app.wpText = 'old';
app.applyProjectWPUpdate({etag: 'one', content: 'ignored'});
check(app.wpText === 'old', 'an identical etag replaced the DOM');

app.wpText = 'old';
app.$refs.wpScroll = { scrollTop: 10, scrollHeight: 120, clientHeight: 100 };
app.applyProjectWPUpdate({etag: 'two', content: 'new', updated_at: 'now'});
check(app.wpText === 'new' && app.wpEtag === 'two', 'new etag did not update the writeup');
check(app.$refs.wpScroll.scrollTop === 120, 'reader at the bottom did not follow the update');
check(app.wpUpdatedFlash === true, 'updated indicator was not raised');

app.$refs.wpScroll = { scrollTop: 40, scrollHeight: 300, clientHeight: 100 };
app.applyProjectWPUpdate({etag: 'three', content: 'newer', updated_at: 'later'});
check(app.$refs.wpScroll.scrollTop === 40, 'scroll position was not preserved');

let closed = 0;
app.wpEventSource = { close() { closed += 1; } };
app.wpPollTimer = 17;
app.wpReconnectTimer = 19;
app.wpFlashTimer = 23;
app.stopProjectWPLive();
check(closed === 1 && app.wpEventSource === null, 'event source was not closed');
check(app.wpPollTimer === null && app.wpReconnectTimer === null && app.wpFlashTimer === null, 'timers were not cleared');
app.closeProjectWPLive();
check(app.wpLiveStatus === 'offline', 'close did not reset live status');
check(app.wpLiveLabel() === '离线', 'offline label is wrong');
app.wpLiveStatus = 'connecting';
check(app.wpLiveDotClass().includes('animate-pulse'), 'connecting dot is not animated');
const sources = [];
class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.listeners = {};
    this.closed = false;
    sources.push(this);
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  close() { this.closed = true; }
}
globalThis.EventSource = FakeEventSource;

app.wpSource = 'project';
app.wpProjectId = 'proj-1';
app.showWP = true;
app.wpEventSource = null;
app.connectProjectWPStream('proj-1');
check(sources.length === 1, 'project stream did not create an EventSource');
check(sources[0].url.includes('/projects/proj-1/wp/stream'), 'wrong stream URL');
check(typeof sources[0].listeners.writeup === 'function', 'writeup listener missing');
check(typeof sources[0].listeners.missing === 'function', 'missing listener missing');
sources[0].listeners.writeup({data: JSON.stringify({etag: 'four', content: 'streamed'})});
check(app.wpText === 'streamed', 'stream event did not update the writeup');
sources[0].listeners.missing({data: '{}'});
check(sources[0].closed, 'missing event did not close the stream');

app.wpSource = 'global';
app.wpProjectId = 'proj-2';
app.connectProjectWPStream('proj-2');
check(sources.length === 1, 'global WP created an EventSource');
console.log('ok');
