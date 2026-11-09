import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {Miniflare, convertV4MiniflareOptions} from 'miniflare';

test('native Workers fetch submits to an HTTP gateway without Node compatibility', async () => {
  let received;
  const server = createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    received = {path:req.url, auth:req.headers.authorization, key:req.headers['idempotency-key'], body:JSON.parse(body)};
    res.writeHead(202, {'Content-Type':'application/json'});
    res.end(JSON.stringify({id:'native-worker-job', function:'square', status:'pending'}));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let runtime;
  try {
    const source = await readFile(new URL('../dist/index.js', import.meta.url), 'utf8');
    runtime = new Miniflare(convertV4MiniflareOptions({
      modules:true, compatibilityDate:'2026-11-09',
      script: source + `\nexport default {async fetch() {
        const client = new ModalBridge({url:'http://127.0.0.1:${server.address().port}', token:'test-only'});
        return Response.json(await client.submit('square', {value:17}, {idempotencyKey:'worker-test'}));
      }};`,
    }));
    const response = await runtime.dispatchFetch('http://worker.test/');
    assert.equal(response.status, 200);
    assert.equal((await response.json()).id, 'native-worker-job');
    assert.deepEqual(received, {path:'/v1/jobs', auth:'Bearer test-only', key:'worker-test', body:{function:'square',input:{value:17}}});
  } finally {
    if (runtime) await runtime.dispose();
    await new Promise(resolve => server.close(resolve));
  }
});
