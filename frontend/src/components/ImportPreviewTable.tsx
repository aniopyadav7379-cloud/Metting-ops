import { useMemo, useState, useCallback } from 'react';
import { Search, ArrowUpDown, UserCheck, Calendar, AlertTriangle } from 'lucide-react';
import { extractCallWithName } from '../pages/Import';
import type { StagedFile } from '../pages/Import';

interface Props {
  staged: StagedFile[];
  onUpdateOverride: (index: number, field: string, value: string) => void;
  onToggleSelected: (index: number) => void;
  onSelectAll: (selected: boolean) => void;
  onBulkSetParticipant: (participant: string, indices: Set<number>) => void;
  onBulkSetDate: (date: string, indices: Set<number>) => void;
  onDeselectLowConfidence: (threshold: number) => void;
  onBack: () => void;
  onNext: () => void;
}

type SortKey = 'filename' | 'date' | 'confidence';
type FilterMode = 'all' | 'low_confidence' | 'no_participant';

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function confidenceColor(c: number): string {
  if (c >= 0.9) return 'bg-emerald-500/20 text-emerald-400';
  if (c >= 0.5) return 'bg-amber-500/20 text-amber-400';
  return 'bg-red-500/20 text-red-400';
}

function confidenceLabel(c: number): string {
  if (c >= 0.9) return 'high';
  if (c >= 0.5) return 'medium';
  return 'low';
}

function estimatedDurationBytes(bytes: number): { hours: number; minutes: number } {
  // Rough estimate: 1 min of audio ~= 1 MB for m4a at 128kbps
  const minutes = bytes / (1024 * 1024);
  return { hours: Math.floor(minutes / 60), minutes: Math.round(minutes % 60) };
}

export default function ImportPreviewTable({
  staged, onUpdateOverride, onToggleSelected, onSelectAll,
  onBulkSetParticipant, onBulkSetDate, onDeselectLowConfidence,
  onBack, onNext,
}: Props) {
  const [sortKey, setSortKey] = useState<SortKey>('filename');
  const [sortAsc, setSortAsc] = useState(true);
  const [filterMode, setFilterMode] = useState<FilterMode>('all');
  const [searchQuery, setSearchQuery] = useState('');
  const [bulkParticipant, setBulkParticipant] = useState('');
  const [bulkDate, setBulkDate] = useState('');
  const [showBulk, setShowBulk] = useState(false);

  const sorted = useMemo(() => {
    let list = [...staged];
    if (filterMode === 'low_confidence') {
      list = list.filter((f) => f.parsed.confidence < 0.5);
    } else if (filterMode === 'no_participant') {
      list = list.filter((f) => !extractCallWithName(f.overrides.title || f.parsed.title));
    }
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      list = list.filter(
        (f) =>
          f.file.name.toLowerCase().includes(q) ||
          (f.overrides.title || '').toLowerCase().includes(q),
      );
    }
    list.sort((a, b) => {
      let cmp = 0;
      if (sortKey === 'filename') cmp = a.file.name.localeCompare(b.file.name);
      else if (sortKey === 'date') {
        const da = a.overrides.meetingDate || a.parsed.meetingDate || '';
        const db = b.overrides.meetingDate || b.parsed.meetingDate || '';
        cmp = da.localeCompare(db);
      } else if (sortKey === 'confidence') {
        cmp = (a.parsed.confidence ?? 0) - (b.parsed.confidence ?? 0);
      }
      return sortAsc ? cmp : -cmp;
    });
    return list;
  }, [staged, sortKey, sortAsc, filterMode, searchQuery]);

  const allSelected = staged.length > 0 && staged.every((f) => f.selected);
  const selectedIndices = new Set(
    staged.map((f, i) => (f.selected ? i : -1)).filter((i) => i >= 0),
  );
  const selectedCount = selectedIndices.size;

  const totalBytes = staged.reduce((s, f) => s + f.file.size, 0);
  const est = estimatedDurationBytes(totalBytes);
  const estProcessingMin = Math.max(1, Math.round(est.minutes / 2)); // ~2x realtime at concurrency=2

  const handleSort = useCallback(
    (key: SortKey) => {
      if (sortKey === key) setSortAsc(!sortAsc);
      else { setSortKey(key); setSortAsc(true); }
    },
    [sortKey, sortAsc],
  );

  const handleApplyParticipant = useCallback(() => {
    if (!bulkParticipant.trim()) return;
    onBulkSetParticipant(bulkParticipant.trim(), selectedIndices);
    setBulkParticipant('');
  }, [bulkParticipant, selectedIndices, onBulkSetParticipant]);

  const handleApplyDate = useCallback(() => {
    if (!bulkDate) return;
    onBulkSetDate(bulkDate, selectedIndices);
    setBulkDate('');
  }, [bulkDate, selectedIndices, onBulkSetDate]);

  return (
    <div className="space-y-4">
      {/* Estimate bar */}
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3 text-sm text-zinc-300">
        {staged.length} files, ~{est.hours > 0 ? `${est.hours}h ` : ''}{est.minutes}m of audio,
        ~{estProcessingMin} min to process at concurrency=2
        {selectedCount < staged.length && (
          <span className="ml-2 text-zinc-500">
            ({selectedCount} selected)
          </span>
        )}
      </div>

      {/* Filter + sort toolbar */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
          <input
            type="text"
            placeholder="Search files..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full rounded-lg border border-zinc-700 bg-zinc-900 py-2 pl-9 pr-3 text-sm text-zinc-100 placeholder-zinc-500 focus:border-fuchsia-500 focus:outline-none"
          />
        </div>
        <select
          value={filterMode}
          onChange={(e) => setFilterMode(e.target.value as FilterMode)}
          className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100"
        >
          <option value="all">All files</option>
          <option value="low_confidence">Low confidence only</option>
          <option value="no_participant">Missing participant</option>
        </select>
        <div className="flex gap-1">
          {(['filename', 'date', 'confidence'] as const).map((key) => (
            <button
              key={key}
              onClick={() => handleSort(key)}
              className={`flex items-center gap-1 rounded-lg border px-3 py-2 text-xs ${
                sortKey === key
                  ? 'border-fuchsia-600 bg-fuchsia-950/40 text-fuchsia-300'
                  : 'border-zinc-700 bg-zinc-900 text-zinc-400 hover:border-zinc-500'
              }`}
            >
              <ArrowUpDown className="h-3 w-3" />
              {key}
            </button>
          ))}
        </div>
        <button
          onClick={() => setShowBulk(!showBulk)}
          className={`rounded-lg border px-3 py-2 text-xs ${
            showBulk
              ? 'border-fuchsia-600 bg-fuchsia-950/40 text-fuchsia-300'
              : 'border-zinc-700 bg-zinc-900 text-zinc-400 hover:border-zinc-500'
          }`}
        >
          Bulk edit
        </button>
      </div>

      {/* Bulk-edit toolbar */}
      {showBulk && (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/70 p-4">
          <div className="flex flex-wrap items-end gap-4">
            <div>
              <label className="mb-1 block text-xs text-zinc-500">
                Apply participant to {selectedCount} selected row(s)
              </label>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={bulkParticipant}
                  onChange={(e) => setBulkParticipant(e.target.value)}
                  placeholder="e.g. Jason Allen"
                  className="w-48 rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-fuchsia-500 focus:outline-none"
                />
                <button
                  onClick={handleApplyParticipant}
                  disabled={!bulkParticipant.trim() || selectedCount === 0}
                  className="flex items-center gap-1 rounded-lg bg-fuchsia-700 px-3 py-2 text-xs text-white hover:bg-fuchsia-600 disabled:opacity-40"
                >
                  <UserCheck className="h-3 w-3" />
                  Apply
                </button>
              </div>
            </div>
            <div>
              <label className="mb-1 block text-xs text-zinc-500">
                Set date for {selectedCount} selected row(s)
              </label>
              <div className="flex gap-2">
                <input
                  type="date"
                  value={bulkDate}
                  onChange={(e) => setBulkDate(e.target.value)}
                  className="rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 focus:border-fuchsia-500 focus:outline-none"
                />
                <button
                  onClick={handleApplyDate}
                  disabled={!bulkDate || selectedCount === 0}
                  className="flex items-center gap-1 rounded-lg bg-fuchsia-700 px-3 py-2 text-xs text-white hover:bg-fuchsia-600 disabled:opacity-40"
                >
                  <Calendar className="h-3 w-3" />
                  Set
                </button>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => onDeselectLowConfidence(0.5)}
                className="flex items-center gap-1 rounded-lg border border-zinc-700 px-3 py-2 text-xs text-zinc-400 hover:border-zinc-500"
              >
                <AlertTriangle className="h-3 w-3" />
                Deselect low confidence
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Table */}
      <div className="overflow-x-auto rounded-lg border border-zinc-800">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-zinc-800 bg-zinc-900/80 text-xs uppercase text-zinc-500">
            <tr>
              <th className="w-10 px-3 py-3">
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={() => onSelectAll(!allSelected)}
                  className="h-4 w-4 rounded border-zinc-600 bg-zinc-800 text-fuchsia-600 focus:ring-fuchsia-500"
                />
              </th>
              <th className="px-3 py-3">Filename</th>
              <th className="px-3 py-3">Title</th>
              <th className="px-3 py-3">Date</th>
              <th className="px-3 py-3">Time</th>
              <th className="px-3 py-3">Participant</th>
              <th className="px-3 py-3">Confidence</th>
              <th className="px-3 py-3 text-right">Size</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800">
            {sorted.map((sf, i) => {
              const realIndex = staged.indexOf(sf);
              const hint =
                sf.overrides.participantHint ||
                extractCallWithName(sf.overrides.title || sf.parsed.title);
              return (
                <tr
                  key={sf.file.name + ':' + sf.file.size}
                  className={`transition-colors hover:bg-zinc-800/40 ${
                    !sf.selected ? 'opacity-50' : ''
                  }`}
                >
                  <td className="px-3 py-2.5">
                    <input
                      type="checkbox"
                      checked={sf.selected}
                      onChange={() => onToggleSelected(realIndex)}
                      className="h-4 w-4 rounded border-zinc-600 bg-zinc-800 text-fuchsia-600 focus:ring-fuchsia-500"
                    />
                  </td>
                  <td
                    className="max-w-56 truncate px-3 py-2.5 text-zinc-300"
                    title={sf.file.name}
                  >
                    {sf.file.name}
                  </td>
                  <td className="px-3 py-2.5">
                    <input
                      type="text"
                      value={sf.overrides.title}
                      onChange={(e) =>
                        onUpdateOverride(realIndex, 'title', e.target.value)
                      }
                      className="w-44 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-sm text-zinc-100 focus:border-fuchsia-500 focus:outline-none"
                    />
                  </td>
                  <td className="px-3 py-2.5">
                    <input
                      type="date"
                      value={
                        sf.overrides.meetingDate || ''
                      }
                      onChange={(e) =>
                        onUpdateOverride(
                          realIndex,
                          'meetingDate',
                          e.target.value,
                        )
                      }
                      className="w-36 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-sm text-zinc-100 focus:border-fuchsia-500 focus:outline-none"
                    />
                  </td>
                  <td className="px-3 py-2.5">
                    <input
                      type="time"
                      value={
                        sf.overrides.meetingTime || ''
                      }
                      onChange={(e) =>
                        onUpdateOverride(
                          realIndex,
                          'meetingTime',
                          e.target.value,
                        )
                      }
                      className="w-28 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-sm text-zinc-100 focus:border-fuchsia-500 focus:outline-none"
                    />
                  </td>
                  <td className="px-3 py-2.5">
                    <input
                      type="text"
                      value={hint}
                      placeholder="Auto from title"
                      onChange={(e) =>
                        onUpdateOverride(realIndex, 'participantHint', e.target.value)
                      }
                      className="w-32 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-sm text-zinc-100 placeholder-zinc-600 focus:border-fuchsia-500 focus:outline-none"
                    />
                  </td>
                  <td className="px-3 py-2.5">
                    <span
                      className={`inline-block rounded-full px-2 py-0.5 text-[11px] font-medium ${confidenceColor(
                        sf.parsed.confidence,
                      )}`}
                    >
                      {confidenceLabel(sf.parsed.confidence)} (
                      {Math.round(sf.parsed.confidence * 100)}%)
                    </span>
                  </td>
                  <td className="px-3 py-2.5 text-right text-zinc-400">
                    {formatBytes(sf.file.size)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {sorted.length === 0 && (
          <div className="px-4 py-8 text-center text-sm text-zinc-500">
            No files match the current filter.
          </div>
        )}
      </div>

      {/* Navigation */}
      <div className="flex items-center justify-between">
        <button
          onClick={onBack}
          className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800"
        >
          Back to file picker
        </button>
        <button
          onClick={onNext}
          disabled={selectedCount === 0}
          className="rounded-lg bg-fuchsia-600 px-5 py-2 text-sm font-medium text-white hover:bg-fuchsia-500 disabled:opacity-50"
        >
          Confirm {selectedCount > 0 ? `(${selectedCount})` : ''}
        </button>
      </div>
    </div>
  );
}
