import test from 'node:test';
import assert from 'node:assert/strict';
import { ModalBridge, BridgeError, JobError } from '../dist/index.js';
const json = (body, status = 200) => Response.json(body, {status});
const client = (transport) => new ModalBridge({url: 'https://gateway.example', token: 'private', fetch: transport});

test('submit sends typed JSON, stable key and auth over fetch', async () => {
  const c = client(async (url, init) => {
    assert.equal(url, 'https://gateway.example/v1/jobs');
    assert.equal(init.headers.get('Authorization'), 'Bearer private');
    assert.equal(init.headers.get('Idempotency-Key'), 'stable');
    assert.equal(init.redirect, 'manual');
    assert.deepEqual(JSON.parse(init.body), {function: 'square', input: {value: 3}});
    return json({id: 'job', status: 'pending'}, 202);
  });
  assert.equal((await c.submit('square', {value: 3}, {idempotencyKey: 'stable'})).id, 'job');
});
test('HTTP errors retain status without swallowing conflicts', async () => {
  await assert.rejects(client(async () => json({detail: 'Conflict'}, 409)).get('x'), e => e instanceof BridgeError && e.status === 409);
});
test('wait polls until success including JSON null', async () => {
  let polls = 0;
  assert.equal(await client(async () => json(++polls === 1 ? {status:'pending'} : {status:'succeeded', result:null})).wait('x', {intervalMs:1}), null);
  assert.equal(polls, 2);
});
test('wait surfaces terminal failures', async () => {
  await assert.rejects(client(async () => json({status:'submission_unknown', error:'Inspect Modal'})).wait('x'), JobError);
});
test('deadline aborts an in-flight fetch, not only polling sleeps', async () => {
  const c = client((url, {signal}) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), {once:true});
  }));
  await assert.rejects(c.wait('x', {timeoutMs:20}), /Timed out/);
});
test('user abort stops polling without cancelling remote job', async () => {
  const controller = new AbortController();
  controller.abort(new Error('Stopped'));
  let calls = 0;
  await assert.rejects(client(async () => { calls++; return json({}); }).wait('x', {signal:controller.signal}), /Stopped/);
  assert.equal(calls, 0);
});
test('rejects insecure remote gateways and URL credentials', () => {
  for (const url of ['http://remote.example','https://user:pass@example.com']) {
    assert.throws(() => new ModalBridge({url, token:'secret'}));
  }
});

test('default transport preserves the native fetch receiver required by Workers', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async function () {
    assert.equal(this, globalThis);
    return json({id:'job', status:'pending'});
  };
  try {
    const c = new ModalBridge({url:'https://gateway.example', token:'private'});
    assert.equal((await c.get('job')).status, 'pending');
  } finally {
    globalThis.fetch = original;
  }
});

test('redirects are rejected without forwarding gateway credentials', async () => {
  let calls = 0;
  const c = client(async (url, init) => {
    calls++;
    assert.equal(init.redirect, 'manual');
    return new Response(null, {status:302, headers:{Location:'https://other.example'}});
  });
  await assert.rejects(c.get('x'), error => error instanceof BridgeError && error.status === 302);
  assert.equal(calls, 1);
});
