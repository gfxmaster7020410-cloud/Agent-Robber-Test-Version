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

export interface ChatMessage {
	id: number;
	role: MessageRole;
	content: string;
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
