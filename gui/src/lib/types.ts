export type MessageRole =
	| 'user'
	| 'assistant'
	| 'tool'
	| 'memory'
	| 'success'
	| 'system'
	| 'error'
	| 'reasoning';

export type BusyKind = 'thinking' | 'memory' | 'tool';

export type ConnectionStatus = 'connecting' | 'online' | 'offline';

export type DashboardView = 'overview' | 'config' | 'mem0' | 'memo-store' | 'fb-shark' | 'console' | 'logs' | 'mcp';

export type BatchClass = 'eco' | 'normal' | 'heavy';

export type RouteTier = 'ECO' | 'BALANCED' | 'BEST';

export type ProtocolType = 'OPENAI_COMPATIBLE' | 'ANTHROPIC_COMPATIBLE' | 'NATIVE';

export type ProviderType = 'native' | 'custom_base' | 'local_ollama';

export interface CatalogModel {
	id: string;
	label: string;
	name: string;
	litellmModel: string;
	provider: string;
	builtin: boolean;
	providerType: ProviderType;
	baseUrl: string;
	batchClass: BatchClass;
	contextLength: number;
	fallbackLabel: string;
	fallbackModelId: string;
	customHeaders: Record<string, string>;
	active: boolean;
	keyReady: boolean;
	health?: string;
	costPerM: number;
	latencyMs: number;
}

export interface ProviderPreset {
	id: string;
	label: string;
	protocol: ProtocolType;
	baseUrl: string;
	modelPlaceholder: string;
	headers: Record<string, string>;
	envHint: string;
}

export interface OrchModel {
	id: string;
	name: string;
	providerPreset: string;
	protocolType: ProtocolType;
	baseUrl: string;
	modelString: string;
	customHeaders: Record<string, string>;
	contextLength: number;
	fallbackModelId: string | null;
	isActive: boolean;
	latencyMs: number;
	costPer1m: number;
	temperature: number;
	createdAt: string;
	keyReady: boolean;
}

export interface ComboModel {
	id: string;
	name: string;
	tier: RouteTier;
	reasoningModelId: string | null;
	codingModelIds: string[];
	generalModelIds: string[];
	isActive: boolean;
	createdAt: string;
}

export interface AddModelPayload {
	label: string;
	provider: string;
	litellmModel: string;
	apiKey?: string;
	providerType: ProviderType;
	baseUrl?: string;
	batchClass: BatchClass;
	contextLength?: number;
	fallbackLabel?: string;
	fallbackModelId?: string;
	customHeaders?: string;
	ollamaModels?: string[];
	active?: boolean;
}

export interface PatchModelPayload {
	label: string;
	batchClass?: BatchClass;
	fallbackLabel?: string;
	fallbackModelId?: string;
	active?: boolean;
	contextLength?: number;
}

export interface OllamaScan {
	ok: boolean;
	host: string;
	models: string[];
	error: string;
}

export interface ChatMessage {
	id: number;
	role: MessageRole;
	content: string;
	timestamp: number;
}

export interface MemoryHit {
	path: string;
	kind: string;
	start: number;
	end: number;
	score: number;
	preview: string;
}

export interface MemoryNote {
	id: string;
	key: string;
	value: string;
	category: string;
	tags: string[];
	created: number;
}

export interface WorkerRow {
	label: string;
	litellmModel: string;
	provider: string;
	live: boolean;
}

export type ControlMode = 'autonomous' | 'manual';

export type SuiteEventType =
	| 'self_heal'
	| 'vision_alert'
	| 'vault_alert'
	| 'worker_tick'
	| 'loop_state'
	| 'checkpoint';

export interface LoopWorker {
	profile_id: string;
	status: string;
	target?: string | null;
}

export interface LoopTree {
	control_mode: ControlMode;
	agent_mode: string;
	phase: string;
	busy: boolean;
	workers: LoopWorker[];
	proxy_count: number;
	proxy_assigned: number;
	vision_ready: boolean;
	healer_bound: boolean;
	playwright_workers: number;
	last_heal: Record<string, unknown> | null;
	last_vision: Record<string, unknown> | null;
	last_tick: Record<string, unknown> | null;
	last_vault: Record<string, unknown> | null;
	checkpoint: Record<string, unknown> | null;
}

export interface SuiteEvent {
	type: SuiteEventType;
	content: string;
	metadata: Record<string, unknown>;
	timestamp: number;
}

export interface EngineHealth {
	ok: boolean;
	detail?: string;
	count?: number;
	assigned?: number;
	optional?: boolean;
	bound?: boolean;
	control_mode?: string;
	multimodal?: boolean;
	started?: boolean;
}

export interface McpServer {
	id: string;
	label: string;
	kind: string;
	description: string;
	enabled: boolean;
	builtin: boolean;
	command: string;
	args: string[];
	env_keys: string[];
	status: string;
	error: string;
	tool_count: number;
}

export interface McpTool {
	server: string;
	name: string;
	qualified: string;
	description: string;
}

export interface McpSnapshot {
	servers: McpServer[];
	tools: McpTool[];
	enabled: number;
	started: boolean;
}

export interface McpAddPayload {
	id: string;
	label: string;
	command: string;
	args: string[];
	envKeys: string[];
	description: string;
	enabled?: boolean;
}

export interface McpCallResult {
	ok: boolean;
	server: string;
	tool: string;
	content: string;
}

export interface SystemHealth {
	ok: boolean;
	control_mode: ControlMode;
	phase: string;
	engines: Record<string, EngineHealth>;
	timestamp: number;
}

let messageId = 0;

export function nextMessageId(): number {
	messageId += 1;
	return messageId;
}

export function createMessage(role: MessageRole, content: string): ChatMessage {
	return {id: nextMessageId(), role, content, timestamp: Date.now()};
}

export function formatClock(timestamp: number): string {
	const date = new Date(timestamp);
	return [date.getHours(), date.getMinutes(), date.getSeconds()]
		.map(unit => String(unit).padStart(2, '0'))
		.join(':');
}
