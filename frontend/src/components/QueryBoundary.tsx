import {Component, createContext, useContext, useState, type ReactNode} from 'react';
import {Suspense} from 'react';

/**
 * Retry counter published to the subtree. Screens must feed it into their
 * query's `fetchKey`:
 *
 *   useLazyLoadQuery(query, vars, {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()})
 *
 * Relay caches a failed query by (fetchPolicy, variables, fetchKey) and stores
 * the *error* as that entry's value, so remounting alone re-throws the cached
 * failure — only a changed fetchKey breaks the entry and hits the network.
 */
const RetryContext = createContext(0);

export function useQueryRetry(): number | undefined {
  const attempt = useContext(RetryContext);
  // Leave the first render keyless so the cache entry matches other screens
  // reading the same query.
  return attempt === 0 ? undefined : attempt;
}

interface ErrorBoundaryProps {
  children: ReactNode;
  fallback: (error: Error) => ReactNode;
}

class ErrorBoundary extends Component<ErrorBoundaryProps, {error: Error | null}> {
  state: {error: Error | null} = {error: null};

  static getDerivedStateFromError(error: Error) {
    return {error};
  }

  render() {
    if (this.state.error) return this.props.fallback(this.state.error);
    return this.props.children;
  }
}

interface Props {
  children: ReactNode;
  /** Rendered while the query suspends. */
  fallback?: ReactNode;
  /** Prefixes the error message, e.g. "Failed to load memory". */
  label?: string;
}

/**
 * Suspense + error handling for a Relay-backed screen.
 *
 * `useLazyLoadQuery` suspends on first load and *throws* on network failure, so
 * an unguarded screen turns a failed fetch into a blank app. Wrap the screen at
 * the route.
 */
export function QueryBoundary({children, fallback, label = 'Failed to load'}: Props) {
  const [attempt, setAttempt] = useState(0);

  return (
    <RetryContext.Provider value={attempt}>
      {/* Remounting on retry resets the boundary's captured error; the new
          `attempt` reaches the query as a fresh fetchKey. */}
      <ErrorBoundary
        key={attempt}
        fallback={(error) => (
          <div className="query-boundary-error">
            <p>
              {label}: {error.message}
            </p>
            <button className="artifact-btn" onClick={() => setAttempt((n) => n + 1)}>
              Retry
            </button>
          </div>
        )}
      >
        <Suspense fallback={fallback ?? <div className="query-boundary-loading">Loading…</div>}>
          {children}
        </Suspense>
      </ErrorBoundary>
    </RetryContext.Provider>
  );
}
