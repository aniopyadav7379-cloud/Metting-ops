/**
 * Shared shell for legal pages (/terms, /privacy, /aup).
 *
 * Renders public, unauthenticated pages with a sober dark-theme
 * layout: header strip with brand mark + back-to-app link, a
 * prose-width main column, effective-date + version metadata at
 * top, and the global LegalFooter at the bottom.
 *
 * No analytics. No tracking. No third-party fonts. Pure read-only
 * legal text. The children typically use a sequence of <section>
 * blocks with numbered <h2> headings so the document can be cited
 * by section number (e.g. "Section 7.2 of Terms").
 */
import React from 'react';
import { Link } from 'react-router-dom';
import { Sparkles } from 'lucide-react';
import LegalFooter from './LegalFooter';

export interface LegalPageProps {
  title: string;
  effectiveDate: string;
  version: string;
  children: React.ReactNode;
}

export const LegalPage: React.FC<LegalPageProps> = ({
  title,
  effectiveDate,
  version,
  children,
}) => {
  return (
    <div className="min-h-screen bg-gradient-to-b from-zinc-950 to-black text-zinc-100">
      <div className="mx-auto flex min-h-screen w-full max-w-4xl flex-col px-4 py-6 sm:px-8 lg:px-10">
        <header className="flex items-center justify-between border-b border-white/10 pb-4">
          <Link to="/" className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-fuchsia-500 to-violet-500 shadow-lg shadow-fuchsia-500/20">
              <Sparkles className="h-5 w-5 text-white" />
            </div>
            <div>
              <div className="text-sm font-semibold tracking-tight text-white">
                Meeting-Ops
              </div>
              <div className="text-xs text-zinc-400">
                Magic Unicorn Unconventional Technology &amp; Stuff Inc.
              </div>
            </div>
          </Link>
          <Link
            to="/"
            className="rounded-full border border-white/10 px-3 py-2 text-sm text-zinc-300 transition hover:border-fuchsia-400/40 hover:text-white"
          >
            Back to app
          </Link>
        </header>

        <main className="flex-1 py-10">
          <h1 className="text-3xl font-semibold tracking-tight text-white sm:text-4xl">
            {title}
          </h1>
          <p className="mt-3 text-sm text-zinc-400">
            Effective {effectiveDate} &middot; Version {version}
          </p>

          <article className="prose prose-invert mt-10 max-w-none text-zinc-300 [&_a]:text-fuchsia-300 [&_a:hover]:text-fuchsia-200 [&_h2]:mt-12 [&_h2]:text-xl [&_h2]:font-semibold [&_h2]:text-white [&_h3]:mt-8 [&_h3]:text-base [&_h3]:font-semibold [&_h3]:text-zinc-100 [&_p]:mt-4 [&_p]:text-sm [&_p]:leading-7 [&_ul]:mt-4 [&_ul]:list-disc [&_ul]:pl-6 [&_ul]:text-sm [&_ul]:leading-7 [&_li]:mt-1 [&_strong]:text-white">
            {children}
          </article>
        </main>

        <LegalFooter />
      </div>
    </div>
  );
};

export default LegalPage;
