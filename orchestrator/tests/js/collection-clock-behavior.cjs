const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {script} = JSON.parse(fs.readFileSync(0, 'utf8'));

async function exercise(options = {}) {
  let reloads = 0;
  const timers = new Map();
  const events = new Map();
  const hint = {textContent:''};
  const revision = {getAttribute: () => 'a'.repeat(64)};
  const document = {
    hidden: Boolean(options.hidden),
    getElementById: id => id === 'sync-status' ? hint : {textContent:''},
    querySelectorAll: () => [],
    querySelector: selector => selector === '[data-receipt-revision]'
      ? (options.editPage ? null : revision)
      : (options.fileInput ? {files:[{}]} : null),
    addEventListener: (name, callback) => events.set(name, callback),
  };
  const response = {server_ms:1000, begin_ms:0, end_ms:60000, opened:true};
  if (!options.noRevision) response.receipt_revision = (options.unchanged ? 'a' : 'b').repeat(64);
  const context = {document, performance:{now:()=>0}, Intl, Date, String, Math,
    setInterval:(fn, ms)=>timers.set(ms, fn),
    window:{location:{reload:()=>reloads++}, addEventListener:()=>{}},
    fetch:async()=>{
      if (options.offline) throw new Error('fixture network unavailable');
      return {ok:true, json:async()=>response};
    },
  };
  vm.runInNewContext(script, context, {timeout:1000});
  await timers.get(15000)();
  if (options.reveal) {
    assert.equal(reloads, 0);
    document.hidden = false;
    await events.get('visibilitychange')();
    await new Promise(resolve => setImmediate(resolve));
  }
  return reloads;
}

(async()=>{
  const cases = [
    [{},1], [{unchanged:true},0], [{editPage:true},0], [{fileInput:true},0],
    [{hidden:true},0], [{offline:true},0], [{noRevision:true},0],
    [{hidden:true,reveal:true},1],
  ];
  for (const [options, expected] of cases)
    assert.equal(await exercise(options), expected, JSON.stringify(options));
  process.stdout.write(JSON.stringify({cases_passed:cases.length}));
})().catch(error=>{ console.error(error); process.exitCode=1; });
