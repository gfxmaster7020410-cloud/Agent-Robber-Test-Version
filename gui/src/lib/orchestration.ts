import type {ComboModel, OrchModel, ProtocolType, ProviderPreset, RouteTier} from './types';

export const ORCH_API = 'http://127.0.0.1:8787';

function asRecord(value: unknown): Record<string, unknown> | null {
	return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : null;
}

function asString(value: unknown): string | null {
	return typeof value === 'string' ? value : null;
}

function asNumber(value: unknown): number | null {
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function asStringRecord(value: unknown): Record<string, string> {
	if (typeof value === 'string') {
		try {
			return asStringRecord(JSON.parse(value) as unknown);
		} catch {
			return {};
		}
	}

	const row = asRecord(value);
	if (!row) {
		return {};
	}

	const out: Record<string, string> = {};
	for (const [key, item] of Object.entries(row)) {
		if (typeof item === 'string') {
			out[key] = item;
		} else if (item !== null && item !== undefined) {
			out[key] = String(item);
		}
	}

	return out;
}

function asStringArray(value: unknown): string[] {
	if (!Array.isArray(value)) {
		return [];
	}

	return value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0);
}

export function asProtocol(value: unknown): ProtocolType {
	if (value === 'ANTHROPIC_COMPATIBLE' || value === 'NATIVE') {
		return value;
	}

	return 'OPENAI_COMPATIBLE';
}

export function asRouteTier(value: unknown): RouteTier {
	if (value === 'ECO' || value === 'BALANCED' || value === 'BEST') {
		return value;
	}

	return 'BALANCED';
}

export function orchModelFromRow(value: unknown): OrchModel | null {
	const row = asRecord(value);
	const id = asString(row?.['id']);
	const name = asString(row?.['name']);
	if (!row || !id || !name) {
		return null;
	}

	return {
		id,
		name,
		providerPreset: asString(row['provider_preset']) ?? 'CUSTOM',
		protocolType: asProtocol(row['protocol_type']),
		baseUrl: asString(row['base_url']) ?? '',
		modelString: asString(row['model_string']) ?? '',
		customHeaders: asStringRecord(row['custom_headers']),
		contextLength: asNumber(row['context_length']) ?? 128000,
		fallbackModelId: asString(row['fallback_model_id']),
		isActive: row['is_active'] !== false && row['is_active'] !== 0,
		latencyMs: asNumber(row['latency_ms']) ?? 0,
		costPer1m: asNumber(row['cost_per_1m']) ?? 0,
		temperature: asNumber(row['temperature']) ?? 0.2,
		createdAt: asString(row['created_at']) ?? '',
		keyReady: row['key_ready'] === true,
	};
}

export function comboFromRow(value: unknown): ComboModel | null {
	const row = asRecord(value);
	const id = asString(row?.['id']);
	const name = asString(row?.['name']);
	if (!row || !id || !name) {
		return null;
	}

	return {
		id,
		name,
		tier: asRouteTier(row['tier']),
		reasoningModelId: asString(row['reasoning_model_id']),
		codingModelIds: asStringArray(row['coding_model_ids']),
		generalModelIds: asStringArray(row['general_model_ids']),
		isActive: row['is_active'] !== false && row['is_active'] !== 0,
		createdAt: asString(row['created_at']) ?? '',
	};
}

export function presetFromRow(value: unknown): ProviderPreset | null {
	const row = asRecord(value);
	const id = asString(row?.['id']);
	const label = asString(row?.['label']);
	if (!row || !id || !label) {
		return null;
	}

	return {
		id,
		label,
		protocol: asProtocol(row['protocol']),
		baseUrl: asString(row['base_url']) ?? '',
		modelPlaceholder: asString(row['model_placeholder']) ?? '',
		headers: asStringRecord(row['headers']),
		envHint: asString(row['env_hint']) ?? '',
	};
}

export function orchModelsFromUnknown(value: unknown): OrchModel[] {
	if (!Array.isArray(value)) {
		return [];
	}

	const rows: OrchModel[] = [];
	for (const item of value) {
		const row = orchModelFromRow(item);
		if (row) {
			rows.push(row);
		}
	}

	return rows;
}

export function combosFromUnknown(value: unknown): ComboModel[] {
	if (!Array.isArray(value)) {
		return [];
	}

	const rows: ComboModel[] = [];
	for (const item of value) {
		const row = comboFromRow(item);
		if (row) {
			rows.push(row);
		}
	}

	return rows;
}

export function presetsFromUnknown(value: unknown): ProviderPreset[] {
	if (!Array.isArray(value)) {
		return [];
	}

	const rows: ProviderPreset[] = [];
	for (const item of value) {
		const row = presetFromRow(item);
		if (row) {
			rows.push(row);
		}
	}

	return rows;
}

function detailFromBody(body: unknown, fallback: string): string {
	const row = asRecord(body);
	const detail = row?.['detail'];
	if (typeof detail === 'string' && detail.trim()) {
		return detail;
	}

	if (Array.isArray(detail) && detail[0]) {
		const first = asRecord(detail[0]);
		const msg = asString(first?.['msg']);
		if (msg) {
			return msg;
		}
	}

	return fallback;
}

export async function orchFetch<T>(path: string, init?: RequestInit): Promise<T> {
	let response: Response;
	try {
		response = await fetch(`${ORCH_API}${path}`, {
			...init,
			headers: {
				Accept: 'application/json',
				...(init?.body ? {'Content-Type': 'application/json'} : {}),
				...(init?.headers ?? {}),
			},
		});
	} catch {
		throw new Error('Daemon unreachable on :8787');
	}

	const text = await response.text();
	let body: unknown = {};
	if (text.trim()) {
		try {
			body = JSON.parse(text) as unknown;
		} catch {
			throw new Error(response.ok ? 'Daemon returned invalid JSON' : `HTTP ${response.status}`);
		}
	}

	if (!response.ok) {
		throw new Error(detailFromBody(body, `HTTP ${response.status}`));
	}

	return body as T;
}

export interface OrchModelWrite {
	name: string;
	provider_preset: string;
	protocol_type: ProtocolType;
	base_url: string;
	model_string: string;
	api_key?: string;
	custom_headers: Record<string, string>;
	context_length: number;
	fallback_model_id: string;
	is_active: boolean;
	temperature: number;
}

export async function createOrchModel(payload: OrchModelWrite): Promise<OrchModel> {
	const body = await orchFetch<{model: unknown}>('/api/models', {
		method: 'POST',
		body: JSON.stringify(payload),
	});
	const row = orchModelFromRow(body.model);
	if (!row) {
		throw new Error('model create did not return a row');
	}

	return row;
}

export async function updateOrchModel(id: string, payload: Partial<OrchModelWrite>): Promise<OrchModel> {
	const body = await orchFetch<{model: unknown}>(`/api/models/${encodeURIComponent(id)}`, {
		method: 'PUT',
		body: JSON.stringify(payload),
	});
	const row = orchModelFromRow(body.model);
	if (!row) {
		throw new Error('model update did not return a row');
	}

	return row;
}

export async function deleteOrchModel(id: string): Promise<void> {
	await orchFetch(`/api/models/${encodeURIComponent(id)}`, {method: 'DELETE'});
}

export async function testOrchModel(id: string): Promise<{ok: boolean; latency_ms: number; error: string}> {
	return orchFetch(`/api/models/${encodeURIComponent(id)}/test`, {method: 'POST'});
}

export async function upsertCombo(payload: {
	id?: string;
	name: string;
	tier: RouteTier;
	reasoning_model_id: string;
	coding_model_ids: string[];
	general_model_ids: string[];
	is_active: boolean;
}): Promise<ComboModel> {
	const body = await orchFetch<{combo: unknown}>('/api/combos', {
		method: 'POST',
		body: JSON.stringify(payload),
	});
	const row = comboFromRow(body.combo);
	if (!row) {
		throw new Error('combo save did not return a row');
	}

	return row;
}

export function parseHeadersJson(raw: string): {ok: true; value: Record<string, string>} | {ok: false; error: string} {
	const text = raw.trim() || '{}';
	try {
		const parsed: unknown = JSON.parse(text);
		if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
			return {ok: false, error: 'Headers must be a JSON object'};
		}

		const value: Record<string, string> = {};
		for (const [key, item] of Object.entries(parsed as Record<string, unknown>)) {
			if (key.trim()) {
				value[key] = typeof item === 'string' ? item : String(item);
			}
		}

		return {ok: true, value};
	} catch {
		return {ok: false, error: 'Custom headers must be valid JSON'};
	}
}
