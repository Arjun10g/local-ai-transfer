export class ToolArgumentError extends Error {
  constructor(message) { super(message); this.name = 'ToolArgumentError'; this.code = 'invalid_tool_arguments'; }
}

const object = value => { if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ToolArgumentError('arguments must be an object'); return value; };
const exact = (value, allowed, required = []) => { object(value); for (const key of Object.keys(value)) if (!allowed.includes(key)) throw new ToolArgumentError(`unknown argument: ${key}`); for (const key of required) if (!(key in value)) throw new ToolArgumentError(`missing argument: ${key}`); return value; };
const string = (value, name, { min = 0, max = 1024 } = {}) => { if (typeof value !== 'string' || value.length < min || value.length > max || value.includes('\0')) throw new ToolArgumentError(`${name} must be a bounded string`); };
const integer = (value, name, min, max) => { if (!Number.isInteger(value) || value < min || value > max) throw new ToolArgumentError(`${name} is out of range`); };

export function validateToolArguments(name, input = {}) {
  const args = object(input);
  if (name === 'system.get_info' || name === 'clipboard.read') return exact(args, []);
  if (name === 'time.now') { exact(args, ['format']); if ('format' in args && !['local', 'utc', 'iso'].includes(args.format)) throw new ToolArgumentError('format is unsupported'); return args; }
  if (name === 'fs.list') { exact(args, ['workspace_id', 'path', 'max_entries'], ['workspace_id']); string(args.workspace_id, 'workspace_id', { min: 1, max: 64 }); if ('path' in args) string(args.path, 'path'); if ('max_entries' in args) integer(args.max_entries, 'max_entries', 1, 500); return args; }
  if (name === 'fs.read_text') { exact(args, ['workspace_id', 'path', 'offset_bytes', 'max_bytes'], ['workspace_id', 'path']); string(args.workspace_id, 'workspace_id', { min: 1, max: 64 }); string(args.path, 'path', { min: 1 }); if ('offset_bytes' in args) integer(args.offset_bytes, 'offset_bytes', 0, 1048576); if ('max_bytes' in args) integer(args.max_bytes, 'max_bytes', 1, 65536); return args; }
  if (name === 'fs.search_text') { exact(args, ['workspace_id', 'path', 'query', 'max_files', 'max_matches', 'max_depth'], ['workspace_id', 'query']); string(args.workspace_id, 'workspace_id', { min: 1, max: 64 }); if ('path' in args) string(args.path, 'path'); string(args.query, 'query', { min: 1, max: 4096 }); if ('max_files' in args) integer(args.max_files, 'max_files', 1, 200); if ('max_matches' in args) integer(args.max_matches, 'max_matches', 1, 500); if ('max_depth' in args) integer(args.max_depth, 'max_depth', 0, 16); return args; }
  if (name === 'fs.write_new') { exact(args, ['workspace_id', 'path', 'content'], ['workspace_id', 'path', 'content']); string(args.workspace_id, 'workspace_id', { min: 1, max: 64 }); string(args.path, 'path', { min: 1 }); string(args.content, 'content', { max: 65536 }); if (Buffer.byteLength(args.content, 'utf8') > 65536) throw new ToolArgumentError('content exceeds byte limit'); return args; }
  if (name === 'fs.apply_patch') { exact(args, ['workspace_id', 'path', 'base_sha256', 'base_hash', 'replacement', 'patch'], ['workspace_id', 'path']); string(args.workspace_id, 'workspace_id', { min: 1, max: 64 }); string(args.path, 'path', { min: 1 }); const bases = ['base_sha256', 'base_hash'].filter(key => key in args); if (bases.length !== 1 || typeof args[bases[0]] !== 'string' || !/^[a-f0-9]{64}$/i.test(args[bases[0]])) throw new ToolArgumentError('exactly one valid base hash is required'); const bodies = ['replacement', 'patch'].filter(key => key in args); if (bodies.length !== 1) throw new ToolArgumentError('exactly one replacement or patch is required'); string(args[bodies[0]], bodies[0], { max: 2 * 1024 * 1024 }); return args; }
  if (name === 'clipboard.write') { exact(args, ['text'], ['text']); string(args.text, 'text', { max: 65536 }); if (Buffer.byteLength(args.text, 'utf8') > 65536) throw new ToolArgumentError('text exceeds byte limit'); return args; }
  if (name === 'app.open') { exact(args, ['app_id'], ['app_id']); string(args.app_id, 'app_id', { min: 1, max: 64 }); return args; }
  if (name === 'browser.open_url') { exact(args, ['url'], ['url']); string(args.url, 'url', { min: 1, max: 2048 }); return args; }
  throw new ToolArgumentError(`no argument schema for ${name}`);
}
