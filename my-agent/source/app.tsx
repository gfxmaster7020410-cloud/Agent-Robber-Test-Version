import React, {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {Box, Text, useApp} from 'ink';
import Spinner from 'ink-spinner';
import {cwd} from 'node:process';
import {Header} from './components/Header.js';
import {MessageList} from './components/MessageList.js';
import {InputBox} from './components/InputBox.js';
import {Sidebar} from './components/Sidebar.js';
import {Footer} from './components/Footer.js';
import {ReasoningCanvas} from './components/ReasoningCanvas.js';
import {DEFAULT_DAEMON_URL, useDaemon} from './hooks/useDaemon.js';
import {useTerminalSize} from './hooks/useTerminalSize.js';
import {formatHelp, parseSlash} from './lib/slashCommands.js';
import {BRAND, type UiTheme} from './utils/theme.js';
import {inkProps} from './utils/typography.js';

const SIDEBAR_WIDTH = 32;
const CANVAS_SIDE_WIDTH = 36;
const CANVAS_DOCK_HEIGHT = 6;
const WIDE_CANVAS = 120;
const RESERVED_LINES = 9;

function formatWindow(value: number): string {
	const n = Math.trunc(value);
	if (!Number.isFinite(n) || n < 0) {
		return '—';
	}
	if (n === 0) {
		return '0';
	}
	if (n >= 1_000_000_000_000 && n % 1_000_000_000_000 === 0) {
		return `${n / 1_000_000_000_000}T`;
	}
	if (n >= 1_000_000_000 && n % 1_000_000_000 === 0) {
		return `${n / 1_000_000_000}B`;
	}
	if (n >= 1_000_000 && n % 1_000_000 === 0) {
		return `${n / 1_000_000}M`;
	}
	if (n >= 1_000 && n % 1_000 === 0) {
		return `${n / 1_000}K`;
	}
	return n.toLocaleString();
}

const OFFLINE_HINT = [
	'Daemon unreachable — start it from the project root:',
	'',
	'    python server.py',
	'',
	'The TUI reconnects on its own once the daemon is up.',
].join('\n');

interface AppProps {
	model?: string;
	memoryActive?: boolean;
	daemonUrl?: string;
}

export default function App({
	model = BRAND.defaultModel,
	memoryActive = true,
	daemonUrl = DEFAULT_DAEMON_URL,
}: AppProps) {
	const {exit} = useApp();
	const {
		status,
		daemon,
		messages,
		busyPhase,
		sendPrompt,
		sendSlash,
		approveHeal,
		rejectHeal,
		mcpRemove,
		mcpCall,
		pushLocal,
		clear,
	} = useDaemon(daemonUrl);
	const {rows: terminalRows, columns} = useTerminalSize();
	const [canvasOpen, setCanvasOpen] = useState(true);

	useEffect(() => {
		pushLocal(
			'system',
			`Agent Robber online — connecting to ${daemonUrl}. Type /help for commands.`,
		);
		// Intentionally runs once on mount to seed the transcript.
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, []);

	const clearConversation = useCallback(() => {
		clear('Conversation cleared.');
	}, [clear]);

	const conversation = useMemo(
		() => messages.filter(message => message.role !== 'reasoning'),
		[messages],
	);

	const canvasBody = useMemo(() => {
		for (let index = messages.length - 1; index >= 0; index -= 1) {
			if (messages[index]!.role === 'reasoning') {
				return messages[index]!.content;
			}
		}

		return '';
	}, [messages]);

	const handleCommand = useCallback(
		(command: string) => {
			const parsed = parseSlash(command);
			const verb = parsed.name;
			const mcpAction = (parsed.args[0] ?? 'list').toLowerCase();
			const mcpRest = parsed.args.slice(1);

			if (verb === 'clear') {
				clearConversation();
				return;
			}
			if (verb === 'canvas') {
				setCanvasOpen(open => {
					const next = !open;
					pushLocal(
						'system',
						next ? 'Reasoning canvas shown.' : 'Reasoning canvas hidden.',
					);
					return next;
				});
				return;
			}
			if (verb === 'approve') {
				if (!approveHeal()) {
					pushLocal('error', 'Daemon offline — cannot approve the heal checkpoint.');
				}
				return;
			}
			if (verb === 'reject') {
				if (!rejectHeal()) {
					pushLocal('error', 'Daemon offline — cannot reject the heal checkpoint.');
				}
				return;
			}
			if (verb === 'help' || verb === 'h' || verb === '?') {
				pushLocal('system', formatHelp());
				return;
			}
			if (verb === 'status') {
				pushLocal(
					'system',
					[
						`daemon   ${daemonUrl}`,
						`link     ${status}`,
						`mode     ${daemon?.mode ?? '—'}`,
						`control  ${daemon?.controlMode ?? '—'}`,
						`loop     ${daemon?.loopPhase ?? '—'}`,
						`mcp      ${daemon ? `${daemon.mcpEnabled} enabled / ${daemon.mcpTools} tools` : '—'}`,
						`model    ${daemon?.model ?? '—'}`,
						`engine   ${daemon?.engine ?? '—'}`,
						`live     ${daemon?.live === true ? 'yes' : 'no'}`,
						`ssot     ${daemon ? `${daemon.ssotFiles} files / ${daemon.ssotChunks} chunks (${daemon.ssotBackend}, ${daemon.ssotSkipped} skipped, watch ${daemon.ssotWatching ? 'on' : 'off'})` : '—'}`,
						`window   ${daemon ? `${formatWindow(daemon.contextWindow)} · packed ${formatWindow(daemon.contextPacked)}` : '—'}`,
						`theme    ${daemon?.theme ?? 'dark'}`,
						`canvas   ${canvasOpen ? 'on' : 'off'}`,
					].join('\n'),
				);
				return;
			}
			if (verb === 'exit' || verb === 'quit') {
				exit();
				const timer = setTimeout(() => process.exit(0), 250);
				timer.unref();
				return;
			}

			if (
				(verb === 'mcp' || verb === 'mcps') &&
				(mcpAction === 'call' || mcpAction === 'remove' || mcpAction === 'add')
			) {
				if (mcpAction === 'remove') {
					const id = mcpRest[0] ?? '';
					if (!id) {
						pushLocal('error', 'Usage: /mcp remove <id>');
						return;
					}
					if (!mcpRemove(id)) {
						pushLocal('error', 'Daemon offline — cannot update MCP.');
					}
					return;
				}
				const server = mcpRest[0] ?? '';
				const tool = mcpRest[1] ?? '';
				if (!server || !tool) {
					pushLocal('error', 'Usage: /mcp call <server> <tool> [json]');
					return;
				}
				if (!mcpCall(server, tool, mcpRest.slice(2).join(' '))) {
					pushLocal('error', 'Daemon offline — cannot call MCP.');
				}
				return;
			}

			const remoteName = verb === 'mcp' ? 'mcps' : verb;
			if (sendSlash(remoteName, parsed.args, command)) {
				return;
			}

			switch (verb) {
				case 'keys': {
					pushLocal('error', 'Daemon offline — cannot read the key vault.');
					break;
				}
				case 'purge': {
					pushLocal('error', 'Daemon offline — cannot purge processes.');
					break;
				}
				case 'mode': {
					pushLocal(
						'system',
						`Turn: ${daemon?.mode ?? '—'} · control: ${daemon?.controlMode ?? '—'}. Use /mode plan | act | review, or /mode autonomous | manual.`,
					);
					break;
				}
				case 'memory': {
					pushLocal('error', 'Daemon offline — cannot query the SSOT index.');
					break;
				}
				case 'window':
				case 'context': {
					pushLocal('error', 'Daemon offline — default window is 1T.');
					break;
				}
				case 'mcp':
				case 'mcps': {
					pushLocal('error', 'Daemon offline — cannot list MCP servers.');
					break;
				}
				case 'theme': {
					pushLocal('error', 'Daemon offline — cannot sync theme with the GUI.');
					break;
				}
				default: {
					pushLocal(
						'error',
						`Unknown command: ${command}. Type /help for the list.`,
					);
				}
			}
		},
		[
			approveHeal,
			canvasOpen,
			clearConversation,
			daemon,
			daemonUrl,
			exit,
			mcpCall,
			mcpRemove,
			pushLocal,
			rejectHeal,
			sendSlash,
			status,
		],
	);

	const busy = busyPhase !== null || daemon?.busy === true;

	const handleSubmit = useCallback(
		(raw: string) => {
			const query = raw.trim();
			if (query.length === 0 || busy) {
				return;
			}

			if (query.startsWith('/')) {
				handleCommand(query);
				return;
			}

			try {
				if (!sendPrompt(query)) {
					pushLocal('user', query);
					pushLocal('error', OFFLINE_HINT);
				}
			} catch (error) {
				pushLocal('user', query);
				pushLocal('error', `Submission error: ${error instanceof Error ? error.message : 'Unknown error'}`);
			}
		},
		[busy, handleCommand, pushLocal, sendPrompt],
	);

	const handleSubmitRef = useRef(handleSubmit);
	handleSubmitRef.current = handleSubmit;
	const onSubmit = useCallback((raw: string) => {
		handleSubmitRef.current(raw);
	}, []);

	const onClear = clearConversation;

	const theme: UiTheme = daemon?.theme === 'light' ? 'light' : 'dark';
	const showSidebar = columns >= 80;
	const sideBySide = canvasOpen && columns >= WIDE_CANVAS;
	const docked = canvasOpen && !sideBySide;
	// Ink issues clearTerminal whenever outputHeight >= stdout.rows, which on
	// Windows looks like the TUI vanishing on every keystroke. Stay one row short.
	const frameRows = Math.max(1, terminalRows - 1);
	const canvasWidth = sideBySide
		? CANVAS_SIDE_WIDTH
		: showSidebar
			? Math.max(24, columns - SIDEBAR_WIDTH - 2)
			: Math.max(24, columns - 2);
	const reservedLines = RESERVED_LINES + (docked ? CANVAS_DOCK_HEIGHT : 0);
	const messageWidth = showSidebar
		? Math.max(24, columns - SIDEBAR_WIDTH - 2 - (sideBySide ? CANVAS_SIDE_WIDTH : 0))
		: Math.max(24, columns - 2 - (sideBySide ? CANVAS_SIDE_WIDTH : 0));

	return (
		<Box flexDirection="column" height={frameRows}>
			<Header
				model={daemon?.model ?? model}
				workingDirectory={cwd()}
				memoryActive={memoryActive}
				connection={status}
				theme={theme}
				controlMode={daemon?.controlMode}
			/>
			<Box flexDirection="row" flexGrow={1} overflow="hidden">
				<Box flexDirection="column" flexGrow={1} flexBasis={0} minWidth={0}>
					<Box flexDirection="row" flexGrow={1} overflow="hidden">
						<Box flexDirection="column" flexGrow={1} flexBasis={0} minWidth={0}>
							<MessageList
								messages={conversation}
								reservedLines={reservedLines}
								busy={busy}
								width={messageWidth}
								rows={frameRows}
							/>
							{busy ? (
								<Box marginLeft={1} marginBottom={1} gap={1}>
									<Text color="yellow">
										<Spinner type="dots" />
									</Text>
									<Text {...inkProps('status')} color="yellow">
										{busyPhase?.label ?? 'Working...'}
									</Text>
								</Box>
							) : null}
						</Box>
						{sideBySide ? (
							<ReasoningCanvas
								body={canvasBody}
								engine={daemon?.engine ?? 'fallback'}
								live={daemon?.live === true}
								width={CANVAS_SIDE_WIDTH}
								height={Math.max(8, frameRows - reservedLines)}
							/>
						) : null}
					</Box>
					{docked ? (
						<ReasoningCanvas
							body={canvasBody}
							engine={daemon?.engine ?? 'fallback'}
							live={daemon?.live === true}
							width={canvasWidth}
							height={CANVAS_DOCK_HEIGHT}
						/>
					) : null}
				</Box>
				{showSidebar ? (
					<Sidebar
						width={SIDEBAR_WIDTH}
						busyKind={busyPhase?.kind ?? null}
						memoryActive={memoryActive}
						theme={theme}
					/>
				) : null}
			</Box>
			<InputBox
				onSubmit={onSubmit}
				onClear={onClear}
				busy={busy}
				theme={theme}
			/>
			<Footer
				memoryActive={memoryActive}
				columns={columns}
				connection={status}
			/>
		</Box>
	);
}
