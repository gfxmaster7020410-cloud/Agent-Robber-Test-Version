import type {BatchClass, ProviderType} from './types';

export interface ProviderCard {
	id: string;
	name: string;
	hint: string;
	mark: string;
	accent: string;
	placeholder: string;
}

export const AI_PROVIDERS: readonly ProviderCard[] = [
	{id: 'openai', name: 'OpenAI', hint: 'GPT-4o / o-series', mark: 'OA', accent: '#10a37f', placeholder: 'gpt-4o-mini'},
	{id: 'anthropic', name: 'Anthropic', hint: 'Claude family', mark: 'AN', accent: '#d4a27f', placeholder: 'claude-3-5-haiku-20241022'},
	{id: 'gemini', name: 'Google Gemini', hint: 'Flash / Pro', mark: 'GM', accent: '#4285f4', placeholder: 'gemini-3.6-flash'},
	{id: 'mistral', name: 'Mistral', hint: 'Small / Large', mark: 'MS', accent: '#ff7000', placeholder: 'mistral-small-latest'},
	{id: 'cohere', name: 'Cohere', hint: 'Command R', mark: 'CH', accent: '#39594d', placeholder: 'command-r'},
	{id: 'groq', name: 'Groq', hint: 'Llama on Groq', mark: 'GQ', accent: '#f55036', placeholder: 'openai/gpt-oss-20b'},
	{id: 'together', name: 'Together AI', hint: 'Open weights', mark: 'TG', accent: '#0f6fff', placeholder: 'meta-llama/Llama-3-8b-chat-hf'},
	{id: 'deepseek', name: 'DeepSeek', hint: 'V3 / Coder', mark: 'DS', accent: '#4d6bfe', placeholder: 'deepseek-chat'},
];

export const KEY_PROVIDERS = [
	'groq',
	'gemini',
	'anthropic',
	'openai',
	'opencode',
	'mistral',
	'cohere',
	'together',
	'deepseek',
] as const;

export const BATCH_OPTIONS: readonly {
	id: BatchClass;
	title: string;
	blurb: string;
}[] = [
	{
		id: 'eco',
		title: 'Eco',
		blurb: 'Fast / cheap pool. Small, flash, or local models.',
	},
	{
		id: 'normal',
		title: 'Balanced',
		blurb: 'Mid-tier daily routing. Mini / Haiku / Flash class.',
	},
	{
		id: 'heavy',
		title: 'Best',
		blurb: 'Frontier quality. Sonnet, GPT-4o, Gemini Pro class.',
	},
];

export const ARCH_OPTIONS: readonly {
	id: ProviderType;
	title: string;
	blurb: string;
}[] = [
	{
		id: 'native',
		title: 'Native integration',
		blurb: 'Provider API with a key and model id.',
	},
	{
		id: 'custom_base',
		title: 'Custom base URL',
		blurb: 'vLLM, RunPod, Hugging Face, or a private OpenAI-compatible endpoint.',
	},
	{
		id: 'local_ollama',
		title: 'Local model / Ollama',
		blurb: 'Talk to a local Ollama daemon. Auto-scan downloaded tags.',
	},
];

export function batchLabel(batch: BatchClass): string {
	if (batch === 'eco') {
		return 'Eco';
	}

	if (batch === 'heavy') {
		return 'Best';
	}

	return 'Balanced';
}
