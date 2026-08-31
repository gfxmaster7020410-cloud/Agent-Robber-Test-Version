import React from 'react';
import {Box, Text} from 'ink';

interface LanguageMeta {
	label: string;
	path: string;
}

const LANGUAGE_META: Record<string, LanguageMeta> = {
	ts: {label: 'TypeScript', path: 'src/plan.ts'},
	typescript: {label: 'TypeScript', path: 'src/plan.ts'},
	tsx: {label: 'TypeScript React', path: 'src/plan.tsx'},
	js: {label: 'JavaScript', path: 'src/plan.js'},
	javascript: {label: 'JavaScript', path: 'src/plan.js'},
	jsx: {label: 'JSX', path: 'src/plan.jsx'},
	py: {label: 'Python', path: 'scripts/plan.py'},
	python: {label: 'Python', path: 'scripts/plan.py'},
	sh: {label: 'Shell', path: 'scripts/run.sh'},
	bash: {label: 'Shell', path: 'scripts/run.sh'},
	shell: {label: 'Shell', path: 'scripts/run.sh'},
	json: {label: 'JSON', path: 'config/agent.json'},
	css: {label: 'CSS', path: 'styles/plan.css'},
	txt: {label: 'Text', path: 'workspace/listing.txt'},
	text: {label: 'Text', path: 'workspace/listing.txt'},
};

export function resolveLanguageMeta(language?: string): LanguageMeta {
	const key = language?.toLowerCase() ?? '';
	return (
		LANGUAGE_META[key] ?? {
			label: language ? language.toUpperCase() : 'Text',
			path: 'snippets/untitled.txt',
		}
	);
}

interface CodeBlockProps {
	code: string;
	language?: string;
	width: number;
}

export function CodeBlock({code, language, width}: CodeBlockProps) {
	const meta = resolveLanguageMeta(language);
	const tab = ` ${meta.path} · ${meta.label} `;
	const innerWidth = Math.max(tab.length + 2, width - 2);
	const fill = Math.max(1, innerWidth - tab.length - 1);
	return (
		<Box flexDirection="column" marginTop={1} marginBottom={1}>
			<Text>
				<Text color="cyan">╭─</Text>
				<Text backgroundColor="cyan" color="black" bold>
					{tab}
				</Text>
				<Text color="cyan">{'─'.repeat(fill)}╮</Text>
			</Text>
			<Box
				borderStyle="round"
				borderColor="cyan"
				borderTop={false}
				paddingX={1}
				width={innerWidth + 2}
			>
				<Text>{code}</Text>
			</Box>
		</Box>
	);
}
