import React, { useCallback, useRef, useState } from 'react';
import { Loader2, Send, Sparkles, X } from 'lucide-react';
import { toast } from 'react-toastify';
import { config } from '../../config';
import { useOrg } from '../../contexts/OrgContext';

interface AskBarMessage {
  question: string;
  answer: string;
  sources?: Array<{ meeting_id?: string; title?: string; snippet?: string }>;
}

const ASK_UNAVAILABLE_MESSAGE = "Ask isn't enabled on this workspace yet.";

export const AskBar: React.FC = () => {
  const { activeOrganization } = useOrg();
  const [value, setValue] = useState('');
  const [streaming, setStreaming] = useState('');
  const [pending, setPending] = useState(false);
  const [exchange, setExchange] = useState<AskBarMessage | null>(null);
  const [askUnavailable, setAskUnavailable] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const submit = useCallback(async () => {
    const question = value.trim();
    if (!question || pending) return;

    setPending(true);
    setStreaming('');
    setExchange(null);
    setAskUnavailable(false);

    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;

    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = localStorage.getItem('access_token');
    if (token) headers['Authorization'] = `Bearer ${token}`;
    if (activeOrganization?.slug) headers['X-MeetingOps-Org'] = activeOrganization.slug;

    try {
      const res = await fetch(`${config.apiBaseUrl}/api/agents/chat`, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          agent_id: 'meeting-rag',
          messages: [{ role: 'user', content: question }],
          stream: true,
          scope: {},
        }),
        signal: controller.signal,
      });

      if (res.status === 404) {
        setAskUnavailable(true);
        toast.info(ASK_UNAVAILABLE_MESSAGE);
        setPending(false);
        return;
      }
      if (!res.ok) {
        toast.error(`Agent returned HTTP ${res.status}`);
        setPending(false);
        return;
      }

      const reader = res.body?.getReader();
      if (!reader) {
        setPending(false);
        return;
      }
      const decoder = new TextDecoder();
      let fullContent = '';
      let sources: AskBarMessage['sources'] = [];

      while (true) {
        const { done, value: chunk } = await reader.read();
        if (done) break;
        const text = decoder.decode(chunk, { stream: true });
        const lines = text.split('\n');
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const payload = line.slice(6).trim();
          if (!payload) continue;
          try {
            const data = JSON.parse(payload);
            if (typeof data.token === 'string') {
              fullContent += data.token;
              setStreaming(fullContent);
            } else if (data.done) {
              sources = Array.isArray(data.sources) ? data.sources : [];
            } else if (data.error) {
              fullContent += `\n[Error: ${data.error}]`;
            }
          } catch {
            /* skip malformed lines */
          }
        }
      }

      setExchange({ question, answer: fullContent || '(no response)', sources });
      setStreaming('');
      setValue('');
    } catch (err) {
      if ((err as Error).name === 'AbortError') return;
      toast.error('Ask failed: ' + (err instanceof Error ? err.message : String(err)));
    } finally {
      setPending(false);
    }
  }, [activeOrganization?.slug, pending, value]);

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void submit();
    }
  };

  const close = () => {
    setExchange(null);
    setStreaming('');
    setAskUnavailable(false);
  };

  const hasOutput = Boolean(exchange) || Boolean(streaming) || askUnavailable;

  return (
    <div className="relative">
      <div className="flex items-center gap-2 rounded-2xl border border-zinc-700 bg-zinc-900/70 px-4 py-3 shadow-lg shadow-black/20 transition focus-within:border-fuchsia-500/60 focus-within:bg-zinc-900">
        <Sparkles className="h-4 w-4 flex-shrink-0 text-fuchsia-300" />
        <input
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder='Ask anything about your meetings — e.g. "What did we decide about Legacy25 financing?"'
          className="min-w-0 flex-1 bg-transparent text-sm text-zinc-100 placeholder:text-zinc-500 focus:outline-none"
          disabled={pending}
        />
        <button
          type="button"
          onClick={submit}
          disabled={!value.trim() || pending}
          className="inline-flex h-9 items-center gap-1 rounded-lg bg-fuchsia-600 px-3 text-xs font-semibold text-white transition hover:bg-fuchsia-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
          Ask
        </button>
      </div>

      {hasOutput && (
        <div className="mt-3 rounded-xl border border-zinc-800 bg-zinc-900/70 p-4 text-sm text-zinc-200 shadow-xl shadow-black/30">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              {askUnavailable ? (
                <div className="text-zinc-300">{ASK_UNAVAILABLE_MESSAGE}</div>
              ) : (
                <>
                  {exchange?.question && (
                    <div className="text-xs uppercase tracking-wide text-zinc-500">
                      {exchange.question}
                    </div>
                  )}
                  <div className="mt-1 whitespace-pre-wrap leading-relaxed text-zinc-100">
                    {exchange?.answer || streaming}
                    {streaming && pending && (
                      <span className="ml-1 inline-block h-3 w-1 animate-pulse bg-fuchsia-300 align-middle" />
                    )}
                  </div>
                  {exchange?.sources && exchange.sources.length > 0 && (
                    <div className="mt-3 flex flex-wrap gap-1">
                      {exchange.sources.slice(0, 5).map((src, idx) => (
                        <a
                          key={idx}
                          href={src.meeting_id ? `#/sessions/${src.meeting_id}` : '#'}
                          className="inline-flex items-center gap-1 truncate rounded-full border border-zinc-700 bg-zinc-800 px-2 py-0.5 text-[11px] text-zinc-300 transition hover:border-fuchsia-500/40 hover:text-fuchsia-200"
                        >
                          {src.title || 'Untitled meeting'}
                        </a>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
            <button
              type="button"
              onClick={close}
              className="rounded-md p-1 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-200"
              aria-label="Close"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
};
