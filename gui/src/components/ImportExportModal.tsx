import { Upload } from 'lucide-react';

export const ImportExportModal = ({ isOpen, onClose: _onClose }: { isOpen: boolean; onClose: () => void }) => {
  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
      <div className="bg-[#0B0F17]/90 border border-white/10 rounded-2xl p-8 w-full max-w-lg">
        <h2 className="text-xl font-bold font-display text-slate-100 uppercase tracking-wider mb-6">Import / Export Models</h2>
        
        <div className="space-y-4">
          <div className="p-8 border-2 border-dashed border-white/10 rounded-xl text-center text-slate-400 hover:border-cyan-500/50 cursor-pointer">
            <Upload className="mx-auto mb-2" />
            Drop JSON Configuration
          </div>

          <div className="grid grid-cols-3 gap-2">
            <button className="bg-white/5 p-3 rounded-lg text-xs text-slate-300">Safe Export</button>
            <button className="bg-white/5 p-3 rounded-lg text-xs text-slate-300">Masked Export</button>
            <button className="bg-cyan-500/10 border border-cyan-500/40 text-cyan-400 p-3 rounded-lg text-xs">Download</button>
          </div>
        </div>
      </div>
    </div>
  );
};
