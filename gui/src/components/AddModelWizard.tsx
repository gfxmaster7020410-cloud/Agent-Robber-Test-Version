import {useEffect, useMemo, useState} from 'react';
import type {AddModelPayload, BatchClass, CatalogModel, OllamaScan, ProviderType} from '../lib/types';
import {AI_PROVIDERS, ARCH_OPTIONS, BATCH_OPTIONS} from '../lib/providers';

interface AddModelWizardProps {
	open: boolean;
	catalog: CatalogModel[];
	ollamaScan: OllamaScan | null;
	onClose: () => void;
	onScan: (host: string) => boolean;
	onSubmit: (payload: AddModelPayload) => boolean;
}

const EMPTY = {
	step: 1 as 1 | 2 | 3,
	provider: 'openai',
	arch: 'native' as ProviderType,
	label: '',
	modelId: '',
	apiKey: '',
	baseUrl: 'https://',
	headersJson: '{\n  \n}',
	ollamaHost: 'http://localhost:11434',
	selectedTags: [] as string[],
	batchClass: 'normal' as BatchClass,
	contextLength: 8192,
	fallbackLabel: '',
};

export function AddModelWizard({
	open,
	catalog,
	ollamaScan,
	onClose,
	onScan,
	onSubmit,
}: AddModelWizardProps) {
	const [form, setForm] = useState(EMPTY);
	const [scanning, setScanning] = useState(false);
	const [localError, setLocalError] = useState('');
	const provider = AI_PROVIDERS.find(item => item.id === form.provider) ?? AI_PROVIDERS[0];

	useEffect(() => {
		if (!open) {
			setForm(EMPTY);
			setScanning(false);
			setLocalError('');
		}
	}, [open]);

	useEffect(() => {
		if (ollamaScan) {
			setScanning(false);
		}
	}, [ollamaScan]);

	const fallbacks = useMemo(
		() => catalog.filter(row => row.label !== 'Hybrid Multi-LLM'),
		[catalog],
	);

	const tags = ollamaScan?.models ?? [];

	function next() {
		setLocalError('');
		if (form.step === 1) {
			setForm(current => ({...current, step: 2}));
			return;
		}

		if (form.step === 2) {
			if (form.arch === 'native' && !form.modelId.trim()) {
				setLocalError('Model ID is required for native integration.');
				return;
			}

			if (form.arch === 'custom_base') {
				if (!form.label.trim()) {
					setLocalError('Display name is required.');
					return;
				}

				if (!form.baseUrl.trim() || form.baseUrl.trim() === 'https://') {
					setLocalError('Base URL is required.');
					return;
				}

				if (form.headersJson.trim()) {
					try {
						const parsed: unknown = JSON.parse(form.headersJson);
						if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
							throw new Error('object');
						}
					} catch {
						setLocalError('Custom headers must be a JSON object.');
						return;
					}
				}
			}

			if (form.arch === 'local_ollama' && form.selectedTags.length === 0 && !form.modelId.trim()) {
				setLocalError('Scan and select at least one local model, or type a tag.');
				return;
			}

			setForm(current => ({...current, step: 3}));
		}
	}

	function submit() {
		const litellmModel =
			form.arch === 'local_ollama'
				? form.selectedTags[0] ?? form.modelId.trim()
				: form.modelId.trim();
		const label =
			form.label.trim() ||
			(form.arch === 'local_ollama' ? (form.selectedTags[0] ?? form.modelId.trim()) : form.modelId.trim());
		const ok = onSubmit({
			label,
			provider: form.arch === 'local_ollama' ? 'ollama' : form.provider,
			litellmModel,
			apiKey: form.apiKey.trim() || undefined,
			providerType: form.arch,
			baseUrl: form.arch === 'local_ollama' ? form.ollamaHost.trim() : form.baseUrl.trim(),
			batchClass: form.batchClass,
			contextLength: form.contextLength,
			fallbackLabel: form.fallbackLabel || undefined,
			customHeaders: form.arch === 'custom_base' ? form.headersJson : undefined,
			ollamaModels: form.arch === 'local_ollama' ? form.selectedTags : undefined,
		});
		if (ok) {
			onClose();
		} else {
			setLocalError('Daemon offline — start python server.py, then retry.');
		}
	}

	if (!open) {
		return null;
	}

	return (
		<div className="mem0-modal-overlay open wizard-overlay">
			<div className="mem0-modal glass-card wizard-modal">
				<div className="card__head">
					<span className="card__title font-display">ADD CUSTOM MODEL</span>
					<button type="button" className="mem0-modal-close" onClick={onClose}>
						×
					</button>
				</div>
				<div className="wizard-steps" aria-label="Wizard progress">
					{[1, 2, 3].map(index => (
						<button
							key={index}
							type="button"
							className={`wizard-steps__dot${form.step === index ? ' is-active' : ''}${form.step > index ? ' is-done' : ''}`}
							onClick={() => {
								if (index < form.step) {
									setForm(current => ({...current, step: index as 1 | 2 | 3}));
								}
							}}
						>
							<span>{index}</span>
							{index === 1 ? 'Provider' : index === 2 ? 'Connection' : 'Batch'}
						</button>
					))}
				</div>
				<div className="card__body wizard-body">
					<div key={form.step} className="wizard-pane">
						{form.step === 1 ? (
							<>
								<p className="t-dim">Choose the global AI provider this route belongs to.</p>
								<div className="provider-grid">
									{AI_PROVIDERS.map(item => (
										<button
											key={item.id}
											type="button"
											className={`provider-card${form.provider === item.id ? ' is-active' : ''}`}
											onClick={() =>
												setForm(current => ({
													...current,
													provider: item.id,
													modelId: current.modelId || item.placeholder,
												}))
											}
										>
											<span className="provider-card__mark" style={{background: item.accent}}>
												{item.mark}
											</span>
											<span className="provider-card__name font-display">{item.name}</span>
											<span className="provider-card__hint">{item.hint}</span>
										</button>
									))}
								</div>
							</>
						) : null}
						{form.step === 2 ? (
							<>
								<p className="t-dim">
									{provider.name} — pick how The Robber Agent reaches this model.
								</p>
								<div className="arch-grid">
									{ARCH_OPTIONS.map(option => (
										<label
											key={option.id}
											className={`arch-card${form.arch === option.id ? ' is-active' : ''}`}
										>
											<input
												type="radio"
												name="arch"
												checked={form.arch === option.id}
												onChange={() => setForm(current => ({...current, arch: option.id}))}
											/>
											<span className="arch-card__title font-display">{option.title}</span>
											<span className="arch-card__blurb font-display">{option.blurb}</span>
										</label>
									))}
								</div>
								{form.arch === 'native' ? (
									<div className="cfg-grid">
										<div className="cfg-field">
											<label>API key</label>
											<input
												type="password"
												className="glass-input"
												autoComplete="off"
												placeholder="stored in local .env — never shown again"
												value={form.apiKey}
												onChange={event =>
													setForm(current => ({...current, apiKey: event.target.value}))
												}
											/>
										</div>
										<div className="cfg-field">
											<label>Model ID</label>
											<input
												className="glass-input"
												placeholder={provider.placeholder}
												value={form.modelId}
												onChange={event =>
													setForm(current => ({...current, modelId: event.target.value}))
												}
											/>
										</div>
										<div className="cfg-field">
											<label>Display name</label>
											<input
												className="glass-input"
												placeholder={`${provider.name} ${form.modelId || provider.placeholder}`}
												value={form.label}
												onChange={event =>
													setForm(current => ({...current, label: event.target.value}))
												}
											/>
										</div>
									</div>
								) : null}
								{form.arch === 'custom_base' ? (
									<div className="cfg-grid">
										<div className="cfg-field">
											<label>Display name</label>
											<input
												className="glass-input"
												placeholder="e.g. RunPod Llama 70B"
												value={form.label}
												onChange={event =>
													setForm(current => ({...current, label: event.target.value}))
												}
											/>
										</div>
										<div className="cfg-field">
											<label>Base URL</label>
											<input
												className="glass-input"
												placeholder="https://domain.com/v1"
												value={form.baseUrl}
												onChange={event =>
													setForm(current => ({...current, baseUrl: event.target.value}))
												}
											/>
										</div>
										<div className="cfg-field">
											<label>Bearer token / API key</label>
											<input
												type="password"
												className="glass-input"
												autoComplete="off"
												value={form.apiKey}
												onChange={event =>
													setForm(current => ({...current, apiKey: event.target.value}))
												}
											/>
										</div>
										<div className="cfg-field">
											<label>Model ID</label>
											<input
												className="glass-input"
												placeholder="served model name"
												value={form.modelId}
												onChange={event =>
													setForm(current => ({...current, modelId: event.target.value}))
												}
											/>
										</div>
										<div className="cfg-field cfg-field--wide">
											<label>Custom HTTP headers (JSON)</label>
											<textarea
												className="glass-textarea"
												value={form.headersJson}
												onChange={event =>
													setForm(current => ({...current, headersJson: event.target.value}))
												}
											/>
										</div>
									</div>
								) : null}
								{form.arch === 'local_ollama' ? (
									<div className="cfg-grid">
										<div className="cfg-field cfg-field--wide">
											<label>Ollama host URL</label>
											<div className="wizard-inline">
												<input
													className="glass-input"
													value={form.ollamaHost}
													onChange={event =>
														setForm(current => ({...current, ollamaHost: event.target.value}))
													}
												/>
												<button
													type="button"
													className="ghost-btn"
													onClick={() => {
														setScanning(true);
														setLocalError('');
														if (!onScan(form.ollamaHost.trim() || 'http://localhost:11434')) {
															setScanning(false);
															setLocalError('Daemon offline — cannot scan Ollama.');
														}
													}}
												>
													{scanning ? 'Scanning…' : 'Auto-scan models'}
												</button>
											</div>
										</div>
										<div className="cfg-field cfg-field--wide">
											<label>Downloaded local models</label>
											{tags.length === 0 ? (
												<p className="t-dim">
													{ollamaScan && !ollamaScan.ok
														? ollamaScan.error
														: 'Run auto-scan to list tags from this host (localhost only).'}
												</p>
											) : (
												<div className="tag-multi">
													{tags.map(tag => {
														const checked = form.selectedTags.includes(tag);
														return (
															<label key={tag} className={`tag-chip${checked ? ' is-active' : ''}`}>
																<input
																	type="checkbox"
																	checked={checked}
																	onChange={() =>
																		setForm(current => ({
																			...current,
																			selectedTags: checked
																				? current.selectedTags.filter(item => item !== tag)
																				: [...current.selectedTags, tag],
																		}))
																	}
																/>
																{tag}
															</label>
														);
													})}
												</div>
											)}
										</div>
										<div className="cfg-field">
											<label>Or type a tag</label>
											<input
												className="glass-input"
												placeholder="llama3.2"
												value={form.modelId}
												onChange={event =>
													setForm(current => ({...current, modelId: event.target.value}))
												}
											/>
										</div>
										<div className="cfg-field">
											<label>Display prefix (optional)</label>
											<input
												className="glass-input"
												placeholder="e.g. Lab"
												value={form.label}
												onChange={event =>
													setForm(current => ({...current, label: event.target.value}))
												}
											/>
										</div>
									</div>
								) : null}
							</>
						) : null}
						{form.step === 3 ? (
							<>
								<p className="t-dim">
									Every catalog entry needs a batch class. Hybrid routing follows the global
									header switcher.
								</p>
								<div className="arch-grid">
									{BATCH_OPTIONS.map(option => (
										<label
											key={option.id}
											className={`arch-card${form.batchClass === option.id ? ' is-active' : ''}`}
										>
											<input
												type="radio"
												name="batch"
												checked={form.batchClass === option.id}
												onChange={() => setForm(current => ({...current, batchClass: option.id}))}
											/>
											<span className="arch-card__title font-display">{option.title}</span>
											<span className="arch-card__blurb font-display">{option.blurb}</span>
										</label>
									))}
								</div>
								<div className="cfg-grid">
									<div className="cfg-field">
										<label>Context length (tokens)</label>
										<input
											className="glass-input"
											type="number"
											min={1024}
											max={2000000}
											value={form.contextLength}
											onChange={event =>
												setForm(current => ({
													...current,
													contextLength: Number.parseInt(event.target.value, 10) || 8192,
												}))
											}
										/>
									</div>
									<div className="cfg-field">
										<label>Fallback model</label>
										<select
											className="glass-select"
											value={form.fallbackLabel}
											onChange={event =>
												setForm(current => ({...current, fallbackLabel: event.target.value}))
											}
										>
											<option value="">None — canned reply</option>
											{fallbacks.map(row => (
												<option key={row.label} value={row.label}>
													{row.label}
												</option>
											))}
										</select>
									</div>
								</div>
							</>
						) : null}
					</div>
					{localError ? <p className="wizard-error">{localError}</p> : null}
					<div className="cfg-actions">
						{form.step > 1 ? (
							<button
								type="button"
								className="ghost-btn"
								onClick={() =>
									setForm(current => ({
										...current,
										step: current.step === 3 ? 2 : 1,
									}))
								}
							>
								Back
							</button>
						) : (
							<button type="button" className="ghost-btn" onClick={onClose}>
								Cancel
							</button>
						)}
						{form.step < 3 ? (
							<button type="button" className="mem0-action mem0-action--inject" onClick={next}>
								<span>Continue</span>
							</button>
						) : (
							<button type="button" className="mem0-action mem0-action--inject" onClick={submit}>
								<span>Add to catalog</span>
							</button>
						)}
					</div>
				</div>
			</div>
		</div>
	);
}
