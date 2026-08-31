import {useMemo, useState} from 'react';
import type {DaemonState} from '../hooks/useDaemon';
import type {McpAddPayload} from '../lib/types';

interface MarketplaceProps {
	daemon: DaemonState | null;
	online: boolean;
	lastResult: {ok: boolean; server: string; tool: string; content: string} | null;
	onEnable: (id: string) => boolean;
	onDisable: (id: string) => boolean;
	onAdd: (payload: McpAddPayload) => boolean;
	onRemove: (id: string) => boolean;
	onCall: (server: string, tool: string, argumentsJson: string) => boolean;
	onList: () => boolean;
}

const EMPTY_FORM = {
	id: '',
	label: '',
	command: 'python',
	args: '',
	envKeys: '',
	description: '',
};

export function MarketplaceView({
	daemon,
	online,
	lastResult,
	onEnable,
	onDisable,
	onAdd,
	onRemove,
	onCall,
	onList,
}: MarketplaceProps) {
	const servers = daemon?.mcp?.servers ?? [];
	const tools = daemon?.mcp?.tools ?? [];
	const [form, setForm] = useState(EMPTY_FORM);
	const [callServer, setCallServer] = useState('');
	const [callTool, setCallTool] = useState('');
	const [callArgs, setCallArgs] = useState('{}');
	const [notice, setNotice] = useState('');

	const enabledTools = useMemo(
		() => tools.filter(tool => !callServer || tool.server === callServer),
		[callServer, tools],
	);

	function fail(message: string): void {
		setNotice(message);
	}

	function submitAdd(): void {
		const id = form.id.trim().toLowerCase();
		if (!id) {
			fail('Id is required.');
			return;
		}
		const args = form.args
			.split(/[\s,]+/)
			.map(item => item.trim())
			.filter(Boolean);
		const envKeys = form.envKeys
			.split(/[\s,]+/)
			.map(item => item.trim())
			.filter(Boolean);
		if (
			!onAdd({
				id,
				label: form.label.trim() || id,
				command: form.command.trim() || 'python',
				args,
				envKeys,
				description: form.description.trim(),
				enabled: false,
			})
		) {
			fail('Daemon offline — cannot add an MCP server.');
			return;
		}
		setForm(EMPTY_FORM);
		setNotice(`Added ${id} to the local catalog. Enable it when you want the host to spawn it.`);
	}

	return (
		<div className="view view--scroll">
			<div className="glass-card full-card">
				<div className="card__head">
					<span className="card__title font-display">MCP MARKETPLACE</span>
					<button type="button" className="mem0-action" onClick={() => onList()}>
						Refresh catalog
					</button>
				</div>
				<div className="card__body card__body--scroll">
					<p className="t-dim mcp-lead">
						Local catalog only. Built-in adapters stay inside the workspace jail. Stdio
						servers must be python or node running a file in this repo — no unsigned
						downloads, and env fields are key names, never values.
					</p>
					<div className="mcp-stats">
						<span>
							{daemon?.mcp?.enabled ?? 0} enabled · {tools.length} tools · host{' '}
							{daemon?.mcp?.started ? 'started' : 'idle'}
						</span>
						<span className={online ? 'accent-text' : 't-dim'}>
							{online ? 'daemon linked' : 'daemon offline'}
						</span>
					</div>
					<div className="mcp-grid">
						{servers.map(server => (
							<article className="mcp-card" key={server.id}>
								<div className="mcp-card__row">
									<div>
										<div className="mcp-card__id">{server.id}</div>
										<div className="mcp-card__label">{server.label}</div>
									</div>
									<button
										type="button"
										className={`toggle${server.enabled ? ' active' : ''}`}
										aria-label={`${server.enabled ? 'Disable' : 'Enable'} ${server.id}`}
										onClick={() => {
											const ok = server.enabled
												? onDisable(server.id)
												: onEnable(server.id);
											if (!ok) {
												fail('Daemon offline — cannot toggle MCP servers.');
											}
										}}
									>
										<span className="toggle__knob" />
									</button>
								</div>
								<p className="mcp-card__desc">{server.description || 'No description.'}</p>
								<div className="mcp-card__meta">
									<span className="mem-card__cat">{server.kind}</span>
									<span className="mcp-card__status">{server.status}</span>
									<span>{server.tool_count} tools</span>
									{server.builtin ? <span>built-in</span> : null}
								</div>
								{server.env_keys.length > 0 ? (
									<div className="mcp-card__env">
										env keys: {server.env_keys.join(', ')}
									</div>
								) : null}
								{server.error ? <div className="t-err">{server.error}</div> : null}
								{server.command ? (
									<div className="mcp-card__cmd">
										{server.command} {server.args.join(' ')}
									</div>
								) : null}
								{server.builtin ? null : (
									<button
										type="button"
										className="purge-btn"
										onClick={() => {
											if (!onRemove(server.id)) {
												fail('Daemon offline — cannot remove MCP servers.');
											}
										}}
									>
										Remove
									</button>
								)}
							</article>
						))}
					</div>

					<div className="cfg-section" style={{marginTop: 22}}>
						<span className="cfg-section__title font-display">Call a tool</span>
						<div className="cfg-grid">
							<div className="cfg-field">
								<label>Server</label>
								<select
									className="glass-select"
									value={callServer}
									onChange={event => {
										setCallServer(event.target.value);
										setCallTool('');
									}}
								>
									<option value="">Select server</option>
									{servers
										.filter(server => server.enabled)
										.map(server => (
											<option key={server.id} value={server.id}>
												{server.id}
											</option>
										))}
								</select>
							</div>
							<div className="cfg-field">
								<label>Tool</label>
								<select
									className="glass-select"
									value={callTool}
									onChange={event => setCallTool(event.target.value)}
								>
									<option value="">Select tool</option>
									{enabledTools.map(tool => (
										<option key={tool.qualified} value={tool.name}>
											{tool.qualified}
										</option>
									))}
								</select>
							</div>
						</div>
						<div className="cfg-field">
							<label>Arguments (JSON object, no secrets)</label>
							<textarea
								className="glass-textarea"
								value={callArgs}
								onChange={event => setCallArgs(event.target.value)}
							/>
						</div>
						<button
							type="button"
							className="mem0-action mem0-action--inject"
							onClick={() => {
								if (!callServer || !callTool) {
									fail('Pick a server and a tool.');
									return;
								}
								if (!onCall(callServer, callTool, callArgs)) {
									fail('Daemon offline — cannot call MCP.');
								}
							}}
						>
							Call tool
						</button>
						{lastResult ? (
							<pre className="mcp-result">
								{lastResult.ok ? 'ok' : 'blocked'} {lastResult.server}/{lastResult.tool}
								{'\n'}
								{lastResult.content}
							</pre>
						) : null}
					</div>

					<div className="cfg-section" style={{marginTop: 22}}>
						<span className="cfg-section__title font-display">Add local stdio server</span>
						<p className="t-dim">
						The script must already exist inside the workspace. Env keys are names
						pulled from .env — values are never stored in mcp.json.
						</p>
						<div className="cfg-grid">
							<div className="cfg-field">
								<label>Id</label>
								<input
									className="glass-input"
									value={form.id}
									onChange={event => setForm({...form, id: event.target.value})}
									placeholder="my-helper"
								/>
							</div>
							<div className="cfg-field">
								<label>Label</label>
								<input
									className="glass-input"
									value={form.label}
									onChange={event => setForm({...form, label: event.target.value})}
									placeholder="My helper"
								/>
							</div>
							<div className="cfg-field">
								<label>Command</label>
								<input
									className="glass-input"
									value={form.command}
									onChange={event => setForm({...form, command: event.target.value})}
									placeholder="python"
								/>
							</div>
							<div className="cfg-field">
								<label>Args</label>
								<input
									className="glass-input"
									value={form.args}
									onChange={event => setForm({...form, args: event.target.value})}
									placeholder="tools/my_mcp.py"
								/>
							</div>
						</div>
						<div className="cfg-field">
							<label>Env key names</label>
							<input
								className="glass-input"
								value={form.envKeys}
								onChange={event => setForm({...form, envKeys: event.target.value})}
								placeholder="GEMINI_API_KEY"
							/>
						</div>
						<div className="cfg-field">
							<label>Description</label>
							<input
								className="glass-input"
								value={form.description}
								onChange={event => setForm({...form, description: event.target.value})}
							/>
						</div>
						<button type="button" className="mem0-action" onClick={submitAdd}>
							Add to catalog
						</button>
					</div>
					{notice ? <p className="mcp-notice">{notice}</p> : null}
				</div>
			</div>
		</div>
	);
}
