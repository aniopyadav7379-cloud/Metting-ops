import React, { useState, useEffect } from 'react';
import {
  Bot, Plus, Upload, Download, Settings, Zap, Brain,
  Shield, Star, Play, Edit, Trash2, Copy, Package,
  Filter, Search, ChevronRight, AlertCircle, CheckCircle,
  Cpu, Timer, Database, Layers, Gauge, FileJson,
  MessageSquare, FileText, Briefcase, Sparkles
} from 'lucide-react';
import { config } from '../../config';
import { AgentEditor } from './AgentEditor';
import { showConfirm } from '../../utils/notifications';

// Define types directly to avoid import issues
interface Agent {
  id: string;
  name: string;
  slug: string;
  description: string;
  icon: string;
  agent_type: AgentType;
  purpose: string;
  is_core: boolean;
  is_active: boolean;
  is_default_live: boolean;
  is_default_final: boolean;
  provider_type: string;
  model_name: string;
  model_endpoint?: string;
  model_settings: ModelSettings;
  system_prompt: string;
  output_format: string;
  output_template?: any;
  progressive_config?: any;
  chat_config?: any;
  permission_level: string;
  performance_metrics?: {
    avg_tokens_per_sec: number;
    model_size: string;
    context_window: string;
  };
  created_at?: string;
  updated_at?: string;
  usage_count?: number;
}

enum AgentType {
  CORE_LIVE = "core_live",
  CORE_FINAL = "core_final",
  TASK_TEMPLATE = "task_template",
  CHAT_SINGLE = "chat_single",
  CHAT_CROSS = "chat_cross",
  CHAT_ALL = "chat_all",
  EXPORT = "export",
  ANALYSIS = "analysis"
}

interface ModelSettings {
  temperature: number;
  max_tokens: number;
  top_p: number;
  context_window: number;
}

interface ModelPerformance {
  model: string;
  speed: string;
  tokens_per_sec: number;
  context: string;
  memory: string;
  best_for: string;
}


const ACTIVE_MODEL_ID = 'qwen-3.6-35b-a3b-vision';
const ACTIVE_MODEL_LABEL = 'Qwen 3.6 35B-A3B-Vision';

const MODEL_PERFORMANCE: ModelPerformance[] = [
  {
    model: ACTIVE_MODEL_ID,
    speed: 'via LiteLLM',
    tokens_per_sec: 60,
    context: '32K',
    memory: 'MoE (~3B active)',
    best_for: 'Summaries, chat, insights, sentiment'
  }
];

export const AgentDashboard: React.FC = () => {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [templates, setTemplates] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedAgent, setSelectedAgent] = useState<Agent | null>(null);
  const [showEditor, setShowEditor] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [filterType, setFilterType] = useState<'all' | 'core' | 'custom' | 'templates'>('all');
  const [searchQuery, setSearchQuery] = useState('');
  const [status, setStatus] = useState<{ type: 'success' | 'error' | null; message: string }>({ type: null, message: '' });
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    loadAgents();
    loadTemplates();
  }, []);

  const loadAgents = async () => {
    setLoadError(null);
    try {
      const response = await fetch(`${config.apiUrl}/api/agents/`);
      if (response.ok) {
        const data = await response.json();
        setAgents(data.agents || data || []);
      } else {
        // NEVER synthesize operational state — an admin must not mistake
        // fabricated defaults for the real production config. Surface the
        // error and keep the last known payload (if any).
        setLoadError(`Couldn't load agents (${response.status}).`);
      }
    } catch (error) {
      console.error('Error loading agents:', error);
      setLoadError("Couldn't reach the agents service.");
    } finally {
      setLoading(false);
    }
  };

  const loadTemplates = async () => {
    try {
      const response = await fetch(`${config.apiUrl}/api/agents/templates/`);
      if (response.ok) {
        const data = await response.json();
        setTemplates(data.templates || []);
      }
    } catch (error) {
      console.error('Error loading templates:', error);
    }
  };

  const handleCreateAgent = () => {
    const newAgent: Agent = {
      id: `agent-${Date.now()}`,
      name: 'New Agent',
      slug: 'new-agent',
      description: 'Custom agent',
      icon: '🤖',
      agent_type: AgentType.ANALYSIS,
      purpose: '',
      is_core: false,
      is_active: false,
      is_default_live: false,
      is_default_final: false,
      provider_type: 'llamacpp',
      model_name: 'qwen-3.6-35b-a3b-vision',
      model_settings: {
        temperature: 0.7,
        max_tokens: 1024,
        top_p: 0.9,
        context_window: 32768
      },
      system_prompt: '',
      output_format: 'text',
      permission_level: 'owner'
    };
    setSelectedAgent(newAgent);
    setShowEditor(true);
  };

  const handleEditAgent = (agent: Agent) => {
    setSelectedAgent(agent);
    setShowEditor(true);
  };

  const handleDeleteAgent = async (agent: Agent) => {
    if (agent.is_core) {
      setStatus({ type: 'error', message: 'Core agents cannot be deleted' });
      return;
    }

    if (!(await showConfirm(
      `Delete agent "${agent.name}"? This cannot be undone.`,
      { title: 'Delete agent', confirmLabel: 'Delete' },
    ))) {
      return;
    }

    try {
      const response = await fetch(`${config.apiUrl}/api/agents/${agent.slug}`, {
        method: 'DELETE'
      });
      if (response.ok) {
        setAgents(prev => prev.filter(a => a.id !== agent.id));
        setStatus({ type: 'success', message: `Agent "${agent.name}" deleted` });
      }
    } catch (error) {
      setStatus({ type: 'error', message: `Failed to delete agent` });
    }
  };

  const handleExportAgent = async (agent: Agent) => {
    try {
      const response = await fetch(`${config.apiUrl}/api/agents/${agent.slug}/export`);
      if (response.ok) {
        const data = await response.json();
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${agent.slug}.agent.json`;
        a.click();
        URL.revokeObjectURL(url);
        setStatus({ type: 'success', message: `Agent "${agent.name}" exported` });
      }
    } catch (error) {
      setStatus({ type: 'error', message: 'Failed to export agent' });
    }
  };

  const handleImportAgent = async (file: File) => {
    const formData = new FormData();
    formData.append('file', file);

    try {
      const response = await fetch(`${config.apiUrl}/api/agents/import`, {
        method: 'POST',
        body: formData
      });
      if (response.ok) {
        const newAgent = await response.json();
        setAgents(prev => [...prev, newAgent]);
        setStatus({ type: 'success', message: `Agent imported successfully` });
        setShowImport(false);
      }
    } catch (error) {
      setStatus({ type: 'error', message: 'Failed to import agent' });
    }
  };

  const handleSetDefault = async (agent: Agent, type: 'live' | 'final') => {
    try {
      const response = await fetch(`${config.apiUrl}/api/agents/defaults`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [type]: agent.slug })
      });
      if (response.ok) {
        setAgents(prev => prev.map(a => ({
          ...a,
          is_default_live: type === 'live' ? a.id === agent.id : a.is_default_live,
          is_default_final: type === 'final' ? a.id === agent.id : a.is_default_final
        })));
        setStatus({ type: 'success', message: `${agent.name} set as default ${type} agent` });
      }
    } catch (error) {
      setStatus({ type: 'error', message: `Failed to set default agent` });
    }
  };

  const getAgentTypeIcon = (type: AgentType) => {
    switch (type) {
      case AgentType.CORE_LIVE: return <Zap className="w-4 h-4 text-yellow-400" />;
      case AgentType.CORE_FINAL: return <Brain className="w-4 h-4 text-blue-400" />;
      case AgentType.TASK_TEMPLATE: return <FileText className="w-4 h-4 text-green-400" />;
      case AgentType.CHAT_SINGLE: return <MessageSquare className="w-4 h-4 text-purple-400" />;
      case AgentType.CHAT_CROSS: return <Layers className="w-4 h-4 text-indigo-400" />;
      case AgentType.CHAT_ALL: return <Database className="w-4 h-4 text-cyan-400" />;
      case AgentType.EXPORT: return <FileJson className="w-4 h-4 text-orange-400" />;
      case AgentType.ANALYSIS: return <Sparkles className="w-4 h-4 text-pink-400" />;
      default: return <Bot className="w-4 h-4 text-gray-400" />;
    }
  };

  const getAgentTypeLabel = (type: AgentType) => {
    switch (type) {
      case AgentType.CORE_LIVE: return 'Core Live';
      case AgentType.CORE_FINAL: return 'Core Final';
      case AgentType.TASK_TEMPLATE: return 'Task Template';
      case AgentType.CHAT_SINGLE: return 'Chat Single';
      case AgentType.CHAT_CROSS: return 'Chat Cross';
      case AgentType.CHAT_ALL: return 'Chat All';
      case AgentType.EXPORT: return 'Export';
      case AgentType.ANALYSIS: return 'Analysis';
      default: return 'Unknown';
    }
  };

  const filteredAgents = agents.filter(agent => {
    const matchesFilter = 
      filterType === 'all' ||
      (filterType === 'core' && agent.is_core) ||
      (filterType === 'custom' && !agent.is_core);
    
    const matchesSearch = !searchQuery || 
      agent.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      agent.description.toLowerCase().includes(searchQuery.toLowerCase());
    
    return matchesFilter && matchesSearch;
  });

  if (loading) {
    return (
      <div className="min-h-screen bg-gradient-to-b from-zinc-950 to-black text-white p-6">
        <div className="flex items-center justify-center h-64">
          <div className="text-zinc-400">Loading agents...</div>
        </div>
      </div>
    );
  }

  if (loadError && agents.length === 0) {
    return (
      <div className="min-h-screen bg-gradient-to-b from-zinc-950 to-black text-white p-6">
        <div className="max-w-7xl mx-auto flex flex-col items-center justify-center h-64 gap-4">
          <div className="text-red-300 text-center">{loadError}</div>
          <button
            onClick={loadAgents}
            className="px-4 py-2 bg-purple-600 hover:bg-purple-700 text-white rounded-lg transition-colors"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gradient-to-b from-zinc-950 to-black text-white p-6">
      <div className="max-w-7xl mx-auto">
        {/* Stale-data banner: real config is loaded but a refresh failed. */}
        {loadError && agents.length > 0 && (
          <div className="mb-4 px-4 py-2 rounded-lg bg-amber-900/40 border border-amber-700/50 text-amber-200 text-sm flex items-center justify-between">
            <span>{loadError} Showing the last loaded configuration.</span>
            <button onClick={loadAgents} className="underline hover:text-amber-100">Retry</button>
          </div>
        )}
        {/* Header */}
        <div className="mb-8">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-3xl font-bold text-white mb-2 flex items-center gap-3">
                <Bot className="w-8 h-8 text-purple-400" />
                Agent Management
              </h1>
              <p className="text-zinc-400">
                Multi-tier agent system powered by Qwen 3.6 35B-A3B-Vision (MoE) via LiteLLM
              </p>
            </div>
            
            <div className="flex gap-3">
              <button
                onClick={() => setShowImport(true)}
                className="flex items-center gap-2 px-4 py-2 bg-zinc-800 hover:bg-zinc-700 text-white rounded-lg transition-colors"
              >
                <Upload className="w-4 h-4" />
                Import
              </button>
              
              <button
                onClick={handleCreateAgent}
                className="flex items-center gap-2 px-6 py-2 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 text-white rounded-lg font-medium transition-colors"
              >
                <Plus className="w-5 h-5" />
                Create Agent
              </button>
            </div>
          </div>
        </div>

        {/* Status Message */}
        {status.type && (
          <div className={`mb-6 p-4 rounded-lg flex items-center gap-2 ${
            status.type === 'error' 
              ? 'bg-red-900/50 border border-red-500/30 text-red-200' 
              : 'bg-green-900/50 border border-green-500/30 text-green-200'
          }`}>
            {status.type === 'error' ? <AlertCircle className="w-5 h-5" /> : <CheckCircle className="w-5 h-5" />}
            {status.message}
          </div>
        )}

        {/* Model Performance Cards */}
        <div className="mb-8">
          <h2 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
            <Gauge className="w-5 h-5 text-yellow-400" />
            Model Performance Metrics
          </h2>
          
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
            {MODEL_PERFORMANCE.map((model) => (
              <div 
                key={model.model}
                className={`bg-zinc-900/70 rounded-lg border p-4 ${
                  model.model === ACTIVE_MODEL_ID
                    ? 'border-green-500/50 bg-green-900/10'
                    : 'border-zinc-800'
                }`}
              >
                <div className="flex items-center justify-between mb-2">
                  <span className="font-medium text-white">
                    {model.model === ACTIVE_MODEL_ID ? ACTIVE_MODEL_LABEL : model.model}
                  </span>
                  {model.model === ACTIVE_MODEL_ID && (
                    <span className="px-2 py-1 text-xs bg-green-500/20 text-green-400 rounded-full">
                      ACTIVE
                    </span>
                  )}
                </div>
                
                <div className="space-y-1 text-sm">
                  <div className="flex items-center justify-between">
                    <span className="text-zinc-400">Speed</span>
                    <span className={`font-medium ${
                      model.tokens_per_sec > 100 ? 'text-yellow-400' : 
                      model.tokens_per_sec > 25 ? 'text-green-400' : 'text-blue-400'
                    }`}>
                      {model.speed}
                    </span>
                  </div>
                  
                  <div className="flex items-center justify-between">
                    <span className="text-zinc-400">Context</span>
                    <span className="text-white">{model.context}</span>
                  </div>
                  
                  <div className="flex items-center justify-between">
                    <span className="text-zinc-400">Memory</span>
                    <span className="text-white">{model.memory}</span>
                  </div>
                </div>
                
                <div className="mt-3 pt-3 border-t border-zinc-800">
                  <span className="text-xs text-zinc-500">{model.best_for}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Filters */}
        <div className="flex items-center gap-4 mb-6">
          <div className="flex gap-2">
            {['all', 'core', 'custom', 'templates'].map(type => (
              <button
                key={type}
                onClick={() => setFilterType(type as any)}
                className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                  filterType === type
                    ? 'bg-zinc-800 text-white'
                    : 'text-zinc-400 hover:text-white hover:bg-zinc-800/50'
                }`}
              >
                {type.charAt(0).toUpperCase() + type.slice(1)}
              </button>
            ))}
          </div>
          
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-zinc-400" />
            <input
              type="text"
              placeholder="Search agents..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-10 pr-4 py-2 bg-zinc-900 border border-zinc-800 rounded-lg text-white placeholder-zinc-500 focus:outline-none focus:border-zinc-700"
            />
          </div>
        </div>

        {/* Agents Grid */}
        <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-6">
          {filteredAgents.map(agent => (
            <div
              key={agent.id}
              className={`bg-zinc-900/70 rounded-xl border overflow-hidden transition-all hover:border-zinc-700 ${
                agent.is_core ? 'border-purple-500/30' : 'border-zinc-800'
              }`}
            >
              {/* Agent Header */}
              <div className="p-5 border-b border-zinc-800">
                <div className="flex items-start justify-between mb-3">
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-2xl">{agent.icon || '🤖'}</span>
                      <h3 className="text-lg font-semibold text-white">{agent.name}</h3>
                      {agent.is_core && (
                        <Shield className="w-4 h-4 text-purple-400" aria-label="Core Agent" />
                      )}
                    </div>
                    
                    <p className="text-sm text-zinc-400 line-clamp-2">{agent.description}</p>
                  </div>
                  
                  <div className={`w-2 h-2 rounded-full ${
                    agent.is_active ? 'bg-green-400' : 'bg-zinc-600'
                  }`} />
                </div>
                
                {/* Agent Tags */}
                <div className="flex items-center gap-2 mb-3">
                  <span className="flex items-center gap-1 px-2 py-1 text-xs rounded-full bg-zinc-800 text-zinc-300">
                    {getAgentTypeIcon(agent.agent_type)}
                    {getAgentTypeLabel(agent.agent_type)}
                  </span>
                  
                  {agent.is_default_live && (
                    <span className="px-2 py-1 text-xs rounded-full bg-yellow-500/20 text-yellow-400 border border-yellow-500/30">
                      Default Live
                    </span>
                  )}
                  
                  {agent.is_default_final && (
                    <span className="px-2 py-1 text-xs rounded-full bg-blue-500/20 text-blue-400 border border-blue-500/30">
                      Default Final
                    </span>
                  )}
                </div>
                
                {/* Model Info */}
                <div className="flex items-center gap-4 text-xs text-zinc-500">
                  <div className="flex items-center gap-1">
                    <Cpu className="w-3 h-3" />
                    {agent.model_name}
                  </div>
                  
                  {agent.performance_metrics && (
                    <div className="flex items-center gap-1">
                      <Timer className="w-3 h-3" />
                      {agent.performance_metrics.avg_tokens_per_sec} t/s
                    </div>
                  )}
                  
                  {agent.usage_count !== undefined && (
                    <div className="flex items-center gap-1">
                      <Play className="w-3 h-3" />
                      {agent.usage_count} uses
                    </div>
                  )}
                </div>
              </div>

              {/* Agent Actions */}
              <div className="p-4 bg-zinc-800/30 flex items-center gap-2">
                <button
                  onClick={() => handleEditAgent(agent)}
                  className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-zinc-800 hover:bg-zinc-700 rounded-lg text-sm text-zinc-200 transition-colors"
                >
                  <Edit className="w-4 h-4" />
                  {agent.is_core ? 'Configure' : 'Edit'}
                </button>
                
                {!agent.is_core && (
                  <>
                    <button
                      onClick={() => handleExportAgent(agent)}
                      className="p-2 bg-zinc-800 hover:bg-zinc-700 rounded-lg text-zinc-200 transition-colors"
                      title="Export agent"
                    >
                      <Download className="w-4 h-4" />
                    </button>
                    
                    <button
                      onClick={() => handleDeleteAgent(agent)}
                      className="p-2 bg-zinc-800 hover:bg-red-900/50 rounded-lg text-zinc-200 hover:text-red-400 transition-colors"
                      title="Delete agent"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </>
                )}
                
                {!agent.is_default_live && agent.agent_type === AgentType.CORE_LIVE && (
                  <button
                    onClick={() => handleSetDefault(agent, 'live')}
                    className="p-2 bg-zinc-800 hover:bg-yellow-900/50 rounded-lg text-zinc-200 hover:text-yellow-400 transition-colors"
                    title="Set as default live agent"
                  >
                    <Star className="w-4 h-4" />
                  </button>
                )}
                
                {!agent.is_default_final && agent.agent_type === AgentType.CORE_FINAL && (
                  <button
                    onClick={() => handleSetDefault(agent, 'final')}
                    className="p-2 bg-zinc-800 hover:bg-blue-900/50 rounded-lg text-zinc-200 hover:text-blue-400 transition-colors"
                    title="Set as default final agent"
                  >
                    <Star className="w-4 h-4" />
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>

        {/* Templates Section */}
        {filterType === 'templates' && templates.length > 0 && (
          <div className="mt-8">
            <h2 className="text-xl font-semibold text-white mb-4">Available Templates</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {templates.map(template => (
                <div
                  key={template.id}
                  className="bg-zinc-900/70 rounded-lg border border-zinc-800 p-4 hover:border-zinc-700 transition-colors"
                >
                  <h3 className="font-medium text-white mb-2">{template.name}</h3>
                  <p className="text-sm text-zinc-400 mb-4">{template.description}</p>
                  <button
                    onClick={() => {
                      const newAgent: Agent = {
                        id: `agent-${Date.now()}`,
                        name: template.name || 'New Agent',
                        slug: (template.name || 'new-agent').toLowerCase().replace(/[^a-z0-9]+/g, '-'),
                        description: template.description || '',
                        icon: '🤖',
                        agent_type: AgentType.ANALYSIS,
                        purpose: template.description || '',
                        is_core: false,
                        is_active: false,
                        is_default_live: false,
                        is_default_final: false,
                        provider_type: 'llamacpp',
                        model_name: template.model_name || 'qwen-3.6-35b-a3b-vision',
                        model_settings: template.model_settings || {
                          temperature: 0.7,
                          max_tokens: 1024,
                          top_p: 0.9,
                          context_window: 8192
                        },
                        system_prompt: template.system_prompt || '',
                        output_format: template.output_format || 'text',
                        permission_level: 'owner'
                      };
                      setSelectedAgent(newAgent);
                      setShowEditor(true);
                      setStatus({ type: 'success', message: `Template "${template.name}" applied — customize and save` });
                    }}
                    className="w-full px-3 py-2 bg-zinc-800 hover:bg-zinc-700 rounded-lg text-sm text-white transition-colors"
                  >
                    Use Template
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Editor Modal */}
        {showEditor && selectedAgent && (
          <AgentEditor
            agent={selectedAgent}
            onSave={(agent) => {
              setAgents(prev => {
                const exists = prev.find(a => a.id === agent.id);
                if (exists) {
                  return prev.map(a => a.id === agent.id ? agent : a);
                } else {
                  return [...prev, agent];
                }
              });
              setShowEditor(false);
              setSelectedAgent(null);
              setStatus({ type: 'success', message: `Agent "${agent.name}" saved` });
            }}
            onCancel={() => {
              setShowEditor(false);
              setSelectedAgent(null);
            }}
          />
        )}

        {/* Import Modal */}
        {showImport && (
          <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
            <div className="bg-zinc-900 rounded-xl border border-zinc-800 p-6 max-w-md w-full">
              <h2 className="text-xl font-semibold text-white mb-4">Import Agent</h2>
              
              <div className="border-2 border-dashed border-zinc-700 rounded-lg p-8 text-center">
                <Upload className="w-12 h-12 text-zinc-500 mx-auto mb-4" />
                <p className="text-zinc-400 mb-4">Drop agent file here or click to browse</p>
                <input
                  type="file"
                  accept=".json"
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) handleImportAgent(file);
                  }}
                  className="hidden"
                  id="import-file"
                />
                <label
                  htmlFor="import-file"
                  className="px-4 py-2 bg-zinc-800 hover:bg-zinc-700 text-white rounded-lg cursor-pointer inline-block"
                >
                  Choose File
                </label>
              </div>
              
              <div className="flex gap-4 mt-6">
                <button
                  onClick={() => setShowImport(false)}
                  className="flex-1 px-4 py-2 bg-zinc-800 hover:bg-zinc-700 text-white rounded-lg transition-colors"
                >
                  Cancel
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};