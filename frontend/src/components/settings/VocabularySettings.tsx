import { useEffect, useState } from 'react';
import { BookOpen, Plus, RefreshCw, Trash2 } from 'lucide-react';
import { config } from '../../config';
import type { SectionProps } from './SettingsContext';

interface VocabTerm {
  id: string;
  term: string;
  expansion: string;
  category: string | null;
  is_active: boolean;
}

export default function VocabularySettings(_props: SectionProps) {
  const [vocabTerms, setVocabTerms] = useState<VocabTerm[]>([]);
  const [vocabLoading, setVocabLoading] = useState(false);
  const [newTerm, setNewTerm] = useState('');
  const [newExpansion, setNewExpansion] = useState('');
  const [newCategory, setNewCategory] = useState('');

  const fetchVocabTerms = async () => {
    setVocabLoading(true);
    try {
      const token = localStorage.getItem('access_token');
      const res = await fetch(`${config.apiUrl}/api/vocabulary/terms?limit=100`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (res.ok) {
        const data = await res.json();
        setVocabTerms(data.items || []);
      }
    } catch (e) {
      console.error('Failed to fetch vocabulary terms:', e);
    } finally {
      setVocabLoading(false);
    }
  };

  const addVocabTerm = async () => {
    if (!newTerm.trim() || !newExpansion.trim()) return;
    try {
      const token = localStorage.getItem('access_token');
      const res = await fetch(`${config.apiUrl}/api/vocabulary/terms`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          term: newTerm.trim(),
          expansion: newExpansion.trim(),
          category: newCategory.trim() || null,
          priority: 0,
        }),
      });
      if (res.ok) {
        setNewTerm('');
        setNewExpansion('');
        setNewCategory('');
        fetchVocabTerms();
      }
    } catch (e) {
      console.error('Failed to add vocabulary term:', e);
    }
  };

  const deleteVocabTerm = async (termId: string) => {
    try {
      const token = localStorage.getItem('access_token');
      await fetch(`${config.apiUrl}/api/vocabulary/terms/${termId}`, {
        method: 'DELETE',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      fetchVocabTerms();
    } catch (e) {
      console.error('Failed to delete vocabulary term:', e);
    }
  };

  useEffect(() => {
    fetchVocabTerms();
  }, []);

  return (
    <div className="space-y-6">
      <div className="bg-indigo-900/20 border border-indigo-800 rounded-lg p-4 mb-4">
        <div className="flex items-start gap-3">
          <BookOpen className="w-5 h-5 text-indigo-400 mt-0.5" />
          <div>
            <p className="text-sm text-indigo-200 font-medium">Custom Vocabulary</p>
            <p className="text-xs text-indigo-300 mt-1">
              Define industry-specific terms, acronyms, and abbreviations. These
              are automatically applied to transcripts after recording.
            </p>
          </div>
        </div>
      </div>

      <div className="space-y-3">
        <h3 className="text-sm font-medium text-zinc-300">Add New Term</h3>
        <div className="grid grid-cols-3 gap-3">
          <input
            type="text"
            value={newTerm}
            onChange={(e) => setNewTerm(e.target.value)}
            placeholder="Term (e.g. K8S)"
            className="bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 text-sm text-zinc-200 placeholder-zinc-500"
          />
          <input
            type="text"
            value={newExpansion}
            onChange={(e) => setNewExpansion(e.target.value)}
            placeholder="Expansion (e.g. Kubernetes)"
            className="bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 text-sm text-zinc-200 placeholder-zinc-500"
          />
          <input
            type="text"
            value={newCategory}
            onChange={(e) => setNewCategory(e.target.value)}
            placeholder="Category (optional)"
            className="bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 text-sm text-zinc-200 placeholder-zinc-500"
          />
        </div>
        <button
          onClick={addVocabTerm}
          disabled={!newTerm.trim() || !newExpansion.trim()}
          className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-sm font-medium flex items-center gap-2 disabled:opacity-50 transition-colors"
        >
          <Plus className="w-4 h-4" />
          Add Term
        </button>
      </div>

      <div>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium text-zinc-300">
            Vocabulary Terms{' '}
            {vocabTerms.length > 0 && `(${vocabTerms.length})`}
          </h3>
          <button
            onClick={fetchVocabTerms}
            className="text-xs text-zinc-400 hover:text-zinc-200 flex items-center gap-1"
          >
            <RefreshCw
              className={`w-3 h-3 ${vocabLoading ? 'animate-spin' : ''}`}
            />
            Refresh
          </button>
        </div>

        {vocabLoading ? (
          <div className="flex items-center justify-center py-8">
            <RefreshCw className="w-6 h-6 text-zinc-500 animate-spin" />
          </div>
        ) : vocabTerms.length === 0 ? (
          <div className="text-center py-8 text-zinc-500 text-sm">
            No vocabulary terms defined yet. Add terms above to improve
            transcription accuracy.
          </div>
        ) : (
          <div className="space-y-2 max-h-96 overflow-y-auto">
            {vocabTerms.map((term) => (
              <div
                key={term.id}
                className="flex items-center justify-between bg-zinc-800/50 border border-zinc-700 rounded-lg px-4 py-3"
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-3">
                    <span className="text-sm font-mono font-medium text-indigo-400">
                      {term.term}
                    </span>
                    <span className="text-zinc-500 text-xs">&rarr;</span>
                    <span className="text-sm text-zinc-200">{term.expansion}</span>
                  </div>
                  {term.category && (
                    <span className="text-xs text-zinc-500 mt-1">
                      {term.category}
                    </span>
                  )}
                </div>
                <button
                  onClick={() => deleteVocabTerm(term.id)}
                  className="ml-3 p-1.5 text-zinc-500 hover:text-red-400 hover:bg-red-900/20 rounded transition-colors"
                  title="Delete term"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
