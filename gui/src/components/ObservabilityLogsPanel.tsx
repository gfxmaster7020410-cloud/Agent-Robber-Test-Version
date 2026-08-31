import { Pause, Filter } from 'lucide-react';

export const ObservabilityLogsPanel = () => {
  const logs = [
    { id: 'tr_123', time: '12:00:01', model: 'gpt-4o', latency: '450ms', tokens: '120/450', status: '200 OK' },
    { id: 'tr_124', time: '12:00:05', model: 'claude-3-5', latency: '800ms', tokens: '300/1200', status: '200 OK' },
    { id: 'tr_125', time: '12:00:10', model: 'deepseek-r1', latency: '1200ms', tokens: '50/0', status: '429 Rate Limit' },
  ];

  return (
    <div className="bg-[#0B0F17]/80 backdrop-blur-xl border border-white/10 rounded-2xl p-6">
      <div className="flex justify-between items-center mb-6">
        <h2 className="text-lg font-bold text-slate-100 uppercase tracking-wider">Live Logs</h2>
        <div className="flex gap-2">
          <button className="text-slate-400"><Filter size={18} /></button>
          <button className="text-slate-400"><Pause size={18} /></button>
        </div>
      </div>
      <table className="w-full text-xs font-mono tabular-nums text-slate-300">
        <thead className="text-slate-500">
          <tr>
            <th className="text-left py-2">ID</th>
            <th className="text-left py-2">Time</th>
            <th className="text-left py-2">Model</th>
            <th className="text-left py-2">Status</th>
          </tr>
        </thead>
        <tbody>
          {logs.map((log) => (
            <tr key={log.id} className="border-t border-white/5">
              <td className="py-3">{log.id}</td>
              <td className="py-3">{log.time}</td>
              <td className="py-3">{log.model}</td>
              <td className={`py-3 ${log.status.includes('200') ? 'text-emerald-400' : 'text-rose-400'}`}>{log.status}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};
