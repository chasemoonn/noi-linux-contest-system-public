import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import { installEchoProbe, measureRemoteEcho, focusRemoteEcho } from '../echo-probe.mjs';

const html=fs.readFileSync(new URL('../remote-echo.html',import.meta.url),'utf8');
const remoteScript=html.match(/<script>([\s\S]*?)<\/script>/)[1];

// Separate VM contexts model the remote application and the observing browser.
// Only this test transport copies pixels; the observer has no drawing capability.
function harness() {
  let now=0, remote, current;
  const timers=new Map(), listeners=new Map();
  const framebuffer=new Uint8ClampedArray(800*128*4);
  const canvas={width:800,height:128,getContext:()=>({getImageData(x,y,w,h){
    const data=new Uint8ClampedArray(w*h*4);
    for(let row=0;row<h;row++) data.set(framebuffer.subarray(((y+row)*800+x)*4,((y+row)*800+x+w)*4),row*w*4);
    return {data};
  }})};
  current=canvas;
  const client=vm.createContext({document:{querySelector:()=>current},
    window:{addEventListener:(name,fn)=>listeners.set(name,fn)},
    performance:{now:()=>now},setInterval:fn=>{const id=Symbol();timers.set(id,fn);return id;},
    clearInterval:id=>timers.delete(id)});
  vm.runInContext(`(${installEchoProbe.toString()})({canvasSelector:'canvas'})`,client);
  const probe=client.window.__noiEchoProbe;
  function resetRemote(seed=123) {
    const pixels=new Uint8ClampedArray(608*16*4), events=new Map();
    const ctx={fillStyle:'',fillRect(x,y,w,h){
      const rgb=this.fillStyle.match(/\d+/g).map(Number);
      for(let yy=y;yy<y+h;yy++) for(let xx=x;xx<x+w;xx++) pixels.set([...rgb,255],(yy*608+xx)*4);
    }};
    const app=vm.createContext({document:{getElementById:()=>({getContext:()=>ctx}),body:{focus(){}}},
      crypto:{getRandomValues:array=>{array[0]=seed;return array;}},
      window:{addEventListener:(name,fn)=>events.set(name,fn)}});
    vm.runInContext(remoteScript,app);
    remote={pixels,events,app};
    paint();
  }
  function paint() {
    for(let y=0;y<16;y++) framebuffer.set(remote.pixels.subarray(y*608*4,(y+1)*608*4),((32+y)*800+24)*4);
  }
  const event=(key,extra={})=>({key,target:canvas,isTrusted:true,repeat:false,
    ctrlKey:false,altKey:false,metaKey:false,shiftKey:false,preventDefault(){},...extra});
  function tick(ms) {now+=ms;for(const fn of [...timers.values()]) fn();}
  const dom=(key,extra)=>listeners.get('keydown')(event(key,extra));
  const remoteKey=(key,extra)=>{remote.events.get('keydown')(event(key,extra));paint();};
  resetRemote();
  return {probe,canvas,framebuffer,timers,tick,dom,remoteKey,paint,resetRemote,client,
    replaceCanvas:()=>{current={...canvas};},
    setCounter:n=>{vm.runInContext(`counter=${n};draw()`,remote.app);paint();},
    arm:key=>probe.arm(key,probe.locate(),250)};
}

test('observer decodes the actual remote HTML and times from DOM keydown, not arm',()=>{
  const h=harness(); h.arm('a');h.tick(80);h.dom('a');h.tick(30);
  assert.equal(h.probe.take(),null);
  h.remoteKey('a');h.tick(20);
  assert.equal(h.probe.take().ok,true);
  assert.equal(h.probe.take().latency_ms,50);
  assert.equal(h.probe.take().counter,1);
  assert.equal(h.timers.size,0);
});

test('cursor blink, unrelated paint and unchanged remote redraw do not acknowledge',()=>{
  const h=harness(); h.arm('a');h.dom('a');
  for(let i=0;i<4;i++) {
    h.framebuffer[i*4]=255;h.paint();h.tick(40);assert.equal(h.probe.take(),null);
  }
  h.tick(90);assert.equal(h.probe.take().ok,false);assert.match(h.probe.take().error,/timed out/);
});

test('remote acknowledgement before the measured DOM key is rejected',()=>{
  const h=harness();h.arm('a');h.remoteKey('a');h.tick(10);
  assert.equal(h.probe.take().ok,false);assert.match(h.probe.take().error,/unexpected/);
});

test('wrong key and skipped counters cannot produce a latency sample',()=>{
  for(const keys of [['b'],['a','a']]) {
    const h=harness();h.arm('a');h.dom('a');keys.forEach(key=>h.remoteKey(key));h.tick(10);
    assert.equal(h.probe.take().ok,false);
  }
});

test('fixture reset and framebuffer replacement or resizing fail closed',()=>{
  for(const change of [h=>h.resetRemote(456),h=>h.replaceCanvas(),h=>{h.canvas.width=801;}]) {
    const h=harness();h.arm('a');h.dom('a');change(h);h.tick(10);
    assert.equal(h.probe.take().ok,false);
  }
});

test('untrusted, repeated, modified, wrong-target and wrong-key events do not start timing',()=>{
  for(const extra of [{isTrusted:false},{repeat:true},{shiftKey:true},{ctrlKey:true},{altKey:true},{metaKey:true},{target:{}}]) {
    const h=harness();h.arm('a');h.dom('a',extra);h.remoteKey('a');h.tick(10);
    assert.equal(h.probe.take().ok,false);
  }
  const h=harness();h.arm('a');h.dom('b');h.remoteKey('a');h.tick(10);
  assert.equal(h.probe.take().ok,false);
});

test('corrupt partial frame is not accepted; valid subsequent frame can complete',()=>{
  const h=harness();h.arm('a');h.dom('a');h.remoteKey('a');
  const p=((32+4)*800+24+12*8+4)*4;
  for(let i=0;i<3;i++) h.framebuffer[p+i]=240-h.framebuffer[p+i]+16;
  h.tick(30);assert.equal(h.probe.take(),null);
  h.paint();h.tick(10);assert.equal(h.probe.take().ok,true);
});

test('repeated same key requires a new counter; digits and all arrows roundtrip',()=>{
  const h=harness();
  for(const key of ['a','a','0','9','ArrowLeft','ArrowRight','ArrowUp','ArrowDown']) {
    h.arm(key);h.dom(key);h.tick(10);assert.equal(h.probe.take(),null);
    h.remoteKey(key);h.tick(10);assert.equal(h.probe.take().ok,true);
  }
});

test('missing fixture and exhausted counter refuse arming; cancellation clears timer',()=>{
  const h=harness();h.framebuffer.fill(0);assert.throws(()=>h.probe.locate(),/not visible/);
  h.paint();h.setCounter(0xffffffff);assert.throws(()=>h.arm('a'),/baseline/);
  h.resetRemote();h.arm('a');assert.throws(()=>h.arm('b'),/already active/);
  h.probe.cancel();assert.equal(h.timers.size,0);assert.equal(h.probe.take().ok,false);
});

test('Playwright adapter reads the observer result and clears failed keyboard attempts',async()=>{
  const h=harness();
  const evaluate=async(fn,arg)=>{
    h.client.argument=arg;
    return vm.runInContext(`(${fn.toString()})(argument)`,h.client);
  };
  const page={evaluate,keyboard:{press:async key=>{
    h.dom(key);h.tick(20);h.remoteKey(key);h.tick(30);
  }},waitForFunction:async fn=>assert.equal(await evaluate(fn),true)};
  assert.equal(await measureRemoteEcho(page,'a',h.probe.locate(),250),50);
  page.keyboard.press=async()=>{throw Error('keyboard disconnected');};
  await assert.rejects(measureRemoteEcho(page,'b',h.probe.locate(),250),/disconnected/);
  assert.equal(h.timers.size,0);
});

test('focus uses decoded remote coordinates with CSS scaling and moves pointer away',async()=>{
  const h=harness();let clicked,moved;
  const page={evaluate:async()=>h.probe.locate(),locator:()=>({
    evaluate:async()=>({width:800,height:128}),
    boundingBox:async()=>({x:10,y:20,width:400,height:64}),
    click:async options=>{clicked=options.position;}
  }),mouse:{move:async(x,y)=>{moved={x,y};}}};
  const region=await focusRemoteEcho(page,'canvas');
  assert.equal(clicked.x,(region.x+304)/2);
  assert.equal(clicked.y,(region.y+8)/2);
  assert.deepEqual(moved,{x:402,y:28});
});

test('agent uses remote observer and never reinstalls the any-draw shortcut',()=>{
  const source=fs.readFileSync(new URL('../agent.mjs',import.meta.url),'utf8');
  assert.match(source,/context.addInitScript\(installEchoProbe/);
  assert.match(source,/measureRemoteEcho\(page, key/);
  assert.doesNotMatch(source,/__noiV1FrameState|waitForNextFrame|CanvasRenderingContext2D.prototype/);
});
