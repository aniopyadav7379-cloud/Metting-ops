import React from 'react';
import { track } from '../utils/posthog';

type Props = {
  children: React.ReactNode;
  scope?: 'app' | 'page';
  resetKey?: string;
};

type State = { error: Error | null };

export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error('[meeting-ops] React render error', error, info.componentStack);
    track('frontend_render_error', {
      scope: this.props.scope ?? 'page',
      message: error.message,
      component_stack: info.componentStack,
    });
  }

  componentDidUpdate(previous: Props) {
    if (this.state.error && previous.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  private reload = () => window.location.reload();

  private reset = () => {
    try {
      window.localStorage?.clear();
    } catch { /* storage may be disabled */ }
    try {
      window.sessionStorage?.clear();
    } catch { /* storage may be disabled */ }
    window.location.reload();
  };

  render() {
    if (!this.state.error) return this.props.children;

    return (
      <section
        role="alert"
        className="flex min-h-[50vh] items-center justify-center bg-zinc-950 px-6 text-zinc-100"
      >
        <div className="max-w-lg rounded-2xl border border-zinc-800 bg-zinc-900 p-8 text-center shadow-xl">
          <div className="mb-3 text-3xl" aria-hidden="true">⚠️</div>
          <h1 className="text-xl font-semibold">Something went wrong</h1>
          <p className="mt-2 text-sm text-zinc-400">
            Reload the page to try again. If the problem persists, reset this device&apos;s app data.
          </p>
          <div className="mt-6 flex flex-wrap justify-center gap-3">
            <button className="rounded-lg bg-violet-600 px-4 py-2 font-medium hover:bg-violet-500" onClick={this.reload}>
              Reload
            </button>
            <button className="rounded-lg border border-zinc-700 px-4 py-2 text-zinc-300 hover:bg-zinc-800" onClick={this.reset}>
              Reset app data
            </button>
          </div>
        </div>
      </section>
    );
  }
}
