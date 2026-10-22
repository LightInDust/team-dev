// Pinned OMP 18.2.8 tool_call gate. Cooperative policy, not a host sandbox.
import { readFileSync, openSync, writeSync } from 'node:fs';
import { createHash } from 'node:crypto';

const sha = (s: string) => createHash('sha256').update(s, 'utf8').digest('hex');
// Python str.strip semantics, shared with worker_bash.py (JS trim differs).
const strip = (s: string) => s.replace(/^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g, '');

export function classify(args: any, rules: any): string {
  const command = args && typeof args === 'object' ? args.command : undefined;
  if (!(typeof command === 'string' && strip(command).length > 0 && [...command].length <= 32768)) return 'unavailable';
  const invalid = Object.keys(args).some(k => !['command','timeout','cwd'].includes(k)) ||
    ('timeout' in args && !(typeof args.timeout === 'number' && args.timeout > 0 && args.timeout <= 3600)) ||
    ('cwd' in args && args.cwd !== rules.snapshot_cwd);
  return invalid ? 'invalid_arguments' : rules.allowed_command_sha256.includes(sha(strip(command))) ? 'allowed' : 'undeclared_command';
}

export function correctionHint(args: any, rules: any, entries: any[]): string {
  const command = args && typeof args === 'object' ? args.command : undefined;
  const valid = typeof command === 'string' && strip(command).length > 0 && [...command].length <= 32768;
  const issues: string[] = [];
  let match = 'unknown', cwd = 'unavailable';
  if (valid) {
    match = rules.allowed_command_sha256.includes(sha(strip(command))) ? 'yes' : 'no';
    if (Object.keys(args).some(k => !['command','timeout','cwd'].includes(k))) issues.push('unsupported_arguments');
    if ('timeout' in args && !(typeof args.timeout === 'number' && args.timeout > 0 && args.timeout <= 3600)) issues.push('invalid_timeout');
    if (!('cwd' in args)) cwd = 'omitted';
    else if (typeof args.cwd !== 'string' || !args.cwd || args.cwd.includes('\x00')) {
      cwd = 'invalid'; issues.push('invalid_cwd');
    } else if (args.cwd === rules.snapshot_cwd) cwd = 'snapshot';
    else { cwd = 'different'; issues.push('cwd_not_snapshot'); }
  }
  const fields = [`command_match=${match}`, `argument_issues=${issues.join(',') || 'none'}`, `cwd=${cwd}`];
  let budget = 131072, best: any = null, ambiguous = false;
  if (typeof command === 'string' && [...command].length <= 32768) {
    const actual = [...strip(command)];
    for (const entry of entries) {
      const expected = [...strip(entry.arguments.command)];
      budget -= expected.length;
      if (budget < 0) break;
      let prefix = 0, suffix = 0;
      while (prefix < Math.min(actual.length, expected.length) && actual[prefix] === expected[prefix]) prefix++;
      while (suffix < Math.min(actual.length, expected.length) - prefix && actual[actual.length-1-suffix] === expected[expected.length-1-suffix]) suffix++;
      const score = (prefix + suffix) / Math.max(actual.length, expected.length, 1);
      const item = {score, id:entry.id, prefix, expected:expected.join(''), length:expected.length};
      if (!best || score > best.score) { best = item; ambiguous = false; }
      else if (score === best.score && item.expected !== best.expected) ambiguous = true;
    }
    if (best && best.score >= .5 && !ambiguous) {
      fields.push(`reference=${best.id}`, `first_difference_codepoint=${actual.join('') === best.expected ? 'none' : best.prefix}`,
                  `expected_length=${best.length}`, `received_length=${actual.length}`);
    }
  }
  return 'Exact-test hint: ' + fields.join('; ') + '. References identify worker_test_invocations; copy its complete arguments object unchanged. Do not execute a reference label or rebuild a logging envelope. Hint is not permission.';
}

export default function(pi: any) {
  const config = JSON.parse(readFileSync(process.env.TEAM_DEV_GATE_CONFIG!, 'utf8'));
  if (![1,2].includes(config.version) || config.rules.version !== 2 || config.rules.mode !== 'declared_tests_only' || config.max_denials !== 3 ||
      config.version === 2 && (!Array.isArray(config.test_catalog) || !config.test_catalog.length))
    throw new Error('Invalid team-dev gate config');
  const fd = openSync(config.journal, 'wx', 0o600);
  let bytes = 0, denials = 0, broken = false, ready = false;
  const record = (value: any) => {
    const line = Buffer.from(JSON.stringify(value) + '\n');
    if (line.length > 1024 * 1024 || bytes + line.length > 16 * 1024 * 1024) throw new Error('Gate journal limit');
    if (writeSync(fd, line) !== line.length) throw new Error('Incomplete gate journal write');
    bytes += line.length;
  };
  pi.on('session_start', () => {
    if (ready) throw new Error('Gate session cannot be switched');
    record({type:'ready', version:1, nonce:config.nonce, policy_sha256:config.policy_sha256});
    ready = true;
  });
  pi.on('tool_call', (event: any) => {
    if (!ready || broken) return {block:true, reason:'TEAM_DEV_GATE_UNAVAILABLE: stop and report the coordinator blocker.'};
    if (event.toolName !== 'bash') {
      if (denials >= config.max_denials) {
        const reason = `TEAM_DEV_GATE_EXHAUSTED:${config.nonce}:${event.toolCallId}: Tool NOT executed; correction budget exhausted.`;
        try {
          record({type:'lockout', tool_name:event.toolName, id:event.toolCallId, args:event.input, reason});
        } catch { broken = true; }
        return {block:true, reason};
      }
      return;
    }
    try {
      const args = event.input;
      const status = classify(args, config.rules);
      if (status !== 'allowed') denials++;
      const decision = status === 'allowed' && denials < config.max_denials ? 'allow' : 'block';
      const reason = `TEAM_DEV_GATE_BLOCK:${config.nonce}:${event.toolCallId}:${denials}: Command was NOT executed. Use read/grep/glob for inspection, edit/write for source, or copy an exact prepared test command and its permitted arguments. ${denials >= config.max_denials ? 'Correction budget exhausted; stop.' : 'Correct the tool choice; do not repeat this request.'}` +
        (config.version === 2 && decision === 'block' ? ' ' + correctionHint(args, config.rules, config.test_catalog) : '');
      record({type:'decision', id:event.toolCallId, args, status, decision, denials, reason:decision === 'block' ? reason : null});
      if (decision === 'block') return {block:true, reason};
    } catch {
      broken = true;
      return {block:true, reason:'TEAM_DEV_GATE_UNAVAILABLE: command NOT executed; stop and report evidence failure.'};
    }
  });
}
