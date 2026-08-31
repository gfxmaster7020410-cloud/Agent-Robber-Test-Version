import React from 'react';
import {render} from 'ink-testing-library';
import App from '../source/app.js';
import {DEFAULT_DAEMON_URL} from '../source/hooks/useDaemon.js';

// Integration check: requires the daemon to be running.
//   python server.py
const instance = render(<App />);

const wait = async (ms: number) => {
	await new Promise(resolve => {
		setTimeout(resolve, ms);
	});
};

const type = async (text: string) => {
	for (const char of text) {
		instance.stdin.write(char);
		await wait(15);
	}
};

await wait(700);

if (!(instance.lastFrame() ?? '').includes('Linked')) {
	console.error(
		`verify-frame needs the daemon at ${DEFAULT_DAEMON_URL}.\n` +
			'Start it from the project root with:  python server.py',
	);
	instance.unmount();
	process.exit(1);
}

await type('scan the project files');
await wait(100);
instance.stdin.write('\r');

// Mid-turn: the daemon has streamed the echo and phase badges, but the reply
// has not landed yet and pushed them out of the viewport.
await wait(1400);
const midFrame = instance.lastFrame() ?? '';

await wait(2200);
const frame = instance.lastFrame() ?? '';
console.log('--- FRAME START ---');
console.log(frame);
console.log('--- FRAME END ---');

const checks: Array<[string, boolean]> = [
	['daemon linked', frame.includes('Linked')],
	['footer natural spacing', /Ctrl\+C Exit/.test(frame) && /Ctrl\+L Clear/.test(frame)],
	['canvas pane', frame.includes('REASONING CANVAS')],
	['no spaced-out Exit', !frame.includes('E x i t')],
	['user prompt echoed by daemon', midFrame.includes('❯ scan the project files')],
	['memory badge (mid-turn)', midFrame.includes('MEM0 RECALL')],
	['tool badge (mid-turn)', midFrame.includes('TOOL RUNNING')],
	['spinner label (mid-turn)', /Thinking|Recalling|Executing/.test(midFrame)],
	['done badge', /✔ DONE\s+Turn complete/.test(frame)],
	['code tab or plan', /TypeScript|agent plan preview/.test(frame)],
	['code bottom border', (frame.match(/╰─+/g) ?? []).length >= 2],
	['assistant visible', frame.includes('agent plan preview') || frame.includes('Proposed next step')],
	['real tool listing', /my-agent\/|server\.py|jailed listing/.test(frame)],
	['graph tree glyphs', frame.includes('├── preference') && frame.includes('└── convention')],
];

let failed = 0;
for (const [label, ok] of checks) {
	console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}`);
	if (!ok) {
		failed += 1;
	}
}

instance.unmount();
process.exit(failed > 0 ? 1 : 0);
