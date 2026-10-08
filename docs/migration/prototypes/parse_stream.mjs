// Parse a UI message stream with the exact reader DefaultChatTransport uses
// (parseJsonEventStream + uiMessageChunkSchema), timestamp each chunk's arrival, then fold the
// chunks with readUIMessageStream. Source: a URL (POST body = argv[3]) or a file path.
// Usage: node parse_stream.mjs <url|file> [jsonBody]
import { readFileSync } from 'node:fs';
import { ai } from './repo.mjs';

const { parseJsonEventStream, uiMessageChunkSchema, readUIMessageStream } = ai;
const [src, body] = process.argv.slice(2);

let bytes;
const t0 = Date.now();
if (src.startsWith('http')) {
  const res = await fetch(src, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: body ?? JSON.stringify({ id: 'c', trigger: 'submit-message', messages: [{ id: 'u1', role: 'user', parts: [{ type: 'text', text: 'hi' }] }] }),
  });
  console.log('HTTP', res.status, Object.fromEntries(res.headers));
  bytes = res.body;
} else {
  const buf = readFileSync(src);
  bytes = new ReadableStream({ start(c) { c.enqueue(new Uint8Array(buf)); c.close(); } });
}

const results = [];
const chunks = parseJsonEventStream({ stream: bytes, schema: uiMessageChunkSchema }).pipeThrough(
  new TransformStream({
    transform(r, c) {
      const ms = Date.now() - t0;
      results.push(r.success ? `+${ms}ms ok   ${r.value.type}` : `+${ms}ms FAIL ${r.error.name}: ${String(r.error.message).split('\n')[0].slice(0, 140)}`);
      if (r.success) c.enqueue(r.value);
    },
  }),
);

let last;
for await (const message of readUIMessageStream({ stream: chunks, onError: e => results.push('readUIMessageStream onError: ' + e.message) })) {
  last = message;
}
console.log(results.join('\n'));
console.log('final message:', JSON.stringify(last));
