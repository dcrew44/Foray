// The UI-stream contract check (design §12.4): reads SSE files with the exact reader the browser
// uses — `parseJsonEventStream({schema: uiMessageChunkSchema})` and `readUIMessageStream` from the
// repo's installed `ai` — and compares the message the client rebuilds with the row the server saved.
//
// Usage: node validate_stream.mjs <dir>
//   <dir>/<name>.sse              the response body, as sent
//   <dir>/<name>.persisted.json   optional: the message the server saved
//   <dir>/<name>.last.json        optional: the assistant message the stream continues
//
// Prints a JSON report on stdout. Exit 0 when every case passes, 1 when one fails, 2 on misuse.
// A case fails when a chunk is rejected (schema or JSON), when the client's stream processor
// throws (out-of-order chunks), or when the rebuilt message differs from the persisted one.
// `error` chunks are not failures: their texts are reported as `errorTexts`.
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { isDeepStrictEqual } from 'node:util';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, '..', '..', '..');
const require = createRequire(join(repoRoot, 'package.json'));
const ai = await import(pathToFileURL(require.resolve('ai')).href);
const aiVersion = require('ai/package.json').version;
const { parseJsonEventStream, readUIMessageStream, uiMessageChunkSchema } = ai;

const dir = process.argv[2];
if (!dir || !existsSync(dir)) {
  console.error('usage: node validate_stream.mjs <dir>');
  process.exit(2);
}

const readJson = (path) => (existsSync(path) ? JSON.parse(readFileSync(path, 'utf8')) : undefined);

// The client renders its last written snapshot; a `step-start` with nothing after it is never
// written (`start-step` does not call write()), while the server's copy keeps it.
function comparable(message) {
  if (message == null) return message;
  const parts = [...(message.parts ?? [])];
  while (parts.length && parts[parts.length - 1].type === 'step-start') parts.pop();
  return { ...message, parts };
}

async function check(name) {
  const bytes = readFileSync(join(dir, `${name}.sse`));
  const persisted = readJson(join(dir, `${name}.persisted.json`));
  const last = readJson(join(dir, `${name}.last.json`));

  const rejected = [];
  const types = [];
  const errorTexts = [];
  const streamErrors = [];

  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new Uint8Array(bytes));
      controller.close();
    },
  });
  const chunks = parseJsonEventStream({ stream: body, schema: uiMessageChunkSchema }).pipeThrough(
    new TransformStream({
      transform(result, controller) {
        if (result.success) {
          types.push(result.value.type);
          controller.enqueue(result.value);
        } else {
          rejected.push(`${result.error.name}: ${String(result.error.message).split('\n')[0].slice(0, 300)}`);
        }
      },
    }),
  );

  let client;
  const onError = (error) => {
    if (error instanceof Error && Object.getPrototypeOf(error) === Error.prototype) {
      errorTexts.push(error.message); // an `error` chunk: `new Error(chunk.errorText)`
    } else {
      streamErrors.push(`${error?.name ?? 'Error'}: ${error?.message ?? String(error)}`);
    }
  };
  for await (const message of readUIMessageStream({ message: last, stream: chunks, onError })) {
    client = message;
  }
  client = client === undefined ? null : JSON.parse(JSON.stringify(client));

  const matches = persisted === undefined ? null : isDeepStrictEqual(comparable(client), comparable(persisted));
  const ok = rejected.length === 0 && streamErrors.length === 0 && matches !== false;
  return {
    ok,
    chunks: types.length,
    types,
    rejected,
    streamErrors,
    errorTexts,
    done: bytes.toString('utf8').includes('data: [DONE]'),
    matches,
    ...(matches === false ? { client, persisted } : {}),
  };
}

const names = readdirSync(dir)
  .filter((f) => f.endsWith('.sse'))
  .map((f) => f.slice(0, -'.sse'.length))
  .sort();
const cases = {};
for (const name of names) {
  try {
    cases[name] = await check(name);
  } catch (error) {
    cases[name] = { ok: false, crashed: `${error?.name}: ${error?.message}` };
  }
}
process.stdout.write(JSON.stringify({ ai: aiVersion, cases }));
process.exit(names.length > 0 && Object.values(cases).every((c) => c.ok) ? 0 : 1);
