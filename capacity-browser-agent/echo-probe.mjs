// Read-only framebuffer observer. This function is serialized into the noVNC page.
// Its counterpart must run INSIDE the remote qualification desktop, never here.
export function installEchoProbe({ canvasSelector }) {
  const cell = 8, cells = 76, width = cell * cells;
  const magic = [[235,35,205], [20,210,220], [235,215,25], [35,65,230]];
  const near = (pixel, rgb) => rgb.every((v, i) => Math.abs(pixel[i] - v) <= 20);
  const keyCode = key => {
    const arrows = { ArrowLeft:128, ArrowRight:129, ArrowUp:130, ArrowDown:131 };
    return arrows[key] || (/^[a-z0-9]$/.test(key) ? key.charCodeAt(0) : 0);
  };
  let active = null, result = null;
  function checksum(bits) {
    let crc=0xffff;
    for(const bit of bits) {
      const top=(crc>>>15)^bit;
      crc=(crc<<1)&0xffff;
      if(top) crc^=0x1021;
    }
    return crc;
  }

  function read(canvas, region) {
    if (!canvas || region.x < 0 || region.y < 0 || region.x + width > canvas.width ||
        region.y + 9 >= canvas.height) return null;
    const pixels = canvas.getContext('2d').getImageData(region.x, region.y + 4, width, 1).data;
    const pixel = n => pixels.subarray((n * cell + 4) * 4, (n * cell + 4) * 4 + 3);
    if (!magic.every((rgb, n) => near(pixel(n), rgb))) return null;
    const bits = [];
    for (let n = 4; n < cells; n++) {
      const p = pixel(n);
      if (near(p, [16,16,16])) bits.push(0);
      else if (near(p, [240,240,240])) bits.push(1);
      else return null;
    }
    const number = list => list.reduce((v, bit) => v * 2 + bit, 0);
    if (checksum(bits.slice(0,56)) !== number(bits.slice(56))) return null;
    return { epoch:number(bits.slice(0,16)), counter:number(bits.slice(16,48)), key:number(bits.slice(48,56)) };
  }

  function locate() {
    const canvas = document.querySelector(canvasSelector);
    if (!canvas || canvas.width < width || canvas.height < 16) throw Error('remote echo framebuffer missing');
    const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    for (let y = 0; y + 16 <= canvas.height; y += 4) {
      for (let x = 0; x + width <= canvas.width; x++) {
        const at = (y * canvas.width + x) * 4;
        if (!near(pixels.subarray(at, at + 3), magic[0])) continue;
        const region = { x, y, width, height:16 };
        const state = read(canvas, region);
        if (state && state.epoch !== 0) return region;
      }
    }
    throw Error('remote echo fixture is not visible; do not sample arbitrary redraws');
  }

  function finish(value) {
    clearInterval(active.timer);
    result = value;
    active = null;
  }
  window.addEventListener('keydown', event => {
    if (!active || active.sent !== null || event.target !== active.canvas || event.repeat ||
        !event.isTrusted || event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) return;
    if (event.key !== active.key) return;
    // Use the same browser clock for DOM dispatch and the observed remote frame.
    active.sent = performance.now();
  }, true);

  window.__noiEchoProbe = {
    locate,
    arm(key, region, timeoutMs) {
      if (active) throw Error('echo observation already active');
      if (!keyCode(key) || !Number.isInteger(timeoutMs) || timeoutMs < 250 || timeoutMs > 5000)
        throw Error('invalid echo observation request');
      const canvas = document.querySelector(canvasSelector);
      const baseline = read(canvas, region);
      if (!baseline || baseline.counter === 0xffffffff || baseline.epoch === 0)
        throw Error('remote echo baseline unavailable');
      result = null;
      active = { canvas, region, baseline, key, sent:null, armed:performance.now(),
        canvasWidth:canvas.width, canvasHeight:canvas.height, timer:null };
      active.timer = setInterval(() => {
        try {
          const p = active;
          if (document.querySelector(canvasSelector) !== p.canvas || p.canvas.width !== p.canvasWidth ||
              p.canvas.height !== p.canvasHeight) return finish({ok:false,error:'framebuffer changed'});
          const state = read(p.canvas, p.region);
          if (state) {
            if (state.epoch !== p.baseline.epoch || state.counter < p.baseline.counter)
              return finish({ok:false,error:'remote fixture reset'});
            if (state.counter > p.baseline.counter) {
              if (p.sent === null || state.counter !== p.baseline.counter + 1 || state.key !== keyCode(p.key))
                return finish({ok:false,error:'unexpected remote acknowledgment'});
              const latency = performance.now() - p.sent;
              if (latency <= 0 || latency > timeoutMs)
                return finish({ok:false,error:'remote acknowledgment outside timing window'});
              return finish({ok:true,latency_ms:latency,epoch:state.epoch,counter:state.counter,key:state.key});
            }
          }
          if (performance.now() - (p.sent === null ? p.armed : p.sent) >= timeoutMs)
            finish({ok:false,error:'remote acknowledgment timed out'});
        } catch {
          finish({ok:false,error:'framebuffer could not be read'});
        }
      }, 5);
    },
    take() { return result; },
    cancel() { if (active) finish({ok:false,error:'observation cancelled'}); }
  };
}

export async function focusRemoteEcho(page, canvasSelector) {
  const region = await page.evaluate(() => window.__noiEchoProbe.locate());
  const canvas = page.locator(canvasSelector);
  const dimensions = await canvas.evaluate(item => ({width:item.width,height:item.height}));
  const box = await canvas.boundingBox();
  if (!box || box.width <= 0 || box.height <= 0) throw Error('echo framebuffer is not visible');
  await canvas.click({position:{x:(region.x + region.width/2)*box.width/dimensions.width,
    y:(region.y+8)*box.height/dimensions.height}});
  // A server-rendered mouse pointer can corrupt the sampled pixels.
  await page.mouse.move(box.x+box.width-8, box.y+8);
  return region;
}

export async function measureRemoteEcho(page, key, region, timeoutMs) {
  try {
    await page.evaluate(args => window.__noiEchoProbe.arm(args.key,args.region,args.timeoutMs),
      {key,region,timeoutMs});
    await page.keyboard.press(key);
    await page.waitForFunction(() => window.__noiEchoProbe.take() !== null, null,
      {timeout:timeoutMs+1000,polling:10});
    const result = await page.evaluate(() => window.__noiEchoProbe.take());
    if (!result.ok) throw Error(result.error);
    return result.latency_ms;
  } finally {
    await page.evaluate(() => window.__noiEchoProbe?.cancel()).catch(() => {});
  }
}
