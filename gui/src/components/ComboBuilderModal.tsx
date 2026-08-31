import {useEffect, useMemo, useState} from 'react';
import {upsertCombo} from '../lib/orchestration';
import type {ComboModel, OrchModel, RouteTier} from '../lib/types';

interface ComboBuilderModalProps {
	open: boolean;
	models: OrchModel[];
	combos: ComboModel[];
	onClose: () => void;
	onNotice: (message: string) => void;
}

const TIERS: readonly {id: RouteTier; label: string; hint: string}[] = [
	{id: 'ECO', label: 'Eco', hint: 'Fast / cheap pool — 4+ coding, 2+ general'},
	{id: 'BALANCED', label: 'Balanced', hint: 'Mid-tier daily routing'},
	{id: 'BEST', label: 'Best', hint: 'Frontier / max quality'},
];

const DEFAULT_NAMES: Record<RouteTier, string> = {
	ECO: 'Eco ensemble',
	BALANCED: 'Balanced ensemble',
	BEST: 'Best ensemble',
};

function toggleId(list: string[], id: string): string[] {
	return list.includes(id) ? list.filter(item => item !== id) : [...list, id];
}

function nameOf(models: OrchModel[], id: string | null): string {
	if (!id) {
		return '—';
	}

	return models.find(item => item.id === id)?.name ?? id;
}

export function ComboBuilderModal({open, models, combos, onClose, onNotice}: ComboBuilderModalProps) {
	const [tier, setTier] = useState<RouteTier>('ECO');
	const [name, setName] = useState(DEFAULT_NAMES.ECO);
	const [comboId, setComboId] = useState<string | undefined>();
	const [reasoning, setReasoning] = useState('');
	const [coding, setCoding] = useState<string[]>([]);
	const [general, setGeneral] = useState<string[]>([]);
	const [error, setError] = useState('');
	const [saving, setSaving] = useState(false);

	const live = useMemo(() => models.filter(item => item.isActive), [models]);

	const existing = combos.find(item => item.tier === tier);
	useEffect(() => {
		if (!open) {
			return;
		}

		setComboId(existing?.id);
		setName(existing?.name || DEFAULT_NAMES[tier]);
		setReasoning(existing?.reasoningModelId ?? '');
		setCoding(existing?.codingModelIds ?? []);
		setGeneral(existing?.generalModelIds ?? []);
		setError('');
	}, [open, tier, existing?.id, existing?.name, existing?.reasoningModelId]);

	const failover = useMemo(() => {
		const codingNames = coding.map(id => nameOf(models, id));
		const generalNames = general.map(id => nameOf(models, id));
		return [
			`Reasoning → ${nameOf(models, reasoning || null)}`,
			`Coding RR → ${codingNames.length ? codingNames.join(' → ') : '—'}`,
			`General RR → ${generalNames.length ? generalNames.join(' → ') : '—'}`,
			'Then HTTP / 429 / timeout walks that order before the per-model fallback.',
		];
	}, [coding, general, models, reasoning]);

	async function save() {
		if (!name.trim()) {
			setError('Ensemble name is required');
			return;
		}

		if (coding.length === 0 && general.length === 0 && !reasoning) {
			setError('Assign at least one model to a role');
			return;
		}

		setSaving(true);
		setError('');
		try {
			await upsertCombo({
				id: comboId,
				name: name.trim(),
				tier,
				reasoning_model_id: reasoning,
				coding_model_ids: coding,
				general_model_ids: general,
				is_active: true,
			});
			onNotice(`Saved ${tier} ensemble`);
			onClose();
		} catch (item) {
			setError(item instanceof Error ? item.message : 'Save failed');
		} finally {
			setSaving(false);
		}
	}

	if (!open) {
		return null;
	}

	return (
		<div className="mem0-modal-overlay open" onClick={onClose} role="presentation">
			<div className="mem0-modal glass-card orch-combo" onClick={event => event.stopPropagation()} role="dialog">
				<div className="orch-drawer__head">
					<span className="card__title font-display">COMBO ENSEMBLE</span>
					<button type="button" className="ghost-btn" onClick={onClose}>
						Close
					</button>
				</div>
				<div className="orch-proto-tabs" role="tablist" aria-label="Tier">
					{TIERS.map(item => (
						<button
							key={item.id}
							type="button"
							className={`orch-proto-tabs__btn${tier === item.id ? ' is-active' : ''}`}
							onClick={() => setTier(item.id)}
						>
							{item.label}
						</button>
					))}
				</div>
				<p className="t-dim">{TIERS.find(item => item.id === tier)?.hint}</p>
				{live.length === 0 ? (
					<p className="wizard-error">Register and activate endpoints first, then assign them here.</p>
				) : null}
				<label className="cfg-field">
					<span>Ensemble name</span>
					<input className="glass-input" value={name} onChange={event => setName(event.target.value)} />
				</label>
				<label className="cfg-field">
					<span>Reasoning model (single)</span>
					<select className="glass-select" value={reasoning} onChange={event => setReasoning(event.target.value)}>
						<option value="">None</option>
						{live.map(item => (
							<option key={item.id} value={item.id}>
								{item.name} · {item.modelString}
							</option>
						))}
					</select>
				</label>
				<div className="orch-pool">
					<span className="cfg-section__title font-display">Coding pool ({coding.length})</span>
					<p className="t-dim">Round-robin. Four or more fast models recommended.</p>
					<div className="orch-check-grid">
						{live.map(item => (
							<label className="consensus-item" key={`code-${item.id}`}>
								<input
									type="checkbox"
									checked={coding.includes(item.id)}
									onChange={() => setCoding(current => toggleId(current, item.id))}
								/>
								<span>{item.name}</span>
								<span className="roster__id font-mono font-bold">{item.modelString}</span>
							</label>
						))}
					</div>
				</div>
				<div className="orch-pool">
					<span className="cfg-section__title font-display">Generalist pool ({general.length})</span>
					<p className="t-dim">Round-robin. Two or more lightweight models recommended.</p>
					<div className="orch-check-grid">
						{live.map(item => (
							<label className="consensus-item" key={`gen-${item.id}`}>
								<input
									type="checkbox"
									checked={general.includes(item.id)}
									onChange={() => setGeneral(current => toggleId(current, item.id))}
								/>
								<span>{item.name}</span>
								<span className="roster__id font-mono font-bold">{item.modelString}</span>
							</label>
						))}
					</div>
				</div>
				<div className="orch-failover">
					<span className="cfg-section__title font-display">Failover preview</span>
					<ol>
						{failover.map(line => (
							<li key={line}>{line}</li>
						))}
					</ol>
				</div>
				{error ? <p className="wizard-error">{error}</p> : null}
				<div className="cfg-actions">
					<button type="button" className="ghost-btn" onClick={onClose} disabled={saving}>
						Cancel
					</button>
					<button type="button" className="mem0-action mem0-action--inject" onClick={() => void save()} disabled={saving}>
						{saving ? 'Saving…' : `Save ${tier} combo`}
					</button>
				</div>
			</div>
		</div>
	);
}
