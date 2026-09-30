"""Native input ownership: run real drawing gesture adapter without a browser."""
import unittest
from pathlib import Path

from js_test_util import require_node, run_node


class DrawInputTest(unittest.TestCase):
    def test_pointer_lifecycle(self):
        require_node()
        path = Path(__file__).resolve().parents[1] / 'static/js/draw-input.js'
        self.assertEqual(run_node(r"""
const assert = require('node:assert/strict');
const {bind} = require(process.argv[1]);
function setup() {
  const handlers = {}, captures = new Set(), log = [];
  const state = {enabled: true, owns: true, creating: true, key: 'A:1d'};
  const el = {clientWidth: 800, clientHeight: 400,
    getBoundingClientRect: () => ({left: 20, top: 40, width: 800, height: 400}),
    addEventListener: (k, f) => {handlers[k] = f;}, removeEventListener: k => delete handlers[k],
    setPointerCapture: id => captures.add(id), hasPointerCapture: id => captures.has(id),
    releasePointerCapture: id => captures.delete(id)};
  const api = Object.fromEntries(['enabled','owns','creating','key'].map(k=>[k,()=>state[k]]));
  for (const k of ['down','move','up','cancel']) api[k] = e => log.push([k, e && e.offsetX]);
  const adapter = bind(el, api);
  const send = (type, extra={}) => {
    const e = {pointerId:1, pointerType:'touch', button:0, isPrimary:true, clientX:120, clientY:140,
      preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;},...extra};
    if (handlers[type]) handlers[type](e);
    return e;
  };
  return {state,log,adapter,send,captures,handlers,el};
}
let t = setup();
assert.ok(t.send('pointerdown').stopped);
assert.deepEqual(t.log, [['move',100]]); // no early commit
t.send('pointermove',{clientX:220});
assert.ok(t.send('touchstart').stopped);
t.send('pointerup',{clientX:240});
assert.deepEqual(t.log.slice(-3), [['move',220],['down',220],['up',220]]);
assert.ok(t.send('mousedown').stopped); // compatibility mouse cannot add another point
assert.equal(t.captures.size,0);
t = setup(); t.send('pointerdown',{pointerType:'mouse'});
assert.equal(t.log[0][0],'down'); // desktop still places on mouse down
t.send('pointerup',{pointerType:'mouse'});
assert.equal(t.log.filter(e=>e[0]==='down').length,1);
t = setup(); t.state.creating=false; t.send('pointerdown');
assert.equal(t.log[0][0],'down'); // existing endpoint starts drag immediately
t.send('pointercancel'); assert.equal(t.log.at(-1)[0],'cancel');
assert.equal(t.captures.size,0);
t = setup(); t.send('pointerdown');
const second = t.send('pointerdown',{pointerId:2,isPrimary:false});
assert.ok(!second.stopped); // second finger is handed to the chart for pinch
assert.equal(t.log.at(-1)[0],'cancel');
assert.ok(!t.log.some(e=>e[0]==='down')); // the unfinished point is not committed
assert.ok(!t.send('touchmove',{pointerId:2}).stopped);
assert.equal(t.captures.size,0);
t = setup(); t.send('pointerdown');
t.send('lostpointercapture'); assert.equal(t.log.at(-1)[0],'cancel');
t = setup(); t.send('pointerdown'); t.state.key='B:1d'; t.send('pointerup');
assert.equal(t.log.at(-1)[0],'cancel'); assert.ok(!t.log.some(e=>e[0]==='down'));
t = setup(); t.state.owns=false;
assert.ok(!t.send('pointerdown').stopped); assert.deepEqual(t.log,[]);
t = setup(); t.state.enabled=false;
assert.ok(!t.send('pointerdown').stopped);
t = setup(); t.send('pointerdown'); t.adapter.dispose();
assert.equal(t.captures.size,0); assert.equal(t.log.at(-1)[0],'cancel');
t = setup(); t.send('pointerdown'); t.send('pointerup');
assert.equal(t.handlers.dblclick, undefined);
assert.ok(!t.send('dblclick').stopped); // double-click still finishes a polyline / edits an hline
t = setup();
t.el.setPointerCapture = () => { throw new Error('InvalidPointerId'); };
assert.ok(!t.send('pointerdown').stopped);
assert.deepEqual(t.log, []);
process.stdout.write('ok');
""", str(path)), 'ok')


if __name__ == '__main__':
    unittest.main()
