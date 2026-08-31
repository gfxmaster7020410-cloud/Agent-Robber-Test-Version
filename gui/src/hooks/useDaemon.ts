import {useCallback, useEffect, useRef, useState} from 'react';
import {
	createMessage,
	type AddModelPayload,
	type BatchClass,
	type BusyKind,
	type CatalogModel,
	type ChatMessage,
	type ConnectionStatus,
	type ComboModel,
	type ControlMode,
	type LoopTree,
	type MemoryHit,
	type MemoryNote,
	type MessageRole,
	type McpAddPayload,
	type McpCallResult,
	type McpSnapshot,
	type OllamaScan,
	type OrchModel,
	type PatchModelPayload,
	type ProviderPreset,
	type ProviderType,
	type RouteTier,
	type SuiteEvent,
	type SuiteEventType,
	type WorkerRow,
} from '../lib/types';
import {asContextWindow, DEFAULT_CONTEXT_WINDOW} from '../lib/contextWindow';
import {combosFromUnknown, orchModelsFromUnknown, presetsFromUnknown} from '../lib/orchestration';

export const DEFAULT_DAEMON_URL = 'ws://127.0.0.1:8787/ws';

const RECONNECT_STEPS_MS = [500, 1000, 2000, 4000, 5000] as const;
const SPARK_MAX = 24;

const MESSAGE_ROLES: readonly MessageRole[] = [
	'user',
	'assistant',
	'tool',
	'memory',
	'success',
	'system',
	'error',
	'reasoning',
];

const BUSY_KINDS: readonly BusyKind[] = ['thinking', 'memory', 'tool'];

export interface DaemonState {
	mode: string;
	model: string;
	busy: boolean;
	engine: string;
	live: boolean;
	modes: string[];
	models: string[];
	catalog: CatalogModel[];
	orchModels: OrchModel[];
	combos: ComboModel[];
	presets: ProviderPreset[];
	routeMode: RouteTier;
	consensus: string[];
	notes: MemoryNote[];
	ssotFiles: number;
	ssotChunks: number;
	ssotBackend: string;
	ssotSkipped: number;
	ssotWatching: boolean;
	contextWindow: number;
	contextPacked: number;
	contextHits: number;
	ttftMs: number;
	tps: number;
	workspace: string;
	clients: number;
	keys: Record<string, boolean>;
	theme: 'dark' | 'light';
	batch: BatchClass;
	workers: WorkerRow[];
	controlMode: ControlMode;
	loop: LoopTree | null;
	mcp: McpSnapshot | null;
}

export interface BusyPhase {
	kind: BusyKind;
	label: string;
}

export interface UseDaemonResult {
	status: ConnectionStatus;
	daemon: DaemonState | null;
	messages: ChatMessage[];
	busyPhase: BusyPhase | null;
	hits: MemoryHit[];
	tpsHistory: number[];
	suiteEvents: SuiteEvent[];
	sendPrompt: (text: string) => boolean;
	sendSlash: (command: string, args: string[], text?: string) => boolean;
	setMode: (mode: string) => boolean;
	setControlMode: (mode: ControlMode) => boolean;
	approveHeal: () => boolean;
	rejectHeal: () => boolean;
	setModel: (model: string) => boolean;
	requestKeys: () => boolean;
	requestMemory: (text?: string) => boolean;
	purge: (pid?: number) => boolean;
	addModel: (payload: AddModelPayload) => boolean;
	patchModel: (payload: PatchModelPayload) => boolean;
	pingModel: (label: string) => boolean;
	setBatch: (batch: BatchClass) => boolean;
	scanOllama: (host: string) => boolean;
	ollamaScan: OllamaScan | null;
	setKey: (provider: string, secret: string) => boolean;
	setConsensus: (models: string[]) => boolean;
	injectNote: (payload: {
		key: string;
		value: string;
		category: string;
		tags: string[];
	}) => boolean;
	purgeNotes: () => boolean;
	ping: () => boolean;
	setTheme: (theme: 'dark' | 'light') => boolean;
	setContextWindow: (window: string | number) => boolean;
	mcpList: () => boolean;
	mcpEnable: (id: string) => boolean;
	mcpDisable: (id: string) => boolean;
	mcpAdd: (payload: McpAddPayload) => boolean;
	mcpRemove: (id: string) => boolean;
	mcpCall: (server: string, tool: string, argumentsJson?: string) => boolean;
	lastMcpResult: McpCallResult | null;
	pushLocal: (role: MessageRole, content: string) => void;
	clear: (notice: string) => void;
}

function asRecord(value: unknown): Record<string, unknown> | null {
	return typeof value === 'object' && value !== null
		? (value as Record<string, unknown>)
		: null;
}

function asString(value: unknown): string | null {
	return typeof value === 'string' ? value : null;
}

function asNumber(value: unknown): number | null {
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function asBatch(value: unknown): BatchClass {
	return value === 'eco' || value === 'heavy' ? value : 'normal';
}

function asProviderType(value: unknown): ProviderType {
	return value === 'custom_base' || value === 'local_ollama' ? value : 'native';
}

function asStringRecord(value: unknown): Record<string, string> {
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

function asStringArray(value: unknown): string[] | null {
	return Array.isArray(value) && value.every(item => typeof item === 'string')
		? (value as string[])
		: null;
}

function keysFromFrame(frame: Record<string, unknown>): Record<string, boolean> {
	const raw = asRecord(frame['keys']);
	if (!raw) {
		return {};
	}

	const keys: Record<string, boolean> = {};
	for (const [name, ok] of Object.entries(raw)) {
		keys[name] = ok === true;
	}

	return keys;
}

function ssotFromFrame(frame: Record<string, unknown>) {
	const ssot = asRecord(frame['ssot']);
	return {
		ssotFiles: asNumber(ssot?.['files']) ?? 0,
		ssotChunks: asNumber(ssot?.['chunks']) ?? 0,
		ssotBackend: asString(ssot?.['backend']) ?? 'hash-json',
		ssotSkipped: asNumber(ssot?.['skipped']) ?? 0,
		ssotWatching: ssot?.['watching'] === true,
		contextWindow: asContextWindow(frame['context_window'] ?? ssot?.['window'] ?? DEFAULT_CONTEXT_WINDOW),
		contextPacked: asNumber(frame['context_packed'] ?? ssot?.['packed']) ?? 0,
		contextHits: asNumber(frame['context_hits'] ?? ssot?.['hits']) ?? 0,
	};
}

function asRouteMode(value: unknown, batch: BatchClass): RouteTier {
	if (value === 'ECO' || value === 'BALANCED' || value === 'BEST') {
		return value;
	}

	if (batch === 'eco') {
		return 'ECO';
	}

	if (batch === 'heavy') {
		return 'BEST';
	}

	return 'BALANCED';
}

function catalogFromFrame(frame: Record<string, unknown>): CatalogModel[] {
	const raw = frame['catalog'];
	if (!Array.isArray(raw)) {
		return [];
	}

	const rows: CatalogModel[] = [];
	for (const item of raw) {
		const row = asRecord(item);
		if (!row) {
			continue;
		}

		const label = asString(row['label']);
		if (!label) {
			continue;
		}

		rows.push({
			id: asString(row['id']) ?? label,
			label,
			name: asString(row['name']) ?? label,
			litellmModel: asString(row['litellm_model']) ?? '',
			provider: asString(row['provider']) ?? '',
			builtin: row['builtin'] === true,
			providerType: asProviderType(row['provider_type']),
			baseUrl: asString(row['base_url']) ?? '',
			batchClass: asBatch(row['batch_class']),
			contextLength: asNumber(row['context_length']) ?? 8192,
			fallbackLabel: asString(row['fallback_label']) ?? '',
			fallbackModelId: asString(row['fallback_model_id']) ?? '',
			customHeaders: asStringRecord(row['custom_headers']),
			active: row['active'] !== false,
			keyReady: row['key_ready'] === true,
			health: asString(row['health']) ?? 'unknown',
			costPerM: asNumber(row['cost_per_m']) ?? 0,
			latencyMs: asNumber(row['latency_ms']) ?? 0,
		});
	}

	return rows;
}

function workersFromFrame(frame: Record<string, unknown>): WorkerRow[] {
	const raw = frame['workers'];
	if (!Array.isArray(raw)) {
		return [];
	}

	const rows: WorkerRow[] = [];
	for (const item of raw) {
		const row = asRecord(item);
		if (!row) {
			continue;
		}

		const label = asString(row['label']);
		if (!label) {
			continue;
		}

		rows.push({
			label,
			litellmModel: asString(row['litellm_model']) ?? '',
			provider: asString(row['provider']) ?? '',
			live: row['live'] === true,
		});
	}

	return rows;
}

const SUITE_TYPES: readonly SuiteEventType[] = [
	'self_heal',
	'vision_alert',
	'vault_alert',
	'worker_tick',
	'loop_state',
	'checkpoint',
];

function asControlMode(value: unknown): ControlMode {
	return value === 'manual' ? 'manual' : 'autonomous';
}

function loopFromValue(value: unknown): LoopTree | null {
	const row = asRecord(value);
	if (!row) {
		return null;
	}

	const workersRaw = row['workers'];
	const workers: LoopTree['workers'] = [];
	if (Array.isArray(workersRaw)) {
		for (const item of workersRaw) {
			const worker = asRecord(item);
			const profileId = asString(worker?.['profile_id']);
			if (!profileId) {
				continue;
			}

			workers.push({
				profile_id: profileId,
				status: asString(worker?.['status']) ?? 'idle',
				target: asString(worker?.['target']),
			});
		}
	}

	return {
		control_mode: asControlMode(row['control_mode']),
		agent_mode: asString(row['agent_mode']) ?? 'act',
		phase: asString(row['phase']) ?? 'idle',
		busy: row['busy'] === true,
		workers,
		proxy_count: asNumber(row['proxy_count']) ?? 0,
		proxy_assigned: asNumber(row['proxy_assigned']) ?? 0,
		vision_ready: row['vision_ready'] === true,
		healer_bound: row['healer_bound'] === true,
		playwright_workers: asNumber(row['playwright_workers']) ?? 0,
		last_heal: asRecord(row['last_heal']),
		last_vision: asRecord(row['last_vision']),
		last_tick: asRecord(row['last_tick']),
		last_vault: asRecord(row['last_vault']),
		checkpoint: asRecord(row['checkpoint']),
	};
}

function suiteFromFrame(frame: Record<string, unknown>, type: SuiteEventType): SuiteEvent {
	return {
		type,
		content: asString(frame['content']) ?? '',
		metadata: asRecord(frame['metadata']) ?? {},
		timestamp: asNumber(frame['timestamp']) ?? Date.now() / 1000,
	};
}

function notesFromValue(value: unknown): MemoryNote[] {
	if (!Array.isArray(value)) {
		return [];
	}

	const notes: MemoryNote[] = [];
	for (const item of value) {
		const row = asRecord(item);
		if (!row) {
			continue;
		}

		const id = asString(row['id']);
		const key = asString(row['key']);
		const noteValue = asString(row['value']);
		if (!id || !key || noteValue === null) {
			continue;
		}

		const tagsRaw = row['tags'];
		notes.push({
			id,
			key,
			value: noteValue,
			category: asString(row['category']) ?? 'user',
			tags: Array.isArray(tagsRaw)
				? tagsRaw.filter((tag): tag is string => typeof tag === 'string')
				: [],
			created: asNumber(row['created']) ?? 0,
		});
	}

	return notes;
}

function mcpFromFrame(frame: Record<string, unknown>): McpSnapshot | null {
	const raw = asRecord(frame['mcp']);
	if (!raw) {
		return null;
	}

	const servers: McpSnapshot['servers'] = [];
	const tools: McpSnapshot['tools'] = [];
	if (Array.isArray(raw['servers'])) {
		for (const item of raw['servers']) {
			const row = asRecord(item);
			const id = asString(row?.['id']);
			if (!row || !id) {
				continue;
			}

			const argsRaw = row['args'];
			const envRaw = row['env_keys'];
			servers.push({
				id,
				label: asString(row['label']) ?? id,
				kind: asString(row['kind']) ?? 'stdio',
				description: asString(row['description']) ?? '',
				enabled: row['enabled'] === true,
				builtin: row['builtin'] === true,
				command: asString(row['command']) ?? '',
				args: Array.isArray(argsRaw)
					? argsRaw.filter((value): value is string => typeof value === 'string')
					: [],
				env_keys: Array.isArray(envRaw)
					? envRaw.filter((value): value is string => typeof value === 'string')
					: [],
				status: asString(row['status']) ?? 'stopped',
				error: asString(row['error']) ?? '',
				tool_count: asNumber(row['tool_count']) ?? 0,
			});
		}
	}

	if (Array.isArray(raw['tools'])) {
		for (const item of raw['tools']) {
			const row = asRecord(item);
			const server = asString(row?.['server']);
			const name = asString(row?.['name']);
			if (!row || !server || !name) {
				continue;
			}

			tools.push({
				server,
				name,
				qualified: asString(row['qualified']) ?? `mcp__${server}__${name}`,
				description: asString(row['description']) ?? '',
			});
		}
	}

	return {
		servers,
		tools,
		enabled: asNumber(raw['enabled']) ?? servers.filter(row => row.enabled).length,
		started: raw['started'] === true,
	};
}

function hitsFromValue(value: unknown): MemoryHit[] {
	if (!Array.isArray(value)) {
		return [];
	}

	const hits: MemoryHit[] = [];
	for (const item of value) {
		const row = asRecord(item);
		if (!row) {
			continue;
		}

		const path = asString(row['path']);
		if (!path) {
			continue;
		}

		hits.push({
			path,
			kind: asString(row['kind']) ?? 'chunk',
			start: asNumber(row['start']) ?? 0,
			end: asNumber(row['end']) ?? 0,
			score: asNumber(row['score']) ?? 0,
			preview: asString(row['preview']) ?? '',
		});
	}

	return hits;
}

export function useDaemon(url: string = DEFAULT_DAEMON_URL): UseDaemonResult {
	const [status, setStatus] = useState<ConnectionStatus>('connecting');
	const [daemon, setDaemon] = useState<DaemonState | null>(null);
	const [messages, setMessages] = useState<ChatMessage[]>([]);
	const [busyPhase, setBusyPhase] = useState<BusyPhase | null>(null);
	const [hits, setHits] = useState<MemoryHit[]>([]);
	const [tpsHistory, setTpsHistory] = useState<number[]>(() => Array.from({length: SPARK_MAX}, () => 8));
	const [ollamaScan, setOllamaScan] = useState<OllamaScan | null>(null);
	const [suiteEvents, setSuiteEvents] = useState<SuiteEvent[]>([]);
	const [lastMcpResult, setLastMcpResult] = useState<McpCallResult | null>(null);
	const socketRef = useRef<WebSocket | null>(null);
	const openRef = useRef(false);
	const timerRef = useRef<number | null>(null);
	const attemptRef = useRef(0);
	const disposedRef = useRef(false);

	const append = useCallback((message: ChatMessage) => {
		setMessages(previous => [...previous, message]);
	}, []);

	const pushLocal = useCallback(
		(role: MessageRole, content: string) => {
			append(createMessage(role, content));
		},
		[append],
	);

	const clear = useCallback((notice: string) => {
		setMessages([createMessage('system', notice)]);
	}, []);

	const handleFrame = useCallback(
		(raw: unknown) => {
			const frame = asRecord(raw);
			if (!frame) {
				return;
			}

			switch (frame['type']) {
				case 'state': {
					const busy = frame['busy'] === true;
					const tps = asNumber(frame['tps']) ?? 0;
					const batch = asBatch(frame['batch']);
					setDaemon({
						mode: asString(frame['mode']) ?? 'act',
						model: asString(frame['model']) ?? 'unknown',
						busy,
						engine: asString(frame['engine']) ?? 'fallback',
						live: frame['live'] === true,
						modes: asStringArray(frame['modes']) ?? [],
						models: asStringArray(frame['models']) ?? [],
						catalog: catalogFromFrame(frame),
						orchModels: orchModelsFromUnknown(frame['orch_models']),
						combos: combosFromUnknown(frame['combos']),
						presets: presetsFromUnknown(frame['presets']),
						routeMode: asRouteMode(frame['route_mode'], batch),
						consensus: asStringArray(frame['consensus']) ?? [],
						notes: notesFromValue(frame['notes']),
						...ssotFromFrame(frame),
						ttftMs: asNumber(frame['ttft_ms']) ?? 0,
						tps,
						workspace: asString(frame['workspace']) ?? '',
						clients: asNumber(frame['clients']) ?? 0,
						keys: keysFromFrame(frame),
						theme: asString(frame['theme']) === 'light' ? 'light' : 'dark',
						batch,
						workers: workersFromFrame(frame),
						controlMode: asControlMode(frame['control_mode']),
						loop: loopFromValue(frame['loop']),
						mcp: mcpFromFrame(frame),
					});
					if (tps > 0) {
						setTpsHistory(previous => [...previous.slice(1), Math.min(100, tps)]);
					}

					if (!busy) {
						setBusyPhase(null);
					}

					break;
				}

				case 'memory_hits': {
					setHits(hitsFromValue(frame['hits']));
					break;
				}

				case 'message': {
					const role = asString(frame['role']);
					const content = asString(frame['content']);
					if (content === null || role === null) {
						break;
					}

					const resolved: MessageRole = MESSAGE_ROLES.includes(role as MessageRole)
						? (role as MessageRole)
						: 'system';
					if (frame['append'] === true) {
						setMessages(previous => {
							for (let index = previous.length - 1; index >= 0; index -= 1) {
								if (previous[index]?.role === resolved) {
									const next = previous.slice();
									const current = previous[index];
									if (!current) {
										break;
									}

									next[index] = {
										...current,
										content: current.content + content,
									};
									return next;
								}
							}

							return [...previous, createMessage(resolved, content)];
						});
					} else {
						append(createMessage(resolved, content));
					}

					break;
				}

				case 'busy': {
					const kind = asString(frame['kind']);
					const label = asString(frame['label']);
					if (kind !== null && BUSY_KINDS.includes(kind as BusyKind)) {
						setBusyPhase({kind: kind as BusyKind, label: label ?? ''});
					}

					break;
				}

				case 'mcp': {
					const snap = mcpFromFrame({mcp: frame});
					if (snap) {
						setDaemon(previous => (previous ? {...previous, mcp: snap} : previous));
					}
					break;
				}

				case 'mcp_result': {
					setLastMcpResult({
						ok: frame['ok'] === true,
						server: asString(frame['server']) ?? '',
						tool: asString(frame['tool']) ?? '',
						content: asString(frame['content']) ?? '',
					});
					break;
				}

				case 'error': {
					const content = asString(frame['content']);
					if (content !== null) {
						append(createMessage('error', content));
					}

					break;
				}

				case 'ollama_tags': {
					setOllamaScan({
						ok: frame['ok'] === true,
						host: asString(frame['host']) ?? '',
						models: asStringArray(frame['models']) ?? [],
						error: asString(frame['error']) ?? '',
					});
					break;
				}

				case 'self_heal':
				case 'vision_alert':
				case 'vault_alert':
				case 'worker_tick':
				case 'loop_state':
				case 'checkpoint': {
					const kind = frame['type'] as SuiteEventType;
					if (!SUITE_TYPES.includes(kind)) {
						break;
					}

					const event = suiteFromFrame(frame, kind);
					setSuiteEvents(previous => [...previous.slice(-99), event]);
					if (event.content) {
						append(createMessage('system', event.content));
					}

					if (kind === 'loop_state') {
						const tree = loopFromValue(frame['metadata']) ?? loopFromValue(frame['loop']);
						if (tree) {
							setDaemon(previous =>
								previous
									? {
											...previous,
											controlMode: tree.control_mode,
											loop: tree,
										}
									: previous,
							);
						}
					}

					break;
				}

				case 'action_result':
				case 'log':
				case 'action_update': {
					const content = asString(frame['content']);
					if (content) {
						append(createMessage('system', content));
					}

					break;
				}

				case 'stream': {
					const content = asString(frame['content']);
					if (content) {
						setMessages(previous => {
							const last = previous[previous.length - 1];
							if (last?.role === 'assistant') {
								const next = previous.slice();
								next[next.length - 1] = {
									...last,
									content: last.content + content,
								};
								return next;
							}

							return [...previous, createMessage('assistant', content)];
						});
					}

					break;
				}

				case 'complete': {
					const content = asString(frame['content']);
					if (content) {
						append(createMessage('success', content));
					}

					break;
				}

				case 'slash_result': {
					if (frame['ephemeral'] === true) {
						const content = asString(frame['content']);
						if (content) {
							append(
								createMessage(frame['ok'] === false ? 'error' : 'system', content),
							);
						}
					}
					break;
				}

				default:
					break;
			}
		},
		[append],
	);

	useEffect(() => {
		disposedRef.current = false;

		const connect = () => {
			if (disposedRef.current) {
				return;
			}

			setStatus(attemptRef.current === 0 ? 'connecting' : 'offline');
			let socket: WebSocket;
			try {
				socket = new WebSocket(url);
			} catch {
				scheduleReconnect();
				return;
			}

			socketRef.current = socket;
			socket.addEventListener('open', () => {
				if (disposedRef.current) {
					return;
				}

				attemptRef.current = 0;
				openRef.current = true;
				setStatus('online');
			});
			socket.addEventListener('message', event => {
				if (disposedRef.current || typeof event.data !== 'string') {
					return;
				}

				try {
					handleFrame(JSON.parse(event.data));
				} catch {
					// ignore malformed frames
				}
			});
			socket.addEventListener('close', () => {
				if (disposedRef.current) {
					return;
				}

				socketRef.current = null;
				openRef.current = false;
				setStatus('offline');
				setBusyPhase(null);
				setDaemon(null);
				scheduleReconnect();
			});
		};

		const scheduleReconnect = () => {
			if (disposedRef.current || timerRef.current !== null) {
				return;
			}

			const wait =
				RECONNECT_STEPS_MS[
					Math.min(attemptRef.current, RECONNECT_STEPS_MS.length - 1)
				]!;
			attemptRef.current += 1;
			timerRef.current = window.setTimeout(() => {
				timerRef.current = null;
				connect();
			}, wait);
		};

		connect();
		return () => {
			disposedRef.current = true;
			openRef.current = false;
			if (timerRef.current !== null) {
				window.clearTimeout(timerRef.current);
				timerRef.current = null;
			}

			socketRef.current?.close();
			socketRef.current = null;
		};
	}, [handleFrame, url]);

	const send = useCallback((payload: Record<string, unknown>): boolean => {
		const socket = socketRef.current;
		if (socket === null || !openRef.current) {
			return false;
		}

		try {
			socket.send(JSON.stringify(payload));
			return true;
		} catch {
			return false;
		}
	}, []);

	return {
		status,
		daemon,
		messages,
		busyPhase,
		hits,
		tpsHistory,
		suiteEvents,
		ollamaScan,
		sendPrompt: useCallback((text: string) => send({type: 'prompt', text}), [send]),
		sendSlash: useCallback(
			(command: string, args: string[], text?: string) =>
				send({
					type: 'slash_command',
					command,
					args,
					session_id: 'current_session',
					text: text ?? `/${command}${args.length ? ` ${args.join(' ')}` : ''}`,
				}),
			[send],
		),
		setMode: useCallback((mode: string) => send({type: 'set_mode', mode}), [send]),
		setControlMode: useCallback(
			(mode: ControlMode) => send({type: 'set_control_mode', mode}),
			[send],
		),
		approveHeal: useCallback(() => send({type: 'heal_approve'}), [send]),
		rejectHeal: useCallback(() => send({type: 'heal_reject'}), [send]),
		setModel: useCallback(
			(model: string) => send({type: 'set_model', model}),
			[send],
		),
		requestKeys: useCallback(() => send({type: 'keys_status'}), [send]),
		requestMemory: useCallback(
			(text?: string) => send({type: 'memory_query', text: text ?? ''}),
			[send],
		),
		purge: useCallback(
			(pid?: number) =>
				pid === undefined ? send({type: 'purge'}) : send({type: 'purge', pid}),
			[send],
		),
		addModel: useCallback(
			payload =>
				send({
					type: 'add_model',
					label: payload.label,
					provider: payload.provider,
					litellm_model: payload.litellmModel,
					api_key: payload.apiKey || undefined,
					provider_type: payload.providerType,
					base_url: payload.baseUrl || '',
					batch_class: payload.batchClass,
					context_length: payload.contextLength ?? 8192,
					fallback_label: payload.fallbackLabel || '',
					fallback_model_id: payload.fallbackModelId || '',
					custom_headers: payload.customHeaders || undefined,
					ollama_models: payload.ollamaModels ?? [],
					active: payload.active !== false,
				}),
			[send],
		),
		patchModel: useCallback(
			payload =>
				send({
					type: 'patch_model',
					label: payload.label,
					batch_class: payload.batchClass,
					fallback_label: payload.fallbackLabel,
					fallback_model_id: payload.fallbackModelId,
					active: payload.active,
					context_length: payload.contextLength,
				}),
			[send],
		),
		pingModel: useCallback(
			(label: string) => send({type: 'ping_model', label}),
			[send],
		),
		setBatch: useCallback(
			(batch: BatchClass) => send({type: 'set_batch', batch}),
			[send],
		),
		scanOllama: useCallback(
			(host: string) => send({type: 'scan_ollama', host}),
			[send],
		),
		setKey: useCallback(
			(provider: string, secret: string) =>
				send({type: 'set_key', provider, secret}),
			[send],
		),
		setConsensus: useCallback(
			(models: string[]) => send({type: 'set_consensus', models}),
			[send],
		),
		injectNote: useCallback(
			payload =>
				send({
					type: 'memory_inject',
					key: payload.key,
					value: payload.value,
					category: payload.category,
					tags: payload.tags,
				}),
			[send],
		),
		purgeNotes: useCallback(() => send({type: 'memory_purge'}), [send]),
		ping: useCallback(() => send({type: 'ping'}), [send]),
		setTheme: useCallback(
			(theme: 'dark' | 'light') => send({type: 'set_theme', theme}),
			[send],
		),
		setContextWindow: useCallback(
			(window: string | number) => send({type: 'set_context_window', window}),
			[send],
		),
		mcpList: useCallback(() => send({type: 'mcp_list'}), [send]),
		mcpEnable: useCallback((id: string) => send({type: 'mcp_enable', id}), [send]),
		mcpDisable: useCallback((id: string) => send({type: 'mcp_disable', id}), [send]),
		mcpAdd: useCallback(
			(payload: McpAddPayload) =>
				send({
					type: 'mcp_add',
					id: payload.id,
					label: payload.label,
					command: payload.command,
					args: payload.args,
					env_keys: payload.envKeys,
					description: payload.description,
					enabled: payload.enabled === true,
				}),
			[send],
		),
		mcpRemove: useCallback((id: string) => send({type: 'mcp_remove', id}), [send]),
		mcpCall: useCallback(
			(server: string, tool: string, argumentsJson?: string) => {
				let parsed: Record<string, unknown> = {};
				const raw = argumentsJson?.trim() ?? '';
				if (raw) {
					try {
						const value = JSON.parse(raw) as unknown;
						parsed = typeof value === 'object' && value !== null && !Array.isArray(value)
							? (value as Record<string, unknown>)
							: {value};
					} catch {
						parsed = {text: raw, query: raw};
					}
				}
				return send({type: 'mcp_call', server, tool, arguments: parsed});
			},
			[send],
		),
		lastMcpResult,
		pushLocal,
		clear,
	};
}
