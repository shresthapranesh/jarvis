import {useEffect, useRef, useState} from 'react';
import type {GraphQLTaggedNode} from 'react-relay';
import {requestSubscription} from 'react-relay';
import type {OperationType} from 'relay-runtime';

import {environment} from '../relay/environment';

export interface RunStreamState {
  streaming: boolean;
  text: string;
  error: string | null;
}

/**
 * The event shape shared by every run-token subscription. `automationRunEvents`
 * and `boardTaskEvents` both resolve the AutomationEvent union server-side (same
 * wire shape — see the board-task subscription resolver), so one reducer covers
 * both; only the response field name differs, which is what `select` supplies.
 */
type RunStreamEvent =
  | {readonly __typename: 'TokenEvent'; readonly text: string}
  | {
      readonly __typename: 'AutomationDoneEvent' | 'AutomationStoppedEvent';
      readonly output?: string | null;
    }
  | {readonly __typename: 'ErrorEvent'; readonly error: string}
  | {readonly __typename: '%other'};

interface Options {
  /**
   * Replace the accumulated text with the terminal event's `output`. Automations
   * do (the persisted output is the canonical result); board tasks don't, since
   * their terminal `summary` is a digest rather than the full reply.
   */
  adoptTerminalOutput?: boolean;
  /** Fires once per terminal event (done / stopped / error). */
  onFinished?: () => void;
}

/**
 * Live token stream for one run. Resubscribes only when `runId` changes —
 * `onFinished` is read through a ref, so passing an inline closure does not
 * tear down and reopen the WebSocket subscription on every render.
 */
export function useRunTokenStream<TSubscription extends OperationType>(
  subscription: GraphQLTaggedNode,
  runId: string | null,
  select: (response: TSubscription['response']) => RunStreamEvent | null | undefined,
  options: Options = {},
): RunStreamState {
  const [state, setState] = useState<RunStreamState>({
    streaming: false,
    text: '',
    error: null,
  });

  const optionsRef = useRef(options);
  optionsRef.current = options;
  const selectRef = useRef(select);
  selectRef.current = select;

  useEffect(() => {
    if (!runId) return;
    setState({streaming: true, text: '', error: null});

    const disposable = requestSubscription<TSubscription>(environment, {
      subscription,
      variables: {runId},
      onNext: (response) => {
        const evt = response ? selectRef.current(response) : null;
        if (!evt) return;

        switch (evt.__typename) {
          case 'TokenEvent':
            setState((s) => ({...s, text: s.text + evt.text}));
            break;
          case 'AutomationDoneEvent':
          case 'AutomationStoppedEvent':
            setState((s) => ({
              ...s,
              text: optionsRef.current.adoptTerminalOutput ? (evt.output ?? s.text) : s.text,
              streaming: false,
            }));
            optionsRef.current.onFinished?.();
            break;
          case 'ErrorEvent':
            setState((s) => ({...s, streaming: false, error: evt.error}));
            optionsRef.current.onFinished?.();
            break;
        }
      },
      onError: (err) => setState((s) => ({...s, streaming: false, error: err.message})),
      onCompleted: () => setState((s) => ({...s, streaming: false})),
    });

    return () => disposable.dispose();
  }, [subscription, runId]);

  return state;
}
