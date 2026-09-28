import React, { useState, useRef, useEffect, useMemo } from 'react';
import { Send, User, Trash2, ExternalLink, ChevronDown, AlertTriangle } from 'lucide-react';
import { useOrg } from '../contexts/OrgContext';
import { config } from '../config';
import AgentActionProposalCard from '../components/agent-actions/AgentActionProposalCard';
import { cancelAgentAction, confirmAgentAction } from '../services/agentActionsApi';
import type {
  AgentActionProposalMessageState,
  AgentActionProposal,
} from '../types/agent-actions.types';

interface Citation {
  // Shape the backend actually streams (meeting_rag _sources_from_history):
  // deduped meeting briefs keyed by session_id, with a human title + date.
  session_id?: string;
  title?: string;
  created_at?: string;
  score?: number;
  // Legacy/fallback fields tolerated if an older agent path ever sends them.
  meeting_id?: string;
  snippet?: string;
}

interface Message {
  role: 'user' | 'assistant';
  content: string;
  citations?: Citation[];
  metadata?: {
    pending_confirmation?: AgentActionProposalMessageState;
    [key: string]: any;
  };
  agent_id?: string;
}

interface AgentDescriptor {
  id: string;
  name: string;
  description: string;
  source: 'local' | 'brigade' | string;
  domain?: string | null;
  capabilities?: string[] | null;
  requires_role?: string | null;
  icon?: string | null;
  streaming?: boolean;
}

const DEFAULT_AGENT_ID = 'meeting-rag';

const SOURCE_LABEL: Record<string, string> = {
  local: 'Meeting Ops',
  brigade: 'Brigade Agents',
};

function sourceGroupLabel(source: string): string {
  return SOURCE_LABEL[source] || source.charAt(0).toUpperCase() + source.slice(1);
}

export default function RAGChatPanel() {
  const { activeOrganization } = useOrg();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [streaming, setStreaming] = useState('');
  const [agents, setAgents] = useState<AgentDescriptor[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string>(DEFAULT_AGENT_ID);
  const [agentWarnings, setAgentWarnings] = useState<string[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [busyProposalToken, setBusyProposalToken] = useState<string | null>(null);
  const chatEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streaming]);

  useEffect(() => {
    if (activeOrganization) {
      loadHistory();
      loadAgents();
    }
  }, [activeOrganization?.slug]);

  const getHeaders = () => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (activeOrganization?.slug) {
      headers['X-MeetingOps-Org'] = activeOrganization.slug;
    }
    return headers;
  };

  const loadAgents = async () => {
    const localFallback: AgentDescriptor = {
      id: 'meeting-rag',
      name: 'Meeting RAG',
      description: "Searches and synthesizes across this org's meetings",
      source: 'local',
      icon: '🎙️',
    };
    try {
      const res = await fetch(`${config.apiBaseUrl}/api/agents/available`, {
        headers: getHeaders(),
      });
      if (res.ok) {
        const data = await res.json();
        const list: AgentDescriptor[] = Array.isArray(data?.agents) ? data.agents : [];
        if (list.length === 0) {
          setAgents([localFallback]);
        } else {
          setAgents(list);
        }
        setAgentWarnings(Array.isArray(data?.warnings) ? data.warnings : []);
      } else {
        setAgents([localFallback]);
        setAgentWarnings([`Agent registry returned HTTP ${res.status}; showing local agents only.`]);
      }
    } catch (e) {
      setAgents([localFallback]);
      setAgentWarnings(['Agent registry unreachable; showing local agents only.']);
    }
  };

  const loadHistory = async () => {
    try {
      const res = await fetch(`${config.apiBaseUrl}/api/rag/chat/history`, {
        headers: getHeaders(),
      });
      if (res.ok) {
        const data = await res.json();
        if (Array.isArray(data)) {
          setMessages(data.map((m: any) => ({
            role: m.role as 'user' | 'assistant',
            content: m.content,
          })));
        }
      }
    } catch { /* skip */ }
  };

  const groupedAgents = useMemo(() => {
    const groups: Record<string, AgentDescriptor[]> = {};
    for (const a of agents) {
      const key = a.source || 'other';
      if (!groups[key]) groups[key] = [];
      groups[key].push(a);
    }
    return groups;
  }, [agents]);

  const selectedAgent = useMemo(
    () => agents.find((a) => a.id === selectedAgentId) || null,
    [agents, selectedAgentId]
  );

  const updateProposalMessage = (
    token: string,
    patch: Partial<AgentActionProposalMessageState>
  ) => {
    setMessages((prev) =>
      prev.map((msg) => {
        const pending = msg.metadata?.pending_confirmation;
        if (!pending || pending.proposal.confirmation_token !== token) {
          return msg;
        }
        return {
          ...msg,
          metadata: {
            ...(msg.metadata || {}),
            pending_confirmation: {
              ...pending,
              ...patch,
            },
          },
        };
      })
    );
  };

  const handleProposalConfirm = async (token: string, typedConfirmation?: string) => {
    if (busyProposalToken) return;
    setBusyProposalToken(token);
    try {
      const result = await confirmAgentAction(token, getHeaders(), typedConfirmation);
      updateProposalMessage(token, {
        state: 'applied',
        result: result.result,
      });
    } catch (error) {
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: `Error confirming action: ${(error as Error).message}`,
        },
      ]);
    } finally {
      setBusyProposalToken(null);
    }
  };

  const handleProposalCancel = async (token: string) => {
    if (busyProposalToken) return;
    setBusyProposalToken(token);
    try {
      await cancelAgentAction(token, getHeaders());
      updateProposalMessage(token, {
        state: 'cancelled',
      });
    } catch (error) {
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: `Error cancelling action: ${(error as Error).message}`,
        },
      ]);
    } finally {
      setBusyProposalToken(null);
    }
  };

  const sendMessage = async () => {
    if (!input.trim() || loading) return;
    const question = input.trim();
    setInput('');
    const newUserMsg: Message = { role: 'user', content: question };
    setMessages((prev) => [...prev, newUserMsg]);
    setLoading(true);
    setStreaming('');

    const isLocalRag = selectedAgentId === 'meeting-rag';
    const messagesForRequest = [...messages, newUserMsg].map((m) => ({
      role: m.role,
      content: m.content,
    }));

    try {
      const res = await fetch(`${config.apiBaseUrl}/api/agents/chat`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({
          agent_id: selectedAgentId,
          messages: messagesForRequest,
          stream: true,
          scope: isLocalRag ? {} : undefined,
        }),
      });

      if (!res.ok) {
        setMessages((prev) => [
          ...prev,
          { role: 'assistant', content: `Error: agent dispatch returned HTTP ${res.status}.` },
        ]);
        setLoading(false);
        return;
      }

      const reader = res.body?.getReader();
      if (!reader) {
        setLoading(false);
        return;
      }

      const decoder = new TextDecoder();
      let fullContent = '';
      let finalCitations: Citation[] = [];
      let finalMetadata: any = {};
      let finalPendingConfirmation: AgentActionProposalMessageState | null = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value, { stream: true });
        const lines = chunk.split('\n');
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6));
              if (data.token) {
                fullContent += data.token;
                setStreaming(fullContent);
              } else if (data.event === 'needs_confirmation' && data.proposal) {
                finalPendingConfirmation = {
                  proposal: data.proposal as AgentActionProposal,
                  state: 'pending',
                };
                finalMetadata = {
                  ...finalMetadata,
                  pending_confirmation: finalPendingConfirmation,
                };
                if (!fullContent) {
                  fullContent = 'Action proposal requires confirmation.';
                  setStreaming(fullContent);
                }
              } else if (data.done) {
                finalCitations = data.sources || [];
                finalMetadata = {
                  ...(data.metadata || {}),
                  ...(finalMetadata || {}),
                };
                if (finalPendingConfirmation) {
                  finalMetadata.pending_confirmation = finalPendingConfirmation;
                }
              } else if (data.error) {
                fullContent += `\n[Error: ${data.error}]`;
              }
            } catch { /* skip malformed JSON */ }
          }
        }
      }

      setStreaming('');
      setMessages((prev) => [...prev, {
        role: 'assistant',
        content: fullContent,
        citations: finalCitations,
        metadata: finalMetadata,
        agent_id: selectedAgentId,
      }]);
    } catch (e) {
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: 'Error: Failed to connect to agent dispatcher.' },
      ]);
    }
    setLoading(false);
  };

  const clearHistory = async () => {
    try {
      await fetch(`${config.apiBaseUrl}/api/rag/chat/history`, {
        method: 'DELETE',
        headers: getHeaders(),
      });
    } catch { /* skip */ }
    setMessages([]);
    setStreaming('');
  };

  const isBrigade = (source: string) => source === 'brigade';

  return (
    <div className="flex flex-col h-full bg-zinc-950">
      <div className="flex flex-col gap-2 p-3 border-b border-zinc-800">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <img src="/brand/meeting-ops-mark.png?v=3" alt="Meeting-Ops agent" className="w-7 h-7 rounded-full object-cover ring-1 ring-gold-500/40" />
            <span className="text-sm font-medium text-zinc-100">Meeting-Ops Agent</span>
          </div>
          <button
            onClick={clearHistory}
            className="p-2 rounded-lg text-zinc-500 hover:text-red-400 hover:bg-zinc-800 transition-colors"
            title="Clear history"
            aria-label="Clear chat history"
          >
            <Trash2 className="w-4 h-4" />
          </button>
        </div>

        {/* Agent picker */}
        <div className="relative">
          <button
            type="button"
            onClick={() => setPickerOpen((v) => !v)}
            data-testid="agent-picker-button"
            aria-haspopup="listbox"
            aria-expanded={pickerOpen}
            className="w-full flex items-center justify-between gap-2 px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-700 text-left text-sm text-zinc-100 hover:border-fuchsia-500 transition-colors"
          >
            <span className="flex items-center gap-2 truncate">
              <span className="text-base leading-none">{selectedAgent?.icon || '🤖'}</span>
              <span className="truncate">{selectedAgent?.name || 'Select an agent'}</span>
              {selectedAgent && isBrigade(selectedAgent.source) && (
                <span className="ml-2 px-2 py-0.5 text-[10px] rounded bg-purple-700/50 text-purple-200">
                  Powered by Brigade
                </span>
              )}
            </span>
            <ChevronDown className="w-4 h-4 text-zinc-400 flex-shrink-0" />
          </button>
          {pickerOpen && (
            <div
              role="listbox"
              data-testid="agent-picker-list"
              className="absolute z-10 mt-1 w-full max-h-72 overflow-y-auto rounded-lg bg-zinc-900 border border-zinc-700 shadow-xl"
            >
              {Object.entries(groupedAgents).map(([source, list]) => (
                <div key={source}>
                  <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider text-zinc-500 bg-zinc-950">
                    {sourceGroupLabel(source)}
                  </div>
                  {list.map((agent) => (
                    <button
                      key={agent.id}
                      type="button"
                      role="option"
                      aria-selected={agent.id === selectedAgentId}
                      data-testid={`agent-option-${agent.id}`}
                      onClick={() => {
                        setSelectedAgentId(agent.id);
                        setPickerOpen(false);
                      }}
                      title={agent.description}
                      className={`w-full text-left px-3 py-2 hover:bg-zinc-800 transition-colors flex items-start gap-2 ${
                        agent.id === selectedAgentId ? 'bg-zinc-800' : ''
                      }`}
                    >
                      <span className="text-base leading-none mt-0.5">{agent.icon || '🤖'}</span>
                      <span className="flex-1 min-w-0">
                        <span className="flex items-center gap-2">
                          <span className="text-sm text-zinc-100 truncate">{agent.name}</span>
                          {isBrigade(agent.source) && (
                            <span className="px-1.5 py-0.5 text-[9px] rounded bg-purple-700/50 text-purple-200 flex-shrink-0">
                              Brigade
                            </span>
                          )}
                        </span>
                        <span className="block text-xs text-zinc-500 line-clamp-2">{agent.description}</span>
                      </span>
                    </button>
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>

        {agentWarnings.length > 0 && (
          <div
            data-testid="agent-warnings"
            className="flex items-start gap-2 px-2 py-1.5 rounded bg-yellow-900/30 border border-yellow-800/50 text-xs text-yellow-200"
          >
            <AlertTriangle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
            <span>{agentWarnings[0]}</span>
          </div>
        )}

        <div className="text-right">
          <a
            href="#/admin/agents"
            className="text-xs text-fuchsia-400 hover:text-fuchsia-300 transition-colors inline-flex items-center gap-1"
          >
            Manage local agents <ExternalLink className="w-3 h-3" />
          </a>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {messages.length === 0 && (
          <div className="text-center text-zinc-500 py-8">
            <img src="/brand/meeting-ops-mark.png?v=3" alt="" className="w-16 h-16 mx-auto mb-3 rounded-full object-cover opacity-90 ring-1 ring-gold-500/30" />
            <p>Ask questions about your meetings across time.</p>
            <p className="text-xs mt-1">E.g. "What were the key decisions from last month?"</p>
          </div>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={`flex gap-3 ${msg.role === 'user' ? 'justify-end' : ''}`}>
            {msg.role === 'assistant' && (
              <img src="/brand/meeting-ops-mark.png?v=3" alt="" className="w-6 h-6 mt-1 rounded-full object-cover ring-1 ring-gold-500/30 flex-shrink-0" />
            )}
            <div className={`max-w-[80%] rounded-lg px-3 py-2 ${
              msg.role === 'user'
                ? 'bg-fuchsia-600 text-white'
                : 'bg-zinc-800 text-zinc-100'
            }`}>
              <p className="text-sm whitespace-pre-wrap">{msg.content}</p>
              {msg.citations && msg.citations.length > 0 && (
                <div className="mt-2 pt-2 border-t border-zinc-700">
                  <p className="text-xs text-zinc-400 mb-1">Sources:</p>
                  {msg.citations.map((c, j) => {
                    const sid = c.session_id || c.meeting_id;
                    const label = c.title || 'Untitled meeting';
                    const dateStr = c.created_at
                      ? new Date(c.created_at).toLocaleDateString()
                      : '';
                    return (
                      <div key={j} className="mb-1.5 last:mb-0">
                        <a
                          href={sid ? `#/sessions/${sid}` : undefined}
                          className="flex items-center gap-1 text-xs text-fuchsia-400 hover:text-fuchsia-300 transition-colors"
                        >
                          <ExternalLink className="w-3 h-3 flex-shrink-0" />
                          <span className="truncate">
                            {label}{dateStr && ` · ${dateStr}`}
                          </span>
                        </a>
                        {c.snippet && (
                          <p className="ml-4 text-[11px] text-zinc-500 line-clamp-2 leading-snug">
                            {c.snippet}
                          </p>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
              {msg.metadata?.pending_confirmation && msg.role === 'assistant' && (
                <AgentActionProposalCard
                  state={msg.metadata.pending_confirmation}
                  busy={busyProposalToken === msg.metadata.pending_confirmation.proposal.confirmation_token}
                  onConfirm={handleProposalConfirm}
                  onCancel={handleProposalCancel}
                />
              )}
            </div>
            {msg.role === 'user' && (
              <User className="w-5 h-5 mt-1 text-zinc-400 flex-shrink-0" />
            )}
          </div>
        ))}
        {streaming && (
          <div className="flex gap-3">
            <img src="/brand/meeting-ops-mark.png?v=3" alt="" className="w-6 h-6 mt-1 rounded-full object-cover ring-1 ring-gold-500/30 flex-shrink-0" />
            <div className="max-w-[80%] rounded-lg px-3 py-2 bg-zinc-800 text-zinc-100">
              <p className="text-sm whitespace-pre-wrap">{streaming}<span className="animate-pulse">|</span></p>
            </div>
          </div>
        )}
        {loading && !streaming && (
          <div className="flex gap-3">
            <img src="/brand/meeting-ops-mark.png?v=3" alt="" className="w-6 h-6 mt-1 rounded-full object-cover ring-1 ring-gold-500/30 flex-shrink-0" />
            <div className="rounded-lg px-3 py-2 bg-zinc-800 text-zinc-400">
              <span className="text-sm animate-pulse">Thinking...</span>
            </div>
          </div>
        )}
        <div ref={chatEndRef} />
      </div>

      <div className="p-3 border-t border-zinc-800">
        <div className="flex gap-2">
          <input
            type="text"
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && sendMessage()}
            placeholder="Ask a question about your meetings..."
            className="flex-1 bg-zinc-900 border border-zinc-700 rounded-lg px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-fuchsia-500 outline-none"
            disabled={loading}
          />
          <button
            onClick={sendMessage}
            disabled={loading || !input.trim()}
            className="p-2 rounded-lg bg-fuchsia-600 hover:bg-fuchsia-500 disabled:opacity-40 disabled:cursor-not-allowed text-white transition-colors"
          >
            <Send className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
