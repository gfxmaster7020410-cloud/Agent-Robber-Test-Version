import React from 'react';
import {render} from 'ink-testing-library';
import App from '../source/app.js';

// Deliberately unused port so the daemon link is deterministically offline.
const DEAD_DAEMON = 'ws://127.0.0.1:59599/ws';

const instance = render(<App daemonUrl={DEAD_DAEMON} />);

const wait = async (ms: number) => {
	await new Promise(resolve => {
		setTimeout(resolve, ms);
	});
};

const type = async (text: string) => {
	for (const char of text) {
		instance.stdin.write(char);
		await wait(15);
		assertFrameContains(
			instance.lastFrame(),
			'header while typing',
			'AGENT ROBBER CLI',
		);
	}
};

function assertFrameContains(
	frame: string | undefined,
	label: string,
	needle: string,
) {
	if (!frame || !frame.includes(needle)) {
		console.error(`smoke check failed: ${label} missing`);
		process.exit(1);
	}
}

await wait(400);
assertFrameContains(instance.lastFrame(), 'header', 'AGENT ROBBER CLI');
assertFrameContains(instance.lastFrame(), 'canvas pane', 'REASONING CANVAS');

await type('/help');
await wait(100);
instance.stdin.write('\r');
await wait(300);
assertFrameContains(instance.lastFrame(), '/help output', '/clear');
assertFrameContains(instance.lastFrame(), '/help daemon note', '/status');
assertFrameContains(instance.lastFrame(), '/help canvas', '/canvas');
assertFrameContains(instance.lastFrame(), '/help purge', '/purge');
assertFrameContains(instance.lastFrame(), '/help mcp', '/mcp');
assertFrameContains(instance.lastFrame(), '/help window', '/window');

// Give the failed connection time to settle into the offline state.
await wait(900);
assertFrameContains(instance.lastFrame(), 'offline footer', 'daemon offline');
assertFrameContains(instance.lastFrame(), 'offline header', 'Offline');

await type('scan the project files');
await wait(100);
instance.stdin.write('\r');
await wait(300);

assertFrameContains(
	instance.lastFrame(),
	'user prompt echo',
	'❯ scan the project files',
);
assertFrameContains(
	instance.lastFrame(),
	'offline guidance',
	'python server.py',
);

instance.unmount();
console.log('smoke check passed (offline mode)');
process.exit(0);
