import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  /** Page / surface identifier surfaced in the fallback + console. */
  scope: string;
  children: ReactNode;
  /** Optional render override; default surface is good enough for app-shell. */
  fallback?: (error: Error, reset: () => void) => ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * Per-route render-error boundary.
 *
 * A render-time throw inside any descendant — bad API shape, divide by zero,
 * a hook returning undefined — is caught here and surfaces as a friendly
 * card. Without this, one bad chart white-screens the entire app: React
 * unmounts the whole tree on an uncaught throw.
 *
 * Reset is exposed so users can recover without a hard reload (clears the
 * error state so the next render re-mounts the child). The instance is
 * keyed by ``location.key`` at the route boundary so navigating to another
 * page also clears the state.
 */
export default class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Console is the only sink we can rely on in the browser; if a remote
    // logger is added later, wire it here.
    // eslint-disable-next-line no-console
    console.error(`[ErrorBoundary:${this.props.scope}]`, error, info);
  }

  private reset = () => this.setState({ error: null });

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    if (this.props.fallback) return this.props.fallback(error, this.reset);

    return (
      <div className="m-6 p-6 border border-danger-200 bg-danger-50 rounded-lg max-w-3xl">
        <h2 className="text-lg font-semibold text-danger-700 mb-2">
          Something went wrong on this page
        </h2>
        <p className="text-body text-text-secondary mb-3">
          The {this.props.scope} surface hit an unexpected error. The rest of the app is still
          usable — switch tabs or click Retry below.
        </p>
        <pre className="text-caption font-mono bg-surface-raised border border-default rounded p-3 overflow-auto max-h-48 mb-4">
          {error.message || error.name}
        </pre>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={this.reset}
            className="px-4 py-2 rounded-lg bg-brand-500 text-white hover:bg-brand-600 transition-colors"
          >
            Retry
          </button>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="px-4 py-2 rounded-lg border border-default hover:bg-surface-sunken transition-colors"
          >
            Hard reload
          </button>
        </div>
      </div>
    );
  }
}
