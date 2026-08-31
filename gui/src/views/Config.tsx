import {useState} from 'react';
import type {DaemonState} from '../hooks/useDaemon';
import type {AddModelPayload, OllamaScan, OrchModel, PatchModelPayload} from '../lib/types';
import {AddModelWizard} from '../components/AddModelWizard';
import {ComboBuilderModal} from '../components/ComboBuilderModal';
import {ContextWindowPanel} from '../components/ContextWindowPanel';
import {ModelConfigDrawer} from '../components/ModelConfigDrawer';
import {ModelListView} from '../components/ModelListView';
import {TestModal} from '../components/TestModal';
import {useApiModels, type LogEntry} from '../lib/apiModels';
import {KEY_PROVIDERS, batchLabel} from '../lib/providers';

interface ConfigViewProps {
	daemon: DaemonState | null;
	ollamaScan: OllamaScan | null;
	onSetKey: (provider: string, secret: string) => boolean;
	onAddModel: (payload: AddModelPayload) => boolean;
	onPatchModel: (payload: PatchModelPayload) => boolean;
	onScanOllama: (host: string) => boolean;
	onSetModel: (model: string) => boolean;
	onConsensus: (models: string[]) => boolean;
	onPingModel: (label: string) => boolean;
	onPingAll: () => boolean;
	onPatchParams?: (label: string, params: {temperature?: number; maxTokens?: number}) => boolean;
	onSetWindow?: (window: string | number) => boolean;
}

export function ConfigView({
	daemon,
	ollamaScan,
	onSetKey,
	onAddModel,
	onPatchModel,
	onScanOllama,
	onSetModel,
	onConsensus,
	onPingModel,
	onPingAll,
	onSetWindow,
}: ConfigViewProps) {
	const [drafts, setDrafts] = useState<Record<string, string>>({});
	const [visibleKeys, setVisibleKeys] = useState<Record<string, boolean>>({});
	const [wizard, setWizard] = useState(false);
	const [drawerOpen, setDrawerOpen] = useState(false);
	const [editing, setEditing] = useState<OrchModel | null>(null);
	const [comboOpen, setComboOpen] = useState(false);
	const [testOpen, setTestOpen] = useState(false);
	const [notice, setNotice] = useState('');
	const [logs, setLogs] = useState<LogEntry[]>([]);
	const [logsOpen, setLogsOpen] = useState(false);
	const apiModels = useApiModels();
	const catalog = daemon?.catalog ?? [];
	const orch = daemon?.orchModels ?? [];
	const combos = daemon?.combos ?? [];
	const consensus = new Set(daemon?.consensus ?? []);
	const keys = daemon?.keys ?? {};
	const routeMode = daemon?.routeMode ?? 'BALANCED';

	function toggleConsensus(name: string) {
		const next = new Set(consensus);
		if (next.has(name)) {
			next.delete(name);
		} else {
			next.add(name);
		}

		onConsensus([...next]);
	}

	return (
		<div className="view view--scroll">
			<div className="glass-card full-card">
				<div className="card__head">
					<span className="card__title font-display">ORCHESTRATION CONTROL</span>
					<div className="orch-head-ops">
						<button type="button" className="ghost-btn" onClick={() => setTestOpen(true)}>
							Quick Test
						</button>
						<button type="button" className="ghost-btn" onClick={() => setComboOpen(true)}>
							Combo ensembles
						</button>
						<button
							type="button"
							className="mem0-action mem0-action--inject"
							onClick={() => {
								setEditing(null);
								setDrawerOpen(true);
							}}
						>
							<span>Add endpoint</span>
						</button>
					</div>
				</div>
				<div className="card__body card__body--scroll">
					{notice ? <p className="orch-notice">{notice}</p> : null}
					<div className="cfg-section">
						<span className="cfg-section__title">SSOT unified context</span>
						<ContextWindowPanel
							windowTokens={daemon?.contextWindow}
							packedTokens={daemon?.contextPacked}
							hitCount={daemon?.contextHits}
							online={Boolean(daemon)}
							onApply={raw => onSetWindow?.(raw) === true}
						/>
					</div>
					<div className="cfg-section">
						<span className="cfg-section__title">API Keys (.env BYOK)</span>
						<p className="t-dim">
							Presence only. Values are written to local .env and never shown again. Endpoint-specific
							keys in the table below are encrypted in SQLite separately.
						</p>
						{KEY_PROVIDERS.map(name => (
							<div className="key-row" key={name}>
								<label>{name}</label>
								<input
									type={visibleKeys[name] ? 'text' : 'password'}
									className="glass-input"
									autoComplete="off"
									placeholder={keys[name] ? 'ready — paste to replace' : 'missing'}
									value={drafts[name] ?? ''}
									onChange={event =>
										setDrafts(current => ({...current, [name]: event.target.value}))
									}
								/>
								<button
									type="button"
									className="ghost-btn"
									onClick={() =>
										setVisibleKeys(current => ({...current, [name]: !current[name]}))
									}
								>
									{visibleKeys[name] ? 'Hide' : 'Show'}
								</button>
								<span
									className={`key-status ${keys[name] ? 'key-status--ready' : 'key-status--missing'}`}
								>
									{keys[name] ? 'ready' : 'missing'}
								</span>
								<button
									type="button"
									className="ghost-btn"
									onClick={() => {
										const secret = drafts[name]?.trim() ?? '';
										if (!secret) {
											return;
										}

										onSetKey(name, secret);
										setDrafts(current => ({...current, [name]: ''}));
									}}
								>
									Save
								</button>
							</div>
						))}
					</div>

					<div className="cfg-section">
						<div className="cfg-section__head">
							<span className="cfg-section__title">Endpoints</span>
							<span className={`roster__tag roster__tag--${daemon?.batch ?? 'normal'}`}>
								Route {routeMode}
							</span>
						</div>
						<p className="t-dim">
							High-density registry for OpenAI-compatible, Anthropic-compatible, and native adapters.
							Header Eco / Balanced / Best selects the active combo ensemble. Keys never echo back.
						</p>
						<ModelListView
							models={orch}
							onConfigure={row => {
								setEditing(row);
								setDrawerOpen(true);
							}}
							onNotice={setNotice}
						/>
					</div>

					<div className="cfg-section">
						<span className="cfg-section__title">Health Check</span>
						<button type="button" className="ghost-btn" onClick={onPingAll}>
							Ping catalog engines
						</button>
					</div>

					<div className="cfg-section">
						<div className="cfg-section__head">
							<span className="cfg-section__title">REST API Models (MVP-v2 Proxy)</span>
							<div className="flex gap-2">
								<button
									type="button"
									className="ghost-btn"
									onClick={async () => {
										const entries = await apiModels.fetchLogs(50);
										setLogs(entries);
										setLogsOpen(!logsOpen);
									}}
								>
									{logsOpen ? 'Hide Logs' : 'View Logs'}
								</button>
							</div>
						</div>
						<p className="t-dim">
							SQLite-backed models with encrypted keys. Priority controls fallback order (1=highest).
							Failed requests auto-route to next model by priority.
						</p>
						{apiModels.models.length === 0 ? (
							<p className="t-dim">No API models configured. Use the Add Model wizard above or call POST /api/v1/models.</p>
						) : (
							<div className="space-y-2 mt-2">
								{apiModels.models.map(m => (
									<div key={m.id} className="bg-white/5 p-3 rounded-lg border border-white/5">
										<div className="flex items-center justify-between">
											<div className="flex items-center gap-3">
												<span className="text-sm text-slate-200 font-medium">{m.alias}</span>
												<span className="text-[10px] text-slate-500 font-mono">{m.provider} · {m.model_id}</span>
												<span className={`text-[9px] px-1.5 py-0.5 rounded border font-bold ${
													m.health_status === 'HEALTHY' ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' :
													m.health_status === 'OFFLINE' ? 'bg-rose-500/10 text-rose-400 border-rose-500/20' :
													m.health_status === 'DEGRADED' ? 'bg-amber-500/10 text-amber-400 border-amber-500/20' :
													'bg-slate-500/10 text-slate-400 border-slate-500/20'
												}`}>
													{m.health_status}
												</span>
												<span className="text-[10px] text-slate-500 font-mono">P{m.priority} · W{m.weight}</span>
												<span className="text-[10px] text-slate-600 font-mono">{m.api_key_ready ? 'key ready' : 'no key'}</span>
											</div>
											<div className="flex gap-2">
												<button
													type="button"
													className="ghost-btn text-cyan-400"
													onClick={async () => {
														setNotice(`Testing ${m.alias}...`);
														const result = await apiModels.testModel(m.id);
														if (result?.ok) {
															setNotice(`${m.alias}: HEALTHY (${result.latency_ms}ms)`);
														} else {
															setNotice(`${m.alias}: OFFLINE — ${result?.error ?? 'unknown'}`);
														}
													}}
												>
													Ping
												</button>
												<button
													type="button"
													className="ghost-btn text-slate-400"
													onClick={async () => {
														const newP = m.priority === 1 ? 2 : 1;
														await apiModels.patchModel(m.id, {priority: newP});
														setNotice(`${m.alias}: priority set to ${newP}`);
													}}
												>
													P{m.priority === 1 ? '2' : '1'}
												</button>
												<button
													type="button"
													className="ghost-btn text-rose-400"
													onClick={async () => {
														await apiModels.deleteModel(m.id);
														setNotice(`${m.alias} deleted.`);
													}}
												>
													Delete
												</button>
											</div>
										</div>
									</div>
								))}
							</div>
						)}
						{logsOpen && logs.length > 0 && (
							<div className="mt-4 bg-[#0B0F17]/60 border border-white/5 rounded-lg overflow-hidden">
								<div className="px-3 py-2 border-b border-white/5 text-[10px] text-slate-500 font-mono uppercase tracking-wider">Recent Execution Logs</div>
								<div className="max-h-64 overflow-y-auto">
									<table className="w-full text-[11px] font-mono tabular-nums text-slate-400">
										<thead className="text-slate-600 sticky top-0 bg-[#0B0F17]/90">
											<tr>
												<th className="text-left py-1.5 px-2">Time</th>
												<th className="text-left py-1.5 px-2">Model</th>
												<th className="text-left py-1.5 px-2">Status</th>
												<th className="text-left py-1.5 px-2">Latency</th>
												<th className="text-left py-1.5 px-2">Error</th>
											</tr>
										</thead>
										<tbody>
											{logs.map(log => (
												<tr key={log.id} className="border-t border-white/5">
													<td className="py-1.5 px-2 text-slate-500">{new Date(log.timestamp).toLocaleTimeString()}</td>
													<td className="py-1.5 px-2">{log.model_alias}</td>
													<td className={`py-1.5 px-2 ${log.status_code === 200 ? 'text-emerald-400' : 'text-rose-400'}`}>{log.status_code || 'ERR'}</td>
													<td className="py-1.5 px-2">{log.latency_ms}ms</td>
													<td className="py-1.5 px-2 text-rose-400/70 max-w-[120px] truncate">{log.error_message}</td>
												</tr>
											))}
										</tbody>
									</table>
								</div>
							</div>
						)}
					</div>

					<div className="cfg-section">
						<div className="cfg-section__head">
							<span className="cfg-section__title">Hybrid catalog (legacy)</span>
							<button type="button" className="ghost-btn" onClick={() => setWizard(true)}>
								Import Ollama / catalog
							</button>
						</div>
						<p className="t-dim">
							Built-in LiteLLM catalog used when no combo is active for the current tier. Consensus
							still fans out these engines in parallel.
						</p>
						<div className="consensus-list">
							{catalog
								.filter(row => row.label !== 'Hybrid Multi-LLM')
								.map(row => {
									const checked = consensus.has(row.label);
									const locked = !checked && consensus.size >= 10;
									return (
										<label className="consensus-item" key={row.label}>
											<input
												type="checkbox"
												checked={checked}
												disabled={locked}
												onChange={() => {
													if (locked) {
														return;
													}

													toggleConsensus(row.label);
												}}
											/>
											<span>{row.label}</span>
											<span className={`roster__tag roster__tag--batch roster__tag--${row.batchClass}`}>
												{batchLabel(row.batchClass)}
											</span>
											<button
												type="button"
												className="ghost-btn"
												onClick={event => {
													event.preventDefault();
													onPingModel(row.label);
												}}
											>
												Ping
											</button>
											<button
												type="button"
												className="ghost-btn"
												onClick={event => {
													event.preventDefault();
													onSetModel(row.label);
												}}
											>
												{daemon?.model === row.label ? 'Selected' : 'Use'}
											</button>
											<button
												type="button"
												className="ghost-btn"
												onClick={event => {
													event.preventDefault();
													onPatchModel({label: row.label, active: !row.active});
												}}
											>
												{row.active ? 'On' : 'Off'}
											</button>
										</label>
									);
								})}
						</div>
						<p className="cfg-val">
							{consensus.size} / 10 consensus · {(daemon?.workers ?? []).filter(row => row.live).length} live
						</p>
					</div>
				</div>
			</div>
			<AddModelWizard
				open={wizard}
				catalog={catalog}
				ollamaScan={ollamaScan}
				onClose={() => setWizard(false)}
				onScan={onScanOllama}
				onSubmit={onAddModel}
			/>
			<ModelConfigDrawer
				open={drawerOpen}
				model={editing}
				models={orch}
				presets={daemon?.presets ?? []}
				onClose={() => {
					setDrawerOpen(false);
					setEditing(null);
				}}
				onNotice={setNotice}
			/>
			<ComboBuilderModal
				open={comboOpen}
				models={orch}
				combos={combos}
				onClose={() => setComboOpen(false)}
				onNotice={setNotice}
			/>
			<TestModal
				isOpen={testOpen}
				onClose={() => setTestOpen(false)}
				models={apiModels.models.map(m => ({id: m.id, alias: m.alias, model_id: m.model_id, provider: m.provider}))}
			/>
		</div>
	);
}
