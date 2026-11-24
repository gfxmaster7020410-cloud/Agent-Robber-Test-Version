import {vscDarkPlus} from 'react-syntax-highlighter/dist/esm/styles/prism';

export const HIGHLIGHT = {
	...vscDarkPlus,
	'pre[class*="language-"]': {
		...(vscDarkPlus['pre[class*="language-"]'] as object),
		background: '#1e1e1e',
		margin: 0,
		padding: '12px 16px',
		fontSize: '13px',
		lineHeight: 1.5,
		width: '100%',
		maxWidth: '100%',
		overflowX: 'auto' as const,
	},
	'code[class*="language-"]': {
		...(vscDarkPlus['code[class*="language-"]'] as object),
		background: 'transparent',
		fontFamily: 'BogitaMono, ui-monospace, monospace',
		fontSize: '13px',
	},
	keyword: {color: '#C586C0'},
	builtin: {color: '#569CD6'},
	boolean: {color: '#569CD6'},
	function: {color: '#DCDCAA'},
	'class-name': {color: '#4EC9B0'},
	string: {color: '#CE9178'},
	template: {color: '#CE9178'},
	comment: {color: '#6A9955'},
	number: {color: '#B5CEA8'},
	operator: {color: '#D4D4D4'},
	punctuation: {color: '#D4D4D4'},
	property: {color: '#9CDCFE'},
	variable: {color: '#9CDCFE'},
	constant: {color: '#4FC1FF'},
	tag: {color: '#569CD6'},
	atrule: {color: '#C586C0'},
	selector: {color: '#D7BA7D'},
	inserted: {color: '#22c55e', background: 'rgba(34, 197, 94, 0.12)'},
	deleted: {color: '#ef4444', background: 'rgba(239, 68, 68, 0.12)'},
};

const LANG: Record<string, string> = {
	py: 'python',
	python: 'python',
	ts: 'typescript',
	tsx: 'tsx',
	js: 'javascript',
	jsx: 'jsx',
	mjs: 'javascript',
	json: 'json',
	css: 'css',
	html: 'markup',
	md: 'markdown',
	diff: 'diff',
	patch: 'diff',
	sh: 'bash',
	bash: 'bash',
	shell: 'bash',
	rs: 'rust',
	go: 'go',
	toml: 'toml',
	yaml: 'yaml',
	yml: 'yaml',
};

export function prismLang(language: string): string {
	const key = (language || '').toLowerCase();
	return LANG[key] || key || 'text';
}
