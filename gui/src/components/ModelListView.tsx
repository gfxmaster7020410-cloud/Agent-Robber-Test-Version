import {useState} from 'react';
import {protocolLabel} from '../lib/presets';
import {deleteOrchModel, testOrchModel, updateOrchModel} from '../lib/orchestration';
import type {OrchModel} from '../lib/types';

interface ModelListViewProps {
	models: OrchModel[];
	onConfigure: (model: OrchModel) => void;
	onNotice: (message: string) => void;
}

export function ModelListView({models, onConfigure, onNotice}: ModelListViewProps) {
	const [busyId, setBusyId] = useState<string | null>(null);

	async function ping(row: OrchModel) {
		setBusyId(row.id);
		try {
			const result = await testOrchModel(row.id);
			if (result.ok) {
				onNotice(`${row.name}: ${result.latency_ms} ms`);
			} else {
				onNotice(`${row.name}: ${result.error || 'ping failed'}`);
			}
		} catch (error) {
			onNotice(error instanceof Error ? error.message : 'Ping failed');
		} finally {
			setBusyId(null);
		}
	}

	async function toggle(row: OrchModel) {
		setBusyId(row.id);
		try {
			await updateOrchModel(row.id, {is_active: !row.isActive});
		} catch (error) {
			onNotice(error instanceof Error ? error.message : 'Toggle failed');
		} finally {
			setBusyId(null);
		}
	}

	async function remove(row: OrchModel) {
		if (!window.confirm(`Delete ${row.name}? This removes the SQLite endpoint, not .env keys.`)) {
			return;
		}

		setBusyId(row.id);
		try {
			await deleteOrchModel(row.id);
			onNotice(`Deleted ${row.name}`);
		} catch (error) {
			onNotice(error instanceof Error ? error.message : 'Delete failed');
		} finally {
			setBusyId(null);
		}
	}

	if (models.length === 0) {
		return (
			<div className="orch-empty">
				No custom endpoints yet. Add an OpenAI-compatible, Anthropic-compatible, or native model.
			</div>
		);
	}

	return (
		<div className="orch-table-wrap">
			<table className="orch-table">
				<thead>
					<tr>
						<th>Model</th>
						<th>Preset</th>
						<th>Protocol</th>
						<th>Active</th>
						<th>Latency</th>
						<th>Actions</th>
					</tr>
				</thead>
				<tbody>
					{models.map(row => (
						<tr key={row.id} className={row.isActive ? '' : 'is-off'}>
							<td>
								<div className="orch-table__name">{row.name}</div>
								<div className="orch-table__alias">
									{row.modelString || '—'}
									{row.keyReady ? '' : ' · key missing'}
								</div>
							</td>
							<td>
								<span className="roster__tag font-mono font-bold">{row.providerPreset.replaceAll('_', ' ')}</span>
							</td>
							<td>
								<span className={`orch-proto orch-proto--${row.protocolType.toLowerCase()}`}>
									{protocolLabel(row.protocolType)}
								</span>
							</td>
							<td>
								<button
									type="button"
									role="switch"
									aria-checked={row.isActive}
									className={`orch-switch${row.isActive ? ' is-on' : ''}`}
									disabled={busyId === row.id}
									onClick={() => {
										void toggle(row);
									}}
								>
									<span className="orch-switch__knob" />
								</button>
							</td>
							<td>
								<span className={`orch-latency font-mono font-bold${row.latencyMs > 0 ? ' is-hot' : ''}`}>
									{row.latencyMs > 0 ? `${row.latencyMs} ms` : '—'}
								</span>
							</td>
							<td>
								<div className="orch-table__ops">
									<button
										type="button"
										className="ghost-btn"
										disabled={busyId === row.id}
										onClick={() => {
											void ping(row);
										}}
									>
										{busyId === row.id ? '…' : 'Test'}
									</button>
									<button type="button" className="ghost-btn" onClick={() => onConfigure(row)}>
										Configure
									</button>
									<button
										type="button"
										className="ghost-btn ghost-btn--danger"
										disabled={busyId === row.id}
										onClick={() => {
											void remove(row);
										}}
									>
										Delete
									</button>
								</div>
							</td>
						</tr>
					))}
				</tbody>
			</table>
		</div>
	);
}
