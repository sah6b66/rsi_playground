#!/usr/bin/env node
// pg-mcp-proxy: wraps an MCP server process, logs tools/call to PG_PROXY_SINK
// (DD-06 §5.3, C9 — MCP wiretap for third-party agent harnesses).
// Usage: pg-mcp-proxy -- <original-command> [args...]
// Env: PG_PROXY_SINK=path to NDJSON output file

import { spawn } from 'child_process';
import { appendFileSync } from 'fs';
import { createInterface } from 'readline';

const args = process.argv.slice(2);
if (args[0] === '--') args.shift();
if (args.length === 0) { console.error('pg-mcp-proxy: no command'); process.exit(1); }

const sink = process.env.PG_PROXY_SINK || '/trace/events.ndjson';
const child = spawn(args[0], args.slice(1), { stdio: ['pipe', 'pipe', 'inherit'] });

const log = (event_type, data) => {
  const line = JSON.stringify({
    ts: new Date().toISOString(),
    event_type, data,
    run_id: process.env.RSI_RUN_ID || ''
  });
  try { appendFileSync(sink, line + '\n'); } catch (e) { /* non-blocking */ }
};

const rl = createInterface({ input: process.stdin });
rl.on('line', (line) => {
  try {
    const msg = JSON.parse(line);
    if (msg.method === 'tools/call') {
      log('tool_call', { name: msg.params?.name, arguments: msg.params?.arguments });
    }
  } catch (e) { /* not JSON, pass through */ }
  child.stdin.write(line + '\n');
});

child.stdout.on('data', (chunk) => {
  const lines = chunk.toString().split('\n').filter(Boolean);
  for (const line of lines) {
    try {
      const msg = JSON.parse(line);
      if (msg.result && msg.result.content) {
        log('tool_result', { content: msg.result.content, isError: !!msg.result.isError });
      }
    } catch (e) { /* pass through */ }
  }
  process.stdout.write(chunk);
});

child.on('exit', (code) => process.exit(code || 0));
process.on('SIGTERM', () => { child.kill('SIGTERM'); process.exit(0); });
