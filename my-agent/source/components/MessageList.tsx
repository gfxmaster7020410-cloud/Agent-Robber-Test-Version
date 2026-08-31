import React from 'react';
import {Box, Text} from 'ink';
import type {ChatMessage} from '../types.js';
import {
	countWrappedLines,
	parseContentSegments,
} from '../utils/content.js';
import {inkProps} from '../utils/typography.js';
import {ToolBadge} from './ToolBadge.js';
import {CodeBlock} from './CodeBlock.js';

interface MessageListProps {
	messages: ChatMessage[];
	reservedLines: number;
	busy: boolean;
	width: number;
	rows: number;
}

function estimateMessageLines(message: ChatMessage, width: number): number {
	const innerWidth = Math.max(10, width - 1);
	switch (message.role) {
		case 'tool':
		case 'memory':
		case 'success': {
			return 1;
		}

		case 'assistant': {
			const segments = parseContentSegments(message.content);
			return segments.reduce((total, segment) => {
				if (segment.type === 'code') {
					return (
						total +
						segment.value.split('\n').length +
						4
					);
				}

				return total + countWrappedLines(segment.value, innerWidth);
			}, 0);
		}

		default: {
			return countWrappedLines(message.content, innerWidth);
		}
	}
}

function sliceToFit(
	messages: ChatMessage[],
	budget: number,
	width: number,
): {shown: ChatMessage[]; hiddenCount: number} {
	if (messages.length === 0) {
		return {shown: [], hiddenCount: 0};
	}

	let lastAnchor = messages.length - 1;
	for (let index = messages.length - 1; index >= 0; index -= 1) {
		const role = messages[index]!.role;
		if (role === 'assistant' || role === 'user') {
			lastAnchor = index;
			break;
		}
	}

	let used = 0;
	for (let index = lastAnchor; index < messages.length; index += 1) {
		used += estimateMessageLines(messages[index]!, width) + 1;
	}

	let firstShown = lastAnchor;
	if (used <= budget) {
		for (let index = lastAnchor - 1; index >= 0; index -= 1) {
			const cost = estimateMessageLines(messages[index]!, width) + 1;
			if (used + cost > budget) {
				break;
			}

			used += cost;
			firstShown = index;
		}
	}

	return {
		shown: messages.slice(firstShown),
		hiddenCount: firstShown,
	};
}

function MessageView({message, width}: {message: ChatMessage; width: number}) {
	switch (message.role) {
		case 'user':
			return (
				<Text {...inkProps('body')}>
					<Text color="cyanBright">❯ </Text>
					<Text color="green">{message.content}</Text>
				</Text>
			);

		case 'assistant': {
			const segments = parseContentSegments(message.content);
			return (
				<Box flexDirection="column">
					{segments.map((segment, index) =>
						segment.type === 'code' ? (
							<CodeBlock
								key={index}
								code={segment.value}
								language={segment.language}
								width={width - 2}
							/>
						) : (
							<Text key={index}>{segment.value}</Text>
						),
					)}
				</Box>
			);
		}

		case 'tool':
			return (
				<ToolBadge
					kind="tool"
					label={message.content}
					timestamp={message.timestamp}
				/>
			);

		case 'memory':
			return (
				<ToolBadge
					kind="memory"
					label={message.content}
					timestamp={message.timestamp}
				/>
			);

		case 'success':
			return (
				<ToolBadge
					kind="success"
					label={message.content}
					timestamp={message.timestamp}
				/>
			);

		case 'system':
			return (
				<Text {...inkProps('metadata')}>ℹ {message.content}</Text>
			);

		case 'error':
			return (
				<Text {...inkProps('status')} color="red">
					✖ {message.content}
				</Text>
			);

		default:
			return null;
	}
}

function MessageListInner({
	messages,
	reservedLines,
	busy,
	width,
	rows,
}: MessageListProps) {
	const availableHeight = Math.max(4, rows - reservedLines - (busy ? 2 : 0));
	const firstPass = sliceToFit(messages, availableHeight, width);
	const {shown, hiddenCount} =
		firstPass.hiddenCount > 0
			? sliceToFit(messages, availableHeight - 1, width)
			: firstPass;

	if (messages.length === 0) {
		return (
			<Box
				flexDirection="column"
				height={availableHeight}
				paddingLeft={1}
				justifyContent="center"
			>
				<Text {...inkProps('metadata')}>
					Ask anything — Agent Robber recalls SSOT memory, scans the
					workspace and answers.
				</Text>
				<Text {...inkProps('metadata')}>
					Type /help to see available commands.
				</Text>
			</Box>
		);
	}

	return (
		<Box
			flexDirection="column"
			height={availableHeight}
			overflow="hidden"
			paddingLeft={1}
		>
			{hiddenCount > 0 ? (
				<Text {...inkProps('micro')}>
					▲ {hiddenCount} earlier message(s) scrolled up
				</Text>
			) : null}
			{shown.map(message => (
				<Box key={message.id} marginBottom={1}>
					<MessageView message={message} width={width} />
				</Box>
			))}
		</Box>
	);
}

export const MessageList = React.memo(MessageListInner);
