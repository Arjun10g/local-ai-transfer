#!/usr/bin/env node
// BMO MCP bridge: a stdio MCP server that lets GitHub Copilot (VS Code agent mode, Copilot
// CLI, other IDE Copilots) delegate small chores to the BMO host already running on this
// computer. It holds no model and no tools of its own; it forwards validated requests to
// http://127.0.0.1:<port> using the delegate key from the BMO_DELEGATE_KEY environment
// variable. Client setup: docs/copilot/README.md.

import { startStdioBridge } from './host/mcp/stdio.mjs';

const USAGE = 'usage: node lae-mcp.mjs   (no arguments; the delegate key is read only from the BMO_DELEGATE_KEY environment variable)\n';

// stdout is the MCP channel. Route console output away from it so no stray log line can
// ever corrupt the stream, and keep it silent because it could carry unscreened text.
for (const method of ['log', 'info', 'debug', 'warn', 'error', 'trace', 'dir', 'table']) console[method] = () => {};

const args = process.argv.slice(2);
if (args.length === 1 && (args[0] === '--help' || args[0] === '-h')) {
  process.stderr.write(USAGE);
  process.exit(0);
}
if (args.length > 0) {
  // Secrets on a command line are visible to every local process and land in shell history
  // and client config files. Refuse any argument, and never echo what was passed.
  process.stderr.write(`lae-mcp: refusing to start: command-line arguments are not accepted (${args.length} given). Pass the delegate key only through the BMO_DELEGATE_KEY environment variable.\n${USAGE}`);
  process.exit(2);
}

const bridge = startStdioBridge();
process.on('SIGTERM', () => bridge.stop('sigterm'));
process.on('SIGINT', () => bridge.stop('sigint'));
process.on('uncaughtException', error => { bridge.logger.error('uncaught_exception', { kind: error?.name ?? 'unknown' }); process.exit(70); });
process.on('unhandledRejection', reason => { bridge.logger.error('unhandled_rejection', { kind: reason?.name ?? 'unknown' }); });
