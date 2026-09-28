import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { GraduationCap, RefreshCw, Plus } from 'lucide-react';
import { config } from '../config';

interface Lecture {
  session_id: string;
  title: string;
  meeting_date: string | null;
  duration: number;
  status: string;
  course: string | null;
  subject: string | null;
  instructor: string | null;
  lecture_number: number | null;
  concepts: string[];
  extraction_status: string;
}

function formatDate(value: string | null): string {
  if (!value) return '';
  try {
    return new Date(value).toLocaleDateString();
  } catch {
    return value;
  }
}

export default function Lectures() {
  const [lectures, setLectures] = useState<Lecture[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showMarkForm, setShowMarkForm] = useState(false);
  const [markSessionId, setMarkSessionId] = useState('');
  const [markCourse, setMarkCourse] = useState('');
  const [markInstructor, setMarkInstructor] = useState('');
  const [marking, setMarking] = useState(false);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${config.apiBaseUrl}/api/lectures`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setLectures(await res.json());
    } catch (err) {
      setError(`Failed to load lectures: ${(err as Error).message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const markLecture = async () => {
    if (!markSessionId.trim() || marking) return;
    setMarking(true);
    setError(null);
    try {
      const res = await fetch(`${config.apiBaseUrl}/api/lectures/${markSessionId.trim()}/mark`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          course: markCourse.trim() || null,
          instructor: markInstructor.trim() || null,
        }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => null);
        throw new Error(detail?.detail || `HTTP ${res.status}`);
      }
      setMarkSessionId('');
      setMarkCourse('');
      setMarkInstructor('');
      setShowMarkForm(false);
      await load();
    } catch (err) {
      setError(`Failed to mark as lecture: ${(err as Error).message}`);
    } finally {
      setMarking(false);
    }
  };

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 lg:px-8">
      <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2 text-fuchsia-200">
            <GraduationCap className="h-5 w-5" aria-hidden="true" />
            <span className="text-sm font-medium">Lecture intelligence</span>
          </div>
          <h1 className="mt-2 text-2xl font-semibold text-white">Lectures</h1>
          <p className="mt-1 max-w-2xl text-sm text-zinc-400">
            Concepts, definitions, examples, and study notes extracted from recorded lectures — searchable
            the same way as any meeting through Ask AI.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowMarkForm((v) => !v)}
          className="inline-flex items-center gap-2 rounded-lg bg-fuchsia-600 px-3 py-2 text-sm font-medium text-white hover:bg-fuchsia-500"
        >
          <Plus className="h-4 w-4" />
          Mark a meeting as lecture
        </button>
      </div>

      {showMarkForm && (
        <div className="mb-6 space-y-3 rounded-lg border border-zinc-800 bg-zinc-950/50 p-4">
          <p className="text-xs text-zinc-500">
            Enter the session ID of an already-recorded/uploaded session to tag it as a lecture and set its
            course metadata. Run extraction from the lecture's detail view afterward.
          </p>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <input
              value={markSessionId}
              onChange={(e) => setMarkSessionId(e.target.value)}
              placeholder="Session ID"
              aria-label="Session ID"
              className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-fuchsia-500"
            />
            <input
              value={markCourse}
              onChange={(e) => setMarkCourse(e.target.value)}
              placeholder="Course (e.g. CS 231n)"
              aria-label="Course"
              className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-fuchsia-500"
            />
            <input
              value={markInstructor}
              onChange={(e) => setMarkInstructor(e.target.value)}
              placeholder="Instructor"
              aria-label="Instructor"
              className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-fuchsia-500"
            />
          </div>
          <button
            type="button"
            onClick={markLecture}
            disabled={!markSessionId.trim() || marking}
            className="inline-flex items-center gap-2 rounded-lg bg-fuchsia-600 px-3 py-2 text-sm font-medium text-white hover:bg-fuchsia-500 disabled:opacity-50"
          >
            {marking ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
            Mark as lecture
          </button>
        </div>
      )}

      {error && (
        <div className="mb-4 rounded-lg border border-red-500/30 bg-red-950/30 px-3 py-2 text-sm text-red-200">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex items-center gap-2 px-2 py-6 text-sm text-zinc-400">
          <RefreshCw className="h-4 w-4 animate-spin" />
          Loading...
        </div>
      ) : lectures.length === 0 ? (
        <div className="rounded-lg border border-zinc-800 px-4 py-8 text-sm text-zinc-500">
          No lectures yet. Mark a recorded session as a lecture to get started.
        </div>
      ) : (
        <ul className="space-y-2">
          {lectures.map((lecture) => (
            <li
              key={lecture.session_id}
              className="rounded-lg border border-zinc-800 bg-zinc-950/50 px-4 py-3"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <Link to={`/sessions/${lecture.session_id}`} className="font-medium text-zinc-100 hover:text-fuchsia-300">
                  {lecture.title || 'Untitled lecture'}
                </Link>
                {lecture.lecture_number && (
                  <span className="rounded-full border border-zinc-700 px-2 py-0.5 text-xs text-zinc-400">
                    Lecture {lecture.lecture_number}
                  </span>
                )}
              </div>
              <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-zinc-500">
                {lecture.course && <span>{lecture.course}</span>}
                {lecture.instructor && <span>· {lecture.instructor}</span>}
                {lecture.meeting_date && <span>· {formatDate(lecture.meeting_date)}</span>}
                <span>· {lecture.extraction_status}</span>
              </div>
              {lecture.concepts.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1">
                  {lecture.concepts.slice(0, 6).map((c) => (
                    <span key={c} className="rounded-full bg-zinc-800 px-2 py-0.5 text-xs text-zinc-300">
                      {c}
                    </span>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
