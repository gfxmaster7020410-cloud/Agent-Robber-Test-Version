import {useCallback, useEffect, useRef, useState} from 'react';
import {
	createMessage,
	type BusyKind,
	type ChatMessage,
	type ConnectionStatus,
	type MessageRole,
} from '../types.js';

export const DEFAULT_DAEMON_URL = 'ws://127.0.0.1:8787/ws';

/**
 * Structural view of the WebSocket global. Declared locally so the TUI does
 * not depend on `@types/node` shipping the global, and so a Node build without
 * it degrades to "offline" instead of failing to compile.
 */
type SocketLike = {
	send(data: string): void;
	close(): void;
	addEventListener(
		type: 'open' | 'close' | 'error',
		listener: () => void,
	): void;
	addEventListener(
		type: 'message',
		listener: (event: {data: unknown}) => void,
	): void;
};

type SocketConstructor = new (url: string) => SocketLike;

const SocketImpl = (globalThis as {WebSocket?: SocketConstructor}).WebSocket;

const RECONNECT_STEPS_MS = [500, 1000, 2000, 4000, 5000] as const;

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
	ssotFiles: number;
	ssotChunks: number;
	ssotBackend: string;
	ssotSkipped: number;
	ssotWatching: boolean;
	contextWindow: number;
	contextPacked: number;
	contextHits: number;
	theme: 'dark' | 'light';
	controlMode: 'autonomous' | 'manual';
	loopPhase: string;
	mcpEnabled: number;
	mcpTools: number;
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
	sendPrompt: (text: string) => boolean;
	sendSlash: (command: string, args: string[], text?: string) => boolean;
	setMode: (mode: string) => boolean;
	approveHeal: () => boolean;
	rejectHeal: () => boolean;
	setModel: (model: string) => boolean;
	requestKeys: () => boolean;
	requestMemory: (text?: string) => boolean;
	purge: (pid?: number) => boolean;
	mcpList: () => boolean;
	mcpEnable: (id: string) => boolean;
	mcpDisable: (id: string) => boolean;
	mcpRemove: (id: string) => boolean;
	mcpCall: (server: string, tool: string, argumentsJson?: string) => boolean;
	setTheme: (theme: 'dark' | 'light') => boolean;
	setContextWindow: (window: string | number) => boolean;
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

function parseShorthand(raw: string): number {
	const text = raw.trim().toUpperCase().replace(/,/g, '').replace(/_/g, '').replace(/\s/g, '');
	const match = text.match(/^(\d+(?:\.\d+)?(?:E[+-]?\d+)?)([KMBTG])?$/);
	let parsed = match ? Number(match[1]) : Number(text);
	if (match?.[2] === 'K') {
		parsed *= 1_000;
	} else if (match?.[2] === 'M') {
		parsed *= 1_000_000;
	} else if (match?.[2] === 'B' || match?.[2] === 'G') {
		parsed *= 1_000_000_000;
	} else if (match?.[2] === 'T') {
		parsed *= 1_000_000_000_000;
	}
	return Number.isFinite(parsed) && parsed > 0 ? Math.trunc(parsed) : 200_000;
}

function ssotFromFrame(frame: Record<string, unknown>): {
	ssotFiles: number;
	ssotChunks: number;
	ssotBackend: string;
	ssotSkipped: number;
	ssotWatching: boolean;
	contextWindow: number;
	contextPacked: number;
	contextHits: number;
} {
	const ssot = asRecord(frame['ssot']);
	const windowRaw = frame['context_window'] ?? ssot?.['window'] ?? 200_000;
	const windowValue =
		typeof windowRaw === 'number' && Number.isFinite(windowRaw) && windowRaw > 0
			? Math.trunc(windowRaw)
			: typeof windowRaw === 'string'
				? parseShorthand(windowRaw)
				: 200_000;
	if (!ssot) {
		return {
			ssotFiles: 0,
			ssotChunks: 0,
			ssotBackend: 'hash-json',
			ssotSkipped: 0,
			ssotWatching: false,
			contextWindow: windowValue,
			contextPacked: asNumber(frame['context_packed']) ?? 0,
			contextHits: asNumber(frame['context_hits']) ?? 0,
		};
	}

	return {
		ssotFiles: asNumber(ssot['files']) ?? 0,
		ssotChunks: asNumber(ssot['chunks']) ?? 0,
		ssotBackend: asString(ssot['backend']) ?? 'hash-json',
		ssotSkipped: asNumber(ssot['skipped']) ?? 0,
		ssotWatching: ssot['watching'] === true,
		contextWindow: windowValue,
		contextPacked: asNumber(frame['context_packed'] ?? ssot['packed']) ?? 0,
		contextHits: asNumber(frame['context_hits'] ?? ssot['hits']) ?? 0,
	};
}

function asStringArray(value: unknown): string[] | null {
	return Array.isArray(value) && value.every(item => typeof item === 'string')
		? (value as string[])
		: null;
}

export function useDaemon(url: string = DEFAULT_DAEMON_URL): UseDaemonResult {
	const [status, setStatus] = useState<ConnectionStatus>(
		SocketImpl ? 'connecting' : 'offline',
	);
	const [daemon, setDaemon] = useState<DaemonState | null>(null);
	const [messages, setMessages] = useState<ChatMessage[]>([]);
	const [busyPhase, setBusyPhase] = useState<BusyPhase | null>(null);

	const socketRef = useRef<SocketLike | null>(null);
	const openRef = useRef(false);
	const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
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

	const clear = useCallback(
		(notice: string) => {
			setMessages([createMessage('system', notice)]);
		},
		[],
	);

	const handleFrame = useCallback(
		(raw: unknown) => {
			const frame = asRecord(raw);
			if (!frame) {
				return;
			}

			switch (frame['type']) {
				case 'state': {
					const mode = asString(frame['mode']);
					const model = asString(frame['model']);
					const engine = asString(frame['engine']);
					const busy = frame['busy'] === true;
					setDaemon({
						mode: mode ?? 'act',
						model: model ?? 'unknown',
						busy,
						engine: engine ?? 'fallback',
						live: frame['live'] === true,
						modes: asStringArray(frame['modes']) ?? [],
						models: asStringArray(frame['models']) ?? [],
						...ssotFromFrame(frame),
						theme: asString(frame['theme']) === 'light' ? 'light' : 'dark',
						controlMode:
							asString(frame['control_mode']) === 'manual' ? 'manual' : 'autonomous',
						loopPhase: asString(asRecord(frame['loop'])?.['phase']) ?? 'idle',
						mcpEnabled: asNumber(asRecord(frame['mcp'])?.['enabled']) ?? 0,
						mcpTools: Array.isArray(asRecord(frame['mcp'])?.['tools'])
							? (asRecord(frame['mcp'])!['tools'] as unknown[]).length
							: 0,
					});
					if (!busy) {
						setBusyPhase(null);
					}

					break;
				}

				case 'message': {
					const role = asString(frame['role']);
					const content = asString(frame['content']);
					if (content === null || role === null) {
						break;
					}

					const resolved: MessageRole = MESSAGE_ROLES.includes(
						role as MessageRole,
					)
						? (role as MessageRole)
						: 'system';
					const isAppend = frame['append'] === true;
					if (isAppend) {
						setMessages(previous => {
							for (let index = previous.length - 1; index >= 0; index -= 1) {
								if (previous[index]!.role === resolved) {
									const next = previous.slice();
									const current = previous[index]!;
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

				case 'error': {
					const content = asString(frame['content']);
					if (content !== null) {
						append(createMessage('error', content));
					}

					break;
				}

				case 'self_heal':
				case 'vision_alert':
				case 'vault_alert':
				case 'worker_tick':
				case 'loop_state':
				case 'checkpoint': {
					const content = asString(frame['content']);
					if (content) {
						append(createMessage('system', content));
					}

					if (frame['type'] === 'loop_state') {
						const meta = asRecord(frame['metadata']);
						const control =
							asString(meta?.['control_mode']) === 'manual' ? 'manual' : 'autonomous';
						const phase = asString(meta?.['phase']) ?? 'idle';
						setDaemon(previous =>
							previous
								? {...previous, controlMode: control, loopPhase: phase}
								: previous,
						);
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
		if (!SocketImpl) {
			return undefined;
		}

		disposedRef.current = false;

		const connect = () => {
			if (disposedRef.current) {
				return;
			}

			setStatus(attemptRef.current === 0 ? 'connecting' : 'offline');

			let socket: SocketLike;
			try {
				socket = new SocketImpl(url);
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
				if (disposedRef.current) {
					return;
				}

				const {data} = event;
				if (typeof data !== 'string') {
					return;
				}

				try {
					handleFrame(JSON.parse(data));
				} catch {
					// Ignore malformed frames rather than tearing down the session.
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

			socket.addEventListener('error', () => {
				// `close` always follows, which owns the reconnect.
			});
		};

		const scheduleReconnect = () => {
			if (disposedRef.current || timerRef.current !== null) {
				return;
			}

			const index = Math.min(
				attemptRef.current,
				RECONNECT_STEPS_MS.length - 1,
			);
			const wait = RECONNECT_STEPS_MS[index]!;
			attemptRef.current += 1;

			const timer = setTimeout(() => {
				timerRef.current = null;
				connect();
			}, wait);
			timer.unref?.();
			timerRef.current = timer;
		};

		connect();

		return () => {
			disposedRef.current = true;
			openRef.current = false;
			if (timerRef.current !== null) {
				clearTimeout(timerRef.current);
				timerRef.current = null;
			}

			socketRef.current?.close();
			socketRef.current = null;
		};
	}, [handleFrame, url]);

	const send = useCallback((payload: Record<string, unknown>): boolean => {
		const socket = socketRef.current as WebSocket | null;
		if (socket === null || socket.readyState !== WebSocket.OPEN) {
			return false;
		}

		try {
			socket.send(JSON.stringify(payload));
			return true;
		} catch {
			return false;
		}
	}, []);

	const sendPrompt = useCallback(
		(text: string) => send({action: 'USER_PROMPT', prompt: text}),
		[send],
	);

	const sendSlash = useCallback(
		(command: string, args: string[], text?: string) =>
			send({
				type: 'slash_command',
				command,
				args,
				session_id: 'current_session',
				text: text ?? `/${command}${args.length ? ` ${args.join(' ')}` : ''}`,
			}),
		[send],
	);

	const setMode = useCallback(
		(mode: string) => send({type: 'set_mode', mode}),
		[send],
	);

	const approveHeal = useCallback(() => send({type: 'heal_approve'}), [send]);
	const rejectHeal = useCallback(() => send({type: 'heal_reject'}), [send]);

	const setModel = useCallback(
		(model: string) => send({type: 'set_model', model}),
		[send],
	);

	const requestKeys = useCallback(
		() => send({type: 'keys_status'}),
		[send],
	);

	const requestMemory = useCallback(
		(text?: string) => send({type: 'memory_query', text: text ?? ''}),
		[send],
	);

	const purge = useCallback(
		(pid?: number) =>
			pid === undefined
				? send({type: 'purge'})
				: send({type: 'purge', pid}),
		[send],
	);

	const mcpList = useCallback(() => send({type: 'mcp_list'}), [send]);
	const mcpEnable = useCallback((id: string) => send({type: 'mcp_enable', id}), [send]);
	const mcpDisable = useCallback((id: string) => send({type: 'mcp_disable', id}), [send]);
	const mcpRemove = useCallback((id: string) => send({type: 'mcp_remove', id}), [send]);
	const mcpCall = useCallback(
		(server: string, tool: string, argumentsJson?: string) => {
			let parsed: Record<string, unknown> = {};
			const raw = argumentsJson?.trim() ?? '';
			if (raw) {
				try {
					const value = JSON.parse(raw) as unknown;
					parsed =
						typeof value === 'object' && value !== null && !Array.isArray(value)
							? (value as Record<string, unknown>)
							: {value};
				} catch {
					parsed = {text: raw, query: raw};
				}
			}
			return send({type: 'mcp_call', server, tool, arguments: parsed});
		},
		[send],
	);

	const setTheme = useCallback(
		(theme: 'dark' | 'light') => send({type: 'set_theme', theme}),
		[send],
	);

	const setContextWindow = useCallback(
		(window: string | number) => send({type: 'set_context_window', window}),
		[send],
	);

	return {
		status,
		daemon,
		messages,
		busyPhase,
		sendPrompt,
		sendSlash,
		setMode,
		approveHeal,
		rejectHeal,
		setModel,
		requestKeys,
		requestMemory,
		purge,
		mcpList,
		mcpEnable,
		mcpDisable,
		mcpRemove,
		mcpCall,
		setTheme,
		setContextWindow,
		pushLocal,
		clear,
	};
}
