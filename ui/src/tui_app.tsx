import React, {useCallback, useEffect, useMemo, useState} from 'react';
import {Box, Text, useApp, useInput} from 'ink';
import TextInput from 'ink-text-input';
import {GatewayClient} from './gateway_client.ts';
import {SlashCommandParser} from './command_parser.ts';
import {CommandRouter} from './command_router.ts';
import type {GatewayEvent, PacingState, QuestionPrompt} from './types.ts';

const LOG_CAP = 240;

export interface TuiProps {
  client: GatewayClient;
  root?: string;
  onExit?: () => void;
}

export function formatLogLine(event: GatewayEvent): string | null {
  switch (event.type) {
    case 'open':
      return `ws ${event.channel} open`;
    case 'close':
      return `ws ${event.channel} closed`;
    case 'error':
      return `ws error ${event.message}`;
    case 'token':
      return event.text;
    case 'ssot':
      return `ssot ${event.table} ${event.op} n=${event.rows.length}`;
    case 'snapshot':
      return `snapshot tasks=${event.snapshot.tasks.length}`;
    default:
      return null;
  }
}

export function formatStatePane(
  client: GatewayClient,
  pacing: PacingState,
  connected: boolean,
): string {
  const tasks = [...client.store.tasks.values()].slice(0, 8);
  const agent = [...client.store.agents.values()][0];
  const lines = [
    `link     ${connected ? 'live' : 'down'}`,
    `pacing   ${pacing}`,
    `tasks    ${client.store.tasks.size}`,
    `agents   ${client.store.agents.size}`,
    `decisions ${client.store.decisions.size}`,
  ];
  if (agent) {
    lines.push(`phase    ${agent.phase} ${agent.loop_status}`.trim());
  }
  for (const task of tasks) {
    lines.push(`${task.status.padEnd(8)} ${task.title.slice(0, 22)}`);
  }
  return lines.join('\n');
}

export function TuiApp({client, root = process.cwd(), onExit}: TuiProps): React.ReactElement {
  const {exit} = useApp();
  const parser = useMemo(() => new SlashCommandParser(), []);
  const router = useMemo(
    () => new CommandRouter({client, parser, root, pacing: 'running'}),
    [client, parser, root],
  );
  const [logs, setLogs] = useState<string[]>(['Agent Robber TUI. Type /help.']);
  const [scroll, setScroll] = useState(0);
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(client.connected);
  const [pacing, setPacing] = useState<PacingState>(router.pacing);
  const [tokenBuf, setTokenBuf] = useState('');
  const [question, setQuestion] = useState<QuestionPrompt | null>(null);
  const [questionIdx, setQuestionIdx] = useState(0);
  const [questionCustom, setQuestionCustom] = useState('');

  const pushLog = useCallback((line: string) => {
    const parts = line.split('\n').filter((row) => row.length > 0);
    if (!parts.length) {
      return;
    }
    setLogs((prev) => {
      const next = [...prev, ...parts];
      return next.length > LOG_CAP ? next.slice(next.length - LOG_CAP) : next;
    });
    setScroll(0);
  }, []);

  useEffect(() => {
    const off = client.onEvent((event) => {
      if (event.type === 'open' || event.type === 'close') {
        setConnected(client.connected);
      }
      if (event.type === 'question') {
        setQuestion({
          id: event.id,
          question: event.question,
          options: event.options,
          allow_custom: event.allow_custom,
          timeout: event.timeout,
          source_worker: event.source_worker,
          task_id: event.task_id,
          created_at: event.created_at,
        });
        setQuestionIdx(0);
        setQuestionCustom('');
        return;
      }
      if (event.type === 'question_answer') {
        setQuestion(null);
        setQuestionIdx(0);
        setQuestionCustom('');
        return;
      }
      if (event.type === 'token') {
        setTokenBuf((prev) => {
          const merged = prev + event.text;
          if (merged.includes('\n') || merged.length > 80) {
            pushLog(merged);
            return '';
          }
          return merged;
        });
        return;
      }
      const line = formatLogLine(event);
      if (line) {
        pushLog(line);
      }
    });
    return off;
  }, [client, pushLog]);

  useInput((input, key) => {
    if (question) {
      if (key.upArrow) {
        setQuestionIdx((i) => Math.max(0, i - 1));
        return;
      }
      if (key.downArrow) {
        setQuestionIdx((i) => Math.min((question.options.length || 1) - 1, i + 1));
        return;
      }
      if (key.return) {
        const answer =
          questionCustom.trim() || question.options[questionIdx] || '';
        if (answer) {
          client.answerQuestion(question.id, answer);
          pushLog(`> [answer] ${answer}`);
          setQuestion(null);
          setQuestionCustom('');
        }
        return;
      }
      if (key.escape) {
        client.answerQuestion(question.id, '[timeout]');
        pushLog('> [answer] dismissed');
        setQuestion(null);
        setQuestionCustom('');
        return;
      }
      return;
    }
    if (key.pageUp) {
      setScroll((n) => n + 5);
    }
    if (key.pageDown) {
      setScroll((n) => Math.max(0, n - 5));
    }
    if (key.ctrl && input === 'c') {
      onExit?.();
      exit();
    }
  });

  const submit = async (line: string) => {
    const text = line.trim();
    setValue('');
    if (!text || busy) {
      return;
    }
    pushLog(`> ${text}`);
    setBusy(true);
    try {
      const result = await router.handle(text);
      if (result.pacing) {
        setPacing(result.pacing);
      }
      if (result.content === '__clear__') {
        setLogs([]);
        setTokenBuf('');
      } else if (result.content === '__exit__') {
        onExit?.();
        exit();
      } else if (result.content) {
        pushLog(result.content);
      }
    } catch (err) {
      pushLog(`error ${String(err)}`);
    } finally {
      setBusy(false);
    }
  };

  const visible = useMemo(() => {
    const end = logs.length - scroll;
    const start = Math.max(0, end - 16);
    return logs.slice(start, end);
  }, [logs, scroll]);

  const stateText = formatStatePane(client, pacing, connected);

  return (
    <Box flexDirection="column" width="100%" height="100%">
      <Box>
        <Text color="cyan">Agent Robber</Text>
        <Text>  gateway {client.baseUrl}  </Text>
        <Text color={connected ? 'green' : 'red'}>{connected ? 'live' : 'down'}</Text>
        {busy ? <Text color="yellow">  busy</Text> : null}
        {question ? <Text color="magenta">  CLARIFICATION NEEDED</Text> : null}
      </Box>
      <Box flexDirection="row" flexGrow={1}>
        <Box flexDirection="column" width="65%" borderStyle="single" paddingX={1}>
          <Text color="gray">execution log / tokens</Text>
          {visible.map((row, i) => (
            <Text key={`${i}-${row.slice(0, 12)}`} wrap="truncate">
              {row}
            </Text>
          ))}
          {tokenBuf ? <Text color="white">{tokenBuf}</Text> : null}
        </Box>
        <Box flexDirection="column" width="35%" borderStyle="single" paddingX={1}>
          <Text color="gray">live state</Text>
          {stateText.split('\n').map((row) => (
            <Text key={row} wrap="truncate">
              {row}
            </Text>
          ))}
        </Box>
      </Box>
      {question ? (
        <Box flexDirection="column" borderStyle="double" borderColor="cyan" paddingX={1} marginY={0}>
          <Text color="cyan" bold>CLARIFICATION REQUIRED</Text>
          <Text wrap="wrap">{question.question}</Text>
          {question.source_worker ? (
            <Text color="gray">from: {question.source_worker}</Text>
          ) : null}
          <Text />
          {question.options.map((opt, i) => (
            <Text key={opt}>
              {i === questionIdx ? <Text color="cyan" bold>{' > '}</Text> : <Text>{'   '}</Text>}
              {i === questionIdx ? <Text color="white" bold>{opt}</Text> : <Text color="gray">{opt}</Text>}
            </Text>
          ))}
          {question.allow_custom ? (
            <Text color="gray">
              {questionCustom ? `custom: ${questionCustom}` : '(type to enter custom answer, Enter to submit)'}
            </Text>
          ) : null}
          <Text color="gray">[↑/↓] select  [Enter] confirm  [Esc] dismiss</Text>
        </Box>
      ) : null}
      <Box>
        <Text>{busy ? '* ' : '> '}</Text>
        <TextInput value={value} onChange={setValue} onSubmit={submit} placeholder="/help" />
      </Box>
    </Box>
  );
}

export default TuiApp;
