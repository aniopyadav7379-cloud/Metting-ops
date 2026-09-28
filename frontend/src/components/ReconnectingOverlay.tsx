import React, { useState, useEffect } from 'react';

let reconnectingListeners: Set<() => void> = new Set();
let isReconnecting = false;

export function setReconnectingState(val: boolean) {
  isReconnecting = val;
  for (const fn of reconnectingListeners) {
    fn();
  }
}

export function subscribeReconnecting(fn: () => void): () => void {
  reconnectingListeners.add(fn);
  return () => { reconnectingListeners.delete(fn); };
}

export function ReconnectingOverlay() {
  const [visible, setVisible] = useState(isReconnecting);

  useEffect(() => {
    const unsub = subscribeReconnecting(() => setVisible(isReconnecting));
    return unsub;
  }, []);

  if (!visible) return null;

  return (
    <div
      className="fixed inset-0 z-[9999] flex items-center justify-center"
      style={{
        background: "rgba(0,0,0,0.6)",
        backdropFilter: "blur(2px)",
        animation: "fadeIn 0.3s ease-out",
      }}
    >
      <div className="flex flex-col items-center gap-3 text-zinc-300">
        <div className="w-8 h-8 border-2 border-fuchsia-500 border-t-transparent rounded-full animate-spin" />
        <span className="text-sm font-medium">Reconnecting...</span>
      </div>
    </div>
  );
}
