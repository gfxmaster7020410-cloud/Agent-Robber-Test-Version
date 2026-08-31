import {useMemo, useState} from 'react';
import {formatClock, type ChatMessage, type SuiteEvent} from '../lib/types';

type Level = 'all' | 'info' | 'warn' | 'error';

interface LogsViewProps {
	messages: ChatMessage[];
	suiteEvents?: SuiteEvent[];
}

export function LogsView({messages, suiteEvents = []}: LogsViewProps) {
	const [level, setLevel] = useState<Level>('all');
	const rows = useMemo(
		() => {
			const fromChat = messages
				.filter(message => message.role !== 'reasoning')
				.map(message => ({
					id: `msg-${message.id}`,
					time: formatClock(message.timestamp),
					level: levelOf(message),
					msg: `${message.role}: ${message.content.slice(0, 240)}`,
				}));
			const fromSuite = suiteEvents.map((event, index) => ({
				id: `suite-${event.type}-${event.timestamp}-${index}`,
				time: formatClock(event.timestamp * 1000),
				level: event.type === 'checkpoint' || event.type === 'self_heal' ? 'warn' : 'info',
				msg: `${event.type}: ${event.content.slice(0, 240)}`,
			}));
			return [...fromChat, ...fromSuite]
				.filter(row => level === 'all' || row.level === level);
		},
		[level, messages, suiteEvents],
	);

	return (
		<div className="view view--scroll">
			<div className="glass-card full-card">
				<div className="card__head">
					<span className="card__title font-display">SYSTEM AUDIT LOGS</span>
					<div className="log-filters">
						{(['all', 'info', 'warn', 'error'] as const).map(item => (
							<button
								key={item}
								type="button"
								className={`log-filter${level === item ? ' active' : ''}`}
								onClick={() => setLevel(item)}
							>
								{item.toUpperCase()}
							</button>
						))}
					</div>
				</div>
				<div className="card__body card__body--scroll">
					<div className="log-list">
						{rows.length === 0 ? (
							<div className="log-entry">
								<span className="log-msg">No log lines for this filter.</span>
							</div>
						) : (
							rows.map(row => (
								<div className="log-entry" key={row.id}>
									<span className="log-time font-mono font-bold">{row.time}</span>
									<span className={`log-level font-mono font-bold log-level--${row.level}`}>{row.level.toUpperCase()}</span>
									<span className="log-msg">{row.msg}</span>
								</div>
							))
						)}
					</div>
				</div>
			</div>
		</div>
	);
}

function levelOf(message: ChatMessage): Exclude<Level, 'all'> {
	if (message.role === 'error') {
		return 'error';
	}

	if (message.role === 'tool') {
		return 'warn';
	}

	return 'info';
}
