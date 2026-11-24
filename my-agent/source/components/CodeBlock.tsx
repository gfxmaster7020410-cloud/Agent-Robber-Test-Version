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
	diff: {label: 'Diff', path: 'workspace/changes.diff'},
	txt: {label: 'Text', path: 'workspace/listing.txt'},
	text: {label: 'Text', path: 'workspace/listing.txt'},
};

const KEYWORD_RE =
	/\b(and|as|assert|async|await|break|case|catch|class|const|continue|def|elif|else|enum|export|extends|finally|for|from|function|if|import|in|interface|let|new|not|or|return|switch|throw|try|type|typeof|var|void|while|with|yield)\b/g;
const STRING_RE = /(['"`])(?:\\.|(?!\1).)*\1/g;
const COMMENT_RE = /(\/\/.*?$|#.*?$)/gm;

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

type Span = {text: string; color?: string; dim?: boolean; bold?: boolean};

function diffSpans(line: string): Span[] {
	if (/^(diff |index |--- |\+\+\+ |@@ )/.test(line)) {
		return [{text: line, color: 'cyan', dim: true}];
	}
	if (line.startsWith('+')) {
		return [{text: line, color: 'green'}];
	}
	if (line.startsWith('-')) {
		return [{text: line, color: 'red'}];
	}
	return [{text: line}];
}

function syntaxSpans(line: string): Span[] {
	const marks: {start: number; end: number; color: string}[] = [];
	for (const match of line.matchAll(COMMENT_RE)) {
		marks.push({start: match.index ?? 0, end: (match.index ?? 0) + match[0].length, color: '#6A9955'});
	}
	for (const match of line.matchAll(STRING_RE)) {
		marks.push({start: match.index ?? 0, end: (match.index ?? 0) + match[0].length, color: '#CE9178'});
	}
	for (const match of line.matchAll(/\b([A-Z][A-Za-z0-9_]*)\b/g)) {
		marks.push({start: match.index ?? 0, end: (match.index ?? 0) + match[0].length, color: '#4EC9B0'});
	}
	for (const match of line.matchAll(/\b([A-Za-z_][\w]*)\s*(?=\()/g)) {
		marks.push({start: match.index ?? 0, end: (match.index ?? 0) + match[1].length, color: '#DCDCAA'});
	}
	for (const match of line.matchAll(KEYWORD_RE)) {
		marks.push({start: match.index ?? 0, end: (match.index ?? 0) + match[0].length, color: '#C586C0'});
	}
	for (const match of line.matchAll(/\b(true|false|null|None|undefined)\b/g)) {
		marks.push({start: match.index ?? 0, end: (match.index ?? 0) + match[0].length, color: '#569CD6'});
	}
	marks.sort((a, b) => a.start - b.start || b.end - a.end);
	const picked: typeof marks = [];
	let cursor = 0;
	for (const mark of marks) {
		if (mark.start < cursor) {
			continue;
		}
		picked.push(mark);
		cursor = mark.end;
	}
	if (picked.length === 0) {
		return [{text: line}];
	}
	const spans: Span[] = [];
	let index = 0;
	for (const mark of picked) {
		if (mark.start > index) {
			spans.push({text: line.slice(index, mark.start)});
		}
		spans.push({text: line.slice(mark.start, mark.end), color: mark.color});
		index = mark.end;
	}
	if (index < line.length) {
		spans.push({text: line.slice(index)});
	}
	return spans;
}

function CodeLine({line, language}: {line: string; language?: string}) {
	const spans = (language || '').toLowerCase() === 'diff' ? diffSpans(line) : syntaxSpans(line);
	return (
		<Text wrap="wrap">
			{spans.map((span, index) => (
				<Text key={index} color={span.color} dimColor={span.dim} bold={span.bold}>
					{span.text.length > 0 ? span.text : ' '}
				</Text>
			))}
		</Text>
	);
}

export function CodeBlock({code, language, width}: CodeBlockProps) {
	const meta = resolveLanguageMeta(language);
	const tab = ` ${meta.path} · ${meta.label} `;
	const pane = Math.max(60, width - 4);
	const innerWidth = Math.max(tab.length + 2, pane);
	const fill = Math.max(1, innerWidth - tab.length - 1);
	const lines = code.length > 0 ? code.split('\n') : [' '];
	return (
		<Box flexDirection="column" marginTop={1} marginBottom={1} width={pane}>
			<Text wrap="truncate">
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
				width={Math.min(width, innerWidth + 2)}
				flexDirection="column"
			>
				{lines.map((line, index) => (
					<CodeLine key={index} line={line} language={language} />
				))}
			</Box>
		</Box>
	);
}
