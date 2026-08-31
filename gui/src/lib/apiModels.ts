import {useEffect, useState} from 'react';

export interface ApiModel {
	id: string;
	alias: string;
	provider: string;
	base_url: string;
	api_key: string;
	api_key_ready: boolean;
	model_id: string;
	protocol: string;
	context_length: number;
	priority: number;
	weight: number;
	is_active: boolean;
	health_status: string;
	last_tested_at: string | null;
	created_at: string;
}

export interface ApiModelIn {
	alias: string;
	provider: string;
	base_url: string;
	api_key: string;
	model_id: string;
	protocol: string;
	context_length: number;
	priority?: number;
	weight?: number;
}

export interface TestResult {
	ok: boolean;
	latency_ms: number;
	health_status?: string;
	response?: Record<string, unknown>;
	error?: string;
}

export interface LogEntry {
	id: string;
	timestamp: string;
	model_id: string;
	model_alias: string;
	status_code: number;
	latency_ms: number;
	tokens_used: number;
	error_message: string;
	endpoint: string;
}

const BASE = '/api/v1';

export function useApiModels() {
	const [models, setModels] = useState<ApiModel[]>([]);
	const [loading, setLoading] = useState(false);

	const refresh = async () => {
		setLoading(true);
		try {
			const resp = await fetch(`${BASE}/models`);
			if (resp.ok) {
				setModels(await resp.json());
			}
		} catch {
			// API server may not be running
		} finally {
			setLoading(false);
		}
	};

	useEffect(() => {
		refresh();
	}, []);

	const addModel = async (payload: ApiModelIn): Promise<ApiModel | null> => {
		try {
			const resp = await fetch(`${BASE}/models`, {
				method: 'POST',
				headers: {'Content-Type': 'application/json'},
				body: JSON.stringify(payload),
			});
			if (resp.ok) {
				const model = await resp.json();
				setModels(prev => [...prev, model]);
				return model;
			}
		} catch {
			// ignore
		}
		return null;
	};

	const patchModel = async (id: string, payload: Partial<ApiModelIn & {priority: number; weight: number; is_active: boolean; health_status: string}>): Promise<ApiModel | null> => {
		try {
			const resp = await fetch(`${BASE}/models/${id}`, {
				method: 'PATCH',
				headers: {'Content-Type': 'application/json'},
				body: JSON.stringify(payload),
			});
			if (resp.ok) {
				const updated = await resp.json();
				setModels(prev => prev.map(m => m.id === id ? updated : m));
				return updated;
			}
		} catch {
			// ignore
		}
		return null;
	};

	const deleteModel = async (id: string): Promise<boolean> => {
		try {
			const resp = await fetch(`${BASE}/models/${id}`, {method: 'DELETE'});
			if (resp.ok) {
				setModels(prev => prev.filter(m => m.id !== id));
				return true;
			}
		} catch {
			// ignore
		}
		return false;
	};

	const testModel = async (id: string): Promise<TestResult | null> => {
		try {
			const resp = await fetch(`${BASE}/models/${id}/test`, {method: 'POST'});
			if (resp.ok) {
				const result = await resp.json();
				// Refresh model list to get updated health_status
				refresh();
				return result;
			}
		} catch {
			// ignore
		}
		return null;
	};

	const chatCompletion = async (modelId: string, prompt: string): Promise<{data: Record<string, unknown>; servedBy: string | null} | null> => {
		try {
			const resp = await fetch(`${BASE}/chat/completions`, {
				method: 'POST',
				headers: {'Content-Type': 'application/json'},
				body: JSON.stringify({
					model: modelId,
					messages: [{role: 'user', content: prompt}],
					temperature: 0.7,
				}),
			});
			if (resp.ok) {
				const servedBy = resp.headers.get('X-Robber-Served-By');
				const data = await resp.json();
				return {data, servedBy};
			}
		} catch {
			// ignore
		}
		return null;
	};

	const fetchLogs = async (limit = 50): Promise<LogEntry[]> => {
		try {
			const resp = await fetch(`${BASE}/logs?limit=${limit}`);
			if (resp.ok) {
				return await resp.json();
			}
		} catch {
			// ignore
		}
		return [];
	};

	return {models, loading, refresh, addModel, patchModel, deleteModel, testModel, chatCompletion, fetchLogs};
}
