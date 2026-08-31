import { useState, useEffect, useRef, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import type { SuiteEvent } from '../lib/types';

interface Profile {
  id: string;
  target: string;
  state: 'Idle' | 'Executing' | 'Cooldown';
  proxy: string;
}

export function FBSharkMonitor({ suiteEvents = [] }: { suiteEvents?: SuiteEvent[] }) {
  const [status, setStatus] = useState({ instances: 0, motion_engine: 'Calibration', micro_delay: '45ms - 180ms', health: 'Ready', is_running: false });
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [logs, setLogs] = useState<string[]>(['[SYSTEM] FB SHARK initialized...']);
  const terminalRef = useRef<HTMLDivElement>(null);

  const addLog = useCallback((msg: string) => {
    setLogs(prev => [...prev, `[${new Date().toLocaleTimeString()}] ${msg}`]);
  }, []);

  useEffect(() => {
    if (terminalRef.current) terminalRef.current.scrollTop = terminalRef.current.scrollHeight;
  }, [logs]);

  const fetchData = useCallback(async () => {
    try {
      const [s, p, l] = await Promise.all([
        fetch('/api/fb_shark/status').then(r => r.json()),
        fetch('/api/fb_shark/profiles').then(r => r.json()),
        fetch('/api/fb_shark/logs').then(r => r.json()),
      ]);
      setStatus(s);
      setProfiles(p);
      setLogs(l.map((log: any) => `[${log.timestamp.split(' ')[1]}] [${log.level}] ${log.message}`));
    } catch (e) {
      addLog('[ERROR] Failed to fetch data, using local fallback.');
    }
  }, [addLog]);

  useEffect(() => { fetchData(); }, [fetchData]);

  useEffect(() => {
    const latest = suiteEvents[suiteEvents.length - 1];
    if (!latest) {
      return;
    }

    if (latest.type === 'worker_tick' || latest.type === 'vision_alert' || latest.type === 'vault_alert') {
      addLog(latest.content || `[${latest.type}]`);
    }
  }, [addLog, suiteEvents]);

  const handleAction = async (action: 'start' | 'stop' | 'refresh' | string, profileId?: string) => {
    try {
      if (profileId) {
        await fetch(`/api/fb_shark/profile/${profileId}/action?action=${action}`, { method: 'POST' });
        addLog(`[ACTION] Profile ${profileId} ${action}ed`);
      } else if (action === 'refresh') {
        fetchData();
        return;
      } else {
        await fetch(`/api/fb_shark/${action}`, { method: 'POST' });
        addLog(`[ACTION] Engine ${action}ed`);
      }
      fetchData();
    } catch (e) { addLog(`[ERROR] ${action} failed`); }
  };

  const themeStyles = {
    bg: 'rgba(var(--bg-card), 0.6)',
    text: 'var(--text-main)',
    border: 'rgba(var(--text-main), 0.25)',
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px', padding: '24px', height: '100%', backgroundColor: themeStyles.bg, backdropFilter: 'blur(16px)', borderRadius: '16px', border: `1px solid ${themeStyles.border}`, color: themeStyles.text }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '16px' }}>
        {[
          { label: 'Instances', value: `${status.instances} Workers` },
          { label: 'Motion Engine', value: status.motion_engine },
          { label: 'Micro-Delay', value: status.micro_delay },
          { label: 'Health', value: status.health },
        ].map((m, i) => (
          <div key={i} style={{ backgroundColor: 'rgba(128, 128, 128, 0.05)', padding: '16px', borderRadius: '12px', border: `1px solid ${themeStyles.border}`, textAlign: 'center' }}>
            <div style={{ fontSize: '0.75rem', letterSpacing: '0.1em', opacity: 0.7, marginBottom: '8px', textTransform: 'uppercase' }}>{m.label}</div>
            <div style={{ fontSize: '1.25rem', fontWeight: 600, letterSpacing: '0.06em' }}>{m.value}</div>
          </div>
        ))}
      </div>

      <div style={{ display: 'flex', gap: '12px' }}>
        <button onClick={() => handleAction('start')} style={{ padding: '10px 20px', borderRadius: '8px', border: 'none', background: status.is_running ? '#4f46e5' : '#8b5cf6', color: 'white', cursor: 'pointer', transition: 'all 0.2s ease-in-out' }}>Start Loop</button>
        <button onClick={() => handleAction('stop')} style={{ padding: '10px 20px', borderRadius: '8px', border: 'none', background: '#ef4444', color: 'white', cursor: 'pointer', transition: 'all 0.2s ease-in-out' }}>Emergency Stop</button>
        <button onClick={() => handleAction('refresh')} style={{ padding: '10px 20px', borderRadius: '8px', border: 'none', background: 'rgba(128, 128, 128, 0.2)', cursor: 'pointer', transition: 'all 0.2s ease-in-out' }}>Refresh</button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '16px', overflowY: 'auto' }}>
        <AnimatePresence>
          {profiles.map(p => (
            <motion.div key={p.id} initial={{ opacity: 0 }} animate={{ opacity: 1 }} style={{ padding: '16px', borderRadius: '12px', border: `1px solid ${themeStyles.border}`, background: 'rgba(128, 128, 128, 0.05)' }}>
              <div style={{ fontWeight: 'bold' }}>{p.id}</div>
              <div style={{ fontSize: '12px', opacity: 0.8 }}>{p.target} | {p.state} | Proxy: {p.proxy}</div>
              <div style={{ marginTop: '10px', display: 'flex', gap: '5px' }}>
                <button onClick={() => handleAction('launch', p.id)} style={{ fontSize: '10px', padding: '4px', cursor: 'pointer' }}>Launch</button>
                <button onClick={() => handleAction('pause', p.id)} style={{ fontSize: '10px', padding: '4px', cursor: 'pointer' }}>Pause</button>
                <button onClick={() => handleAction('logs', p.id)} style={{ fontSize: '10px', padding: '4px', cursor: 'pointer' }}>Logs</button>
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>

      <div ref={terminalRef} style={{ height: '150px', backgroundColor: '#000', color: '#10b981', padding: '10px', borderRadius: '8px', fontFamily: 'var(--mono)', fontSize: '12px', overflowY: 'auto' }}>
        {logs.map((log, i) => <div key={i}>{log}</div>)}
      </div>
    </div>
  );
}
