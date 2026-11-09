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
    assert.equal(init.redirect, 'error');
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
