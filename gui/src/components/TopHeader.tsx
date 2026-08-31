import type {BatchClass, ConnectionStatus, ControlMode} from '../lib/types';
import type {DaemonState} from '../hooks/useDaemon';
import {formatWindow} from '../lib/contextWindow';
import {HeaderBatchSwitcher} from './HeaderBatchSwitcher';

interface TopHeaderProps {
	status: ConnectionStatus;
	daemon: DaemonState | null;
	theme: 'dark' | 'light';
	onTheme: () => void;
	onBatch: (batch: BatchClass) => void;
	onControl: (mode: ControlMode) => void;
	tokenEstimate: number;
}

const ENGINE: Record<ConnectionStatus, string> = {
	connecting: 'LINKING DAEMON',
	online: 'DAEMON LINKED',
	offline: 'ENGINE STOPPED',
};

export function TopHeader({status, daemon, theme, onTheme, onBatch, onControl, tokenEstimate}: TopHeaderProps) {
	const running = status === 'online';
	const workers = daemon?.workers ?? [];
	const liveCount = workers.filter(row => row.live).length;
	const control = daemon?.controlMode ?? 'autonomous';
	return (
		<header className="top-header">
			<div className="top-header__left">
				<h1 className="top-header__title font-display">
					AGENT ROBBER<span className="accent-cursor">_</span>
				</h1>
				<span className="top-header__sub font-display">CONTROL CENTER</span>
			</div>
			<div className="top-header__center">
				<div className={`header-badge header-badge--status${running ? ' running' : ''}`}>
					<span className={`pulse-dot${running ? ' on' : ''}`} />
					<span className="badge-label">{ENGINE[status]}</span>
				</div>
				<div className="header-badge">
					<span className="badge-label badge-label--dim">ENGINE</span>
					<span className="badge-value">{daemon?.model ?? '—'}</span>
				</div>
				<div className="header-badge">
					<span className="badge-label badge-label--dim">WORKERS</span>
					<span className="badge-value accent-text">
						{liveCount}/{Math.max(workers.length, 1)}
					</span>
				</div>
				<div className="header-badge" title={`Transcript ~${tokenEstimate} tokens`}>
					<span className="badge-label badge-label--dim">WINDOW</span>
					<span className="badge-value accent-text">
						{formatWindow(daemon?.contextWindow ?? 1_000_000_000_000)}
					</span>
				</div>
				<div className="header-badge">
					<span className="badge-label badge-label--dim">LOOP</span>
					<span className="badge-value accent-text">{control.toUpperCase()}</span>
				</div>
				<div className="header-badge header-badge--mem0">
					<span className="mem0-dot" />
					<span className="badge-label">
						{daemon ? `MEM0 ${daemon.ssotChunks} CHUNKS` : 'MEM0 WAITING'}
					</span>
				</div>
			</div>
			<div className="top-header__right">
				<div className="batch-switch" role="group" aria-label="Control suite">
					{(['autonomous', 'manual'] as const).map(mode => (
						<button
							key={mode}
							type="button"
							className={`batch-switch__btn${control === mode ? ' is-active' : ''}`}
							onClick={() => onControl(mode)}
						>
							{mode === 'autonomous' ? 'AUTO' : 'MANUAL'}
						</button>
					))}
				</div>
				<HeaderBatchSwitcher value={daemon?.batch ?? 'normal'} onChange={onBatch} />
				<button
					className={`theme-switch${theme === 'light' ? ' is-light' : ''}`}
					type="button"
					title="Toggle light / dark — synced with the TUI"
					onClick={onTheme}
				>
					<span className="theme-switch__label">{theme === 'light' ? 'LIGHT' : 'DARK'}</span>
					<span className="theme-switch__track">
						<span className="theme-switch__knob" />
					</span>
				</button>
			</div>
		</header>
	);
}
