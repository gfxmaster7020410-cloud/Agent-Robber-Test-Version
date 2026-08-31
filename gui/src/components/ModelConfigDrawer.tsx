import {useEffect, useMemo, useState} from 'react';
import {PROTOCOL_OPTIONS, PROVIDER_PRESETS} from '../lib/presets';
import {createOrchModel, parseHeadersJson, updateOrchModel} from '../lib/orchestration';
import type {OrchModel, ProtocolType, ProviderPreset} from '../lib/types';

const FALLBACK_PRESETS: ProviderPreset[] = PROVIDER_PRESETS.map(item => ({
	...item,
	envHint: '',
}));

interface ModelConfigDrawerProps {
	open: boolean;
	model: OrchModel | null;
	models: OrchModel[];
	presets: ProviderPreset[];
	onClose: () => void;
	onNotice: (message: string) => void;
}

interface Draft {
	name: string;
	providerPreset: string;
	protocolType: ProtocolType;
	baseUrl: string;
	modelString: string;
	apiKey: string;
	fallbackModelId: string;
	headersText: string;
	contextLength: number;
	temperature: number;
	isActive: boolean;
}

function emptyDraft(presets: ProviderPreset[]): Draft {
	const first = presets[0];
	return {
		name: '',
		providerPreset: first?.id ?? 'CUSTOM',
		protocolType: first?.protocol ?? 'OPENAI_COMPATIBLE',
		baseUrl: first?.baseUrl ?? '',
		modelString: first?.modelPlaceholder ?? '',
		apiKey: '',
		fallbackModelId: '',
		headersText: JSON.stringify(first?.headers ?? {}, null, 2),
		contextLength: 128000,
		temperature: 0.2,
		isActive: true,
	};
}

function draftFromModel(model: OrchModel): Draft {
	return {
		name: model.name,
		providerPreset: model.providerPreset,
		protocolType: model.protocolType,
		baseUrl: model.baseUrl,
		modelString: model.modelString,
		apiKey: '',
		fallbackModelId: model.fallbackModelId ?? '',
		headersText: JSON.stringify(model.customHeaders, null, 2),
		contextLength: model.contextLength,
		temperature: model.temperature,
		isActive: model.isActive,
	};
}

export function ModelConfigDrawer({
	open,
	model,
	models,
	presets,
	onClose,
	onNotice,
}: ModelConfigDrawerProps) {
	const catalog = presets.length > 0 ? presets : FALLBACK_PRESETS;
	const [draft, setDraft] = useState<Draft>(() => emptyDraft(catalog));
	const [error, setError] = useState('');
	const [saving, setSaving] = useState(false);
	const [advanced, setAdvanced] = useState(false);

	useEffect(() => {
		if (!open) {
			return;
		}

		setError('');
		setAdvanced(false);
		setDraft(model ? draftFromModel(model) : emptyDraft(presets.length > 0 ? presets : FALLBACK_PRESETS));
	}, [open, model]);

	const selected = useMemo(
		() => catalog.find(item => item.id === draft.providerPreset) ?? catalog[0],
		[catalog, draft.providerPreset],
	);

	const fallbacks = models.filter(item => item.id !== model?.id);
	const headerCheck = parseHeadersJson(draft.headersText);

	function applyPreset(id: string) {
		const preset = catalog.find(item => item.id === id);
		if (!preset) {
			return;
		}

		setDraft(current => ({
			...current,
			providerPreset: preset.id,
			protocolType: preset.protocol,
			baseUrl: preset.baseUrl,
			modelString: current.modelString.trim() ? current.modelString : preset.modelPlaceholder,
			headersText: JSON.stringify(preset.headers, null, 2),
		}));
	}

	async function save() {
		const name = draft.name.trim();
		if (!name) {
			setError('Display name is required');
			return;
		}

		if (!headerCheck.ok) {
			setError(headerCheck.error);
			setAdvanced(true);
			return;
		}

		setSaving(true);
		setError('');
		try {
			const payload = {
				name,
				provider_preset: draft.providerPreset,
				protocol_type: draft.protocolType,
				base_url: draft.baseUrl.trim(),
				model_string: draft.modelString.trim(),
				custom_headers: headerCheck.value,
				context_length: draft.contextLength,
				fallback_model_id: draft.fallbackModelId.trim(),
				is_active: draft.isActive,
				temperature: draft.temperature,
				...(draft.apiKey.trim() ? {api_key: draft.apiKey.trim()} : {}),
			};
			if (model) {
				await updateOrchModel(model.id, payload);
				onNotice(`Updated ${name}`);
			} else {
				await createOrchModel(payload);
				onNotice(`Registered ${name}`);
			}

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
		<div className="orch-drawer-overlay" onClick={onClose} role="presentation">
			<aside
				className="orch-drawer glass-card"
				role="dialog"
				aria-label={model ? 'Configure endpoint' : 'Add endpoint'}
				onClick={event => event.stopPropagation()}
			>
				<div className="orch-drawer__head">
					<span className="card__title font-display">{model ? 'CONFIGURE ENDPOINT' : 'ADD ENDPOINT'}</span>
					<button type="button" className="ghost-btn" onClick={onClose}>
						Close
					</button>
				</div>
				<div className="orch-drawer__body">
					<div className="orch-proto-tabs" role="tablist" aria-label="Protocol">
						{PROTOCOL_OPTIONS.map(option => (
							<button
								key={option.id}
								type="button"
								className={`orch-proto-tabs__btn${draft.protocolType === option.id ? ' is-active' : ''}`}
								onClick={() => setDraft(current => ({...current, protocolType: option.id}))}
							>
								{option.label}
							</button>
						))}
					</div>
					<label className="cfg-field">
						<span>Provider preset</span>
						<select
							className="glass-select"
							value={draft.providerPreset}
							onChange={event => applyPreset(event.target.value)}
						>
							{catalog.map(item => (
								<option key={item.id} value={item.id}>
									{item.label}
								</option>
							))}
						</select>
						{selected?.envHint ? (
							<span className="orch-hint">Falls back to {selected.envHint} in .env when the stored key is empty.</span>
						) : null}
					</label>
					<label className="cfg-field">
						<span>Display name</span>
						<input
							className="glass-input"
							value={draft.name}
							onChange={event => setDraft(current => ({...current, name: event.target.value}))}
							placeholder="DeepSeek R1 via OmniRoute"
						/>
					</label>
					<label className="cfg-field">
						<span>Base URL</span>
						<input
							className="glass-input"
							value={draft.baseUrl}
							onChange={event => setDraft(current => ({...current, baseUrl: event.target.value}))}
							placeholder="https://api.omniroute.ai/v1"
						/>
					</label>
					<label className="cfg-field">
						<span>Model identifier</span>
						<input
							className="glass-input"
							value={draft.modelString}
							onChange={event => setDraft(current => ({...current, modelString: event.target.value}))}
							placeholder={selected?.modelPlaceholder || 'deepseek/deepseek-r1'}
						/>
					</label>
					<label className="cfg-field">
						<span>API key / bearer</span>
						<input
							className="glass-input"
							type="password"
							autoComplete="off"
							value={draft.apiKey}
							onChange={event => setDraft(current => ({...current, apiKey: event.target.value}))}
							placeholder={model?.keyReady ? 'ready — paste to replace' : 'optional if preset uses .env'}
						/>
					</label>
					<label className="cfg-field">
						<span>Fallback model</span>
						<select
							className="glass-select"
							value={draft.fallbackModelId}
							onChange={event => setDraft(current => ({...current, fallbackModelId: event.target.value}))}
						>
							<option value="">None — canned reply</option>
							{fallbacks.map(item => (
								<option key={item.id} value={item.id}>
									{item.name}
								</option>
							))}
						</select>
					</label>
					<button
						type="button"
						className="orch-advanced"
						onClick={() => setAdvanced(current => !current)}
					>
						{advanced ? 'Hide advanced' : 'Advanced — headers, context, temperature'}
					</button>
					{advanced ? (
						<div className="orch-advanced__body">
							<label className="cfg-field">
								<span>Custom HTTP headers (JSON)</span>
								<textarea
									className="glass-input orch-json"
									spellCheck={false}
									value={draft.headersText}
									onChange={event => setDraft(current => ({...current, headersText: event.target.value}))}
								/>
								<span className={headerCheck.ok ? 'orch-hint' : 'wizard-error'}>
									{headerCheck.ok ? 'Object of header name → value. Injected on every request.' : headerCheck.error}
								</span>
							</label>
							<div className="cfg-grid">
								<label className="cfg-field">
									<span>Context window</span>
									<input
										className="glass-input"
										type="number"
										min={1024}
										max={2000000}
										value={draft.contextLength}
										onChange={event =>
											setDraft(current => ({
												...current,
												contextLength: Number.parseInt(event.target.value, 10) || 128000,
											}))
										}
									/>
								</label>
								<label className="cfg-field">
									<span>Temperature</span>
									<input
										className="glass-input"
										type="number"
										step={0.1}
										min={0}
										max={2}
										value={draft.temperature}
										onChange={event =>
											setDraft(current => ({
												...current,
												temperature: Number.parseFloat(event.target.value) || 0,
											}))
										}
									/>
								</label>
							</div>
							<label className="consensus-item">
								<input
									type="checkbox"
									checked={draft.isActive}
									onChange={event => setDraft(current => ({...current, isActive: event.target.checked}))}
								/>
								<span>Active in routing</span>
							</label>
						</div>
					) : null}
					{error ? <p className="wizard-error">{error}</p> : null}
				</div>
				<div className="orch-drawer__foot">
					<button type="button" className="ghost-btn" onClick={onClose} disabled={saving}>
						Cancel
					</button>
					<button type="button" className="mem0-action mem0-action--inject" onClick={() => void save()} disabled={saving}>
						{saving ? 'Saving…' : model ? 'Save endpoint' : 'Register endpoint'}
					</button>
				</div>
			</aside>
		</div>
	);
}
