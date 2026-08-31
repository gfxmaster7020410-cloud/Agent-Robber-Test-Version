import { Settings2, Zap, Eye, Brain, Bot, FileText, ToggleLeft, MoreVertical } from 'lucide-react';

export interface OrchModel {
  id: string;
  name: string;
  provider: 'OPENAI' | 'ANTHROPIC' | 'CUSTOM' | 'OLLAMA';
  status: 'HEALTHY' | 'DEGRADED' | 'RATE_LIMITED' | 'OFFLINE';
  capabilities: ('text' | 'vision' | 'reasoning' | 'tools' | 'streaming')[];
  metrics: { latency: number; context: string; cost: string };
  routing: { priority: number; weight: number };
}

const CapabilityBadge = ({ cap }: { cap: string }) => {
  const icons: Record<string, JSX.Element> = {
    text: <FileText size={10} />,
    vision: <Eye size={10} />,
    reasoning: <Brain size={10} />,
    tools: <Bot size={10} />,
    streaming: <Zap size={10} />,
  };
  return (
    <div className="flex items-center gap-1 bg-white/5 px-1.5 py-0.5 rounded text-[10px] text-cyan-400 border border-white/5">
      {icons[cap]} {cap.toUpperCase()}
    </div>
  );
};

const StatusBadge = ({ status }: { status: OrchModel['status'] }) => {
  const colors = {
    HEALTHY: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
    DEGRADED: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
    RATE_LIMITED: 'bg-purple-500/10 text-purple-400 border-purple-500/20',
    OFFLINE: 'bg-rose-500/10 text-rose-400 border-rose-500/20',
  };
  return (
    <span className={`px-2 py-0.5 rounded-full text-[9px] uppercase tracking-wider border font-bold ${colors[status]}`}>
      {status}
    </span>
  );
};

export const ModelCard = ({ model, onEdit }: { model: OrchModel; onEdit: () => void }) => (
  <div className="bg-[#0B0F17]/80 backdrop-blur-xl border border-white/10 rounded-2xl p-5 hover:border-cyan-500/30 transition-all">
    <div className="flex justify-between items-start mb-4">
      <div>
        <h3 className="font-bold text-slate-100 uppercase tracking-wider text-sm">{model.name}</h3>
        <p className="text-[10px] text-slate-500 font-mono tracking-normal">{model.provider}</p>
      </div>
      <StatusBadge status={model.status} />
    </div>

    <div className="flex flex-wrap gap-1 mb-4">
      {model.capabilities.map(c => <CapabilityBadge key={c} cap={c} />)}
    </div>

    <div className="grid grid-cols-2 gap-2 mb-4 font-mono tabular-nums text-[11px] text-slate-400">
      <div className="bg-white/5 p-2 rounded">p95: {model.metrics.latency}ms</div>
      <div className="bg-white/5 p-2 rounded">Ctx: {model.metrics.context}</div>
    </div>

    <div className="flex items-center justify-between mt-4 border-t border-white/5 pt-4">
      <span className="text-[11px] font-mono text-cyan-400 tracking-normal">P{model.routing.priority} · W{model.routing.weight}</span>
      <div className="flex gap-2">
        <button className="text-slate-400 hover:text-white"><ToggleLeft size={16} /></button>
        <button onClick={onEdit} className="text-cyan-400 hover:text-cyan-300"><Settings2 size={16} /></button>
        <button className="text-slate-400 hover:text-rose-400"><MoreVertical size={16} /></button>
      </div>
    </div>
  </div>
);

export const ModelGrid = ({ models, onEdit }: { models: OrchModel[]; onEdit: (m: OrchModel) => void }) => (
  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
    {models.map(m => <ModelCard key={m.id} model={m} onEdit={() => onEdit(m)} />)}
  </div>
);
