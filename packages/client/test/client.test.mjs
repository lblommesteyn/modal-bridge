import test from 'node:test';
import assert from 'node:assert/strict';
import { ModalBridge, BridgeError, JobError, WebhookError, verifyWebhook } from '../dist/index.js';
import { createHmac } from 'node:crypto';
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
test('wait keeps polling after cancellation is requested, since completion can race', async () => {
  let polls = 0;
  const won = client(async () => json(++polls < 3 ? {status:'cancel_requested'} : {status:'succeeded', result:9}));
  assert.equal(await won.wait('x', {intervalMs:1}), 9);
  assert.equal(polls, 3);
  polls = 0;
  const lost = client(async () => json(++polls < 2 ? {status:'cancel_requested'} : {status:'failed', error:'Remote execution failed'}));
  await assert.rejects(lost.wait('x', {intervalMs:1}), e => e instanceof JobError && e.job.status === 'failed');
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

test('submit forwards the completion callback and owner', async () => {
  const c = client(async (url, init) => {
    assert.equal(init.headers.get('Bridge-Owner'), 'user-7');
    assert.deepEqual(JSON.parse(init.body), {function:'square', input:1, on_complete:{url:'https://app.example/done'}});
    return json({id:'job', status:'pending'}, 202);
  });
  await c.submit('square', 1, {idempotencyKey:'k', owner:'user-7', onComplete:{url:'https://app.example/done'}});
});

test('list and resolve use the operations routes', async () => {
  const seen = [];
  const c = client(async (url, init) => { seen.push([init.method ?? 'GET', url, init.body]); return json({jobs:[{id:'a'}], id:'a'}); });
  assert.deepEqual(await c.list({status:'submission_unknown', limit:5}), [{id:'a'}]);
  await c.resolve('a', {action:'attach', call_id:'fc-1'});
  assert.deepEqual(seen, [
    ['GET', 'https://gateway.example/v1/jobs?status=submission_unknown&limit=5', undefined],
    ['POST', 'https://gateway.example/v1/jobs/a/resolve', '{"action":"attach","call_id":"fc-1"}']]);
});

const signedRequest = (secretBytes, {body = JSON.stringify({type:'job.succeeded', id:'job-1', job:{id:'job-1', status:'succeeded', result:9}}),
    timestamp = Math.floor(Date.now() / 1000), id = 'job-1', tamper = false} = {}) => {
  const signature = createHmac('sha256', secretBytes).update(`${id}.${timestamp}.${body}`).digest('base64');
  return new Request('https://app.example/done', {method:'POST', body: tamper ? body.replace('9', '8') : body,
    headers:{'webhook-id':id, 'webhook-timestamp':String(timestamp), 'webhook-signature':`v1,${signature}`}});
};
test('verifyWebhook accepts genuine events for whsec_ and plain secrets', async () => {
  const bytes = Buffer.alloc(32, 7);
  const event = await verifyWebhook(signedRequest(bytes), 'whsec_' + bytes.toString('base64'));
  assert.equal(event.job.result, 9);
  assert.equal((await verifyWebhook(signedRequest(Buffer.from('a-plain-shared-secret-value')), 'a-plain-shared-secret-value')).id, 'job-1');
});
test('verifyWebhook rejects tampering, wrong secrets, stale and unsigned requests', async () => {
  const bytes = Buffer.alloc(32, 7), secret = 'whsec_' + bytes.toString('base64');
  await assert.rejects(verifyWebhook(signedRequest(bytes, {tamper:true}), secret), WebhookError);
  await assert.rejects(verifyWebhook(signedRequest(Buffer.alloc(32, 8)), secret), WebhookError);
  await assert.rejects(verifyWebhook(signedRequest(bytes, {timestamp: Math.floor(Date.now() / 1000) - 3600}), secret), /tolerance/);
  await assert.rejects(verifyWebhook(new Request('https://app.example/done', {method:'POST', body:'{}'}), secret), /Missing/);
});
