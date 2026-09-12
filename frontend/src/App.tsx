import { useEffect, useState } from 'react';

export function App() {
  const [status, setStatus] = useState('Connecting to control plane…');

  useEffect(() => {
    const controller = new AbortController();
    void fetch('/api/health', { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error('Control plane unavailable');
        const health: unknown = await response.json();
        if (typeof health !== 'object' || health === null ||
            !('service' in health) || health.service !== 'control-plane' ||
            !('status' in health) || health.status !== 'ok') {
          throw new Error('Unexpected service response');
        }
        if (!controller.signal.aborted) setStatus('Control plane connected');
      })
      .catch(() => { if (!controller.signal.aborted) setStatus('Control plane unavailable'); });
    return () => controller.abort();
  }, []);

  return (
    <main>
      <p className="eyebrow">Persistent agent organizations</p>
      <h1>Fesnyng</h1>
      <p className="intro">A shared home for your agent organization.</p>
      <section aria-label="Connection status">
        <span className="status-icon" aria-hidden="true">◉</span>
        <div><p role="status">{status}</p><p className="detail">Application foundation · Organization workspace in development</p></div>
      </section>
    </main>
  );
}
