import React, { useState, useEffect } from 'react';
import {
  X, Save, Settings, Database, Cpu, Shield, FileJson,
  Zap, Brain, MessageSquare, FileText, Sparkles,
  AlertCircle, Info, Code, Globe, Key, Users
} from 'lucide-react';
import { config } from '../../config';

// Define types directly in this file to avoid import issues
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
  model_settings: {
    temperature: number;
    max_tokens: number;
    top_p: number;
    context_window: number;
  };
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

interface AgentEditorProps {
  agent: Agent;
  onSave: (agent: Agent) => void;
  onCancel: () => void;
}

const PROVIDERS = [
  { value: 'llamacpp', label: 'LlamaCPP', icon: '🦙' },
  { value: 'openai', label: 'OpenAI', icon: '🌐' },
  { value: 'anthropic', label: 'Anthropic', icon: '🤖' }
];

const OUTPUT_FORMATS = [
  { value: 'text', label: 'Plain Text' },
  { value: 'json', label: 'JSON' },
  { value: 'markdown', label: 'Markdown' }
];

const PERMISSION_LEVELS = [
  { value: 'public', label: 'Public - Everyone can use' },
  { value: 'team', label: 'Team - Team members only' },
  { value: 'owner', label: 'Owner - Only creator can use' }
];

const AGENT_TYPE_OPTIONS = [
  { value: AgentType.CORE_LIVE, label: 'Core Live', icon: <Zap className="w-4 h-4" />, disabled: true },
  { value: AgentType.CORE_FINAL, label: 'Core Final', icon: <Brain className="w-4 h-4" />, disabled: true },
  { value: AgentType.TASK_TEMPLATE, label: 'Task Template', icon: <FileText className="w-4 h-4" /> },
  { value: AgentType.CHAT_SINGLE, label: 'Chat Single Meeting', icon: <MessageSquare className="w-4 h-4" /> },
  { value: AgentType.CHAT_CROSS, label: 'Chat Cross Meeting', icon: <MessageSquare className="w-4 h-4" /> },
  { value: AgentType.CHAT_ALL, label: 'Chat All Meetings', icon: <Database className="w-4 h-4" /> },
  { value: AgentType.EXPORT, label: 'Export Formatter', icon: <FileJson className="w-4 h-4" /> },
  { value: AgentType.ANALYSIS, label: 'Analysis', icon: <Sparkles className="w-4 h-4" /> }
];

const MODEL_PRESETS = {
  'qwen-3.6-35b-a3b-vision': { context: 32768, temp: 0.7, tokens: 2048, top_p: 0.9 }
};

export const AgentEditor: React.FC<AgentEditorProps> = ({ agent, onSave, onCancel }) => {
  const [editedAgent, setEditedAgent] = useState<Agent>({ ...agent });
  const [activeTab, setActiveTab] = useState<'basic' | 'model' | 'prompt' | 'output' | 'permissions'>('basic');
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    // Apply model preset when model changes
    const preset = MODEL_PRESETS[editedAgent.model_name as keyof typeof MODEL_PRESETS];
    if (preset && !agent.is_core) {
      setEditedAgent(prev => ({
        ...prev,
        model_settings: {
          ...prev.model_settings,
          context_window: preset.context,
          temperature: preset.temp,
          max_tokens: preset.tokens,
          top_p: preset.top_p
        }
      }));
    }
  }, [editedAgent.model_name]);

  const updateField = (field: string, value: any) => {
    if (field.includes('.')) {
      const [parent, child] = field.split('.');
      setEditedAgent(prev => ({
        ...prev,
        [parent]: { ...prev[parent as keyof Agent], [child]: value }
      }));
    } else {
      setEditedAgent(prev => ({ ...prev, [field]: value }));
    }
    
    // Clear error for this field
    setErrors(prev => ({ ...prev, [field]: '' }));
  };

  const validateAgent = (): boolean => {
    const newErrors: Record<string, string> = {};
    
    if (!editedAgent.name) newErrors.name = 'Name is required';
    if (!editedAgent.slug) newErrors.slug = 'Slug is required';
    if (!editedAgent.description) newErrors.description = 'Description is required';
    if (!editedAgent.system_prompt) newErrors.system_prompt = 'System prompt is required';
    if (!editedAgent.model_name) newErrors.model_name = 'Model is required';
    
    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  };

  const handleSave = async () => {
    if (!validateAgent()) return;

    setSaving(true);
    setSaveError(null);
    try {
      const endpoint = editedAgent.id.startsWith('agent-')
        ? `${config.apiUrl}/api/agents/`
        : `${config.apiUrl}/api/agents/${editedAgent.slug}`;

      const method = editedAgent.id.startsWith('agent-') ? 'POST' : 'PUT';

      const response = await fetch(endpoint, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(editedAgent)
      });

      if (response.ok) {
        const savedAgent = await response.json();
        onSave(savedAgent);
      } else {
        const errorText = await response.text().catch(() => 'Unknown error');
        setSaveError(`Failed to save agent (${response.status}): ${errorText}`);
      }
    } catch (error) {
      console.error('Error saving agent:', error);
      setSaveError('Failed to save agent: ' + (error as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const generateSlug = (name: string) => {
    return name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  };

  const tabs = [
    { id: 'basic', label: 'Basic Info', icon: Settings },
    { id: 'model', label: 'Model Config', icon: Cpu },
    { id: 'prompt', label: 'System Prompt', icon: Database },
    { id: 'output', label: 'Output', icon: FileJson },
    { id: 'permissions', label: 'Permissions', icon: Shield }
  ];

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-zinc-900 rounded-2xl border border-zinc-800 w-full max-w-5xl max-h-[90vh] overflow-hidden flex flex-col">
        {/* Header */}
        <div className="p-6 border-b border-zinc-800 flex items-center justify-between">
          <div>
            <h2 className="text-2xl font-semibold text-white">
              {editedAgent.id.startsWith('agent-') ? 'Create New Agent' : 'Edit Agent'}
            </h2>
            {editedAgent.is_core && (
              <p className="text-sm text-purple-400 mt-1 flex items-center gap-1">
                <Shield className="w-4 h-4" />
                Core agent - Some settings are protected
              </p>
            )}
          </div>
          
          <button
            onClick={onCancel}
            className="p-2 hover:bg-zinc-800 rounded-lg text-zinc-400 hover:text-white transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Tabs */}
        <div className="flex gap-2 px-6 pt-4 border-b border-zinc-800">
          {tabs.map(tab => {
            const Icon = tab.icon;
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id as any)}
                className={`flex items-center gap-2 px-4 py-2 rounded-t-lg transition-colors ${
                  activeTab === tab.id
                    ? 'bg-zinc-800 text-white border-b-2 border-purple-500'
                    : 'text-zinc-400 hover:text-white hover:bg-zinc-800/50'
                }`}
              >
                <Icon className="w-4 h-4" />
                {tab.label}
              </button>
            );
          })}
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6">
          {activeTab === 'basic' && (
            <div className="space-y-6">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div>
                  <label className="block text-sm font-medium text-zinc-300 mb-2">
                    Agent Name *
                  </label>
                  <input
                    type="text"
                    value={editedAgent.name}
                    onChange={(e) => {
                      updateField('name', e.target.value);
                      if (!editedAgent.is_core) {
                        updateField('slug', generateSlug(e.target.value));
                      }
                    }}
                    disabled={editedAgent.is_core}
                    className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white focus:outline-none focus:border-purple-500 disabled:opacity-50"
                  />
                  {errors.name && <p className="text-red-400 text-xs mt-1">{errors.name}</p>}
                </div>
                
                <div>
                  <label className="block text-sm font-medium text-zinc-300 mb-2">
                    Slug (URL-safe name) *
                  </label>
                  <input
                    type="text"
                    value={editedAgent.slug}
                    onChange={(e) => updateField('slug', e.target.value)}
                    disabled={editedAgent.is_core}
                    className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white focus:outline-none focus:border-purple-500 disabled:opacity-50"
                  />
                  {errors.slug && <p className="text-red-400 text-xs mt-1">{errors.slug}</p>}
                </div>
              </div>
              
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div>
                  <label className="block text-sm font-medium text-zinc-300 mb-2">
                    Icon (Emoji)
                  </label>
                  <input
                    type="text"
                    value={editedAgent.icon}
                    onChange={(e) => updateField('icon', e.target.value)}
                    placeholder="🤖"
                    maxLength={2}
                    className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white text-2xl focus:outline-none focus:border-purple-500"
                  />
                </div>
                
                <div>
                  <label className="block text-sm font-medium text-zinc-300 mb-2">
                    Agent Type
                  </label>
                  <select
                    value={editedAgent.agent_type}
                    onChange={(e) => updateField('agent_type', e.target.value)}
                    disabled={editedAgent.is_core}
                    className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white focus:outline-none focus:border-purple-500 disabled:opacity-50"
                  >
                    {AGENT_TYPE_OPTIONS.map(option => (
                      <option key={option.value} value={option.value} disabled={option.disabled}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              
              <div>
                <label className="block text-sm font-medium text-zinc-300 mb-2">
                  Description *
                </label>
                <textarea
                  value={editedAgent.description}
                  onChange={(e) => updateField('description', e.target.value)}
                  rows={3}
                  className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white focus:outline-none focus:border-purple-500"
                />
                {errors.description && <p className="text-red-400 text-xs mt-1">{errors.description}</p>}
              </div>
              
              <div>
                <label className="block text-sm font-medium text-zinc-300 mb-2">
                  Purpose
                </label>
                <textarea
                  value={editedAgent.purpose}
                  onChange={(e) => updateField('purpose', e.target.value)}
                  rows={2}
                  placeholder="What is this agent designed to do?"
                  className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white focus:outline-none focus:border-purple-500"
                />
              </div>
            </div>
          )}

          {activeTab === 'model' && (
            <div className="space-y-6">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div>
                  <label className="block text-sm font-medium text-zinc-300 mb-2">
                    Provider
                  </label>
                  <select
                    value={editedAgent.provider_type}
                    onChange={(e) => updateField('provider_type', e.target.value)}
                    className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white focus:outline-none focus:border-purple-500"
                  >
                    {PROVIDERS.map(provider => (
                      <option key={provider.value} value={provider.value}>
                        {provider.icon} {provider.label}
                      </option>
                    ))}
                  </select>
                </div>
                
                <div>
                  <label className="block text-sm font-medium text-zinc-300 mb-2">
                    Model *
                  </label>
                  <select
                    value={editedAgent.model_name}
                    onChange={(e) => updateField('model_name', e.target.value)}
                    className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white focus:outline-none focus:border-purple-500"
                  >
                    <option value="">Select Model</option>
                    <option value="qwen-3.6-35b-a3b-vision">Qwen 3.6 35B-A3B-Vision (MoE)</option>
                  </select>
                  {errors.model_name && <p className="text-red-400 text-xs mt-1">{errors.model_name}</p>}
                </div>
              </div>
              
              {editedAgent.provider_type !== 'ollama' && (
                <div>
                  <label className="block text-sm font-medium text-zinc-300 mb-2">
                    Model Endpoint URL
                  </label>
                  <input
                    type="text"
                    value={editedAgent.model_endpoint || ''}
                    onChange={(e) => updateField('model_endpoint', e.target.value)}
                    placeholder="https://api.openai.com/v1"
                    className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white focus:outline-none focus:border-purple-500"
                  />
                </div>
              )}
              
              <div className="space-y-4">
                <h3 className="text-lg font-medium text-white">Model Settings</h3>
                
                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div>
                    <label className="block text-sm font-medium text-zinc-300 mb-2">
                      Temperature: {editedAgent.model_settings.temperature}
                    </label>
                    <input
                      type="range"
                      min="0"
                      max="1"
                      step="0.1"
                      value={editedAgent.model_settings.temperature}
                      onChange={(e) => updateField('model_settings.temperature', parseFloat(e.target.value))}
                      className="w-full h-2 bg-zinc-700 rounded-lg appearance-none cursor-pointer"
                    />
                    <div className="flex justify-between text-xs text-zinc-500 mt-1">
                      <span>Focused (0.0)</span>
                      <span>Creative (1.0)</span>
                    </div>
                  </div>
                  
                  <div>
                    <label className="block text-sm font-medium text-zinc-300 mb-2">
                      Top P: {editedAgent.model_settings.top_p}
                    </label>
                    <input
                      type="range"
                      min="0"
                      max="1"
                      step="0.05"
                      value={editedAgent.model_settings.top_p}
                      onChange={(e) => updateField('model_settings.top_p', parseFloat(e.target.value))}
                      className="w-full h-2 bg-zinc-700 rounded-lg appearance-none cursor-pointer"
                    />
                    <div className="flex justify-between text-xs text-zinc-500 mt-1">
                      <span>Narrow (0.0)</span>
                      <span>Diverse (1.0)</span>
                    </div>
                  </div>
                </div>
                
                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div>
                    <label className="block text-sm font-medium text-zinc-300 mb-2">
                      Max Tokens
                    </label>
                    <input
                      type="number"
                      value={editedAgent.model_settings.max_tokens}
                      onChange={(e) => updateField('model_settings.max_tokens', parseInt(e.target.value))}
                      min="128"
                      max="8192"
                      className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white focus:outline-none focus:border-purple-500"
                    />
                  </div>
                  
                  <div>
                    <label className="block text-sm font-medium text-zinc-300 mb-2">
                      Context Window
                    </label>
                    <select
                      value={editedAgent.model_settings.context_window}
                      onChange={(e) => updateField('model_settings.context_window', parseInt(e.target.value))}
                      className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white focus:outline-none focus:border-purple-500"
                    >
                      <option value="4096">4K tokens</option>
                      <option value="8192">8K tokens</option>
                      <option value="16384">16K tokens</option>
                      <option value="32768">32K tokens</option>
                      <option value="131072">128K tokens</option>
                    </select>
                  </div>
                </div>
              </div>
              
              {/* Model Performance Info */}
              {editedAgent.model_name && MODEL_PRESETS[editedAgent.model_name as keyof typeof MODEL_PRESETS] && (
                <div className="p-4 bg-purple-900/20 border border-purple-500/30 rounded-lg">
                  <h4 className="font-medium text-purple-400 mb-2 flex items-center gap-2">
                    <Info className="w-4 h-4" />
                    Model Performance
                  </h4>
                  <div className="text-sm text-zinc-300">
                    {editedAgent.model_name === 'qwen-3.6-35b-a3b-vision' && (
                      <p>Qwen 3.6 35B-A3B-Vision: Mixture-of-Experts model (~3B active params) with 32K context, served via LiteLLM. Used for summaries, chat, insights, and sentiment analysis. Fast model: gemma-4-e4b for low-latency tasks.</p>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}

          {activeTab === 'prompt' && (
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-zinc-300 mb-2">
                  System Prompt *
                </label>
                <textarea
                  value={editedAgent.system_prompt}
                  onChange={(e) => updateField('system_prompt', e.target.value)}
                  rows={20}
                  placeholder="Define how this agent should behave..."
                  className="w-full px-4 py-3 bg-zinc-800 border border-zinc-700 rounded-lg text-white focus:outline-none focus:border-purple-500 font-mono text-sm"
                />
                {errors.system_prompt && <p className="text-red-400 text-xs mt-1">{errors.system_prompt}</p>}
              </div>
              
              <div className="p-4 bg-blue-900/20 border border-blue-500/30 rounded-lg">
                <h4 className="font-medium text-blue-400 mb-2 flex items-center gap-2">
                  <Info className="w-4 h-4" />
                  Prompt Tips
                </h4>
                <ul className="text-sm text-zinc-300 space-y-1">
                  <li>• Be specific about the output format you want</li>
                  <li>• Include examples if needed</li>
                  <li>• Use template variables like {`{{transcript}}`} for dynamic content</li>
                  <li>• Keep prompts focused on a single task for best results</li>
                </ul>
              </div>
            </div>
          )}

          {activeTab === 'output' && (
            <div className="space-y-6">
              <div>
                <label className="block text-sm font-medium text-zinc-300 mb-2">
                  Output Format
                </label>
                <select
                  value={editedAgent.output_format}
                  onChange={(e) => updateField('output_format', e.target.value)}
                  className="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-white focus:outline-none focus:border-purple-500"
                >
                  {OUTPUT_FORMATS.map(format => (
                    <option key={format.value} value={format.value}>
                      {format.label}
                    </option>
                  ))}
                </select>
              </div>
              
              {editedAgent.output_format === 'json' && (
                <div>
                  <label className="block text-sm font-medium text-zinc-300 mb-2">
                    Output Template (JSON Schema)
                  </label>
                  <textarea
                    value={JSON.stringify(editedAgent.output_template || {}, null, 2)}
                    onChange={(e) => {
                      try {
                        const parsed = JSON.parse(e.target.value);
                        updateField('output_template', parsed);
                      } catch (err) {
                        // Invalid JSON, don't update
                      }
                    }}
                    rows={15}
                    placeholder={`{
  "summary": "string",
  "action_items": ["string"],
  "decisions": ["string"]
}`}
                    className="w-full px-4 py-3 bg-zinc-800 border border-zinc-700 rounded-lg text-white focus:outline-none focus:border-purple-500 font-mono text-sm"
                  />
                </div>
              )}
            </div>
          )}

          {activeTab === 'permissions' && (
            <div className="space-y-6">
              <div>
                <label className="block text-sm font-medium text-zinc-300 mb-2">
                  Permission Level
                </label>
                <div className="space-y-2">
                  {PERMISSION_LEVELS.map(level => (
                    <label
                      key={level.value}
                      className="flex items-center gap-3 p-3 bg-zinc-800 rounded-lg cursor-pointer hover:bg-zinc-700 transition-colors"
                    >
                      <input
                        type="radio"
                        name="permission"
                        value={level.value}
                        checked={editedAgent.permission_level === level.value}
                        onChange={(e) => updateField('permission_level', e.target.value)}
                        className="text-purple-500"
                      />
                      <div>
                        <div className="font-medium text-white">{level.label.split(' - ')[0]}</div>
                        <div className="text-sm text-zinc-400">{level.label.split(' - ')[1]}</div>
                      </div>
                    </label>
                  ))}
                </div>
              </div>
              
              <div>
                <label className="flex items-center gap-3">
                  <input
                    type="checkbox"
                    checked={editedAgent.is_active}
                    onChange={(e) => updateField('is_active', e.target.checked)}
                    className="w-5 h-5 rounded bg-zinc-800 border-zinc-700 text-purple-500"
                  />
                  <div>
                    <span className="font-medium text-white">Agent Active</span>
                    <p className="text-sm text-zinc-400">Enable this agent for use in the system</p>
                  </div>
                </label>
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="p-6 border-t border-zinc-800">
          {saveError && (
            <div className="mb-4 p-3 bg-red-900/50 border border-red-500/30 rounded-lg flex items-center gap-2 text-red-200 text-sm">
              <AlertCircle className="w-4 h-4 shrink-0" />
              {saveError}
            </div>
          )}
          <div className="flex gap-4">
            <button
              onClick={handleSave}
              disabled={saving}
              className="flex-1 flex items-center justify-center gap-2 px-6 py-3 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 text-white rounded-xl font-medium transition-colors disabled:opacity-50"
            >
              <Save className="w-5 h-5" />
              {saving ? 'Saving...' : 'Save Agent'}
            </button>

            <button
              onClick={onCancel}
              className="px-6 py-3 bg-zinc-800 hover:bg-zinc-700 text-zinc-200 rounded-xl font-medium transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};