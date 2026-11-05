#!/usr/bin/env node
// pg-mcp-proxy: wraps an MCP server process, logs tool_call/tool_result
// events to PG_PROXY_SINK (DD-06 §5.3, C9/B-11 — MCP wiretap).
// Usage: pg-mcp-proxy -- <original-command> [args...]
// Env: PG_PROXY_SINK=path to NDJSON output file
//
// B-11 contract notes:
// - requests/responses are paired by JSON-RPC id: a pending tools/call is
//   remembered with its tool name + start time so the matching response can
//   be logged as a tool_result event carrying duration_ms;
// - child stdout is line-buffered across chunks (a JSON payload split over
//   two chunks must not be lost);
// - any uncaught error explicitly kills the child and exits non-zero.

import { spawn } from 'child_process';
import { appendFileSync } from 'fs';
import { createInterface } from 'readline';

const args = process.argv.slice(2);
if (args[0] === '--') args.shift();
if (args.length === 0) { console.error('pg-mcp-proxy: no command'); process.exit(1); }

const sink = process.env.PG_PROXY_SINK || '/trace/events.ndjson';
const child = spawn(args[0], args.slice(1), { stdio: ['pipe', 'pipe', 'inherit'] });

const log = (event_type, data, extra = {}) => {
  const line = JSON.stringify({
    ts: new Date().toISOString(),
    event_type, data,
    run_id: process.env.RSI_RUN_ID || '',
    ...extra,
  });
  try { appendFileSync(sink, line + '\n'); } catch (e) { /* non-blocking */ }
};

// JSON-RPC id -> { name, startMs } for in-flight tools/call requests
const pending = new Map();

const handleRequest = (msg) => {
  if (msg.method === 'tools/call' && msg.id !== undefined) {
    pending.set(msg.id, {
      name: msg.params?.name,
      startMs: Date.now(),
    });
    log('tool_call', { name: msg.params?.name, arguments: msg.params?.arguments });
  }
};

const handleResponse = (msg) => {
  if (msg.id === undefined || !pending.has(msg.id)) return;
  const call = pending.get(msg.id);
  pending.delete(msg.id);
  const duration_ms = Date.now() - call.startMs;
  log('tool_result',
    { tool: call.name, content: msg.result?.content, isError: !!msg.result?.isError },
    { duration_ms });
};

const rl = createInterface({ input: process.stdin });
rl.on('line', (line) => {
  try {
    const msg = JSON.parse(line);
    handleRequest(msg);
  } catch (e) { /* not JSON, pass through */ }
  child.stdin.write(line + '\n');
});

// B-11: line-buffer child stdout — chunks are accumulated until a newline.
let stdoutBuf = '';
child.stdout.on('data', (chunk) => {
  stdoutBuf += chunk.toString();
  process.stdout.write(chunk);
  let idx;
  while ((idx = stdoutBuf.indexOf('\n')) >= 0) {
    const line = stdoutBuf.slice(0, idx).trim();
    stdoutBuf = stdoutBuf.slice(idx + 1);
    if (!line) continue;
    try {
      const msg = JSON.parse(line);
      handleResponse(msg);
    } catch (e) { /* not JSON, ignore */ }
  }
});

child.on('exit', (code) => process.exit(code || 0));
process.on('SIGTERM', () => { child.kill('SIGTERM'); process.exit(0); });
// B-11: explicit panic handling — kill the child, then exit non-zero.
process.on('uncaughtException', (err) => {
  console.error('pg-mcp-proxy: uncaught', err);
  try { child.kill('SIGKILL'); } catch (e) { /* already gone */ }
  process.exit(1);
});
