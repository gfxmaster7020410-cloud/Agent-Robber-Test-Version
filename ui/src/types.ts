export type NativeCommand =
  | 'audit'
  | 'sync'
  | 'graph'
  | 'resolve'
  | 'pause'
  | 'resume'
  | 'snapshots'
  | 'restore'
  | 'help'
  | 'clear'
  | 'exit';

export type ParsedKind = 'empty' | 'prompt' | 'command' | 'unknown';

export interface ParsedInput {
  kind: ParsedKind;
  text: string;
  name: string;
  args: string[];
  raw: string;
  suggestions: string[];
}

export interface TaskRow {
  id: string;
  title: string;
  status: string;
  depends_on?: string;
  updated_at?: number;
}

export interface AgentRow {
  id: string;
  worker_id: string;
  phase: string;
  loop_status: string;
  updated_at?: number;
}

export interface DecisionRow {
  id: string;
  branch?: string;
  parent_id?: string;
  choice?: string;
  version_tag?: string;
  is_head?: boolean;
  is_final?: boolean;
  confidence_score?: number;
  summary?: string;
}

export interface SsotSnapshot {
  project_id?: string;
  tasks: TaskRow[];
  agents: AgentRow[];
  decisions: DecisionRow[];
}

export interface TaskAck {
  id: string;
  status?: string;
  task_type?: string;
  forwarded?: boolean;
  ipc?: boolean;
  project_id?: string;
  route?: string;
}

export type GatewayEvent =
  | {type: 'open'; channel: 'ssot' | 'tokens'}
  | {type: 'close'; channel: 'ssot' | 'tokens'; code?: number}
  | {type: 'error'; message: string}
  | {
      type: 'ssot';
      table: string;
      op: string;
      rows: Record<string, unknown>[];
      seq: number;
      ts?: number;
    }
  | {type: 'token'; text: string; raw?: string}
  | {type: 'snapshot'; snapshot: SsotSnapshot}
  | {
      type: 'question';
      id: string;
      question: string;
      options: string[];
      allow_custom: boolean;
      timeout: number;
      source_worker: string;
      task_id: string;
      created_at: number;
    }
  | {type: 'question_answer'; question_id: string; answer: string};

export interface SubmitTaskInput {
  prompt: string;
  title?: string;
  task_type?: string;
}

export type PacingState = 'running' | 'paused';

export interface CommandResult {
  ok: boolean;
  content: string;
  interactive?: boolean;
  options?: string[];
  pacing?: PacingState;
}

export interface QuestionPrompt {
  id: string;
  question: string;
  options: string[];
  allow_custom: boolean;
  timeout: number;
  source_worker: string;
  task_id: string;
  created_at: number;
}
