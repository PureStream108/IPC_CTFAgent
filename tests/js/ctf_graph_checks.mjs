import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = process.argv[2] || process.cwd();
const html = fs.readFileSync(path.join(root, 'frontend/index.html'), 'utf8');
const start = html.indexOf('function ipcApp()');
const source = html.slice(start, html.indexOf('</script>', start));
const factory = new Function('pentestWorkspace', source + '\nreturn ipcApp;')(() => ({}));
const app = factory();
app.workspaceMode = 'ctf';
app.selectedId = 'ctf-demo';
app.current = {
  agents: [
    {name: 'ipc', role: 'ipc', state: 'active'},
    {name: 'diamond', role: 'diamond', state: 'active'},
    {name: 'sugilite', role: 'member', start_fact_id: 'f1', state: 'active'},
  ],
  facts: [{id: 'origin', description: 'Start'}, {id: 'f1', description: 'Verified evidence'}],
  intents: [
    {id: 'i1', from: ['origin'], to: 'f1', description: 'Verify the fixture', creator: 'diamond', worker: 'sugilite'},
    {id: 'i2', from: ['f1'], to: null, description: 'Explore another direction', creator: 'diamond', worker: null},
  ],
  agent_links: [
    {id: 'a', src: 'ipc', dst: 'diamond', kind: 'assign'},
    {id: 'b', src: 'ipc', dst: 'diamond', kind: 'report'},
    {id: 'c', src: 'diamond', dst: 'sugilite', kind: 'assign'},
  ],
};
const elements = app.buildElements();
assert.equal(elements.edges.find(edge => edge.data.etype === 'context').data.curveStyle, 'taxi');
assert.equal(elements.edges.find(edge => edge.data.etype === 'explore').data.curveStyle, 'unbundled-bezier');
assert.equal(elements.edges.find(edge => edge.data.etype === 'create').data.curveStyle, 'bezier');
const parallel = elements.edges.filter(edge => ['a', 'b'].map(id => 'L' + id).includes(edge.data.id));
assert.equal(parallel.length, 2);
assert.notDeepEqual(parallel[0].data.curveDistances, parallel[1].data.curveDistances);
for (const edge of elements.edges) {
  assert.ok(edge.data.lineColor);
  assert.ok(edge.data.curveDistances.every(Number.isFinite));
  assert.deepEqual(edge.data.curveWeights, [0.5]);
}
const styles = app.graphStyles();
assert.ok(styles.some(rule => rule.selector === 'node[ntype="member"]' && rule.style.shape === 'hexagon'));
assert.ok(styles.some(rule => rule.selector === 'edge[etype="orch"]' && rule.style['line-dash-pattern'].length === 2));
assert.ok(styles.some(rule => rule.selector === 'node.is-dragging'));
assert.ok(styles.some(rule => rule.selector === 'node.manual'));
assert.deepEqual(app.nodeSize('FLAG', 'flag'), {width: 64, height: 64});
assert.ok(app.nodeSize('intent: one\nC: diamond\nW: sugilite', 'in_progress').height >= 72);

const node = {
  id: () => 'ipc',
  point: {x: 100, y: 120},
  position(value) { if (value) this.point = value; return this.point; },
  addClass(value) { this.classes = value; },
};
app.rememberCtfNodePosition(node);
assert.equal(app.ctfManualPositionCount(), 1);
app.selectedId = 'another-project';
assert.equal(app.ctfManualPositionCount(), 0);
app.selectedId = 'ctf-demo';
assert.equal(app.ctfManualPositionCount(), 1);
app.cy = {getElementById: () => node, fit: () => {}, nodes: () => ({removeClass() {}})};
app.restoreCtfManualPositions();
assert.deepEqual(node.point, {x: 100, y: 120});
app.relayoutCtf(false, true);
assert.deepEqual(node.point, {x: 100, y: 120});
let layoutOptions;
app.cy.layout = options => { layoutOptions = options; return {run() {}}; };
app.resetCtfLayout();
assert.equal(app.ctfManualPositionCount(), 0);
assert.equal(layoutOptions.fit, true);

const callbacks = new Map();
let frameId = 0;
globalThis.window = {
  requestAnimationFrame(callback) { callbacks.set(++frameId, callback); return frameId; },
  cancelAnimationFrame(id) { callbacks.delete(id); },
};
globalThis.document = {hidden: false};
const offsets = [];
app.cy = {edges: () => ({style: (_name, value) => offsets.push(value)})};
app.edgeFlowEnabled = true;
app.ctfReducedMotion = false;
app.startEdgeFlow();
assert.equal(callbacks.size, 1);
const [id, frame] = callbacks.entries().next().value;
callbacks.delete(id);
frame(1000);
assert.ok(offsets.at(-1) < 0);
app.toggleEdgeFlow();
assert.equal(app._flowFrame, null);
assert.equal(callbacks.size, 0);
assert.equal(app.edgeFlowEnabled, false);
app.startEdgeFlow();
assert.equal(callbacks.size, 0);
app.ctfReducedMotion = true;
app.toggleEdgeFlow();
assert.equal(app.edgeFlowEnabled, false);
assert.equal(callbacks.size, 0);
console.log('ctf graph checks passed');
