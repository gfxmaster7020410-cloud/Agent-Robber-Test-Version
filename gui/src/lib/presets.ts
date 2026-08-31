import type {ProtocolType} from './types';

export interface PresetOption {
	id: string;
	label: string;
	protocol: ProtocolType;
	baseUrl: string;
	modelPlaceholder: string;
	headers: Record<string, string>;
}

export const PROTOCOL_OPTIONS: readonly {id: ProtocolType; label: string}[] = [
	{id: 'OPENAI_COMPATIBLE', label: 'OpenAI-compatible'},
	{id: 'ANTHROPIC_COMPATIBLE', label: 'Anthropic-compatible'},
	{id: 'NATIVE', label: 'Native'},
];

export const PROVIDER_PRESETS: readonly PresetOption[] = [
	{id: 'OPENAI', label: 'OpenAI', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'https://api.openai.com/v1', modelPlaceholder: 'gpt-4o-mini', headers: {}},
	{id: 'ANTHROPIC', label: 'Anthropic', protocol: 'ANTHROPIC_COMPATIBLE', baseUrl: 'https://api.anthropic.com', modelPlaceholder: 'claude-sonnet-4-20250514', headers: {'anthropic-version': '2023-06-01'}},
	{id: 'GROQ', label: 'Groq', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'https://api.groq.com/openai/v1', modelPlaceholder: 'llama-3.3-70b-versatile', headers: {}},
	{id: 'GEMINI', label: 'Gemini', protocol: 'NATIVE', baseUrl: '', modelPlaceholder: 'gemini-3.6-flash', headers: {}},
	{id: 'GOOGLE_AI_STUDIO', label: 'Google AI Studio', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai', modelPlaceholder: 'gemini-3.6-flash', headers: {}},
	{id: 'OPENROUTER', label: 'OpenRouter', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'https://openrouter.ai/api/v1', modelPlaceholder: 'deepseek/deepseek-r1', headers: {}},
	{id: 'OMNIROUTE', label: 'OmniRoute', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'https://api.omniroute.ai/v1', modelPlaceholder: 'deepseek/deepseek-r1', headers: {}},
	{id: 'NEW_API', label: 'New API', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'http://127.0.0.1:3000/v1', modelPlaceholder: 'gpt-4o-mini', headers: {}},
	{id: 'LITELLM', label: 'LiteLLM proxy', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'http://127.0.0.1:4000', modelPlaceholder: 'gpt-4o-mini', headers: {}},
	{id: 'OPENCODE_ZEN', label: 'OpenCode Zen', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'https://opencode.ai/zen/v1', modelPlaceholder: 'opencode/zen', headers: {}},
	{id: 'OPENCODE_GO', label: 'OpenCode Go', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'https://opencode.ai/go/v1', modelPlaceholder: 'opencode/go', headers: {}},
	{id: 'DEEPSEEK', label: 'DeepSeek', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'https://api.deepseek.com/v1', modelPlaceholder: 'deepseek-chat', headers: {}},
	{id: 'MISTRAL', label: 'Mistral', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'https://api.mistral.ai/v1', modelPlaceholder: 'mistral-small-latest', headers: {}},
	{id: 'ALIBABA', label: 'Alibaba Qwen', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', modelPlaceholder: 'qwen-plus', headers: {}},
	{id: 'MINIMAX', label: 'MiniMax', protocol: 'ANTHROPIC_COMPATIBLE', baseUrl: 'https://api.minimax.io/anthropic', modelPlaceholder: 'MiniMax-M2.5', headers: {'anthropic-version': '2023-06-01'}},
	{id: 'COHERE', label: 'Cohere', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'https://api.cohere.ai/compatibility/v1', modelPlaceholder: 'command-r', headers: {}},
	{id: 'OLLAMA', label: 'Ollama', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'http://127.0.0.1:11434/v1', modelPlaceholder: 'llama3.2', headers: {}},
	{id: 'TOKEN_ROUTER', label: 'Token Router', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'https://api.tokenrouter.io/v1', modelPlaceholder: 'gpt-4o-mini', headers: {}},
	{id: 'ZAI', label: 'Z.ai', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'https://api.z.ai/api/paas/v4', modelPlaceholder: 'glm-4.5-flash', headers: {}},
	{id: 'LLAMA', label: 'Llama', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'https://api.together.xyz/v1', modelPlaceholder: 'meta-llama/Llama-3.3-70B-Instruct-Turbo', headers: {}},
	{id: 'VERTEX', label: 'Vertex AI', protocol: 'NATIVE', baseUrl: '', modelPlaceholder: 'gemini-3.6-flash', headers: {}},
	{id: 'WATSONX', label: 'watsonx', protocol: 'NATIVE', baseUrl: '', modelPlaceholder: 'ibm/granite-3-8b-instruct', headers: {}},
	{id: 'KILO', label: 'Kilo', protocol: 'OPENAI_COMPATIBLE', baseUrl: '', modelPlaceholder: 'kilo-code', headers: {}},
	{id: 'MIMO', label: 'Mimo', protocol: 'OPENAI_COMPATIBLE', baseUrl: '', modelPlaceholder: 'mimo-v2', headers: {}},
	{id: 'CUSTOM', label: 'Custom', protocol: 'OPENAI_COMPATIBLE', baseUrl: '', modelPlaceholder: 'my-model', headers: {}},
];

export function protocolLabel(protocol: string): string {
	if (protocol === 'ANTHROPIC_COMPATIBLE') {
		return 'Anthropic';
	}

	if (protocol === 'NATIVE') {
		return 'Native';
	}

	return 'OpenAI';
}
