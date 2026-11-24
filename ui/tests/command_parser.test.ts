import assert from 'node:assert/strict';
import {describe, it} from 'node:test';
import {SlashCommandParser, fuzzyCommands, parseSlash} from '../src/command_parser.ts';

describe('SlashCommandParser', () => {
  const parser = new SlashCommandParser();

  it('routes native slash commands before prompts', () => {
    for (const name of ['audit', 'sync', 'graph', 'resolve', 'pause', 'resume', 'snapshots', 'restore'] as const) {
      const parsed = parser.parse(`/${name}`);
      assert.equal(parsed.kind, 'command');
      assert.equal(parsed.name, name);
      assert.deepEqual(parsed.args, []);
    }
  });

  it('maps /horizon pause and /horizon resume', () => {
    assert.equal(parser.parse('/horizon pause').name, 'pause');
    assert.equal(parser.parse('/horizon resume extra').name, 'resume');
    assert.deepEqual(parser.parse('/horizon resume extra').args, ['extra']);
  });

  it('keeps raw prompts off the slash path', () => {
    const parsed = parser.parse('explain the task DAG');
    assert.equal(parsed.kind, 'prompt');
    assert.equal(parsed.text, 'explain the task DAG');
    assert.equal(parsed.name, '');
  });

  it('treats blank lines as empty', () => {
    assert.equal(parser.parse('   ').kind, 'empty');
    assert.equal(parser.parse('').kind, 'empty');
  });

  it('suggests close matches for unknown slashes', () => {
    const parsed = parser.parse('/audt');
    assert.equal(parsed.kind, 'unknown');
    assert.ok(parsed.suggestions.includes('audit'));
  });

  it('resolves aliases', () => {
    assert.equal(parser.parse('/reindex').name, 'audit');
    assert.equal(parser.parse('/dag').name, 'graph');
    assert.equal(parser.parse('/quit').name, 'exit');
  });

  it('exposes help text for /audit and /graph', () => {
    const help = parser.help();
    assert.match(help, /\/audit/);
    assert.match(help, /\/graph/);
    assert.match(help, /\/snapshots/);
    assert.match(help, /\/restore/);
    assert.match(parser.help('audit'), /Rust engine/);
  });

  it('exports parseSlash helper', () => {
    assert.equal(parseSlash('/sync now').kind, 'command');
    assert.deepEqual(parseSlash('/sync now').args, ['now']);
  });

  it('fuzzy ranks exact names first', () => {
    const hits = fuzzyCommands('graph');
    assert.equal(hits[0]?.name, 'graph');
  });
});
