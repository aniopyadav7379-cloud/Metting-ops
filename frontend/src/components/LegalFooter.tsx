/**
 * Global legal footer. Subtle gray, single line on desktop, wraps
 * cleanly on mobile. Used on:
 *   - The authed AppLayout shell (so every page exposes legal links).
 *   - The unauth marketing surfaces (Landing, Signup, Login, Pricing).
 *   - The legal pages themselves (Terms, Privacy, AUP).
 *
 * Uses hash-router links (`#/...`) because the app is mounted under
 * HashRouter. Plain `<a>` rather than `<Link>` so this component is
 * safe to drop anywhere (including the authed AppLayout that may
 * render before a Router boundary in some test surfaces).
 */
import React from 'react';

export const LegalFooter: React.FC = () => {
  const year = new Date().getFullYear();
  return (
    <footer className="mt-12 border-t border-zinc-800/60 px-4 py-4 text-center text-xs text-zinc-500">
      <nav
        aria-label="Legal"
        className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1"
      >
        <a
          href="#/terms"
          className="text-zinc-400 underline-offset-4 transition-colors hover:text-fuchsia-300 hover:underline"
        >
          Terms
        </a>
        <span aria-hidden="true">&middot;</span>
        <a
          href="#/privacy"
          className="text-zinc-400 underline-offset-4 transition-colors hover:text-fuchsia-300 hover:underline"
        >
          Privacy
        </a>
        <span aria-hidden="true">&middot;</span>
        <a
          href="#/aup"
          className="text-zinc-400 underline-offset-4 transition-colors hover:text-fuchsia-300 hover:underline"
        >
          Acceptable Use
        </a>
        <span aria-hidden="true">&middot;</span>
        <a
          href="#/contact"
          className="text-zinc-400 underline-offset-4 transition-colors hover:text-fuchsia-300 hover:underline"
        >
          Contact Support
        </a>
        <span aria-hidden="true">&middot;</span>
        <span>
          &copy; {year} Magic Unicorn Unconventional Technology &amp; Stuff Inc.
        </span>
      </nav>
    </footer>
  );
};

export default LegalFooter;
