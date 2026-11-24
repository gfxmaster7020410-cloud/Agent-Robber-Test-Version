import {useCallback, useEffect, useMemo, useState} from 'react';
import {useDaemon} from './hooks/useDaemon';
import {formatHelp, parseSlash} from './lib/slashCommands';
import {TopHeader} from './components/TopHeader';
import {Sidebar} from './components/Sidebar';
import {FBSharkMonitor} from './components/FBSharkMonitor';
import {Overview} from './views/Overview';
import {ConfigView} from './views/Config';
import {MeMOStoreInspector} from './components/MeMOStore';
import {ConsoleView} from './views/Console';
import {LogsView} from './views/Logs';
import {MarketplaceView} from './views/Marketplace';
import type {DashboardView} from './lib/types';
import {Workbench} from './workbench/Workbench';
import {formatWindow} from './lib/contextWindow';

const THEME_KEY = 'robber-theme';
const VIEWS: readonly DashboardView[] = [
	'workbench',
	'overview',
	'config',
	'memo-store',
	'console',
	'logs',
	'fb-shark',
	'mcp',
];

function parseDashboardView(hash: string): DashboardView {
	const raw = hash.replace(/^#\/?/, '').split(/[/?#]/)[0]?.toLowerCase() ?? '';
	return VIEWS.includes(raw as DashboardView) ? (raw as DashboardView) : 'workbench';
}

export default function App() {
	const {
		status,
		daemon,
		messages,
		busyPhase,
		tpsHistory,
		suiteEvents,
		sendPrompt,
		sendSlash,
		setDashboardMode,
		triggerStepAction,
		killTask,
		setMode,
		setControlMode,
		approveHeal,
		rejectHeal,
		setModel,
		requestMemory,
		addModel,
		patchModel,
		pingModel,
		setBatch,
		scanOllama,
		ollamaScan,
		setKey,
		setConsensus,
		purgeNotes,
		ping,
		setTheme,
		setContextWindow,
		mcpList,
		mcpEnable,
		mcpDisable,
		mcpAdd,
		mcpRemove,
		mcpCall,
		lastMcpResult,
		pushLocal,
		clear,
		purge,
	} = useDaemon();
	const [view, setView] = useState<DashboardView>(() => parseDashboardView(window.location.hash));
	const [theme, setThemeLocal] = useState<'dark' | 'light'>(() => {
		const stored = window.localStorage.getItem(THEME_KEY);
		return stored === 'light' ? 'light' : 'dark';
	});

	useEffect(() => {
		if (daemon?.theme === 'light' || daemon?.theme === 'dark') {
			setThemeLocal(daemon.theme);
		}
	}, [daemon?.theme]);

	useEffect(() => {
		document.documentElement.setAttribute('data-theme', theme);
		window.localStorage.setItem(THEME_KEY, theme);
	}, [theme]);

	useEffect(() => {
		window.location.hash = view === 'workbench' ? 'workbench' : view;
	}, [view]);

	useEffect(() => {
		if (view === 'workbench') {
			return;
		}
		pushLocal(
			'system',
			'Control Center attached. The TUI is the agent; this dashboard configures, monitors, and maintains it.',
		);
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, []);

	const busy = busyPhase !== null || daemon?.busy === true;
	const tokenEstimate = useMemo(
		() =>
			messages.reduce((sum, message) => sum + Math.ceil(message.content.length / 4), 0),
		[messages],
	);

	const handleCommand = useCallback(
		(command: string) => {
			const parsed = parseSlash(command);
			const verb = parsed.name;
			const arg = parsed.args.join(' ').trim();
			const mcpAction = (parsed.args[0] ?? 'list').toLowerCase();
			const mcpRest = parsed.args.slice(1);

			if (verb === 'clear') {
				clear('Conversation cleared.');
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
						`link     ${status}`,
						`mode     ${daemon?.mode ?? '—'}`,
						`model    ${daemon?.model ?? '—'}`,
						`engine   ${daemon?.engine ?? '—'}`,
						`live     ${daemon?.live === true ? 'yes' : 'no'}`,
						`theme    ${theme}`,
						`batch    ${daemon?.batch ?? '—'}`,
						`control  ${daemon?.controlMode ?? '—'}`,
						`loop     ${daemon?.loop?.phase ?? '—'}`,
						`mcp      ${daemon?.mcp ? `${daemon.mcp.enabled} enabled / ${daemon.mcp.tools.length} tools` : '—'}`,
						`ssot     ${daemon ? `${daemon.ssotFiles} files / ${daemon.ssotChunks} chunks${daemon.ssotWatching ? ' · watching' : ''}` : '—'}`,
						`window   ${daemon ? `${formatWindow(daemon.contextWindow)} · packed ${formatWindow(daemon.contextPacked)}` : '—'}`,
						`clients  ${daemon?.clients ?? 0}`,
					].join('\n'),
				);
				return;
			}
			if (verb === 'theme') {
				let next: 'dark' | 'light' = theme === 'dark' ? 'light' : 'dark';
				if (arg === 'dark' || arg === 'light') {
					next = arg;
				} else if (arg && arg !== 'toggle') {
					pushLocal('error', 'Usage: /theme  or  /theme dark|light');
					return;
				}
				setThemeLocal(next);
				if (!sendSlash('theme', [next], command) && !setTheme(next)) {
					pushLocal(
						'system',
						`Theme set locally to ${next}. Start the daemon to sync it with the TUI.`,
					);
				}
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

			if (verb === 'keys') {
				pushLocal('error', 'Daemon offline — cannot read the key vault.');
				return;
			}
			if (verb === 'purge') {
				pushLocal('error', 'Daemon offline — cannot purge processes.');
				return;
			}
			if (verb === 'mode') {
				pushLocal(
					'system',
					`Turn: ${daemon?.mode ?? '—'} · control: ${daemon?.controlMode ?? '—'}. Use /mode plan | act | review, or /mode autonomous | manual.`,
				);
				return;
			}
			if (verb === 'memory') {
				pushLocal('error', 'Daemon offline — cannot query SSOT.');
				return;
			}
			if (verb === 'window' || verb === 'context') {
				pushLocal(
					'system',
					daemon
						? `SSOT window ${formatWindow(daemon.contextWindow)} · packed ${formatWindow(daemon.contextPacked)} · ${daemon.contextHits} hits`
						: 'SSOT window — daemon offline (default 1T)',
				);
				return;
			}
			if (verb === 'mcp' || verb === 'mcps') {
				pushLocal('error', 'Daemon offline — cannot list MCP servers.');
				return;
			}
			if (
				verb === 'undo' ||
				verb === 'redo' ||
				verb === 'accept' ||
				verb === 'revert' ||
				verb === 'accept-all' ||
				verb === 'revert-all'
			) {
				pushLocal('error', `Daemon offline — cannot ${verb}.`);
				return;
			}
			pushLocal('error', `Unknown command: ${command}`);
		},
		[approveHeal, clear, daemon, mcpCall, mcpRemove, pushLocal, rejectHeal, sendSlash, setTheme, status, theme],
	);

	const handleSubmit = useCallback(
		(query: string) => {
			if (!query || busy) {
				return;
			}

			if (query.startsWith('/')) {
				handleCommand(query);
				return;
			}

			if (!sendPrompt(query)) {
				pushLocal('user', query);
				pushLocal(
					'error',
					'Daemon unreachable — start it with python server.py, then this page reconnects.',
				);
			}
		},
		[busy, handleCommand, pushLocal, sendPrompt],
	);

	const statusLabel = busyPhase?.label.toUpperCase() ?? (busy ? 'BUSY' : 'IDLE');

	if (view === 'workbench') {
		return (
			<Workbench
				daemon={daemon}
				messages={messages}
				busy={busyPhase}
				onSubmit={handleSubmit}
				onUndo={() => {
					if (!triggerStepAction('undo') && !sendSlash('undo', [], '/undo')) {
						pushLocal('error', 'Daemon offline — cannot undo the last write.');
					}
				}}
				onRedo={() => {
					if (!triggerStepAction('redo') && !sendSlash('redo', [], '/redo')) {
						pushLocal('error', 'Daemon offline — cannot redo the last write.');
					}
				}}
				onAccept={() => {
					approveHeal();
					if (!triggerStepAction('accept_all') && !sendSlash('accept', [], '/accept')) {
						pushLocal('system', 'Nothing pending to accept.');
					}
				}}
				onRevert={() => {
					rejectHeal();
					if (!triggerStepAction('revert_all') && !sendSlash('revert', [], '/revert')) {
						pushLocal('error', 'Daemon offline — cannot revert.');
					}
				}}
				onKill={() => {
					if (!killTask()) {
						purge();
					}
				}}
				onPlan={() => {
					if (!setDashboardMode('Plan Mode') && !setMode('plan')) {
						pushLocal('error', 'Daemon offline — cannot switch to plan mode.');
					}
				}}
				onHybrid={() => {
					if (!setDashboardMode('Hybrid Multi-LLM')) {
						pushLocal('error', 'Daemon offline — cannot arm Hybrid Multi-LLM.');
					}
				}}
				onControl={() => {
					const next = daemon?.controlMode === 'manual' ? 'Autonomous' : 'Manual';
					if (!setDashboardMode(next) && !setControlMode(next.toLowerCase() as 'manual' | 'autonomous')) {
						pushLocal('error', 'Daemon offline — cannot switch the control suite.');
					}
				}}
				onBeast={() => {
					if (!setDashboardMode('Beast Mode') && !sendSlash('mode', ['best'], '/mode best')) {
						pushLocal('error', 'Daemon offline — cannot switch Beast routing.');
					}
				}}
				onView={setView}
			/>
		);
	}

	return (
		<>
			<div className="ambient-orb ambient-orb--1" />
			<div className="ambient-orb ambient-orb--2" />
			<div className="ambient-orb ambient-orb--3" />
			<div className="app-layout">
				<TopHeader
					status={status}
					daemon={daemon}
					theme={theme}
					onTheme={() => {
						const next = theme === 'dark' ? 'light' : 'dark';
						setThemeLocal(next);
						if (!setTheme(next)) {
							pushLocal(
								'system',
								`Theme set locally to ${next}. Start the daemon to sync it with the TUI.`,
							);
						}
					}}
					tokenEstimate={tokenEstimate}
					onBatch={batch => {
						if (!setBatch(batch)) {
							pushLocal(
								'system',
								`Batch set locally is not persisted until the daemon is online. Start python server.py.`,
							);
						}
					}}
					onControl={mode => {
						if (!setControlMode(mode)) {
							pushLocal('error', 'Daemon offline — cannot switch the control suite.');
						}
					}}
				/>
				<Sidebar
					view={view}
					onView={setView}
					online={status === 'online'}
					onEngage={() => {
						if (!ping()) {
							pushLocal(
								'error',
								'Daemon is offline. Start it with python server.py — the TUI and this dashboard both attach to :8787.',
							);
						}
					}}
				/>
				<main id="views">
					{view === 'overview' ? (
						<Overview
							daemon={daemon}
							messages={messages}
							busy={busyPhase}
							tpsHistory={tpsHistory}
							onMode={setMode}
							onControl={setControlMode}
							onApprove={approveHeal}
							onReject={rejectHeal}
							onModel={setModel}
							onPrompt={handleSubmit}
							onMemory={requestMemory}
							onPurgeNotes={purgeNotes}
							onSetWindow={setContextWindow}
							suiteEvents={suiteEvents}
						/>
					) : null}
					{view === 'config' ? (
						<ConfigView
							daemon={daemon}
							ollamaScan={ollamaScan}
							onSetKey={setKey}
							onAddModel={addModel}
							onPatchModel={patchModel}
							onScanOllama={scanOllama}
							onSetModel={setModel}
							onConsensus={setConsensus}
							onPingModel={pingModel}
							onPingAll={() => { ping(); return true; }}
							onSetWindow={setContextWindow}
						/>
					) : null}
					{view === 'memo-store' ? (
						<MeMOStoreInspector
							daemon={daemon}
							online={status === 'online'}
							onSetWindow={setContextWindow}
						/>
					) : null}
					{view === 'console' ? (
						<ConsoleView
							messages={messages}
							busy={busyPhase}
							statusLabel={statusLabel}
							onSubmit={handleSubmit}
							onClear={() => clear('Conversation cleared.')}
							onExport={() => exportLog(messages)}
						/>
					) : null}
					{view === 'logs' ? <LogsView messages={messages} suiteEvents={suiteEvents} /> : null}
					{view === 'mcp' ? (
						<MarketplaceView
							daemon={daemon}
							online={status === 'online'}
							lastResult={lastMcpResult}
							onEnable={mcpEnable}
							onDisable={mcpDisable}
							onAdd={mcpAdd}
							onRemove={mcpRemove}
							onCall={mcpCall}
							onList={mcpList}
						/>
					) : null}
					{view === 'fb-shark' ? <FBSharkMonitor suiteEvents={suiteEvents} /> : null}
				</main>
			</div>
		</>
	);
}

function exportLog(messages: {role: string; content: string; timestamp: number}[]): void {
	const body = messages
		.filter(message => message.role !== 'reasoning')
		.map(message => `${new Date(message.timestamp).toISOString()} ${message.role} ${message.content}`)
		.join('\n');
	const blob = new Blob([body], {type: 'text/plain'});
	const url = URL.createObjectURL(blob);
	const link = document.createElement('a');
	link.href = url;
	link.download = 'agent-robber-console.log';
	link.click();
	URL.revokeObjectURL(url);
}
