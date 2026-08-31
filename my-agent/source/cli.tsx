#!/usr/bin/env node
import React from 'react';
import {render} from 'ink';
import meow from 'meow';
import App from './app.js';
import {DEFAULT_DAEMON_URL} from './hooks/useDaemon.js';
import {createStableStdout} from './utils/stableStdout.js';

const cli = meow(
	`
		Usage
		  $ my-agent

		Options
		  --model       Display name of the active model
		  --daemon      WebSocket URL of the Agent Robber daemon
		  --no-memory   Start with the SSOT memory link offline

		Examples
		  $ my-agent
		  $ my-agent --model="GPT-X / Local"
		  $ my-agent --daemon="ws://127.0.0.1:8787/ws"
		  $ my-agent --no-memory
	`,
	{
		importMeta: import.meta,
		flags: {
			model: {type: 'string'},
			daemon: {type: 'string', default: DEFAULT_DAEMON_URL},
			memory: {type: 'boolean', default: true},
		},
	},
);

render(
	<App
		model={cli.flags.model}
		daemonUrl={cli.flags.daemon}
		memoryActive={cli.flags.memory}
	/>,
	{
		stdout: createStableStdout(),
		// Console patches call log.clear() which flushes the frame on stray logs.
		patchConsole: false,
	},
);
