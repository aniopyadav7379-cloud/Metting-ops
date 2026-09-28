import React from 'react';
import { Link } from 'react-router-dom';
import { ListChecks } from 'lucide-react';
import { RecentActionItems } from '../components/dashboard/RecentActionItems';

/** Workspace-level action queue.  A dashboard preview should never be the only
 * way to reach the first-class action-item records across meetings. */
export default function ActionItems() {
  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 lg:px-8">
      <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2 text-fuchsia-200">
            <ListChecks className="h-5 w-5" aria-hidden="true" />
            <span className="text-sm font-medium">Follow-through workspace</span>
          </div>
          <h1 className="mt-2 text-2xl font-semibold text-white">All action items</h1>
          <p className="mt-1 max-w-2xl text-sm text-zinc-400">
            Work captured across every meeting. Hand an item to Project-Ops to track it there — Project-Ops owns its status, not Meeting-Ops — or dismiss it.
          </p>
        </div>
        <Link
          to="/sessions"
          className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 transition hover:border-fuchsia-500/50 hover:text-white"
        >
          View all meetings
        </Link>
      </div>
      <RecentActionItems
        sessions={[]}
        loading={false}
        detailsLoading={false}
        limit={200}
        status=""
        title="All action items"
        description="Send an item to Project-Ops to track it, or dismiss it. Open an item for meeting context, ownership, and Project-Ops lifecycle details."
        showViewAll={false}
      />
    </div>
  );
}
