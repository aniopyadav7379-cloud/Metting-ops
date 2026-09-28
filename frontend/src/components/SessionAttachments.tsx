/**
 * SessionAttachments — drop-zone + list for files attached to a meeting.
 *
 * Mirrors the API in backend/api/session_attachments.py:
 *   GET    /api/simple/recording-sessions/{id}/attachments
 *   POST   /api/simple/recording-sessions/{id}/attachments   (multipart)
 *   GET    /api/simple/recording-sessions/{id}/attachments/{aid}/download
 *   PUT    /api/simple/recording-sessions/{id}/attachments/{aid}
 *   DELETE /api/simple/recording-sessions/{id}/attachments/{aid}
 *
 * Drag-and-drop OR click-to-browse. Files >100MB are rejected client-side
 * with a friendly message; backend re-enforces. Type-filter chips reduce
 * noise on sessions that accumulate many attachments.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Paperclip,
  Upload,
  FileText,
  FileAudio,
  FileImage,
  FileVideo,
  File as FileIcon,
  StickyNote,
  Trash2,
  Download,
  Edit2,
  X,
  AlertTriangle,
} from 'lucide-react';
import { config } from '../config';
import { showConfirm } from '../utils/notifications';

// 100 MB matches the backend MAX_ATTACHMENT_BYTES — keep in sync.
const MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024;

interface Attachment {
  id: string;
  session_id: number;
  filename: string;
  mime_type: string | null;
  size_bytes: number;
  attachment_type: string;
  source_label: string | null;
  notes: string | null;
  uploaded_by_user_id: number | null;
  uploaded_by_username: string | null;
  created_at: string | null;
  storage_backend: string;
}

interface SessionAttachmentsProps {
  sessionPublicId: string | undefined;
}

const TYPE_FILTERS: Array<{ key: string; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'notes', label: 'Notes' },
  { key: 'transcript', label: 'Transcripts' },
  { key: 'document', label: 'Documents' },
  { key: 'audio', label: 'Audio' },
  { key: 'image', label: 'Images' },
  { key: 'video', label: 'Video' },
  { key: 'other', label: 'Other' },
];

const TYPE_OPTIONS = TYPE_FILTERS.filter((t) => t.key !== 'all').map((t) => t.key);

function iconForType(type: string) {
  switch (type) {
    case 'notes':
      return <StickyNote className="w-4 h-4 text-amber-600" />;
    case 'transcript':
      return <FileText className="w-4 h-4 text-blue-600" />;
    case 'document':
      return <FileText className="w-4 h-4 text-purple-600" />;
    case 'audio':
      return <FileAudio className="w-4 h-4 text-green-600" />;
    case 'image':
      return <FileImage className="w-4 h-4 text-pink-600" />;
    case 'video':
      return <FileVideo className="w-4 h-4 text-rose-600" />;
    default:
      return <FileIcon className="w-4 h-4 text-gray-500" />;
  }
}

function formatBytes(n: number): string {
  if (!n || n <= 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(k)), sizes.length - 1);
  return `${(n / Math.pow(k, i)).toFixed(i === 0 ? 0 : 1)} ${sizes[i]}`;
}

function formatDate(iso: string | null): string {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

function defaultTypeForFile(file: File): string {
  const name = (file.name || '').toLowerCase();
  const mt = (file.type || '').toLowerCase();
  if (mt.startsWith('image/')) return 'image';
  if (mt.startsWith('audio/')) return 'audio';
  if (mt.startsWith('video/')) return 'video';
  if (name.endsWith('.txt') || name.endsWith('.md')) return 'notes';
  if (
    name.endsWith('.pdf') ||
    name.endsWith('.docx') ||
    name.endsWith('.doc') ||
    name.endsWith('.pptx') ||
    name.endsWith('.xlsx') ||
    name.endsWith('.csv')
  ) {
    return 'document';
  }
  return 'other';
}

export const SessionAttachments: React.FC<SessionAttachmentsProps> = ({
  sessionPublicId,
}) => {
  const [items, setItems] = useState<Attachment[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>('all');
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<number>(0);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editType, setEditType] = useState('');
  const [editLabel, setEditLabel] = useState('');
  const [editNotes, setEditNotes] = useState('');
  const [pendingType, setPendingType] = useState<string>('document');
  const [pendingLabel, setPendingLabel] = useState<string>('');
  const [dragOver, setDragOver] = useState(false);

  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const baseUrl =
    sessionPublicId &&
    `${config.apiUrl}/api/simple/recording-sessions/${sessionPublicId}/attachments`;

  const fetchAttachments = useCallback(async () => {
    if (!sessionPublicId) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(baseUrl!, { credentials: 'include' });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const rows: Attachment[] = await res.json();
      setItems(rows);
    } catch (e: any) {
      setError(e?.message || 'Failed to load attachments');
    } finally {
      setLoading(false);
    }
  }, [sessionPublicId, baseUrl]);

  useEffect(() => {
    fetchAttachments();
  }, [fetchAttachments]);

  const handleFiles = useCallback(
    async (files: FileList | File[]) => {
      if (!sessionPublicId || !files || files.length === 0) return;
      const arr = Array.from(files);
      for (const file of arr) {
        if (file.size > MAX_ATTACHMENT_BYTES) {
          setError(
            `${file.name} is ${formatBytes(file.size)}. Maximum is 100 MB per file.`,
          );
          continue;
        }

        const form = new FormData();
        form.append('file', file);
        // Default type from mime/extension — user can edit afterward.
        form.append('attachment_type', pendingType || defaultTypeForFile(file));
        if (pendingLabel.trim()) {
          form.append('source_label', pendingLabel.trim());
        }

        setUploading(true);
        setUploadProgress(0);
        try {
          await new Promise<void>((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', baseUrl!);
            xhr.withCredentials = true;
            xhr.upload.onprogress = (evt) => {
              if (evt.lengthComputable) {
                setUploadProgress(Math.round((evt.loaded / evt.total) * 100));
              }
            };
            xhr.onload = () => {
              if (xhr.status >= 200 && xhr.status < 300) {
                try {
                  const row: Attachment = JSON.parse(xhr.responseText);
                  setItems((prev) => [row, ...prev]);
                  resolve();
                } catch (e) {
                  reject(e);
                }
              } else {
                let detail = `HTTP ${xhr.status}`;
                try {
                  const body = JSON.parse(xhr.responseText || '{}');
                  detail = body.detail || detail;
                } catch {
                  // ignore
                }
                reject(new Error(detail));
              }
            };
            xhr.onerror = () => reject(new Error('Network error'));
            xhr.send(form);
          });
        } catch (e: any) {
          setError(e?.message || 'Upload failed');
        } finally {
          setUploading(false);
          setUploadProgress(0);
        }
      }
      // Clear the file-input element so the same file can be re-selected.
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
      // Reset the pending label after a batch — keep the type sticky so
      // a user uploading 4 PDFs in a row doesn't have to set Document
      // four times.
      setPendingLabel('');
    },
    [baseUrl, pendingLabel, pendingType, sessionPublicId],
  );

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      if (e.dataTransfer?.files?.length) {
        handleFiles(e.dataTransfer.files);
      }
    },
    [handleFiles],
  );

  const beginEdit = (att: Attachment) => {
    setEditingId(att.id);
    setEditType(att.attachment_type || 'other');
    setEditLabel(att.source_label || '');
    setEditNotes(att.notes || '');
  };

  const saveEdit = async (id: string) => {
    if (!baseUrl) return;
    setError(null);
    try {
      const res = await fetch(`${baseUrl}/${id}`, {
        method: 'PUT',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          attachment_type: editType || undefined,
          source_label: editLabel || undefined,
          notes: editNotes || undefined,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const updated: Attachment = await res.json();
      setItems((prev) => prev.map((r) => (r.id === id ? updated : r)));
      setEditingId(null);
    } catch (e: any) {
      setError(e?.message || 'Failed to save changes');
    }
  };

  const remove = async (id: string) => {
    if (!baseUrl) return;
    if (!(await showConfirm(
      'Delete this attachment? The file will be removed from storage.',
      { title: 'Delete attachment', confirmLabel: 'Delete' },
    ))) {
      return;
    }
    setError(null);
    try {
      const res = await fetch(`${baseUrl}/${id}`, {
        method: 'DELETE',
        credentials: 'include',
      });
      if (!res.ok && res.status !== 204) {
        throw new Error(`HTTP ${res.status}`);
      }
      setItems((prev) => prev.filter((r) => r.id !== id));
    } catch (e: any) {
      setError(e?.message || 'Delete failed');
    }
  };

  const download = (id: string, filename: string) => {
    if (!baseUrl) return;
    // Anchor + click pattern preserves the Content-Disposition filename.
    const a = document.createElement('a');
    a.href = `${baseUrl}/${id}/download`;
    a.download = filename;
    a.target = '_blank';
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };

  const visible =
    filter === 'all' ? items : items.filter((r) => r.attachment_type === filter);

  return (
    <div className="bg-white rounded-lg shadow-sm p-6">
      <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
        <Paperclip className="w-5 h-5 text-blue-600" />
        Attachments
        {items.length > 0 && (
          <span className="text-sm font-normal text-gray-500">({items.length})</span>
        )}
      </h3>

      {error && (
        <div className="mb-3 rounded border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-700 flex items-start gap-2">
          <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
          <div className="flex-1">{error}</div>
          <button
            onClick={() => setError(null)}
            className="text-red-700 hover:text-red-900"
            aria-label="Dismiss"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      {/* Upload form: type + label + drop zone */}
      <div className="mb-4 space-y-2 border-b border-gray-200 pb-4">
        <div className="flex gap-2 items-center">
          <select
            value={pendingType}
            onChange={(e) => setPendingType(e.target.value)}
            className="rounded border border-gray-300 px-2 py-1 text-xs"
          >
            {TYPE_OPTIONS.map((opt) => (
              <option key={opt} value={opt}>
                {opt}
              </option>
            ))}
          </select>
          <input
            type="text"
            value={pendingLabel}
            onChange={(e) => setPendingLabel(e.target.value)}
            placeholder="Source label (e.g. 'Granola from Mike')"
            className="flex-1 rounded border border-gray-300 px-2 py-1 text-xs"
          />
        </div>
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
          onClick={() => fileInputRef.current?.click()}
          className={`flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed px-4 py-6 text-center cursor-pointer transition ${
            dragOver
              ? 'border-blue-500 bg-blue-50'
              : 'border-gray-300 hover:border-blue-400 hover:bg-gray-50'
          }`}
        >
          <Upload className="w-6 h-6 text-gray-400" />
          <div className="text-xs text-gray-600">
            <span className="font-medium text-blue-600">Click to upload</span> or drag
            and drop a file
          </div>
          <div className="text-[10px] text-gray-400">
            Granola notes, external transcripts, slide decks, photos. Max 100 MB.
          </div>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={(e) => {
              if (e.target.files) handleFiles(e.target.files);
            }}
          />
        </div>
        {uploading && (
          <div className="w-full bg-gray-200 rounded-full h-1.5 overflow-hidden">
            <div
              className="bg-blue-600 h-1.5 transition-all"
              style={{ width: `${uploadProgress}%` }}
            />
          </div>
        )}
      </div>

      {/* Filter chips */}
      {items.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-1">
          {TYPE_FILTERS.map((f) => {
            const count =
              f.key === 'all'
                ? items.length
                : items.filter((r) => r.attachment_type === f.key).length;
            if (f.key !== 'all' && count === 0) return null;
            return (
              <button
                key={f.key}
                onClick={() => setFilter(f.key)}
                className={`rounded-full px-2 py-0.5 text-[11px] font-medium border transition ${
                  filter === f.key
                    ? 'border-blue-500 bg-blue-100 text-blue-700'
                    : 'border-gray-300 bg-white text-gray-600 hover:bg-gray-50'
                }`}
              >
                {f.label} ({count})
              </button>
            );
          })}
        </div>
      )}

      {/* List */}
      {loading && (
        <p className="text-sm text-gray-500 text-center py-2">Loading...</p>
      )}
      {!loading && items.length === 0 && (
        <p className="mb-3 text-sm text-gray-500 text-center py-2">
          No attachments yet. Drop files above to attach Granola notes, external
          transcripts, slide decks, photos of a whiteboard, etc.
        </p>
      )}
      <ul className="space-y-2">
        {visible.map((row) => (
          <li
            key={row.id}
            className="rounded border border-gray-200 px-3 py-2 text-sm"
          >
            {editingId === row.id ? (
              <div className="space-y-2">
                <div className="flex gap-2 items-center">
                  {iconForType(row.attachment_type)}
                  <span className="font-medium text-gray-900 truncate">
                    {row.filename}
                  </span>
                </div>
                <select
                  value={editType}
                  onChange={(e) => setEditType(e.target.value)}
                  className="w-full rounded border border-gray-300 px-2 py-1 text-xs"
                >
                  {TYPE_OPTIONS.map((opt) => (
                    <option key={opt} value={opt}>
                      {opt}
                    </option>
                  ))}
                </select>
                <input
                  type="text"
                  value={editLabel}
                  onChange={(e) => setEditLabel(e.target.value)}
                  placeholder="Source label"
                  className="w-full rounded border border-gray-300 px-2 py-1 text-xs"
                />
                <textarea
                  value={editNotes}
                  onChange={(e) => setEditNotes(e.target.value)}
                  placeholder="Notes"
                  className="w-full rounded border border-gray-300 px-2 py-1 text-xs"
                  rows={2}
                />
                <div className="flex gap-2">
                  <button
                    onClick={() => saveEdit(row.id)}
                    className="rounded bg-blue-600 px-3 py-1 text-xs font-medium text-white hover:bg-blue-700"
                  >
                    Save
                  </button>
                  <button
                    onClick={() => setEditingId(null)}
                    className="rounded border border-gray-300 px-3 py-1 text-xs text-gray-700 hover:bg-gray-100"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-2">
                    {iconForType(row.attachment_type)}
                    <span className="truncate font-medium text-gray-900">
                      {row.filename}
                    </span>
                    <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">
                      {row.attachment_type}
                    </span>
                  </div>
                  {row.source_label && (
                    <p className="truncate text-xs text-gray-700 mt-0.5">
                      {row.source_label}
                    </p>
                  )}
                  {row.notes && (
                    <p className="text-xs text-gray-500 mt-0.5 whitespace-pre-wrap">
                      {row.notes}
                    </p>
                  )}
                  <p className="text-[10px] text-gray-400 mt-1">
                    {formatBytes(row.size_bytes)}
                    {row.uploaded_by_username && ` · ${row.uploaded_by_username}`}
                    {row.created_at && ` · ${formatDate(row.created_at)}`}
                  </p>
                </div>
                <div className="flex shrink-0 gap-1">
                  <button
                    onClick={() => download(row.id, row.filename)}
                    className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-blue-600"
                    title="Download"
                  >
                    <Download className="h-3.5 w-3.5" />
                  </button>
                  <button
                    onClick={() => beginEdit(row)}
                    className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-700"
                    title="Edit metadata"
                  >
                    <Edit2 className="h-3.5 w-3.5" />
                  </button>
                  <button
                    onClick={() => remove(row.id)}
                    className="rounded p-1 text-gray-400 hover:bg-red-50 hover:text-red-600"
                    title="Delete"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
};

export default SessionAttachments;
