import {useEffect, useState} from 'react';
import {formatClock, type ChatMessage, type ControlMode, type SuiteEvent, type SystemHealth} from '../lib/types';
import type {BusyPhase, DaemonState} from '../hooks/useDaemon';
import {IconSearch} from '../components/Icons';
import {ContextWindowPanel} from '../components/ContextWindowPanel';
import {SlashField} from '../components/CommandAutocomplete';

interface OverviewProps {
	daemon: DaemonState | null;
	messages: ChatMessage[];
	busy: BusyPhase | null;
	tpsHistory: number[];
	onMode: (mode: string) => boolean;
	onControl: (mode: ControlMode) => boolean;
	onApprove: () => boolean;
	onReject: () => boolean;
	onModel: (model: string) => boolean;
	onPrompt: (text: string) => void;
	onMemory: (text?: string) => boolean;
	onPurgeNotes: () => boolean;
	onSetWindow?: (window: string | number) => boolean;
	suiteEvents: SuiteEvent[];
}

export function Overview({
	daemon,
	messages,
	busy,
	tpsHistory,
	onMode,
	onControl,
	onApprove,
	onReject,
	onModel,
	onPrompt,
	onMemory,
	onPurgeNotes,
	onSetWindow,
	suiteEvents,
}: OverviewProps) {
	const [draft, setDraft] = useState('');
	const [memQuery, setMemQuery] = useState('');
	const [health, setHealth] = useState<SystemHealth | null>(null);
	const modes = daemon?.modes.length ? daemon.modes : ['plan', 'act', 'review'];
	const recent = messages.filter(message => message.role !== 'reasoning').slice(-8);
	const notes = daemon?.notes ?? [];
	const loop = daemon?.loop;
	const checkpointPending = loop?.checkpoint?.pending === true;
	const lastSuite = suiteEvents.slice(-4);

	function sendMonitor() {
		const text = draft.trim();
		if (!text) {
			return;
		}

		setDraft('');
		onPrompt(text);
	}

	useEffect(() => {
		let cancelled = false;
		fetch('http://127.0.0.1:8787/api/system/health')
			.then(response => (response.ok ? response.json() : null))
			.then((payload: SystemHealth | null) => {
				if (!cancelled && payload) {
					setHealth(payload);
				}
			})
			.catch(() => {
				if (!cancelled) {
					setHealth(null);
				}
			});
		return () => {
			cancelled = true;
		};
	}, [daemon?.controlMode, daemon?.loop?.phase]);

	return (
		<div className="view view--scroll">
			<div className="bento-grid">
				<article className="glass-card glass-card--wide">
					<div className="card__head">
						<span className="card__title font-display">PERFORMANCE AND ANALYTICS</span>
						<span className="card__badge font-mono font-bold">LATENCY: {daemon?.ttftMs ?? 0}ms</span>
					</div>
					<div className="card__body">
						<div className="graph-container">
							<div className="graph-y">
								<span className="graph-tick">100</span>
								<span className="graph-tick">75</span>
								<span className="graph-tick">50</span>
								<span className="graph-tick">25</span>
								<span className="graph-tick">0</span>
							</div>
							<div className="graph-area">
								<div className="graph-grid" />
								<div className="graph-bars">
									{tpsHistory.map((value, index) => (
										<div
											key={`bar-${index}`}
											className="graph-bar"
											style={{height: `${Math.max(4, value)}%`}}
										/>
									))}
								</div>
							</div>
						</div>
						<div className="stats-row">
							<div className="stat-cell">
							<span className="stat-lbl font-display">AVG LATENCY</span>
							<span className="stat-val font-mono font-bold">{daemon?.ttftMs ?? 0}ms</span>
							</div>
							<div className="stat-sep" />
							<div className="stat-cell">
							<span className="stat-lbl font-display">CLIENTS</span>
							<span className="stat-val font-mono font-bold">{daemon?.clients ?? 0}</span>
							</div>
							<div className="stat-sep" />
							<div className="stat-cell">
							<span className="stat-lbl font-display">TOKEN RATE</span>
							<span className="stat-val font-mono font-bold accent-text">{daemon?.tps ?? 0}/s</span>
							</div>
						</div>
					</div>
				</article>

				<article className="glass-card">
					<div className="card__head">
						<span className="card__title font-display">QUICK CONFIGURATION</span>
					</div>
					<div className="card__body card__body--config">
						<div className="cfg-row">
							<div className="cfg-info">
								<span className="cfg-label">TUI mode</span>
								<span className="cfg-val">{(daemon?.mode ?? 'act').toUpperCase()}</span>
							</div>
							<div className="log-filters">
								{modes.map(mode => (
									<button
										key={mode}
										type="button"
										className={`log-filter${daemon?.mode === mode ? ' active' : ''}`}
										onClick={() => onMode(mode)}
									>
										{mode.toUpperCase()}
									</button>
								))}
							</div>
						</div>
						<div className="cfg-row">
							<div className="cfg-info">
								<span className="cfg-label">Control suite</span>
								<span className="cfg-val">{(daemon?.controlMode ?? 'autonomous').toUpperCase()}</span>
							</div>
							<div className="log-filters">
								{(['autonomous', 'manual'] as const).map(mode => (
									<button
										key={mode}
										type="button"
										className={`log-filter${daemon?.controlMode === mode ? ' active' : ''}`}
										onClick={() => onControl(mode)}
									>
										{mode.toUpperCase()}
									</button>
								))}
							</div>
						</div>
						<div className="cfg-row">
							<div className="cfg-info">
								<span className="cfg-label">Active model</span>
								<span className="cfg-val">{daemon?.live ? 'LIVE' : 'FALLBACK'}</span>
							</div>
							<select
								className="glass-select"
								value={daemon?.model ?? ''}
								onChange={event => onModel(event.target.value)}
							>
								{(daemon?.models ?? []).map(model => (
									<option key={model} value={model}>
										{model}
									</option>
								))}
							</select>
						</div>
						<div className="cfg-row">
							<div className="cfg-info">
								<span className="cfg-label">SSOT persistence</span>
								<span className="cfg-val">
									{daemon ? `${daemon.ssotFiles} FILES` : 'WAITING'}
								</span>
							</div>
							<button
								type="button"
								className={`toggle${(daemon?.ssotFiles ?? 0) > 0 ? ' active' : ''}`}
								onClick={() => onMemory('reindex')}
							>
								<span className="toggle__knob" />
							</button>
						</div>
						<ContextWindowPanel
							compact
							windowTokens={daemon?.contextWindow}
							packedTokens={daemon?.contextPacked}
							hitCount={daemon?.contextHits}
							online={Boolean(daemon)}
							onApply={raw => onSetWindow?.(raw) === true}
						/>
					</div>
				</article>

				<article className="glass-card glass-card--wide">
					<div className="card__head">
						<span className="card__title font-display">AUTONOMOUS CONTROL SUITE</span>
						<span className={`card__badge${health?.ok ? '' : ' card__badge--dim'}`}>
							{health ? (health.ok ? 'HEALTHY' : 'DEGRADED') : 'WAITING'}
						</span>
					</div>
					<div className="card__body">
						<div className="stats-row">
							<div className="stat-cell">
							<span className="stat-lbl font-display">PHASE</span>
							<span className="stat-val font-mono font-bold">{(loop?.phase ?? 'idle').toUpperCase()}</span>
							</div>
							<div className="stat-sep" />
							<div className="stat-cell">
							<span className="stat-lbl font-display">WORKERS</span>
							<span className="stat-val font-mono font-bold">{loop?.playwright_workers ?? 0}</span>
							</div>
							<div className="stat-sep" />
							<div className="stat-cell">
							<span className="stat-lbl font-display">PROXIES</span>
							<span className="stat-val font-mono font-bold">
									{loop?.proxy_assigned ?? 0}/{loop?.proxy_count ?? 0}
								</span>
							</div>
							<div className="stat-sep" />
							<div className="stat-cell">
								<span className="stat-lbl font-display">HEAL / VISION</span>
								<span className="stat-val font-mono font-bold accent-text">
									{loop?.healer_bound ? 'ON' : 'OFF'} / {loop?.vision_ready ? 'ON' : 'OFF'}
								</span>
							</div>
						</div>
						<div className="suite-engines">
							{(['database', 'proxies', 'playwright', 'self_healing', 'vision', 'mcp', 'ssot_watch', 'ssot_context'] as const).map(name => {
								const engine = health?.engines?.[name];
								return (
									<div key={name} className={`suite-engine${engine?.ok ? ' is-ok' : ''}`}>
										<span className="suite-engine__name">{name.replace('_', ' ')}</span>
										<span className="suite-engine__val">{engine ? (engine.ok ? 'ok' : 'down') : '—'}</span>
									</div>
								);
							})}
						</div>
						{checkpointPending ? (
							<div className="checkpoint-bar">
								<span className="cfg-val">Heal patch waiting for operator</span>
								<button type="button" className="log-filter active" onClick={() => onApprove()}>
									APPROVE
								</button>
								<button type="button" className="log-filter" onClick={() => onReject()}>
									REJECT
								</button>
							</div>
						) : null}
						<div className="terminal" style={{marginTop: 12}}>
							{lastSuite.length === 0 ? (
								<div className="t-line">
									<span className="t-dim">Waiting for self_heal, vision_alert, vault_alert, worker_tick</span>
								</div>
							) : (
								lastSuite.map((event, index) => (
									<div className="t-line" key={`${event.type}-${event.timestamp}-${index}`}>
										<span className="t-prompt">{event.type}</span>{' '}
										<span className="t-info">{event.content.slice(0, 160)}</span>
									</div>
								))
							)}
						</div>
					</div>
				</article>

				<article className="glass-card">
					<div className="card__head">
						<span className="card__title font-display">LIVE CLI MONITOR</span>
						<span className={`card__badge${busy ? '' : ' card__badge--dim'}`}>
							{busy ? busy.label.toUpperCase() : 'IDLE'}
						</span>
					</div>
					<div className="card__body card__body--terminal">
						<div className="terminal">
							{recent.length === 0 ? (
								<div className="t-line">
									<span className="t-prompt">&gt;</span>{' '}
									<span className="t-dim">
										TUI is the agent. This pane monitors the daemon stream.
									</span>
								</div>
							) : (
								recent.map(message => (
									<div className="t-line" key={message.id}>
										<span className="t-dim">[{formatClock(message.timestamp)}]</span>{' '}
										<span
											className={
												message.role === 'error'
													? 't-err'
													: message.role === 'user'
														? 't-cmd'
														: message.role === 'assistant'
															? 't-body'
															: 't-info'
											}
										>
											{message.role === 'user' ? '> ' : ''}
											{message.content.slice(0, 140)}
										</span>
									</div>
								))
							)}
						</div>
						<div className="cli-bar">
							<span className="t-prompt">&gt;</span>
							<SlashField
								className="cli-input"
								value={draft}
								placeholder="Send a monitor prompt or /help..."
								ariaLabel="Monitor prompt"
								onChange={setDraft}
								onSubmit={text => {
									setDraft('');
									onPrompt(text);
								}}
							/>
							<button type="button" className="cli-send" onClick={sendMonitor}>
								▶
							</button>
						</div>
					</div>
				</article>

				<article className="glass-card glass-card--wide">
					<div className="card__head">
						<span className="card__title font-display">MEM0 KNOWLEDGE STORE</span>
						<span className="card__badge">{notes.length} NOTES</span>
					</div>
					<div className="card__body card__body--mem">
						<div className="mem-toolbar">
							<div className="mem-search-wrap">
								<IconSearch />
								<input
									className="mem-search"
									value={memQuery}
									placeholder="Search SSOT and injected notes..."
									onChange={event => setMemQuery(event.target.value)}
									onKeyDown={event => {
										if (event.key === 'Enter') {
											onMemory(memQuery);
										}
									}}
								/>
							</div>
							<button type="button" className="purge-btn" onClick={onPurgeNotes}>
								PURGE
							</button>
						</div>
						<div className="mem-list">
							{notes.length === 0 ? (
								<div className="mem-chip">
									<span className="mem-chip__key">ssot</span>
									<span className="mem-chip__val">
										{daemon
											? `${daemon.ssotChunks} chunks · ${daemon.ssotBackend}`
											: 'daemon offline'}
									</span>
								</div>
							) : (
								notes.slice(0, 12).map(note => (
									<div className="mem-chip" key={note.id}>
										<span className="mem-chip__key">{note.key}</span>
										<span className="mem-chip__val">{note.value}</span>
									</div>
								))
							)}
						</div>
					</div>
				</article>
			</div>
		</div>
	);
}
